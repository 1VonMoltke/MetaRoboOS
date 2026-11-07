#!/usr/bin/env python3
"""Exercise the installed plugin, original workflow, and portable deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from roborsi.assembly.resolver import core_identity


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    demo, work = args.demo.resolve(), args.work.resolve()
    commands, checks = [], []
    python = Path(sys.executable)

    def check(name, **details):
        checks.append({"name": name, "status": "passed", **details})
        print(f"PASS {name}", flush=True)

    def command(name, arguments, *, executable=python, expected_failure=None):
        argv = [str(executable), "-I", "-B", *[str(x) for x in arguments]]
        result = subprocess.run(argv, cwd=work, text=True, capture_output=True, timeout=180)
        stdout, stderr = work / "logs" / f"{name}.stdout", work / "logs" / f"{name}.stderr"
        stdout.write_text(result.stdout)
        stderr.write_text(result.stderr)
        commands.append({"name": name, "argv": argv, "exit_code": result.returncode,
                         "stdout": str(stdout), "stderr": str(stderr)})
        if expected_failure is None:
            require(result.returncode == 0, f"{name} failed; see {stderr}")
        else:
            require(result.returncode == 2, f"{name}: expected validation exit 2, got {result.returncode}")
            require(expected_failure in result.stderr + result.stdout,
                    f"{name}: failed for an unexpected reason; see {stderr}")
        return result.stdout

    def project(name):
        root = work / f"project-{name}"
        (root / "assets").mkdir(parents=True)
        shutil.copyfile(demo / "assets/counter-memory.txt", root / "assets/counter-memory.txt")
        shutil.copyfile(demo / f"configs/assemblies/{name}.yaml", root / "assembly.yaml")
        shutil.copyfile(demo / "configs/runs/local.yaml", root / "run.yaml")
        return root

    def assembly(root, *, dry=False, lock=None):
        arguments = ["-m", "roborsi.assembly", "--assembly", root / "assembly.yaml",
                     "--run-spec", root / "run.yaml", "--root", root]
        if dry:
            arguments.append("--dry-run")
        if lock:
            arguments.extend(["--lock" if dry else "--expect-lock", lock])
        return arguments

    cleanup_records = []

    def inspect_run(report, root, expected_steps):
        metric = report["benchmarks"]["manual_counter_demo"]
        require(metric["planned"] == metric["scored"] == metric["successes"] == 2,
                "Expected exactly two successful scored cases")
        require(metric["aggregate_score"] == 1.0, "Expected aggregate score 1.0")
        require({c["result"]["task"]["task_id"] for c in report["cases"]} == {"count-4", "count-5"},
                "Unexpected task selection")
        require(all(c["result"]["status"] == "success" for c in report["cases"]), "Case failed")
        scores = [json.loads(p.read_text()) for p in root.rglob("native/score.json")]
        require(len(scores) == 2, "Missing or duplicate native score evidence")
        observed = {record["goal"]: record["physical_steps"] for record in scores}
        require(observed == expected_steps, f"Unexpected native step counts: {observed}")
        ledgers = list(root.rglob("ledger.json"))
        require(bool(ledgers), "Missing lease ledger evidence")
        require(all(not json.loads(p.read_text())["leases"] for p in ledgers), "Leases remain allocated")
        deployments = list(root.rglob("deployments.json"))
        require(bool(deployments), "Missing runtime deployment evidence")
        records = [r for p in deployments for r in json.loads(p.read_text()) if r["owned"]]
        require(bool(records) and all(r.get("cleaned") for r in records), "Owned runtime was not cleaned")
        for pid in {r["pid"] for r in records if r.get("pid")}:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            raise RuntimeError(f"Owned runtime PID is still alive: {pid}")
        cleanup_records.append({"root": str(root), "empty_ledgers": [str(p) for p in ledgers],
                                "cleaned_pids": sorted({r["pid"] for r in records if r.get("pid")})})
        return {"aggregate_score": metric["aggregate_score"], "native_scores": scores,
                "physical_steps": sum(observed.values())}

    source = json.loads((demo / "vendor/source.json").read_text())
    for filename, expected in source["wheels"].items():
        require(digest(demo / "vendor/wheels" / filename) == expected, f"Vendor wheel changed: {filename}")
    core_before = core_identity()
    entries = json.loads(command("components", ["-m", "roborsi.components", "list"]))
    require(any(r["id"] == "model.manual_counter" and r["origin"] == "roborsi-manual-counter"
                for r in entries), "Manual model is not discovered from its own distribution")
    command("describe-model", ["-m", "roborsi.components", "describe", "model.manual_counter"])
    check("external_entry_point", distribution="roborsi-manual-counter", component="model.manual_counter")

    unit_log = (work / "logs/sdk-only-tests.log").read_text()
    require("Ran 7 tests" in unit_log and "\nOK\n" in unit_log, "SDK-only unit tests did not pass")
    check("sdk_only_plugin", unit_tests=7, core_installed=False)

    roots = {name: project(name) for name in ("baseline", "manual")}
    specs = {name: yaml.safe_load((root / "assembly.yaml").read_text()) for name, root in roots.items()}
    require({k: v for k, v in specs["baseline"].items() if k != "components"} ==
            {k: v for k, v in specs["manual"].items() if k != "components"}, "Entrypoints changed")
    for alias in ("environment", "agent", "memory"):
        require(specs["baseline"]["components"][alias] == specs["manual"]["components"][alias],
                f"Unexpected non-Model change: {alias}")
    plans, runs = {}, {}
    for name, root in roots.items():
        lock = work / f"{name}.lock.json"
        plans[name] = json.loads(command(f"plan-{name}", assembly(root, dry=True, lock=lock)))
        require(not (root / "runtime").exists(), "Planning allocated a runtime")
        report = json.loads(command(f"run-{name}", assembly(root, lock=lock)))
        runs[name] = inspect_run(report, root, {4: 4, 5: 5} if name == "baseline" else {4: 2, 5: 3})
    require(plans["baseline"]["fingerprint"] != plans["manual"]["fingerprint"], "Model change did not change lock")
    require(plans["baseline"]["lock"]["core"] == plans["manual"]["lock"]["core"], "Core changed")
    for alias in ("environment", "agent", "memory"):
        require(plans["baseline"]["lock"]["components"][alias] ==
                plans["manual"]["lock"]["components"][alias], f"Non-Model lock changed: {alias}")
    check("same_workflow_model_replacement", baseline=runs["baseline"], manual=runs["manual"],
          unchanged_components=["environment", "agent", "memory"], core_unchanged=True)

    negative = work / "negative"
    negative.mkdir()
    for name, mutate, expected, use_lock in (
        ("invalid-config", lambda s: s["components"]["model"]["config"].update(increment=3), "Expected only optional", False),
        ("incompatible-version", lambda s: s["components"]["model"].update(version=">=2,<3"), "No compatible version", False),
        ("lock-drift", lambda s: s["components"]["model"]["config"].update(increment=1), "differs from --expect-lock", True),
    ):
        spec = json.loads(json.dumps(specs["manual"]))
        mutate(spec)
        path = negative / f"{name}.json"
        path.write_text(json.dumps(spec, indent=2))
        arguments = ["-m", "roborsi.assembly", "--assembly", path, "--root", roots["manual"], "--dry-run"]
        if use_lock:
            arguments += ["--expect-lock", work / "manual.lock.json"]
        command(name, arguments, expected_failure=expected)
        check(name)

    exported = work / "export/manual-deployment"
    exported.parent.mkdir()
    exported_identity = json.loads(command("export", ["-m", "roborsi.deployment", "export",
        "--assembly", roots["manual"] / "assembly.yaml", "--run-spec", roots["manual"] / "run.yaml",
        "--root", roots["manual"], "--wheelhouse", work / "wheels", "--output", exported]))
    relocated = work / "relocated/manual-deployment"
    relocated.parent.mkdir()
    shutil.move(exported, relocated)
    # Only delete the disposable staged input project, never the user's source.
    shutil.rmtree(roots["manual"])
    cold = work / "cold"
    command("cold-install", [relocated / "install.py", "--target", cold,
                             "--expect-fingerprint", exported_identity["fingerprint"]],
            executable=Path(sys._base_executable))
    cold_python = cold / "bin/python"
    command("cold-verify", ["-m", "roborsi.deployment", "verify", "--bundle", relocated,
                            "--installed", "--expect-fingerprint", exported_identity["fingerprint"]],
            executable=cold_python)
    inspection = json.loads(command("cold-imports", ["-c", """
