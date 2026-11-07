"""Build and run selected task campaigns without replacing the episode graph."""
import argparse
import json
from pathlib import Path
import signal
import tempfile

from metaos_harness.cli import build, verify
from metaos_harness.graph import Host, digest, resolve
from .panel import API, TASKS, select, validate


def count(value):
    if value == "all":
        return value
    number = int(value)
    if not 1 <= number <= 120:
        raise argparse.ArgumentTypeError("Choose 1..120 or all")
    return number


def specification(args):
    project = args.project_root.absolute()
    return {"api": "metaos.harness-spec/v1", "name": "roborsi-libero-campaign", "entrypoint": "campaign",
        "components": {"executor": {"component": "executor.roborsi_local", "config": {
            "bundle": str(args.episode_bundle.absolute()), "composer": str(project / "metaos-local"),
            "composer_script": str(project / "scripts/metaos_local.py"),
            "verifier": str(project / "scripts/verify_roborsi_run.py"), "timeout_s": args.timeout}},
            "campaign": {"component": "campaign.roborsi_libero", "bindings": {"executor": "executor"},
                "config": {"count": args.count, "selection": args.selection,
                    "selection_seed": args.selection_seed, "infra_retries": args.infra_retries}}}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    panel_parser = sub.add_parser("panel", help="Write a fixed allowed task panel; no model or simulator calls")
    panel_parser.add_argument("--count", type=count, default="all")
    panel_parser.add_argument("--seed", type=int, default=0)
    panel_parser.add_argument("--tool-budget", type=int, default=24)
    panel_parser.add_argument("--output", type=Path, required=True)
    build_parser = sub.add_parser("build", help="Build the two-component campaign graph; no episode execution")
    build_parser.add_argument("--episode-bundle", type=Path, required=True)
    build_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    build_parser.add_argument("--count", type=count, default=10)
    build_parser.add_argument("--selection", choices=["stratified", "first", "random"], default="stratified")
    build_parser.add_argument("--selection-seed", type=int, default=0)
    build_parser.add_argument("--infra-retries", type=int, default=1)
    build_parser.add_argument("--timeout", type=int, default=14400)
    build_parser.add_argument("--output", type=Path, required=True)
    for command in ["plan", "run"]:
        child = sub.add_parser(command)
        child.add_argument("--bundle", type=Path, required=True)
        if command == "run":
            child.add_argument("--output", type=Path, required=True)
            child.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "panel":
        panel = validate({"api": API, "tasks": select(TASKS, args.count), "seed": args.seed,
            "tool_budget": args.tool_budget, "atomic": "libero_pick_place",
            "instruction": "Complete the current runtime task with the released LIBERO skills."})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            json.dump(panel, stream, indent=2)
        result = {"panel": str(args.output), "tasks": len(panel["tasks"]), "seed": panel["seed"]}
    elif args.command == "build":
        spec = specification(args)
        with Host(resolve(spec)) as campaign:
            selection = campaign.plan()["tasks"]
        with tempfile.TemporaryDirectory(prefix="metaos-campaign-") as temporary:
            spec_path = Path(temporary) / "campaign.json"
            spec_path.write_text(json.dumps(spec))
            result = build(spec_path, args.output)
        result["selection"] = selection
    else:
        resolved, expected = verify(args.bundle)
        runtime = {"fingerprint": digest(expected)}
        if args.command == "run":
            runtime["run_dir"] = str(args.output.absolute())
            def interrupted(*_):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, interrupted)
        with Host(resolved, runtime, expected_lock=expected) as campaign:
            result = campaign.plan() if args.command == "plan" else campaign.run(resume=args.resume)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.command == "run":
        return 0 if result["status"] == "completed" else 130 if result["status"] == "cancelled" else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
