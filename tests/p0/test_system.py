"""Exercise actual HTTP clients, independent processes, and physical-style faults."""

import copy
import json
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

from metaos_contracts import SystemError
from metaos_runtime.client import Client, OutcomeUnknown

from support import CAPABILITY, RESOURCE, RunningSystem, motion, wait_for


class GatewayTests(unittest.TestCase):
    def system(self, **options):
        system = RunningSystem(self._testMethodName, **options).start()
        self.addCleanup(system.stop)
        return system

    def error(self, code, operation):
        with self.assertRaises(SystemError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_complete_motion_and_native_state(self):
        s = self.system()
        self.assertEqual(s.a.resources()[0]["resource_id"], RESOURCE)
        lease = s.a.acquire(RESOURCE, 3.0)
        command = motion(s.a, lease)
        accepted = s.a.submit(command)
        self.assertIn(accepted["status"], {"accepted", "executing"})
        result = s.a.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded")
        state = s.a.state(RESOURCE)
        self.assertEqual(state["data"]["positions"], [1.0, -1.0])
        self.assertFalse(state["data"]["moving"])
        self.assertEqual(state["data"]["execution_count"], 1)
        self.assertTrue(s.a.release(lease)["released"])

    def test_concurrent_control_and_authenticated_owner(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 3.0)
        self.error("resource_busy", lambda: s.b.acquire(RESOURCE, 1.0))
        self.assertEqual(self.error("forbidden", lambda: s.b.submit(motion(s.b, lease))).status, 403)
        self.error("forbidden", lambda: s.observer.acquire(RESOURCE, 1.0))
        self.assertFalse(s.observer.state(RESOURCE)["data"]["moving"])
        forged = motion(s.a, lease)
        forged["owner"] = "operator-b"
        self.error("invalid_request", lambda: s.a.submit(forged))
        s.a.release(lease)

    def test_simultaneous_acquire_has_exactly_one_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        s = self.system()
        barrier = threading.Barrier(2)
        def acquire(client):
            barrier.wait(3)
            try:
                return client, client.acquire(RESOURCE, 4.0)
            except SystemError as exc:
                return client, exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, [s.a, s.b]))
        winners = [(client, grant) for client, grant in results if isinstance(grant, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual([grant for _, grant in results if isinstance(grant, str)], ["resource_busy"])
        winners[0][0].release(winners[0][1])

    def test_unauthenticated_requests_and_resource_acl(self):
        s = RunningSystem(self._testMethodName)
        document = json.loads(s.credentials.read_text())
        document["clients"][1]["resources"] = ["robots/other-arm"]
        s.credentials.write_text(json.dumps(document))
        s.start()
        self.addCleanup(s.stop)
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            urlopen(s.ready["endpoint"] + "/v1/resources")
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(s.b.resources(), [])
        self.error("forbidden", lambda: s.b.state(RESOURCE))

    def test_old_fencing_epoch_after_release_is_rejected(self):
        s = self.system()
        old = s.a.acquire(RESOURCE, 3.0)
        s.a.release(old)
        new = s.b.acquire(RESOURCE, 3.0)
        self.assertGreater(new["control_epoch"], old["control_epoch"])
        self.error("stale_lease", lambda: s.a.submit(motion(s.a, old)))
        command = motion(s.b, new)
        s.b.submit(command)
        self.assertEqual(s.b.wait(command["command_id"])["status"], "succeeded")
        s.b.release(new)

    def test_invalid_expired_and_semantically_incompatible_actions(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 3.0)
        changes = [({"not_after_ns": 1}, "command_expired"),
                   ({"clock_id": "another-clock"}, "clock_mismatch"),
                   ({"units": ["deg", "deg"]}, "semantic_mismatch"),
                   ({"frame_id": "world"}, "semantic_mismatch"),
                   ({"state_sequence": 100}, "state_changed"),
                   ({"capability": "navigation.navigate_to/v1"}, "unsupported_capability"),
                   ({"arguments": {"positions": [100.0, 0.0], "duration_s": 0.1}}, "invalid_action")]
        for change, expected in changes:
            with self.subTest(change=change):
                command = motion(s.a, lease)
                command.update(change)
                self.error(expected, lambda: s.a.submit(command))
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 0)
        s.a.release(lease)

    def test_stale_device_state_is_rejected_before_execution(self):
        s = self.system(config={"state_age_s": 5.0})
        lease = s.a.acquire(RESOURCE, 2.0)
        self.error("stale_state", lambda: s.a.submit(motion(s.a, lease)))
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 0)
        s.a.release(lease)

    def test_cancel_is_separate_from_device_stop_confirmation(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 5.0)
        command = motion(s.a, lease, duration=3.0)
        s.a.submit(command)
        wait_for(lambda: s.a.result(command["command_id"])["feedback"].get("progress", 0) > 0)
        request = s.a.cancel(command["command_id"])
        self.assertEqual(request["status"], "cancel_requested")
        result = s.a.wait(command["command_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "client_cancelled")
        self.assertTrue(result["result"]["stop"]["stopped"])
        position = s.a.state(RESOURCE)["data"]["positions"]
        time.sleep(0.04)
        self.assertEqual(position, s.a.state(RESOURCE)["data"]["positions"])
        s.a.release(lease)

    def test_release_stops_inflight_action_before_next_owner(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 5.0)
        command = motion(s.a, lease, duration=3.0)
        s.a.submit(command)
        wait_for(lambda: s.a.state(RESOURCE)["data"]["moving"])
        self.assertTrue(s.a.release(lease)["stop_confirmed"])
        result = s.a.result(command["command_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "lease_released")
        self.assertFalse(s.a.state(RESOURCE)["data"]["moving"])
        replacement = s.b.acquire(RESOURCE, 1.0)
        self.assertGreater(replacement["control_epoch"], lease["control_epoch"])
        s.b.release(replacement)

    def test_expired_lease_stops_action_after_real_caller_exits(self):
        s = self.system()
        script = '''
import json, os, sys
from metaos_runtime.client import Client
c=Client(sys.argv[1], sys.argv[2])
lease=c.acquire("robots/mock-arm",0.35)
command=c.build_command(lease,"manipulation.move_joint/v1",{"positions":[1.0,-1.0],"duration_s":3.0})
c.submit(command)
print(json.dumps({"command_id":command["command_id"],"pid":os.getpid()}),flush=True)
os._exit(0)
'''
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", script,
            s.ready["endpoint"], str(s.credentials)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        caller = json.loads(stdout)
        (s.root / "caller-exit.json").write_text(json.dumps({**caller, "exit_code": process.returncode}))
        result = s.a.wait(caller["command_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["reason"], "lease_expired")
        self.assertTrue(result["result"]["stop"]["stopped"])
        self.assertFalse(s.a.state(RESOURCE)["data"]["moving"])
        new = s.b.acquire(RESOURCE, 1.0)
        s.b.release(new)

    def test_command_validity_expiry_stops_inflight_action(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 3.0)
        command = motion(s.a, lease, duration=2.0, valid_for_s=0.15)
        s.a.submit(command)
        result = s.a.wait(command["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("cancelled", "command_expired"))
        s.a.release(lease)

    def test_device_failure_confirms_stop_and_records_failure(self):
        s = self.system(config={"failure_step": 1})
        lease = s.a.acquire(RESOURCE, 3.0)
        command = motion(s.a, lease)
        s.a.submit(command)
        result = s.a.wait(command["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("failed", "device_execution_failed"))
        self.assertTrue(result["result"]["stop"]["stopped"])
        self.assertFalse(s.a.state(RESOURCE)["data"]["moving"])
        s.a.release(lease)

    def test_lost_submit_response_is_queried_without_duplicate_execution(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 4.0)
        command = motion(s.a, lease, duration=0.15)
        forwarded = []
        class DropResponse(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                request = Request(s.ready["endpoint"] + self.path, data=body,
                    headers={"Authorization": self.headers["Authorization"], "Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=3) as result:
                    forwarded.append(json.load(result))
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
        proxy = HTTPServer(("127.0.0.1", 0), DropResponse)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            lossy = Client(f"http://127.0.0.1:{proxy.server_port}", s.credentials)
            with self.assertRaises(OutcomeUnknown) as caught:
                lossy.submit(command)
            self.assertEqual(caught.exception.command_id, command["command_id"])
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(2)
        self.assertEqual(len(forwarded), 1)
        result = s.a.wait(command["command_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(s.a.submit(command), result)
        conflict = copy.deepcopy(command)
        conflict["arguments"]["positions"] = [0.0, 0.0]
        self.error("command_conflict", lambda: s.a.submit(conflict))
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 1)
        (s.root / "lost-response.json").write_text(json.dumps({"command_id": command["command_id"],
            "forwarded": 1, "outcome_unknown_observed": True, "execution_count": 1, "result": result}, indent=2))
        s.a.release(lease)

    def test_unconfirmed_stop_quarantines_resource(self):
        s = self.system(config={"stop_unconfirmed": True})
        lease = s.a.acquire(RESOURCE, 5.0)
        command = motion(s.a, lease, duration=3.0)
        s.a.submit(command)
        wait_for(lambda: s.a.state(RESOURCE)["data"]["moving"])
        s.a.cancel(command["command_id"])
        result = s.a.wait(command["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("outcome_unknown", "stop_unconfirmed"))
        self.assertTrue(s.a.state(RESOURCE)["gateway_fault"])
        self.error("resource_fault", lambda: s.b.acquire(RESOURCE, 1.0))
        self.error("resource_fault", lambda: s.a.submit(motion(s.a, lease)))

    def test_other_caller_cannot_query_or_cancel_command(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 3.0)
        command = motion(s.a, lease)
        s.a.submit(command)
        self.error("forbidden", lambda: s.b.result(command["command_id"]))
        self.error("forbidden", lambda: s.b.cancel(command["command_id"]))
        s.a.wait(command["command_id"])
        s.a.release(lease)

    def test_renewal_extends_current_lease_without_changing_epoch(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 0.2)
        new = s.a.renew(lease, 3.0)
        self.assertEqual(new["control_epoch"], lease["control_epoch"])
        self.assertGreater(new["expires_at_ns"], lease["expires_at_ns"])
        command = motion(s.a, new, duration=0.3)
        s.a.submit(command)
        self.assertEqual(s.a.wait(command["command_id"])["status"], "succeeded")
        s.a.release(new)

    def test_gateway_shutdown_stops_action_and_clears_leases(self):
        s = RunningSystem(self._testMethodName).start()
        try:
            lease = s.a.acquire(RESOURCE, 5.0)
            command = motion(s.a, lease, duration=3.0)
            s.a.submit(command)
            wait_for(lambda: s.a.state(RESOURCE)["data"]["moving"])
        finally:
            s.stop()
        import sqlite3
        with sqlite3.connect(s.workspace / "state.sqlite3") as db:
            result = json.loads(db.execute("SELECT record FROM commands WHERE id=?", (command["command_id"],)).fetchone()[0])
        self.assertEqual((result["status"], result["reason"]), ("cancelled", "gateway_shutdown"))
        self.assertTrue(result["result"]["stop"]["stopped"])
