"""Namespace-independent client for the existing metaos.system/v1 API.

The old runtime client imports the old roborsi SDK through metaos_contracts;
this wire client keeps the new RoboRSI environment independent of that package.
"""
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener
import json
import hashlib
import time
import uuid


class OutcomeUnknown(RuntimeError):
    def __init__(self, command_id):
        self.command_id = command_id
        super().__init__("Query the existing command; do not replay: " + command_id)


class APIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(code + ": " + message)


class Client:
    def __init__(self, endpoint, credentials, caller="operator-a"):
        self.endpoint = endpoint.rstrip("/")
        path = Path(credentials)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Credentials must be an owner-only regular file")
        self.token = next(x["token"] for x in json.loads(path.read_text())["clients"] if x["id"] == caller)

    def request(self, method, path, body=None):
        request = Request(self.endpoint + path, method=method,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            error = json.load(exc)["error"]
            raise APIError(error["code"], error["message"]) from None

    def state(self, resource):
        return self.request("GET", "/v1/state?" + urlencode({"resource_id": resource}))

    @staticmethod
    def reference(lease):
        return {key: lease[key] for key in ("resource_id", "lease_id", "control_epoch")}

    def command(self, keeper, capability, arguments, timeout=650):
        lease = keeper.check()
        resource = lease["resource_id"]
        info = self.request("GET", "/v1/info")
        state = self.state(resource)
        description = next(x for x in self.request("GET", "/v1/resources") if x["resource_id"] == resource)
        cap = next(x for x in description["capabilities"] if x["id"] == capability)
        command_id = uuid.uuid4().hex
        command = {**self.reference(lease), "command_id": command_id, "capability": capability,
            "arguments": arguments, "clock_id": info["clock_id"],
            "not_after_ns": info["now_ns"] + int(min(timeout, cap["max_command_validity_s"]) * 1e9),
            "state_sequence": state["sequence"], "frame_id": cap["frame_id"], "units": cap["units"]}
        uncertain = False
        try:
            self.request("POST", "/v1/commands", command)
        except APIError as exc:
            if exc.code != "worker_unavailable":
                raise
            # Manager RPC may time out after the worker accepted the action.
            uncertain = True
        except (OSError, URLError, ValueError):
            # Submission may have reached the worker. Resolve this ID only.
            uncertain = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                keeper.check()
            except Exception as exc:
                exc.command_id = command_id
                raise
            try:
                result = self.request("GET", "/v1/commands/" + command_id)
            except APIError as exc:
                if exc.code == "worker_unavailable" or (uncertain and exc.code == "command_not_found"):
                    time.sleep(.1)
                    continue
                exc.command_id = command_id
                raise
            except (OSError, URLError):
                # A read is safe to retry. Never submit a second action.
                time.sleep(.1)
                continue
            if result["status"] == "succeeded":
                result["_request_sha256"] = hashlib.sha256(json.dumps(command, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                return command_id, result
            if result["status"] in {"failed", "cancelled", "outcome_unknown"}:
                error = RuntimeError(f"Command {command_id}: {result['status']}: {result.get('reason')}")
                error.command_id = command_id
                error.code = result.get("reason")
                raise error
            time.sleep(.04)
        raise OutcomeUnknown(command_id)


class LeaseKeeper:
    def __init__(self, client, resource, ttl=15.):
        self.client, self.resource, self.ttl = client, resource, ttl
        self.stopping, self.error = Event(), None

    def __enter__(self):
        self.lease = self.client.request("POST", "/v1/leases/acquire", {"resource_id": self.resource, "ttl_s": self.ttl})
        def keep():
            while not self.stopping.wait(.5):
                try:
                    self.lease = self.client.request("POST", "/v1/leases/renew", {**self.client.reference(self.lease), "ttl_s": self.ttl})
                except Exception as exc:
                    self.error = exc
                    return
        self.thread = Thread(target=keep, daemon=True, name="roborsi-lease-keeper")
        self.thread.start()
        return self

    def check(self):
        if self.error or time.monotonic_ns() >= self.lease["expires_at_ns"]:
            raise RuntimeError("Control lease lost; no reacquisition or action replay") from self.error
        return self.lease

    def __exit__(self, *args):
        self.stopping.set()
        self.thread.join(2)
        self.client.request("POST", "/v1/leases/release", self.client.reference(self.lease))
