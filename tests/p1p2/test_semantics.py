import json
import math
import unittest
from pathlib import Path

from metaos_contracts import SystemError
from metaos_runtime.composition import load_spec, open_system, resolve

from helpers import RunningManager

PROJECT = Path(__file__).resolve().parents[2]


class SemanticTests(unittest.TestCase):
    def system(self, document):
        s = RunningManager(self._testMethodName)
        s.spec.write_text(json.dumps(document))
        self.addCleanup(s.stop)
        return s.start()

    def test_explicit_degrees_adapter_converts_action_state_and_feedback(self):
        original = load_spec(PROJECT / "configs/managed-degrees.yaml").model_dump(mode="json")
        s = self.system(original)
        resource = "robots/degrees-arm"
        lease = s.a.acquire(resource, 30.)
        command = s.a.build_command(lease, "manipulation.move_joint_degrees/v1", {"positions": [90., -90.], "duration_s": .1})
        s.a.submit(command)
        result = s.a.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["feedback"]["positions"], [90., -90.])
        state = s.a.state(resource)
        self.assertEqual(state["data"]["units"], ["deg", "deg"])
        self.assertEqual(state["data"]["positions"], [90., -90.])
        for changes, expected in [({"units": ["rad", "rad"]}, "semantic_mismatch"),
                ({"frame_id": "world"}, "semantic_mismatch"),
                ({"arguments": {"positions": [90.], "duration_s": .1}}, "invalid_action")]:
            request = {**s.a.build_command(lease, command["capability"], command["arguments"]), **changes}
            with self.assertRaises(SystemError) as caught:
                s.a.submit(request)
            self.assertEqual(caught.exception.code, expected)
        s.a.release(lease)

    def test_missing_explicit_adapter_binding_fails_before_open(self):
        spec = load_spec(PROJECT / "configs/managed-degrees.yaml")
        document = spec.model_dump(mode="json")
        document["components"]["degrees"]["bindings"] = {}
        with self.assertRaises(SystemError) as caught:
            resolve(type(spec).model_validate_json(json.dumps(document)), PROJECT)
        self.assertEqual(caught.exception.code, "missing_binding")

    def test_raw_and_adapter_cannot_export_two_control_domains_for_one_device(self):
        spec = load_spec(PROJECT / "configs/managed-degrees.yaml")
        document = spec.model_dump(mode="json")
        document["exports"] = ["arm", "degrees"]
        document["resources"]["arm"] = "robots/internal-arm"
        s = RunningManager(self._testMethodName)
        resolved = resolve(type(spec).model_validate_json(json.dumps(document)), s.root)
        with self.assertRaises(SystemError) as caught:
            with open_system(resolved, s.root / "unopened"):
                self.fail("Overlapping controllers opened")
        self.assertEqual(caught.exception.code, "control_domain_conflict")

    def test_declared_file_lock_is_portable_and_still_binds_asset_bytes(self):
        s = RunningManager(self._testMethodName)
        spec = load_spec(PROJECT / "configs/managed-libero.yaml")
        for name in ("before", "relocated"):
            path = s.root / name / "assets/libero"
            path.mkdir(parents=True)
            (path / "protocol.yaml").write_text("benchmark: libero_pro\n")
        before = resolve(spec, s.root / "before")
        relocated = resolve(spec, s.root / "relocated")
        self.assertEqual(before.lock, relocated.lock)
        self.assertNotIn(str(s.root), json.dumps(before.lock))
        (s.root / "relocated/assets/libero/protocol.yaml").write_text("benchmark: changed\n")
        self.assertNotEqual(before.lock, resolve(spec, s.root / "relocated").lock)
