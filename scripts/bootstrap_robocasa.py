#!/usr/bin/env python3
"""Build/install RoboCasa plugins offline; snapshot inputs without opening a task."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--icl", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path(__file__).absolute().parents[1] / "evidence/p1p2-latest.json")
    args = parser.parse_args()
    project = Path(__file__).absolute().parents[1]
    previous = json.loads(args.evidence.read_text())["artifacts"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output / "logs"
    logs.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"}}
    env.update(PIP_NO_INDEX="1", PIP_CONFIG_FILE=os.devnull, PYTHONDONTWRITEBYTECODE="1", PIP_NO_CACHE_DIR="1")
    commands = []

    def run(name, command, cwd=project):
        with (logs / (name + ".log")).open("w") as stream:
            result = subprocess.run([str(x) for x in command], cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=600)
        commands.append({"name": name, "exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError(name + " failed; see " + str(logs / (name + ".log")))
        print("PASS " + name, flush=True)

    wheels = output / "wheels"
    wheels.mkdir()
    for kind in ("device", "harness"):
        run("build-" + kind, [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
            "--wheel-dir", wheels, project / ("plugins/robocasa-" + kind)])
    source_hashes = {}
    for kind in ("device", "harness"):
        package = project / ("plugins/robocasa-" + kind)
        for path in [package / "pyproject.toml", *sorted((package / "src").rglob("*.py"))]:
            source_hashes[str(path.relative_to(project))] = sha(path)
    deployments = {}
    for role, baseline, requirement in (
        ("os", Path(previous["system_deployment"]), "metaos-robocasa-device==0.1.0"),
        ("harness", Path(previous["harness_deployment"]), "metaos-robocasa-harness==0.1.0"),
    ):
        delivery = output / (role + "-deployment")
        wheelhouse = delivery / "wheels"
        wheelhouse.mkdir(parents=True)
        base_manifest = json.loads((baseline / ("deployment.json" if role == "os" else "harness.json")).read_text())
        for path in sorted((baseline / "wheels").glob("*.whl")):
            if sha(path) != base_manifest["files"].get("wheels/" + path.name):
                raise ValueError("Baseline wheel identity changed: " + path.name)
            shutil.copyfile(path, wheelhouse / path.name)
        for path in wheels.glob("*.whl"):
            shutil.copyfile(path, wheelhouse / path.name)
        # The baseline Harness also supplies the pinned MetaOS runtime wheels.
        if role == "os":
            requirements = ["metaos-runtime==0.2.0", requirement]
        else:
            requirements = [requirement]
        resolution = output / (role + "-resolution.json")
        run(role + "-resolve", [sys.executable, "-I", "-B", "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
            "--no-index", "--find-links", wheelhouse, "--report", resolution, *requirements])
        packages = {row["metadata"]["name"]: {"version": row["metadata"]["version"],
            "sha256": row["download_info"]["archive_info"]["hashes"]["sha256"]} for row in json.loads(resolution.read_text())["install"]}
        selected = {row["sha256"] for row in packages.values()}
        for path in wheelhouse.glob("*.whl"):
            if sha(path) not in selected:
                path.unlink()
        (delivery / "requirements.lock").write_text("".join(f"{name}=={row['version']} --hash=sha256:{row['sha256']}\n" for name, row in sorted(packages.items())))
        shutil.copyfile(project / "scripts/install_harness.py", delivery / "install.py")
        manifest = {"api": "metaos.harness/v1", "role": role, "schema_version": 1, "packages": packages,
            "python": {"implementation": platform.python_implementation(), "version": ".".join(platform.python_version_tuple()[:2]),
                "system": platform.system(), "machine": platform.machine()},
            "files": {p.relative_to(delivery).as_posix(): sha(p) for p in sorted(delivery.rglob("*")) if p.is_file()}}
        manifest["fingerprint"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        (delivery / "harness.json").write_text(json.dumps(manifest, indent=2) + "\n")
        target = output / (role + "-env")
        run(role + "-install", [sys.executable, "-I", "-B", delivery / "install.py", "--target", target, "--expect-fingerprint", manifest["fingerprint"], "--symlinks"])
        deployments[role] = {"deployment": str(delivery), "python": str(target / "bin/python"), "fingerprint": manifest["fingerprint"], "packages": len(packages)}
    assets = output / "assets"
    assets.mkdir()
    baseline = args.baseline.resolve()
    for relative in ("robots/__init__.py", "configs/agents/embodied/default.yaml", "configs/agents/robocasa365/icl.yaml", "configs/agents/robocasa365/target50_prompt.md", "LICENSE"):
        destination = assets / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline / relative, destination)
    shutil.copytree(baseline / "robots/robocasa", assets / "robots/robocasa", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(args.icl, assets / "icl", ignore=shutil.ignore_patterns(".download.json"))
    # A minimal native build context, no image pull/build and no native imports.
    run("native-context", [sys.executable, "-B", "-m", "benchmarks.package", "--benchmark", "robocasa365", "--output", output / "native-context"], cwd=baseline)
    source_manifest = {p.relative_to(assets).as_posix(): sha(p) for p in sorted(assets.rglob("*")) if p.is_file()}
    (output / "asset-inventory.json").write_text(json.dumps(source_manifest, indent=2) + "\n")
    summary = {"status": "installed_inputs_snapshotted", "native_task_tested": False, "model_inference": False,
        "native_image_built": False, "output": str(output), "assets": str(assets), "deployments": deployments,
        "plugin_source_sha256": source_hashes, "asset_inventory_sha256": sha(output / "asset-inventory.json"), "commands": commands}
    (output / "installation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
