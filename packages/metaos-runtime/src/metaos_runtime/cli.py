"""Thin CLI over generic graph, system API and offline bundle operations."""

import argparse
import json
import sys
from pathlib import Path

from metaos_contracts import SystemError

from .composition import Registry, load_spec, resolve


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plugins")
    credentials = commands.add_parser("credentials")
    credentials.add_argument("--output", type=Path, required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--root", type=Path, default=Path.cwd())
    plan.add_argument("--lock", type=Path)
    serve = commands.add_parser("serve")
    serve.add_argument("--spec", type=Path, required=True)
    serve.add_argument("--root", type=Path, default=Path.cwd())
    serve.add_argument("--lock", type=Path)
    serve.add_argument("--workspace", type=Path, required=True)
    serve.add_argument("--credentials", type=Path, required=True)
    serve.add_argument("--ready-file", type=Path, required=True)
    serve.add_argument("--port", type=int, default=0)
    managed = commands.add_parser("manage")
    managed.add_argument("--spec", type=Path)
    managed.add_argument("--root", type=Path)
    managed.add_argument("--workspace", type=Path, required=True)
    managed.add_argument("--credentials", type=Path, required=True)
    managed.add_argument("--ready-file", type=Path, required=True)
    managed.add_argument("--instance-id", default="main")
    managed.add_argument("--resume", action="store_true")
    managed.add_argument("--port", type=int, default=0)
    bundle = commands.add_parser("bundle")
    sub = bundle.add_subparsers(dest="operation", required=True)
    export = sub.add_parser("export")
    export.add_argument("--spec", type=Path, required=True)
    export.add_argument("--root", type=Path, default=Path.cwd())
    export.add_argument("--wheelhouse", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--images", choices=["reference", "archive"], default="reference")
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--installed", action="store_true")
    verify.add_argument("--expect-fingerprint")
    verify.add_argument("--native", action="store_true")
    client = commands.add_parser("client")
    client.add_argument("--endpoint", required=True)
    client.add_argument("--credentials", type=Path, required=True)
    client.add_argument("--caller", default="operator-a")
    operations = client.add_subparsers(dest="operation", required=True)
    operations.add_parser("info")
    operations.add_parser("resources")
    for name in ("instances", "tasks", "health"):
        operations.add_parser(name)
    instance = operations.add_parser("instance")
    instance.add_argument("instance_operation", choices=["start", "stop", "restart", "reconcile"])
    instance.add_argument("instance_id")
    instance.add_argument("--acknowledge", action="store_true")
    state = operations.add_parser("state")
    state.add_argument("resource")
    acquire = operations.add_parser("acquire")
    acquire.add_argument("resource")
    acquire.add_argument("--ttl", type=float, default=10.0)
    for name in ("release", "renew"):
        p = operations.add_parser(name)
        p.add_argument("--lease", type=Path, required=True)
        if name == "renew":
            p.add_argument("--ttl", type=float, default=10.0)
    submit = operations.add_parser("submit")
    submit.add_argument("--request", type=Path, required=True)
    for name in ("result", "cancel", "wait"):
        p = operations.add_parser(name)
        p.add_argument("command_id")
    try:
        args = parser.parse_args(argv)
        if args.command == "plugins":
            result = Registry().list()
        elif args.command == "credentials":
            from .server import create_credentials
            result = create_credentials(args.output)
        elif args.command == "plan":
            resolved = resolve(load_spec(args.spec), args.root)
            if args.lock:
                with args.lock.open("x") as stream:
                    stream.write(json.dumps(resolved.lock, indent=2) + "\n")
            result = {"fingerprint": resolved.fingerprint, "order": resolved.order, "lock": resolved.lock}
        elif args.command == "manage":
            from .server import manage
            manage(spec_path=args.spec, root=args.root, workspace=args.workspace, credentials=args.credentials,
                ready_file=args.ready_file, resume=args.resume, instance_id=args.instance_id, port=args.port)
            return 0
        elif args.command == "serve":
            from .server import serve
            serve(load_spec(args.spec), root=args.root, workspace=args.workspace, credentials=args.credentials,
                  ready_file=args.ready_file, lock=args.lock, port=args.port)
            return 0
        elif args.command == "bundle":
            from .bundle import export, verify
            result = (export(args.spec, args.root, args.wheelhouse, args.output, args.images) if args.operation == "export"
                      else verify(args.bundle, args.installed, args.expect_fingerprint, args.native))
            result = {k: result[k] for k in ("fingerprint", "system_fingerprint", "packages")}
        else:
            from .client import Client
            client = Client(args.endpoint, args.credentials, args.caller)
            if args.operation in {"info", "resources", "instances", "tasks", "health"}:
                result = getattr(client, args.operation)()
            elif args.operation == "instance":
                result = client.instance(args.instance_id, args.instance_operation, acknowledge=args.acknowledge)
            elif args.operation == "state":
                result = client.state(args.resource)
            elif args.operation == "acquire":
                result = client.acquire(args.resource, args.ttl)
            elif args.operation in {"renew", "release"}:
                lease = json.loads(args.lease.read_text())
                result = client.renew(lease, args.ttl) if args.operation == "renew" else client.release(lease)
            elif args.operation == "submit":
                result = client.submit(json.loads(args.request.read_text()))
            else:
                result = getattr(client, args.operation)(args.command_id)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except SystemError as exc:
        print(json.dumps({"error": exc.as_dict(), **({"command_id": exc.command_id} if hasattr(exc, "command_id") else {})}), file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError, KeyError, StopIteration) as exc:
        print(json.dumps({"error": {"code": "invalid_operation", "message": "Operation failed; check inputs and installed packages"}}), file=sys.stderr)
        return 2
