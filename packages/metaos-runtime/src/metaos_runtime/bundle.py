"""Offline package/file closure and explicitly declared native-image delivery."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from metaos_contracts import SystemError

from .composition import canonical, fingerprint, load_spec, resolve


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def native_images(resolved):
    rows = {}
    for plan in resolved.plans.values():
        for row in plan.metadata.get("native_images", []):
            if not isinstance(row, dict) or not isinstance(row.get("image"), str):
                raise SystemError("invalid_plan", "Native images require explicit immutable identities", 400)
            identity = row["image"]
            if not __import__("re").fullmatch(r"sha256:[a-f0-9]{64}", identity):
                raise SystemError("invalid_plan", "Mutable native image references cannot be delivered", 400)
            if identity in rows and rows[identity] != row:
                raise SystemError("invalid_plan", "Conflicting native image transports", 400)
            rows[identity] = row
    return list(rows.values())


def docker(row, *args, output=None):
    env = dict(os.environ)
    if row.get("docker_host"):
        env["DOCKER_HOST"] = row["docker_host"]
    result = subprocess.run(["docker", *args], env=env, stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE, text=output is None, timeout=900)
    if result.returncode:
        raise SystemError("native_image_unavailable", "Native image operation failed; check declared Docker transport", 503)
    return result.stdout.strip() if output is None else None


def preflight_images(rows):
    for row in rows:
        if docker(row, "image", "inspect", row["image"], "--format", "{{.Id}}") != row["image"]:
            raise SystemError("native_image_changed", "Native image identity differs", 503)
    return rows


def target_python():
    return {"implementation": platform.python_implementation(), "version": ".".join(platform.python_version_tuple()[:2]),
            "system": platform.system(), "machine": platform.machine()}


def wheel_index(directory):
    result = {}
    for path in sorted(Path(directory).glob("*.whl")):
        name, version, _, tags = parse_wheel_filename(path.name)
        if name in result or not tags.intersection(sys_tags()):
            raise SystemError("invalid_wheelhouse", "Duplicate or incompatible wheel: " + path.name)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SystemError("invalid_wheel", "Duplicate archive members")
            for info in archive.infolist():
                member = PurePosixPath(info.filename)
                if member.is_absolute() or ".." in member.parts or "\\" in info.filename or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise SystemError("invalid_wheel", "Unsafe wheel member")
            records = [n for n in names if n.endswith(".dist-info/METADATA")]
            if len(records) != 1:
                raise SystemError("invalid_wheel", "Wheel must contain one package metadata record")
            doc = BytesParser().parsebytes(archive.read(records[0]))
        if canonicalize_name(doc["Name"]) != name or doc["Version"] != str(version):
            raise SystemError("invalid_wheel", "Wheel metadata differs from filename")
        if doc["Requires-Python"] and not SpecifierSet(doc["Requires-Python"]).contains(platform.python_version()):
            raise SystemError("incompatible_python", "Wheel requires a different Python")
        result[name] = {"path": path, "version": str(version), "requirements": doc.get_all("Requires-Dist", []), "sha256": sha(path)}
    return result


def closure(directory, requested):
    available, selected = wheel_index(directory), {}
    pending = [Requirement(name) for name in requested]
    while pending:
        request = pending.pop()
        name = canonicalize_name(request.name)
        if request.url or request.extras or name not in available:
            raise SystemError("missing_dependency", "A versioned base wheel is required: " + str(request))
        wheel = available[name]
        if not request.specifier.contains(wheel["version"], prereleases=True):
            raise SystemError("incompatible_dependency", "Dependency version mismatch: " + str(request))
        if name in selected:
            continue
        selected[name] = wheel
        for raw in wheel["requirements"]:
            child = Requirement(raw)
            if child.marker is None or child.marker.evaluate({**default_environment(), "extra": ""}):
                pending.append(child)
    return selected


def verify_installed(wheels):
    owners = set()
    for name, wheel in wheels.items():
        dist = metadata.distribution(name)
        if dist.version != wheel["version"] or any(str(x).endswith(".pth") for x in dist.files or ()):
            raise SystemError("installed_dependency_changed", "Installed distribution differs: " + name)
        with zipfile.ZipFile(wheel["path"]) as archive:
            for member in archive.namelist():
                if member.endswith("/") or member.endswith(".dist-info/RECORD") or ".data/" in member:
                    continue
                path = Path(dist.locate_file(member))
                if member in owners or path.is_symlink() or not path.is_file() or path.read_bytes() != archive.read(member):
                    raise SystemError("installed_dependency_changed", "Installed wheel bytes differ: " + name)
                owners.add(member)


def verify(directory, installed=False, expected=None, native=False):
    directory = Path(directory).resolve()
    manifest = directory / "deployment.json"
    if manifest.is_symlink():
        raise SystemError("artifact_changed", "Linked artifact manifest")
    doc = json.loads(manifest.read_text())
    if doc.get("api") != "metaos.deployment/v1" or doc.get("schema_version") != 1:
        raise SystemError("unsupported_schema", "Unsupported P0 deployment schema")
    content = {k: v for k, v in doc.items() if k != "fingerprint"}
    if fingerprint(content) != doc["fingerprint"] or expected and expected != doc["fingerprint"]:
        raise SystemError("artifact_changed", "Deployment identity differs")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise SystemError("artifact_changed", "Linked artifact member")
        if path.is_file() and path != manifest:
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(doc["files"]):
        raise SystemError("artifact_changed", "Deployment file inventory differs")
    for name, digest in doc["files"].items():
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name or sha(directory / name) != digest:
            raise SystemError("artifact_changed", "Deployment content differs: " + name)
    if doc["python"] != target_python():
        raise SystemError("incompatible_python", "Deployment targets a different Python/platform")
    if installed:
        verify_installed(closure(directory / "wheels", ["metaos-runtime", *doc["providers"]]))
        planned = resolve(load_spec(directory / "system.json"), directory / "project")
        if planned.fingerprint != doc["system_fingerprint"]:
            raise SystemError("lock_changed", "Installed system reconstructs a different lock")
    if native:
        preflight_images(doc.get("native_images", []))
    return doc


def export(spec_path, root, wheelhouse, output, images="reference"):
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError("Deployment target already exists")
    spec = load_spec(spec_path)
    resolved = resolve(spec, root)
    root = Path(root).resolve()
    assets = {}
    for plan in resolved.plans.values():
        for identity in plan.inputs:
            source = Path(identity.path)
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise SystemError("unsupported_assets", "Declared file assets must be regular files within project root", 400)
            relative = source.relative_to(root).as_posix()
            if any(parent.is_symlink() for parent in source.parents if parent != root):
                raise SystemError("unsupported_assets", "Linked asset directories cannot be delivered", 400)
            assets[relative] = source
    # Reject undeclared absolute file locators rather than silently borrowing files.
    def check_paths(value):
        if isinstance(value, dict):
            for item in value.values():
                check_paths(item)
        elif isinstance(value, list):
            for item in value:
                check_paths(item)
        elif isinstance(value, str) and Path(value).is_absolute():
            raise SystemError("unsupported_assets", "P0 configuration cannot contain undeclared absolute paths")
    check_paths(spec.model_dump(mode="json"))
    providers = sorted({resolved.registry.entries[n.component].dist.metadata["Name"] for n in spec.components.values()})
    wheels = closure(wheelhouse, ["metaos-runtime", *providers])
    verify_installed(wheels)
    declared_images = preflight_images(native_images(resolved))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".p0-export-", dir=output.parent) as temporary:
        staging = Path(temporary) / "deployment"
        (staging / "wheels").mkdir(parents=True)
        (staging / "project").mkdir()
        for relative, source in assets.items():
            destination = staging / "project" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        for wheel in wheels.values():
            shutil.copyfile(wheel["path"], staging / "wheels" / wheel["path"].name)
        (staging / "system.json").write_text(spec.model_dump_json(indent=2) + "\n")
        (staging / "system.lock.json").write_text(json.dumps(resolved.lock, indent=2) + "\n")
        (staging / "requirements.lock").write_text("".join(
            f"{name}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n" for name, wheel in sorted(wheels.items())))
        provenance_path = Path(wheelhouse) / "wheel-provenance.json"
        provenance = []
        if provenance_path.exists():
            for row in json.loads(provenance_path.read_text()):
                name = canonicalize_name(row["name"])
                if name in wheels:
                    if row["sha256"] != wheels[name]["sha256"]:
                        raise SystemError("provenance_changed", "Wheel source record differs")
                    provenance.append(row)
        (staging / "wheel-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        shutil.copyfile(Path(__file__).with_name("install.py"), staging / "install.py")
        delivered_images = []
        for row in declared_images:
            delivered = {**row, "mode": images}
            if images == "archive":
                relative = "images/" + row["image"].split(":")[1] + ".tar"
                (staging / "images").mkdir(exist_ok=True)
                with (staging / relative).open("wb") as stream:
                    docker(row, "image", "save", row["image"], output=stream)
                delivered["archive"] = relative
            delivered_images.append(delivered)
        doc = {"api": "metaos.deployment/v1", "schema_version": 1, "python": target_python(),
               "providers": providers, "system_fingerprint": resolved.fingerprint,
               "native_images": delivered_images,
               "assets": {name: sha(source) for name, source in sorted(assets.items())},
               "packages": {name: {"version": wheel["version"], "sha256": wheel["sha256"]} for name, wheel in wheels.items()},
               "prerequisites": {"transport": "local_http", "credentials": "external_owner_only_file",
                   "device_execution": "declared_device_plugin", "native_images": "offline_archive" if declared_images and images == "archive" else "external_preinstalled" if declared_images else "none",
                   "native_host": "Linux, Docker and compatible NVIDIA driver/runtime/GPU required" if declared_images else None},
               "files": {p.relative_to(staging).as_posix(): sha(p) for p in sorted(staging.rglob("*")) if p.is_file()}}
        doc["fingerprint"] = fingerprint(doc)
        (staging / "deployment.json").write_text(json.dumps(doc, indent=2) + "\n")
        shutil.move(staging, output)
    return verify(output)
