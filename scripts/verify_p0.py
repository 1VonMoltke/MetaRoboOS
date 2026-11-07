#!/usr/bin/env python3
"""Verify real deployment movement, cold execution and failure boundaries."""

import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from metaos_runtime.composition import distribution_identity
from metaos_runtime.server import create_credentials


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    args = parser.parse_args()
    project, work = args.project, args.work
    logs, negative = work / "logs", work / "negative"
    negative.mkdir()
    summary = {"status": "in_progress", "created_at": datetime.now(timezone.utc).isoformat(),
        "project": str(project), "work": str(work), "checks": [], "commands": [], "artifacts": {}}

    def save():
        (work / "acceptance-summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    def passed(name, **evidence):
        summary["checks"].append({"name": name, "status": "passed", **evidence})
        save()
        print("PASS " + name, flush=True)

    def run(name, arguments, expected=0, code=None, timeout=180):
        stdout, stderr = logs / (name + ".stdout"), logs / (name + ".stderr")
        with stdout.open("w") as out, stderr.open("w") as err:
            result = subprocess.run([str(x) for x in arguments], cwd=work, stdout=out, stderr=err, timeout=timeout)
        summary["commands"].append({"name": name, "argv": [str(x) for x in arguments], "exit_code": result.returncode,
            "stdout": str(stdout), "stderr": str(stderr)})
        save()
        require(result.returncode == expected, f"{name} returned {result.returncode}; inspect {stderr}")
        if code:
            require(json.loads(stderr.read_text())["error"]["code"] == code, name + " did not reject the intended fault")
        return stdout.read_text()

    python = Path(sys.executable)
    cli = [python, "-I", "-B", "-m", "metaos_runtime"]
    for name in ("roborsi.evaluation", "roborsi.assembly", "roborsi.components", "torch"):
        require(importlib.util.find_spec(name) is None, "Unexpected baseline dependency: " + name)
    paths = {}
    boundaries = {"metaos_contracts": {"metaos_runtime", "metaos_mock_arm"},
                  "metaos_mock_arm": {"metaos_runtime"}, "metaos_runtime": {"metaos_mock_arm"}}
    for name in ("roborsi.contracts", *boundaries):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        require(path.is_relative_to(Path(sys.prefix).resolve()), "Source checkout import: " + name)
        paths[name] = str(path)
        if name not in boundaries:
            continue
        for source in path.parent.glob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for imported in imports:
                    require(imported.split(".")[0] not in boundaries[name], "Invalid package dependency: " + imported)
                    require(not imported.startswith("roborsi.") or imported.startswith("roborsi.contracts"), "Core import: " + imported)
    passed("installed_package_boundaries", paths=paths)
    for name in ("sdk-only", "producer"):
        report = json.loads((work / (name + "-tests.json")).read_text())
        require(report["status"] == "passed" and report["count"] == len(report["tests"]), "Incomplete test outcomes")
        passed(name + "_tests", count=report["count"], report=str(work / (name + "-tests.json")))

    source = work / "temporary-system-input"
    source.mkdir()
    shutil.copyfile(project / "configs/p0-monitor.yaml", source / "system.yaml")
    planned = json.loads(run("plan", [*cli, "plan", "--spec", source / "system.yaml", "--root", source, "--lock", work / "system.lock.json"]))
    require(planned["order"] == ["arm", "monitor"] and not (source / ".system-plan").exists(), "Plan mutated workspace or graph order differs")
    passed("generic_two_node_plan", fingerprint=planned["fingerprint"], order=planned["order"])
    creds = work / "credentials.json"
    create_credentials(creds)
    drift = json.loads(json.dumps(planned["lock"]["spec"]))
    drift["components"]["arm"]["config"]["resource_id"] = "robots/other-arm"
    drift_path = negative / "changed-system.json"
    drift_path.write_text(json.dumps(drift))
    run("changed-lock", [*cli, "serve", "--spec", drift_path, "--root", source, "--lock", work / "system.lock.json",
        "--workspace", negative / "must-not-open", "--credentials", creds, "--ready-file", negative / "never-ready.json"], 2, "lock_changed")
    require(not (negative / "must-not-open").exists(), "Lock failure created runtime state")
    passed("lock_drift_rejected_before_open")

    exported = work / "export/p0-deployment"
    doc = json.loads(run("export", [*cli, "bundle", "export", "--spec", source / "system.yaml", "--root", source,
        "--wheelhouse", work / "wheels", "--output", exported]))
    bundle = work / "relocated/p0-deployment"
    bundle.parent.mkdir()
    shutil.move(exported, bundle)
    shutil.rmtree(source)  # Only the per-run temporary copy, never user's source.
    identity = doc["fingerprint"]
    cold = work / "cold"
    run("cold-install", [args.launcher, "-I", "-B", bundle / "install.py", "--target", cold, "--expect-fingerprint", identity])
    cold_cli = [cold / "bin/python", "-I", "-B", "-m", "metaos_runtime"]
    run("cold-verify", [*cold_cli, "bundle", "verify", "--bundle", bundle, "--installed", "--expect-fingerprint", identity])
    imports = json.loads(run("cold-imports", [cold / "bin/python", "-I", "-B", "-c", '''
import importlib, importlib.util, json, sys
from pathlib import Path
paths={n:str(Path(importlib.import_module(n).__file__).resolve()) for n in ["metaos_runtime","metaos_contracts","metaos_mock_arm","roborsi.contracts"]}
assert all(Path(p).is_relative_to(Path(sys.prefix).resolve()) for p in paths.values())
assert all(importlib.util.find_spec(n) is None for n in ["roborsi.evaluation","roborsi.assembly","torch"])
print(json.dumps(paths))
''']))
    passed("offline_relocated_cold_install", original_input_removed=not source.exists(), fingerprint=identity, imports=imports,
        packages=sorted(doc["packages"]))
    forbidden = {"roborsi", "roborsi-example-component", "torch", "numpy"}
    require(not forbidden.intersection(doc["packages"]), "Unrelated heavy/core packages entered deployment")

    ready = work / "cold-ready.json"
    gateway_log = (logs / "cold-gateway.log").open("w")
    process = subprocess.Popen([str(x) for x in [*cold_cli, "serve", "--spec", bundle / "system.json", "--root", bundle / "project",
        "--lock", bundle / "system.lock.json", "--workspace", work / "cold-runtime", "--credentials", creds, "--ready-file", ready]],
        cwd=work, stdout=gateway_log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 12
        while True:
            require(process.poll() is None, "Cold gateway exited before ready")
            try:
                started = json.loads(ready.read_text())
                break
            except (OSError, ValueError):
                require(time.monotonic() < deadline, "Cold gateway did not become ready")
                time.sleep(0.01)
        require(started["fingerprint"] == planned["fingerprint"], "Relocated graph identity differs")
        run("cold-actions", [cold / "bin/python", "-I", "-B", project / "scripts/exercise_p0.py", "--ready-file", ready,
            "--credentials", creds, "--mode", "all", "--output", work / "cold-actions.json"])
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(3)
            raise RuntimeError("Cold gateway required forced termination")
        finally:
            gateway_log.close()
            (work / "cold-process.json").write_text(json.dumps({"pid": process.pid, "exit_code": process.returncode}) + "\n")
    closed = json.loads((work / "cold-runtime/shutdown.json").read_text())
    require(process.returncode == 0 and not ready.exists() and closed["handles_closed"] and closed["active_leases"] == 0, "Incomplete cold cleanup")
    actions = json.loads((work / "cold-actions.json").read_text())
    require(actions["status"] == "passed" and len(actions["cases"]) == 3, "Cold action scenarios incomplete")
    passed("cold_actions_and_cleanup", scenarios=[c["case"] for c in actions["cases"]], report=str(work / "cold-actions.json"), shutdown=closed)

    for fault in ("tampered-content", "missing-wheel", "unknown-schema"):
        altered = negative / fault
        shutil.copytree(bundle, altered)
        if fault == "tampered-content":
            with (altered / "system.json").open("a") as stream:
                stream.write(" ")
        elif fault == "missing-wheel":
            next((altered / "wheels").glob("*.whl")).unlink()
        else:
            manifest = json.loads((altered / "deployment.json").read_text())
            manifest["schema_version"] = 999
            (altered / "deployment.json").write_text(json.dumps(manifest))
        run(fault, [*cli, "bundle", "verify", "--bundle", altered], 2,
            "unsupported_schema" if fault == "unknown-schema" else "artifact_changed")
        target = negative / (fault + "-install")
        run(fault + "-install", [args.launcher, "-I", "-B", altered / "install.py", "--target", target], 1)
        require(not target.exists(), "Invalid artifact created a cold environment")
        passed(fault + "_rejected")
    run("wrong-fingerprint", [*cli, "bundle", "verify", "--bundle", bundle, "--expect-fingerprint", "0" * 64], 2, "artifact_changed")
    run("existing-install-target", [args.launcher, "-I", "-B", bundle / "install.py", "--target", cold], 1)
    passed("identity_and_existing_target_rejected")

    installed = Path(importlib.import_module("metaos_mock_arm").__file__)
    original = installed.read_bytes()
    try:
        installed.write_bytes(original + b"\n# acceptance tamper\n")
        run("installed-code-tamper", [*cli, "bundle", "verify", "--bundle", bundle, "--installed"], 2, "installed_dependency_changed")
    finally:
        installed.write_bytes(original)
    run("restored-code", [*cli, "bundle", "verify", "--bundle", bundle, "--installed"])
    passed("installed_code_tamper_rejected_and_restored")

    before = {name: distribution_identity(name) for name in ("metaos-runtime", "metaos-contracts", "roborsi-sdk")}
    run("uninstall-device", [python, "-I", "-B", "-m", "pip", "uninstall", "-y", "metaos-mock-arm"])
    listed = json.loads(run("plugins-after-uninstall", [*cli, "plugins"]))
    require(not any(row["id"] in {"embodiment.mock_arm", "tool.mock_arm_monitor"} for row in listed), "Plugin remained discoverable")
    run("missing-plugin", [*cli, "plan", "--spec", bundle / "system.json", "--root", bundle / "project"], 2, "component_not_found")
    run("reinstall-device", [python, "-I", "-B", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir", "--no-deps",
        "--find-links", work / "wheels", "metaos-mock-arm==0.1.0"])
    run("restored-plugin", [*cli, "bundle", "verify", "--bundle", bundle, "--installed"])
    require(before == {name: distribution_identity(name) for name in before}, "Plugin removal changed unrelated packages")
    passed("plugin_removal_and_restore", unrelated_packages_unchanged=True)

    processes = []
    for path in sorted((work / "test-evidence").glob("*/process.json")):
        record = json.loads(path.read_text())
        require(record["exit_code"] == 0, "Test gateway failed cleanup")
        shutdown = json.loads((Path(record["workspace"]) / "shutdown.json").read_text())
        require(shutdown["handles_closed"] and shutdown["active_leases"] == 0 and not (path.parent / "ready.json").exists(), "Incomplete test cleanup")
        try:
            os.kill(record["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            raise RuntimeError("Owned gateway process is still live")
        processes.append({"pid": record["pid"], "exit_code": record["exit_code"], "evidence": str(path)})
    require(len(processes) >= 17, "Missing gateway process evidence")
    passed("owned_process_cleanup", processes=processes)
    summary["artifacts"] = {"producer_python": str(python), "cold_python": str(cold / "bin/python"), "deployment": str(bundle),
        "credentials": str(creds), "cold_runtime": str(work / "cold-runtime"), "test_evidence": str(work / "test-evidence")}
    summary["status"] = "p0_passed_pending_demo_regression"
    save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
