#!/usr/bin/env python3
"""Freeze/relocate the separate Harness and real ICL, then test without inference."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline-bundle", "metaos-wheels", "packages", "tokenizer", "task", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--symlinks", action="store_true", help="Qualify a standard venv using interpreter symlinks")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in ("tmp", "logs", "temporary-inputs"):
        (output / name).mkdir()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update(TMPDIR=str(output / "tmp"), PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1")
    report = {"status": "running", "goal_complete": False, "model_inference": False, "commands": []}
    def run(name, argv, timeout=600):
        path = output / "logs" / (name + ".log")
        with path.open("w") as stream:
            result = subprocess.run([str(x) for x in argv], cwd=output, env=env,
                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        report["commands"].append({"name": name, "argv": [str(x) for x in argv], "exit_code": result.returncode, "log": str(path)})
        if result.returncode:
            raise RuntimeError(name + " failed; inspect " + str(path))
        print("PASS " + name, flush=True)
    try:
        app_wheels = output / "application-wheels"
        app_wheels.mkdir()
        package = project / "plugins/libero-harness"
        run("build-harness", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "wheel", "--no-index", "--no-deps",
            "--no-build-isolation", "--no-cache-dir", "--wheel-dir", app_wheels, package])
        meta = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        wheel = next(app_wheels.glob("*.whl"))
        app_source = {str(p.relative_to(package)): sha(p) for p in sorted(package.rglob("*")) if p.is_file()
            and (p == package / "pyproject.toml" or p.suffix == ".py" and "src" in p.relative_to(package).parts)}
        app_provenance = {"name": meta["name"], "version": meta["version"], "wheel": wheel.name, "sha256": sha(wheel),
            "provenance": "local-source-wheel", "source_files": app_source}
        staging = output / "temporary-inputs/harness"
        (staging / "wheels").mkdir(parents=True)
        baseline = args.baseline_bundle.resolve()
        source = json.loads((baseline / "deployment.json").read_text())
        for path in sorted((baseline / "wheels").glob("*.whl")):
            if sha(path) != source["files"].get("wheels/" + path.name):
                raise ValueError("Baseline wheel identity differs: " + path.name)
            shutil.copyfile(path, staging / "wheels" / path.name)
        local = [row for row in json.loads((args.metaos_wheels / "wheel-provenance.json").read_text())
            if row["name"] in {"metaos-contracts", "metaos-runtime"}] + [app_provenance]
        for row in local:
            if row["name"] not in {"metaos-contracts", "metaos-runtime", "metaos-libero-harness"}:
                continue
            path = (app_wheels if row["name"] == meta["name"] else args.metaos_wheels) / row["wheel"]
            if sha(path) != row["sha256"]:
                raise ValueError("MetaOS wheel identity differs")
            shutil.copyfile(path, staging / "wheels" / path.name)
        resolution = output / "pip-resolution.json"
        run("resolve-offline", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
            "--no-index", "--no-cache-dir", "--find-links", staging / "wheels", "--report", resolution,
            "metaos-libero-harness[original-agent]==" + meta["version"]])
        packages = {}
        for row in json.loads(resolution.read_text())["install"]:
            packages[row["metadata"]["name"]] = {"version": row["metadata"]["version"],
                "sha256": row["download_info"]["archive_info"]["hashes"]["sha256"]}
        selected = {row["sha256"] for row in packages.values()}
        for path in (staging / "wheels").glob("*.whl"):
            if sha(path) not in selected:
                path.unlink()
        (staging / "requirements.lock").write_text("".join(f"{name}=={row['version']} --hash=sha256:{row['sha256']}\n"
            for name, row in sorted(packages.items())))
        (staging / "assets").mkdir()
        shutil.copytree(args.packages, staging / "assets/packages")
        shutil.copyfile(args.tokenizer, staging / "assets/tokenizer.json")
        shutil.copyfile(args.task, staging / "task.json")
        shutil.copyfile(project / "scripts/install_harness.py", staging / "install.py")
        (staging / "scripts").mkdir()
        for name in ("run_p0_tests.py", "qualify_agent_service.py", "qualify_27b_inputs.py", "exercise_libero_harness.py", "verify_harness_delivery.py", "exercise_harness_failure.py"):
            shutil.copyfile(project / "scripts" / name, staging / "scripts" / name)
        shutil.copytree(project / "tests/harness", staging / "tests")
        (staging / "wheel-provenance.json").write_text(json.dumps({"baseline_fingerprint": source["fingerprint"],
            "metaos": [row for row in local if row["name"] in {"metaos-contracts", "metaos-runtime", "metaos-libero-harness"}]}, indent=2))
        doc = {"api": "metaos.harness/v1", "schema_version": 1, "packages": packages,
            "python": {"implementation": platform.python_implementation(), "version": ".".join(platform.python_version_tuple()[:2]),
                "system": platform.system(), "machine": platform.machine()},
            "model_inference": "requires_external_available_service", "credentials": "external_environment_only",
            "files": {p.relative_to(staging).as_posix(): sha(p) for p in sorted(staging.rglob("*")) if p.is_file()}}
        doc["fingerprint"] = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        (staging / "harness.json").write_text(json.dumps(doc, indent=2) + "\n")
        relocated = output / "relocated/harness"
        relocated.parent.mkdir()
        shutil.move(staging, relocated)
        shutil.rmtree(output / "temporary-inputs")
        cold = output / "cold"
        run("cold-install", [sys.executable, "-I", "-B", relocated / "install.py", "--target", cold,
            "--expect-fingerprint", doc["fingerprint"], *(["--symlinks"] if args.symlinks else [])])
        python = cold / "bin/python"
        run("module-cli", [python, "-I", "-B", "-m", "metaos_libero_harness", "--help"])
        run("harness-contracts", [python, "-I", "-B", relocated / "scripts/run_p0_tests.py", "--tests", relocated / "tests",
            "--output", output / "harness-tests.json"])
        for name, script in (("original-inputs", "qualify_27b_inputs.py"), ("original-service", "qualify_agent_service.py")):
            run(name, [python, "-I", "-B", relocated / "scripts" / script, "--packages", relocated / "assets/packages",
                "--tokenizer", relocated / "assets/tokenizer.json", "--task", relocated / "task.json", "--output", output / name])
        run("delivery-negative-tests", [sys.executable, "-I", "-B", relocated / "scripts/verify_harness_delivery.py",
            "--bundle", relocated, "--output", output / "delivery-negative"])
        shutil.copyfile(output / "delivery-negative/report.json", output / "delivery-negative-tests.json")
        report.update(status="contracts_inputs_and_relocated_install_passed", deployment=str(relocated),
            fingerprint=doc["fingerprint"], cold_python=str(python), original_temporary_inputs_removed=True,
            harness_version=meta["version"], interpreter_symlinks=args.symlinks)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        (output / "acceptance-summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "goal_complete": False, "summary": str(output / "acceptance-summary.json")}))


if __name__ == "__main__":
    main()