import json, sys
from pathlib import Path
import roborsi, roborsi.contracts, roborsi_manual_counter, roborsi_example_component
paths = {m.__name__: str(Path(m.__file__).resolve()) for m in
         [roborsi, roborsi.contracts, roborsi_manual_counter, roborsi_example_component]}
if not all(Path(p).is_relative_to(Path(sys.prefix).resolve()) for p in paths.values()):
    raise RuntimeError('Module imported outside the cold environment')
print(json.dumps(paths))
"""], executable=cold_python))
    cold_workspace = work / "cold-run"
    cold_output = json.loads(command("cold-run", ["-m", "roborsi.deployment", "run",
        "--bundle", relocated, "--workspace", cold_workspace,
        "--expect-fingerprint", exported_identity["fingerprint"]], executable=cold_python))
    require(cold_output["deployment"] == exported_identity["fingerprint"], "Deployment identity drifted")
    cold_result = inspect_run(cold_output["report"], cold_workspace, {4: 2, 5: 3})
    require(not roots["manual"].exists() and not exported.exists(), "Original inputs were not removed/relocated")
    require(json.loads((cold_workspace / "assembly.bundle.json").read_text())["lock"] == plans["manual"]["lock"],
            "Cold reconstruction changed assembly lock")
    check("offline_relocated_cold_run", deployment=exported_identity["fingerprint"],
          assembly=plans["manual"]["fingerprint"], original_project_removed=True,
          imported_modules=inspection, result=cold_result)

    corrupt = work / "negative/corrupt-deployment"
    shutil.copytree(relocated, corrupt)
    memory = corrupt / "project/assets/counter-memory.txt"
    memory.write_text(memory.read_text() + "\nmanual tamper\n")
    command("tamper", ["-m", "roborsi.deployment", "verify", "--bundle", corrupt],
            expected_failure="deployment content differs")
    check("tamper_rejected")

    command("uninstall-model", ["-m", "pip", "uninstall", "-y", "roborsi-manual-counter"])
    try:
        remaining = json.loads(command("components-after-removal", ["-m", "roborsi.components", "list"]))
        require(all(r["id"] != "model.manual_counter" for r in remaining), "Removed model is still discoverable")
        require(any(r["id"] == "agent.example_counter" for r in remaining), "Unrelated example plugin disappeared")
        command("missing-model", ["-m", "roborsi.assembly", "--assembly", demo / "configs/assemblies/manual.yaml",
                "--root", demo, "--dry-run"], expected_failure="Component is not registered: model.manual_counter")
        require(core_before == core_identity(), "Core bytes changed during plugin removal")
    finally:
        command("reinstall-model", ["-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir",
                "--no-deps", "--find-links", work / "wheels", "roborsi-manual-counter==1.0.0"])
    command("verify-restored-install", ["-m", "roborsi.deployment", "verify", "--bundle", relocated, "--installed"])
    require(core_before == core_identity(), "Core bytes changed during acceptance")
    check("plugin_removal_and_restore", core_unchanged=True, unrelated_plugins_preserved=True)
    check("owned_runtime_cleanup", records=cleanup_records)

    summary = {
        "status": "passed", "created_at": datetime.now(timezone.utc).isoformat(),
        "demo": str(demo), "work": str(work), "component": "model.manual_counter",
        "distribution": "roborsi-manual-counter", "version": "1.0.0",
        "checks": checks, "commands": commands,
        "artifacts": {"plugin_wheel": str(next((work / "wheels").glob("roborsi_manual_counter-*.whl"))),
                      "producer_python": str(python), "cold_python": str(cold_python),
                      "deployment": str(relocated), "cold_workspace": str(cold_workspace)},
        "source_deployment": source["source_deployment_fingerprint"],
        "limits": ["Deterministic counting model, not an LLM or embodied policy.",
                   "No GPU, LIBERO physics, or real 27B benchmark is exercised.",
                   "Frozen dependency wheels target CPython 3.11 / Linux x86_64; upstream snapshot provenance is retained."],
    }
    (work / "acceptance-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"status": "passed", "checks": len(checks),
                      "baseline_steps": runs["baseline"]["physical_steps"],
                      "manual_steps": runs["manual"]["physical_steps"],
                      "cold_score": cold_result["aggregate_score"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
