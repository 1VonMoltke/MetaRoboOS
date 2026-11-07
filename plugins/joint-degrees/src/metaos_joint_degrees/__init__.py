"""All unit conversion is explicit and supplied by this independent plugin."""

import json
import math

from jsonschema import Draft202012Validator
from metaos_contracts import CapabilityDescriptor, ResourceDescriptor, StateSample, SystemError, TickResult
from roborsi.contracts import Capability, ComponentDescriptor, ComponentHandle, ComponentPlan, DependencyPort, PreparedComponent


def converted(value):
    return {**value, **({"positions": [math.degrees(x) for x in value["positions"]]} if "positions" in value else {})}


class Motion:
    def __init__(self, operation):
        self.operation = operation

    def tick(self, elapsed_s):
        result = self.operation.tick(elapsed_s)
        return TickResult(done=result.done, feedback=converted(result.feedback))


class DegreesDevice:
    def __init__(self, device, config):
        self.device, self.config = device, config
        source = device.describe_resource()
        cap = next((x for x in source.capabilities if x.id == "manipulation.move_joint/v1"), None)
        if cap is None or cap.units != ("rad", "rad") or cap.frame_id != "mock_joint":
            raise SystemError("incompatible_semantics", "Adapter requires two radian joints in mock_joint", 400)
        self.source = source

    def describe_resource(self):
        return ResourceDescriptor(resource_id=self.config.get("resource_id", "robots/degrees-arm"), kind="joint_degrees_adapter",
            capabilities=(CapabilityDescriptor(id="manipulation.move_joint_degrees/v1", units=("deg", "deg"), frame_id="mock_joint",
                parameters={"type": "object", "additionalProperties": False, "required": ["positions", "duration_s"],
                    "properties": {"positions": {"type": "array", "minItems": 2, "maxItems": 2,
                        "items": {"type": "number", "minimum": -170, "maximum": 170}},
                        "duration_s": {"type": "number", "minimum": .02, "maximum": 30}}}),),
            metadata={"adapter": "degrees_to_radians", "source_resource": self.source.resource_id,
                "control_domain": self.source.metadata.get("control_domain", self.source.resource_id), "units": ["deg", "deg"]})

    def read_state(self, clock_id):
        source = self.device.read_state(clock_id)
        return StateSample(resource_id=self.describe_resource().resource_id, sequence=source.sequence, clock_id=source.clock_id,
            sampled_at_ns=source.sampled_at_ns, frame_id=source.frame_id, valid=source.valid,
            data={**converted(source.data), "units": ["deg", "deg"]})

    def start(self, capability, arguments):
        cap = self.describe_resource().capabilities[0]
        if capability != cap.id or not Draft202012Validator(cap.parameters).is_valid(arguments):
            raise SystemError("invalid_action", "Degrees adapter capability or arguments mismatch", 400)
        return Motion(self.device.start("manipulation.move_joint/v1", {"positions": [math.radians(x) for x in arguments["positions"]],
            "duration_s": arguments["duration_s"]}))

    def stop(self, reason):
        return converted(self.device.stop(reason))

    def close(self):
        pass  # Dependency owner closes the underlying device after this handle.


class Provider:
    descriptor = ComponentDescriptor(id="embodiment.joint_degrees", kind="embodiment", version="0.1.0",
        dependencies=(DependencyPort(name="device", capability=Capability(contract="manipulation.move_joint/v1")),),
        provides=(Capability(contract="manipulation.move_joint_degrees/v1"),),
        config_schema={"type": "object", "additionalProperties": False,
            "properties": {"resource_id": {"type": "string", "pattern": r"^[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)+$"}}})

    def describe(self):
        return self.descriptor.model_copy(deep=True)

    def plan(self, spec, context):
        context.check()
        if spec.component != self.descriptor.id or not Draft202012Validator(self.descriptor.config_schema).is_valid(spec.config):
            raise SystemError("invalid_config", "Invalid explicit unit adapter configuration", 400)
        return ComponentPlan(descriptor=self.describe(), configuration_json=json.dumps(dict(spec.config)))

    def prepare(self, plan, context):
        context.check()
        plan.verify_inputs()
        return PreparedComponent(plan)

    def open(self, prepared, context):
        device = DegreesDevice(context.binding("device").value, prepared.plan.config)
        return ComponentHandle(self.descriptor.id, device, cleanup=device.close)


def provider():
    return Provider()
