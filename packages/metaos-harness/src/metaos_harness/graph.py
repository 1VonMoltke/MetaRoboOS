from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
import json
import re
import sys

from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def distribution_identity(name):
    dist = metadata.distribution(name)
    files = {}
    for item in dist.files or ():
        path = Path(dist.locate_file(item))
        if str(item).endswith(".pth"):
            raise ValueError("Editable/path-injected distributions cannot be locked: " + name)
        if str(item).endswith("direct_url.json"):
            if json.loads(path.read_text()).get("dir_info", {}).get("editable"):
                raise ValueError("Editable distribution cannot be locked: " + name)
            continue
        if path.suffix in {".py", ".json", ".yaml", ".md"} or path.name in {"METADATA", "entry_points.txt"}:
            if not path.is_file() or path.is_symlink():
                raise ValueError("Missing or linked installed file: " + str(item))
            files[str(item)] = sha256(path.read_bytes()).hexdigest()
    return {"name": dist.metadata["Name"], "version": dist.version, "files": files}


@dataclass(frozen=True)
class Descriptor:
    id: str
    version: str
    provides: str
    requires: dict[str, str] = field(default_factory=dict)
    config_schema: dict = field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    semantics: dict = field(default_factory=dict)
    port_semantics: dict = field(default_factory=dict)
    implementation_distributions: tuple[str, ...] = ()


@runtime_checkable
class PlannerAPI(Protocol):
    def plan(self, **kwargs) -> dict: ...


@runtime_checkable
class EngineerAPI(Protocol):
    def execute(self, **kwargs) -> dict: ...


@runtime_checkable
class ReviewerAPI(Protocol):
    def review(self, **kwargs) -> dict: ...


@runtime_checkable
class ModelAPI(Protocol):
    def call(self, model, messages, tools, **kwargs): ...
    def role(self, *args, **kwargs) -> str: ...
    def review(self, model, system_prompt, user_block, workspace_root) -> str: ...


@runtime_checkable
class ContextAPI(Protocol):
    def wiki(self, task) -> str: ...
    def recent_plans(self, task, n=3) -> list: ...
    def persistent_plan(self, task) -> tuple: ...
    def task_history(self, task) -> str: ...
    def initial_messages(self, *args, **kwargs) -> list: ...
    def summarize(self, *args, **kwargs) -> list: ...


@runtime_checkable
class SkillsAPI(Protocol):
    def specs(self, **kwargs) -> list: ...
    def catalog(self, task, user_msg, ns) -> str: ...
    def dispatch_native(self, state, call) -> tuple: ...


@runtime_checkable
class ToolsetAPI(Protocol):
    def specs(self, **kwargs) -> list: ...
    def catalog(self, task, user_msg, ns) -> str: ...
    def dispatch(self, state, call, timeout_s=600) -> tuple: ...


@runtime_checkable
class EpisodeAPI(Protocol):
    def reset(self, seed, *, task=None, skill_identity=None): ...
    def take_snapshot(self): ...
    def execute_tool(self, call, workdir, task, last_image_path): ...
    def finalize(self) -> dict: ...
    def list_artifacts(self) -> list: ...
    def artifact_chunk(self, artifact_id, offset, length) -> dict: ...
    def close(self): ...


@runtime_checkable
class EvaluatorAPI(Protocol):
    def adjudicate(self, engineer_result, workspace, task, seed) -> dict: ...


@runtime_checkable
class ResultsAPI(Protocol):
    def workspace(self, task): ...
    def event(self, kind, **kwargs): ...
    def finish(self, result) -> dict: ...


@runtime_checkable
class OrchestratorAPI(Protocol):
    def run(self, experiment) -> dict: ...


CONTRACT_PROTOCOLS = {"metaos.roborsi." + name + "/v1": api for name, api in {
    "planner": PlannerAPI, "engineer": EngineerAPI, "reviewer": ReviewerAPI, "model": ModelAPI,
    "context": ContextAPI, "skills": SkillsAPI, "tools": ToolsetAPI, "episode": EpisodeAPI,
    "evaluator": EvaluatorAPI, "results": ResultsAPI, "orchestrator": OrchestratorAPI}.items()}


class Provider:
    """Four explicit lifecycle steps; open receives only declared bindings."""
    def __init__(self, descriptor: Descriptor, factory):
        self.descriptor, self.factory = descriptor, factory

    def describe(self):
        return asdict(self.descriptor)

    def plan(self, config):
        Draft202012Validator(self.descriptor.config_schema).validate(config)
        return {"descriptor": self.describe(), "config": json.loads(canonical(config))}

    def prepare(self, plan):
        if plan != self.plan(plan["config"]):
            raise ValueError("Component plan changed")
        return plan

    def open(self, prepared, bindings, runtime):
        if set(bindings) != set(self.descriptor.requires):
            raise ValueError("Component bindings differ from descriptor")
        return self.factory(prepared["config"], bindings, runtime)


