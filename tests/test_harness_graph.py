"""Small contract checks; no model, simulator or campaign runs."""
from dataclasses import replace
import unittest

from metaos_harness.graph import Descriptor, Host, Provider, Registry, resolve


class HarnessGraphTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        events = self.events
        class Handle:
            def __init__(self, config, bindings, runtime):
                self.name = config.get("name", "root")
                self.bindings = bindings
                events.append("open:" + self.name)
            def close(self):
                events.append("close:" + self.name)
        self.leaf = Provider(Descriptor("test.leaf", "1.0", "test.device/v1",
            config_schema={"type": "object"}, semantics={"controller": "JOINT_POSITION"}), Handle)
        self.root = Provider(Descriptor("test.root", "1.0", "test.root/v1", {"device": "test.device/v1"},
            port_semantics={"device": {"controller": "JOINT_POSITION"}}), Handle)
        self.registry = Registry({"test.leaf": self.leaf, "test.root": self.root})
        self.spec = {"api": "metaos.harness-spec/v1", "name": "test", "entrypoint": "root", "components": {
            "leaf": {"component": "test.leaf", "config": {"name": "leaf"}},
            "root": {"component": "test.root", "bindings": {"device": "leaf"}}}}

    def test_plan_has_no_open_side_effects(self):
        self.assertEqual(resolve(self.spec, self.registry).order, ["leaf", "root"])
        self.assertEqual(self.events, [])

    def test_host_binds_real_dependency_and_closes_reverse(self):
        with Host(resolve(self.spec, self.registry)) as root:
            self.assertEqual(root.bindings["device"].name, "leaf")
        self.assertEqual(self.events, ["open:leaf", "open:root", "close:root", "close:leaf"])

    def test_cleanup_failure_attached_to_primary_error(self):
        original = self.root.factory
        def factory(config, bindings, runtime):
            value = original(config, bindings, runtime)
            def close():
                self.events.append("close:root")
                raise OSError("fixture cleanup failed")
            value.close = close
            return value
        self.root.factory = factory
        primary = ValueError("fixture execution failed")
        with self.assertRaises(ValueError) as caught:
            with Host(resolve(self.spec, self.registry)):
                raise primary
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.cleanup_errors[0]["error_type"], "OSError")
        self.assertEqual(self.events[-2:], ["close:root", "close:leaf"])
        with self.assertRaises(OSError):
            with Host(resolve(self.spec, self.registry)):
                pass

    def test_partial_open_failure_keeps_primary_when_cleanup_also_fails(self):
        original = self.leaf.factory
        def factory(config, bindings, runtime):
            value = original(config, bindings, runtime)
            def close():
                raise OSError("fixture cleanup failed")
            value.close = close
            return value
        self.leaf.factory = factory
        def fail(*args):
            raise ValueError("fixture open failed")
        self.root.factory = fail
        with self.assertRaisesRegex(ValueError, "fixture open failed") as caught:
            with Host(resolve(self.spec, self.registry)):
                pass
        self.assertEqual(caught.exception.cleanup_errors[0]["component"], "leaf")

    def test_missing_binding_rejected(self):
        self.spec["components"]["root"]["bindings"] = {}
        with self.assertRaisesRegex(ValueError, "ports"):
            resolve(self.spec, self.registry)

    def test_osc_cannot_replace_joint_environment_silently(self):
        self.leaf.descriptor = replace(self.leaf.descriptor, semantics={"controller": "OSC_POSE"})
        with self.assertRaisesRegex(ValueError, "Semantic mismatch"):
            resolve(self.spec, self.registry)

    def test_new_provider_can_replace_same_contract(self):
        substitute = Provider(replace(self.leaf.descriptor, id="test.replacement"), self.leaf.factory)
        self.registry.providers["test.replacement"] = substitute
        self.spec["components"]["leaf"]["component"] = "test.replacement"
        with Host(resolve(self.spec, self.registry)) as root:
            self.assertEqual(root.bindings["device"].name, "leaf")

    def test_wrong_contract_rejected(self):
        self.leaf.descriptor = replace(self.leaf.descriptor, provides="test.other/v1")
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            resolve(self.spec, self.registry)

    def test_lock_drift_rejected(self):
        resolved = resolve(self.spec, self.registry)
        with self.assertRaisesRegex(ValueError, "lock"):
            Host(resolved, expected_lock={})

    def test_cycle_rejected(self):
        self.leaf.descriptor = replace(self.leaf.descriptor, requires={"root": "test.root/v1"})
        self.spec["components"]["leaf"]["bindings"] = {"root": "root"}
        with self.assertRaisesRegex(ValueError, "cycle"):
            resolve(self.spec, self.registry)

    def test_partial_open_cleans_up(self):
        def broken(*args):
            raise RuntimeError("deliberate")
        self.root.factory = broken
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            with Host(resolve(self.spec, self.registry)):
                pass
        self.assertEqual(self.events, ["open:leaf", "close:leaf"])


if __name__ == "__main__":
    unittest.main()
