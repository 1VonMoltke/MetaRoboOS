#!/usr/bin/env python3
"""Standalone stdlib-only cold installer shipped inside a P0 deployment."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import venv
from pathlib import Path, PurePosixPath


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--expect-fingerprint")
    parser.add_argument("--load-images", action="store_true", help="Load declared offline image archives into their Docker engine")
    args = parser.parse_args(argv)
    bundle = Path(__file__).resolve().parent
    manifest = bundle / "deployment.json"
    if manifest.is_symlink():
        raise ValueError("Linked deployment manifest")
    doc = json.loads(manifest.read_text())
    if doc.get("api") != "metaos.deployment/v1" or doc.get("schema_version") != 1:
        raise ValueError("Unsupported deployment schema")
    content = {k: v for k, v in doc.items() if k != "fingerprint"}
    identity = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if identity != doc["fingerprint"] or args.expect_fingerprint and args.expect_fingerprint != identity:
        raise ValueError("Deployment identity differs")
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ValueError("Linked deployment member")
        if path.is_file() and path != manifest:
            actual.add(path.relative_to(bundle).as_posix())
    if actual != set(doc["files"]):
        raise ValueError("Deployment inventory differs")
    for name, expected in doc["files"].items():
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name:
            raise ValueError("Invalid deployment path")
        digest = hashlib.sha256()
        with (bundle / name).open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("Deployment content differs: " + name)
    current = {"implementation": platform.python_implementation(), "version": ".".join(platform.python_version_tuple()[:2]),
               "system": platform.system(), "machine": platform.machine()}
    if doc["python"] != current:
        raise ValueError("Deployment Python/platform differs")
    target = args.target.absolute()
    if target.exists() or target.is_relative_to(bundle):
        raise ValueError("A new target outside the immutable deployment is required")
    environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX="1", PIP_NO_CACHE_DIR="1")
    venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target)
    python = target / "bin/python"
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir",
                    "--require-hashes", "--find-links", str(bundle / "wheels"), "-r", str(bundle / "requirements.lock")],
                   env=environment, cwd=target, check=True)
    subprocess.run([str(python), "-I", "-B", "-m", "pip", "check"], env=environment, cwd=target, check=True)
    if args.load_images:
        for row in doc.get("native_images", []):
            if row.get("mode") != "archive":
                raise ValueError("Image loading requires an offline archive deployment")
            docker_environment = dict(environment)
            if row.get("docker_host"):
                docker_environment["DOCKER_HOST"] = row["docker_host"]
            subprocess.run(["docker", "image", "load", "--input", str(bundle / row["archive"])],
                env=docker_environment, cwd=target, check=True)
    subprocess.run([str(python), "-I", "-B", "-m", "metaos_runtime", "bundle", "verify", "--bundle", str(bundle),
                    "--installed", "--expect-fingerprint", identity, *( ["--native"] if doc.get("native_images") else [])], env=environment, cwd=target, check=True)
    print(json.dumps({"fingerprint": identity, "python": str(python)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
