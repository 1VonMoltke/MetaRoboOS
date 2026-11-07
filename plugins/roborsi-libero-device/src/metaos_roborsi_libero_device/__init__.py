"""OS-owned original JOINT_POSITION LIBERO runtime; no simulator imports here."""
from hashlib import sha256
from pathlib import Path
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time

from jsonschema import Draft202012Validator
from metaos_contracts import CapabilityDescriptor, ResourceDescriptor, StateSample, SystemError, TickResult
from roborsi.contracts import Capability, ComponentDescriptor, ComponentHandle, ComponentPlan, FileIdentity, PreparedComponent

FRAME = "libero_world_joint_position"
MAX_MESSAGE = 32 * 1024 * 1024


class Operation:
    def __init__(self, device, method, arguments):
        self.device, self.done, self.error = device, threading.Event(), None
        def execute():
            try:
                reply = device.request(method, arguments)
                with device.lock:
                    device.reply = reply
                    device.sequence += 1
                    device.ended = method == "adjudicate"
                if method == "adjudicate":
                    # Finalization owns the expensive simulator teardown. Do
                    # not defer it to the Worker's short shutdown window.
                    # Finalized artifact files remain available through us.
                    device.stop("episode_finalized")
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                self.done.set()
        threading.Thread(target=execute, daemon=True).start()

    def tick(self, elapsed_s):
        if self.error:
            raise SystemError("native_operation_failed", "Inspect the retained native.log", 503)
        return TickResult(done=self.done.is_set(), feedback={"elapsed_s": elapsed_s})


