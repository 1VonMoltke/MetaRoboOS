#!/usr/bin/env python3
"""Local, stdlib-only resource composition over the existing MetaOS runtime."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


RESOURCE_API = "metaos.local-resource/v1"
SPEC_API = "metaos.local-harness/v1"
BUNDLE_API = "metaos.local-bundle/v1"
NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
TOKEN = re.compile(r"\{([a-zA-Z0-9_.-]+)\}")
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH")


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def absolute(value, root):
    path = Path(value).expanduser()
    # Do not resolve interpreter symlinks: doing so discards a venv's prefix.
    return Path(os.path.abspath(path if path.is_absolute() else root / path))


def environment(names=()):
    env = {key: os.environ[key] for key in BASE_ENV if key in os.environ}
    for key in names:
        if key not in os.environ:
            raise ValueError("Required environment variable is absent: " + key)
        env[key] = os.environ[key]
    env.update(PYTHONDONTWRITEBYTECODE="1", PIP_NO_INDEX="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    return env


# Hash installed package bytes, not just version labels or RECORD claims.
PYTHON_IDENTITY = r'''
import hashlib,importlib.metadata,json,sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as s:
        for b in iter(lambda:s.read(4*1024*1024),b''): h.update(b)
    return h.hexdigest()
def file_identity(d,f):
    p=Path(d.locate_file(f))
    if p.suffix=='.pth' and not (d.metadata['Name'].lower()=='setuptools' and p.name=='distutils-precedence.pth'):
        raise ValueError('Path injected distribution: '+d.metadata['Name'])
    if not p.is_file(): raise ValueError('Missing installed file: '+str(f))
    if str(f).endswith('direct_url.json'):
        locator=json.loads(p.read_text())
        if locator.get('dir_info',{}).get('editable'): raise ValueError('Editable distribution')
    return str(f),sha(p)
packages=[]
# Cold ML environments contain tens of thousands of small headers/test files.
# Bound filesystem concurrency while still hashing every tracked byte. map
# preserves input order; the resulting content identity is unchanged.
with ThreadPoolExecutor(max_workers=8) as pool:
    for d in sorted(importlib.metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
        if d.files is None: raise ValueError('Untracked distribution: '+d.metadata['Name'])
        tracked=[f for f in d.files if f.suffix!='.pyc' and '__pycache__' not in f.parts]
        files=dict(pool.map(partial(file_identity,d),tracked))
        packages.append({'name':d.metadata['Name'],'version':d.version,'files':files})
cfg=Path(sys.prefix)/'pyvenv.cfg'
print(json.dumps({'version':sys.version,'prefix':sys.prefix,'base_prefix':sys.base_prefix,
    'interpreter_sha256':sha(Path(sys.executable).resolve()),
    'venv_config':sha(cfg) if cfg.exists() else None,'packages':packages},sort_keys=True))
'''


def snapshot(path, mode):
    if mode == "python":
        result = subprocess.run([str(path), "-I", "-B", "-c", PYTHON_IDENTITY],
            # Full ML environments include GB of libraries and tens of
            # thousands of headers. Retain byte verification, but allow a
            # bounded cold-cache scan on slower local storage.
            env=environment(), capture_output=True, text=True, timeout=1200)
        if result.returncode:
            raise ValueError("Cannot identify installed Python environment: " + str(path))
        return json.loads(result.stdout)
    if mode == "file":
        if not path.is_file():
            raise ValueError("Missing resource file: " + str(path))
        return {"sha256": sha(path), "size": path.stat().st_size}
    if mode == "directory":
        if not path.is_dir():
            raise ValueError("Missing resource directory: " + str(path))
        return {"exists": True}
    if mode == "tree":
        if not path.is_dir():
            raise ValueError("Missing asset tree: " + str(path))
        files = {}
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise ValueError("Asset tree must contain ordinary files: " + str(item))
            if item.is_file():
                files[item.relative_to(path).as_posix()] = snapshot(item, "file")
        return {"files": files}
    raise ValueError("Unknown path mode: " + str(mode))


def render(value, context):
    def replace(match):
        key = match.group(1)
        if key not in context:
            raise ValueError("Unbound template variable: " + key)
        return str(context[key])
    return TOKEN.sub(replace, value)


def argv(command, context):
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError("Commands must be nonempty argv lists, never shell strings")
    return [render(x, context) for x in command]


def compose(spec_path, resources):
    spec = read(spec_path)
    if spec.get("api") != SPEC_API or not isinstance(spec.get("components"), dict):
        raise ValueError("Expected " + SPEC_API)
    if not spec["components"]:
        raise ValueError("Harness has no components")
    nodes = {}
    root = resources.resolve()
    for alias, selection in spec["components"].items():
        if not NAME.fullmatch(alias) or not isinstance(selection, dict):
            raise ValueError("Invalid component alias or selection")
        ref = selection.get("resource", "")
        source = (root / ref / "resource.json").resolve()
        if not source.is_relative_to(root):
            raise ValueError("Resource reference escapes local resource pool")
        descriptor = read(source)
        if descriptor.get("api") != RESOURCE_API or descriptor.get("kind") not in {"system", "application", "service", "asset"}:
            raise ValueError("Invalid local resource descriptor: " + ref)
        for field in ("provides", "env"):
            if not isinstance(descriptor.get(field, []), list) or not all(isinstance(x, str) for x in descriptor.get(field, [])):
                raise ValueError("Invalid resource " + field)
        required = descriptor.get("requires", {})
        bindings = selection.get("bindings", {})
        if not isinstance(required, dict) or not isinstance(bindings, dict) or set(required) != set(bindings):
            raise ValueError("Required ports must be explicitly bound: " + alias)
        outputs = descriptor.get("outputs", {})
        if not isinstance(outputs, dict) or any(not NAME.fullmatch(k) or not isinstance(v, str) for k, v in outputs.items()):
            raise ValueError("Resource outputs must be named string templates")
        paths = {}
        for name, item in descriptor.get("paths", {}).items():
            if not NAME.fullmatch(name) or not isinstance(item, dict) or set(item) != {"path", "mode"}:
                raise ValueError("Invalid path declaration")
            if item["mode"] not in {"file", "tree", "directory", "python"}:
                raise ValueError("Invalid path mode")
            if item["mode"] == "directory" and not (descriptor["kind"] == "system" and name == "root"):
                raise ValueError("Only the OS root may be an existence-only directory; track assets with tree")
            paths[name] = {"path": str(absolute(item["path"], source.parent)), "mode": item["mode"]}
        nodes[alias] = {"descriptor": descriptor, "source": str(source), "paths": paths, "bindings": bindings}
    order, active = [], set()
    def visit(alias):
        if alias not in nodes:
            raise ValueError("Missing binding target: " + alias)
        if alias in active:
            raise ValueError("Component dependency cycle: " + alias)
        if alias in order:
            return
        active.add(alias)
        node = nodes[alias]
        for port, target in node["bindings"].items():
            visit(target)
            if node["descriptor"]["requires"][port] not in nodes[target]["descriptor"].get("provides", []):
                raise ValueError("Incompatible port: " + alias + "." + port)
        active.remove(alias)
        order.append(alias)
    for alias in nodes:
        visit(alias)
    systems = [n for n in nodes if nodes[n]["descriptor"]["kind"] == "system"]
    apps = [n for n in nodes if nodes[n]["descriptor"]["kind"] == "application"]
    if len(systems) != 1 or len(apps) != 1:
        raise ValueError("Local v1 needs exactly one OS system and one application")
    if order[-1] != apps[0] or set(order) - {apps[0]} - ancestors(apps[0], nodes):
        raise ValueError("Every selected resource must be a dependency of the application")
    for alias, node in nodes.items():
        desc = node["descriptor"]
        if desc["kind"] == "system" and not {"python", "spec", "root"}.issubset(node["paths"]):
            raise ValueError("System needs python, spec and root paths")
        if desc["kind"] in {"application", "service"}:
            argv(desc.get("command"), {key: "placeholder" for x in desc.get("command", []) if isinstance(x, str) for key in TOKEN.findall(x)})
        if desc["kind"] == "service" and not all(isinstance(desc.get(key), str) for key in ("health_url", "endpoint")):
            raise ValueError("Service needs health_url and endpoint")
        startup = float(desc.get("startup_timeout_s", 300.))
        if not math.isfinite(startup) or not 0 < startup <= 86400:
            raise ValueError("Invalid service startup timeout")
    return {"api": BUNDLE_API, "spec": spec, "components": nodes, "order": order,
        "system": systems[0], "application": apps[0]}


def ancestors(alias, nodes):
    result = set()
    for target in nodes[alias]["bindings"].values():
        result.add(target)
        result.update(ancestors(target, nodes))
    return result


def context_for(bundle, alias, dynamic=None):
    allowed = ancestors(alias, bundle["components"]) | {alias}
    context = {key: value for key, value in (dynamic or {}).items() if key.split(".")[0] in allowed | {"run"}}
    for name, node in bundle["components"].items():
        if name not in allowed:
            continue
        for key, item in node["paths"].items():
            context[name + ".paths." + key] = item["path"]
    def aliases(name):
        values = dict(context)
        for prefix, target in [("self", name), *bundle["components"][name]["bindings"].items()]:
            for key, value in context.items():
                if key.startswith(target + "."):
                    values[prefix + key[len(target):]] = value
        return values
    for name in bundle["order"]:
        if name in allowed:
            for key, value in bundle["components"][name]["descriptor"].get("outputs", {}).items():
                context[name + ".outputs." + key] = render(value, aliases(name))
    return aliases(alias)


def validate_templates(bundle):
    dynamic = {"run.directory": "run", "run.credentials": "credentials", "run.store": "store"}
    for name, node in bundle["components"].items():
        if node["descriptor"]["kind"] in {"system", "service"}:
            for field in ("endpoint", "port", "ready_file"):
                dynamic[name + "." + field] = "runtime"
    for alias, node in bundle["components"].items():
        desc = node["descriptor"]
        context = context_for(bundle, alias, dynamic)
        if "command" in desc:
            argv(desc["command"], context)
        for key in ("health_url", "endpoint"):
            if key in desc:
                render(desc[key], context)


def inspect_inputs(bundle):
    identities = {}
    python_cache = {}
    for alias in bundle["order"]:
        node = bundle["components"][alias]
        rows = {}
        for name, item in node["paths"].items():
            path = Path(item["path"])
            if item["mode"] == "python":
                if str(path) not in python_cache:
                    python_cache[str(path)] = snapshot(path, "python")
                rows[name] = python_cache[str(path)]
            else:
                rows[name] = snapshot(path, item["mode"])
        identities[alias] = rows
    return identities


def system_plan(bundle):
    node = bundle["components"][bundle["system"]]
    paths = {key: value["path"] for key, value in node["paths"].items()}
    command = [paths["python"], "-I", "-B", "-m", "metaos_runtime", "plan", "--spec", paths["spec"], "--root", paths["root"]]
    result = subprocess.run(command, env=environment(), capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("Existing OS plan rejected the selected system")
    return json.loads(result.stdout)


def build(spec, resources, output):
    bundle = compose(spec, resources)
    validate_templates(bundle)
    bundle["inputs"] = inspect_inputs(bundle)
    bundle["system_plan"] = system_plan(bundle)
    bundle["source_spec"] = str(spec.resolve())
    bundle["fingerprint"] = digest(bundle)
    if output.exists():
        raise ValueError("Build output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".metaos-local-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        write(stage / "bundle.json", bundle)
        write(stage / "system.lock.json", bundle["system_plan"]["lock"])
        shutil.move(stage, output)
    return {"status": "built", "bundle": str(output.absolute()), "fingerprint": bundle["fingerprint"],
        "components": bundle["order"], "mode": "local_references", "model_inference": False}


def install(directory, target, python, expected=None, load_images=False):
    directory = directory.resolve()
    if (directory / "deployment.json").is_file():
        manifest, api = directory / "deployment.json", "metaos.deployment/v1"
    elif (directory / "harness.json").is_file():
        manifest, api = directory / "harness.json", "metaos.harness/v1"
    else:
        raise ValueError("Expected an exported local OS or Harness deployment")
    doc = read(manifest)
    identity = doc.get("fingerprint")
    if doc.get("api") != api or digest({k: v for k, v in doc.items() if k != "fingerprint"}) != identity:
        raise ValueError("Local deployment identity differs")
    if expected is not None and identity != expected:
        raise ValueError("Local deployment differs from expected fingerprint")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Linked deployment member")
        if path.is_file() and path != manifest:
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(doc["files"]):
        raise ValueError("Deployment file inventory differs")
    for name, value in doc["files"].items():
        path = directory / name
        if not path.resolve().is_relative_to(directory) or sha(path) != value:
            raise ValueError("Deployment content differs")
    if target.exists() or target.absolute().is_relative_to(directory):
        raise ValueError("Install target must be a new directory outside the deployment")
    if load_images and api != "metaos.deployment/v1":
        raise ValueError("Only an OS deployment may load images")
    command = [str(python.absolute()), "-I", "-B", str(directory / "install.py"), "--target", str(target.absolute()),
        "--expect-fingerprint", identity, *(["--load-images"] if load_images else [])]
    result = subprocess.run(command, env=environment())
    if result.returncode:
        raise ValueError("Local installer failed; inspect its output and incomplete target")
    return {"status": "installed", "python": str(target.absolute() / "bin/python"), "fingerprint": identity,
        "source": str(directory), "remote_download": False}


def verify(directory):
    bundle = read(directory / "bundle.json")
    expected = bundle.pop("fingerprint")
    if bundle.get("api") != BUNDLE_API or digest(bundle) != expected:
        raise ValueError("Local bundle identity differs")
    bundle["fingerprint"] = expected
    validate_templates(bundle)
    if inspect_inputs(bundle) != bundle["inputs"]:
        raise ValueError("A local resource changed; rebuild before running")
    if system_plan(bundle) != bundle["system_plan"] or read(directory / "system.lock.json") != bundle["system_plan"]["lock"]:
        raise ValueError("OS plan or lock changed")
    return bundle


def proc_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else (stat[19], Path("/proc/sys/kernel/random/boot_id").read_text())
    except FileNotFoundError:
        return None


def run(directory, output, timeout):
    bundle = verify(directory)
    if not 0 < timeout <= 86400:
        raise ValueError("Run timeout must be between 0 and 86400 seconds")
    # Check required credentials before creating a run or starting the OS.
    for node in bundle["components"].values():
        environment(node["descriptor"].get("env", []))
        if any(value.startswith("SET_") for value in node["descriptor"].get("outputs", {}).values()):
            raise ValueError("Configure placeholder resource outputs and rebuild before running")
    output.mkdir(parents=True, exist_ok=False)
    output = output.absolute()
    output.chmod(0o700)
    credentials = output / "credentials.json"
    dynamic = {"run.directory": str(output), "run.credentials": str(credentials), "run.store": str(output / "results")}
    processes, streams = [], []
    report = {"status": "starting", "bundle_fingerprint": bundle["fingerprint"], "model_inference": "not_asserted",
        "output": str(output), "processes": [], "cleanup": []}
    end = time.monotonic() + timeout
    def start(alias, command, env):
        stream = (output / (alias + ".log")).open("w")
        streams.append(stream)
        process = subprocess.Popen(command, cwd=output, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        row = {"alias": alias, "pid": process.pid, "identity": proc_identity(process.pid)}
        report["processes"].append(row)
        processes.append((alias, process, row["identity"]))
        write(output / "run.json", report)
        return process
    def wait_ready(process, probe, budget):
        deadline = min(end, time.monotonic() + budget)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("A required service exited before readiness; inspect its log")
            try:
                if probe():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise TimeoutError("Service readiness timed out")
    try:
        for alias in bundle["order"]:
            node, desc = bundle["components"][alias], bundle["components"][alias]["descriptor"]
            kind = desc["kind"]
            if kind == "asset":
                continue
            env = environment(desc.get("env", []))
            if kind == "system":
                paths = {key: item["path"] for key, item in node["paths"].items()}
                result = subprocess.run([paths["python"], "-I", "-B", "-m", "metaos_runtime", "credentials", "--output", str(credentials)],
                    env=env, capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError("Cannot create local OS credentials")
                ready = output / (alias + "-ready.json")
                dynamic[alias + ".ready_file"] = str(ready)
                command = [paths["python"], "-I", "-B", "-m", "metaos_runtime", "manage", "--spec", paths["spec"],
                    "--root", paths["root"], "--workspace", str(output / "managed"), "--credentials", str(credentials), "--ready-file", str(ready)]
                process = start(alias, command, env)
                wait_ready(process, lambda: ready.is_file(), 120.)
                if read(output / "managed/instances/main/system.lock.json") != bundle["system_plan"]["lock"]:
                    raise RuntimeError("Started OS lock differs from the built composition")
                dynamic[alias + ".endpoint"] = read(ready)["endpoint"]
            elif kind == "service":
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    dynamic[alias + ".port"] = str(sock.getsockname()[1])
                context = context_for(bundle, alias, dynamic)
                dynamic[alias + ".endpoint"] = render(desc["endpoint"], context)
                process = start(alias, argv(desc["command"], context_for(bundle, alias, dynamic)), env)
                url = render(desc["health_url"], context_for(bundle, alias, dynamic))
                def healthy():
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=1.) as response:
                        return response.status == 200
                wait_ready(process, healthy, float(desc.get("startup_timeout_s", 300.)))
            else:
                process = start(alias, argv(desc["command"], context_for(bundle, alias, dynamic)), env)
                report["status"] = "running"
                write(output / "run.json", report)
                while process.poll() is None:
                    if time.monotonic() >= end:
                        raise TimeoutError("Experiment timed out")
                    if any(p.poll() is not None for n, p, _ in processes if n != alias):
                        raise RuntimeError("A required service exited during the experiment")
                    time.sleep(.05)
                report.update(status="application_completed" if process.returncode == 0 else "application_failed", application_exit_code=process.returncode)
    except KeyboardInterrupt:
        report.update(status="cancelled", error="Interrupted")
    except Exception as exc:
        report.update(status="infrastructure_error", error_type=type(exc).__name__, error=str(exc))
    finally:
        for alias, process, identity in reversed(processes):
            forced = False
            if process.poll() is None and identity and proc_identity(process.pid) == tuple(identity):
                # The Manager must first get the chance to stop its own Workers.
                try:
                    if bundle["components"][alias]["descriptor"]["kind"] == "system":
                        process.terminate()
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    kind = bundle["components"][alias]["descriptor"]["kind"]
                    process.wait(40 if kind in {"system", "application"} else 10)
                except subprocess.TimeoutExpired:
                    forced = True
                    if proc_identity(process.pid) == tuple(identity) and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(5)
            report["cleanup"].append({"alias": alias, "exit_code": process.poll(), "forced": forced})
        shutdown = output / "managed/shutdown.json"
        report["system_shutdown"] = read(shutdown) if shutdown.is_file() else None
        report["cleanup_confirmed"] = bool(report["system_shutdown"] and report["system_shutdown"].get("handles_closed")
            and report["system_shutdown"].get("active_leases") == 0 and all(not x["forced"] and x["exit_code"] is not None for x in report["cleanup"]))
        if not report["cleanup_confirmed"] and report["status"] == "application_completed":
            report["status"] = "cleanup_unconfirmed"
        for stream in streams:
            stream.close()
        write(output / "run.json", report)
    return {"status": report["status"], "output": str(output), "cleanup_confirmed": report["cleanup_confirmed"],
        "model_inference": "not_asserted"}


def init_pool(output, evidence):
    report = read(evidence)
    artifacts = report["artifacts"]
    project = Path(__file__).absolute().parents[1]
    output.mkdir(parents=True, exist_ok=False)
    def resource(name, desc):
        path = output / name
        path.mkdir()
        write(path / "resource.json", {"api": RESOURCE_API, **desc})
    def item(path, mode):
        return {"path": str(Path(path).absolute()), "mode": mode}
    mock = output / "mock"
    mock.mkdir()
    shutil.copyfile(project / "configs/managed-mock.yaml", mock / "system.yaml")
    write(mock / "resource.json", {"api": RESOURCE_API, "kind": "system", "provides": ["metaos.system/v1"],
        "paths": {"python": item(artifacts["producer_python"], "python"), "spec": {"path": "system.yaml", "mode": "file"},
            "root": {"path": ".", "mode": "directory"}}})
    resource("mock-check", {"kind": "application", "requires": {"system": "metaos.system/v1"},
        "paths": {"python": item(artifacts["producer_python"], "python"), "script": item(project / "scripts/exercise_p0.py", "file")},
        "command": ["{self.paths.python}", "-I", "-B", "{self.paths.script}", "--ready-file", "{system.ready_file}",
            "--credentials", "{run.credentials}", "--mode", "all", "--output", "{run.directory}/actions.json"]})
    deployment = Path(artifacts["system_deployment"])
    resource("libero", {"kind": "system", "provides": ["metaos.system/v1"], "paths": {
        "python": item(artifacts["os_python"], "python"), "spec": item(deployment / "system.json", "file"),
        "root": item(deployment / "project", "directory")}})
    app = Path(artifacts["harness_deployment"])
    resource("libero-assets", {"kind": "asset", "provides": ["libero.context/v1"], "paths": {
        "packages": item(app / "assets/packages", "tree"), "tokenizer": item(app / "assets/tokenizer.json", "file"),
        "task": item(app / "task.json", "file")}})
    resource("model-api", {"kind": "asset", "provides": ["model.chat/v1"],
        "outputs": {"model_name": "SET_ACTUAL_MODEL_NAME", "endpoint": "http://127.0.0.1:8000/v1"}})
    # Public model settings are part of the frozen descriptor; credentials only
    # enter the application environment and never the command or Bundle.
    resource("libero-harness", {"kind": "application", "requires": {
        "system": "metaos.system/v1", "assets": "libero.context/v1", "model": "model.chat/v1"},
        "env": ["OPENAI_API_KEY"], "paths": {"python": item(artifacts["harness_python"], "python"),
            "launcher": item(project / "scripts/local_libero_agent.py", "file")},
        "command": ["{self.paths.python}", "-I", "-B", "{self.paths.launcher}", "--endpoint", "{system.endpoint}",
            "--credentials", "{run.credentials}", "--model-name", "{model.outputs.model_name}", "--model-base-url", "{model.outputs.endpoint}", "--task", "{assets.paths.task}",
            "--packages", "{assets.paths.packages}", "--tokenizer", "{assets.paths.tokenizer}", "--store", "{run.store}"]})
    write(output / "mock.json", {"api": SPEC_API, "components": {"system": {"resource": "mock"},
        "harness": {"resource": "mock-check", "bindings": {"system": "system"}}}})
    write(output / "libero.json", {"api": SPEC_API, "components": {"system": {"resource": "libero"},
        "context": {"resource": "libero-assets"}, "model": {"resource": "model-api"},
        "harness": {"resource": "libero-harness", "bindings": {"system": "system", "assets": "context", "model": "model"}}}})
    return {"status": "initialized", "resources": str(output.absolute()), "specs": ["mock.json", "libero.json"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    init = commands.add_parser("init", help="Register this workspace's qualified resources; copy no images or weights")
    init.add_argument("--resources", type=Path, required=True)
    init.add_argument("--evidence", type=Path, default=Path(__file__).absolute().parents[1] / "evidence/p1p2-latest.json")
    installation = commands.add_parser("install", help="Install an existing local OS/Harness deployment offline")
    installation.add_argument("directory", type=Path)
    installation.add_argument("--target", type=Path, required=True)
    installation.add_argument("--python", type=Path, default=Path(sys.executable))
    installation.add_argument("--expect-fingerprint")
    installation.add_argument("--load-images", action="store_true")
    for name in ("plan", "build"):
        sub = commands.add_parser(name)
        sub.add_argument("spec", type=Path)
        sub.add_argument("--resources", type=Path, required=True)
        if name == "build":
            sub.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    launch = commands.add_parser("run")
    launch.add_argument("bundle", type=Path)
    launch.add_argument("--output", type=Path, required=True)
    launch.add_argument("--timeout", type=float, default=7200.)
    args = parser.parse_args()
    try:
        if args.operation == "init":
            result = init_pool(args.resources, args.evidence)
        elif args.operation == "install":
            result = install(args.directory, args.target, args.python, args.expect_fingerprint, args.load_images)
        elif args.operation == "plan":
            bundle = compose(args.spec, args.resources)
            validate_templates(bundle)
            result = {"status": "planned", "order": bundle["order"], "system_plan": system_plan(bundle), "opens_devices": False}
        elif args.operation == "build":
            result = build(args.spec, args.resources, args.output)
        elif args.operation == "verify":
            result = {"status": "verified", "fingerprint": verify(args.bundle)["fingerprint"]}
        else:
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            result = run(args.bundle, args.output, args.timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] not in {"application_failed", "infrastructure_error", "cancelled", "cleanup_unconfirmed"} else 1
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
