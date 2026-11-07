import concurrent.futures
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

from metaos_contracts import SystemError
from metaos_runtime.client import LeaseKeeper, OutcomeUnknown
from metaos_runtime.process import alive

from helpers import RESOURCE, RunningManager, command, wait


class FaultMatrixTests(unittest.TestCase):
    def system(self):
        s = RunningManager(self._testMethodName)
        self.addCleanup(s.stop)
        return s.start()

    def error(self, code, operation):
        with self.assertRaises(SystemError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_second_manager_cannot_take_workspace_or_stop_existing_worker(self):
        s = self.system()
        identity = s.worker.ready["identity"]
        result = subprocess.run([sys.executable, "-I", "-B", "-m", "metaos_runtime", "manage", "--resume",
            "--workspace", str(s.workspace), "--credentials", str(s.credentials), "--ready-file", str(s.root / "competing-ready.json")],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("workspace_busy", result.stderr)
        self.assertTrue(alive(identity))
        lease = s.a.acquire(RESOURCE, 2.)
        s.a.release(lease)

    def test_old_manager_generation_and_clock_are_fenced_after_recovery(self):
        s = self.system()
        old = s.worker
        old.manager_id = s.a.info()["manager_id"]
        clock = s.a.info()["clock_id"]
        s.kill_manager()
        wait(lambda: old.call("health")["suspended"], 6)
        s.start(resume=True)
        self.error("stale_manager", lambda: old.call("acquire", owner="operator-a", request={"resource_id": RESOURCE, "ttl_s": 2.}))
        self.error("stale_manager", lambda: old.call("shutdown"))
        lease = s.a.acquire(RESOURCE, 3.)
        request = {**command(s.a, lease), "clock_id": clock}
        self.error("clock_mismatch", lambda: s.a.submit(request))
        s.a.release(lease)

    def test_resume_drift_rejected_without_killing_unclaimed_worker(self):
        s = self.system()
        worker = s.worker
        identity = worker.ready["identity"]
        s.kill_manager()
        wait(lambda: worker.call("health")["suspended"], 6)
        path = s.workspace / "instances/main/system.json"
        original = path.read_text()
        changed = json.loads(original)
        changed["components"]["arm"]["config"]["tick_delay_s"] = .02
        path.write_text(json.dumps(changed))
        try:
            with self.assertRaises(RuntimeError) as caught:
                s.start(resume=True)
            self.assertIn("lock_changed", str(caught.exception))
            self.assertTrue(alive(identity))
        finally:
            path.write_text(original)
        s.start(resume=True)
        self.assertEqual(s.worker.ready["identity"], identity)

    def test_reconciliation_without_ack_has_no_lease_revocation_side_effect(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 3.)
        self.error("acknowledgement_required", lambda: s.admin.instance("main", "reconcile"))
        self.assertEqual(s.a.renew(lease, 3.)["control_epoch"], lease["control_epoch"])
        s.a.release(lease)

    def test_actual_caller_process_exit_expires_and_stops_inflight_action(self):
        s = self.system()
        evidence = s.root / "caller-command.json"
        code = '''import json,sys
from metaos_runtime.client import Client
c=Client(sys.argv[1],sys.argv[2]); lease=c.acquire("robots/mock-arm",1.2)
r=c.build_command(lease,"manipulation.move_joint/v1",{"positions":[1.,-1.],"duration_s":8.},valid_for_s=20.)
c.submit(r);open(sys.argv[3],"w").write(json.dumps(r))
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, s.ready["endpoint"], str(s.credentials), str(evidence)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(evidence.read_text())
        terminal = s.a.wait(request["command_id"], 6.)
        self.assertEqual((terminal["status"], terminal["reason"]), ("cancelled", "lease_expired"))
        self.assertTrue(terminal["result"]["stop"]["stopped"])

    def test_lost_response_and_identical_id_do_not_reexecute(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 5.)
        request = command(s.a, lease, .3)
        endpoint = s.a.endpoint
        class Drop(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                req = urllib.request.Request(endpoint + self.path, self.rfile.read(int(self.headers["Content-Length"])),
                    headers={"Authorization": self.headers["Authorization"], "Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3) as reply:
                    reply.read()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            s.a.endpoint = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(OutcomeUnknown):
                s.a.submit(request)
        finally:
            s.a.endpoint = endpoint
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(s.a.wait(request["command_id"])["status"], "succeeded")
        self.assertEqual(s.a.submit(request)["status"], "succeeded")
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 1)
        s.a.release(lease)

    def test_concurrent_owners_permissions_and_sequence_validation(self):
        s = self.system()
        self.error("forbidden", lambda: s.observer.acquire(RESOURCE, 3.))
        barrier = threading.Barrier(2)
        def acquire(client):
            barrier.wait()
            try:
                return client, client.acquire(RESOURCE, 5.)
            except SystemError as exc:
                self.assertEqual(exc.code, "resource_busy")
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(acquire, [s.a, s.b]))
        owners = [row for row in outcomes if row is not None]
        self.assertEqual(len(owners), 1)
        client, lease = owners[0]
        other = s.b if client is s.a else s.a
        request = command(client, lease)
        self.error("state_changed", lambda: client.submit({**request, "state_sequence": request["state_sequence"]+1}))
        request = command(client, lease)
        client.submit(request)
        self.error("forbidden", lambda: other.result(request["command_id"]))
        self.error("forbidden", lambda: other.cancel(request["command_id"]))
        self.assertEqual(client.wait(request["command_id"])["status"], "succeeded")
        client.release(lease)

    def test_repeated_actions_and_clean_shutdown_leave_no_worker_or_lease(self):
        s = self.system()
        with LeaseKeeper(s.a, RESOURCE, ttl_s=10.) as control:
            for _ in range(25):
                request = command(s.a, control.check(), .02)
                s.a.submit(request)
                self.assertEqual(s.a.wait(request["command_id"])["status"], "succeeded")
        identity = s.worker.ready["identity"]
        s.stop()
        self.assertFalse(alive(identity))
        shutdown = json.loads((s.workspace / "instances/main/worker-shutdown.json").read_text())
        self.assertTrue(shutdown["handles_closed"])
        self.assertEqual(shutdown["active_leases"], 0)