class Device:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_lock = threading.RLock()
        self.sequence, self.reply, self.episode_id = 0, {}, None
        self.ended = self.closed = False
        # Unix socket path lengths are bounded; workspace paths can be long.
        self.private = Path(tempfile.mkdtemp(prefix="metaos-rsi-"))
        self.socket = self.private / "native.sock"
        runtime = json.loads(Path(config["runtime_manifest"]).read_text())
        self.runtime = runtime
        configuration = self.private / "config.json"
        configuration.write_text(json.dumps(runtime))
        env = dict(os.environ)
        env.update(runtime["environment"])
        env["PYTHONNOUSERSITE"] = "1"
        env.pop("PYTHONPATH", None)
        self.log = (self.workspace / "native.log").open("w")
        self.process = subprocess.Popen([runtime["python"], "-I", "-B", "-m", "metaos_roborsi.native",
            "--config", str(configuration), "--socket", str(self.socket)], env=env,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        from metaos_runtime.process import process_identity, atomic_json
        self.identity = process_identity(self.process.pid)
        atomic_json(self.workspace / "native-process.json", {"identity": self.identity, "closed": False})
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("Native process exited; inspect native.log")
                if self.socket.exists() and self.request("ping", timeout=2).get("ready"):
                    break
                time.sleep(.1)
            else:
                raise TimeoutError("Native startup timed out")
        except BaseException:
            self.close()
            raise

    def request(self, method, arguments=None, timeout=650):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.socket))
            data = json.dumps({"method": method, "arguments": arguments or {}}, allow_nan=False).encode()
            sock.sendall(struct.pack("!I", len(data)) + data)
            def exact(count):
                buf = bytearray()
                while len(buf) < count:
                    part = sock.recv(count - len(buf))
                    if not part:
                        raise ConnectionError("Native runtime disconnected")
                    buf.extend(part)
                return buf
            size = struct.unpack("!I", exact(4))[0]
            if size > MAX_MESSAGE:
                raise ValueError("Native reply exceeds limit")
            result = json.loads(exact(size))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def describe_resource(self):
        episode = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
        def cap(name, properties, required):
            return CapabilityDescriptor(id="roborsi." + name + "/v1", units=("roborsi_tool_call/v1",),
                frame_id=FRAME, max_command_validity_s=650.,
                parameters={"type": "object", "additionalProperties": False, "properties": properties, "required": required})
        return ResourceDescriptor(resource_id=self.config["resource_id"], kind="roborsi_joint_libero",
            capabilities=(cap("reset", {"episode_id": episode, "task": {"enum": self.runtime["native_config"]["tasks"]},
                "seed": {"type": "integer", "minimum": 0}, "skill_identity": {"type": "string"},
                "run_dir": {"type": "string"}}, ["episode_id", "task", "seed", "skill_identity", "run_dir"]),
                cap("snapshot", {"episode_id": episode}, ["episode_id"]),
                cap("tool", {"episode_id": episode, "call": {"type": "object"}, "workdir": {"type": "string"},
                    "task": {"enum": ["libero_pick_place"]}, "last_image_path": {"type": ["string", "null"]}},
                    ["episode_id", "call", "workdir", "task", "last_image_path"]),
                cap("adjudicate", {"episode_id": episode}, ["episode_id"])),
            metadata={"controller": "JOINT_POSITION", "action_dim": 8, "skill_execution": "native_owner_thread",
                "scoring": "final_predicate_only", "control_domain": self.config["resource_id"],
                "runtime_manifest_sha256": sha256(Path(self.config["runtime_manifest"]).read_bytes()).hexdigest()})

    def read_state(self, clock_id):
        with self.lock:
            return StateSample(resource_id=self.config["resource_id"], sequence=self.sequence, clock_id=clock_id,
                sampled_at_ns=time.monotonic_ns(), frame_id=FRAME,
                valid=not self.closed and self.process.poll() is None,
                data={"reply": self.reply, "ended": self.ended, "episode_id": self.episode_id,
                      "command_id": self.reply.get("command_id")})

    def validate_action(self, capability, arguments):
        cap = next((c for c in self.describe_resource().capabilities if c.id == capability), None)
        if cap is None or not Draft202012Validator(cap.parameters).is_valid(arguments):
            raise SystemError("invalid_action", "Incompatible RoboRSI LIBERO command", 400)
        if self.closed or self.process.poll() is not None:
            raise SystemError("native_closed", "Native runtime must be restarted", 503)
        if capability == "roborsi.reset/v1":
            if self.episode_id is not None:
                raise SystemError("episode_active", "Reset prohibited during multi-round evaluation")
        elif self.ended or arguments["episode_id"] != self.episode_id:
            raise SystemError("stale_episode", "Stale or terminal episode")

    def start(self, capability, arguments):
        self.validate_action(capability, arguments)
        if capability == "roborsi.reset/v1":
            self.episode_id = arguments["episode_id"]
            self.run_dir = Path(arguments["run_dir"]).resolve()
        return Operation(self, capability.split(".", 1)[1].split("/")[0], arguments)

    def list_artifacts(self):
        return list(self.reply.get("artifacts", [])) if self.ended else []

    def read_artifact(self, artifact_id, offset, length):
        info = next((row for row in self.list_artifacts() if row["artifact_id"] == artifact_id), None)
        if info is None:
            raise SystemError("artifact_not_found", "Artifact is not in this finalized episode", 404)
        path = self.run_dir / info["ref"]
        if path.is_symlink() or not path.resolve().is_relative_to(self.run_dir):
            raise SystemError("invalid_artifact", "Artifact escapes episode directory", 400)
        if sha256(path.read_bytes()).hexdigest() != info["sha256"]:
            raise SystemError("artifact_changed", "Native artifact identity changed", 503)
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)

    def stop(self, reason):
        # The original loops aren't independently cancellable. Terminating the
        # exclusively owned process prevents any more servo ticks after stop.
        # Process retirement may take seconds. It must not hold the state lock:
        # Worker health and independent lease renewal stay responsive meanwhile.
        with self.stop_lock:
            if not self.closed:
                forced = False
                if self.process.poll() is None:
                    if reason != "episode_finalized":
                        self.process.terminate()
                    try:
                        # Native exits its serve loop after sending the final
                        # score, so ordinary finalization needs no signal.
                        # Cancellation must fit the gateway's 2 s stop window.
                        # A SIGTERM can be delayed in a CUDA/model-loading call.
                        self.process.wait(10 if reason == "episode_finalized" else .75)
                    except subprocess.TimeoutExpired:
                        forced = True
                        self.process.kill()
                        self.process.wait(.75)
                with self.lock:
                    self.closed = True
                from metaos_runtime.process import atomic_json
                atomic_json(self.workspace / "native-process.json", {"identity": self.identity, "closed": True,
                    "exit_code": self.process.poll(), "reason": reason, "forced": forced})
            return {"stopped": self.process.poll() is not None, "reason": reason, "scored": self.ended}

    def close(self):
        self.stop("component_closed")
        self.log.close()
        import shutil
        shutil.rmtree(self.private)


class Provider:
    descriptor = ComponentDescriptor(id="environment.roborsi_libero_device", kind="environment", version="0.1.0",
        provides=(Capability(contract="roborsi.tool/v1"), Capability(contract="roborsi.reset/v1")),
        config_schema={"type": "object", "additionalProperties": False, "required": ["resource_id", "runtime_manifest"],
            "properties": {"resource_id": {"type": "string"}, "runtime_manifest": {"type": "string"}}})

    def describe(self):
        return self.descriptor.model_copy(deep=True)

    def plan(self, spec, context):
        context.check()
        Draft202012Validator(self.descriptor.config_schema).validate(dict(spec.config))
        config = dict(spec.config)
        config["runtime_manifest"] = str(context.resolve(config["runtime_manifest"]))
        return ComponentPlan(descriptor=self.describe(), configuration_json=json.dumps(config),
                             inputs=(FileIdentity.capture(config["runtime_manifest"]),))

    def prepare(self, plan, context):
        context.check()
        plan.verify_inputs()
        return PreparedComponent(plan)

    def open(self, prepared, context):
        device = Device(prepared.plan.config, context.workspace)
        return ComponentHandle(self.descriptor.id, device, cleanup=device.close)


def provider():
    return Provider()
