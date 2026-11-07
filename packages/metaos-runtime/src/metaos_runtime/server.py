"""Local HTTP system API, authenticated before any state or control operation."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
from metaos_contracts import CommandRequest, LeaseReference, LeaseRequest, SystemError

from .composition import open_system, resolve
from .gateway import Gateway


def create_credentials(path):
    path = Path(path)
    document = {"schema_version": 1, "clients": [
        {"id": name, "token": secrets.token_urlsafe(32), "scopes": scopes, "resources": ["*"]}
        for name, scopes in (("operator-a", ["read", "control"]),
                             ("operator-b", ["read", "control"]), ("observer", ["read"]),
                             ("administrator", ["read", "control", "admin"]))]}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
    return {"credentials_file": str(path), "clients": [x["id"] for x in document["clients"]]}


def load_credentials(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise SystemError("insecure_credentials", "Credentials must be a regular owner-only file", 400)
    document = json.loads(path.read_text())
    rows = document.get("clients", [])
    if document.get("schema_version") != 1 or not rows or len({r["id"] for r in rows}) != len(rows):
        raise SystemError("invalid_credentials", "Invalid client identity configuration", 400)
    if any(len(r.get("token", "")) < 32 or set(r.get("scopes", [])) - {"read", "control", "admin"}
           or not r.get("resources") for r in rows):
        raise SystemError("invalid_credentials", "Invalid client grants", 400)
    if len({r["token"] for r in rows}) != len(rows):
        raise SystemError("invalid_credentials", "Each caller requires a distinct credential", 400)
    return rows


def handler_for(gateway, credentials):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3.0)

        def log_message(self, *args):
            pass  # Never echo authorization headers, request data, or raw errors.

        def identity(self):
            value = self.headers.get("Authorization", "")
            token = value[7:] if value.startswith("Bearer ") else ""
            caller = next((c for c in credentials if hmac.compare_digest(c["token"], token)), None)
            if caller is None:
                raise SystemError("unauthenticated", "A valid client credential is required", 401)
            return caller

        def permitted(self, caller, scope, resource=None):
            if scope not in caller["scopes"] or resource and "*" not in caller["resources"] and resource not in caller["resources"]:
                raise SystemError("forbidden", "Caller is not authorized for this resource or operation", 403)

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 65536:
                raise SystemError("invalid_request", "Expected a bounded JSON request", 400)
            def invalid_constant(_):
                raise ValueError("Nonfinite JSON")
            return json.loads(self.rfile.read(size), parse_constant=invalid_constant)

        def route(self):
            caller = self.identity()
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path
            if self.command == "GET":
                self.permitted(caller, "read")
                if path == "/v1/info":
                    return gateway.info()
                if path == "/v1/health":
                    return gateway.health() if hasattr(gateway, "health") else {"status": "ready", **gateway.info()}
                if path == "/v1/instances" and hasattr(gateway, "instances"):
                    rows = gateway.instances()
                    return [row for row in rows if "*" in caller["resources"] or
                            any(r in caller["resources"] for r in row["resource_ids"])]
                if path == "/v1/tasks":
                    return [row for row in gateway.tasks(caller["id"]) if "*" in caller["resources"] or row["resource_id"] in caller["resources"]]
                if path == "/v1/resources":
                    return [r for r in gateway.resources()
                            if "*" in caller["resources"] or r["resource_id"] in caller["resources"]]
                if path == "/v1/state":
                    resource = query.get("resource_id", [""])[0]
                    self.permitted(caller, "read", resource)
                    return gateway.state(resource)
                if path in {"/v1/artifacts", "/v1/artifact-chunk"}:
                    resource = query.get("resource_id", [""])[0]
                    self.permitted(caller, "read", resource)
                    if path == "/v1/artifacts":
                        return gateway.artifacts(resource)
                    return gateway.artifact_chunk(resource, query.get("artifact_id", [""])[0],
                        int(query.get("offset", ["0"])[0]), int(query.get("length", ["1048576"])[0]))
                if path.startswith(("/v1/commands/", "/v1/tasks/")):
                    record = gateway.get(caller["id"], path.rsplit("/", 1)[1])
                    self.permitted(caller, "read", record["resource_id"])
                    return record
            if self.command == "POST":
                if path == "/v1/instances" or path.startswith("/v1/instances/"):
                    self.permitted(caller, "admin")
                    if not hasattr(gateway, "instances"):
                        raise SystemError("unsupported_backend", "Instance management requires the supervised backend", 400)
                    body = self.body()
                    if path == "/v1/instances":
                        from metaos_contracts import SystemSpec
                        if set(body) != {"id", "spec"}:
                            raise ValueError("Expected id and SystemSpec")
                        return gateway.register(body["id"], SystemSpec.model_validate_json(json.dumps(body["spec"])))
                    parts = path.strip("/").split("/")
                    if len(parts) != 4:
                        raise ValueError("Expected instance lifecycle operation")
                    return gateway.control_instance(parts[2], parts[3], body)
                self.permitted(caller, "control")
                body = self.body()
                if path == "/v1/leases/acquire":
                    request = LeaseRequest.model_validate_json(json.dumps(body))
                    self.permitted(caller, "control", request.resource_id)
                    return gateway.acquire(caller["id"], request)
                if path in {"/v1/leases/release", "/v1/leases/renew"}:
                    ttl = None
                    if path.endswith("renew"):
                        body = dict(body)
                        ttl = body.pop("ttl_s")
                        LeaseRequest.model_validate_json(json.dumps({"resource_id": body.get("resource_id"), "ttl_s": ttl}))
                    reference = LeaseReference.model_validate_json(json.dumps(body))
                    self.permitted(caller, "control", reference.resource_id)
                    if ttl is None:
                        return gateway.release(caller["id"], reference)
                    return gateway.renew(caller["id"], reference, ttl)
                if path in {"/v1/commands", "/v1/tasks"}:
                    request = CommandRequest.model_validate_json(json.dumps(body, allow_nan=False))
                    self.permitted(caller, "control", request.resource_id)
                    return gateway.submit(caller["id"], request)
                if path.startswith(("/v1/commands/", "/v1/tasks/")) and path.endswith("/cancel"):
                    if body:
                        raise SystemError("invalid_request", "Cancel request body must be empty", 400)
                    command_id = path.strip("/").split("/")[2]
                    record = gateway.get(caller["id"], command_id)
                    self.permitted(caller, "control", record["resource_id"])
                    return gateway.cancel(caller["id"], command_id)
            raise SystemError("not_found", "System API route does not exist", 404)

        def dispatch(self):
            try:
                document, status = self.route(), 200
            except SystemError as exc:
                document, status = {"error": exc.as_dict()}, exc.status
            except (ValueError, KeyError, TypeError, ValidationError):
                document, status = {"error": {"code": "invalid_request", "message": "Invalid system request"}}, 400
            except Exception:
                document, status = {"error": {"code": "internal_error", "message": "System operation failed"}}, 500
            payload = json.dumps(document, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Lost responses do not cause device command retries.

        do_GET = do_POST = dispatch

    return Handler


def serve(spec, *, root, workspace, credentials, ready_file, lock=None, port=0):
    resolved = resolve(spec, root)
    if lock is not None and json.loads(Path(lock).read_text()) != resolved.lock:
        raise SystemError("lock_changed", "System differs from the reviewed content lock")
    callers = load_credentials(credentials)
    ready_file = Path(ready_file)
    if ready_file.exists() or ready_file.is_symlink():
        raise SystemError("ready_file_exists", "Ready file must be a new path", 400)
    workspace = Path(workspace).absolute()
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "system.lock.json").write_text(json.dumps(resolved.lock, indent=2) + "\n")
    stopping = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopping.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    ready_owned = False
    try:
        with open_system(resolved, workspace) as resources:
            gateway = Gateway(resources, workspace)
            server = None
            worker = None
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(gateway, callers))
                # Drain bounded in-flight HTTP requests before closing the DB.
                server.daemon_threads = False
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                worker.start()
                ready = {"endpoint": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(),
                         "workspace": str(workspace), "fingerprint": resolved.fingerprint, **gateway.info()}
                with ready_file.open("x") as stream:
                    ready_owned = True
                    stream.write(json.dumps(ready, indent=2) + "\n")
                print(json.dumps(ready), flush=True)
                stopping.wait()
            finally:
                gateway.closing.set()
                if server is not None:
                    server.shutdown()
                    server.server_close()
                if worker is not None:
                    worker.join(2.0)
                gateway.close()
        (workspace / "shutdown.json").write_text(json.dumps({"status": "closed", "handles_closed": True,
                                                           "active_leases": 0, "pid": os.getpid()}) + "\n")
    finally:
        if ready_owned:
            ready_file.unlink(missing_ok=True)
        for sig, callback in previous.items():
            signal.signal(sig, callback)


def manage(*, spec_path, root, workspace, credentials, ready_file, resume=False, instance_id="main", port=0):
    from .composition import load_spec
    from .manager import Manager
    from .process import WorkspaceLock, alive, atomic_json, process_identity
    workspace = Path(workspace).absolute()
    if resume:
        if not (workspace / "manager-config.json").is_file():
            raise SystemError("workspace_not_found", "Resume requires an existing managed workspace", 400)
    else:
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    callers = load_credentials(credentials)
    with WorkspaceLock(workspace):
        if resume:
            settings = json.loads((workspace / "manager-config.json").read_text())
            project_root = Path(settings["project_root"])
            if root is not None and Path(root).resolve() != project_root:
                raise SystemError("root_changed", "Resume must use the original reviewed project root")
        else:
            if spec_path is None:
                raise SystemError("spec_required", "A new manager requires --spec", 400)
            project_root = Path(root or Path.cwd()).resolve()
            atomic_json(workspace / "manager-config.json", {"project_root": str(project_root), "api": "metaos.manager/v1"})
        ready_file = Path(ready_file).absolute()
        if ready_file.exists() or ready_file.is_symlink():
            try:
                stale = json.loads(ready_file.read_text())
            except (OSError, ValueError):
                raise SystemError("ready_file_exists", "Refusing to overwrite an unrelated ready file") from None
            old_process = json.loads((workspace / "manager-process.json").read_text()) if (workspace / "manager-process.json").exists() else {}
            if not resume or stale.get("manager_id") != old_process.get("manager_id") or stale.get("workspace") != str(workspace) or alive(old_process.get("identity")):
                raise SystemError("ready_file_exists", "Refusing to overwrite an unrelated or live ready file")
            ready_file.unlink()
        manager = Manager(workspace, project_root)
        stopping = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stopping.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        server, worker, owned = None, None, False
        try:
            if resume:
                manager.recover()
            else:
                manager.register(instance_id, load_spec(spec_path))
            atomic_json(workspace / "manager-process.json", {"identity": process_identity(), "manager_id": manager.id})
            server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(manager, callers))
            server.daemon_threads = False
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            worker.start()
            ready = {"endpoint": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(), "identity": process_identity(),
                "workspace": str(workspace), **manager.info()}
            atomic_json(ready_file, ready)
            owned = True
            print(json.dumps(ready), flush=True)
            stopping.wait()
        finally:
            manager.closing.set()
            if server is not None:
                server.shutdown()
                server.server_close()
            if worker is not None:
                worker.join(2)
            outcomes = manager.close()
            atomic_json(workspace / "shutdown.json", {"status": "closed", "instances": outcomes,
                "handles_closed": all(row["actual"] == "stopped" for row in outcomes), "active_leases": 0})
            if owned:
                ready_file.unlink(missing_ok=True)
            for sig, callback in previous.items():
                signal.signal(sig, callback)
