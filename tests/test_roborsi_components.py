"""Focused dependency-seam and single-episode protocol checks, no API calls."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from metaos_roborsi.components import ComposedSkills, Orchestrator, normalize_image_mime
from roborsi.embodied.agent_loop.env import Observation


class RoboRSIComponentTests(unittest.TestCase):
    def test_orbit_png_mime_is_corrected_without_changing_image_bytes(self):
        import base64
        # A real one-pixel PNG, presented exactly like upstream's image helper.
        data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB9kAAAAASUVORK5CYII="
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + data, "detail": "high"}}]}]
        fixed = normalize_image_mime(messages)
        url = fixed[0]["content"][0]["image_url"]["url"]
        self.assertEqual(url, "data:image/png;base64," + data)
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), base64.b64decode(data))
        self.assertEqual(fixed[0]["content"][0]["image_url"]["detail"], "high")
        self.assertTrue(messages[0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg"))

    def test_extracted_rollout_uses_injected_model_context_and_tools(self):
        from roborsi.embodied.agent_loop.rollout import run_rollout
        from roborsi.runtime_mode import use_run_mode
        seen = []
        class Model:
            def call(self, model, messages, tools):
                name = "look" if not seen else "done"
                seen.append("model:" + name)
                return SimpleNamespace(content="", tool_calls=[SimpleNamespace(id=str(len(seen)),
                    function=SimpleNamespace(name=name, arguments='{"success": false}' if name == 'done' else '{}'))])
        class Context:
            def initial_messages(self, *args, **kwargs):
                return [{"role": "system", "content": "injected"}, {"role": "user", "content": "test"}]
            def summarize(self, messages, **kwargs):
                return messages
        class Tools:
            def specs(self, **kwargs):
                return []
            def dispatch(self, state, call, **kwargs):
                seen.append("dispatch:" + call["tool"])
                return {"ok": True}, Observation()
        class Env:
            backend_name = "libero-pro"
            def take_snapshot(self):
                return Observation()
            def tool_handlers(self):
                return {}
            def hook_physics_step(self, callback):
                return lambda: None
            def check_success(self):
                raise AssertionError("Mid-round simulator adjudication is prohibited")
        with tempfile.TemporaryDirectory() as root, use_run_mode("frozen"):
            result = run_rollout(Env(), seed=0, task_name="libero_pick_place", instruction="test",
                expected_on_success="test", tool_budget=2, workdir=Path(root), use_sim_predicate=False,
                services={"model": Model(), "context": Context(), "tools": Tools()})
        self.assertEqual(seen, ["model:look", "dispatch:look", "model:done"])
        self.assertEqual(len(result.trace), 2)
        self.assertIsNone(result.rollout.meta["predicate_check"])

    def test_multi_round_budget_one_reset_final_adjudication(self):
        from roborsi.agents.memory.workspace import Workspace
        events = []
        class Results:
            def __init__(self, root):
                self.root = root
            def workspace(self, task):
                return Workspace(task, "test", self.root)
            def event(self, kind, **kwargs):
                pass
            def finish(self, result):
                return result
        class Env:
            instruction = "runtime instruction"
            def reset(self, *args, **kwargs):
                events.append("reset")
        class Planner:
            def plan(self, **kwargs):
                events.append("planner")
                self.assert_request = "runtime instruction" in kwargs["user_msg"]
                return {"goal": "test"}
        class Engineer:
            budgets = []
            def execute(self, **kwargs):
                assert kwargs["reset_env"] is False and kwargs["adjudicate"] is False
                self.budgets.append(kwargs["tool_budget"])
                events.append("engineer")
                return {"tool_calls": 5, "trace": [{"tool_call": {"tool": "look"}}] * 5,
                    "rollout_meta": {"vlm_declared": False}, "outcome": "budget_exceeded"}
        class Reviewer:
            def review(self, **kwargs):
                assert "success" not in kwargs["engineer_result"]
                events.append("reviewer")
                return {"verdict": "continue" if events.count("reviewer") == 1 else "blocked"}
        class Evaluator:
            def adjudicate(self, *args):
                events.append("adjudicate")
                return {"success": False, "outcome": "budget_exceeded", "native": {}}
        class Tools:
            def specs(self, **kwargs):
                return []
        engineer = Engineer()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ):
            graph = Orchestrator({"max_rounds": 6}, {"results": Results(Path(root)), "environment": Env(),
                "planner": Planner(), "engineer": engineer, "reviewer": Reviewer(), "evaluator": Evaluator(), "tools": Tools()}, {})
            result = graph.run({"atomic": "libero_pick_place", "sim_task": "libero_goal_task/0", "seed": 0, "tool_budget": 24})
        self.assertEqual(engineer.budgets, [24, 19])
        self.assertEqual(events, ["reset", "planner", "engineer", "reviewer", "planner", "engineer", "reviewer", "adjudicate"])
        self.assertEqual(result["tool_calls"], 10)

    def test_skill_composition_routes_and_rejects_implicit_override(self):
        class Pack:
            def __init__(self, name):
                self.name = name
            def specs(self, **kwargs):
                return [{"function": {"name": self.name, "parameters": {"type": "object"}}}]
            def dispatch_native(self, state, call):
                return self.name
        pack = ComposedSkills({}, {"base": Pack("look"), "extension": Pack("new_skill")}, {})
        state = SimpleNamespace(ns="libero", task="libero_pick_place")
        self.assertEqual(len(pack.specs()), 2)
        self.assertEqual(pack.dispatch_native(state, {"tool": "new_skill"}), "new_skill")
        bad = ComposedSkills({}, {"base": Pack("look"), "extension": Pack("look")}, {})
        with self.assertRaisesRegex(ValueError, "override"):
            bad.specs()


if __name__ == "__main__":
    unittest.main()
