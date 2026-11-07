#!/usr/bin/env python3
"""Verify the installed CLI's original-Agent failure and real native cleanup.

Use a closed local port or a deliberately blocked local HTTP fixture to
exercise connection failure, Agent exit or model timeout. No model inference
is performed and the unavailable remote 27B service is never contacted.
"""

import argparse
import http.server
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from metaos_contracts import SystemError
from metaos_runtime.client import Client
from metaos_runtime.process import alive, process_identity, terminate
from metaos_runtime.server import create_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("system-python", "harness-python", "harness-bundle", "spec", "root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--failure-mode", choices=("connection", "agent_exit", "model_timeout"), default="connection")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    credentials, ready = output / "credentials.json", output / "ready.json"
    create_credentials(credentials)
    report = {"status": "running", "model_inference": False, "scope": "original_agent_unavailable_local_model_failure",
        "remote_model_contacted": False, "failure_mode": args.failure_mode}
    process = harness_process = model_server = model_thread = None
    model_requested, model_release = threading.Event(), threading.Event()
    def owned_agents():
        if harness_process is None or harness_process.poll() is not None:
            return []
        path = Path(f"/proc/{harness_process.pid}/task/{harness_process.pid}/children")
        result = []
        try:
            children = path.read_text().split()
        except FileNotFoundError:
            return result
        for value in children:
            try:
                if b"metaos_libero_harness.agent_service" in Path(f"/proc/{value}/cmdline").read_bytes():
                    identity = process_identity(int(value))
                    if identity:
                        result.append(identity)
            except FileNotFoundError:
                pass
        return result
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
        endpoint = json.loads(ready.read_text())["endpoint"]
        client = Client(endpoint, credentials)
        resource = next(row for row in client.resources() if row["kind"] == "libero_pro_native")
        bundle = args.harness_bundle.resolve()
        env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        env["OPENAI_API_KEY"] = "local-unavailable-probe-only"
        model_url = "http://127.0.0.1:9/v1"
        if args.failure_mode != "connection":
            class BlockedModel(http.server.BaseHTTPRequestHandler):
                def log_message(self, *unused):
                    pass
                def do_POST(self):
                    try:
                        self.connection.settimeout(15.)
                        length = int(self.headers["Content-Length"])
                        if not 0 < length <= 32 * 1024 * 1024:
                            raise ValueError("Unexpected model request size")
                        body = json.loads(self.rfile.read(length))
                        report["local_model_request"] = {"path": self.path, "model": body["model"]}
                        model_requested.set()
                        model_release.wait(45.)
                        payload = b'{"error":{"message":"Controlled failure probe; no model inference","type":"probe"}}'
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    except (OSError, ValueError):
                        pass
            model_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BlockedModel)
            model_server.daemon_threads = True
            model_thread = threading.Thread(target=model_server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            model_thread.start()
            model_url = f"http://127.0.0.1:{model_server.server_port}/v1"
        argv = [str(args.harness_python.absolute().parent / "metaos-libero-harness"), "--endpoint", endpoint,
            "--credentials", str(credentials), "--task-id", resource["metadata"]["tasks"][0]["task_id"],
            "--agent-python", str(args.harness_python.absolute()), "--packages", str(bundle / "assets/packages"),
            "--tokenizer", str(bundle / "assets/tokenizer.json"), "--model", "unavailable-local-probe",
            "--model-base-url", model_url, "--agent-set", "runtime.llm.retry.max_retries=0",
            "--agent-set", "agent.action_timeout_s=10" if args.failure_mode == "model_timeout" else "agent.action_timeout_s=30",
            "--agent-set", "agent.planner_timeout_s=2" if args.failure_mode == "model_timeout" else "agent.planner_timeout_s=30",
            "--agent-version", "original-27b-unavailable-model-probe", "--case-timeout", "180", "--operation-timeout", "290",
            "--store", str(output / "results")]
        with (output / "harness.log").open("w") as stream:
            harness_process = subprocess.Popen(argv, cwd=output, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            if args.failure_mode != "connection":
                end = time.monotonic() + 100.
                while not model_requested.wait(.1):
                    if harness_process.poll() is not None or time.monotonic() >= end:
                        raise AssertionError("Original Agent never reached the controlled local model service")
                if args.failure_mode == "agent_exit":
                    agents = owned_agents()
                    if len(agents) != 1 or not alive(agents[0]):
                        raise AssertionError("Cannot identify the Agent child owned by this CLI")
                    report["killed_agent"] = agents[0]
                    os.kill(agents[0]["pid"], signal.SIGKILL)
            harness_process.wait(240)
        results = list((output / "results").rglob("result.json"))
        if len(results) != 1:
            raise AssertionError("CLI did not publish exactly one classified result")
        outcome = json.loads(results[0].read_text())
        report.update(cli_exit_code=harness_process.returncode, result=outcome, case_directory=str(results[0].parent))
        expected = "infrastructure_error" if args.failure_mode == "agent_exit" else "agent_error"
        if harness_process.returncode != 1 or outcome["status"] != expected or outcome["score"] is not None:
            raise AssertionError("The controlled model/Agent fault did not become the expected unscored failure")
        if args.failure_mode == "model_timeout" and "time" not in outcome["error"]["message"].lower():
            raise AssertionError("Original Agent did not report a timeout")
        directory = results[0].parent
        commands = [json.loads(line) for line in (directory / "gateway-commands.jsonl").read_text().splitlines()]
        capabilities = [row["request"]["capability"] for row in commands if row["event"] == "command_submit"]
        if capabilities != ["simulation.reset/v1", "simulation.cancel_episode/v1"]:
            raise AssertionError("Failure cleanup executed unexpected native actions or scoring")
        agent_process = json.loads((directory / "agent-process.json").read_text())
        if alive(agent_process["identity"]):
            raise AssertionError("Original Agent process survived CLI cleanup")
        runtime = json.loads((directory / "agent/agent-runtime.json").read_text())
        if Path(runtime["prefix"]).absolute() != args.harness_python.absolute().parent.parent:
            raise AssertionError("CLI launched the base Python instead of the requested Harness venv")
        report["agent_runtime"] = runtime
        state = client.state(resource["resource_id"])
        if not state["data"]["ended"] or state["data"]["moving"]:
            raise AssertionError("Native cancellation did not stop the episode")
        replacement = Client(endpoint, credentials, "operator-b")
        lease = replacement.acquire(resource["resource_id"], 10.)
        try:
            stale = replacement.build_command(lease, "simulation.finish/v1", {"episode_id": state["data"]["episode_id"]})
            if stale["state_sequence"] <= 0:
                raise AssertionError("Native reset/cancellation did not advance the state identity")
            stale["state_sequence"] -= 1
            try:
                replacement.submit(stale)
            except SystemError as exc:
                if exc.code != "state_changed":
                    raise
                report["past_native_state_rejected"] = {"request": stale, "current_sequence": state["sequence"], "error_code": exc.code}
            else:
                raise AssertionError("A past native state was accepted")
        finally:
            replacement.release(lease)
        report.update(status="passed", capabilities=capabilities, agent_process_closed=True, replacement_caller_acquired=True)
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for identity in owned_agents():
            terminate(identity, 2.)
        if harness_process is not None and harness_process.poll() is None:
            harness_process.terminate()
            try:
                harness_process.wait(5)
            except subprocess.TimeoutExpired:
                harness_process.kill()
                harness_process.wait(5)
        model_release.set()
        if model_server is not None:
            model_server.shutdown()
            model_server.server_close()
            model_thread.join(2)
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
