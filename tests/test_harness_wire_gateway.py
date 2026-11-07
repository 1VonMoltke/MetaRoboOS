"""Faults on command reads must not replay robot actions."""
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from metaos_harness.gateway import APIError, Client, LeaseKeeper, OutcomeUnknown


class WireGatewayTests(unittest.TestCase):
    def client(self, failures):
        client = Client.__new__(Client)
        client.calls = []
        lease = {"resource_id": "fixture", "lease_id": "lease", "control_epoch": 1}
        def request(method, path, body=None):
            client.calls.append((method, path, body))
            if path == "/v1/info":
                return {"clock_id": "clock", "now_ns": time.monotonic_ns()}
            if path.startswith("/v1/state?"):
                return {"sequence": 1}
            if path == "/v1/resources":
                return [{"resource_id": "fixture", "capabilities": [{"id": "move", "max_command_validity_s": 1,
                    "units": [], "frame_id": "world"}]}]
            if method == "POST":
                return {}
            if failures:
                raise failures.pop(0)
            return {"status": "succeeded"}
        client.request = request
        return client, SimpleNamespace(check=lambda: lease)

    def test_transient_reads_retry_same_id_without_resubmission(self):
        client, keeper = self.client([TimeoutError(), APIError("worker_unavailable", "fixture")])
        with patch("metaos_harness.gateway.time.sleep"):
            command_id, result = client.command(keeper, "move", {}, timeout=1)
        submits = [x for x in client.calls if x[0] == "POST"]
        reads = [x for x in client.calls if x[1].startswith("/v1/commands/")]
        self.assertEqual(len(submits), 1)
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(x[1].endswith(command_id) for x in reads))
        self.assertEqual(result["status"], "succeeded")

    def test_uncertain_submit_is_resolved_by_reading_existing_id(self):
        client, keeper = self.client([])
        original = client.request
        def request(method, path, body=None):
            result = original(method, path, body)
            if method == "POST":
                raise APIError("worker_unavailable", "fixture response lost")
            return result
        client.request = request
        command_id, _ = client.command(keeper, "move", {}, timeout=1)
        self.assertEqual(len([x for x in client.calls if x[0] == "POST"]), 1)
        self.assertEqual(client.calls[-1][1], "/v1/commands/" + command_id)

    def test_permanent_api_error_is_not_retried(self):
        client, keeper = self.client([APIError("forbidden", "fixture")])
        with self.assertRaises(APIError):
            client.command(keeper, "move", {}, timeout=1)
        self.assertEqual(len([x for x in client.calls if x[1].startswith("/v1/commands/")]), 1)

    def test_expired_poll_budget_does_not_replay(self):
        client, keeper = self.client([])
        with self.assertRaises(OutcomeUnknown) as caught:
            client.command(keeper, "move", {}, timeout=0)
        self.assertEqual([x for x in client.calls if x[0] == "POST"][0][2]["command_id"], caught.exception.command_id)

    def test_lease_failure_retains_original_cause(self):
        keeper = LeaseKeeper(None, "fixture")
        keeper.error = APIError("stale_lease", "fixture")
        with self.assertRaises(RuntimeError) as caught:
            keeper.check()
        self.assertIs(caught.exception.__cause__, keeper.error)
