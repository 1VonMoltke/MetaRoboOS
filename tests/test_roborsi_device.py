"""Focused owned-process lifecycle checks; no simulator or model requests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from metaos_roborsi_libero_device import Device, Operation


class DeviceLifecycleTests(unittest.TestCase):
    def device(self, directory, seconds):
        device = Device.__new__(Device)
        device.workspace = Path(directory)
        device.lock = threading.RLock()
        device.stop_lock = threading.RLock()
        device.process = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        self.addCleanup(lambda: device.process.poll() is None and (device.process.kill(), device.process.wait()))
        device.identity = {"pid": device.process.pid}
        device.reply, device.sequence = {}, 0
        device.ended = device.closed = False
        device.config = {"resource_id": "test/libero"}
        device.episode_id = "a" * 32
        return device

    def test_final_operation_waits_for_natural_exit_and_keeps_artifacts(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as root:
            device = self.device(root, .25)
            device.run_dir = Path(root)
            payload = b"final-score-evidence"
            (Path(root) / "evidence.bin").write_bytes(payload)
            artifact = {"artifact_id": "test", "ref": "evidence.bin", "sha256": sha256(payload).hexdigest()}
            device.request = lambda *args: {"success": False, "artifacts": [artifact]}
            operation = Operation(device, "adjudicate", {})
            self.assertFalse(operation.done.wait(.05))
            self.assertTrue(operation.done.wait(4))
            self.assertIsNone(operation.error)
            self.assertEqual(device.process.returncode, 0)
            self.assertTrue(device.ended and device.closed)
            self.assertFalse(device.read_state("test-clock").valid)
            self.assertEqual(device.read_artifact("test", 0, 100), payload)
            device.stop("component_closed")
            receipt = json.loads((Path(root) / "native-process.json").read_text())
            self.assertEqual(receipt["reason"], "episode_finalized")
            self.assertFalse(receipt["forced"])

    def test_failed_adjudication_does_not_invent_final_score(self):
        with tempfile.TemporaryDirectory() as root:
            device = self.device(root, 20)
            def failed(*args):
                raise ConnectionError("disconnected")
            device.request = failed
            operation = Operation(device, "adjudicate", {})
            self.assertTrue(operation.done.wait(2))
            self.assertEqual(operation.error, "ConnectionError")
            self.assertFalse(device.ended or device.closed)
            self.assertEqual(device.list_artifacts(), [])
            self.assertFalse(device.stop("cancelled")["scored"])
            self.assertIsNotNone(device.process.poll())

    def test_cancellation_kills_term_resistant_native_within_gateway_window(self):
        import time
        with tempfile.TemporaryDirectory() as root:
            device = self.device(root, 20)
            device.process.terminate()
            device.process.wait()
            device.process = subprocess.Popen([sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(20)"],
                stdout=subprocess.PIPE, text=True)
            self.addCleanup(device.process.stdout.close)
            self.assertEqual(device.process.stdout.readline().strip(), "ready")
            device.identity = {"pid": device.process.pid}
            started = time.monotonic()
            self.assertTrue(device.stop("lease_released")["stopped"])
            self.assertLess(time.monotonic() - started, 2)
            receipt = json.loads((Path(root) / "native-process.json").read_text())
            self.assertTrue(receipt["closed"] and receipt["forced"])
            self.assertEqual(receipt["exit_code"], -9)

    def test_state_reads_remain_responsive_during_process_retirement(self):
        with tempfile.TemporaryDirectory() as root:
            device = self.device(root, 2)
            waiting, sampled = threading.Event(), threading.Event()
            original_wait = device.process.wait
            def wait(timeout):
                waiting.set()
                return original_wait(timeout)
            device.process.wait = wait
            device.request = lambda *args: {"success": False, "artifacts": []}
            operation = Operation(device, "adjudicate", {})
            self.assertTrue(waiting.wait(2))
            states = []
            def sample():
                states.append(device.read_state("test-clock"))
                sampled.set()
            reader = threading.Thread(target=sample, daemon=True)
            reader.start()
            try:
                self.assertTrue(sampled.wait(.5), "Native retirement blocked Worker health reads")
                self.assertTrue(states[0].data["ended"])
                self.assertFalse(operation.done.is_set())
            finally:
                self.assertTrue(operation.done.wait(4))
                reader.join(1)


if __name__ == "__main__":
    unittest.main()
