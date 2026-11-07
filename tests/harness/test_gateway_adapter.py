"""Installed Harness contract tests. Fixtures are not native/model qualification."""

import base64
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from contextlib import redirect_stdout
from unittest.mock import patch

from metaos_contracts import SystemError
from metaos_libero_harness import GatewayAdapter, main
from metaos_libero_harness.agent_service import model_overrides
from metaos_runtime.client import Client, OutcomeUnknown
from roborsi.evaluation.deadline import CaseDeadline
from roborsi.evaluation.media import CaseMediaChannel
from roborsi.evaluation.messages import Action, BenchmarkQuery, NativeResult, Observation, Provenance, Task
from roborsi.evaluation.runner import CaseRunner
from roborsi.evaluation.store import ResultStore

RESOURCE = "environments/libero-pro"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
TASK = Task(benchmark="libero_pro", task_id="fixture-only", seed=0, instruction="Contract fixture", max_steps=1)
CAPABILITIES = ("reset", "libero_action", "libero_query", "finish", "cancel_episode")


class FixtureClient:
    """In-memory transport fixture; downloads use the real checksum client."""
    timeout_s = .1
    reference = staticmethod(Client.reference)
    build_command = Client.build_command
    download = Client.download

    def __init__(self, directory):
        self.channel = CaseMediaChannel(directory / "remote/media")
        self.submissions, self.results, self.payloads, self.infos = [], {}, {}, []
        self.observation, self.episode, self.ended = None, None, False
        self.renews, self.releases, self.acquires, self.download_count = 0, 0, 0, 0
        self.lost_response, self.corrupt_bytes = False, False

    def resources(self):
        return [{"resource_id": RESOURCE, "metadata": {"tasks": [TASK.as_dict()], "native_image": "sha256:" + "0" * 64}, "capabilities": [
            {"id": "simulation." + name + "/v1", "frame_id": "libero_world_osc", "units": [], "max_command_validity_s": 300.}
            for name in CAPABILITIES]}]

    def info(self):
        return {"clock_id": "fixture-clock", "now_ns": time.monotonic_ns()}

    def state(self, resource):
        return {"sequence": len(self.submissions), "data": {"episode_id": self.episode,
            "observation": self.observation.as_dict() if self.observation else None}}

    def acquire(self, resource, ttl):
        self.acquires += 1
        return {"resource_id": resource, "lease_id": "fixture-lease", "control_epoch": 1,
            "expires_at_ns": time.monotonic_ns() + int(ttl * 1e9)}

    def renew(self, lease, ttl):
        self.renews += 1
        return {**lease, "expires_at_ns": time.monotonic_ns() + int(ttl * 1e9)}

    def release(self, lease):
        self.releases += 1

    def _artifact(self, ref, payload, kind, mime, step=None):
        info = {"schema_version": 1, "artifact_id": hashlib.sha256(ref.encode()).hexdigest(), "episode_id": self.episode,
            "ref": ref, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload), "kind": kind,
            "mime_type": mime, "step_index": step}
        self.infos.append(info)
        self.payloads[info["artifact_id"]] = payload
        return info

    def _observation(self, step):
        reference = self.channel.write(self.episode, step, PNG, "image/png")
        self._artifact(reference.ref, PNG, "image", "image/png", step)
        self.observation = Observation(benchmark="libero_pro", episode_id=self.episode, step_index=step, media=(reference,))
        return self.observation.as_dict()

    def submit(self, request):
        self.submissions.append(request)
        name, arguments = request["capability"], request["arguments"]
        if name == "simulation.reset/v1":
            self.episode = arguments["episode_id"]
            reply = {"task": TASK.as_dict(), "observation": self._observation(0)}
        elif name == "simulation.libero_action/v1":
            self.ended = True
            reply = {"observation": self._observation(1), "terminated": False, "truncated": True,
                "termination_reason": "fixture_budget"}
        elif name == "simulation.libero_query/v1":
            reply = {"benchmark": "libero_pro", "episode_id": self.episode, "step_index": self.observation.step_index,
                "query_id": arguments["query_id"], "data": {"fixture": True}}
        elif name == "simulation.finish/v1":
            payload = b'{"fixture_only":true,"score":0.0}'
            info = self._artifact("native/score.json", payload, "native_result", "application/json")
            reply = NativeResult(task=TASK, episode_id=self.episode, success=False, score=0.,
                source=info["ref"], source_sha256=info["sha256"]).as_dict()
        elif name == "simulation.cancel_episode/v1":
            self.ended, reply = True, {"stopped": True, "scored": False}
        else:
            raise AssertionError(name)
        self.results[request["command_id"]] = {"status": "succeeded", "result": {"state": {"data": {
            "ended": self.ended, "last_reply": {"payload": reply}}}}}
        if self.lost_response:
            self.lost_response = False
            raise OutcomeUnknown(request["command_id"])
        return {"status": "accepted"}

    def wait(self, command_id, timeout_s):
        return self.results[command_id]

    def artifacts(self, resource):
        return self.infos

    def request(self, method, path):
        query = parse_qs(urlsplit(path).query)
        info = next(row for row in self.infos if row["artifact_id"] == query["artifact_id"][0])
        self.download_count += 1
        offset, length = int(query["offset"][0]), int(query["length"][0])
        payload = self.payloads[info["artifact_id"]][offset:offset + length]
        if self.corrupt_bytes:
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        return {"artifact": info, "offset": offset, "data_base64": base64.b64encode(payload).decode()}


