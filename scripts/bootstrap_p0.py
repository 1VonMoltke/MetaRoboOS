#!/usr/bin/env python3
"""Build and verify the P0 closure offline in new, independent environments."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new directory; existing directories are rejected")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 11) or platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("Supplied offline wheels target CPython 3.11 / Linux x86_64")
    if args.output:
        work = args.output.absolute()
        work.mkdir(parents=True, exist_ok=False)
    else:
        (project / "work").mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="p0-acceptance-", dir=project / "work"))
    for name in ("tmp", "logs"):
        (work / name).mkdir()
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        environment.pop(key, None)
    environment.update(TMPDIR=str(work / "tmp"), PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull,
        PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1", METAOS_TEST_ROOT=str(work / "test-evidence"))
    commands, started = [], time.monotonic()
    print(f"Output: {work}", flush=True)

    def run(name, arguments, timeout=300):
        path = work / "logs" / (name + ".log")
        with path.open("w") as stream:
            result = subprocess.run([str(x) for x in arguments], cwd=work, env=environment,
                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        commands.append({"name": name, "argv": [str(x) for x in arguments], "exit_code": result.returncode, "log": str(path)})
        (work / "bootstrap-commands.json").write_text(json.dumps(commands, indent=2) + "\n")
        if result.returncode:
            raise RuntimeError(f"{name} failed; see {path}")
        print(f"PASS {name}", flush=True)

    vendor = project / "modularize-demo/vendor"
    source = json.loads((vendor / "source.json").read_text())
    if set(source["wheels"]) != {p.name for p in (vendor / "wheels").glob("*.whl")}:
        raise RuntimeError("Frozen vendor wheel inventory differs")
    for name, digest in source["wheels"].items():
        if sha(vendor / "wheels" / name) != digest:
            raise RuntimeError("Frozen vendor wheel changed: " + name)
    wheels = work / "wheels"
    shutil.copytree(vendor / "wheels", wheels)
    shutil.copyfile(vendor / "source.json", work / "vendor-source.json")
    packages = [project / "packages/metaos-contracts", project / "packages/metaos-runtime", project / "plugins/mock-arm"]
    run("build-packages", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "wheel", "--no-index",
        "--no-deps", "--no-build-isolation", "--no-cache-dir", "--wheel-dir", wheels, *packages])
    provenance = json.loads((wheels / "wheel-provenance.json").read_text())
    for package in packages:
        import tomllib
        metadata = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        wheel = next(wheels.glob(metadata["name"].replace("-", "_") + "-*.whl"))
        files = {str(p.relative_to(package)): sha(p) for p in sorted(package.rglob("*"))
                 if p.is_file() and (p == package / "pyproject.toml" or p.suffix == ".py" and "src" in p.relative_to(package).parts)}
        provenance.append({"name": metadata["name"], "version": metadata["version"], "wheel": wheel.name,
            "sha256": sha(wheel), "provenance": "local-source-wheel", "source_files": files})
    (wheels / "wheel-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    runtime_version = __import__("tomllib").loads((project / "packages/metaos-runtime/pyproject.toml").read_text())["project"]["version"]
    for name, requirements in [("producer", [f"metaos-runtime=={runtime_version}", "metaos-mock-arm==0.1.0"]),
                               ("sdk-only", ["metaos-mock-arm==0.1.0"])]:
        target = work / name
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target)
        python = target / "bin/python"
        run("install-" + name, [python, "-I", "-B", "-m", "pip", "--isolated", "install", "--no-index",
            "--no-cache-dir", "--find-links", wheels, *requirements])
        run("pip-check-" + name, [python, "-I", "-B", "-m", "pip", "check"])
        run("tests-" + name, [python, "-I", "-B", project / "scripts/run_p0_tests.py", "--tests", project / "tests/p0",
            "--output", work / (name + "-tests.json"), *(["--sdk-only"] if name == "sdk-only" else [])])
    run("verify-delivery", [work / "producer/bin/python", "-I", "-B", project / "scripts/verify_p0.py",
        "--project", project, "--work", work, "--launcher", sys.executable], timeout=600)
    run("original-demo-regression", [sys.executable, "-I", "-B", project / "modularize-demo/scripts/bootstrap.py",
        "--output", work / "demo-regression"], timeout=600)
    summary = json.loads((work / "acceptance-summary.json").read_text())
    regression = json.loads((work / "demo-regression/acceptance-summary.json").read_text())
    if regression.get("status") != "passed" or len(regression["checks"]) < 10:
        raise RuntimeError("Original demo acceptance is incomplete")
    summary["demo_regression"] = {"status": regression["status"], "checks": len(regression["checks"]),
        "summary": str(work / "demo-regression/acceptance-summary.json"), "artifacts": regression["artifacts"]}
    summary.update(status="passed", duration_s=round(time.monotonic() - started, 2), bootstrap_commands=commands)
    (work / "acceptance-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (project / "evidence").mkdir(exist_ok=True)
    (project / "evidence/p0-latest.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"Verified summary: {work / 'acceptance-summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
