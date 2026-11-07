from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import os
import time

from metaos_harness.gateway import Client, LeaseKeeper
from metaos_harness.graph import canonical, digest
from .wire import restore_obs


def normalize_image_mime(value):
    """Preserve image bytes while correcting upstream PNG-as-JPEG labels.

    In particular, observe_orbit produces PNG files, whereas the original
    generic image-message helper labels every file image/jpeg. Strict model
    endpoints reject the request before inference. This is a transport fix;
    no scene evidence is dropped or recompressed.
    """
    if isinstance(value, list):
        return [normalize_image_mime(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_image_mime(item) for key, item in value.items()}
    if value.get("type") != "image_url":
        return result
    image_url = value.get("image_url")
    url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:image/"):
        return result
    header, separator, data = url.partition(",")
    if not separator or not header.endswith(";base64"):
        return result
    import base64
    magic = base64.b64decode(data[:32], validate=True)
    if magic.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif magic.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif magic.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif magic.startswith(b"RIFF") and magic[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("Unrecognized encoded image format")
    corrected = "data:" + mime + ";base64," + data
    result["image_url"] = {**image_url, "url": corrected} if isinstance(image_url, dict) else corrected
    return result


class Model:
    def __init__(self, config, bindings, runtime):
        self.config, self.runtime = config, runtime
        # Original supported backends: preserve persistent CLI sessions by
        # default. Stateless API mode is an explicit, locked baseline option.
        os.environ["ROBORSI_ROLE_SESSION"] = "1" if config["role_sessions"] == "persistent" else "0"
        os.environ["ROBORSI_OPENAI_TRANSPORT"] = config["transport"]
        if config.get("base_url"):
            os.environ["OPENAI_BASE_URL"] = config["base_url"]

    def _record(self, kind, request, invoke):
        path = Path(self.runtime["run_dir"]) / "model-events.jsonl"
        started = time.time()
        base = {"kind": kind, "request_sha256": digest(request), "started_at": started,
                "role_sessions": self.config["role_sessions"], "provider": "original_roborsi"}
        try:
            response = invoke()
        except Exception as exc:
            # Exception messages can contain provider request/credential data.
            with path.open("a") as f:
                f.write(canonical({**base, "status": "error", "error_type": type(exc).__name__, "elapsed_s": time.time() - started}) + "\n")
            raise
        value = response.model_dump(mode="json") if hasattr(response, "model_dump") else response
        with path.open("a") as f:
            f.write(canonical({**base, "status": "response", "response_sha256": digest(value),
                              "elapsed_s": time.time() - started}) + "\n")
        return response

    def call(self, model, messages, tools, **kwargs):
        from roborsi.embodied.agent_loop.vlm_io import _call_vlm_tools
        messages = normalize_image_mime(messages)
        return self._record("chat", {"model": model, "messages": messages, "tools": tools, **kwargs},
                            lambda: _call_vlm_tools(model, messages, tools, **kwargs))

    def role(self, *args, **kwargs):
        from roborsi.agents.sessions.persistent_agent import run_role
        return self._record("role:" + args[0], {"args": args, "kwargs": kwargs}, lambda: run_role(*args, **kwargs))

    def review(self, model, system_prompt, user_block, workspace_root):
        from roborsi.agents.tools.reviewer import run_review_agent
        return run_review_agent(model, system_prompt, user_block, workspace_root, model_client=self)


class Skills:
    def __init__(self, config, bindings, runtime):
        self.config = config

    def specs(self, **kwargs):
        from roborsi.embodied.agent_loop.prompt_tools import _build_tool_specs
        if kwargs.get("ns", "libero") != "libero":
            raise ValueError("This frozen skill pack only supports LIBERO")
        return _build_tool_specs(**kwargs)

    def catalog(self, task, user_msg, ns):
        from roborsi.agents.roles.planner import _skill_catalog
        return _skill_catalog(task, user_msg, ns)

    def dispatch_native(self, state, call):
        from roborsi.embodied.agent_loop.rollout import _dispatch_with_timeout
        return _dispatch_with_timeout(state, call, timeout_s=600)


class ComposedSkills:
    """Combine native skill packs; replacements require an explicit allowlist."""
    is_composed = True

    def __init__(self, config, bindings, runtime):
        self.base, self.extension = bindings["base"], bindings["extension"]
        self.overrides = set(config.get("overrides", []))

    def _routes(self, **kwargs):
        routes = {}
        for owner in [self.base, self.extension]:
            for spec in owner.specs(**kwargs):
                name = spec["function"]["name"]
                if name in routes and name not in self.overrides:
                    raise ValueError("Duplicate skill requires explicit override: " + name)
                routes[name] = (owner, spec)
        if self.overrides - set(routes):
            raise ValueError("Override names are absent from composed catalog")
        return routes

    def specs(self, **kwargs):
        return [spec for owner, spec in self._routes(**kwargs).values()]

    def catalog(self, task, user_msg, ns):
        from roborsi.agents.tools.planner import render_contract
        return "\n".join(render_contract(spec["function"]) for spec in self.specs(ns=ns, task=task))

    def dispatch_native(self, state, call):
        owner, _ = self._routes(ns=state.ns, task=state.task)[call["tool"]]
        return owner.dispatch_native(state, call)


class Context:
    def __init__(self, config, bindings, runtime):
        self.skills, self.config = bindings["skills"], config

    def wiki(self, task):
        from roborsi.agents.memory.task_wiki import read_wiki
        return read_wiki(task)

    def recent_plans(self, task, n=3):
        from roborsi.agents.memory.plan_archive import get_recent_plans
        return get_recent_plans(task, n=n)

    def persistent_plan(self, task):
        from roborsi.agents.roles.planner import persistent_plan_input
        return persistent_plan_input(task)

    def task_history(self, task):
        from roborsi.agents.roles.reviewer import _task_history_block
        return _task_history_block(task)

    def initial_messages(self, *args, **kwargs):
        from roborsi.embodied.agent_loop.messages import _initial_messages
        messages = _initial_messages(*args, **kwargs)
        if getattr(self.skills, "is_composed", False):
            messages[0]["content"] += "\n\nCURRENT COMPOSED TOOL CATALOG (binding):\n" + self.skills.catalog(
                kwargs.get("task_name", "libero_pick_place"), "", kwargs.get("ns", "libero"))
        return messages

    def summarize(self, *args, **kwargs):
        from roborsi.embodied.agent_loop.messages import _summarize_old_trace
        return _summarize_old_trace(*args, **kwargs)


class Environment:
    backend_name = "libero-pro"

    def __init__(self, config, bindings, runtime):
        self.config, self.runtime = config, runtime
        self.client = Client(runtime["endpoint"], runtime["credentials"])
        self.keeper = LeaseKeeper(self.client, config["resource_id"])
        self.keeper.__enter__()
        self.episode_id, self.instruction = None, ""
        self.closed = False

    def _command(self, capability, args):
        command_id, result = self.client.command(self.keeper, "roborsi." + capability + "/v1", args)
        # Read the terminal state attached to this exact command record. A
        # separate latest-state query could race a subsequent operation.
        state = result["result"]["state"]["data"]
        with (Path(self.runtime["run_dir"]) / "gateway-commands.jsonl").open("a") as f:
            f.write(canonical({"command_id": command_id, "capability": capability,
                              "status": result["status"], "episode_id": self.episode_id,
                              "request_sha256": result["_request_sha256"]}) + "\n")
        return state["reply"]

    def reset(self, seed, *, task=None, skill_identity=None):
        import uuid
        if self.episode_id is not None:
            raise RuntimeError("Multi-round RoboRSI must not reset its active scene")
        self.episode_id = uuid.uuid4().hex
        self.task = task
        reply = self._command("reset", {"episode_id": self.episode_id, "task": task,
            "seed": seed, "skill_identity": skill_identity, "run_dir": str(Path(self.runtime["run_dir"]).resolve())})
        self.instruction = reply["instruction"]
        return restore_obs(reply["observation"])

    def take_snapshot(self):
        reply = self._command("snapshot", {"episode_id": self.episode_id})
        return restore_obs(reply["observation"])

    def execute_tool(self, call, workdir, task, last_image_path):
        return self._command("tool", {"episode_id": self.episode_id, "call": call,
            "workdir": str(workdir), "task": task, "last_image_path": str(last_image_path) if last_image_path else None})

    def finalize(self):
        return self._command("adjudicate", {"episode_id": self.episode_id})

    def list_artifacts(self):
        from urllib.parse import urlencode
        return self.client.request("GET", "/v1/artifacts?" + urlencode({"resource_id": self.config["resource_id"]}))

    def artifact_chunk(self, artifact_id, offset, length):
        from urllib.parse import urlencode
        return self.client.request("GET", "/v1/artifact-chunk?" + urlencode({"resource_id": self.config["resource_id"],
            "artifact_id": artifact_id, "offset": offset, "length": length}))

    def check_success(self):
        raise PermissionError("Native simulator verdict is reserved for the final Evaluator")

    def tool_handlers(self):
        return {}

    def hook_physics_step(self, callback):
        # The same capture hook executes beside the native skill in the device
        # process. Captured frames are returned as immutable artifacts.
        return lambda: None

    def close(self):
        if not self.closed:
            self.closed = True
            self.keeper.__exit__(None, None, None)


class Toolset:
    def __init__(self, config, bindings, runtime):
        self.skills, self.environment = bindings["skills"], bindings["environment"]

    def specs(self, **kwargs):
        return self.skills.specs(**kwargs)

    def catalog(self, *args):
        return self.skills.catalog(*args)

    def dispatch(self, state, call, timeout_s=600):
        reply = self.environment.execute_tool(call, state.workdir.resolve(), state.task, state.last_image_path)
        state.last_image_path = Path(reply["last_image_path"]) if reply.get("last_image_path") else state.last_image_path
        from roborsi.embodied.agent_loop.vlm_io import record_remote_usage
        record_remote_usage(reply.get("usage", {}))
        return reply["result"], restore_obs(reply["observation"])


class Evaluator:
    def __init__(self, config, bindings, runtime):
        self.environment = bindings["environment"]

    def adjudicate(self, engineer_result, workspace, task, seed):
        reply = self.environment.finalize()
        # Exercise the existing OS immutable artifact channel and verify bytes;
        # the shared filesystem is only a local placement optimization.
        import base64
        records = self.environment.list_artifacts()
        if records != reply["artifacts"]:
            raise RuntimeError("Native and OS artifact manifests differ")
        for record in records:
            if record["kind"] not in {"video", "score"}:
                continue
            checksum, offset = sha256(), 0
            while offset < record["size_bytes"]:
                chunk = self.environment.artifact_chunk(record["artifact_id"], offset, min(1048576, record["size_bytes"] - offset))
                data = base64.b64decode(chunk["data_base64"], validate=True)
                if not data or chunk["artifact"] != record or chunk["offset"] != offset:
                    raise RuntimeError("OS artifact stream identity/range changed")
                checksum.update(data)
                offset += len(data)
            if checksum.hexdigest() != record["sha256"] or offset != record["size_bytes"]:
                raise RuntimeError("OS artifact checksum differs")
        predicate = reply["success"]
        declared = bool(engineer_result.get("rollout_meta", {}).get("vlm_declared"))
        outcome = ("vlm_declared_done" if declared else "predicate_passed_without_done") if predicate else (
            "vlm_overclaimed" if declared else engineer_result.get("outcome", "budget_exceeded"))
        result = {"success": predicate, "verified": True, "outcome": outcome,
                  "native": reply, "task": task, "seed": seed, "native_artifact_transport_verified": True}
        (workspace.root / "episode_result.json").write_text(json.dumps(result, indent=2))
        return result


class Results:
    def __init__(self, config, bindings, runtime):
        self.root = Path(runtime["run_dir"])
        self.run_id = runtime.get("run_id", self.root.parent.name)

    def workspace(self, task):
        from roborsi.agents.memory.workspace import Workspace
        path = self.root / "episode"
        path.mkdir()
        return Workspace(task=task, run_id=self.run_id, root=path)

    def event(self, kind, **kwargs):
        with (self.root / "events.jsonl").open("a") as f:
            f.write(canonical({"kind": kind, "time": time.time(), **kwargs}) + "\n")

    def finish(self, result):
        from roborsi.store import trace_db
        identity = result["experiment"]
        trace_db.insert_run(self.run_id, task=identity["atomic"], seed=identity["seed"], run_mode="frozen")
        trace_db.update_run(self.run_id, status="success" if result["success"] else "failed",
            outcome=result["outcome"], video_path=result["native"].get("video"),
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            episode_summary={"usage": result["usage"], "tool_calls": result["tool_calls"],
                             "run_mode": "frozen", "sim_task": identity["sim_task"], "native": result["native"]})
        files = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and p.suffix in {".jpg", ".mp4", ".md", ".json", ".jsonl"} and p.name != "result.json":
                files[p.relative_to(self.root).as_posix()] = {"sha256": sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size}
        result["artifacts"] = files
        (self.root / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
        return result


class Orchestrator:
    """Original frozen multi-round protocol with explicit role/service ports."""
    def __init__(self, config, bindings, runtime):
        self.config, self.parts = config, bindings

    def run(self, experiment):
        from roborsi.runtime_mode import use_run_mode
        from roborsi.embodied.agent_loop.vlm_io import capture_usage, merge_usage
        from roborsi.channels.core.agent import _public_engineer_result, _save_round_view
        parts = self.parts
        results, env = parts["results"], parts["environment"]
        task, seed = experiment["atomic"], experiment["seed"]
        workspace = results.workspace(task)
        os.environ["ROBORSI_CURRENT_SIM_TASK"] = experiment["sim_task"]
        identity = {**experiment, "run_id": workspace.run_id, "run_mode": "frozen", "backend": "libero-pro"}
        (workspace.root / "episode_identity.json").write_text(json.dumps(identity, indent=2))
        usages, reviews, trace = {"planner": [], "engineer": [], "reviewer": []}, [], []
        budget_left, feedback = experiment["tool_budget"], ""
        with use_run_mode("frozen"):
            specs = parts["tools"].specs(ns="libero", task=task)
            env.reset(seed, task=experiment["sim_task"], skill_identity=digest(specs))
            request = experiment.get("instruction", "Complete the current task using the released skills.")
            request += "\n\n=== RUNTIME TASK INSTRUCTION ===\n" + env.instruction
            for round_number in range(1, self.config["max_rounds"] + 1):
                results.event("round_start", round=round_number, budget_left=budget_left)
                with capture_usage() as usage:
                    mission = parts["planner"].plan(task=task, user_msg=request + feedback,
                        recent_reflections="", workspace=workspace, ns="libero")
                usages["planner"].append(usage)
                with capture_usage() as usage:
                    eng = parts["engineer"].execute(mission_spec=mission, workspace=workspace,
                        seed=seed, tool_budget=budget_left, backend_name="libero-pro",
                        sim_task=experiment["sim_task"], env=env, task_instruction=env.instruction,
                        reset_env=False, record_memory=False, adjudicate=False)
                usages["engineer"].append(usage)
                calls = int(eng.get("tool_calls") or 0)
                trace.extend(eng.get("trace") or [])
                budget_left -= max(1, calls)
                _save_round_view(env, workspace.root)
                # Final native verdict is deliberately unavailable to every role.
                try:
                    with capture_usage() as usage:
                        review = parts["reviewer"].review(workspace=workspace,
                            engineer_result=_public_engineer_result(eng), run_id=workspace.run_id,
                            ns="libero", in_episode=True, task_instruction=env.instruction)
                except Exception as exc:
                    # Original atomic evaluation retains its simulator verdict
                    # even if Reviewer is unavailable. Preserve that behavior;
                    # never turn a review outage into a fabricated task result.
                    review = {"verdict": "unavailable", "root_cause": type(exc).__name__,
                              "next_action": "", "proposal_decision": "NO_PROPOSAL"}
                    workspace.write_review("# Reviewer unavailable\n\n" + type(exc).__name__ + "\n")
                    results.event("reviewer_error", error_type=type(exc).__name__)
                usages["reviewer"].append(usage)
                reviews.append(review)
                round_dirs = sorted((workspace.root / "rollout").glob("round_*"))
                if round_dirs and workspace.review_path.exists():
                    (round_dirs[-1] / "review.md").write_text(workspace.review_path.read_text())
                results.event("round_complete", round=round_number, tool_calls=calls,
                              budget_left=budget_left, review_verdict=review.get("verdict"))
                if review.get("verdict") != "continue" or budget_left < 10:
                    break
                feedback = (f"\n\n=== REVIEW OF ROUND {round_number} IN THIS EPISODE ===\n"
                    f"Root cause: {review.get('root_cause')}\nNext action: {review.get('next_action')}\n"
                    "The scene was NOT reset: all effects of previous actions persist. "
                    "Re-observe the current scene first, plan only the remaining work, "
                    "and avoid repeating the approach that failed.")
            eng.update(trace=trace, tool_calls=len(trace))
            verdict = parts["evaluator"].adjudicate(eng, workspace, experiment["sim_task"], seed)
        return results.finish({**verdict, "status": "success" if verdict["success"] else "task_failure",
            "run_id": workspace.run_id,
            "rounds": len(reviews), "tool_calls": len(trace), "reviews": reviews,
            "usage": {k: merge_usage(*v) for k, v in usages.items()}, "experiment": identity,
            "scope": "one_task_frozen_evaluation", "workspace": str(workspace.root)})
