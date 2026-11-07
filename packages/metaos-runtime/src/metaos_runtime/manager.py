"""Persistent single-host instance directory and supervised execution routing."""

import copy
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from metaos_contracts import SystemError, SystemSpec, TERMINAL

from .composition import fingerprint, resolve
from .process import alive, atomic_json, process_identity, terminate
from .native import cleanup_native
from .worker import WorkerClient


class Catalog:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "catalog.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS instances (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, instance TEXT NOT NULL, owner TEXT NOT NULL, digest TEXT NOT NULL)")
        self.db.commit()

    def records(self):
        with self.lock:
            return {name: json.loads(raw) for name, raw in self.db.execute("SELECT id,record FROM instances ORDER BY id")}

    def save(self, row):
        row = copy.deepcopy(row)
        row["updated_at_ns"] = time.time_ns()
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO instances VALUES (?,?)", (row["id"], json.dumps(row, allow_nan=False)))
        with self.lock, (self.root / "manager-events.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": "instance_update", **{k: row.get(k) for k in
                ("id", "generation", "desired", "actual", "identity", "last_error", "updated_at_ns")}}) + "\n")

    def route(self, command_id):
        with self.lock:
            row = self.db.execute("SELECT instance,owner,digest FROM routes WHERE id=?", (command_id,)).fetchone()
            return row

    def bind(self, command_id, instance, owner, digest):
        with self.lock, self.db:
            row = self.route(command_id)
            if row:
                if row[1] != owner:
                    raise SystemError("forbidden", "Command belongs to another caller", 403)
                if row != (instance, owner, digest):
                    raise SystemError("command_conflict", "Command id already identifies another request")
            else:
                self.db.execute("INSERT INTO routes VALUES (?,?,?,?)", (command_id, instance, owner, digest))


