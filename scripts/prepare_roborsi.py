#!/usr/bin/env python3
"""Compile an installed fine-grained RoboRSI Harness into local MetaOS resources.

No model calls, simulator creation or credentials in this preparation step.
The outer local bundle owns processes; the inner bundle owns component ports.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def execute(argv):
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Preparation command failed: " + result.stderr[-2000:])
    return json.loads(result.stdout)


def materialize_models(cache, output):
    """Resolve existing HF revisions to ordinary immutable-input files."""
    models = {}
    for key, repo in {"ROBORSI_GROUNDING_DINO_MODEL": "IDEA-Research/grounding-dino-tiny",
                      "ROBORSI_POINT_SAM_MODEL": "facebook/sam-vit-base",
                      "ROBORSI_OWLV2_MODEL": "google/owlv2-large-patch14-ensemble"}.items():
        cached = cache / ("models--" + repo.replace("/", "--"))
        revision = (cached / "refs/main").read_text().strip()
        source = cached / "snapshots" / revision
        destination = output / repo.replace("/", "--")
        destination.mkdir(parents=True)
        files = {}
        for path in source.rglob("*"):
            if path.is_file():
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path.resolve(strict=True), target)
                except OSError:
                    shutil.copyfile(path.resolve(strict=True), target)
                files[path.relative_to(source).as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        if not any(name.endswith((".safetensors", ".bin")) for name in files):
            raise ValueError("Missing prepared model weights: " + repo)
        models[key] = {"repo": repo, "revision": revision, "path": str(destination), "files": files}
    write(output / "models.json", models)
    return models


def prepare(args):
    project = Path(__file__).absolute().parents[1]
    local = runpy.run_path(str(project / "scripts/metaos_local.py"))
    output = args.output.absolute()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    resources = output / "resources"
    hp, op = args.harness_python.absolute(), args.os_python.absolute()
    root, bddl, init = args.libero_root.resolve(), args.bddl.resolve(), args.init.resolve()
    for path in [hp, op, root, bddl, init, args.spec, args.experiment, args.pyroki_python]:
        if not path.exists():
            raise FileNotFoundError(path)
    inner = output / "harness"
    inner_report = execute([hp, "-I", "-B", "-m", "metaos_harness.cli", "build", "--spec", args.spec, "--output", inner])
    spec = json.loads((inner / "harness.json").read_text())
    episode = spec["components"][spec["entrypoint"]]
    tool_node = spec["components"][episode["bindings"]["tools"]]
    skill_node = spec["components"][tool_node["bindings"]["skills"]]
    skill_alias = tool_node["bindings"]["skills"]
    skill_nodes = {}
    def collect_skills(alias):
        if alias not in skill_nodes:
            skill_nodes[alias] = spec["components"][alias]
            for target in skill_nodes[alias].get("bindings", {}).values():
                collect_skills(target)
    collect_skills(skill_alias)
    skill_graph = {"api": "metaos.harness-spec/v1", "name": "native-skills", "entrypoint": skill_alias, "components": skill_nodes}
    engineer = spec["components"][episode["bindings"]["engineer"]]
    model_node = spec["components"][engineer["bindings"]["model"]]
    environment_node = spec["components"][episode["bindings"]["environment"]]
    # The preparation launcher itself only needs stdlib; YAML belongs to the
    # declared Harness environment, not whichever Python launched this script.
    experiment = execute([hp, "-I", "-B", "-c",
        "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))", args.experiment])
    panel_path = getattr(args, "panel", None)
    if panel_path is not None:
        experiment = execute([hp, "-I", "-B", "-c",
            "import json,sys; from metaos_roborsi_campaign.panel import validate; print(json.dumps(validate(json.load(open(sys.argv[1])))))",
            panel_path])
    tasks = experiment["tasks"] if panel_path is not None else [experiment["sim_task"]]
    write(output / "experiment.json", experiment)
    model_assets = materialize_models(args.hf_cache.absolute(), output / "models")
    # Installed-byte identity, not the original checkout on sys.path.
    probe = """import json,sys,importlib.metadata as m
