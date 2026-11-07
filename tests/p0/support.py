"""Actual isolated gateway process per scenario; retain logs for acceptance."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from metaos_runtime.client import Client
from metaos_runtime.server import create_credentials

RESOURCE = "robots/mock-arm"
CAPABILITY = "manipulation.move_joint/v1"


def directory(name):
    root = Path(os.environ.get("METAOS_TEST_ROOT", tempfile.gettempdir()))
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=name + "-", dir=root))


def default_spec(config=None):
    return {"schema_version": 1, "components": {"arm": {"component": "embodiment.mock_arm",
            "version": ">=0.1,<0.2", "config": {"resource_id": RESOURCE, **(config or {})}}}, "exports": ["arm"]}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise TimeoutError("Expected condition was not observed")


class RunningSystem:
    def __init__(self, name="gateway", *, config=None, spec=None):
        self.root = directory(name)
        self.spec_path = self.root / "system.json"
        self.spec_path.write_text(json.dumps(spec or default_spec(config), indent=2))
        self.credentials = self.root / "credentials.json"
        create_credentials(self.credentials)
        self.ready_path = self.root / "ready.json"
        self.workspace = self.root / "runtime"

    def start(self):
        self.log = (self.root / "server.log").open("w")
        self.process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "metaos_runtime", "serve",
            "--spec", str(self.spec_path), "--root", str(self.root), "--workspace", str(self.workspace),
            "--credentials", str(self.credentials), "--ready-file", str(self.ready_path)],
            cwd=self.root, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("Gateway exited before ready: " + (self.root / "server.log").read_text())
            try:
                return json.loads(self.ready_path.read_text())
            except (OSError, ValueError):
                return None
        try:
            self.ready = wait_for(ready, 10.0)
        except Exception:
            self.stop(require_clean=False)
            raise
        self.a = self.client("operator-a")
        self.b = self.client("operator-b")
        self.observer = self.client("observer")
        return self

    def client(self, caller):
        return Client(self.ready["endpoint"], self.credentials, caller)

    def stop(self, require_clean=True):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(3)
            raise RuntimeError("Gateway did not shut down cooperatively")
        finally:
            self.log.close()
            (self.root / "process.json").write_text(json.dumps({"pid": self.process.pid,
                "exit_code": self.process.returncode, "workspace": str(self.workspace)}, indent=2))
        if require_clean:
            if self.process.returncode != 0:
                raise RuntimeError("Gateway failed: " + (self.root / "server.log").read_text())
            closed = json.loads((self.workspace / "shutdown.json").read_text())
            if closed["active_leases"] != 0 or not closed["handles_closed"] or self.ready_path.exists():
                raise RuntimeError("Gateway cleanup evidence is incomplete")


def motion(client, lease, duration=0.1, **options):
    return client.build_command(lease, CAPABILITY, {"positions": [1.0, -1.0], "duration_s": duration}, **options)
