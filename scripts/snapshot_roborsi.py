#!/usr/bin/env python3
"""Freeze the original checkout and add explicit optional dependency seams.

Does not modify the checkout. Each textual seam must match the pinned baseline;
the generated receipt records both original and adapted byte identities.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

BASELINE = "da3e855adf0b4b9a2824139f4e924d31d3141e5b"


def snapshot(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    if revision != BASELINE or dirty:
        raise ValueError("Snapshot requires the clean audited RoboRSI baseline " + BASELINE)
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    shutil.copytree(source / "roborsi", output / "roborsi", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(source / "LICENSE", output / "LICENSE")
    patches = []

    def seam(file, old, new, count=1):
        path = output / "roborsi" / file
        text = path.read_text()
        if text.count(old) != count:
            raise ValueError(f"Baseline seam drift: {file}: expected {count}, got {text.count(old)}")
        path.write_text(text.replace(old, new))
        patches.append({"file": file, "old": old, "new": new, "count": count})

    for role in ["planner", "engineer"]:
        seam(f"agents/roles/{role}.py",
             "def __init__(self, model: str | None = None) -> None:\n        self.model = model or self.DEFAULT_MODEL",
             "def __init__(self, model: str | None = None, services=None) -> None:\n        self.services = services\n        self.model = model or self.DEFAULT_MODEL")
    seam("agents/roles/reviewer.py", "allow_evolution: bool | None = None) -> None:\n        self.model",
         "allow_evolution: bool | None = None, services=None) -> None:\n        self.services = services\n        self.model")
    seam("agents/roles/planner.py", "catalog = _skill_catalog(task, user_msg, ns)",
         "catalog = (self.services['tools'].catalog(task, user_msg, ns) if self.services else _skill_catalog(task, user_msg, ns))")
    for role, task in [("planner", "task"), ("engineer", "workspace.task")]:
        seam(f"agents/roles/{role}.py", f"read_wiki({task})", f"(self.services['context'].wiki({task}) if self.services else read_wiki({task}))", count=1 if role == "engineer" else 1)
    seam("agents/roles/planner.py", "recent_plans = get_recent_plans(task, n=3)",
         "recent_plans = (self.services['context'].recent_plans(task, n=3) if self.services else get_recent_plans(task, n=3))")
    seam("agents/roles/planner.py", "plan_file, plan_origin = persistent_plan_input(task)",
         "plan_file, plan_origin = (self.services['context'].persistent_plan(task) if self.services else persistent_plan_input(task))")
    seam("agents/roles/planner.py", 'content = persistent_agent.run_role(\n            "planner", task, user_block,',
         'content = (self.services[\'model\'].role if self.services else persistent_agent.run_role)(\n            "planner", task, user_block,')
    # Only the atomic Reviewer path is extracted; LH and evolution stay upstream.
    seam("agents/roles/reviewer.py", "content = persistent_agent.run_role(",
         "content = (self.services['model'].role if self.services else persistent_agent.run_role)(", count=1)
    seam("agents/roles/reviewer.py", "content = run_review_agent(self.model,",
         "content = (self.services['model'].review if self.services else run_review_agent)(self.model,")
    seam("agents/roles/reviewer.py", "_task_history_block(workspace.task)",
         "(self.services['context'].task_history(workspace.task) if self.services else _task_history_block(workspace.task))")
    seam("agents/tools/reviewer.py", "def run_review_agent(model: str, system_prompt: str, user_block: str,\n                     workspace_root: Path) -> str:",
         "def run_review_agent(model: str, system_prompt: str, user_block: str,\n                     workspace_root: Path, model_client=None) -> str:")
    seam("agents/tools/reviewer.py", "    first: list[dict[str, Any]] = []",
         "    if model_client is not None:\n        _call_vlm_tools = model_client.call\n    first: list[dict[str, Any]] = []")
    seam("agents/roles/engineer.py", "use_sim_predicate=adjudicate,",
         "use_sim_predicate=adjudicate,\n                services=self.services,")
    seam("embodied/agent_loop/rollout.py", "    prior_messages: list[dict[str, Any]] | None = None,\n) -> RolloutResult:",
         "    prior_messages: list[dict[str, Any]] | None = None,\n    services=None,\n) -> RolloutResult:")
    seam("embodied/agent_loop/rollout.py", "    remote_loop = getattr(env, \"run_tool_loop\", None)",
         "    if services is not None:\n        _call_vlm_tools = services['model'].call\n        _initial_messages = services['context'].initial_messages\n        _summarize_old_trace = services['context'].summarize\n        _build_tool_specs = services['tools'].specs\n        _dispatch_with_timeout = services['tools'].dispatch\n    remote_loop = getattr(env, \"run_tool_loop\", None)")
    # Python local assignments require an explicit default on the untouched path.
    seam("embodied/agent_loop/rollout.py", "    if services is not None:\n        _call_vlm_tools",
         "    from roborsi.embodied.agent_loop.vlm_io import _call_vlm_tools\n    from roborsi.embodied.agent_loop.messages import _initial_messages, _summarize_old_trace\n    from roborsi.embodied.agent_loop.prompt_tools import _build_tool_specs\n    _dispatch_with_timeout = globals()['_dispatch_with_timeout']\n    if services is not None:\n        _call_vlm_tools")
    # Keep artifacts in the run workspace, never write into installed package.
    seam("embodied/agent_loop/rollout.py", "demos_dir = Path(__file__).resolve().parents[3] / \"artifacts\" / artifact_group",
         "demos_dir = Path(os.environ.get('ROBORSI_ARTIFACT_ROOT', str(Path(__file__).resolve().parents[3] / 'artifacts'))) / artifact_group")
    original, adapted = {}, {}
    for path in sorted((output / "roborsi").rglob("*")):
        if path.is_file():
            rel = path.relative_to(output).as_posix()
            original[rel] = hashlib.sha256((source / rel).read_bytes()).hexdigest()
            adapted[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {"api": "metaos.roborsi-source/v1", "revision": revision,
               "original_files": original, "adapted_files": adapted, "seams": patches}
    (output / "roborsi" / "metaos_source.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (output / "pyproject.toml").write_text('''[build-system]
requires = ["setuptools>=67", "wheel"]
build-backend = "setuptools.build_meta"
[project]
name = "metaos-roborsi-baseline"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
description = "Pinned original RoboRSI with explicit optional Harness injection seams"
[tool.setuptools.packages.find]
where = ["."]
include = ["roborsi*"]
[tool.setuptools.package-data]
"*" = ["*.md", "*.json", "*.yaml", "*.yml", "*.txt", "*.toml"]
''')
    return {"revision": revision, "source_files": len(original), "seams": len(patches),
            "receipt_sha256": hashlib.sha256((output / "roborsi/metaos_source.json").read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.output), indent=2))
