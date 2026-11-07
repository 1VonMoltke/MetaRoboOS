"""RoboCasa365 stays in its native container; this package speaks the public API."""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from jsonschema import Draft202012Validator
from metaos_contracts import CapabilityDescriptor, ResourceDescriptor, StateSample, SystemError, TickResult
from roborsi.contracts import Capability, ComponentDescriptor, ComponentHandle, ComponentPlan, PreparedComponent

RESET = "simulation.reset/v1"
ACTION = "simulation.robocasa_action/v1"
END = "simulation.end_episode/v1"
FINISH = "simulation.finish/v1"
ABORT = "simulation.cancel_episode/v1"


class Operation:
    def __init__(self, device, capability, arguments):
        self.device, self.capability = device, capability
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.execute, args=(arguments,), daemon=True)
        self.thread.start()

    def execute(self, arguments):
        try:
            self.device.execute(self.capability, arguments)
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()

    def tick(self, elapsed_s):
        if self.error:
            raise self.error
        return TickResult(done=self.done.is_set(), feedback={"operation": self.capability,
            "elapsed_s": elapsed_s, "episode_id": self.device.episode_id})


class RoboCasaDevice:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, Path(workspace)
        self.lock = threading.RLock()
        self.sequence = 0
        self.episode_id = None
        self.observation = None
        self.last_reply = None
        self.ended = self.moving = self.closed = False
        self.operation = None
        self.last_health = 0
        self.stopping = threading.Event()
        self.container = None
        self.artifact_records = {}
        self.episode_ids = set()
        self.native_token = secrets.token_urlsafe(32)
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            self.launch()
            self.tasks = self.request("GET", "/v1/tasks")["tasks"]
            if not self.tasks or any(t["benchmark"] != "robocasa365" for t in self.tasks):
                raise SystemError("native_contract_mismatch", "Native service does not expose RoboCasa365 Target50 tasks", 503)
            identities = {(task["task_id"], task["seed"]) for task in self.tasks}
            if len(identities) != len(self.tasks) or any(task.get("native", {}).get("protocol") != "robocasa-harness-vla-v1"
                    or task["native"].get("environment_split") != "target" or task["seed"] < 1 for task in self.tasks):
                raise SystemError("native_contract_mismatch", "Native Target50 catalog identity/protocol differs", 503)
            schemas = [task["capabilities"]["action_schema"] for task in self.tasks]
            if any(schema != schemas[0] for schema in schemas):
                raise SystemError("native_contract_mismatch", "This adapter requires a uniform action schema", 503)
            self.action_schema = schemas[0]
            self.last_health = time.monotonic_ns()
            self.health_thread = threading.Thread(target=self.watch_health, daemon=True)
            self.health_thread.start()
        except Exception:
            self.remove_container()
            raise

    def docker(self, *arguments, timeout=30):
        env = dict(os.environ)
        if self.config.get("docker_host"):
            env["DOCKER_HOST"] = self.config["docker_host"]
        result = subprocess.run(["docker", *map(str, arguments)], env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise SystemError("native_runtime_error", "Owned native container operation failed; inspect retained runtime logs", 503)
        return result.stdout.strip()

    def journal(self, value):
        fd, path = tempfile.mkstemp(prefix=".native-", dir=self.workspace)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self.workspace / "native-runtime.json")
        finally:
            Path(path).unlink(missing_ok=True)

    def launch(self):
        image = self.config["image"]
        actual = self.docker("image", "inspect", image, "--format", "{{.Id}}")
        if actual != image:
            raise SystemError("native_image_changed", "Native image must match its immutable image id", 400)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.endpoint = f"http://127.0.0.1:{port}"
        owner = uuid.uuid4().hex
        name = "metaos-robocasa-" + owner[:16]
        token_file = self.workspace / "native.env"
        fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("ROBORSI_BENCHMARK_TOKEN=" + self.native_token + "\n")
        args = ["run", "--detach", "--rm", "--name", name, "--label", "metaos.owner=" + owner,
            "--network", "host", "--gpus", "device=" + str(self.config["gpu_device"]),
            "--shm-size", "2g", "--env-file", token_file,
            "--env", "ROBORSI_ROBOCASA_CELLS=" + self.config.get("cells", "all"),
            "--env", "ROBORSI_ROBOCASA_CUDA_DEVICE=0",
            "--env", "RLDX_CHECKPOINT=/opt/rldx/RLDX-1-FT-RC365",
            "--env", "OMP_NUM_THREADS=1", "--env", "OPENBLAS_NUM_THREADS=1", "--env", "MKL_NUM_THREADS=1"]
        if self.config.get("vla_endpoint"):
            args += ["--env", "ROBORSI_ROBOCASA_VLA_ENDPOINT=" + self.config["vla_endpoint"]]
        args += ["--entrypoint", "python", image, "-m", "roborsi.evaluation.service_host",
            "--kind", "benchmark", "--factory", "robots.robocasa.benchmark_service:make_robocasa365_benchmark",
            "--host", "127.0.0.1", "--port", str(port)]
        # Write ownership before launching so manager cleanup can find a container
        # even if the worker dies between docker run and receiving its id.
        journal = {"name": name, "owner": owner, "image": image, "docker_host": self.config.get("docker_host"), "container_id": None}
        self.journal(journal)
        self.container = journal
        journal["container_id"] = self.docker(*args)
        self.journal(journal)
        end = time.monotonic() + 45
        while True:
            try:
                self.request("GET", "/v1/health", timeout=1)
                break
            except Exception:
                if time.monotonic() >= end:
                    raise SystemError("native_not_ready", "Native RoboCasa365 service did not become healthy", 503)
                time.sleep(.1)

    def remove_container(self):
        if not self.container:
            return
        try:
            ids = self.docker("ps", "-aq", "--no-trunc", "--filter", "name=^/" + self.container["name"] + "$")
            if not ids:
                return
            details = json.loads(self.docker("inspect", self.container["name"]))[0]
            if (details["Config"].get("Labels", {}).get("metaos.owner") != self.container["owner"] or details["Image"] != self.container["image"]
                    or self.container.get("container_id") not in {None, details["Id"]}):
                raise SystemError("native_identity_changed", "Refusing to remove an unowned native container", 503)
            log = self.docker("logs", self.container["name"])
            (self.workspace / "native-container.log").write_text(log)
            self.docker("rm", "--force", details["Id"])
        except SystemError:
            # Preserve the ownership journal for manager-side diagnosis/cleanup.
            raise
        finally:
            (self.workspace / "native.env").unlink(missing_ok=True)

    def request(self, method, path, body=None, timeout=3600, binary=False):
        request = Request(self.endpoint + path, method=method,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self.native_token, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as reply:
                data = reply.read(256 * 1024 * 1024 + 1)
                if len(data) > 256 * 1024 * 1024:
                    raise SystemError("artifact_too_large", "Native reply exceeds supported size", 503)
                return data if binary else json.loads(data)
        except HTTPError as exc:
            raise SystemError("native_request_rejected", "Native benchmark rejected the requested operation", exc.code) from None

    def watch_health(self):
        while not self.stopping.wait(.25):
            try:
                self.request("GET", "/v1/health", timeout=1)
                self.last_health = time.monotonic_ns()
            except Exception:
                pass

    def describe_resource(self):
        if hasattr(self, "descriptor"):
            return self.descriptor
        episode = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
        def capability(identifier, properties, required, units=()):
            return CapabilityDescriptor(id=identifier, units=units, frame_id="robocasa_native", max_command_validity_s=3600.,
                parameters={"type": "object", "additionalProperties": False, "properties": properties, "required": required})
        self.descriptor = ResourceDescriptor(resource_id=self.config.get("resource_id", "environments/robocasa365"), kind="robocasa365_native",
            capabilities=(capability(RESET, {"task_id": {"enum": sorted({task["task_id"] for task in self.tasks})}, "seed": {"type": "integer", "minimum": 1}, "episode_id": episode}, ["task_id", "seed", "episode_id"]),
                capability(ACTION, {"episode_id": episode, "action": self.action_schema}, ["episode_id", "action"], ("robocasa_native_action/v1",)),
                capability(END, {"episode_id": episode, "reason": {"type": "string", "minLength": 1}}, ["episode_id", "reason"]),
                capability(ABORT, {"episode_id": episode}, ["episode_id"]),
                capability(FINISH, {"episode_id": episode}, ["episode_id"])),
            metadata={"benchmark": "robocasa365", "native_image": self.config["image"], "frame_semantics": "native RoboCasa365 toolkit primitives",
                "tasks": self.tasks, "episode_stop_supported": True,
                "termination_semantics": "Original Target50 split wall-clock and 400-tool runaway cap; policy stop is scored, operational cancellation is unscored",
                "observation_validity": "lockstep scene unchanged between native operations; native health sampled separately",
                "control_domain": self.config.get("resource_id", "environments/robocasa365")})
        return self.descriptor

    def read_state(self, clock_id):
        with self.lock:
            return StateSample(resource_id=self.config.get("resource_id", "environments/robocasa365"), sequence=self.sequence,
                clock_id=clock_id, sampled_at_ns=self.last_health,
                frame_id="robocasa_native", valid=not self.closed and time.monotonic_ns() - self.last_health < int(2e9),
                data={"episode_id": self.episode_id, "observation": self.observation, "last_reply": self.last_reply,
                    "ended": self.ended, "moving": self.moving, "closed": self.closed})

    def validate_action(self, capability, arguments):
        descriptor = next((cap for cap in self.describe_resource().capabilities if cap.id == capability), None)
        if descriptor is None or not Draft202012Validator(descriptor.parameters).is_valid(arguments):
            raise SystemError("invalid_action", "Native capability arguments mismatch", 400)
        with self.lock:
            if self.moving:
                raise SystemError("resource_busy", "Native operation is already active")
            if capability != RESET and arguments["episode_id"] != self.episode_id:
                raise SystemError("stale_episode", "Command belongs to an old native session")
            if capability == RESET:
                if not any(t["task_id"] == arguments["task_id"] and t["seed"] == arguments["seed"] for t in self.tasks):
                    raise SystemError("invalid_task", "Task and seed pair is not in the native catalog", 400)
                if self.episode_id is not None and not self.ended:
                    raise SystemError("episode_active", "End the current native episode before resetting")
                if arguments["episode_id"] in self.episode_ids:
                    raise SystemError("stale_episode", "Native episode identity cannot be reused")
            elif capability != ABORT and (self.observation is None or (self.ended and capability != FINISH)):
                raise SystemError("episode_ended", "Episode is not available for this operation")
            if capability == FINISH and not self.ended:
                raise SystemError("episode_active", "Finish requires a terminal step or explicit episode stop")

    def start(self, capability, arguments):
        with self.lock:
            self.validate_action(capability, arguments)
            self.moving = True
            self.operation = Operation(self, capability, arguments)
            return self.operation

    def envelope(self, kind, payload, request_id=None):
        return {"schema_version": 1, "kind": kind, "benchmark": "robocasa365", "episode_id": self.episode_id,
            "request_id": request_id or uuid.uuid4().hex, "payload": payload}

    def execute(self, capability, arguments):
        try:
            if capability == RESET:
                if not any(t["task_id"] == arguments["task_id"] and t["seed"] == arguments["seed"] for t in self.tasks):
                    raise SystemError("invalid_task", "Task and seed pair is not in the native catalog", 400)
                if self.episode_id is not None:
                    self.request("POST", self.episode_path() + "/close", {})
                task = next(task for task in self.tasks if task["task_id"] == arguments["task_id"] and task["seed"] == arguments["seed"])
                self.episode_id = arguments["episode_id"]
                self.episode_ids.add(self.episode_id)
                self.artifact_records = {}
                self.observation = None
                self.ended = False
                reply = self.request("POST", "/v1/episodes", self.envelope("task", task))
            elif capability == FINISH:
                reply = self.request("POST", self.episode_path() + "/finish", {"request_id": uuid.uuid4().hex})
                self.collect_artifacts()
            elif capability == ABORT:
                evidence = self.stop("client_cancelled")
                if evidence.get("stopped") is not True:
                    raise SystemError("stop_unconfirmed", "Native cancellation did not confirm stopping", 503)
                reply = {"payload": evidence, "kind": "operational_cancellation"}
            else:
                kind, tail = {ACTION: ("action", "actions"), END: ("episode_stop", "stop")}[capability]
                observation = self.observation
                if observation is None or self.ended:
                    raise SystemError("episode_ended", "Episode is not available for this operation")
                payload = {"schema_version": 1, "benchmark": "robocasa365", "episode_id": self.episode_id,
                    "step_index": observation["step_index"]}
                if capability == ACTION:
                    payload["data"] = arguments["action"]
                else:
                    payload.update(reason=arguments["reason"], details={})
                reply = self.request("POST", self.episode_path() + "/" + tail, self.envelope(kind, payload))
            payload = reply.get("payload", reply)
            observation = payload.get("observation")
            if observation:
                self.collect_artifacts(images_only=True)
            with self.lock:
                self.last_reply = reply
                if observation:
                    self.observation = observation
                self.ended = self.ended or capability == FINISH or bool(payload.get("terminated") or payload.get("truncated"))
                self.sequence += 1
        finally:
            with self.lock:
                self.moving = False

    def episode_path(self):
        return "/v1/episodes/" + quote(self.episode_id, safe="")

    def collect_artifacts(self, images_only=False):
        rows = self.request("GET", self.episode_path() + "/artifacts")["artifacts"]
        base = self.workspace / "episodes" / self.episode_id
        base.mkdir(parents=True, exist_ok=True)
        for info in rows:
            if info.get("episode_id") != self.episode_id:
                raise SystemError("invalid_artifact", "Native artifact belongs to a different session", 503)
            if images_only and info["kind"] != "image":
                continue
            relative = PurePosixPath(info["ref"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in info["ref"]:
                raise SystemError("invalid_artifact", "Native artifact path is unsafe", 503)
            path = base / str(relative)
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]:
                with self.lock:
                    self.artifact_records[info["artifact_id"]] = info
                continue
            data = self.request("GET", self.episode_path() + "/artifacts/" + info["artifact_id"], binary=True)
            if len(data) != info["size_bytes"] or hashlib.sha256(data).hexdigest() != info["sha256"]:
                raise SystemError("artifact_changed", "Native artifact identity differs", 503)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            with self.lock:
                self.artifact_records[info["artifact_id"]] = info
        (base / "artifacts.json").write_text(json.dumps(rows, indent=2))

    def list_artifacts(self):
        with self.lock:
            return list(self.artifact_records.values())

    def read_artifact(self, artifact_id, offset, length):
        with self.lock:
            info = self.artifact_records.get(artifact_id)
            if info is None:
                raise SystemError("artifact_not_found", "Artifact does not belong to the active session", 404)
            path = self.workspace / "episodes" / info["episode_id"] / info["ref"]
        if path.is_symlink() or not path.resolve().is_relative_to(self.workspace.resolve()):
            raise SystemError("invalid_artifact", "Artifact location is unsafe", 503)
        with path.open("rb") as stream:
            stream.seek(offset)
            return stream.read(length)

    def stop(self, reason):
        if self.episode_id is None:
            return {"stopped": True, "reason": reason, "episode_id": None}
        self.request("POST", self.episode_path() + "/cancel", {}, timeout=3)
        end = time.monotonic() + 3
        while True:
            state = self.request("GET", self.episode_path(), timeout=1)
            if state["state"] in {"cancelled", "closed", "completed", "failed"}:
                if state.get("error"):
                    return {"stopped": False, "reason": reason, "episode_id": self.episode_id,
                        "native_state": state["state"], "native_error_code": state["error"].get("code")}
                break
            if time.monotonic() >= end:
                return {"stopped": False, "reason": reason, "episode_id": self.episode_id}
            time.sleep(.03)
        operation = self.operation
        if operation and operation.thread is not threading.current_thread() and not operation.done.wait(1):
            return {"stopped": False, "reason": reason, "episode_id": self.episode_id}
        with self.lock:
            self.ended = True
            self.moving = False
            self.sequence += 1
        return {"stopped": True, "reason": reason, "episode_id": self.episode_id, "native_state": state["state"]}

    def close(self):
        self.stopping.set()
        if hasattr(self, "health_thread"):
            self.health_thread.join(2)
        try:
            if self.episode_id is not None:
                self.stop("component_closed")
        finally:
            self.remove_container()
            self.closed = True


class Provider:
    descriptor = ComponentDescriptor(id="environment.robocasa_device", kind="environment", version="0.1.0",
        provides=(Capability(contract=ACTION), Capability(contract=RESET)),
        config_schema={"type": "object", "additionalProperties": False, "required": ["image", "gpu_device"],
            "properties": {"resource_id": {"type": "string"}, "image": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
                "gpu_device": {"type": "integer", "minimum": 0},
                "docker_host": {"type": "string", "pattern": "^unix:///"},
                "cells": {"enum": ["all", "seed1"]},
                "vla_endpoint": {"type": "string", "pattern": "^http://127\\.0\\.0\\.1:[0-9]+/?$"}}})

    def describe(self):
        return self.descriptor.model_copy(deep=True)

    def plan(self, spec, context):
        context.check()
        if spec.component != self.descriptor.id or not Draft202012Validator(self.descriptor.config_schema).is_valid(spec.config):
            raise SystemError("invalid_config", "Invalid native RoboCasa device configuration", 400)
        config = dict(spec.config)
        return ComponentPlan(descriptor=self.describe(), configuration_json=json.dumps(config),
            metadata_json=json.dumps({"native_images": [{"image": config["image"], "docker_host": config.get("docker_host")}],
                "transport": "local_http", "observation_semantics": "native RoboCasa365 Target50",
                "protocol": "robocasa-harness-vla-v1", "runtime_prerequisites": ["RoboCasa kitchen assets", "RLDX-1 weights or local VLA service"]}))

    def prepare(self, plan, context):
        context.check()
        plan.verify_inputs()
        context.workspace.mkdir(parents=True, exist_ok=True)
        return PreparedComponent(plan)

    def open(self, prepared, context):
        device = RoboCasaDevice(prepared.plan.config, context.workspace / ("native-" + uuid.uuid4().hex[:12]))
        return ComponentHandle(self.descriptor.id, device, cleanup=device.close)


def provider():
    return Provider()
