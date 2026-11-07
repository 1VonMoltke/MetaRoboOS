#!/usr/bin/env python3
"""Real native LIBERO qualification through the installed supervised system API."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from metaos_contracts import SystemError
from metaos_runtime.client import Client, LeaseKeeper
from metaos_runtime.process import alive
from metaos_runtime.server import create_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    credentials, ready = output / "credentials.json", output / "ready.json"
    create_credentials(credentials)
    argv = [sys.executable, "-I", "-B", "-m", "metaos_runtime", "manage", "--spec", str(args.spec.resolve()),
        "--root", str(args.root.resolve()), "--workspace", str(output / "managed"), "--credentials", str(credentials), "--ready-file", str(ready)]
    checks, commands = [], []
    report = {"status": "running", "mode": "deterministic_native_qualification", "model_inference": False,
        "argv": argv, "checks": checks, "commands": commands}
    process = None
    try:
        with (output / "manager.log").open("w") as stream:
            process = subprocess.Popen(argv, cwd=output, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + 90
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError("Manager exited; inspect manager/worker logs")
            if time.monotonic() >= end:
                raise TimeoutError("Manager did not become ready")
            time.sleep(.05)
        info = json.loads(ready.read_text())
        client = Client(info["endpoint"], credentials)
        resource = next(row for row in client.resources() if row["kind"] == "libero_pro_native")
        rid = resource["resource_id"]
        task = resource["metadata"]["tasks"][0]
        episode = "metaos-native-" + uuid.uuid4().hex[:12]
        report.update(task=task, episode_id=episode, native_image=resource["metadata"]["native_image"])
        checks.append({"name": "native_resource_and_process", "status": "passed", "worker": client.instances()[0]["identity"]})
        with LeaseKeeper(client, rid, ttl_s=10.) as keeper:
            def execute(capability, arguments):
                request = client.build_command(keeper.check(), capability, arguments, valid_for_s=290.)
                client.submit(request)
                result = client.wait(request["command_id"], timeout_s=295.)
                commands.append({"request": request, "result": result})
                if result["status"] != "succeeded":
                    raise AssertionError("Native command failed: " + result["status"] + "/" + str(result.get("reason")))
                return result["result"]["state"]["data"]["last_reply"]["payload"]
            start = execute("simulation.reset/v1", {"task_id": task["task_id"], "episode_id": episode})
            assert start["task"] == task and start["observation"]["episode_id"] == episode
            assert start["observation"]["media"], "Real camera images are required"
            checks.append({"name": "native_reset_and_camera_observation", "status": "passed"})
            good = client.build_command(keeper.check(), "simulation.libero_action/v1", {"episode_id": episode, "action": {"vector": [0., 0., 0., 0., 0., 0., -1.]}}, valid_for_s=59.)
            def rejected(name, expected, changes):
                request = {**good, "command_id": uuid.uuid4().hex, **changes}
                try:
                    client.submit(request)
                except SystemError as exc:
                    assert exc.code == expected, (name, exc.code, expected)
                else:
                    raise AssertionError("Unsafe native request was accepted: " + name)
                checks.append({"name": name, "status": "passed", "error_code": expected})
            rejected("wrong_action_dimension", "invalid_action", {"arguments": {"episode_id": episode, "action": {"vector": [0., 0.]}}})
            rejected("wrong_units", "semantic_mismatch", {"units": ["rad", "rad"]})
            rejected("wrong_frame", "semantic_mismatch", {"frame_id": "mock_joint"})
            rejected("stale_episode", "stale_episode", {"arguments": {**good["arguments"], "episode_id": "old-session"}})
            rejected("old_state_sequence", "state_changed", {"state_sequence": good["state_sequence"] - 1})
            step = execute("simulation.libero_action/v1", good["arguments"])
            assert step["observation"]["step_index"] > start["observation"]["step_index"]
            checks.append({"name": "native_7d_osc_action", "status": "passed"})
            # This native adapter has no policy-requested zero-step stop. Run
            # its original 2000-step protocol, never substitute cancellation
            # or a reduced budget for the original native score.
            while not (step.get("terminated") or step.get("truncated")):
                used = step["native"]["native_env_steps"]
                remaining = task["native"]["max_env_steps"] - used
                assert remaining > 0
                step = execute("simulation.libero_action/v1", {"episode_id": episode,
                    "action": {"vectors": [[0., 0., 0., 0., 0., 0., -1.]] * min(100, remaining)}})
            native = execute("simulation.finish/v1", {"episode_id": episode})
            assert native["task"] == task and native["episode_id"] == episode
            artifacts = client.artifacts(rid)
            for item in artifacts:
                assert item["episode_id"] == episode
                path = client.download(rid, item, output / "client-artifacts")
                assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
            assert any(row["ref"] == native["source"] and row["sha256"] == native["source_sha256"] for row in artifacts)
            assert any(row["mime_type"] == "video/mp4" for row in artifacts)
            report.update(native_result=native, artifacts=artifacts, client_tasks=client.tasks())
            checks.append({"name": "original_scoring_video_trace_and_verified_api_downloads", "status": "passed",
                "score": native["score"], "artifacts": len(artifacts)})
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
                report.update(status="failed", cleanup_error="manager_shutdown_timeout")
            report["manager_exit_code"] = process.returncode
        for marker in (output / "managed/instances").glob("*/worker-process.json"):
            identity = json.loads(marker.read_text())["identity"]
            if alive(identity):
                report.update(status="failed", cleanup_error="worker_survived_shutdown")
        shutdown = output / "managed/shutdown.json"
        if shutdown.exists():
            report["shutdown"] = json.loads(shutdown.read_text())
            if not report["shutdown"]["handles_closed"] or report["shutdown"]["active_leases"]:
                report.update(status="failed", cleanup_error="handles_or_leases_not_closed")
        elif process is not None:
            report.update(status="failed", cleanup_error="manager_shutdown_evidence_missing")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "report": str(output / "report.json"), "checks": len(checks)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
