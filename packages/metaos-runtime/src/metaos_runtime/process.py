"""Linux process identity, private state files and single-owner workspace locks."""

import fcntl
import json
import os
import signal
import tempfile
import time
from pathlib import Path

from metaos_contracts import SystemError


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        # comm may contain whitespace and parentheses; parse after its last ')'.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return {"pid": pid, "start_ticks": fields[19], "boot_id": boot}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(identity):
    return bool(identity) and process_identity(identity["pid"]) == identity


def terminate(identity, timeout=5.0):
    if not alive(identity):
        return {"already_exited": True, "forced": False}
    os.kill(identity["pid"], signal.SIGTERM)
    end = time.monotonic() + timeout
    while alive(identity) and time.monotonic() < end:
        time.sleep(0.02)
    forced = alive(identity)
    if forced:
        # Workers start their own session. Never target a reused PID or group.
        if os.getpgid(identity["pid"]) != identity["pid"]:
            raise SystemError("process_identity_changed", "Refusing to signal an unowned process group", 503)
        os.killpg(identity["pid"], signal.SIGKILL)
    return {"already_exited": False, "forced": forced}


class WorkspaceLock:
    def __init__(self, root):
        self.path = Path(root) / "manager.lock"

    def __enter__(self):
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise SystemError("workspace_busy", "Another manager owns this workspace") from None
        self.stream.seek(0)
        self.stream.truncate()
        json.dump(process_identity(), self.stream)
        self.stream.flush()
        return self

    def __exit__(self, *args):
        fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()
