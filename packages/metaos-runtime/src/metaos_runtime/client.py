"""Small system API client. A lost submit response is never automatically retried."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import tempfile
import time
import uuid
from threading import Event, Thread
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from metaos_contracts import SystemError, TERMINAL


class OutcomeUnknown(SystemError):
    def __init__(self, command_id):
        super().__init__("outcome_unknown", "Submit response was lost; query this command id before retrying", 503)
        self.command_id = command_id


class Client:
    def __init__(self, endpoint, credentials, caller="operator-a", timeout_s=3.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("P0 only supports local HTTP origins")
        self.endpoint = endpoint.rstrip("/")
        document = json.loads(Path(credentials).read_text())
        self.token = next(row["token"] for row in document["clients"] if row["id"] == caller)
        self.timeout_s = timeout_s

    def request(self, method, path, body=None, *, timeout_s=None):
        payload = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = Request(self.endpoint + path, data=payload, method=method,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s if timeout_s is None else timeout_s) as response:
                return json.load(response)
        except HTTPError as exc:
            error = json.load(exc)["error"]
            raise SystemError(error["code"], error["message"], exc.code) from None

    def info(self):
        return self.request("GET", "/v1/info")

    def resources(self):
        return self.request("GET", "/v1/resources")

    def instances(self):
        return self.request("GET", "/v1/instances")

    def tasks(self):
        return self.request("GET", "/v1/tasks")

    def health(self):
        return self.request("GET", "/v1/health")

    def create_instance(self, instance_id, spec):
        return self.request("POST", "/v1/instances", {"id": instance_id, "spec": spec}, timeout_s=100)

    def instance(self, instance_id, operation, *, acknowledge=False):
        return self.request("POST", f"/v1/instances/{instance_id}/{operation}", {"acknowledge": acknowledge}, timeout_s=100)

    def state(self, resource_id):
        return self.request("GET", "/v1/state?" + urlencode({"resource_id": resource_id}))

    def artifacts(self, resource_id):
        return self.request("GET", "/v1/artifacts?" + urlencode({"resource_id": resource_id}))

    def download(self, resource_id, info, directory):
        relative = PurePosixPath(info["ref"])
        root = Path(directory).resolve()
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in info["ref"]:
            raise SystemError("invalid_artifact", "Artifact reference is unsafe", 503)
        path = root / str(relative)
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise SystemError("invalid_artifact", "Artifact destination escapes its directory", 503)
        size = info["size_bytes"]
        if type(size) is not int or not 0 <= size <= 256 * 1024 * 1024:
            raise SystemError("artifact_too_large", "Artifact exceeds supported size", 503)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
        digest, offset = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as stream:
                while offset < size:
                    chunk = self.request("GET", "/v1/artifact-chunk?" + urlencode({"resource_id": resource_id,
                        "artifact_id": info["artifact_id"], "offset": offset, "length": min(1048576, size-offset)}))
                    data = base64.b64decode(chunk["data_base64"], validate=True)
                    if chunk["artifact"] != info or chunk["offset"] != offset or not 0 < len(data) <= min(1048576, size-offset):
                        raise SystemError("artifact_changed", "Artifact identity or range changed during download", 503)
                    stream.write(data)
                    digest.update(data)
                    offset += len(data)
                if digest.hexdigest() != info["sha256"]:
                    raise SystemError("artifact_changed", "Artifact checksum differs", 503)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def acquire(self, resource_id, ttl_s=5.0):
        return self.request("POST", "/v1/leases/acquire", {"resource_id": resource_id, "ttl_s": ttl_s})

    @staticmethod
    def reference(lease):
        return {key: lease[key] for key in ("resource_id", "lease_id", "control_epoch")}

    def renew(self, lease, ttl_s=5.0):
        return self.request("POST", "/v1/leases/renew", {**self.reference(lease), "ttl_s": ttl_s})

    def release(self, lease):
        return self.request("POST", "/v1/leases/release", self.reference(lease))

    def build_command(self, lease, capability, arguments, *, valid_for_s=5.0, command_id=None):
        info = self.info()
        state = self.state(lease["resource_id"])
        resource = next(row for row in self.resources() if row["resource_id"] == lease["resource_id"])
        cap = next(row for row in resource["capabilities"] if row["id"] == capability)
        return {**self.reference(lease), "command_id": command_id or uuid.uuid4().hex,
                "capability": capability, "arguments": arguments, "clock_id": info["clock_id"],
                "not_after_ns": info["now_ns"] + int(valid_for_s * 1e9),
                "state_sequence": state["sequence"], "frame_id": cap["frame_id"], "units": cap["units"]}

    def submit(self, command):
        try:
            return self.request("POST", "/v1/commands", command)
        except (OSError, URLError, TimeoutError, ConnectionError, ValueError):
            raise OutcomeUnknown(command["command_id"]) from None

    def result(self, command_id):
        return self.request("GET", "/v1/commands/" + command_id)

    def cancel(self, command_id):
        return self.request("POST", "/v1/commands/" + command_id + "/cancel", {})

    def wait(self, command_id, timeout_s=5.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            result = self.result(command_id)
            if result["status"] in TERMINAL:
                return result
            time.sleep(0.05)
        raise TimeoutError("Command has no terminal result; query its existing id")


class LeaseKeeper:
    """Renew independently of model latency. Loss is observable; never reacquire."""
    def __init__(self, client, resource, ttl_s=10.0):
        self.client, self.resource, self.ttl = client, resource, ttl_s
        self.stopping = Event()
        self.error = None

    def __enter__(self):
        self.lease = self.client.acquire(self.resource, self.ttl)
        def keep():
            while not self.stopping.wait(min(self.ttl / 4, .5)):
                try:
                    self.lease = self.client.renew(self.lease, self.ttl)
                except Exception as exc:
                    self.error = exc
                    return
        self.thread = Thread(target=keep, name="metaos-lease-keeper", daemon=True)
        self.thread.start()
        return self

    def check(self):
        if self.error or time.monotonic_ns() >= self.lease["expires_at_ns"]:
            raise SystemError("lease_lost", "Control lease renewal failed; do not submit further actions", 503) from self.error
        return self.lease

    def __exit__(self, *args):
        self.stopping.set()
        self.thread.join(self.client.timeout_s + 1)
        try:
            self.client.release(self.lease)
        except SystemError as exc:
            if exc.code not in {"stale_lease", "resource_fault", "instance_unavailable", "worker_unavailable"}:
                raise