class FixtureAgent:
    def __init__(self, *, fail=False, delay=0.):
        self.fail, self.delay, self.closed = fail, delay, False

    def reset(self, task, episode_id, *, timeout_s):
        self.episode = episode_id

    def act(self, observation, *, timeout_s):
        time.sleep(self.delay)
        if self.fail:
            raise TimeoutError("fixture model wait exceeded its deadline")
        return BenchmarkQuery(benchmark=observation.benchmark, episode_id=observation.episode_id,
            step_index=observation.step_index, query_id="preserved-query-id", method="fixture")

    def resume(self, reply, *, timeout_s):
        if reply.query_id != "preserved-query-id":
            raise ValueError("Query ID changed")
        return Action(benchmark=reply.benchmark, episode_id=reply.episode_id, step_index=reply.step_index,
            data={"vector": [0., 0., 0., 0., 0., 0., -1.]})

    def close(self):
        self.closed = True


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = FixtureClient(self.root)
        self.adapter = GatewayAdapter(self.client, RESOURCE, evidence=self.root / "gateway.jsonl", lease_ttl_s=.8)
        self.addCleanup(self.adapter.close)

    def run_case(self, agent):
        store = ResultStore(self.root / "store")
        provenance = Provenance(agent_version="contract-fixture")
        directory = store.claim("run", "case", TASK, provenance, episode_id="episode")
        result = CaseRunner(store).run("run", "case", "episode", TASK, provenance, agent, self.adapter,
            timeout_s=5., deadline=CaseDeadline(10.), preclaimed=True, media_channel=CaseMediaChannel(directory / "media"))
        return result, directory

    def test_original_runner_publishes_verified_media_score_query_and_trace(self):
        agent = FixtureAgent()
        result, directory = self.run_case(agent)
        self.assertEqual(result.status, "task_failure")
        self.assertEqual(result.score, 0.)
        self.assertEqual(len(result.benchmark_artifacts), 3)
        self.assertTrue(agent.closed)
        self.assertEqual(self.client.releases, 1)
        self.assertTrue((directory / result.trace_manifest).is_file())
        self.assertTrue((directory / result.native_score_source).is_file())
        self.assertEqual(json.loads((directory / "result.json").read_text())["status"], "task_failure")

    def test_model_wait_keeps_the_same_lease_alive(self):
        result, _ = self.run_case(FixtureAgent(delay=1.4))
        self.assertEqual(result.status, "task_failure")
        self.assertGreaterEqual(self.client.renews, 3)
        self.assertEqual(self.client.acquires, 1)

    def test_model_timeout_is_unscored_and_cancels_before_release(self):
        result, _ = self.run_case(FixtureAgent(fail=True))
        self.assertEqual((result.status, result.error.code), ("agent_error", "timeout"))
        self.assertIsNone(result.score)
        self.assertIsNone(result.native_score_source)
        self.assertEqual(self.client.submissions[-1]["capability"], "simulation.cancel_episode/v1")
        self.assertEqual(self.client.releases, 1)
        self.assertFalse(any(row["capability"] == "simulation.finish/v1" for row in self.client.submissions))

    def test_lost_submit_response_queries_original_id_without_replay(self):
        self.client.lost_response = True
        self.adapter.start(TASK, "episode", timeout_s=5.)
        self.assertEqual(len(self.client.submissions), 1)
        self.assertEqual(self.adapter.command_ids, [self.client.submissions[0]["command_id"]])
        records = [json.loads(row) for row in (self.root / "gateway.jsonl").read_text().splitlines()]
        self.assertTrue(any(row["event"] == "submit_response_lost" and not row["replayed"] for row in records))

    def test_old_session_and_step_rejected_before_command_submission(self):
        self.adapter.start(TASK, "episode", timeout_s=5.)
        for episode, step in (("old", 0), ("episode", 1)):
            with self.subTest(episode=episode, step=step), self.assertRaises(ValueError):
                self.adapter.step(Action(benchmark="libero_pro", episode_id=episode, step_index=step, data={}), timeout_s=5.)
        self.assertEqual(len(self.client.submissions), 1)

    def test_media_identity_mismatch_rejected_before_download(self):
        start = self.adapter.start(TASK, "episode", timeout_s=5.)
        original = dict(self.client.infos[0])
        channel = CaseMediaChannel(self.root / "case/media")
        for key, value in (("episode_id", "old"), ("step_index", 8), ("sha256", "0" * 64),
                ("size_bytes", 0), ("mime_type", "image/jpeg"), ("kind", "file")):
            self.client.infos[0] = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.adapter.collect_observation(start.observation, channel, timeout_s=5.)
        self.client.infos[0] = original
        self.assertEqual(self.client.download_count, 0)

    def test_corrupt_artifact_bytes_leave_no_partial_published_file(self):
        start = self.adapter.start(TASK, "episode", timeout_s=5.)
        self.client.corrupt_bytes = True
        channel = CaseMediaChannel(self.root / "case/media")
        with self.assertRaises(SystemError) as caught:
            self.adapter.collect_observation(start.observation, channel, timeout_s=5.)
        self.assertEqual(caught.exception.code, "artifact_changed")
        self.assertFalse((self.root / "case" / start.observation.media[0].ref).exists())

    def test_local_media_cache_is_revalidated(self):
        start = self.adapter.start(TASK, "episode", timeout_s=5.)
        channel = CaseMediaChannel(self.root / "case/media")
        self.adapter.collect_observation(start.observation, channel, timeout_s=5.)
        path = self.root / "case" / start.observation.media[0].ref
        path.write_bytes(b"changed")
        self.adapter.collect_observation(start.observation, channel, timeout_s=5.)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(self.client.download_count, 2)

    def test_cli_requires_explicit_owned_model_configuration(self):
        with self.assertRaises(SystemExit) as caught:
            main(["--endpoint", "http://127.0.0.1:1", "--credentials", "missing", "--task-id", "fixture-only",
                "--agent-python", "/missing/python", "--packages", "missing", "--tokenizer", "missing",
                "--agent-version", "fixture", "--store", "missing"])
        self.assertEqual(caught.exception.code, 2)

    def cli_arguments(self):
        return ["--endpoint", "http://127.0.0.1:1", "--credentials", "fixture-unused", "--task-id", TASK.task_id,
            "--agent-version", "contract-fixture", "--store", str(self.root / "startup-store")]

    def assert_startup_failure(self, arguments):
        with patch("metaos_libero_harness.Client", return_value=self.client), redirect_stdout(io.StringIO()) as output:
            code = main(arguments)
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "infrastructure_error")
        self.assertIsNone(result["score"])
        self.assertEqual(result["error"]["details"]["stage"], "agent.start")
        paths = list((self.root / "startup-store").rglob("result.json"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(json.loads(paths[0].read_text()), result)
        self.assertTrue((paths[0].parent / result["trace_manifest"]).is_file())
        self.assertEqual(self.client.acquires, 0)

    def test_missing_owned_interpreter_publishes_startup_failure(self):
        self.assert_startup_failure([*self.cli_arguments(), "--agent-python", str(self.root / "missing-python"),
            "--packages", str(self.root), "--tokenizer", str(self.root / "unused-tokenizer"),
            "--model", "fixture", "--model-base-url", "http://127.0.0.1:9/v1"])

    def test_external_service_startup_failure_is_published(self):
        with patch("roborsi.evaluation.http_agent.HttpAgentLauncher") as launcher:
            launcher.return_value.start.side_effect = RuntimeError("fixture service never became ready")
            self.assert_startup_failure([*self.cli_arguments(), "--agent-url", "http://127.0.0.1:9"])

    def test_closed_adapter_cannot_start_another_session(self):
        self.adapter.start(TASK, "episode", timeout_s=5.)
        self.adapter.close()
        with self.assertRaises(ValueError):
            self.adapter.start(TASK, "second-session", timeout_s=5.)
        self.assertEqual(self.client.acquires, 1)

    def test_explicit_model_replaces_preset_and_does_not_record_keys(self):
        overrides = model_overrides("actual-27b", "http://127.0.0.1:8123/v1", ['agent.max_tokens=128000'])
        values = {key: json.loads(value) for item in overrides for key, value in [item.split("=", 1)]}
        self.assertEqual(values["runtime.llm.model"], "actual-27b")
        self.assertEqual(values["runtime.llm.base_url"], "http://127.0.0.1:8123/v1")
        for settings in (['runtime.llm.api_key="secret"'], ['runtime.llm={"api_key":"secret"}']):
            with self.assertRaises(ValueError):
                model_overrides("actual-27b", "http://127.0.0.1:8123/v1", settings)
        with self.assertRaises(ValueError):
            model_overrides("actual-27b", "http://user:secret@127.0.0.1:8123/v1")
