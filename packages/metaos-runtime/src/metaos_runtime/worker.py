"""Independent execution authority. Manager loss revokes control without replay."""

import argparse
import hmac
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from metaos_contracts import CommandRequest, LeaseReference, LeaseRequest, SystemError

from .composition import load_spec, open_system, resolve
from .gateway import Gateway
from .process import atomic_json, process_identity


class WorkerClient:
    def __init__(self, directory, timeout=3.0, manager_id=None):
        self.directory = Path(directory)
        self.ready = json.loads((self.directory / "worker-ready.json").read_text())
        secret = self.directory / "worker-token.json"
        if secret.is_symlink() or secret.stat().st_mode & 0o077:
            raise SystemError("insecure_credentials", "Worker token must be owner-only", 400)
        self.token = json.loads(secret.read_text())["token"]
        self.timeout = timeout
        self.manager_id = manager_id

    def call(self, method, **arguments):
        if self.manager_id is not None:
            arguments.setdefault("manager_id", self.manager_id)
        request = Request(self.ready["endpoint"] + "/rpc", method="POST",
            data=json.dumps({"method": method, "arguments": arguments}, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            data = json.load(exc)["error"]
            raise SystemError(data["code"], data["message"], exc.code) from None
        except (OSError, ValueError):
            raise SystemError("worker_unavailable", "Execution worker did not confirm the operation; query its existing identity", 503) from None


class Authority:
    heartbeat_timeout_s = 1.5

    def __init__(self, gateway, stopping):
        self.gateway, self.stopping = gateway, stopping
        self.lock = threading.RLock()
        self.heartbeat_lock = threading.RLock()
        self.manager = None
        self.heartbeat_at = 0
        gateway.suspend("awaiting_manager")
        self.watchdog = threading.Thread(target=self.watch, name="metaos-manager-watchdog", daemon=True)
        self.watchdog.start()

    def watch(self):
        while not self.stopping.wait(0.02):
            lost = False
            with self.heartbeat_lock:
                if self.manager is not None and time.monotonic() - self.heartbeat_at > self.heartbeat_timeout_s:
                    self.manager = None
                    self.gateway.suspension_reason = "manager_lost"
                    self.gateway.suspended.set()
                    lost = True
            if lost:
                with self.lock:
                    # A new claim may have completed while this watchdog
                    # waited for an in-flight admission/release operation.
                    with self.heartbeat_lock:
                        if self.manager is not None:
                            continue
                    try:
                        self.gateway.suspend("manager_lost")
                    except SystemError as exc:
                        self.gateway.store.event("manager_loss_stop_unconfirmed", error_code=exc.code)

    def invoke(self, method, arguments):
        if method in {"acquire", "renew", "release", "submit", "cancel", "shutdown", "reconcile"}:
            with self.lock:
                with self.heartbeat_lock:
                    if self.manager is None or arguments.get("manager_id") != self.manager:
                        raise SystemError("stale_manager", "Execution authority belongs to another manager generation")
                return self._invoke(method, arguments)
        return self._invoke(method, arguments)

    def _invoke(self, method, arguments):
        gateway = self.gateway
        if method == "claim":
            with self.lock:
                gateway.suspend("manager_reconciliation")
                gateway.clock_id = arguments["clock_id"]
                with self.heartbeat_lock:
                    self.manager = arguments["manager_id"]
                    self.heartbeat_at = time.monotonic()
                gateway.resume()
                gateway.store.event("manager_claimed", manager_id=self.manager, clock_id=gateway.clock_id)
                return gateway.snapshot()
        if method == "heartbeat":
            with self.heartbeat_lock:
                if self.manager != arguments["manager_id"]:
                    raise SystemError("stale_manager", "Worker belongs to another manager generation")
                self.heartbeat_at = time.monotonic()
                return {"ok": True, "manager_id": self.manager}
        if method == "health":
            return gateway.snapshot()
        if method == "describe":
            return gateway.resources()
        if method == "artifacts":
            return gateway.artifacts(arguments["resource"])
        if method == "artifact_chunk":
            return gateway.artifact_chunk(arguments["resource"], arguments["artifact_id"], arguments["offset"], arguments["length"])
        if method == "history":
            return gateway.store.records()
        if method == "shutdown":
            result = gateway.suspend("instance_stopped")
            self.stopping.set()
            return result
        if method == "reconcile":
            return gateway.reconcile(arguments["resource"], arguments["acknowledge"])
        if method in {"acquire", "renew", "release", "submit"}:
            owner = arguments["owner"]
            if method == "acquire":
                return gateway.acquire(owner, LeaseRequest.model_validate_json(json.dumps(arguments["request"])))
            if method == "submit":
                return gateway.submit(owner, CommandRequest.model_validate_json(json.dumps(arguments["request"])))
            reference = LeaseReference.model_validate_json(json.dumps(arguments["reference"]))
            return gateway.renew(owner, reference, arguments["ttl_s"]) if method == "renew" else gateway.release(owner, reference)
        if method == "state":
            return gateway.state(arguments["resource"])
        if method in {"get", "cancel"}:
            return getattr(gateway, method)(arguments["owner"], arguments["command_id"])
        if method == "tasks":
            return gateway.tasks(arguments["owner"])
        raise SystemError("invalid_operation", "Worker RPC operation is not supported", 400)


def handler_for(authority, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path != "/rpc" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise SystemError("unauthenticated", "Private worker credential required", 401)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("Invalid request size")
                document = json.loads(self.rfile.read(size), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                if set(document) != {"method", "arguments"} or not isinstance(document["arguments"], dict):
                    raise ValueError("Invalid RPC document")
                result, status = authority.invoke(document["method"], document["arguments"]), 200
            except SystemError as exc:
                result, status = {"error": exc.as_dict()}, exc.status
            except (ValueError, TypeError, KeyError):
                result, status = {"error": {"code": "invalid_request", "message": "Invalid worker request"}}, 400
            except Exception:
                result, status = {"error": {"code": "worker_error", "message": "Execution worker operation failed"}}, 503
            payload = json.dumps(result, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory
    settings = json.loads((root / "worker-settings.json").read_text())
    atomic_json(root / "worker-process.json", {"identity": process_identity(), "instance_id": settings["instance_id"],
        "generation": settings["generation"]})
    token = json.loads((root / "worker-token.json").read_text())["token"]
    resolved = resolve(load_spec(root / "system.json"), settings["project_root"])
    if resolved.lock != json.loads((root / "system.lock.json").read_text()):
        raise SystemError("lock_changed", "Worker system differs from its reviewed content lock")
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    (root / "runtime").mkdir(exist_ok=True)
    ready = root / "worker-ready.json"
    try:
        with open_system(resolved, root / "runtime") as resources:
            if set(resources) != set(resolved.spec.resources.values()):
                raise SystemError("resource_identity_mismatch", "Worker exports differ from reserved resource identities")
            gateway = Gateway(resources, root / "runtime")
            authority = Authority(gateway, stopping)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(authority, token))
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            atomic_json(ready, {"endpoint": f"http://127.0.0.1:{server.server_port}", "identity": process_identity(),
                "instance_id": settings["instance_id"], "generation": settings["generation"], "fingerprint": resolved.fingerprint})
            try:
                stopping.wait()
            finally:
                gateway.suspended.set()
                server.shutdown()
                server.server_close()
                thread.join(2)
                authority.watchdog.join(2)
                gateway.close()
        atomic_json(root / "worker-shutdown.json", {"status": "closed", "identity": process_identity(), "handles_closed": True, "active_leases": 0})
    finally:
        ready.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
