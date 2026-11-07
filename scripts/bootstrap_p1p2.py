#!/usr/bin/env python3
"""Rebuild P1/P2 infrastructure from wheels and retained source identities.

This does not claim real 27B inference; that needs an available model service
and its separate CaseRunner evidence.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import venv
from pathlib import Path


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native", action="store_true", help="Archive the native image and run the original full protocol from the relocated cold bundle")
    parser.add_argument("--gpu", type=int, default=7)
    parser.add_argument("--skip-regression", action="store_true", help="Do not run the full P0/demo regression; recorded as unverified")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 11):
        parser.error("The supplied offline wheels target CPython 3.11 / Linux x86_64")
    work = args.output.absolute() if args.output else Path(tempfile.mkdtemp(prefix="p1p2-acceptance-", dir=project / "work"))
    if args.output:
        work.mkdir(parents=True, exist_ok=False)
    for name in ("tmp", "logs"):
        (work / name).mkdir()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update(TMPDIR=str(work / "tmp"), PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1", METAOS_TEST_ROOT=str(work / "test-evidence"))
    report = {"status": "running", "goal_complete": False, "model_inference": "not_run",
        "work": str(work), "commands": [], "native_requested": args.native, "started_at_ns": time.time_ns()}
    def run(name, argv, timeout=900):
        path = work / "logs" / (name + ".log")
        with path.open("w") as stream:
            result = subprocess.run([str(x) for x in argv], env=env, cwd=work, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        report["commands"].append({"name": name, "argv": [str(x) for x in argv], "exit_code": result.returncode, "log": str(path)})
        (work / "acceptance-summary.json").write_text(json.dumps(report, indent=2))
        if result.returncode:
            raise RuntimeError(name + " failed: " + str(path))
        print("PASS " + name, flush=True)
    try:
        vendor = project / "modularize-demo/vendor"
        source = json.loads((vendor / "source.json").read_text())
        if set(source["wheels"]) != {p.name for p in (vendor / "wheels").glob("*.whl")}:
            raise RuntimeError("Vendor wheel inventory differs")
        for name, digest in source["wheels"].items():
            if sha(vendor / "wheels" / name) != digest:
                raise RuntimeError("Vendor source hash differs: " + name)
        wheels = work / "wheels"
        shutil.copytree(vendor / "wheels", wheels)
        shutil.copyfile(vendor / "source.json", work / "vendor-source.json")
        packages = [project / p for p in ["packages/metaos-contracts", "packages/metaos-runtime", "plugins/mock-arm",
            "plugins/joint-degrees", "plugins/libero-device", "plugins/libero-harness"]]
        run("build", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "wheel", "--no-index", "--no-deps",
            "--no-build-isolation", "--no-cache-dir", "--wheel-dir", wheels, *packages])
        provenance = json.loads((wheels / "wheel-provenance.json").read_text())
        for package in packages:
            meta = tomllib.loads((package / "pyproject.toml").read_text())["project"]
            wheel = next(wheels.glob(meta["name"].replace("-", "_") + "-*.whl"))
            files = {str(p.relative_to(package)): sha(p) for p in sorted(package.rglob("*")) if p.is_file()
                and (p == package / "pyproject.toml" or p.suffix == ".py" and "src" in p.relative_to(package).parts)}
            provenance.append({"name": meta["name"], "version": meta["version"], "wheel": wheel.name,
                "sha256": sha(wheel), "provenance": "local-source-wheel", "source_files": files})
        (wheels / "wheel-provenance.json").write_text(json.dumps(provenance, indent=2))
        target = work / "producer"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target)
        python = target / "bin/python"
        run("install", [python, "-I", "-B", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir",
            "--find-links", wheels, "metaos-runtime==0.2.0", "metaos-mock-arm==0.1.0", "metaos-joint-degrees==0.1.0", "metaos-libero-device==0.1.0"])
        run("pip-check", [python, "-I", "-B", "-m", "pip", "check"])
        run("p1p2-tests", [python, "-I", "-B", project / "scripts/run_p0_tests.py", "--tests", project / "tests/p1p2", "--output", work / "p1p2-tests.json"])
        inputs = work / "temporary-inputs"
        inputs.mkdir()
        selected = "managed-libero.yaml" if args.native else "managed-degrees.yaml"
        spec = json.loads(subprocess.check_output([str(python), "-I", "-B", "-c",
            "import json,yaml,sys;print(json.dumps(yaml.safe_load(open(sys.argv[1]))))", str(project / "configs" / selected)], env=env))
        if args.native:
            spec["components"]["simulation"]["config"]["gpu_device"] = args.gpu
            (inputs / "assets/libero").mkdir(parents=True)
            shutil.copyfile(project / "assets/libero/protocol.yaml", inputs / "assets/libero/protocol.yaml")
        (inputs / "system.json").write_text(json.dumps(spec))
        exported = work / "exported"
        run("export", [python, "-I", "-B", "-m", "metaos_runtime", "bundle", "export", "--spec", inputs / "system.json", "--root", inputs,
            "--wheelhouse", wheels, "--output", exported, "--images", "archive" if args.native else "reference"])
        relocated = work / "relocated/deployment"
        relocated.parent.mkdir()
        shutil.move(exported, relocated)
        shutil.rmtree(inputs)
        manifest = json.loads((relocated / "deployment.json").read_text())
        cold = work / "cold"
        run("cold-install", [sys.executable, "-I", "-B", relocated / "install.py", "--target", cold,
            "--expect-fingerprint", manifest["fingerprint"], *(["--load-images"] if args.native else [])])
        report["deployment"] = {"path": str(relocated), "fingerprint": manifest["fingerprint"], "original_input_removed": not inputs.exists(),
            "cold_python": str(cold / "bin/python"), "native_images": manifest.get("native_images", [])}
        if args.native:
            run("cold-native", [cold / "bin/python", "-I", "-B", project / "scripts/exercise_libero.py", "--spec", relocated / "system.json",
                "--root", relocated / "project", "--output", work / "cold-native"], timeout=2400)
        if not args.skip_regression:
            run("p0-and-demo-regression", [sys.executable, "-I", "-B", project / "scripts/bootstrap_p0.py", "--output", work / "p0-regression"], timeout=1800)
        report.update(status="infrastructure_passed", p1p2_completion="pending_real_27b_evidence",
            model_blocker="Model availability and real inference must be qualified separately", finished_at_ns=time.time_ns())
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        (work / "acceptance-summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "goal_complete": False, "summary": str(work / "acceptance-summary.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
