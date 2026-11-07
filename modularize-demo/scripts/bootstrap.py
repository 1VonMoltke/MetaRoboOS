#!/usr/bin/env python3
"""Build the external plugin offline, then run acceptance in fresh installed envs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new output directory; never overwritten")
    args = parser.parse_args()
    demo = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 11) or platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("The supplied offline wheel closure targets CPython 3.11 / Linux x86_64")
    if args.output:
        work = args.output.absolute()
        work.mkdir(parents=True, exist_ok=False)
    else:
        (demo / "work").mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="acceptance-", dir=demo / "work"))
    (work / "tmp").mkdir()
    (work / "logs").mkdir()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update(TMPDIR=str(work / "tmp"), PYTHONDONTWRITEBYTECODE="1",
               PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1")
    print(f"Output: {work}", flush=True)

    def run(name, arguments):
        with (work / "logs" / f"{name}.log").open("w") as log:
            result = subprocess.run([str(x) for x in arguments], cwd=work, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=240)
        if result.returncode:
            raise RuntimeError(f"{name} failed; see {work / 'logs' / (name + '.log')}")
        print(f"PASS {name}", flush=True)

    wheels = work / "wheels"
    shutil.copytree(demo / "vendor" / "wheels", wheels)
    # No editable install and no index access. Build tools belong to the launcher
    # Python only; they are not needed by the finished deployment/install.py.
    run("build-plugin", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "wheel",
                        "--no-index", "--no-deps", "--no-cache-dir", "--no-build-isolation",
                        "--wheel-dir", wheels, demo / "plugins" / "manual-counter"])
    plugin = next(wheels.glob("roborsi_manual_counter-*.whl"))
    provenance = json.loads((wheels / "wheel-provenance.json").read_text())
    provenance.append({"name": "roborsi-manual-counter", "version": "1.0.0",
                       "wheel": plugin.name, "sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
                       "provenance": "local-source-wheel"})
    (wheels / "wheel-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    for name, requirements in (
        ("producer", ["roborsi==0.1.0", "roborsi-example-component==1.0.0", "roborsi-manual-counter==1.0.0"]),
        ("sdk-only", ["roborsi-manual-counter==1.0.0"]),
    ):
        target = work / name
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target)
        python = target / "bin" / "python"
        run(f"install-{name}", [python, "-I", "-B", "-m", "pip", "--isolated", "install",
                               "--no-index", "--no-cache-dir", "--find-links", wheels, *requirements])
        run(f"pip-check-{name}", [python, "-I", "-B", "-m", "pip", "check"])
    run("sdk-only-tests", [work / "sdk-only/bin/python", "-I", "-B", "-m", "unittest", "discover",
                           "-s", demo / "plugins/manual-counter/tests", "-v"])
    # Stream the acceptance progress; each underlying command has separate logs.
    result = subprocess.run([str(work / "producer/bin/python"), "-I", "-B",
                             str(demo / "scripts/verify_demo.py"), "--demo", str(demo),
                             "--work", str(work)], cwd=work, env=env, timeout=600)
    if result.returncode:
        raise RuntimeError(f"Acceptance failed; inspect {work / 'logs'}")
    summary = json.loads((work / "acceptance-summary.json").read_text())
    (demo / "evidence").mkdir(exist_ok=True)
    (demo / "evidence/latest.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nVerified summary: {work / 'acceptance-summary.json'}")
    print(f"Cold runtime: {work / 'cold/bin/python'}")
    print(f"Portable deployment: {work / 'relocated/manual-deployment'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
