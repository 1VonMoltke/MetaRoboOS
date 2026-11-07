import json
import os
import signal
import time
import unittest

from metaos_contracts import SystemError
from metaos_runtime.client import LeaseKeeper
from metaos_runtime.process import alive

from helpers import RESOURCE, RunningManager, command, spec, wait


class ManagerTests(unittest.TestCase):
    def system(self, **kwargs):
        system = RunningManager(self._testMethodName, **kwargs)
        self.addCleanup(system.stop)
        return system.start()

    def error(self, code, operation):
        with self.assertRaises(SystemError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_separate_execution_process_and_task_api(self):
        s = self.system()
        row = s.a.instances()[0]
        self.assertEqual(row["actual"], "running")
        self.assertNotEqual(row["identity"]["pid"], s.process.pid)
        lease = s.a.acquire(RESOURCE, 5.)
        request = command(s.a, lease)
        s.a.submit(request)
        result = s.a.wait(request["command_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(s.a.tasks()[0]["command_id"], request["command_id"])
        self.assertEqual(s.b.tasks(), [])
        s.a.release(lease)

    def test_manager_sigkill_stops_at_worker_then_reconciles_without_replay(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 15.)
        request = command(s.a, lease, 12.)
        s.a.submit(request)
        wait(lambda: s.a.state(RESOURCE)["data"]["moving"])
        worker = s.worker
        identity = worker.ready["identity"]
        s.kill_manager()
        self.assertTrue(alive(identity))
        wait(lambda: worker.call("health")["suspended"] and not worker.call("health")["active_commands"], 8)
        result = worker.call("get", owner="operator-a", command_id=request["command_id"])
        self.assertEqual((result["status"], result["reason"]), ("cancelled", "manager_lost"))
        self.assertTrue(result["result"]["stop"]["stopped"])
        s.start(resume=True)
        self.assertEqual(s.a.instances()[0]["identity"], identity)
        self.assertEqual(s.a.result(request["command_id"])["status"], "cancelled")
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 1)
        self.error("stale_lease", lambda: s.a.submit(command(s.a, lease)))
        replacement = s.b.acquire(RESOURCE, 3.)
        self.assertGreater(replacement["control_epoch"], lease["control_epoch"])
        s.b.release(replacement)

    def test_worker_sigkill_requires_acknowledged_reconciliation(self):
        s = self.system()
        lease = s.a.acquire(RESOURCE, 10.)
        request = command(s.a, lease, 8.)
        s.a.submit(request)
        wait(lambda: s.a.state(RESOURCE)["data"]["moving"])
        os.kill(s.worker.ready["identity"]["pid"], signal.SIGKILL)
        wait(lambda: s.admin.instances()[0]["actual"] == "fault", 10)
        self.assertEqual(s.a.result(request["command_id"])["status"], "outcome_unknown")
        self.error("acknowledgement_required", lambda: s.admin.instance("main", "restart"))
        s.admin.instance("main", "restart", acknowledge=True)
        self.error("resource_fault", lambda: s.a.acquire(RESOURCE, 3.))
        s.admin.instance("main", "reconcile", acknowledge=True)
        new = s.a.acquire(RESOURCE, 3.)
        self.assertGreater(new["control_epoch"], lease["control_epoch"])
        self.assertEqual(s.a.result(request["command_id"])["status"], "outcome_unknown")
        self.assertEqual(s.a.state(RESOURCE)["data"]["execution_count"], 0)
        s.a.release(new)

    def test_instance_create_stop_start_and_resource_reservation(self):
        s = self.system()
        self.error("forbidden", lambda: s.a.create_instance("other", spec("robots/other-arm")))
        s.admin.create_instance("other", spec("robots/other-arm"))
        self.assertEqual(len(s.a.instances()), 2)
        self.error("resource_conflict", lambda: s.admin.create_instance("duplicate", spec()))
        s.admin.instance("other", "stop")
        self.error("instance_unavailable", lambda: s.a.acquire("robots/other-arm", 2.))
        s.admin.instance("other", "start")
        lease = s.a.acquire("robots/other-arm", 2.)
        s.a.release(lease)

    def test_lease_keeper_runs_while_model_caller_is_blocked(self):
        s = self.system()
        with LeaseKeeper(s.a, RESOURCE, ttl_s=10.) as control:
            epoch = control.lease["control_epoch"]
            time.sleep(21.)
            self.assertEqual(control.check()["control_epoch"], epoch)
            request = command(s.a, control.check())
            s.a.submit(request)
            self.assertEqual(s.a.wait(request["command_id"])["status"], "succeeded")

    def test_blocked_plugin_is_contained_and_other_instance_stays_live(self):
        s = self.system(config={"tick_delay_s": 20.})
        s.admin.create_instance("healthy", spec("robots/healthy-arm"))
        lease = s.a.acquire(RESOURCE, 15.)
        request = command(s.a, lease, 12.)
        s.a.submit(request)
        wait(lambda: next(row for row in s.admin.instances() if row["id"] == "main")["actual"] == "fault", 10)
        other = s.b.acquire("robots/healthy-arm", 3.)
        s.b.release(other)
        result = s.admin.instance("main", "stop")
        self.assertEqual(result["actual"], "fault")
        self.assertTrue(result["last_stop"]["forced"])
        self.assertFalse(alive(result["identity"]))
        self.assertEqual(s.a.result(request["command_id"])["status"], "outcome_unknown")


if __name__ == "__main__":
    unittest.main()
