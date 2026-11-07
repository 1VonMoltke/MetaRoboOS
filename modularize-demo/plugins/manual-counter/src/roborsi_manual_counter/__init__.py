"""A deterministic Model plugin implemented using only the public SDK.

The existing counter Agent calls generate(request). This model returns a fixed
increment; it requires no network, model weights, GPU, or imports from core.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator
from roborsi.contracts import (
    Capability,
    ComponentContext,
    ComponentDescriptor,
    ComponentError,
    ComponentHandle,
    ComponentPlan,
    ComponentSpec,
    PreparedComponent,
)

__version__ = "1.0.0"


class ManualCounterModel:
    """Use increment=2 by default; observation/context are accepted but unused."""

    def __init__(self, increment: int = 2):
        if type(increment) is not int or increment not in (1, 2):
            raise ValueError("increment must be integer 1 or 2")
        self.increment = increment
        self.closed = False

    def generate(self, request: dict) -> dict:
        if self.closed:
            raise RuntimeError("Manual counter model is closed")
        return {"increment": self.increment}

    def close(self) -> None:
        self.closed = True


class ManualCounterProvider:
    """Implement describe / plan / prepare / open without a core base class."""

    descriptor = ComponentDescriptor(
        id="model.manual_counter",
        kind="model",
        version=__version__,
        description="Deterministic counter action model, no model service required",
        provides=(Capability(contract="model.counter_action/v1"),),
        config_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "increment": {"type": "integer", "enum": [1, 2], "default": 2},
            },
        },
    )

    def describe(self) -> ComponentDescriptor:
        return self.descriptor.model_copy(deep=True)

    def _validate(self, config: dict) -> None:
        if not Draft202012Validator(self.descriptor.config_schema).is_valid(config):
            raise ComponentError(
                "invalid_config",
                "Expected only optional increment, with integer value 1 or 2",
                component_id=self.descriptor.id,
            )
        # Python represents 1.0 as a JSON-schema integer, but this API emits ints.
        if "increment" in config and type(config["increment"]) is not int:
            raise ComponentError("invalid_config", "increment must be an integer")

    def plan(self, spec: ComponentSpec, context: ComponentContext) -> ComponentPlan:
        context.check()
        if spec.component != self.descriptor.id:
            raise ComponentError("provider_mismatch", "Spec selects another provider")
        self._validate(spec.config)
        return ComponentPlan(
            descriptor=self.describe(),
            configuration_json=json.dumps(dict(spec.config), sort_keys=True),
        )

    def prepare(self, plan: ComponentPlan, context: ComponentContext) -> PreparedComponent:
        context.check()
        if plan.descriptor != self.describe():
            raise ComponentError("provider_mismatch", "Plan selects another provider")
        self._validate(plan.config)
        plan.verify_inputs()
        return PreparedComponent(plan)

    def open(self, prepared: PreparedComponent, context: ComponentContext) -> ComponentHandle:
        self.prepare(prepared.plan, context)
        model = ManualCounterModel(prepared.plan.config.get("increment", 2))
        return ComponentHandle(self.descriptor.id, model, cleanup=model.close)


def provider() -> ManualCounterProvider:
    """Entry-point factory, loaded by the installed plugin registry."""
    return ManualCounterProvider()
