"""Run against the installed wheel, including in a venv without RoboRSI core."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from threading import Event

from roborsi.contracts import ComponentContext, ComponentError, ComponentSpec
from roborsi_manual_counter import ManualCounterModel, provider


class ManualCounterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.context = ComponentContext(self.root / "workspace", root=self.root)
        self.provider = provider()

    def plan(self, config=None):
        return self.provider.plan(
            ComponentSpec(component="model.manual_counter", config=config or {}),
            self.context,
        )

    def test_model_default_and_configured_actions(self):
        for increment in (1, 2):
            self.assertEqual(
                ManualCounterModel(increment).generate({"observation": {"position": 0}}),
                {"increment": increment},
            )
        self.assertEqual(ManualCounterModel().generate({}), {"increment": 2})

    def test_sdk_only_installation(self):
        self.assertIsNone(importlib.util.find_spec("roborsi.components"))
        self.assertIsNone(importlib.util.find_spec("roborsi.assembly"))
        self.assertIsNone(importlib.util.find_spec("torch"))

    def test_planning_has_no_workspace_side_effects(self):
        plan = self.plan()
        self.assertFalse(self.context.workspace.exists())
        self.assertEqual(plan.descriptor.provides[0].contract, "model.counter_action/v1")
        self.assertEqual(plan.fingerprint, self.plan().fingerprint)

    def test_invalid_config_is_rejected_before_open(self):
        for config in ({"increment": 0}, {"increment": 3}, {"increment": True},
                       {"increment": 1.0}, {"increment": "2"}, {"unknown": 2}):
            with self.subTest(config=config), self.assertRaises(ComponentError):
                self.plan(config)

    def test_lifecycle_closes_the_model(self):
        prepared = self.provider.prepare(self.plan(), self.context)
        with self.provider.open(prepared, self.context) as handle:
            model = handle.value
            self.assertEqual(model.generate({}), {"increment": 2})
        self.assertTrue(model.closed)
        with self.assertRaises(RuntimeError):
            model.generate({})
        with self.assertRaises(ComponentError):
            _ = handle.value
        handle.close()

    def test_foreign_spec_is_rejected(self):
        with self.assertRaises(ComponentError):
            self.provider.plan(ComponentSpec(component="model.other"), self.context)

    def test_cancelled_context_is_rejected(self):
        cancel = Event()
        cancel.set()
        context = ComponentContext(self.root / "cancelled", root=self.root, cancelled=cancel)
        with self.assertRaises(ComponentError):
            self.provider.plan(ComponentSpec(component="model.manual_counter"), context)


if __name__ == "__main__":
    unittest.main()
