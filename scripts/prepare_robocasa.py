#!/usr/bin/env python3
"""Register local RoboCasa inputs for metaos-local plan/build/run; no task launch."""

import argparse
import json
import re
import runpy
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def prepare(installation, resources, image, model, base_url, task_id="OpenDrawer", seed=1, gpu=0, docker_host=None):
    project = Path(__file__).absolute().parents[1]
    local = runpy.run_path(str(project / "scripts/metaos_local.py"))
    read, write = local["read"], local["write"]
    url = urlsplit(base_url)
    if not model.strip() or model.startswith(("SET_", "YOUR_")) or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Supply an explicit model and credential-free HTTP(S) API base URL")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or type(gpu) is not int or gpu < 0 or type(seed) is not int:
        raise ValueError("Supply a local immutable Docker image ID, a nonnegative GPU and an integer seed")
    if docker_host and not docker_host.startswith("unix:///"):
        raise ValueError("Only local Unix Docker transports are supported")
    installed = read(installation)
    assets = Path(installed["assets"])
    inventory = read(Path(installed["output"]) / "asset-inventory.json")
    if local["sha"](Path(installed["output"]) / "asset-inventory.json") != installed["asset_inventory_sha256"]:
        raise ValueError("Asset inventory changed")
    if set(inventory) != {p.relative_to(assets).as_posix() for p in assets.rglob("*") if p.is_file()}:
        raise ValueError("Snapshot asset inventory differs")
    for name, digest in inventory.items():
        path = assets / name
        if not path.resolve().is_relative_to(assets.resolve()) or path.is_symlink() or local["sha"](path) != digest:
            raise ValueError("Snapshot input changed: " + name)
    target = read(assets / "robots/robocasa/eval/target50.json")
    split = next((v for v in target["splits"].values() if task_id in v["tasks"]), None)
    if split is None or seed not in split["seeds"]:
        raise ValueError("Task and seed are outside the original Target50 evaluation catalog")
    # No Docker calls, image pulls or model API calls: plan/build is deliberately
    # separate from runtime preflight. The native image is checked when opened.
    resources = resources.absolute()
    resources.mkdir(parents=True, exist_ok=False)
    for name in ("robocasa", "context", "robocasa-harness"):
        (resources / name).mkdir()
    context = resources / "context/source"
    shutil.copytree(assets, context)
    config = {"schema_version": 1, "extends": "configs/agents/robocasa365/icl.yaml", "source_root": ".",
        "resources": {"icl_package": "icl"},
        "runtime": {"llm": {"provider": "openai", "model": model, "base_url": base_url.rstrip("/"), "openai_format": "chat"}}}
    write(context / "agent.json", config)
    write(context / "task.json", {"task_id": task_id, "seed": seed})
    system_config = {"image": image, "gpu_device": gpu, "resource_id": "environments/robocasa365", "cells": "all"}
    if docker_host:
        system_config["docker_host"] = docker_host
    write(resources / "robocasa/system.json", {"schema_version": 1, "api": "metaos.system/v1",
        "components": {"simulation": {"component": "environment.robocasa_device", "version": ">=0.1,<0.2", "config": system_config}},
        "exports": ["simulation"], "resources": {"simulation": "environments/robocasa365"}})

    def item(path, mode):
        return {"path": str(path), "mode": mode}

    write(resources / "robocasa/resource.json", {"api": local["RESOURCE_API"], "kind": "system", "provides": ["metaos.system/v1"],
        "paths": {"python": item(installed["deployments"]["os"]["python"], "python"), "spec": item("system.json", "file"), "root": item(".", "directory")}})
    write(resources / "context/resource.json", {"api": local["RESOURCE_API"], "kind": "asset", "provides": ["robocasa.context/v1"],
        "paths": {"source": item("source", "tree")}, "outputs": {"config": "{self.paths.source}/agent.json", "task": "{self.paths.source}/task.json"}})
    write(resources / "robocasa-harness/resource.json", {"api": local["RESOURCE_API"], "kind": "application",
        "requires": {"system": "metaos.system/v1", "context": "robocasa.context/v1"}, "env": ["OPENAI_API_KEY"],
        "paths": {"python": item(installed["deployments"]["harness"]["python"], "python"), "launcher": item(project / "scripts/local_robocasa_agent.py", "file")},
        "command": ["{self.paths.python}", "-I", "-B", "{self.paths.launcher}", "--endpoint", "{system.endpoint}", "--credentials", "{run.credentials}",
            "--task", "{context.outputs.task}", "--agent-config", "{context.outputs.config}", "--store", "{run.store}"]})
    write(resources / "robocasa.json", {"api": local["SPEC_API"], "components": {"system": {"resource": "robocasa"},
        "context": {"resource": "context"}, "harness": {"resource": "robocasa-harness", "bindings": {"system": "system", "context": "context"}}}})
    return {"status": "prepared", "resources": str(resources), "spec": str(resources / "robocasa.json"), "task_id": task_id, "seed": seed,
        "native_image_verified": False, "native_task_tested": False, "model_contacted": False,
        "runtime_prerequisites": ["Locally built RoboCasa benchmark image (not just its base image), bundled kitchen assets and RLDX-1 weights", "Image/tool-capable API model, explicit key in OPENAI_API_KEY"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Actual local sha256 image ID of the RoboRSI RoboCasa benchmark image")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--task-id", default="OpenDrawer")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--docker-host")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.installation, args.resources, args.image, args.model, args.base_url, args.task_id, args.seed, args.gpu, args.docker_host), indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