class Registry:
    def __init__(self, providers=None):
        self.entries, self.providers, self.identities = {}, dict(providers or {}), {}
        if providers is None:
            for entry in metadata.entry_points(group="metaos.harness.components"):
                if entry.name in self.entries:
                    raise ValueError("Duplicate Harness component: " + entry.name)
                self.entries[entry.name] = entry

    def get(self, name):
        if name not in self.providers:
            if name not in self.entries:
                raise ValueError("Harness component not installed: " + name)
            provider = self.entries[name].load()()
            if provider.describe()["id"] != name:
                raise ValueError("Entry point and descriptor disagree: " + name)
            self.providers[name] = provider
        return self.providers[name]

    def identity(self, name):
        if name not in self.entries:
            return {"test_registry": True}
        package = self.entries[name].dist.metadata["Name"]
        if package not in self.identities:
            self.identities[package] = distribution_identity(package)
        return self.identities[package]


@dataclass
class Resolved:
    spec: dict
    order: list[str]
    plans: dict
    lock: dict
    registry: Registry


def resolve(spec, registry=None):
    if set(spec) - {"api", "name", "components", "entrypoint"} or spec.get("api") != "metaos.harness-spec/v1":
        raise ValueError("Expected metaos.harness-spec/v1")
    nodes = spec.get("components", {})
    if not nodes or spec.get("entrypoint") not in nodes:
        raise ValueError("Harness needs components and a valid entrypoint")
    registry = registry or Registry()
    plans, providers, order, visiting = {}, {}, [], set()
    identities = {}
    for alias, node in nodes.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", alias) or set(node) - {"component", "version", "config", "bindings"}:
            raise ValueError("Invalid component node: " + alias)
        provider = registry.get(node["component"])
        description = provider.describe()
        if not SpecifierSet(node.get("version", "")).contains(description["version"]):
            raise ValueError("Component version mismatch: " + alias)
        bindings = node.get("bindings", {})
        if set(bindings) != set(description["requires"]):
            raise ValueError("Missing or extra ports: " + alias)
        plans[alias] = provider.plan(node.get("config", {}))
        providers[alias] = provider
        identity = registry.identity(node["component"])
        identities[digest(identity)] = identity
        for name in description.get("implementation_distributions", ()):
            if name not in registry.identities:
                registry.identities[name] = distribution_identity(name)
            identity = registry.identities[name]
            identities[digest(identity)] = identity

    def visit(alias):
        if alias in visiting:
            raise ValueError("Dependency cycle at " + alias)
        if alias in order:
            return
        visiting.add(alias)
        node, description = nodes[alias], providers[alias].describe()
        for port, target in node.get("bindings", {}).items():
            if target not in nodes:
                raise ValueError(f"Unbound {alias}.{port}: {target}")
            supplied = providers[target].describe()["provides"]
            required = description["requires"][port]
            if supplied != required:
                raise ValueError(f"Incompatible {alias}.{port}: requires {required}, got {supplied}")
            actual_semantics = providers[target].describe()["semantics"]
            for key, expected in description.get("port_semantics", {}).get(port, {}).items():
                if actual_semantics.get(key) != expected:
                    raise ValueError(f"Semantic mismatch {alias}.{port}.{key}: requires {expected}, got {actual_semantics.get(key)}")
            visit(target)
        visiting.remove(alias)
        order.append(alias)

    for alias in nodes:
        visit(alias)
    # Reject dead nodes: a declared module must actually contribute to the run.
    reachable = set()
    def mark(alias):
        if alias not in reachable:
            reachable.add(alias)
            for target in nodes[alias].get("bindings", {}).values():
                mark(target)
    mark(spec["entrypoint"])
    if reachable != set(nodes):
        raise ValueError("Disconnected components: " + ", ".join(sorted(set(nodes) - reachable)))
    lock = {"api": "metaos.harness-lock/v1", "spec": spec, "order": order,
            "plans": plans, "distributions": identities}
    return Resolved(spec, order, plans, lock, registry)


class Host:
    def __init__(self, resolved, runtime=None, expected_lock=None):
        self.resolved, self.runtime = resolved, runtime or {}
        if expected_lock is not None and canonical(resolved.lock) != canonical(expected_lock):
            raise ValueError("Installed components or declaration differ from frozen lock")
        self.stack, self.instances = ExitStack(), {}

    def __enter__(self):
        try:
            for alias in self.resolved.order:
                node = self.resolved.spec["components"][alias]
                provider = self.resolved.registry.get(node["component"])
                prepared = provider.prepare(self.resolved.plans[alias])
                bindings = {port: self.instances[target] for port, target in node.get("bindings", {}).items()}
                value = provider.open(prepared, bindings, self.runtime)
                self.instances[alias] = value
                if callable(getattr(value, "close", None)):
                    def close_component(exc_type, exc, tb, close=value.close, name=alias):
                        try:
                            close()
                        except Exception as cleanup:
                            if exc is None:
                                raise
                            # Keep the execution failure as the primary error;
                            # expose cleanup failure without logging credentials.
                            detail = {"component": name, "error_type": type(cleanup).__name__,
                                      "error_code": getattr(cleanup, "code", None)}
                            errors = getattr(exc, "cleanup_errors", [])
                            exc.cleanup_errors = [*errors, detail]
                            exc.add_note("Component cleanup failed: " + canonical(detail))
                        return False
                    self.stack.push(close_component)
                interface = CONTRACT_PROTOCOLS.get(provider.describe()["provides"])
                if interface is not None and not isinstance(value, interface):
                    raise TypeError("Opened component does not implement its declared Protocol: " + alias)
            return self.instances[self.resolved.spec["entrypoint"]]
        except BaseException:
            self.stack.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)
