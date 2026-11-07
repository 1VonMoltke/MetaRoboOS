"""Plan/build/verify/run a frozen, fine-grained Harness declaration."""
from pathlib import Path
import argparse
import json
import os
import shutil
import signal
import tempfile
import sys
import uuid

import yaml
from .graph import Host, canonical, digest, resolve


def read(path):
    return yaml.safe_load(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def build(spec_path, output):
    resolved = resolve(read(spec_path))
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".harness-", dir=output.parent) as temporary:
        root = Path(temporary)
        write(root / "harness.json", resolved.spec)
        write(root / "harness.lock.json", resolved.lock)
        write(root / "plan.json", {"api": "metaos.harness-plan/v1", "fingerprint": digest(resolved.lock),
            "order": resolved.order, "bindings": {k: v.get("bindings", {}) for k, v in resolved.spec["components"].items()},
            "placement": "one_harness_process_native_skills_in_os_owned_process",
            "plan_has_side_effects": False})
        shutil.copytree(root, output)
    return {"status": "built", "fingerprint": digest(resolved.lock), "components": resolved.order}


def verify(bundle):
    expected = read(bundle / "harness.lock.json")
    resolved = resolve(read(bundle / "harness.json"))
    if json.loads(canonical(resolved.lock)) != expected:
        raise ValueError("Frozen Harness lock differs from installed code/configuration")
    return resolved, expected


def run(bundle, experiment_path, endpoint, credentials, output):
    from jsonschema import Draft202012Validator
    experiment = read(experiment_path)
    Draft202012Validator({"type": "object", "additionalProperties": False,
        "properties": {"atomic": {"enum": ["libero_pick_place"]}, "sim_task": {"type": "string"},
            "seed": {"type": "integer", "minimum": 0}, "tool_budget": {"type": "integer", "minimum": 1, "maximum": 100},
            "instruction": {"type": "string"}}, "required": ["atomic", "sim_task", "seed", "tool_budget"]}).validate(experiment)
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    # Set before importing original roles/memory: no inherited shared home.
    os.environ["ROBORSI_HOME"] = str(output / "home")
    os.environ["ROBORSI_ARTIFACT_ROOT"] = str(output / "artifacts")
    os.environ["ROBORSI_RUN_MODE"] = "frozen"
    resolved, expected = verify(bundle)
    run_id = uuid.uuid4().hex
    write(output / "harness-identity.json", {"run_id": run_id, "fingerprint": digest(expected), "experiment": experiment})
    runtime = {"run_id": run_id, "run_dir": str(output), "endpoint": endpoint, "credentials": credentials}
    try:
        with Host(resolved, runtime, expected_lock=resolved.lock) as orchestrator:
            result = orchestrator.run(experiment)
        write(output / "harness-cleanup.json", {"closed": True, "lease_released": True})
        return {k: result[k] for k in ["status", "success", "outcome", "rounds", "tool_calls", "native"]}
    except BaseException as exc:
        write(output / "harness-error.json", {"status": "implementation_or_infrastructure_error", "error_type": type(exc).__name__,
            "error_code": getattr(exc, "code", None),
            "command_id": getattr(exc, "command_id", None),
            "cleanup_errors": getattr(exc, "cleanup_errors", []),
            "cause": None if exc.__cause__ is None else {
                "error_type": type(exc.__cause__).__name__,
                "error_code": getattr(exc.__cause__, "code", None)}})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("components")
    plan = sub.add_parser("plan")
    plan.add_argument("--spec", type=Path, required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--spec", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle", type=Path, required=True)
    run_parser = sub.add_parser("run")
    for name in ["bundle", "experiment", "endpoint", "credentials", "output"]:
        run_parser.add_argument("--" + name, required=True, type=Path if name in {"bundle", "experiment", "output"} else str)
    args = parser.parse_args(argv)
    if args.command == "components":
        from .graph import Registry
        registry = Registry()
        result = [registry.get(name).describe() for name in sorted(registry.entries)]
    elif args.command == "plan":
        resolved = resolve(read(args.spec))
        result = {"order": resolved.order, "plans": resolved.plans, "fingerprint": digest(resolved.lock)}
    elif args.command == "build":
        result = build(args.spec, args.output)
    elif args.command == "verify":
        resolved, _ = verify(args.bundle)
        result = {"verified": True, "fingerprint": digest(resolved.lock)}
    else:
        def interrupted(*args):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
        result = run(args.bundle, args.experiment, args.endpoint, args.credentials, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
