"""Authenticated callers enter this resource gateway before device execution."""

from __future__ import annotations

import copy
import base64
import time
import uuid
from dataclasses import dataclass, field
from threading import Event, RLock, Thread

from jsonschema import Draft202012Validator
from metaos_contracts import ActionValidationAPI, ArtifactAPI, CommandRequest, LeaseGrant, StateSample, SystemError, TERMINAL

from .composition import fingerprint
from .store import Store


@dataclass
class Active:
    request: CommandRequest
    owner: str
    record: dict
    digest: str
    cancel_reason: str | None = None
    done: Event = field(default_factory=Event)
    thread: Thread | None = None
    last_feedback_at: float = 0.


@dataclass
class Slot:
    device: object
    descriptor: object
    lock: RLock = field(default_factory=RLock)
    lease: LeaseGrant | None = None
    active: Active | None = None
    fault: bool = False


class Gateway:
    tick_s = 0.01

    def __init__(self, resources, workspace):
        self.clock_id = "monotonic:" + uuid.uuid4().hex
        self.store = Store(workspace)
        self.slots = {name: Slot(device, device.describe_resource()) for name, device in resources.items()}
        self.identity_lock = RLock()
        self.closing = Event()
        self.suspended = Event()
        self.suspension_reason = None
        self.workers = []
        for slot in self.slots.values():
            # Never reuse fencing values across clean gateway restarts.
            self.store.epoch(slot.descriptor.resource_id)
            slot.fault = slot.descriptor.resource_id in self.store.faults()
        self.watchdog = Thread(target=self._watch, name="metaos-lease-watchdog", daemon=True)
        self.watchdog.start()
        self.store.event("gateway_started", clock_id=self.clock_id, resources=list(resources))

    def info(self):
        return {"api": "metaos.system/v1", "clock_id": self.clock_id, "now_ns": time.monotonic_ns(),
                "backend": "local", "device_execution": "cooperative_in_gateway"}

    def _slot(self, resource):
        if resource not in self.slots:
            raise SystemError("resource_not_found", "Resource is not exported", 404)
        return self.slots[resource]

    def resources(self):
        return [slot.descriptor.model_dump(mode="json") for slot in self.slots.values()]

    def artifacts(self, resource):
        device = self._slot(resource).device
        if not isinstance(device, ArtifactAPI):
            raise SystemError("unsupported_capability", "Resource has no artifact channel", 400)
        return device.list_artifacts()

    def artifact_chunk(self, resource, artifact_id, offset, length):
        if type(offset) is not int or type(length) is not int or offset < 0 or not 0 < length <= 1048576:
            raise SystemError("invalid_request", "Expected a nonnegative offset and at most 1 MiB", 400)
        info = next((x for x in self.artifacts(resource) if x["artifact_id"] == artifact_id), None)
        if info is None:
            raise SystemError("artifact_not_found", "Artifact is unavailable in this session", 404)
        data = self._slot(resource).device.read_artifact(artifact_id, offset, length)
        return {"artifact": info, "offset": offset, "data_base64": base64.b64encode(data).decode("ascii")}

    def state(self, resource):
        slot = self._slot(resource)
        with slot.lock:
            sample = slot.device.read_state(self.clock_id)
            if not isinstance(sample, StateSample) or sample.resource_id != resource:
                raise SystemError("invalid_state", "Device returned an invalid state", 503)
            return {**sample.model_dump(mode="json"), "gateway_fault": slot.fault}

    def _expire(self, slot):
        if slot.lease is not None and time.monotonic_ns() >= slot.lease.expires_at_ns:
            if slot.active is not None and slot.active.cancel_reason is None:
                slot.active.cancel_reason = "lease_expired"
            self.store.event("lease_expired", resource_id=slot.descriptor.resource_id,
                             control_epoch=slot.lease.control_epoch)
            slot.lease = None

    def _lease(self, slot, owner, reference):
        self._expire(slot)
        grant = slot.lease
        if grant is None or grant.lease_id != reference.lease_id or grant.control_epoch != reference.control_epoch:
            raise SystemError("stale_lease", "Lease is expired, revoked or from an old control epoch")
        if grant.owner != owner:
            raise SystemError("forbidden", "Lease belongs to another authenticated caller", 403)
        return grant

    def acquire(self, owner, request):
        slot = self._slot(request.resource_id)
        with slot.lock:
            if self.closing.is_set() or self.suspended.is_set():
                raise SystemError("shutting_down", "Gateway is shutting down", 503)
            self._expire(slot)
            if slot.fault:
                raise SystemError("resource_fault", "Resource requires operator reconciliation", 503)
            if slot.lease is not None or slot.active is not None:
                raise SystemError("resource_busy", "Resource is leased or has an action awaiting stop confirmation")
            grant = LeaseGrant(resource_id=request.resource_id, owner=owner,
                lease_id=uuid.uuid4().hex, control_epoch=self.store.epoch(request.resource_id),
                clock_id=self.clock_id, expires_at_ns=time.monotonic_ns() + int(request.ttl_s * 1e9))
            slot.lease = grant
            self.store.event("lease_acquired", **grant.model_dump(mode="json"))
            return grant.model_dump(mode="json")

    def renew(self, owner, reference, ttl_s):
        slot = self._slot(reference.resource_id)
        with slot.lock:
            if self.closing.is_set() or self.suspended.is_set():
                raise SystemError("shutting_down", "Gateway is shutting down", 503)
            grant = self._lease(slot, owner, reference)
            slot.lease = grant.model_copy(update={"expires_at_ns": time.monotonic_ns() + int(ttl_s * 1e9)})
            return slot.lease.model_dump(mode="json")

    def release(self, owner, reference):
        slot = self._slot(reference.resource_id)
        with slot.lock:
            grant = self._lease(slot, owner, reference)
            active = slot.active
            slot.lease = None
            if active is not None and active.cancel_reason is None:
                active.cancel_reason = "lease_released"
            self.store.event("lease_released", resource_id=reference.resource_id, control_epoch=grant.control_epoch)
        if active is not None and not active.done.wait(2.0):
            raise SystemError("stop_unconfirmed", "Stop confirmation was not received", 503)
        if slot.fault:
            raise SystemError("resource_fault", "Resource requires operator reconciliation", 503)
        return {"released": True, "stop_confirmed": active is None or active.record["status"] != "outcome_unknown"}

    def _validate(self, slot, owner, request):
        if self.closing.is_set() or self.suspended.is_set():
            raise SystemError("shutting_down", "Gateway is shutting down", 503)
        if slot.fault:
            raise SystemError("resource_fault", "Resource requires operator reconciliation", 503)
        self._lease(slot, owner, request)
        now = time.monotonic_ns()
        if request.clock_id != self.clock_id:
            raise SystemError("clock_mismatch", "Command uses a different clock domain", 400)
        cap = next((c for c in slot.descriptor.capabilities if c.id == request.capability), None)
        if cap is None:
            raise SystemError("unsupported_capability", "Resource does not provide this capability", 400)
        if request.not_after_ns <= now or request.not_after_ns > now + int(cap.max_command_validity_s * 1e9):
            raise SystemError("command_expired", "Command validity is expired or exceeds the capability's declared horizon", 400)
        if request.units != cap.units or request.frame_id != cap.frame_id:
            raise SystemError("semantic_mismatch", "Command units or coordinate frame do not match", 400)
        if not Draft202012Validator(cap.parameters).is_valid(request.arguments):
            raise SystemError("invalid_action", "Command arguments do not match the capability schema", 400)
        state = slot.device.read_state(self.clock_id)
        if not isinstance(state, StateSample) or state.resource_id != request.resource_id:
            raise SystemError("invalid_state", "Device returned invalid state", 503)
        age_s = (time.monotonic_ns() - state.sampled_at_ns) / 1e9
        if not state.valid or state.clock_id != self.clock_id or age_s > cap.max_state_age_s or age_s < 0:
            raise SystemError("stale_state", "Current device state is invalid or not fresh", 409)
        if state.frame_id != cap.frame_id or request.state_sequence != state.sequence:
            raise SystemError("state_changed", "Command state basis does not match current device state", 409)
        if isinstance(slot.device, ActionValidationAPI):
            slot.device.validate_action(request.capability, request.arguments)

    def submit(self, owner, request):
        digest = fingerprint(request.model_dump(mode="json"))
        with self.identity_lock:
            previous = self.store.lookup(request.command_id)
            if previous is not None:
                if previous[0] != owner:
                    raise SystemError("forbidden", "Command belongs to another caller", 403)
                if previous[1] != digest:
                    raise SystemError("command_conflict", "Command id already refers to a different request")
                return previous[2]
            slot = self._slot(request.resource_id)
            with slot.lock:
                self._validate(slot, owner, request)
                if slot.active is not None:
                    raise SystemError("resource_busy", "Resource already has an active action")
                now = time.monotonic_ns()
                record = {"command_id": request.command_id, "resource_id": request.resource_id,
                          "owner": owner, "status": "accepted", "reason": None, "feedback": {}, "result": {},
                          "created_at_ns": now, "updated_at_ns": now, "clock_id": self.clock_id}
                active = Active(request, owner, record, digest)
                self.store.save(record, digest)
                self.store.event("command_accepted", **record)
                slot.active = active
                active.thread = Thread(target=self._execute, args=(slot, active),
                                       name="metaos-command-" + request.command_id, daemon=True)
                self.workers.append(active.thread)
                active.thread.start()
                return copy.deepcopy(record)

    def _publish(self, active, status=None, reason=None, **values):
        if status:
            active.record["status"] = status
        if reason:
            active.record["reason"] = reason
        active.record.update(values, updated_at_ns=time.monotonic_ns())
        self.store.save(active.record, active.digest)
        self.store.event("command_update", **active.record)

    def _stop(self, slot, active, reason, status="cancelled"):
        try:
            evidence = slot.device.stop(reason)
            if not isinstance(evidence, dict) or evidence.get("stopped") is not True:
                raise RuntimeError("Device did not confirm stopping")
            self._publish(active, status, reason, result={"stop": evidence,
                "state": slot.device.read_state(self.clock_id).model_dump(mode="json")})
        except Exception:
            slot.fault = True
            self.store.fault(slot.descriptor.resource_id, "stop_unconfirmed")
            self._publish(active, "outcome_unknown", "stop_unconfirmed")

    def _execute(self, slot, active):
        operation, started = None, time.monotonic()
        try:
            while True:
                with slot.lock:
                    self._expire(slot)
                    if self.closing.is_set() and active.cancel_reason is None:
                        active.cancel_reason = "gateway_shutdown"
                    if self.suspended.is_set() and active.cancel_reason is None:
                        active.cancel_reason = self.suspension_reason or "control_suspended"
                    if time.monotonic_ns() >= active.request.not_after_ns and active.cancel_reason is None:
                        active.cancel_reason = "command_expired"
                    if active.cancel_reason is not None:
                        self._stop(slot, active, active.cancel_reason)
                        break
                    if operation is None:
                        operation = slot.device.start(active.request.capability, active.request.arguments)
                        started = time.monotonic()
                        self._publish(active, "executing")
                    tick = operation.tick(time.monotonic() - started)
                    if tick.done:
                        self._publish(active, "succeeded", feedback=tick.feedback,
                            result={"state": slot.device.read_state(self.clock_id).model_dump(mode="json")})
                        break
                    # Persist acceptance and terminal results immediately.
                    # Feedback is sampled at 10 Hz rather than fsyncing at the
                    # device polling rate and starving renewals on slow disks.
                    if time.monotonic() - active.last_feedback_at >= .1:
                        self._publish(active, feedback=tick.feedback)
                        active.last_feedback_at = time.monotonic()
                self.closing.wait(self.tick_s)
        except Exception:
            with slot.lock:
                self._stop(slot, active, "device_execution_failed", status="failed")
        finally:
            with slot.lock:
                slot.active = None
                active.done.set()

    def get(self, owner, command_id):
        previous = self.store.lookup(command_id)
        if previous is None:
            raise SystemError("command_not_found", "Command has no recorded execution", 404)
        if previous[0] != owner:
            raise SystemError("forbidden", "Command belongs to another caller", 403)
        return previous[2]

    def cancel(self, owner, command_id):
        record = self.get(owner, command_id)
        slot = self._slot(record["resource_id"])
        with slot.lock:
            active = slot.active
            if active is None or active.request.command_id != command_id:
                return self.get(owner, command_id)
            if active.cancel_reason is None:
                active.cancel_reason = "client_cancelled"
            self._publish(active, "cancel_requested")
            return copy.deepcopy(active.record)

    def _watch(self):
        while not self.closing.wait(self.tick_s):
            for slot in self.slots.values():
                with slot.lock:
                    self._expire(slot)

    def tasks(self, owner):
        return self.store.records(owner)

    def suspend(self, reason):
        self.suspension_reason = reason
        self.suspended.set()
        waiting = []
        for slot in self.slots.values():
            with slot.lock:
                slot.lease = None
                self.store.epoch(slot.descriptor.resource_id)
                if slot.active is not None:
                    slot.active.cancel_reason = reason
                    waiting.append(slot.active)
        for active in waiting:
            if not active.done.wait(2.0):
                raise SystemError("stop_unconfirmed", "Worker action did not confirm stopping", 503)
        self.store.event("control_suspended", reason=reason)
        return self.snapshot()

    def resume(self):
        if any(slot.active is not None for slot in self.slots.values()):
            raise SystemError("resource_busy", "Reconciliation must finish before control resumes")
        self.suspension_reason = None
        self.suspended.clear()
        self.store.event("control_resumed")

    def reconcile(self, resource, acknowledge):
        if acknowledge is not True:
            raise SystemError("acknowledgement_required", "An operator must explicitly acknowledge reconciliation", 400)
        slot = self._slot(resource)
        with slot.lock:
            if slot.active is not None or slot.lease is not None:
                raise SystemError("resource_busy", "Cannot reconcile an active resource")
            evidence = slot.device.stop("operator_reconciliation")
            sample = slot.device.read_state(self.clock_id)
            if not isinstance(evidence, dict) or evidence.get("stopped") is not True or not isinstance(sample, StateSample) or not sample.valid:
                raise SystemError("stop_unconfirmed", "Device reconciliation did not confirm a valid stopped state", 503)
            slot.fault = False
            self.store.fault(resource)
            epoch = self.store.epoch(resource)
            self.store.event("resource_reconciled", resource_id=resource, control_epoch=epoch, stop=evidence)
            return {"resource_id": resource, "control_epoch": epoch, "stop": evidence, "state": sample.model_dump(mode="json")}

    def snapshot(self):
        return {"clock_id": self.clock_id, "suspended": self.suspended.is_set(),
                "reason": self.suspension_reason, "resources": [self.state(name) for name in self.slots],
                "active_leases": sum(slot.lease is not None for slot in self.slots.values()),
                "active_commands": [slot.active.request.command_id for slot in self.slots.values() if slot.active is not None]}

    def close(self):
        self.closing.set()
        self.watchdog.join(2.0)
        for thread in self.workers:
            thread.join(2.0)
        remaining = [t.name for t in self.workers if t.is_alive()]
        for slot in self.slots.values():
            with slot.lock:
                slot.lease = None
        self.store.event("gateway_closed", live_workers=remaining, active_leases=0)
        self.store.close()
        if remaining:
            raise RuntimeError("Cooperative device workers did not stop")
