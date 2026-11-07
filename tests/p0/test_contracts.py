"""Contract and SDK-only plugin tests; no MetaOS runtime import is required."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event

from metaos_contracts import CapabilityDescriptor, CommandRequest, LeaseRequest, SystemSpec
from metaos_mock_arm import provider
from pydantic import ValidationError
from roborsi.contracts import ComponentContext, ComponentError, ComponentSpec


class ContractTests(unittest.TestCase):
    def test_contracts_and_mock_do_not_need_baseline_core(self):
        self.assertIsNone(importlib.util.find_spec("roborsi.evaluation"))
        self.assertIsNone(importlib.util.find_spec("roborsi.components"))
        self.assertIsNone(importlib.util.find_spec("torch"))

    def test_system_spec_has_no_episode_or_agent_requirement(self):
        spec = SystemSpec.model_validate_json(json.dumps({"components": {
            "arm": {"component": "embodiment.mock_arm"}}, "exports": ["arm"]}))
        self.assertEqual(spec.exports, ("arm",))
        self.assertNotIn("entrypoints", spec.model_dump())

    def test_invalid_leases_are_rejected(self):
        for ttl in (0, -1, 61, True, "1", float("inf")):
            with self.subTest(ttl=ttl), self.assertRaises(ValidationError):
                LeaseRequest(resource_id="robots/mock-arm", ttl_s=ttl)

    def test_invalid_resource_and_export_are_rejected(self):
        with self.assertRaises(ValidationError):
            LeaseRequest(resource_id="../../device", ttl_s=1.0)
        with self.assertRaises(ValidationError):
            SystemSpec.model_validate_json('{"components":{"a":{"component":"embodiment.mock_arm"}},"exports":["missing"]}')

    def test_nonfinite_nested_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            CommandRequest(resource_id="robots/mock-arm", lease_id="id", control_epoch=1,
                command_id="cmd", capability="manipulation.move_joint/v1", arguments={"bad": float("nan")},
                clock_id="clock", not_after_ns=1, state_sequence=0, frame_id="joint", units=("rad", "rad"))

    def test_capability_requires_valid_object_schema(self):
        with self.assertRaises(ValidationError):
            CapabilityDescriptor(id="move/v1", parameters={"type": "array"}, units=("rad",), frame_id="joint")

    def test_plugin_lifecycle_state_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ComponentContext(Path(directory) / "workspace", root=Path(directory))
            selected = provider()
            plan = selected.plan(ComponentSpec(component="embodiment.mock_arm"), context)
            self.assertFalse(context.workspace.exists())
            with selected.open(selected.prepare(plan, context), context) as handle:
                arm = handle.value
                self.assertEqual(arm.read_state("test").data["positions"], [0.0, 0.0])
                operation = arm.start("manipulation.move_joint/v1", {"positions": [1.0, -1.0], "duration_s": 1.0})
                self.assertFalse(operation.tick(0.5).done)
                self.assertEqual(arm.read_state("test").data["positions"], [0.5, -0.5])
                self.assertTrue(arm.stop("unit_cancel")["stopped"])
                self.assertFalse(arm.read_state("test").data["moving"])
            self.assertTrue(arm.closed)
            self.assertTrue(handle.closed)

    def test_plugin_rejects_config_and_cancelled_context(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ComponentContext(Path(directory), root=Path(directory))
            with self.assertRaises(ComponentError):
                provider().plan(ComponentSpec(component="embodiment.mock_arm", config={"failure_step": 0}), context)
            event = Event()
            event.set()
            cancelled = ComponentContext(Path(directory), cancelled=event)
            with self.assertRaises(ComponentError):
                provider().plan(ComponentSpec(component="embodiment.mock_arm"), cancelled)


if __name__ == "__main__":
    unittest.main()
