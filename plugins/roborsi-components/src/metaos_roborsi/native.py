"""Single owner-thread LIBERO/skill executor, private to an OS Device.

Only the OS worker can reach its owner-only Unix socket. No model/role receives
this socket. MuJoCo objects and control internals never cross the boundary.
"""
from hashlib import sha256
from pathlib import Path
import argparse
import json
import os
import signal
import socket
import traceback

from metaos_harness.graph import Registry, digest
from .wire import observation, receive, send


class Native:
    def __init__(self, config):
        from metaos_harness.graph import distribution_identity
        for expected in config.get("component_identity", []):
            if distribution_identity(expected["name"]) != expected:
                raise ValueError("Native installed component drift: " + expected["name"])
        self.config, self.env, self.episode = config, None, None
        self.states, self.captures, self.ticks = {}, {}, 0
        self.finished = False
        from metaos_harness.graph import Host, resolve
        self.skill_host = Host(resolve(config["skill_harness"]))
        self.skills = self.skill_host.__enter__()

    def under_run(self, path):
        candidate = Path(path).resolve()
        if not candidate.is_relative_to(Path(self.config["run_root"]).resolve()):
            raise ValueError("Native artifact path escapes declared run root")
        return candidate

    def tick(self):
        self.ticks += 1
        state = self.active_state
        key = str(state.workdir)
        if self.ticks % 20 or self.captures.get(key, 0) >= 400:
            return
        self.captures[key] = self.captures.get(key, 0) + 1
        self.frame(state.workdir / f"tick_{self.ticks:06d}.jpg")

    def frame(self, path):
        import cv2
        rgb = self.env.take_snapshot().images.get("head_camera")
        if rgb is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)):
                raise RuntimeError("Could not save native frame")

    def invoke(self, body):
        method, args = body["method"], body.get("arguments", {})
        if method == "ping":
            return {"ready": True, "pid": os.getpid(), "controller": "JOINT_POSITION", "action_dim": 8}
        if method == "reset":
            if self.env is not None:
                raise ValueError("An active scene cannot be reset")
            if args["task"] not in self.config["tasks"]:
                raise ValueError("Task not declared in the locked protocol")
            if args["skill_identity"] != digest(self.skills.specs(ns="libero", task="libero_pick_place")):
                raise ValueError("Planner/Engineer/native tool schemas differ")
            from roborsi.embodied.sim.libero.adapter import LiberoProBackend
            self.run_dir = self.under_run(args["run_dir"])
            os.environ["ROBORSI_ARTIFACT_ROOT"] = str(self.run_dir / "native" / "artifacts")
            self.episode = args["episode_id"]
            self.env = LiberoProBackend().make_env(args["task"], {"require_depth": True})
            obs = self.env.reset(args["seed"])
            robot = self.env._env.env.robots[0]
            self.controller_type = type(robot.controller).__name__
            self.action_dim = len(self.env._env.env.action_spec[0])
            if self.controller_type != "JointPositionController" or self.action_dim != 8:
                raise ValueError("Native controller/action semantics differ from the locked skill contract")
            self.unhook = self.env.hook_physics_step(self.tick)
            self.frame(self.run_dir / "native" / "initial.jpg")
            self.seed, self.task = args["seed"], args["task"]
            return {"instruction": self.env.instruction, "observation": observation(obs),
                    "controller_type": self.controller_type, "action_dim": self.action_dim,
                    "init_state_id": int(args["seed"]) % len(self.env._init_states)}
        if self.env is None or args.get("episode_id") != self.episode or self.finished:
            raise ValueError("Stale, missing or terminal native episode")
        if method == "snapshot":
            return {"observation": observation(self.env.take_snapshot())}
        if method == "tool":
            from roborsi.embodied.agent_loop.rollout import DispatchContext
            from roborsi.embodied.agent_loop.vlm_io import capture_usage
            workdir = self.under_run(args["workdir"])
            workdir.mkdir(parents=True, exist_ok=True)
            key = str(workdir)
            if key not in self.states:
                self.states[key] = DispatchContext(env=self.env, workdir=workdir, last_image_path=None,
                    ns="libero", task=args["task"])
            self.active_state = state = self.states[key]
            if args.get("last_image_path"):
                state.last_image_path = self.under_run(args["last_image_path"])
            allowed = {item["function"]["name"]: item["function"]["parameters"] for item in self.skills.specs(ns="libero", task=args["task"])}
            name = args["call"]["tool"]
            if name not in allowed or name == "done":
                return {"result": {"ok": False, "reason": "Tool is not in frozen callable catalog"},
                        "observation": observation(self.env.take_snapshot()), "usage": {}, "physics_ticks": self.ticks}
            from jsonschema import Draft202012Validator
            errors = list(Draft202012Validator(allowed[name]).iter_errors(args["call"].get("args", {})))
            if errors:
                return {"result": {"ok": False, "reason": "Tool arguments do not match schema: " + errors[0].message[:400]},
                        "observation": observation(self.env.take_snapshot()), "usage": {}, "physics_ticks": self.ticks}
            with capture_usage() as usage:
                result, obs = self.skills.dispatch_native(state, args["call"])
            self.frame(workdir / f"boundary_{self.ticks:06d}.jpg")
            return {"result": result, "observation": observation(obs),
                    "last_image_path": str(state.last_image_path) if state.last_image_path else None,
                    "usage": usage.to_dict(), "physics_ticks": self.ticks}
        if method == "adjudicate":
            from roborsi.embodied.agent_loop.rollout import _finalize_demo_video
            success = bool(self.env.check_success())
            self.finished = True
            self.frame(self.run_dir / "native" / "final.jpg")
            frames = self.run_dir / "native" / "episode_frames"
            frames.mkdir(parents=True, exist_ok=True)
            import shutil
            paths = []
            for workdir in self.states:
                paths.extend(sorted(Path(workdir).glob("tick_*.jpg")))
            if not paths:
                paths = [self.run_dir / "native" / "initial.jpg", self.run_dir / "native" / "final.jpg"]
            for index, source in enumerate(paths):
                shutil.copyfile(source, frames / f"tick_{index:06d}.jpg")
            video = _finalize_demo_video(frames, "libero_pick_place", self.seed, success)
            result = {"success": success, "score": float(success), "task": self.task, "seed": self.seed,
                      "init_state_selection": "seed % len(init_states)", "physics_ticks": self.ticks,
                      "predicate_source": "LiberoProEnv.check_success", "final_adjudications": 1,
                      "controller": "JOINT_POSITION", "controller_type": self.controller_type, "action_dim": self.action_dim,
                      "init_state_id": int(self.seed) % len(self.env._init_states), "init_state_count": len(self.env._init_states),
                      "video": str(video) if video else None}
            (self.run_dir / "native" / "score.json").write_text(json.dumps(result, indent=2))
            result["artifacts"] = []
            for path in sorted(self.run_dir.rglob("*")):
                if path.is_file() and (path.suffix in {".jpg", ".mp4"} or path == self.run_dir / "native/score.json"):
                    relative = path.relative_to(self.run_dir).as_posix()
                    checksum = sha256(path.read_bytes()).hexdigest()
                    result["artifacts"].append({"artifact_id": sha256(relative.encode()).hexdigest(),
                        "episode_id": self.episode, "ref": relative, "sha256": checksum, "size_bytes": path.stat().st_size,
                        "kind": "video" if path.suffix == ".mp4" else "image" if path.suffix == ".jpg" else "score"})
            return result
        raise ValueError("Unsupported native method")

    def close(self):
        if self.env is not None:
            # The integration already captures and finalizes its own run-local
            # media. Upstream's optional auto-demo recorder otherwise performs
            # a second success query and writes under the installed package on
            # close, including after an operational cancellation.
            self.env._vframes.clear()
            self.env.close()
        self.skill_host.__exit__(None, None, None)


def main():
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args()
    runtime = json.loads(args.config.read_text())
    native = Native({**runtime["native_config"], "component_identity": runtime["component_identity"]})
    args.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(args.socket))
            os.chmod(args.socket, 0o600)
            server.listen(4)
            while True:
                connection, _ = server.accept()
                with connection:
                    try:
                        request = receive(connection)
                        reply = native.invoke(request)
                        # Any numpy scalar/array in original tool results uses
                        # the same data-only encoding as observations.
                        from .wire import encode
                        send(connection, encode(reply))
                        if request["method"] == "adjudicate":
                            break
                    except Exception as exc:
                        traceback.print_exc()
                        send(connection, {"error": type(exc).__name__ + ": " + str(exc)[:600]})
    finally:
        native.close()
        args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
