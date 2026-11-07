"""Generic installed component DAG, deliberately independent of Episode/evaluation."""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet
from metaos_contracts import DeviceAPI, SystemError, SystemSpec
from roborsi.contracts import ComponentContext, ComponentDescriptor, ComponentHandle, ComponentPlan, PreparedComponent
from roborsi.contracts.business import require_capability


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def distribution_identity(name):
    dist = metadata.distribution(name)
    files = {}
    for item in dist.files or ():
        if str(item).endswith(".pth"):
            raise SystemError("editable_dependency", "Path-injected distributions are not supported")
        if str(item).endswith(".dist-info/direct_url.json"):
            locator = json.loads(Path(dist.locate_file(item)).read_text())
            if locator.get("dir_info", {}).get("editable"):
                raise SystemError("editable_dependency", "Editable distributions are not supported")
            # pip writes this installation-origin locator, not wheel code. It
            # must not make the same wheel differ after an offline relocation.
            continue
        if str(item).endswith((".py", ".json", ".yaml", ".dist-info/METADATA", ".dist-info/entry_points.txt")):
            path = Path(dist.locate_file(item))
            if path.is_symlink() or not path.is_file():
                raise SystemError("invalid_distribution", "Installed distribution file is missing or linked")
            files[str(item)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"name": dist.metadata["Name"], "version": dist.version, "files": files}


class Registry:
    def __init__(self):
        self.entries = {}
        self.loaded = {}
        for entry in sorted(metadata.entry_points(group="roborsi.components"), key=lambda x: x.name):
            if entry.name in self.entries:
                raise SystemError("duplicate_component", "Duplicate installed component: " + entry.name)
            self.entries[entry.name] = entry

    def list(self):
        return [{"id": name, "distribution": entry.dist.metadata["Name"]}
                for name, entry in sorted(self.entries.items())]

    def provider(self, component, version=""):
        if component not in self.entries:
            raise SystemError("component_not_found", "Component is not installed: " + component)
        if component not in self.loaded:
            provider = self.entries[component].load()()
            if not all(callable(getattr(provider, name, None)) for name in ("describe", "plan", "prepare", "open")):
                raise SystemError("invalid_plugin", "Provider does not implement the SDK lifecycle")
            descriptor = provider.describe()
            if not isinstance(descriptor, ComponentDescriptor) or descriptor.id != component:
                raise SystemError("invalid_plugin", "Provider descriptor differs from installed entry point")
            self.loaded[component] = provider
        provider = self.loaded[component]
        if not SpecifierSet(version).contains(provider.describe().version, prereleases=True):
            raise SystemError("incompatible_version", "Component version does not match: " + component)
        return provider


def load_spec(path):
    return SystemSpec.model_validate_json(json.dumps(yaml.safe_load(Path(path).read_text()), allow_nan=False))


