"""Exercise the process boundary with labeled fixture scripts, no real episodes."""
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from metaos_harness.graph import digest
from metaos_roborsi_campaign.executor import EpisodeCancelled, LocalExecutor
from metaos_roborsi_campaign.panel import API, INDEX_ENV, TASKS


class LocalExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.panel = {"api": API, "tasks": list(TASKS[:3]), "seed": 0, "tool_budget": 24,
            "atomic": "libero_pick_place", "instruction": "fixture"}
        (self.root / "panel.json").write_text(json.dumps(self.panel))
        (self.root / "native.json").write_text(json.dumps({"native_config": {
            "tasks": self.panel["tasks"], "run_root": str(self.root / "runs")}}))
        document = {"application": "harness", "system": "system", "components": {
            "harness": {"bindings": {"assets": "assets"}, "descriptor": {
                "env": [INDEX_ENV], "command": ["metaos_roborsi_campaign.episode"]}},
            "assets": {"paths": {"experiment": {"path": str(self.root / "panel.json")}}},
            "system": {"paths": {"native-runtime": {"path": str(self.root / "native.json")}}}}}
        document["fingerprint"] = digest(document)
        (self.bundle / "bundle.json").write_text(json.dumps(document))
        (self.bundle / "system.lock.json").write_text('{}')
        composer = self.root / "fixture-composer.py"
        composer.write_text('''import json,os,sys
from pathlib import Path
output=Path(sys.argv[sys.argv.index('--output')+1]);output.mkdir(parents=True)
(output/'fixture-index.json').write_text(json.dumps(int(os.environ['METAOS_ROBORSI_EPISODE_INDEX'])))
(output/'run.json').write_text(json.dumps({'status':'application_completed','cleanup_confirmed':True,'processes':[]}))
''')
        verifier = self.root / "fixture-verifier.py"
        verifier.write_text('''import json,sys
from pathlib import Path
run=Path(sys.argv[sys.argv.index('--run')+1]);out=Path(sys.argv[sys.argv.index('--output')+1])
index=json.loads((run/'fixture-index.json').read_text())
root=Path(__file__).parent; panel=json.loads((root/'panel.json').read_text());bundle=json.loads((root/'bundle/bundle.json').read_text())
out.write_text(json.dumps({'verified':True,'task':panel['tasks'][index],'seed':panel['seed'],
'bundle_fingerprint':bundle['fingerprint'],'task_success':False,'physics_ticks':0,'tool_calls':1,
'fixture':True,'campaign_flag':'--campaign-episode' in sys.argv}))
''')
        self.config = {"bundle": str(self.bundle), "composer": str(composer),
            "composer_script": str(composer), "verifier": str(verifier), "timeout_s": 60}

    def test_selection_crosses_real_subprocess_and_verified_failure_is_retained(self):
        executor = LocalExecutor(self.config, {}, {})
        run = self.root / "runs" / "attempt" / "run"
        with (self.root / "campaign.lock").open('a+') as lock:
            result = executor.execute(2, run, lock_fd=lock.fileno())
        self.assertEqual(result["status"], "task_failure")
        self.assertEqual(result["physics_ticks"], 0)
        report = json.loads((run.parent / "audit.json").read_text())
        self.assertTrue(report["fixture"] and report["campaign_flag"])
        self.assertEqual(report["task"], self.panel["tasks"][2])

    def test_changed_input_is_rejected_before_launch(self):
        executor = LocalExecutor(self.config, {}, {})
        Path(self.config["verifier"]).write_text('raise RuntimeError("changed fixture")')
        with self.assertRaisesRegex(ValueError, "changed"):
            executor.identity()

    def test_output_outside_declared_root_is_rejected(self):
        executor = LocalExecutor(self.config, {}, {})
        with self.assertRaisesRegex(ValueError, "run_root"):
            executor.validate_output(self.root / "elsewhere")

    def test_missing_credential_is_rejected_before_any_process_launch(self):
        executor = LocalExecutor(self.config, {}, {})
        with patch.dict('os.environ', {}, clear=True), patch('subprocess.run') as launch:
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                executor.preflight()
            launch.assert_not_called()

    def test_failure_after_spawn_still_retires_the_owned_launcher(self):
        composer = Path(self.config["composer"])
        composer.write_text('import time; time.sleep(30)\n')
        executor = LocalExecutor(self.config, {}, {})
        processes = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with (self.root / "campaign.lock").open('a+') as lock:
            with patch('metaos_roborsi_campaign.executor.atomic', side_effect=OSError('fixture write failure')):
                with patch('metaos_roborsi_campaign.executor.subprocess.Popen', side_effect=spawn):
                    with self.assertRaises(EpisodeCancelled) as interrupted:
                        executor.execute(0, self.root/'runs/attempt/run', lock_fd=lock.fileno())
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(interrupted.exception.result["reason"], "OSError")


if __name__ == "__main__":
    unittest.main()
