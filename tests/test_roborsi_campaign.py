"""Campaign selection/recovery checks with explicit fixtures, no model or GPU."""
import fcntl
import json
from pathlib import Path
import tempfile
import unittest

from metaos_roborsi_campaign.campaign import Campaign
from metaos_roborsi_campaign.panel import API, TASKS, experiment, select, validate
from metaos_roborsi_campaign.storage import atomic


def panel(size=120):
    return {"api": API, "tasks": list(TASKS[:size]), "seed": 0, "tool_budget": 24,
            "atomic": "libero_pick_place", "instruction": "fixture"}


class FixtureExecutor:
    def __init__(self, outcomes=None, interrupt_after_result=False):
        self.outcomes = outcomes or {}
        self.calls = []
        self.interrupt = interrupt_after_result

    def panel(self):
        return panel()

    def identity(self):
        return {"fixture": True, "panel": self.panel()}

    def validate_output(self, output):
        pass

    def recover(self, index, run):
        path = Path(run) / "fixture-result.json"
        return json.loads(path.read_text()) if path.exists() else {
            "status": "infrastructure_error", "cleanup_confirmed": True}

    def execute(self, index, run, *, lock_fd):
        self.calls.append(index)
        run.mkdir(parents=True)
        outcomes = self.outcomes.get(index, ["success"])
        status = outcomes[min(self.calls.count(index) - 1, len(outcomes) - 1)]
        result = {"status": status, "cleanup_confirmed": status != "interrupted",
                  "evidence_sha256": f"fixture:{index}:{status}"}
        atomic(run / "fixture-result.json", result)
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt()
        return result


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "campaign"

    def campaign(self, executor=None, **config):
        self.executor = executor or FixtureExecutor()
        return Campaign({"count": 3, "infra_retries": 1, **config}, {"executor": self.executor},
                        {"run_dir": str(self.root), "fingerprint": "fixture-graph"})

    def test_selected_counts_and_stable_balanced_prefixes(self):
        self.assertEqual(len(set(TASKS)), 120)
        for count in [10, 20, 120, "all"]:
            chosen = select(TASKS, count)
            self.assertEqual(len(chosen), 120 if count == "all" else count)
        chosen = select(TASKS, 10)
        self.assertEqual(len({task.split('/')[0] for task in chosen}), 10)
        self.assertEqual(select(TASKS, 10, "random", 7), select(TASKS, 10, "random", 7))
        self.assertNotEqual(select(TASKS, 10, "random", 7), select(TASKS, 10, "random", 8))
        for count in [0, 121, True]:
            with self.assertRaises(ValueError):
                select(TASKS, count)

    def test_panel_rejects_duplicates_unknown_tasks_and_index_escape(self):
        for tasks in [[TASKS[0], TASKS[0]], ["libero_10/0"]]:
            bad = panel()
            bad["tasks"] = tasks
            with self.assertRaises(Exception):
                validate(bad)
        for index in [-1, 120, True]:
            with self.assertRaises(ValueError):
                experiment(panel(), index)
        self.assertEqual(experiment(panel(), 10)["sim_task"], TASKS[10])

    def test_task_failure_is_valid_and_only_clean_infrastructure_is_retried(self):
        executor = FixtureExecutor({0: ["task_failure"], 1: ["infrastructure_error", "success"]})
        result = self.campaign(executor).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["valid_episodes"], 3)
        self.assertEqual(result["task_failure"], 1)
        self.assertEqual(result["attempt_counts"]["infrastructure_error"], 1)
        self.assertEqual(executor.calls, [0, 1, 1, 2])

    def test_exhausted_infrastructure_is_excluded_from_success_denominator(self):
        result = self.campaign(FixtureExecutor({0: ["infrastructure_error"]}), count=2).run()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["valid_episodes"], 1)
        self.assertEqual(result["success_rate_valid"], 1.0)
        self.assertEqual(result["success_rate_planned"], .5)
        self.assertEqual(result["task_failure"], 0)

    def test_cleanup_unconfirmed_blocks_following_tasks(self):
        executor = FixtureExecutor({0: ["interrupted"]})
        result = self.campaign(executor).run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(executor.calls, [0])
        self.assertEqual(result["valid_episodes"], 0)

    def test_resume_retries_exhausted_infrastructure_without_replaying_valid_tasks(self):
        first = self.campaign(FixtureExecutor({0: ["infrastructure_error"]}), count=2).run()
        self.assertEqual(first["status"], "partial")
        executor = FixtureExecutor()
        result = self.campaign(executor, count=2).run(resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(executor.calls, [0])

    def test_implementation_error_stops_without_task_failure_or_retry(self):
        executor = FixtureExecutor({0: ["implementation_error"]})
        result = self.campaign(executor).run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["attempt_counts"]["implementation_error"], 1)
        self.assertEqual(executor.calls, [0])

    def test_verification_failure_is_not_retried_or_counted_as_task_failure(self):
        executor = FixtureExecutor({0: ["verification_error"]})
        result = self.campaign(executor).run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(executor.calls, [0])
        self.assertEqual(result["task_failure"], 0)

    def test_resume_revalidates_completed_episodes_without_replaying(self):
        self.campaign().run()
        replacement = FixtureExecutor()
        result = self.campaign(replacement).run(resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(replacement.calls, [])
        with self.assertRaises(ValueError):
            self.campaign(replacement, count=2).run(resume=True)

    def test_crash_after_episode_result_recovers_without_replaying(self):
        result = self.campaign(FixtureExecutor(interrupt_after_result=True)).run()
        self.assertEqual(result["status"], "cancelled")
        executor = FixtureExecutor()
        result = self.campaign(executor).run(resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(executor.calls, [1, 2])

    def test_torn_append_is_retained_and_recovered_but_sealed_corruption_is_rejected(self):
        self.campaign().run()
        path = self.root / "episodes.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"event":"sta')
        self.campaign().run(resume=True)
        self.assertTrue(list(self.root.glob("episodes.partial-*.bin")))
        data = path.read_text().replace('"task":"libero_spatial_task/0"', '"task":"libero_spatial_task/9"', 1)
        path.write_text(data)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.campaign().run(resume=True)

    def test_completed_result_tampering_is_rejected(self):
        self.campaign().run()
        path = next(self.root.rglob("fixture-result.json"))
        value = json.loads(path.read_text())
        value["status"] = "task_failure"
        atomic(path, value)
        with self.assertRaisesRegex(ValueError, "no longer verifies"):
            self.campaign().run(resume=True)

    def test_concurrent_campaign_is_refused(self):
        self.campaign().run()
        with (self.root / "campaign.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "still running"):
                self.campaign().run(resume=True)


if __name__ == "__main__":
    unittest.main()
