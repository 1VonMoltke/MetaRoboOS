"""Device protocol fixtures only: no Docker execution, simulator or model."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from metaos_contracts import SystemError
from metaos_robocasa_device import ACTION, RESET, END, FINISH, ABORT, Provider, RoboCasaDevice
from roborsi.contracts import ComponentContext, ComponentSpec


TASKS = [{"benchmark": "robocasa365", "task_id": "OpenDrawer", "seed": seed,
    "instruction": "fixture", "max_steps": 400,
    "native": {"protocol": "robocasa-harness-vla-v1", "environment_split": "target", "timeout_s": 1800},
    "capabilities": {"action_schema": {"type": "object", "properties": {"primitive": {"enum": ["move_to", "finish"]}}, "required": ["primitive"]}}}
    for seed in (1, 2)]


class NativeFixture(RoboCasaDevice):
    catalog = TASKS

    def launch(self):
        self.calls = []

    def remove_container(self):
        self.removed = True

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path == "/v1/tasks":
            return {"tasks": self.catalog}
        if path == "/v1/health":
            return {"status": "ready"}
        if path.endswith("/artifacts"):
            return {"artifacts": []}
        if path.endswith(("/close", "/cancel")):
            return {}
        if method == "GET":
            return {"state": "cancelled"}
        if path == "/v1/episodes":
            return {"payload": {"task": body["payload"], "observation": {"step_index": 0}}}
        if path.endswith("/finish"):
            return {"payload": {"success": False, "score": 0.}}
        if path.endswith("/query"):
            return {"payload": {"query_id": body["payload"]["query_id"], "data": {}}}
        return {"payload": {"observation": {"step_index": 1}, "terminated": False, "truncated": path.endswith("/stop")}}


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = NativeFixture({"image": "sha256:" + "0" * 64, "gpu_device": 0}, self.root)
        self.addCleanup(self.device.close)

    def execute(self, cap, **arguments):
        operation = self.device.start(cap, arguments)
        self.assertTrue(operation.done.wait(3))
        operation.tick(0.)

    def reset(self, seed=1):
        self.execute(RESET, task_id="OpenDrawer", seed=seed, episode_id="episode")

    def test_pair_reset_preserves_seed_and_native_task_budget(self):
        self.reset(2)
        call = next(row for row in self.device.calls if row[1] == "/v1/episodes")
        self.assertEqual(call[2]["payload"], TASKS[1])
        descriptor = self.device.describe_resource()
        self.assertTrue(descriptor.metadata["episode_stop_supported"])
        self.assertEqual(descriptor.capabilities[0].max_command_validity_s, 3600.)

    def test_missing_or_unlisted_seed_rejected_without_native_reset(self):
        for arguments in ({"task_id": "OpenDrawer", "episode_id": "episode"},
                {"task_id": "OpenDrawer", "seed": 0, "episode_id": "episode"},
                {"task_id": "OpenDrawer", "seed": 3, "episode_id": "episode"}):
            with self.subTest(arguments=arguments), self.assertRaises(SystemError):
                self.device.validate_action(RESET, arguments)
        self.assertFalse(any(row[1] == "/v1/episodes" for row in self.device.calls))

    def test_tool_argument_errors_are_left_to_original_toolkit(self):
        self.reset()
        self.execute(ACTION, episode_id="episode", action={"primitive": "move_to"})
        call = next(row for row in self.device.calls if row[1].endswith("/actions"))
        self.assertEqual(call[2]["kind"], "action")
        self.assertEqual(call[2]["payload"]["data"], {"primitive": "move_to"})
        self.assertEqual(call[2]["payload"]["step_index"], 0)

    def test_scored_stop_and_unscored_cancel_use_distinct_protocols(self):
        self.reset()
        with self.assertRaises(SystemError):
            self.device.validate_action(FINISH, {"episode_id": "episode"})
        self.execute(END, episode_id="episode", reason="policy_budget")
        call = next(row for row in self.device.calls if row[1].endswith("/stop"))
        self.assertEqual(call[2]["kind"], "episode_stop")
        self.assertEqual(call[2]["payload"]["reason"], "policy_budget")
        self.execute(FINISH, episode_id="episode")
        self.assertEqual(self.device.last_reply["payload"]["score"], 0.)

    def test_cancellation_never_requests_a_score(self):
        self.reset()
        self.execute(ABORT, episode_id="episode")
        self.assertTrue(self.device.last_reply["payload"]["stopped"])
        self.assertFalse(any(row[1].endswith("/finish") for row in self.device.calls))

    def test_stale_episode_and_reused_identity_rejected(self):
        self.reset()
        with self.assertRaises(SystemError):
            self.device.validate_action(ACTION, {"episode_id": "old", "action": {"primitive": "finish"}})
        self.execute(ABORT, episode_id="episode")
        with self.assertRaises(SystemError):
            self.device.validate_action(RESET, {"task_id": "OpenDrawer", "seed": 1, "episode_id": "episode"})

    def test_unsupported_native_query_is_not_advertised(self):
        self.assertFalse(any("query" in cap.id for cap in self.device.describe_resource().capabilities))

    def test_wrong_protocol_catalog_rejected_and_cleaned(self):
        class Wrong(NativeFixture):
            catalog = [{**TASKS[0], "native": {"protocol": "other"}}]
        with self.assertRaises(SystemError):
            Wrong({"image": "sha256:" + "0" * 64, "gpu_device": 0}, self.root / "bad")

    def test_provider_plan_never_opens_docker_or_imports_simulator(self):
        provider = Provider()
        context = ComponentContext(self.root / "plan", root=self.root)
        with patch("subprocess.run", side_effect=AssertionError("planning must not spawn processes")):
            plan = provider.plan(ComponentSpec(component="environment.robocasa_device", config={"image": "sha256:" + "0" * 64, "gpu_device": 0}), context)
        self.assertEqual(json.loads(plan.metadata_json)["protocol"], "robocasa-harness-vla-v1")
        for config in ({"image": "latest", "gpu_device": 0}, {"image": "sha256:" + "0" * 64, "gpu_device": -1}):
            with self.assertRaises(SystemError):
                provider.plan(ComponentSpec(component="environment.robocasa_device", config=config), context)
