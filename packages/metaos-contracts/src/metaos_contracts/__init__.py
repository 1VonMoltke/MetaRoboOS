"""Public P0 contracts. No runtime, benchmark, model or hardware imports."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from roborsi.contracts import Capability, ComponentSpec

API_VERSION = "metaos.system/v1"
RESOURCE = r"^[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)+$"
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
CAPABILITY = r"^[a-z][a-z0-9_.-]*/v[1-9][0-9]*$"


class SystemError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status

    def as_dict(self):
        return {"code": self.code, "message": str(self)}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def finite_json(self):
        import json
        # JSON-mode serialization can replace NaN with null before validation.
        try:
            json.dumps(self.model_dump(mode="python"), allow_nan=False)
        except TypeError as exc:
            raise ValueError("Contract values must be finite JSON data") from exc
        return self


class CapabilityDescriptor(Contract):
    id: str = Field(pattern=CAPABILITY)
    parameters: dict[str, Any]
    units: tuple[str, ...]
    frame_id: str = Field(min_length=1)
    max_state_age_s: float = Field(default=0.5, gt=0, le=60)
    max_command_validity_s: float = Field(default=60.0, gt=0, le=3600)

    @field_validator("parameters")
    @classmethod
    def schema(cls, value):
        Draft202012Validator.check_schema(value)
        if value.get("type") != "object":
            raise ValueError("Capability parameters must describe an object")
        return value


class ResourceDescriptor(Contract):
    resource_id: str = Field(pattern=RESOURCE)
    kind: str = Field(min_length=1)
    capabilities: tuple[CapabilityDescriptor, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct(self):
        ids = [cap.id for cap in self.capabilities]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Resources require unique nonempty capabilities")
        return self


class StateSample(Contract):
    resource_id: str = Field(pattern=RESOURCE)
    sequence: int = Field(ge=0)
    clock_id: str = Field(min_length=1)
    sampled_at_ns: int = Field(ge=0)
    frame_id: str = Field(min_length=1)
    valid: bool = True
    data: dict[str, Any]


class LeaseRequest(Contract):
    resource_id: str = Field(pattern=RESOURCE)
    ttl_s: float = Field(gt=0, le=60)


class LeaseReference(Contract):
    resource_id: str = Field(pattern=RESOURCE)
    lease_id: str = Field(pattern=IDENTIFIER)
    control_epoch: int = Field(gt=0)


class LeaseGrant(LeaseReference):
    owner: str = Field(pattern=IDENTIFIER)
    clock_id: str
    expires_at_ns: int = Field(ge=0)


class CommandRequest(LeaseReference):
    command_id: str = Field(pattern=IDENTIFIER)
    capability: str = Field(pattern=CAPABILITY)
    arguments: dict[str, Any]
    clock_id: str = Field(min_length=1)
    not_after_ns: int = Field(gt=0)
    state_sequence: int = Field(ge=0)
    frame_id: str = Field(min_length=1)
    units: tuple[str, ...]


CommandStatus = Literal["accepted", "executing", "cancel_requested", "succeeded", "failed", "cancelled", "outcome_unknown"]
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})


class CommandRecord(Contract):
    command_id: str
    resource_id: str
    owner: str
    status: CommandStatus
    reason: str | None = None
    feedback: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    created_at_ns: int
    updated_at_ns: int
    clock_id: str


class SystemNode(ComponentSpec):
    bindings: dict[str, str] = Field(default_factory=dict)


class SystemSpec(Contract):
    schema_version: Literal[1] = 1
    api: Literal["metaos.system/v1"] = API_VERSION
    components: dict[str, SystemNode]
    exports: tuple[str, ...]
    resources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def names(self):
        import re
        if not self.components or not self.exports or len(set(self.exports)) != len(self.exports):
            raise ValueError("System requires components and distinct exports")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in self.components):
            raise ValueError("Component aliases must be lowercase identifiers")
        if any(name not in self.components for name in self.exports):
            raise ValueError("Export names must select component aliases")
        if self.resources and (set(self.resources) != set(self.exports) or
                any(not re.fullmatch(RESOURCE, value) for value in self.resources.values()) or
                len(set(self.resources.values())) != len(self.resources)):
            raise ValueError("Managed resources must name each export with a distinct resource id")
        return self


class TickResult(Contract):
    done: bool
    feedback: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class DeviceOperation(Protocol):
    def tick(self, elapsed_s: float) -> TickResult: ...


@runtime_checkable
class DeviceAPI(Protocol):
    """Cooperative device API. Production drivers need independent stop guarantees."""

    def describe_resource(self) -> ResourceDescriptor: ...
    def read_state(self, clock_id: str) -> StateSample: ...
    def start(self, capability: str, arguments: dict[str, Any]) -> DeviceOperation: ...
    def stop(self, reason: str) -> dict[str, Any]: ...
    def close(self) -> None: ...


@runtime_checkable
class ArtifactAPI(Protocol):
    """Optional immutable media channel; references never grant filesystem access."""

    def list_artifacts(self) -> list[dict[str, Any]]: ...
    def read_artifact(self, artifact_id: str, offset: int, length: int) -> bytes: ...


@runtime_checkable
class ActionValidationAPI(Protocol):
    """Validate device-specific session constraints before command acceptance."""

    def validate_action(self, capability: str, arguments: dict[str, Any]) -> None: ...
