"""Evaluation is a client of the OS; the OS never imports this package."""

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

from metaos_contracts import SystemError
from metaos_runtime.client import Client, LeaseKeeper, OutcomeUnknown
from roborsi.evaluation.media import CaseMediaChannel
from roborsi.evaluation.messages import ArtifactInfo, NativeResult, QueryResult, StartResult, StepResult, Task


class GatewayAdapter:
    """CaseRunner-compatible adapter with no native write endpoint or mount."""

    def __init__(self, client, resource, *, evidence=None, lease_ttl_s=10.):
        self.client, self.resource = client, resource
        self.evidence = Path(evidence) if evidence else None
        self.lease_ttl_s = lease_ttl_s
        self.keeper = None
        self.episode_id = None
        self.command_ids = []
        self.downloads = {}
        self.downloads_complete = False
        self.ended = False

    def tasks(self, **kwargs):
        row = next(x for x in self.client.resources() if x["resource_id"] == self.resource)
        return [Task.from_dict(x) for x in row["metadata"]["tasks"]]

    def _event(self, event, **details):
        if self.evidence:
            self.evidence.parent.mkdir(parents=True, exist_ok=True)
            with self.evidence.open("a") as stream:
                stream.write(json.dumps({"event": event, "time_ns": time.time_ns(), "episode_id": self.episode_id, **details}) + "\n")

    def _execute(self, capability, arguments, timeout_s):
        if self.keeper is None:
            raise ValueError("Harness has no active control lease")
        capabilities = next(x for x in self.client.resources() if x["resource_id"] == self.resource)["capabilities"]
        limit = next(x for x in capabilities if x["id"] == capability)["max_command_validity_s"]
        timeout_s = min(float(timeout_s), limit - 1.)
        request = self.client.build_command(self.keeper.check(), capability, arguments, valid_for_s=timeout_s)
        self.command_ids.append(request["command_id"])
        self._event("command_submit", request=request)
        try:
            self.client.submit(request)
        except OutcomeUnknown:
            self._event("submit_response_lost", command_id=request["command_id"], replayed=False)
            # Query the original identity only. No network failure resubmits a
            # physical command or silently obtains a replacement lease.
        result = self.client.wait(request["command_id"], timeout_s=timeout_s + 5.)
        self._event("command_result", command_id=request["command_id"], status=result["status"], reason=result.get("reason"))
        if result["status"] != "succeeded":
            raise SystemError("native_execution_failed", "Gateway command did not succeed: " + result["status"], 503)
        data = result["result"]["state"]["data"]
        self.ended = data["ended"]
        return data["last_reply"]["payload"]

    def start(self, task, episode_id, *, timeout_s):
        if self.keeper is not None or self.episode_id is not None or task not in self.tasks():
            raise ValueError("Harness requires an unused adapter and an unchanged native task")
        keeper = LeaseKeeper(self.client, self.resource, self.lease_ttl_s)
        keeper.__enter__()
        self.keeper = keeper
        self.episode_id = episode_id
        reply = StartResult.from_dict(self._execute("simulation.reset/v1", {"task_id": task.task_id,
            "episode_id": episode_id}, timeout_s))
        if reply.task != task or reply.observation.episode_id != episode_id:
            raise ValueError("Native reset identity differs")
        return reply

    def step(self, action, *, timeout_s):
        self._identity(action)
        return StepResult.from_dict(self._execute("simulation.libero_action/v1", {"episode_id": self.episode_id,
            "action": action.data}, timeout_s))

    def query(self, request, *, timeout_s):
        self._identity(request)
        return QueryResult.from_dict(self._execute("simulation.libero_query/v1", {"episode_id": self.episode_id,
            "query_id": request.query_id, "method": request.method, "arguments": request.arguments}, timeout_s))

    def stop(self, request, *, timeout_s):
        self._identity(request)
        raise ValueError("The original LIBERO-Pro Adapter does not support policy-requested early stopping; operational cancellation is unscored")

    def finish(self, *, timeout_s):
        return NativeResult.from_dict(self._execute("simulation.finish/v1", {"episode_id": self.episode_id}, timeout_s))

    def _identity(self, request):
        state = self.client.state(self.resource)["data"]
        observation = state["observation"]
        if (request.benchmark != "libero_pro" or request.episode_id != self.episode_id
                or request.episode_id != state["episode_id"] or request.step_index != observation["step_index"]):
            raise ValueError("Agent request differs from current native observation/session")

    def collect_observation(self, observation, channel, *, timeout_s):
        infos = {row["ref"]: row for row in self.client.artifacts(self.resource)}
        for reference in observation.media:
            info = infos.get(reference.ref)
            if (info is None or info["kind"] != "image" or info["episode_id"] != observation.episode_id
                    or info["step_index"] != observation.step_index
                    or (info["sha256"], info["size_bytes"], info["mime_type"]) != (reference.sha256, reference.size_bytes, reference.mime_type)):
                raise ValueError("Observation media identity differs from gateway artifact metadata")
            self._download(info, channel.media_dir.parent)
        channel.verify_observation(observation)

    def _download(self, info, directory):
        if info["episode_id"] != self.episode_id:
            raise ValueError("Artifact belongs to another native session")
        path = Path(directory) / info["ref"]
        if not (self.downloads.get(info["ref"]) == info and path.exists()
                and hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]):
            self.client.download(self.resource, info, directory)
        self.downloads[info["ref"]] = info

    def collect_native(self, native, directory, *, timeout_s):
        if native.source is not None:
            matches = [x for x in self.client.artifacts(self.resource) if x["ref"] == native.source
                and x["sha256"] == native.source_sha256 and x["episode_id"] == native.episode_id]
            if len(matches) != 1:
                raise ValueError("Original native score evidence is missing")
            self._download(matches[0], directory)

    def collect_artifacts(self, directory, *, timeout_s):
        for info in self.client.artifacts(self.resource):
            self._download(info, directory)
        self.downloads_complete = True

    @property
    def artifact_records(self):
        return tuple(ArtifactInfo.from_dict(row) for row in self.downloads.values())

    def close(self):
        if self.keeper is None:
            return
        try:
            if self.episode_id and not self.ended:
                self._execute("simulation.cancel_episode/v1", {"episode_id": self.episode_id}, 5.)
        finally:
            keeper, self.keeper = self.keeper, None
            keeper.__exit__(None, None, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Original RoboRSI CaseRunner + external original Agent through the OS")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--caller", default="operator-a")
    parser.add_argument("--resource", default="environments/libero-pro")
    parser.add_argument("--task-id", required=True)
    agent_source = parser.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--agent-url", help="Existing original Agent RPC service, configured with the case media directory")
    agent_source.add_argument("--agent-python", type=Path, help="Launch the original Agent in this independent installed Python")
    parser.add_argument("--packages", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--model", help="Explicit served model identity for the owned original Agent")
    parser.add_argument("--model-base-url", help="Explicit model API base URL; credentials come from OPENAI_API_KEY")
    parser.add_argument("--agent-set", action="append", default=[], help="Non-secret original Agent key=JSON override")
    parser.add_argument("--agent-version", required=True, help="Verified original Agent/preset content identity")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--run-id", default="metaos-" + uuid.uuid4().hex[:12])
    parser.add_argument("--case-id", default="single-task")
    parser.add_argument("--episode-id", default="episode-" + uuid.uuid4().hex[:12])
    parser.add_argument("--case-timeout", type=float, default=1800.)
    parser.add_argument("--operation-timeout", type=float, default=290.)
    args = parser.parse_args(argv)
    if args.agent_python and not (args.packages and args.tokenizer):
        parser.error("--agent-python requires --packages and --tokenizer")
    if args.agent_python and not (args.model and args.model_base_url):
        parser.error("--agent-python requires --model and --model-base-url; no inherited preset model is selected implicitly")
    if args.agent_url and (args.model or args.model_base_url or args.agent_set or args.packages or args.tokenizer):
        parser.error("Original Agent launch settings require --agent-python")
    if not all(math.isfinite(value) and value > 0 for value in (args.case_timeout, args.operation_timeout)):
        parser.error("Timeouts must be finite and positive")
    if args.agent_python:
        from .agent_service import model_overrides
        model_overrides(args.model, args.model_base_url, args.agent_set)
    from roborsi.evaluation.deadline import CaseDeadline
    from roborsi.evaluation.http_agent import HttpAgentLauncher
    from roborsi.evaluation.messages import EvaluationResult, Provenance, ServiceError
    from roborsi.evaluation.runner import CaseRunner
    from roborsi.evaluation.store import ResultStore
    client = Client(args.endpoint, args.credentials, args.caller)
    adapter = GatewayAdapter(client, args.resource)
    task = next(row for row in adapter.tasks() if row.task_id == args.task_id)
    resource = next(x for x in client.resources() if x["resource_id"] == args.resource)
    store = ResultStore(args.store)
    provenance = Provenance(docker_image_digest=resource["metadata"]["native_image"],
        adapter_version="metaos-libero-device/0.1.0", agent_version=args.agent_version)
    directory = store.claim(args.run_id, args.case_id, task, provenance, episode_id=args.episode_id)
    adapter.evidence = directory / "gateway-commands.jsonl"
    channel = CaseMediaChannel(directory / "media")
    process, identity = None, None
    deadline = CaseDeadline(args.case_timeout)
    runner_started = False
    try:
        endpoint = args.agent_url
        if args.agent_python:
            from metaos_runtime.process import process_identity
            from roborsi.utils.daemon import pick_free_port
            port = pick_free_port()
            with (directory / "agent-service.log").open("w") as stream:
                # Keep the venv executable path: resolving its symlink selects
                # the base Python and discards the installed Agent environment.
                process = subprocess.Popen([str(args.agent_python.absolute()), "-I", "-B", "-m", "metaos_libero_harness.agent_service",
                    "--packages", str(args.packages.resolve()), "--tokenizer", str(args.tokenizer.resolve()),
                    "--case-dir", str(directory / "agent"), "--media-dir", str(channel.media_dir), "--port", str(port),
                    "--model", args.model, "--model-base-url", args.model_base_url,
                    *["--set=" + value for value in args.agent_set]],
                    stdout=stream, stderr=subprocess.STDOUT, cwd=directory, start_new_session=True)
            identity = process_identity(process.pid)
            endpoint = f"http://127.0.0.1:{port}"
        with HttpAgentLauncher(endpoint, startup_timeout_s=120.).start(directory, media_dir=channel.media_dir, deadline=deadline) as session:
            runner_started = True
            result = CaseRunner(store).run(args.run_id, args.case_id, args.episode_id, task, provenance,
                session.client, adapter, timeout_s=args.operation_timeout, preclaimed=True, media_channel=channel,
                deadline=deadline)
    except Exception as exc:
        if runner_started:
            raise
        failure = ServiceError(code="infrastructure_error", message="Original Agent service failed to start",
            details={"stage": "agent.start", "exception": type(exc).__name__})
        with store.execution(args.run_id, args.case_id, args.episode_id):
            trace = store.start_trace(args.run_id, args.case_id, args.episode_id)
            trace.record(failure, stage="agent.start")
            reference = trace.finish("infrastructure_error", failure)
            result = EvaluationResult(task=task, episode_id=args.episode_id, status="infrastructure_error",
                success=None, score=None, provenance=provenance, error=failure, trace_manifest=reference)
            store.publish(args.run_id, args.case_id, result)
    finally:
        try:
            adapter.close()
        finally:
            if process is not None:
                from metaos_runtime.process import terminate
                outcome = terminate(identity, 5.)
                process.wait(5)
                (directory / "agent-process.json").write_text(json.dumps({"identity": identity, "exit_code": process.returncode, **outcome}))
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.status in {"success", "task_failure"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
