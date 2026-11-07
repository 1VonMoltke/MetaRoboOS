"""Clean owned native runtimes after a worker exits, without guessing ownership."""

import json
import os
import re
import subprocess
from pathlib import Path

from metaos_contracts import SystemError

from .process import atomic_json


def cleanup_native(directory):
    root, outcomes = Path(directory).resolve(), []
    for path in sorted(root.glob("runtime/components/*/native-*/native-runtime.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SystemError("native_identity_changed", "Native ownership journal is linked", 503)
        row = json.loads(path.read_text())
        name, owner, image = row.get("name", ""), row.get("owner", ""), row.get("image", "")
        if not (re.fullmatch(r"metaos-[a-z0-9-]+", name) and re.fullmatch(r"[a-f0-9]{32}", owner)
                and re.fullmatch(r"sha256:[a-f0-9]{64}", image)):
            raise SystemError("native_identity_changed", "Native ownership journal is invalid", 503)
        env = dict(os.environ)
        host = row.get("docker_host")
        if host:
            if not host.startswith("unix:///"):
                raise SystemError("native_identity_changed", "Native Docker transport is invalid", 503)
            env["DOCKER_HOST"] = host
        def docker(*args):
            result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise SystemError("native_cleanup_failed", "Owned native cleanup failed; keep resource quarantined", 503)
            return result.stdout
        ids = docker("ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$").strip()
        if ids:
            details = json.loads(docker("inspect", name))[0]
            if (details["Config"].get("Labels", {}).get("metaos.owner") != owner or details["Image"] != image
                    or row.get("container_id") not in {None, details["Id"]}):
                raise SystemError("native_identity_changed", "Refusing to remove a native container with different ownership", 503)
            docker("rm", "--force", details["Id"])
        row["cleanup"] = {"container_absent": True, "removed_after_worker_exit": bool(ids),
            "physical_stop_confirmation": False}
        atomic_json(path, row)
        (path.parent / "native.env").unlink(missing_ok=True)
        outcomes.append({"journal": str(path.relative_to(root)), **row["cleanup"]})
    return outcomes
