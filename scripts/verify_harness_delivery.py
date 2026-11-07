#!/usr/bin/env python3
"""Test immutable Harness rejection paths on a disposable deployment copy."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source, copy = args.bundle.resolve(), output / "harness"
    shutil.copytree(source, copy)
    fingerprint = json.loads((copy / "harness.json").read_text())["fingerprint"]
    checks = []
    def probe(name, expected):
        target = output / ("target-" + name)
        with (output / (name + ".log")).open("w") as stream:
            result = subprocess.run([sys.executable, "-I", "-B", str(copy / "install.py"), "--target", str(target),
                "--expect-fingerprint", expected], stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode == 0 or target.exists():
            raise AssertionError(name)
        checks.append({"name": name, "status": "passed", "exit_code": result.returncode, "target_created": False})
    probe("wrong-expected-fingerprint", "0" * 64)
    path = copy / "task.json"
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    probe("changed-declared-task", fingerprint)
    path.write_bytes(original)
    (copy / "undeclared-file").write_text("unexpected")
    probe("changed-file-inventory", fingerprint)
    (copy / "undeclared-file").unlink()
    path.unlink()
    path.symlink_to(source / "task.json")
    probe("linked-declared-asset", fingerprint)
    path.unlink()
    path.write_bytes(original)
    report = {"status": "passed", "checks": checks}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "count": len(checks), "report": str(output / "report.json")}))


if __name__ == "__main__":
    main()
