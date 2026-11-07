#!/usr/bin/env python3
"""Install the fine-grained RoboRSI integration into two owned environments.

The existing Python 3.12 LIBERO environment is an explicitly supplied local
dependency template. Clone it offline; never pip-install into the user's base
environment or inject the original checkout into the new interpreter.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def call(argv, log, env=None):
    with log.open("a") as stream:
        result = subprocess.run(list(map(str, argv)), env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError("Installation failed; inspect " + str(log))


def clone_environment(base_env, conda):
    """Find the source's real Conda caches before copying pip dependencies.

    A source may have been installed with CONDA_PKGS_DIRS different from the
    current shell. An offline clone otherwise copies GB of untracked files
    before discovering that its default cache lacks the Python interpreter.
    """
    records = [json.loads(p.read_text()) for p in (base_env / "conda-meta").glob("*.json")]
    if not records:
        raise ValueError("The dependency template must be a Conda environment")
    info = json.loads(subprocess.check_output([str(conda), "info", "--json"], text=True))
    caches = list(info["pkgs_dirs"])
    for record in records:
        for key in ["extracted_package_dir", "package_tarball_full_path"]:
            if record.get(key):
                parent = str(Path(record[key]).parent)
                if Path(parent).is_dir() and parent not in caches:
                    caches.append(parent)
    missing = []
    for record in records:
        dist = f"{record['name']}-{record['version']}-{record['build']}"
        if not any((Path(cache) / dist / "info/index.json").is_file()
                   or (Path(cache) / record.get("fn", dist + ".conda")).is_file()
                   for cache in caches):
            missing.append(dist)
    if missing:
        raise ValueError("Offline clone needs these exact packages cached first: " + ", ".join(missing))
    return dict(os.environ, CONDA_PKGS_DIRS=",".join(caches))


def install(args):
    project = Path(__file__).absolute().parents[1]
    clone_env = clone_environment(args.base_env, args.conda)
    output = args.output.absolute()
    if output.exists():
        raise FileExistsError("Choose a new installation prefix: " + str(output))
    output.mkdir(parents=True)
    log = output / "install.log"
    source = output / "baseline"
    call([sys.executable, project / "scripts/snapshot_roborsi.py", "--source", args.source, "--output", source], log)
    wheels = output / "wheels"
    wheels.mkdir()
    call([args.base_env / "bin/python", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", wheels,
          project / "packages/metaos-harness", project / "plugins/roborsi-components",
          project / "plugins/roborsi-libero-device", source], log)
    os_python = output / "os-env/bin/python"
    call([args.os_base_python, "-m", "venv", output / "os-env"], log)
    call([os_python, "-m", "pip", "install", "--no-index", "--find-links", args.os_wheelhouse,
          "metaos-runtime==0.2.0", wheels / "metaos_roborsi_libero_device-0.1.0-py3-none-any.whl"], log)
    call([args.conda, "create", "--offline", "--yes", "--clone", args.base_env, "--prefix", output / "harness-env"], log, env=clone_env)
    hp = output / "harness-env/bin/python"
    # Neither the original editable project nor unrelated hardware/TUI plugins
    # belong on the isolated LIBERO Harness path. Their .pth files can resolve
    # code outside the frozen runtime. Original robot policy files are retained.
    call([hp, "-m", "pip", "uninstall", "--yes", "roborsi", "lerobot", "rerun-sdk"], log)
    call([hp, "-m", "pip", "install", "--no-index", "--no-deps", "--find-links", wheels,
          "metaos-harness==0.1.0", "metaos-roborsi-baseline==0.1.0", "metaos-roborsi-components==0.1.0"], log)
    call([hp, "-I", "-B", "-m", "metaos_harness.cli", "plan", "--spec", project / "configs/roborsi-libero.harness.yaml"], log)
    receipt = {"api": "metaos.roborsi-install/v1", "os_python": str(os_python), "harness_python": str(hp),
        "baseline_environment": str(args.base_env.absolute()), "source": str(args.source.absolute()),
        "dependency_strategy": "offline_owned_clone_then_noneditable_wheels",
        "conda_package_caches": clone_env["CONDA_PKGS_DIRS"].split(","),
        "scope": "local_LIBERO_dependency_closure", "installation_log": str(log)}
    (output / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("../RoboRSI"))
    parser.add_argument("--base-env", type=Path, required=True)
    parser.add_argument("--os-base-python", type=Path, required=True)
    parser.add_argument("--os-wheelhouse", type=Path, required=True)
    parser.add_argument("--conda", type=Path, default=Path("/opt/miniconda3/bin/conda"))
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(install(parser.parse_args()), indent=2))


if __name__ == "__main__":
    main()
