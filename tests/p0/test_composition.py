"""Installed SDK providers exercise the generic graph, separate from episodes."""

import copy
import json
import unittest
from unittest.mock import patch

from metaos_contracts import SystemError, SystemSpec
from metaos_runtime.composition import Registry, open_system, resolve
from roborsi.contracts import Capability, ComponentDescriptor, DependencyPort

from support import RESOURCE, default_spec, directory


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.root = directory(self._testMethodName)

    def spec(self, value=None):
        return SystemSpec.model_validate_json(json.dumps(value or default_spec()))

    def graph(self):
        doc = default_spec()
        doc["components"]["monitor"] = {"component": "tool.mock_arm_monitor", "bindings": {"device": "arm"}}
        return doc

    def error(self, code, operation):
        with self.assertRaises(SystemError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_installed_two_node_graph_opens_and_closes_in_reverse_order(self):
        selected = resolve(self.spec(self.graph()), self.root)
        self.assertEqual(selected.order, ("arm", "monitor"))
        self.assertFalse((self.root / ".system-plan").exists())
        self.assertFalse((self.root / "runtime").exists())
        closed = []
        from metaos_mock_arm import MockArm, Monitor
        arm_close, monitor_close = MockArm.close, Monitor.close
        def close_arm(value):
            closed.append("arm")
            arm_close(value)
        def close_monitor(value):
            closed.append("monitor")
            monitor_close(value)
        with patch.object(MockArm, "close", close_arm), patch.object(Monitor, "close", close_monitor):
            with open_system(selected, self.root / "runtime") as resources:
                arm = resources[RESOURCE]
                self.assertFalse(arm.closed)
            self.assertTrue(arm.closed)
        self.assertEqual(closed, ["monitor", "arm"])

    def test_missing_explicit_binding_is_rejected(self):
        doc = self.graph()
        doc["components"]["monitor"].pop("bindings")
        self.error("missing_binding", lambda: resolve(self.spec(doc), self.root))

    def test_unknown_port_and_missing_target_are_rejected(self):
        for bindings, code in [({"typo": "arm"}, "unknown_binding"), ({"device": "absent"}, "missing_component")]:
            doc = self.graph()
            doc["components"]["monitor"]["bindings"] = bindings
            self.error(code, lambda: resolve(self.spec(doc), self.root))

    def test_incompatible_capability_binding_is_rejected(self):
        doc = self.graph()
        doc["components"]["monitor"]["bindings"]["device"] = "monitor"
        self.error("incompatible_capability", lambda: resolve(self.spec(doc), self.root))

    def test_cycle_is_rejected_before_any_lifecycle_side_effect(self):
        # Only this algorithmic negative fixture alters a descriptor; positive
        # composition above uses the installed, unmodified entry point graph.
        registry = Registry()
        selected = registry.provider("tool.mock_arm_monitor")
        selected.descriptor = ComponentDescriptor(id="tool.mock_arm_monitor", kind="tool", version="0.1.0",
            provides=(Capability(contract="monitor.device_state/v1"),),
            dependencies=(DependencyPort(name="device", capability=Capability(contract="monitor.device_state/v1")),))
        doc = self.graph()
        doc["components"]["monitor"]["bindings"]["device"] = "monitor"
        self.error("dependency_cycle", lambda: resolve(self.spec(doc), self.root, registry))
        self.assertFalse((self.root / ".system-plan").exists())

    def test_missing_plugin_and_version_mismatch_are_rejected(self):
        for component, version, code in [("embodiment.absent", "", "component_not_found"),
                                         ("embodiment.mock_arm", ">=9", "incompatible_version")]:
            doc = default_spec()
            doc["components"]["arm"].update(component=component, version=version)
            self.error(code, lambda: resolve(self.spec(doc), self.root))

    def test_invalid_configuration_is_rejected_in_plan(self):
        doc = default_spec({"unknown": True})
        self.error("invalid_config", lambda: resolve(self.spec(doc), self.root))

    def test_lock_covers_configuration_and_installed_code_identity(self):
        one = resolve(self.spec(), self.root)
        two = resolve(self.spec(default_spec({"resource_id": "robots/second-arm"})), self.root)
        self.assertNotEqual(one.fingerprint, two.fingerprint)
        one.lock["order"] = ["tampered"]
        def opening():
            with open_system(one, self.root / "runtime"):
                self.fail("Must reject changed lock before opening")
        self.error("lock_changed", opening)
        self.assertFalse((self.root / "runtime").exists())

    def test_duplicate_resource_exports_close_both_handles(self):
        doc = default_spec()
        doc["components"]["second"] = copy.deepcopy(doc["components"]["arm"])
        doc["exports"].append("second")
        selected = resolve(self.spec(doc), self.root)
        from metaos_mock_arm import MockArm
        closed, original = [], MockArm.close
        def closing(arm):
            original(arm)
            closed.append(arm.closed)
        def opening():
            with open_system(selected, self.root / "runtime"):
                self.fail("Duplicate resource id must not open")
        with patch.object(MockArm, "close", closing):
            self.error("duplicate_resource", opening)
        self.assertEqual(closed, [True, True])

    def test_non_device_export_closes_every_handle(self):
        doc = self.graph()
        doc["exports"] = ["monitor"]
        selected = resolve(self.spec(doc), self.root)
        def opening():
            with open_system(selected, self.root / "runtime"):
                self.fail("Non-device export must be rejected")
        self.error("invalid_resource", opening)

    def test_existing_ready_path_is_not_overwritten_or_removed(self):
        from metaos_runtime.server import create_credentials, serve
        ready = self.root / "ready.json"
        ready.write_text("owned by another run")
        credentials = self.root / "credentials.json"
        create_credentials(credentials)
        self.error("ready_file_exists", lambda: serve(self.spec(), root=self.root, workspace=self.root / "runtime",
            credentials=credentials, ready_file=ready))
        self.assertEqual(ready.read_text(), "owned by another run")
        self.assertFalse((self.root / "runtime").exists())


if __name__ == "__main__":
    unittest.main()
