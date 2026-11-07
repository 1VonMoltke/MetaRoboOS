"""Local composition tests against the actual qualified Manager/Worker stack."""

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).absolute().parents[2]
module = importlib.util.spec_from_file_location("local_composer", ROOT / "scripts/metaos_local.py")
local = importlib.util.module_from_spec(module)
module.loader.exec_module(local)


class LocalCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "work")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pool = self.root / "pool"
        local.init_pool(self.pool, ROOT / "evidence/p1p2-latest.json")
        self.spec = self.pool / "mock.json"
        self.bundle = self.root / "bundle"

    def change(self, resource, update):
        path = self.pool / resource / "resource.json"
        value = local.read(path)
        update(value)
        local.write(path, value)

    def build(self):
        return local.build(self.spec, self.pool, self.bundle)

    def app(self, text):
        path = self.root / "app.py"
        path.write_text(text)
        def update(value):
            value["paths"]["script"] = {"path": str(path), "mode": "file"}
            value["command"] = ["{self.paths.python}", "-I", "-B", "{self.paths.script}"]
        self.change("mock-check", update)

    def assert_closed(self, result):
        self.assertTrue(result["cleanup_confirmed"])
        report = local.read(Path(result["output"]) / "run.json")
        for item in report["processes"]:
            self.assertNotEqual(local.proc_identity(item["pid"]), tuple(item["identity"]) if item["identity"] else "missing")
        self.assertTrue(report["system_shutdown"]["handles_closed"])
        self.assertEqual(report["system_shutdown"]["active_leases"], 0)

    def test_actual_actions_cancel_expire_and_cleanup(self):
        self.build()
        result = local.run(self.bundle, self.root / "run", 20)
        self.assertEqual(result["status"], "application_completed")
        actions = local.read(self.root / "run/actions.json")
        self.assertEqual([item["case"] for item in actions["cases"]], ["normal", "cancel", "expire"])
        self.assert_closed(result)

    def test_content_drift_rejected_before_run_is_created(self):
        self.app("print('first')\n")
        self.build()
        (self.root / "app.py").write_text("print('changed')\n")
        with self.assertRaisesRegex(ValueError, "resource changed"):
            local.run(self.bundle, self.root / "run", 20)
        self.assertFalse((self.root / "run").exists())

    def test_tree_addition_is_drift(self):
        assets = self.pool / "data"
        assets.mkdir()
        weights = assets / "weights"
        weights.mkdir()
        (weights / "config.json").write_text('{}')
        local.write(assets / "resource.json", {"api": local.RESOURCE_API, "kind": "asset", "provides": ["test.weights/v1"],
            "paths": {"weights": {"path": "weights", "mode": "tree"}}})
        self.change("mock-check", lambda r: r["requires"].update(weights="test.weights/v1"))
        spec = local.read(self.spec)
        spec["components"]["weights"] = {"resource": "data"}
        spec["components"]["harness"]["bindings"]["weights"] = "weights"
        local.write(self.spec, spec)
        self.build()
        (weights / "extra.safetensors").write_bytes(b'new weights')
        with self.assertRaisesRegex(ValueError, "resource changed"):
            local.verify(self.bundle)

    def test_bundle_edit_is_rejected(self):
        self.build()
        path = self.bundle / "bundle.json"
        value = local.read(path)
        value["order"].reverse()
        local.write(path, value)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            local.verify(self.bundle)

    def test_dependency_errors_and_unknown_placeholders(self):
        self.change("mock-check", lambda r: r["requires"].update(system="incompatible/v1"))
        with self.assertRaisesRegex(ValueError, "Incompatible port"):
            local.compose(self.spec, self.pool)
        self.change("mock-check", lambda r: r["requires"].update(system="metaos.system/v1"))
        self.change("mock-check", lambda r: r["command"].append("{system.not_available}"))
        with self.assertRaisesRegex(ValueError, "Unbound template"):
            self.build()
        self.assertFalse(self.bundle.exists())

    def test_path_escape_and_cycles_are_rejected(self):
        spec = local.read(self.spec)
        bad = copy.deepcopy(spec)
        bad["components"]["system"]["resource"] = "../../elsewhere"
        local.write(self.spec, bad)
        with self.assertRaisesRegex(ValueError, "escapes"):
            local.compose(self.spec, self.pool)
        spec["components"]["system"]["bindings"] = {"loop": "harness"}
        self.change("mock", lambda r: r.update(requires={"loop": "test.loop/v1"}))
        local.write(self.spec, spec)
        with self.assertRaisesRegex(ValueError, "cycle"):
            local.compose(self.spec, self.pool)

    def test_application_failure_still_cleans_system(self):
        self.app("raise SystemExit(7)\n")
        self.build()
        result = local.run(self.bundle, self.root / "run", 20)
        self.assertEqual(result["status"], "application_failed")
        self.assertEqual(local.read(self.root / "run/run.json")["application_exit_code"], 7)
        self.assert_closed(result)

    def test_timeout_stops_owned_application_and_system(self):
        self.app("import time\nfrom pathlib import Path\nPath('started').write_text('yes')\ntime.sleep(60)\n")
        self.build()
        result = local.run(self.bundle, self.root / "run", 10)
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertTrue((self.root / "run/started").exists())
        self.assertEqual(local.read(self.root / "run/run.json")["error_type"], "TimeoutError")
        self.assert_closed(result)

    def test_service_outputs_and_real_http_readiness(self):
        self.service()
        self.app("import urllib.request,sys\nassert urllib.request.urlopen(sys.argv[1]+'/health').status==200\n")
        self.change("mock-check", lambda r: r["command"].append("{model.outputs.endpoint}"))
        self.build()
        result = local.run(self.bundle, self.root / "run", 20)
        self.assertEqual(result["status"], "application_completed")
        self.assert_closed(result)

    def test_service_loss_cancels_experiment(self):
        self.service(exit_after=True)
        self.app("import time\ntime.sleep(60)\n")
        self.build()
        result = local.run(self.bundle, self.root / "run", 20)
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertIn("service exited", local.read(self.root / "run/run.json")["error"])
        self.assert_closed(result)

    def test_missing_credential_rejected_without_starting_os(self):
        self.change("mock-check", lambda r: r.update(env=["METAOS_TEST_ABSENT_KEY"]))
        self.build()
        os.environ.pop("METAOS_TEST_ABSENT_KEY", None)
        with self.assertRaisesRegex(ValueError, "environment variable is absent"):
            local.run(self.bundle, self.root / "run", 20)
        self.assertFalse((self.root / "run").exists())

    def service(self, exit_after=False):
        directory = self.pool / "model"
        directory.mkdir()
        (directory / "server.py").write_text(
            "import http.server,sys,threading,os\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200);self.end_headers();self.wfile.write(b'fixture, no model inference')\n"
            " def log_message(self,*args): pass\n"
            + ("threading.Timer(.7,lambda:os._exit(9)).start()\n" if exit_after else "")
            + "http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()\n")
        python = local.read(self.pool / "mock-check/resource.json")["paths"]["python"]
        local.write(directory / "resource.json", {"api": local.RESOURCE_API, "kind": "service", "provides": ["model.chat/v1"],
            "paths": {"python": python, "script": {"path": "server.py", "mode": "file"}},
            "command": ["{self.paths.python}", "-I", "-B", "{self.paths.script}", "{self.port}"],
            "health_url": "http://127.0.0.1:{self.port}/health", "endpoint": "http://127.0.0.1:{self.port}",
            "outputs": {"endpoint": "http://127.0.0.1:{self.port}"}})
        self.change("mock-check", lambda r: r["requires"].update(model="model.chat/v1"))
        spec = local.read(self.spec)
        spec["components"]["model"] = {"resource": "model"}
        spec["components"]["harness"]["bindings"]["model"] = "model"
        local.write(self.spec, spec)

    def test_existing_offline_installer_and_preexecution_validation(self):
        python = Path(local.read(self.pool / "mock/resource.json")["paths"]["python"]["path"])
        source = self.root / "deployment"
        result = subprocess.run([str(python), "-I", "-B", "-m", "metaos_runtime", "bundle", "export",
            "--spec", str(ROOT / "configs/managed-mock.yaml"), "--root", str(ROOT),
            "--wheelhouse", str(ROOT / "work/p1p2-final-03/wheels"), "--output", str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / "installed"
        with self.assertRaisesRegex(ValueError, "expected fingerprint"):
            local.install(source, target, python, expected="0" * 64)
        self.assertFalse(target.exists())
        outcome = local.install(source, target, python)
        self.assertEqual(outcome["status"], "installed")
        plugins = subprocess.run([outcome["python"], "-I", "-B", "-m", "metaos_runtime", "plugins"], capture_output=True, text=True)
        self.assertEqual(plugins.returncode, 0, plugins.stderr)
        self.assertIn("embodiment.mock_arm", plugins.stdout)
        (source / "install.py").write_text("raise AssertionError('must not execute changed installer')\n")
        with self.assertRaisesRegex(ValueError, "content differs"):
            local.install(source, self.root / "another-target", python)
        self.assertFalse((self.root / "another-target").exists())


if __name__ == "__main__":
    unittest.main()