from metaos_harness.graph import distribution_identity
import roborsi
print(json.dumps({'python':sys.version,'base_prefix':sys.base_prefix,'roborsi_module':roborsi.__file__,
'components':[distribution_identity(n) for n in ['metaos-harness','metaos-roborsi-baseline','metaos-roborsi-components']]}))
"""
    identity = execute([hp, "-I", "-B", "-c", probe])
    # A campaign venv may explicitly inherit the already locked baseline
    # installation via --system-site-packages, without changing that baseline.
    allowed_prefixes = [hp.parent.parent.resolve(), Path(identity["base_prefix"]).resolve()]
    if not any(Path(identity["roborsi_module"]).resolve().is_relative_to(prefix) for prefix in allowed_prefixes):
        raise ValueError("RoboRSI is not installed inside the selected isolated runtime")
    installed_graph_lock = json.loads((inner / "harness.lock.json").read_text())
    runtime = {"api": "metaos.roborsi-native/v1", "python": str(hp), "component_identity": list(installed_graph_lock["distributions"].values()),
        "native_config": {"tasks": tasks, "run_root": str(args.run_root.absolute()),
            "skill_harness": skill_graph},
        "environment": {"ROBORSI_LIBERO_ROOT": str(root), "ROBORSI_LIBERO_BDDLDIR": str(bddl),
            "ROBORSI_LIBERO_INITDIR": str(init), "ROBORSI_HOME": str(output / "native-home"),
            "ROBORSI_RUN_MODE": "frozen", "ROBORSI_VLM_PROVIDER": "openai",
            "ROBORSI_OPENAI_TRANSPORT": model_node["config"]["transport"],
            "OPENAI_BASE_URL": model_node["config"].get("base_url", "https://api.openai.com/v1"),
            "ROBORSI_PERCEPTION_MODEL": engineer["config"]["model"],
            "ROBORSI_PYROKI_PORT": str(args.pyroki_port), "GRASPGEN_HOST": "127.0.0.1",
            "ROBORSI_SAM3_PORT": "0", "ROBORSI_VLM_PORT": "0",
            "GRASPGEN_PORT": str(args.graspgen_port) if args.graspgen_port else "",
            "MUJOCO_GL": "egl", "MUJOCO_EGL_DEVICE_ID": str(args.gpu), "CUDA_VISIBLE_DEVICES": str(args.gpu)}}
    runtime["environment"].update({key: value["path"] for key, value in model_assets.items()})
    write(output / "native-runtime.json", runtime)
    write(output / "system.json", {"api": "metaos.system/v1", "schema_version": 1,
        "components": {"libero": {"component": "environment.roborsi_libero_device", "version": "==0.1.0",
            "config": {"resource_id": environment_node["config"]["resource_id"],
                       "runtime_manifest": str(output / "native-runtime.json")}}},
        "exports": ["libero"], "resources": {"libero": environment_node["config"]["resource_id"]}})
    def item(path, mode):
        return {"path": str(path), "mode": mode}
    def resource(name, value):
        write(resources / name / "resource.json", {"api": "metaos.local-resource/v1", **value})
    resource("system", {"kind": "system", "provides": ["metaos.system/v1"], "env": ["OPENAI_API_KEY"],
        "requires": {"kinematics": "pyroki.ik/v1"},
        "paths": {"python": item(op, "python"), "spec": item(output / "system.json", "file"),
                  "root": item(output, "directory"), "native-runtime": item(output / "native-runtime.json", "file")}})
    pyroki_source = Path(identity["roborsi_module"]).parent / "embodied/motion/pyroki"
    resource("pyroki", {"kind": "service", "provides": ["pyroki.ik/v1"], "requires": {"assets": "roborsi.libero-assets/v1"},
        "paths": {"python": item(args.pyroki_python.absolute(), "python"), "source": item(pyroki_source, "tree"),
                  "launcher": item(project / "scripts/owned_pyroki.py", "file")},
        "command": ["{self.paths.python}", "-I", "-B", "{self.paths.launcher}", "--source", "{self.paths.source}",
            "--http-port", "{self.port}", "--zmq-port", str(args.pyroki_port), "--gpu", str(args.gpu),
            "--robot-cache", str(args.robot_cache.absolute()),
            "--record", "{run.directory}/pyroki-process.json"],
        "endpoint": "http://127.0.0.1:{self.port}", "health_url": "http://127.0.0.1:{self.port}/health",
        "startup_timeout_s": 600})
    resource("assets", {"kind": "asset", "provides": ["roborsi.libero-assets/v1"], "paths": {
        "libero-code": item(root / "libero", "tree"), "bddl": item(bddl, "tree"), "init": item(init, "tree"),
        "perception-models": item(output / "models", "tree"),
        "panda": item(args.robot_cache.absolute() / "example-robot-data/robots/panda_description", "tree"),
        "harness": item(inner, "tree"), "experiment": item(output / "experiment.json", "file")}})
    entry = ["metaos_roborsi_campaign.episode"] if panel_path is not None else ["metaos_harness.cli", "run"]
    experiment_flag = "--panel" if panel_path is not None else "--experiment"
    resource("harness", {"kind": "application", "requires": {"system": "metaos.system/v1", "assets": "roborsi.libero-assets/v1"},
        "env": ["OPENAI_API_KEY"] + (["METAOS_ROBORSI_EPISODE_INDEX"] if panel_path is not None else []),
        "paths": {"python": item(hp, "python")},
        "command": ["{self.paths.python}", "-I", "-B", "-m", *entry,
            "--bundle", "{assets.paths.harness}", experiment_flag, "{assets.paths.experiment}",
            "--endpoint", "{system.endpoint}", "--credentials", "{run.credentials}", "--output", "{run.store}"]})
    write(output / "local.json", {"api": "metaos.local-harness/v1", "components": {
        "pyroki": {"resource": "pyroki", "bindings": {"assets": "assets"}}, "system": {"resource": "system", "bindings": {"kinematics": "pyroki"}}, "assets": {"resource": "assets"},
        "harness": {"resource": "harness", "bindings": {"system": "system", "assets": "assets"}}}})
    write(output / "preparation.json", {"api": "metaos.roborsi-preparation/v1", "inner": inner_report,
        "runtime_identity": identity, "gpu": args.gpu, "pyroki_port": args.pyroki_port,
        "graspgen_port": args.graspgen_port, "mode": "local_content_locked_references",
        "panel_tasks": len(tasks),
        "limits": ["External model aliases are not immutable model weight identities",
            "One isolated episode per invocation; campaign results require actual execution"]})
    return {"resources": str(resources), "spec": str(output / "local.json"), "inner_harness": inner_report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["harness-python", "os-python", "libero-root", "bddl", "init", "output", "run-root"]:
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--spec", type=Path, default=Path("configs/roborsi-libero.harness.yaml"))
    parser.add_argument("--experiment", type=Path, default=Path("configs/roborsi-libero.experiment.yaml"))
    parser.add_argument("--panel", type=Path, help="Locked LIBERO-PRO task panel; enables campaign episode selection")
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--pyroki-port", type=int, default=15559)
    parser.add_argument("--pyroki-python", type=Path, required=True)
    parser.add_argument("--hf-cache", type=Path, default=Path.home() / ".cache/huggingface/hub")
    parser.add_argument("--robot-cache", type=Path, default=Path.home() / ".cache/robot_descriptions")
    parser.add_argument("--graspgen-port", type=int, default=0)
    args = parser.parse_args()
    if args.gpu < 0 or not 0 < args.pyroki_port < 65536 or not 0 <= args.graspgen_port < 65536:
        raise ValueError("Invalid resource assignment")
    print(json.dumps(prepare(args), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