def portable(value, root):
    if isinstance(value, dict):
        return {k: (canonical(portable(json.loads(v), root)) if k in {"configuration_json", "metadata_json"}
                    and isinstance(v, str) else portable(v, root)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [portable(v, root) for v in value]
    if isinstance(value, str) and Path(value).is_absolute():
        path = Path(value)
        if path.is_relative_to(root):
            return "project:" + path.relative_to(root).as_posix()
    return value


@dataclass
class ResolvedSystem:
    spec: SystemSpec
    registry: Registry
    plans: dict
    order: tuple
    lock: dict
    root: Path

    @property
    def fingerprint(self):
        return fingerprint(self.lock)


def resolve(spec, root, registry=None):
    root = Path(root).resolve()
    registry = registry or Registry()
    providers = {name: registry.provider(node.component, node.version) for name, node in spec.components.items()}
    descriptors = {name: provider.describe() for name, provider in providers.items()}
    visited, active, order = set(), set(), []

    def visit(name):
        if name in active:
            raise SystemError("dependency_cycle", "Component dependency cycle")
        if name in visited:
            return
        active.add(name)
        ports = {port.name: port for port in descriptors[name].dependencies}
        bindings = spec.components[name].bindings
        if set(bindings) - set(ports):
            raise SystemError("unknown_binding", "Unknown dependency port on " + name)
        for port in ports.values():
            if port.name not in bindings:
                if port.optional:
                    continue
                raise SystemError("missing_binding", "Explicit binding required: " + name + "." + port.name)
            target = bindings[port.name]
            if target not in descriptors:
                raise SystemError("missing_component", "Binding target does not exist: " + target)
            matched = False
            for provided in descriptors[target].provides:
                try:
                    require_capability(port.capability, provided)
                    matched = True
                    break
                except RuntimeError:
                    pass
            if not matched:
                raise SystemError("incompatible_capability", "Dependency capability mismatch: " + name + "." + port.name)
            visit(target)
        for required in descriptors[name].requires:
            candidates = [cap for target in bindings.values() for cap in descriptors[target].provides]
            matched = False
            for provided in candidates:
                try:
                    require_capability(required, provided)
                    matched = True
                    break
                except RuntimeError:
                    pass
            if not matched:
                raise SystemError("missing_capability", "Required capability is not bound: " + name)
        active.remove(name)
        visited.add(name)
        order.append(name)

    for name in sorted(spec.components):
        visit(name)
    plans, records, packages = {}, {}, {"metaos-runtime", "metaos-contracts", "roborsi-sdk"}
    for name in order:
        node = spec.components[name]
        descriptor = descriptors[name]
        if not Draft202012Validator(descriptor.config_schema).is_valid(node.config):
            raise SystemError("invalid_config", "Invalid component configuration: " + name)
        context = ComponentContext(root / ".system-plan" / name, root=root)
        plan = providers[name].plan(node, context)
        if not isinstance(plan, ComponentPlan) or plan.descriptor != descriptor:
            raise SystemError("invalid_plan", "Provider returned an inconsistent plan")
        plan.verify_inputs()
        plans[name] = plan
        owner = registry.entries[node.component].dist.metadata["Name"]
        packages.add(owner)
        records[name] = {"plan": portable(plan.model_dump(mode="json"), root), "origin": owner}
    lock = {"api": "metaos.lock/v1", "spec": portable(spec.model_dump(mode="json"), root),
            "order": order, "components": records,
            "distributions": {name: distribution_identity(name) for name in sorted(packages)}}
    return ResolvedSystem(spec, registry, plans, tuple(order), lock, root)


@contextmanager
def open_system(resolved, workspace):
    if resolve(resolved.spec, resolved.root).lock != resolved.lock:
        raise SystemError("lock_changed", "System inputs changed since planning")
    with ExitStack() as stack:
        handles, resources = {}, {}
        for name in resolved.order:
            node, plan = resolved.spec.components[name], resolved.plans[name]
            context = ComponentContext(Path(workspace) / "components" / name, root=resolved.root,
                                       bindings={port: handles[target] for port, target in node.bindings.items()})
            provider = resolved.registry.provider(node.component, node.version)
            plan.verify_inputs()
            prepared = provider.prepare(plan, context)
            if not isinstance(prepared, PreparedComponent) or prepared.plan != plan:
                raise SystemError("invalid_prepared", "Provider changed its plan during prepare")
            handle = provider.open(prepared, context)
            if not isinstance(handle, ComponentHandle) or handle.component_id != node.component:
                if isinstance(handle, ComponentHandle):
                    handle.close()
                raise SystemError("invalid_handle", "Provider returned an invalid handle")
            handles[name] = stack.enter_context(handle)
        domains = set()
        for alias in resolved.spec.exports:
            value = handles[alias].value
            if not isinstance(value, DeviceAPI):
                raise SystemError("invalid_resource", "Export does not implement DeviceAPI: " + alias)
            descriptor = value.describe_resource()
            if descriptor.resource_id in resources:
                raise SystemError("duplicate_resource", "Duplicate exported resource id")
            domain = descriptor.metadata.get("control_domain", descriptor.resource_id)
            if not isinstance(domain, str) or domain in domains:
                raise SystemError("control_domain_conflict", "Overlapping devices and adapters must expose one control authority", 400)
            domains.add(domain)
            resources[descriptor.resource_id] = value
        yield resources
