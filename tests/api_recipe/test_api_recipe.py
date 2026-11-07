"""Verify API recipe bindings and the actual installed Agent configuration."""

import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).absolute().parents[2]
recipe = runpy.run_path(str(ROOT / "scripts/prepare_libero_api.py"))
local = runpy.run_path(str(ROOT / "scripts/metaos_local.py"))


class ApiRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pool = self.root / "resources"

    def test_preparation_and_original_provider_configuration(self):
        source = local["read"](ROOT / "evidence/p1p2-latest.json")["artifacts"]["system_deployment"]
        original = local["sha"](Path(source) / "system.json")
        recipe["prepare"](self.pool, ROOT / "evidence/p1p2-latest.json", "api-fixture-no-inference",
            "http://127.0.0.1:9/v1", "MY_MODEL_KEY", 2, 128000, 8192, 7200)
        self.assertEqual(original, local["sha"](Path(source) / "system.json"))
        app = local["read"](self.pool / "libero-harness/resource.json")
        self.assertEqual(app["env"], ["MY_MODEL_KEY"])
        overrides = [x.split("=", 1)[1] for x in app["command"] if x.startswith("--agent-set=")]
        program = r'''
import json,sys
from metaos_libero_harness.agent_service import model_overrides
from metaos_runtime.composition import Registry
from roborsi.contracts import ComponentContext,ComponentSpec
from pathlib import Path
settings=json.loads(sys.argv[1])
spec=ComponentSpec(component='agent.roborsi_27b',config={
    'preset':'sensenova_roborsi_27b_no_tools','tokenizer_file':sys.argv[2],
    'overrides':model_overrides('api-fixture-no-inference','http://127.0.0.1:9/v1',settings)})
plan=Registry().provider(spec.component).plan(spec,ComponentContext(Path(sys.argv[3]),root=Path(sys.argv[3])))
config=plan.metadata['agent_configuration']
assert config['runtime']['llm']['extra_body'] is None
assert config['runtime']['llm']['model']=='api-fixture-no-inference'
assert config['runtime']['llm']['openai_format']=='chat'
assert config['runtime']['llm']['parallel_tool_calls'] is False
assert config['agent']['max_tokens']==8192
assert config['agent']['reasoning_effort']=='none'
assert config['agent']['planner_timeout_s']==7200
assert config['runtime']['context']['context_window']==128000
assert not plan.metadata['missing_assets']
print(json.dumps({'status':'passed','model_contacted':False}))
'''
        evidence = local["read"](ROOT / "evidence/p1p2-latest.json")["artifacts"]
        result = subprocess.run([app["paths"]["python"]["path"], "-I", "-B", "-c", program,
            json.dumps(overrides), str(Path(evidence["harness_deployment"]) / "assets/tokenizer.json"), str(self.root)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["model_contacted"])
        bundle = local["compose"](self.pool / "libero.json", self.pool)
        local["validate_templates"](bundle)
        self.assertEqual(bundle["order"][-1], "harness")

    def test_credential_url_and_invalid_budgets_rejected_before_creation(self):
        for url, limit in [("https://user:key@example.invalid/v1", 128000),
                ("https://example.invalid/v1?api_key=key", 128000),
                ("https://example.invalid/v1", 8192)]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    recipe["prepare"](self.pool, ROOT / "evidence/p1p2-latest.json", "model", url, context_window=limit)
                self.assertFalse(self.pool.exists())


if __name__ == "__main__":
    unittest.main()
