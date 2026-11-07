import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from metaos_runtime.client import Client
from metaos_runtime.process import alive, terminate
from metaos_runtime.server import create_credentials
from metaos_runtime.worker import WorkerClient

RESOURCE = "robots/mock-arm"
CAP = "manipulation.move_joint/v1"


def wait(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.03)
    raise TimeoutError("Expected condition did not become true")


def spec(resource=RESOURCE, config=None):
    return {"components": {"arm": {"component": "embodiment.mock_arm", "version": ">=0.1,<0.2",
        "config": {"resource_id": resource, **(config or {})}}}, "exports": ["arm"], "resources": {"arm": resource}}


class RunningManager:
    def __init__(self, name, config=None):
        parent = Path(os.environ.get("METAOS_TEST_ROOT", "/data3/xzx/MetaRoboOS/work/p1p2-development/tests"))
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=name + "-", dir=parent))
        self.workspace = self.root / "managed"
        self.spec = self.root / "system.json"
        self.spec.write_text(json.dumps(spec(config=config)))
        self.credentials = self.root / "credentials.json"
        create_credentials(self.credentials)
        self.ready_path = self.root / "ready.json"
        self.launches = []

    @property
    def worker(self):
        return WorkerClient(self.workspace / "instances/main")

    def start(self, resume=False):
        path = self.root / f"manager-{len(self.launches)}.log"
        with path.open("w") as stream:
            self.process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "metaos_runtime", "manage",
                "--spec", str(self.spec), "--root", str(self.root), "--workspace", str(self.workspace),
                "--credentials", str(self.credentials), "--ready-file", str(self.ready_path), *( ["--resume"] if resume else [])],
                cwd=self.root, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.launches.append(self.process)
        def ready():
            if self.process.poll() is not None:
                raise RuntimeError(path.read_text())
            try:
                value = json.loads(self.ready_path.read_text())
                return value if value["pid"] == self.process.pid else None
            except (OSError, ValueError):
                return None
        self.ready = wait(ready, 25)
        self.a = self.client("operator-a")
        self.b = self.client("operator-b")
        self.observer = self.client("observer")
        self.admin = self.client("administrator")
        return self

    def client(self, caller):
        return Client(self.ready["endpoint"], self.credentials, caller)

    def kill_manager(self):
        self.process.kill()
        self.process.wait(5)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(25)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
                raise RuntimeError("Manager did not stop within its shutdown budget")
        # Clean only identities created by this fixture, even after a failed test.
        for path in (self.workspace / "instances").glob("*/worker-process.json"):
            identity = json.loads(path.read_text())["identity"]
            if alive(identity):
                terminate(identity, 2)
        (self.root / "processes.json").write_text(json.dumps({"managers": [{"pid": p.pid, "exit_code": p.poll()} for p in self.launches]}))


def command(client, lease, duration=.2):
    return client.build_command(lease, CAP, {"positions": [1., -1.], "duration_s": duration}, valid_for_s=30)
