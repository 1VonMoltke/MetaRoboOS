import threading
import time
import unittest
from unittest.mock import patch

from metaos_runtime.manager import Manager
from metaos_runtime.process import process_identity
from metaos_runtime.worker import Authority


class AuthorityTests(unittest.TestCase):
    def test_attach_starts_heartbeats_before_slow_catalog_save(self):
        class Gateway:
            def __init__(self):
                self.suspended = threading.Event()
                self.store = self
            def event(self, *args, **kwargs):
                pass
            def suspend(self, reason):
                self.suspended.set()
                return {}
            def resume(self):
                self.suspended.clear()
            def snapshot(self):
                return {}
        gateway, stopping = Gateway(), threading.Event()
        authority = Authority(gateway, stopping)
        class Worker:
            ready = {"identity": process_identity(), "generation": 1, "fingerprint": "content"}
            def call(self, method, **arguments):
                if method == "describe":
                    time.sleep(2.1)
                    return [{"resource_id": "robots/arm"}]
                return authority.invoke(method, arguments)
        class Catalog:
            def save(self, row):
                time.sleep(2.1)
        manager = object.__new__(Manager)
        manager.id, manager.clock_id = "current", "clock"
        manager.clients, manager.monitors = {}, {}
        manager.closing, manager.catalog = threading.Event(), Catalog()
        manager.record = lambda _: {"generation": 1, "fingerprint": "content", "resource_ids": ["robots/arm"]}
        manager.directory = lambda _: "/unused"
        manager._validate_lock = lambda _: None
        try:
            with patch("metaos_runtime.manager.WorkerClient", return_value=Worker()):
                manager._attach("main")
            self.assertFalse(gateway.suspended.is_set())
            self.assertEqual(authority.manager, "current")
        finally:
            manager.closing.set()
            for stop, *threads in manager.monitors.values():
                stop.set()
                for thread in threads:
                    thread.join(4)
            stopping.set()
            authority.watchdog.join(4)

    def test_heartbeat_does_not_wait_behind_slow_command_admission(self):
        class Gateway:
            def __init__(self):
                self.suspended = threading.Event()
                self.suspension_reason = None
                self.entered = threading.Event()
                self.store = self
                self.events = []
            def event(self, name, **kwargs):
                self.events.append(name)
            def suspend(self, reason):
                self.suspended.set()
                self.suspension_reason = reason
                return {}
            def resume(self):
                self.suspended.clear()
            def snapshot(self):
                return {}
            def acquire(self, owner, request):
                self.entered.set()
                time.sleep(2.1)  # Longer than the independent watchdog budget.
                return {"owner": owner}
        gateway, stopping = Gateway(), threading.Event()
        authority = Authority(gateway, stopping)
        result = []
        authority.invoke("claim", {"manager_id": "current", "clock_id": "clock"})
        thread = threading.Thread(target=lambda: result.append(authority.invoke("acquire", {
            "manager_id": "current", "owner": "operator-a", "request": {"resource_id": "robots/arm", "ttl_s": 10.}})))
        try:
            thread.start()
            self.assertTrue(gateway.entered.wait(1))
            while thread.is_alive():
                authority.invoke("heartbeat", {"manager_id": "current"})
                time.sleep(.1)
            self.assertEqual(result, [{"owner": "operator-a"}])
            self.assertFalse(gateway.suspended.is_set())
            self.assertEqual(authority.manager, "current")
        finally:
            stopping.set()
            thread.join(4)
            authority.watchdog.join(4)
