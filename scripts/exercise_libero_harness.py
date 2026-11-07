#!/usr/bin/env python3
"""Real native CaseRunner/Harness qualification with deterministic policy, no model."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from metaos_libero_harness import GatewayAdapter
from metaos_runtime.client import Client
from metaos_runtime.process import alive
from metaos_runtime.server import create_credentials
from roborsi.evaluation.deadline import CaseDeadline
from roborsi.evaluation.media import CaseMediaChannel
from roborsi.evaluation.messages import Action, Provenance
from roborsi.evaluation.runner import CaseRunner
from roborsi.evaluation.store import ResultStore


class DeterministicPolicy:
    """Only exercises the actual transport/runner, never claims model behavior."""
    def reset(self, task, episode_id, *, timeout_s):
        self.task, self.episode, self.used = task, episode_id, 0

    def act(self, observation, *, timeout_s):
        count = min(100, self.task.native["max_env_steps"] - self.used)
        if count <= 0:
            raise AssertionError("Original physical-step budget exhausted without native terminal result")
        self.used += count
        return Action(benchmark=self.task.benchmark, episode_id=self.episode, step_index=observation.step_index,
            data={"vectors": [[0., 0., 0., 0., 0., 0., -1.]] * count})

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("system-python", "spec", "root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    credentials, ready = output / "credentials.json", output / "ready.json"
    create_credentials(credentials)
    report = {"status": "running", "model_inference": False, "scope": "actual_native_gateway_and_original_case_runner",
        "policy": "deterministic_zero_osc_full_native_budget"}
    process = None
    try:
        argv = [str(args.system_python.absolute()), "-I", "-B", "-m", "metaos_runtime", "manage", "--spec", str(args.spec.resolve()),
            "--root", str(args.root.resolve()), "--workspace", str(output / "managed"), "--credentials", str(credentials), "--ready-file", str(ready)]
        with (output / "manager.log").open("w") as stream:
            process = subprocess.Popen(argv, cwd=output, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + 90
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError("Manager exited before readiness")
            if time.monotonic() >= end:
                raise TimeoutError("Manager did not become ready")
            time.sleep(.05)
        client = Client(json.loads(ready.read_text())["endpoint"], credentials)
        resource = next(row for row in client.resources() if row["kind"] == "libero_pro_native")
        adapter = GatewayAdapter(client, resource["resource_id"], evidence=output / "gateway-commands.jsonl")
        task = adapter.tasks()[0]
        provenance = Provenance(docker_image_digest=resource["metadata"]["native_image"],
            adapter_version="metaos-libero-device/0.1.0", agent_version="deterministic-qualification-no-model")
        store = ResultStore(output / "results")
        episode = "harness-qualification-" + uuid.uuid4().hex[:12]
        directory = store.claim("native-harness", "single-task", task, provenance, episode_id=episode)
        result = CaseRunner(store).run("native-harness", "single-task", episode, task, provenance, DeterministicPolicy(), adapter,
            timeout_s=290., preclaimed=True, deadline=CaseDeadline(1800.), media_channel=CaseMediaChannel(directory / "media"))
        report.update(result=result.as_dict(), case_directory=str(directory), command_count=len(adapter.command_ids))
        if result.status not in {"success", "task_failure"}:
            raise AssertionError("Original Runner classified an operational error: " + result.status)
        if not result.native_score_source or not result.trace_manifest:
            raise AssertionError("Original score or Runner trace is missing")
        if not any(row.mime_type == "video/mp4" for row in result.benchmark_artifacts):
            raise AssertionError("Native MP4 was not published by the Harness")
        for row in result.benchmark_artifacts:
            if hashlib.sha256((directory / row.ref).read_bytes()).hexdigest() != row.sha256:
                raise AssertionError("Harness artifact bytes differ")
        report.update(status="passed", native_budget=task.native["max_env_steps"], original_scoring_preserved=True)
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
            if alive(json.loads(marker.read_text())["identity"]):
                report.update(status="failed", cleanup_error="worker_survived_shutdown")
        shutdown = output / "managed/shutdown.json"
        if shutdown.exists():
            report["shutdown"] = json.loads(shutdown.read_text())
            if not report["shutdown"]["handles_closed"]:
                report.update(status="failed", cleanup_error="handles_not_closed")
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "model_inference": False, "report": str(output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