class Manager:
    def __init__(self, root, project_root):
        self.root, self.project_root = Path(root), Path(project_root).resolve()
        self.catalog = Catalog(root)
        self.id = uuid.uuid4().hex
        self.clock_id = "monotonic:" + uuid.uuid4().hex
        self.lock = threading.RLock()
        self.clients, self.monitors, self.processes = {}, {}, {}
        self.closing = threading.Event()

    def info(self):
        return {"api": "metaos.system/v1", "clock_id": self.clock_id, "now_ns": time.monotonic_ns(),
                "backend": "supervised_process", "manager_id": self.id, "device_execution": "independent_worker"}

    def directory(self, instance_id):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", instance_id):
            raise SystemError("invalid_instance_id", "Instance id must be a lowercase identifier", 400)
        return self.root / "instances" / instance_id

    def record(self, instance_id):
        row = self.catalog.records().get(instance_id)
        if row is None:
            raise SystemError("instance_not_found", "Instance is not registered", 404)
        return row

    def instances(self):
        rows = []
        for row in self.catalog.records().values():
            rows.append({k: row.get(k) for k in ("id", "generation", "desired", "actual", "identity", "fingerprint",
                "resource_ids", "last_error", "updated_at_ns", "last_health")})
        return rows

    def health(self):
        rows = self.instances()
        return {"status": "degraded" if any(row["desired"] == "running" and row["actual"] != "running" for row in rows) else "ready",
            "instances": [{"id": row["id"], "desired": row["desired"], "actual": row["actual"],
                "last_error": row["last_error"]} for row in rows], **self.info()}

    def register(self, instance_id, spec, start=True):
        with self.lock:
            directory = self.directory(instance_id)
            if instance_id in self.catalog.records() or directory.exists():
                raise SystemError("instance_exists", "Instance id already exists")
            if not spec.resources:
                raise SystemError("resource_reservation_required", "Managed SystemSpec must declare resources for every export", 400)
            reserved = {rid for row in self.catalog.records().values() for rid in row["resource_ids"]}
            if reserved.intersection(spec.resources.values()):
                raise SystemError("resource_conflict", "A resource identity is reserved by another instance")
            resolved = resolve(spec, self.project_root)
            directory.mkdir(parents=True, mode=0o700)
            atomic_json(directory / "system.json", spec.model_dump(mode="json"))
            atomic_json(directory / "system.lock.json", resolved.lock)
            atomic_json(directory / "worker-token.json", {"token": secrets.token_urlsafe(32)})
            self.catalog.save({"id": instance_id, "generation": 0, "desired": "stopped", "actual": "stopped",
                "identity": None, "resource_ids": list(spec.resources.values()), "descriptors": [],
                "fingerprint": resolved.fingerprint, "last_error": None})
            return self.start(instance_id) if start else self.record(instance_id)

    def _validate_lock(self, instance_id):
        directory = self.directory(instance_id)
        spec = SystemSpec.model_validate_json((directory / "system.json").read_text())
        resolved = resolve(spec, self.project_root)
        if resolved.lock != json.loads((directory / "system.lock.json").read_text()):
            raise SystemError("lock_changed", "Instance inputs or installed code changed since registration")
        return resolved

    def start(self, instance_id, acknowledge=False):
        with self.lock:
            row = self.record(instance_id)
            directory = self.directory(instance_id)
            self._validate_lock(instance_id)
            if row["actual"] == "fault" and row["generation"] and not acknowledge:
                raise SystemError("acknowledgement_required", "Restarting a faulted instance requires explicit acknowledgement", 400)
            if alive(row["identity"]):
                raise SystemError("instance_running", "Instance process is already alive; reconcile instead of duplicating it")
            marker = directory / "worker-process.json"
            if marker.exists() and alive(json.loads(marker.read_text()).get("identity")):
                raise SystemError("instance_running", "Unreconciled worker process is alive")
            cleanup_native(directory)
            ready_path = directory / "worker-ready.json"
            if ready_path.exists():
                ready = json.loads(ready_path.read_text())
                if alive(ready.get("identity")):
                    raise SystemError("instance_running", "Unreconciled worker is alive")
                ready_path.unlink()
            row.update(generation=row["generation"] + 1, desired="running", actual="starting", last_error=None)
            self.catalog.save(row)
            atomic_json(directory / "worker-settings.json", {"project_root": str(self.project_root),
                "instance_id": instance_id, "generation": row["generation"]})
            (directory / "worker-shutdown.json").unlink(missing_ok=True)
            log = (directory / f"worker-{row['generation']}.log").open("w")
            try:
                process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "metaos_runtime.worker", "--directory", str(directory)],
                    cwd=directory, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            finally:
                log.close()
            self.processes[instance_id] = process
            row["identity"] = process_identity(process.pid)
            self.catalog.save(row)
            try:
                self._await_ready(instance_id, 75)
                self._attach(instance_id)
            except Exception:
                terminate(row["identity"], 1)
                cleanup_native(directory)
                row.update(actual="fault", last_error="worker_start_failed")
                self.catalog.save(row)
                raise
            return self.record(instance_id)

    def _await_ready(self, instance_id, timeout):
        row, directory = self.record(instance_id), self.directory(instance_id)
        end = time.monotonic() + timeout
        while True:
            try:
                ready = json.loads((directory / "worker-ready.json").read_text())
                if ready["instance_id"] == instance_id and ready["generation"] == row["generation"] and alive(ready["identity"]):
                    return ready
            except (OSError, ValueError, KeyError):
                pass
            if row.get("identity") and not alive(row["identity"]):
                raise SystemError("worker_start_failed", "Worker exited before readiness; inspect its retained log", 503)
            if time.monotonic() >= end:
                raise SystemError("worker_start_failed", "Worker did not become ready", 503)
            time.sleep(0.02)

    def _attach(self, instance_id):
        row = self.record(instance_id)
        self._validate_lock(instance_id)
        client = WorkerClient(self.directory(instance_id), manager_id=self.id)
        if not alive(client.ready["identity"]) or client.ready["generation"] != row["generation"] or client.ready["fingerprint"] != row["fingerprint"]:
            raise SystemError("worker_identity_changed", "Worker process or content identity differs", 503)
        descriptors = client.call("describe")
        if {x["resource_id"] for x in descriptors} != set(row["resource_ids"]):
            raise SystemError("resource_identity_mismatch", "Worker descriptor differs from reserved resources", 503)
        snapshot = client.call("claim", manager_id=self.id, clock_id=self.clock_id)
        self.clients[instance_id] = client
        # Start liveness traffic before any catalog I/O. A slow fsync or
        # descriptor request must not consume the worker's heartbeat budget.
        activated = self._monitor(instance_id, client)
        row.update(actual="running", identity=client.ready["identity"], descriptors=descriptors, last_error=None, last_health=snapshot)
        try:
            self.catalog.save(row)
        except Exception:
            self.monitors[instance_id][0].set()
            raise
        activated.set()

    def recover(self):
        # Validate every lock before altering any live instance.
        for instance_id in self.catalog.records():
            self._validate_lock(instance_id)
        for instance_id, row in self.catalog.records().items():
            if row["desired"] != "running":
                continue
            try:
                self._await_ready(instance_id, 3 if row["actual"] != "starting" else 20)
                self._attach(instance_id)
            except SystemError as exc:
                row.update(actual="fault", last_error=exc.code)
                self.catalog.save(row)

    def _monitor(self, instance_id, client):
        stop = threading.Event()
        activated = threading.Event()
        def failed(exc):
            row = self.record(instance_id)
            if row["actual"] == "running":
                row.update(actual="fault", last_error=exc.code)
                self.catalog.save(row)
            stop.set()
        def heartbeat():
            while not stop.wait(.15) and not self.closing.is_set():
                try:
                    client.call("heartbeat", manager_id=self.id)
                except SystemError as exc:
                    failed(exc)
                    return
        def health():
            while not activated.wait(.1):
                if stop.is_set() or self.closing.is_set():
                    return
            while not stop.wait(1.) and not self.closing.is_set():
                try:
                    snapshot = client.call("health")
                    row = self.record(instance_id)
                    if row["actual"] != "running" or stop.is_set():
                        return
                    row["last_health"] = snapshot
                    self.catalog.save(row)
                except SystemError as exc:
                    failed(exc)
                    return
        threads = [threading.Thread(target=target, name=name + instance_id, daemon=True)
                   for target, name in [(heartbeat, "metaos-heartbeat-"), (health, "metaos-health-")]]
        self.monitors[instance_id] = (stop, *threads)
        for thread in threads:
            thread.start()
        return activated

    def _client(self, instance_id):
        row = self.record(instance_id)
        if row["actual"] != "running" or instance_id not in self.clients:
            raise SystemError("instance_unavailable", "Instance is stopped or requires reconciliation", 503)
        return self.clients[instance_id]

    def _instance_for(self, resource):
        for instance_id, row in self.catalog.records().items():
            if resource in row["resource_ids"]:
                return instance_id
        raise SystemError("resource_not_found", "Resource is not registered", 404)

    def resources(self):
        result = []
        for row in self.catalog.records().values():
            for descriptor in row["descriptors"]:
                result.append({**descriptor, "metadata": {**descriptor.get("metadata", {}),
                    "instance_id": row["id"], "instance_state": row["actual"]}})
        return result

    def state(self, resource):
        return self._client(self._instance_for(resource)).call("state", resource=resource)

    def artifacts(self, resource):
        return self._client(self._instance_for(resource)).call("artifacts", resource=resource)

    def artifact_chunk(self, resource, artifact_id, offset, length):
        return self._client(self._instance_for(resource)).call("artifact_chunk", resource=resource,
            artifact_id=artifact_id, offset=offset, length=length)

    def acquire(self, owner, request):
        return self._client(self._instance_for(request.resource_id)).call("acquire", owner=owner, request=request.model_dump(mode="json"))

    def renew(self, owner, reference, ttl_s):
        return self._client(self._instance_for(reference.resource_id)).call("renew", owner=owner, reference=reference.model_dump(mode="json"), ttl_s=ttl_s)

    def release(self, owner, reference):
        return self._client(self._instance_for(reference.resource_id)).call("release", owner=owner, reference=reference.model_dump(mode="json"))

    def submit(self, owner, request):
        instance_id = self._instance_for(request.resource_id)
        self.catalog.bind(request.command_id, instance_id, owner, fingerprint(request.model_dump(mode="json")))
        return self._client(instance_id).call("submit", owner=owner, request=request.model_dump(mode="json"))

    def _command_instance(self, owner, command_id):
        route = self.catalog.route(command_id)
        if route is None:
            raise SystemError("command_not_found", "Command has no recorded route", 404)
        if route[1] != owner:
            raise SystemError("forbidden", "Command belongs to another caller", 403)
        return route[0]

    def get(self, owner, command_id):
        instance_id = self._command_instance(owner, command_id)
        try:
            return self._client(instance_id).call("get", owner=owner, command_id=command_id)
        except SystemError as exc:
            if exc.code not in {"instance_unavailable", "worker_unavailable"}:
                raise
            for record in self._history(instance_id):
                if record["command_id"] == command_id:
                    if record["status"] not in TERMINAL:
                        record.update(status="outcome_unknown", reason="execution_worker_unavailable")
                    return record
            raise SystemError("outcome_unknown", "Command route exists but acceptance cannot be confirmed", 503) from None

    def cancel(self, owner, command_id):
        return self._client(self._command_instance(owner, command_id)).call("cancel", owner=owner, command_id=command_id)

    def _history(self, instance_id):
        path = self.directory(instance_id) / "runtime/state.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record FROM commands ORDER BY rowid")]

    def tasks(self, owner):
        return [self.get(owner, row["command_id"]) for instance_id in self.catalog.records()
                for row in self._history(instance_id) if row["owner"] == owner]

    def stop(self, instance_id):
        with self.lock:
            row = self.record(instance_id)
            row.update(desired="stopped", actual="stopping")
            self.catalog.save(row)
            monitor = self.monitors.pop(instance_id, None)
            if monitor:
                monitor[0].set()
                for thread in monitor[1:]:
                    thread.join(4)
            confirmed = False
            if alive(row["identity"]):
                try:
                    WorkerClient(self.directory(instance_id), manager_id=self.id).call("shutdown")
                    confirmed = True
                except SystemError:
                    pass
            outcome = terminate(row["identity"], 4)
            process = self.processes.pop(instance_id, None)
            if process:
                process.wait(3)
            self.clients.pop(instance_id, None)
            shutdown = self.directory(instance_id) / "worker-shutdown.json"
            clean = shutdown.exists() and json.loads(shutdown.read_text()).get("handles_closed") is True
            native_cleanup = cleanup_native(self.directory(instance_id))
            row.update(actual="stopped" if clean else "fault", last_error=None if clean else "stop_unconfirmed",
                last_stop={"requested": confirmed, "cleanup_confirmed": clean, "native_cleanup": native_cleanup, **outcome})
            self.catalog.save(row)
            return self.record(instance_id)

    def control_instance(self, instance_id, operation, body):
        if operation == "start":
            return self.start(instance_id)
        if operation == "stop":
            return self.stop(instance_id)
        if operation == "restart":
            if not body.get("acknowledge"):
                raise SystemError("acknowledgement_required", "Restart requires explicit acknowledgement", 400)
            self.stop(instance_id)
            return self.start(instance_id, acknowledge=True)
        if operation == "reconcile":
            if body.get("acknowledge") is not True:
                raise SystemError("acknowledgement_required", "Reconciliation requires explicit acknowledgement", 400)
            with self.lock:
                old = self.monitors.pop(instance_id, None)
                if old:
                    old[0].set()
                    for thread in old[1:]:
                        thread.join(4)
                self._attach(instance_id)
                results = [self.clients[instance_id].call("reconcile", resource=resource, acknowledge=body.get("acknowledge"))
                           for resource in self.record(instance_id)["resource_ids"]]
                return {"instance": instance_id, "reconciled": results}
        raise SystemError("invalid_operation", "Unsupported instance lifecycle operation", 400)

    def close(self):
        self.closing.set()
        outcomes = []
        for instance_id in set(self.clients) | set(self.processes):
            outcomes.append(self.stop(instance_id))
        self.catalog.db.close()
        return outcomes
