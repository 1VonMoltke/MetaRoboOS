#!/usr/bin/env python3
"""Stdlib-only cold installer for the separate original-Agent Harness application."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
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
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--expect-fingerprint", required=True)
    parser.add_argument("--symlinks", action="store_true", help="Use standard venv interpreter symlinks")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = root / "harness.json"
    doc = json.loads(manifest.read_text())
    identity = hashlib.sha256(json.dumps({k: v for k, v in doc.items() if k != "fingerprint"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if identity != doc["fingerprint"] or identity != args.expect_fingerprint or doc["api"] != "metaos.harness/v1":
        raise ValueError("Harness manifest identity differs")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Linked Harness artifact")
        if path.is_file() and path != manifest:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(doc["files"]):
        raise ValueError("Harness file inventory differs")
    for name, digest in doc["files"].items():
        path = root / name
        if not path.resolve().is_relative_to(root) or sha(path) != digest:
            raise ValueError("Harness bytes differ: " + name)
    current = {"implementation": platform.python_implementation(), "version": ".".join(platform.python_version_tuple()[:2]),
        "system": platform.system(), "machine": platform.machine()}
    if current != doc["python"] or args.target.exists():
        raise ValueError("Use a fresh target on the declared Python/platform")
    target = args.target.resolve()
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update(PYTHONDONTWRITEBYTECODE="1", PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1")
    venv.EnvBuilder(with_pip=True, system_site_packages=False, symlinks=args.symlinks).create(target)
    python = target / "bin/python"
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir",
        "--require-hashes", "--find-links", str(root / "wheels"), "-r", str(root / "requirements.lock")], env=env, check=True)
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], env=env, check=True)
    subprocess.run([str(python), "-I", "-B", "-c",
        "import sys;from metaos_runtime.bundle import wheel_index,verify_installed;verify_installed(wheel_index(sys.argv[1]))",
        str(root / "wheels"), str(manifest)], env=env, check=True)
    print(json.dumps({"status": "installed_and_verified", "fingerprint": identity, "python": str(python)}))


if __name__ == "__main__":
    main()
