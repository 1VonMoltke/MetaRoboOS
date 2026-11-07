"""Cooperative CPU-only arm. No runtime/core or evaluation dependency."""

from __future__ import annotations

import json
import time
from threading import RLock

from jsonschema import Draft202012Validator
from metaos_contracts import CapabilityDescriptor, ResourceDescriptor, StateSample, SystemError, TickResult
from roborsi.contracts import Capability, ComponentDescriptor, ComponentError, ComponentHandle, ComponentPlan, DependencyPort, PreparedComponent


class Motion:
    def __init__(self, arm, positions, duration_s):
        self.arm, self.target, self.duration = arm, positions, duration_s
        self.initial = list(arm.positions)
        self.ticks = 0

    def tick(self, elapsed_s):
        with self.arm.lock:
            if self.arm.config.get("tick_delay_s"):
                time.sleep(self.arm.config["tick_delay_s"])
            if self.arm.closed or not self.arm.moving:
                raise RuntimeError("Device is no longer executing")
            self.ticks += 1
            if self.arm.config.get("failure_step") == self.ticks:
                raise RuntimeError("Injected mock motion failure")
            progress = min(1.0, elapsed_s / self.duration)
            self.arm.positions = [a + (b - a) * progress for a, b in zip(self.initial, self.target)]
            self.arm.sequence += 1
            if progress >= 1.0:
                self.arm.moving = False
            return TickResult(done=progress >= 1.0, feedback={"positions": list(self.arm.positions), "progress": progress})


class MockArm:
    def __init__(self, config):
        self.config = config
        self.lock = RLock()
        self.positions = [0.0, 0.0]
        self.sequence = self.executions = self.stops = 0
        self.moving = self.closed = False

    def describe_resource(self):
        return ResourceDescriptor(
            resource_id=self.config.get("resource_id", "robots/mock-arm"),
            kind="mock_joint_arm",
            capabilities=(CapabilityDescriptor(
                id="manipulation.move_joint/v1", units=("rad", "rad"), frame_id="mock_joint",
                parameters={"type": "object", "additionalProperties": False,
                            "properties": {"positions": {"type": "array", "minItems": 2, "maxItems": 2,
                                "items": {"type": "number", "minimum": -3.0, "maximum": 3.0}},
                                "duration_s": {"type": "number", "minimum": 0.02, "maximum": 30.0}},
                            "required": ["positions", "duration_s"]}),),
            metadata={"joint_names": ["joint_1", "joint_2"], "test_device": True,
                      "stop_policy": "cooperative_hold", "tick_interval_s": 0.01},
        )

    def read_state(self, clock_id):
        with self.lock:
            return StateSample(resource_id=self.describe_resource().resource_id,
                sequence=self.sequence, clock_id=clock_id,
                sampled_at_ns=max(0, time.monotonic_ns() - int(self.config.get("state_age_s", 0.0) * 1e9)),
                frame_id="mock_joint", data={"positions": list(self.positions), "moving": self.moving,
                    "execution_count": self.executions, "stop_count": self.stops, "closed": self.closed})

    def start(self, capability, arguments):
        with self.lock:
            if self.closed or self.moving:
                raise RuntimeError("Mock device unavailable")
            if capability != "manipulation.move_joint/v1":
                raise SystemError("unsupported_capability", "Mock arm does not provide this capability", 400)
            Draft202012Validator(self.describe_resource().capabilities[0].parameters).validate(arguments)
            self.moving = True
            self.executions += 1
            return Motion(self, list(arguments["positions"]), arguments["duration_s"])

    def stop(self, reason):
        with self.lock:
            self.moving = False
            self.stops += 1
            self.sequence += 1
            if self.config.get("stop_unconfirmed"):
                return {"stopped": False, "reason": reason}
            return {"stopped": True, "reason": reason, "positions": list(self.positions)}

    def close(self):
        with self.lock:
            self.stop("component_closed")
            self.closed = True


class Provider:
    descriptor = ComponentDescriptor(
        id="embodiment.mock_arm", kind="embodiment", version="0.1.0",
        provides=(Capability(contract="manipulation.move_joint/v1"),),
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "resource_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)+$"},
            "state_age_s": {"type": "number", "minimum": 0, "maximum": 60},
            "failure_step": {"type": "integer", "minimum": 1},
            "stop_unconfirmed": {"type": "boolean"},
            "tick_delay_s": {"type": "number", "minimum": 0, "maximum": 30}},
        },
    )

    def describe(self):
        return self.descriptor.model_copy(deep=True)

    def plan(self, spec, context):
        context.check()
        if spec.component != self.descriptor.id:
            raise ComponentError("provider_mismatch", "Spec selects another provider")
        if not Draft202012Validator(self.descriptor.config_schema).is_valid(spec.config):
            raise ComponentError("invalid_config", "Invalid mock arm configuration")
        return ComponentPlan(descriptor=self.describe(), configuration_json=json.dumps(dict(spec.config)))

    def prepare(self, plan, context):
        context.check()
        if plan.descriptor != self.describe():
            raise ComponentError("provider_mismatch", "Plan selects another provider")
        plan.verify_inputs()
        return PreparedComponent(plan)

    def open(self, prepared, context):
        self.prepare(prepared.plan, context)
        arm = MockArm(prepared.plan.config)
        return ComponentHandle(self.descriptor.id, arm, cleanup=arm.close)


def provider():
    return Provider()


class Monitor:
    def __init__(self, device):
        self.resource_id = device.describe_resource().resource_id
        self.closed = False

    def close(self):
        self.closed = True


class MonitorProvider(Provider):
    descriptor = ComponentDescriptor(
        id="tool.mock_arm_monitor", kind="tool", version="0.1.0",
        dependencies=(DependencyPort(name="device", capability=Capability(contract="manipulation.move_joint/v1")),),
        provides=(Capability(contract="monitor.device_state/v1"),),
    )

    def open(self, prepared, context):
        self.prepare(prepared.plan, context)
        monitor = Monitor(context.binding("device").value)
        return ComponentHandle(self.descriptor.id, monitor, cleanup=monitor.close)


def monitor_provider():
    return MonitorProvider()
