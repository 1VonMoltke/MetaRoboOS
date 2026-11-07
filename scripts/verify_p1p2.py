#!/usr/bin/env python3
"""Audit retained P1/P2 infrastructure evidence without claiming real model inference."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def same_process(identity):
    if not identity:
        return False
    try:
        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == identity["start_ticks"] and Path(
            "/proc/sys/kernel/random/boot_id").read_text().strip() == identity["boot_id"]
    except FileNotFoundError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("infrastructure", "harness", "native-harness", "failure-harness", "agent-exit-harness", "model-timeout-harness", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    infrastructure, harness, native_harness = (value.resolve() for value in (args.infrastructure, args.harness, args.native_harness))
    failure_harness = args.failure_harness.resolve()
    fault_roots = {"agent_exit": args.agent_exit_harness.resolve(), "model_timeout": args.model_timeout_harness.resolve()}
    run_roots = (infrastructure / "cold-native", native_harness, failure_harness, *fault_roots.values())
    scan_roots = (infrastructure, harness, native_harness, failure_harness, *fault_roots.values())
    project = Path(__file__).resolve().parents[1]
    checks, evidence = [], {}
    def check(name, condition, **details):
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "status": "passed", **details})
    def document(name, path):
        value = read(path)
        evidence[name] = {"path": str(path), "sha256": sha(path)}
        return value
    report = {"status": "audit_running", "goal_complete": False, "model_inference": False,
        "created_at": datetime.now(timezone.utc).isoformat(), "checks": checks, "evidence": evidence,
        "audit_source": {"path": str(Path(__file__).resolve()), "sha256": sha(__file__)},
        "remaining_acceptance": [{"id": "real_27b_single_task", "status": "blocked_external",
            "reason": "User reported insufficient GPU memory on the remote machine; the 27B model service cannot start",
            "reported_date": "2026-11-04", "required": "Original Agent act/model response, gateway actions, native score/video/trace and cleanup"}]}
    try:
        base = document("infrastructure", infrastructure / "acceptance-summary.json")
        check("all_infrastructure_stages", base["status"] == "infrastructure_passed" and base["native_requested"]
            and all(row["exit_code"] == 0 for row in base["commands"]))
        tests = document("p1p2_tests", infrastructure / "p1p2-tests.json")
        check("process_and_semantics_matrix", tests["status"] == "passed" and tests["count"] >= 20, count=tests["count"])
        native = document("cold_native", infrastructure / "cold-native/report.json")
        check("real_native_full_protocol", native["status"] == "passed" and native["model_inference"] is False
            and native["native_result"]["details"]["completed_env_steps"] == native["task"]["native"]["max_env_steps"] == 2000)
        cold_shutdown = document("native_shutdown", infrastructure / "cold-native/managed/shutdown.json")
        check("native_clean_shutdown", cold_shutdown["handles_closed"] and cold_shutdown["active_leases"] == 0
            and native["manager_exit_code"] == 0 and not (infrastructure / "cold-native/ready.json").exists())
        manifest = document("system_deployment", Path(base["deployment"]["path"]) / "deployment.json")
        check("offline_native_image_and_declared_assets", base["deployment"]["original_input_removed"]
            and manifest["assets"] and all(row["mode"] == "archive" for row in manifest["native_images"]))
        check("lightweight_os_environment", not {"roborsi", "torch", "numpy", "metaos-libero-harness"}.intersection(manifest["packages"]),
            packages=len(manifest["packages"]))
        p0 = document("p0_regression", infrastructure / "p0-regression/acceptance-summary.json")
        check("p0_and_original_demo_regression", p0["status"] == "passed")
        for label, path, minimum in (("p0_system_tests", infrastructure / "p0-regression/producer-tests.json", 36),
                ("p0_sdk_tests", infrastructure / "p0-regression/sdk-only-tests.json", 8)):
            value = document(label, path)
            check(label, value["status"] == "passed" and value["count"] >= minimum, count=value["count"])
        app = document("harness_deployment", harness / "acceptance-summary.json")
        check("separate_relocated_harness", app["status"] == "contracts_inputs_and_relocated_install_passed"
            and app["original_temporary_inputs_removed"])
        app_tests = document("harness_tests", harness / "harness-tests.json")
        check("harness_protocol_media_and_failure_tests", app_tests["status"] == "passed" and app_tests["count"] >= 13, count=app_tests["count"])
        negative = document("harness_delivery_negative_tests", harness / "delivery-negative-tests.json")
        check("harness_delivery_rejects_changed_inputs", negative["status"] == "passed" and len(negative["checks"]) == 4
            and all(row["status"] == "passed" and not row["target_created"] for row in negative["checks"]))
        for name in ("original-inputs", "original-service"):
            value = document(name, harness / name / "report.json")
            check(name, value["status"] == "passed" and value["model_inference"] is False
                and value["selected_context"]["manifest_sha256"] == "e01858b3917ab1e82aab93da2bb9e86f40fd81516236b11461e8a00049704382"
                and value["tokenizer_sha256"] == "0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3"
                and (name != "original-service" or value["process_closed"]))
        runner = document("native_harness", native_harness / "report.json")
        check("real_gateway_original_case_runner", runner["status"] == "passed" and runner["model_inference"] is False
            and runner["result"]["status"] in {"success", "task_failure"} and runner["shutdown"]["handles_closed"])
        directory = Path(runner["case_directory"])
        score = document("harness_original_score", directory / runner["result"]["native_score_source"])
        check("harness_preserves_original_budget", score["completed_env_steps"] == score["official_1x_snapshot"]["env_steps"] == 2000)
        failure = document("original_agent_unavailable_model", failure_harness / "report.json")
        check("actual_cli_model_failure_cancels_and_releases", failure["status"] == "passed" and failure["remote_model_contacted"] is False
            and failure["result"]["status"] == "agent_error" and failure["result"]["score"] is None
            and failure["agent_process_closed"] and failure["replacement_caller_acquired"] and failure["shutdown"]["handles_closed"])
        expected_prefix = str(Path(app["cold_python"]).absolute().parent.parent)
        check("owned_agent_preserves_requested_venv", failure["agent_runtime"]["prefix"] == expected_prefix
            and failure["agent_runtime"]["harness_version"] == app["harness_version"]
            and failure["agent_runtime"]["prefix"] != failure["agent_runtime"]["base_prefix"]
            and (not app["interpreter_symlinks"] or Path(app["cold_python"]).is_symlink()))
        check("past_native_state_is_rejected", failure["past_native_state_rejected"]["error_code"] == "state_changed"
            and failure["past_native_state_rejected"]["request"]["state_sequence"] < failure["past_native_state_rejected"]["current_sequence"])
        for mode, root in fault_roots.items():
            fault = document("original_agent_" + mode, root / "report.json")
            expected_status = "infrastructure_error" if mode == "agent_exit" else "agent_error"
            check("actual_cli_" + mode + "_cancels_and_releases", fault["status"] == "passed"
                and fault["failure_mode"] == mode and fault["model_inference"] is False
                and fault["remote_model_contacted"] is False and fault["cli_exit_code"] == 1
                and fault["local_model_request"]["path"] == "/v1/chat/completions"
                and fault["result"]["status"] == expected_status and fault["result"]["score"] is None
                and fault["capabilities"] == ["simulation.reset/v1", "simulation.cancel_episode/v1"]
                and fault["agent_process_closed"] and fault["replacement_caller_acquired"]
                and fault["shutdown"]["handles_closed"] and fault["shutdown"]["active_leases"] == 0
                and fault["manager_exit_code"] == 0
                and fault["agent_runtime"]["prefix"] == expected_prefix
                and fault["agent_runtime"]["harness_version"] == app["harness_version"]
                and fault["past_native_state_rejected"]["error_code"] == "state_changed"
                and (mode != "agent_exit" or not same_process(fault["killed_agent"]))
                and (mode != "model_timeout" or "time" in fault["result"]["error"]["message"].lower()))
        artifacts = [*native["artifacts"], *runner["result"]["benchmark_artifacts"]]
        check("real_camera_score_and_mp4", any(row["kind"] == "image" for row in artifacts)
            and any(row["kind"] == "native_result" for row in artifacts) and any(row["mime_type"] == "video/mp4" for row in artifacts))
        for row in native["artifacts"]:
            check_path = infrastructure / "cold-native/client-artifacts" / row["ref"]
            if sha(check_path) != row["sha256"]:
                raise AssertionError("Native artifact differs")
        for row in runner["result"]["benchmark_artifacts"]:
            if sha(directory / row["ref"]) != row["sha256"]:
                raise AssertionError("Harness artifact differs")
        check("retained_artifact_bytes", True, artifacts=len(artifacts))
        for name, path in (("cold_native_media", infrastructure / "cold-native/media-validation.json"),
                ("harness_native_media", native_harness / "media-validation.json")):
            media = document(name, path)
            check(name, media["status"] == "passed" and media["images_verified"] > 0 and media["videos"]
                and all(row["full_decode"] == "passed" and int(row["nb_frames"]) >= 2000 for row in media["videos"]),
                images=media["images_verified"], videos=len(media["videos"]))
        base_provenance = read(infrastructure / "wheels/wheel-provenance.json")
        app_provenance = document("harness_wheel_provenance", Path(app["deployment"]) / "wheel-provenance.json")["metaos"]
        # The application can be released independently of the already
        # qualified OS. Compare each installed artifact with its own source.
        provenance = [row for row in base_provenance if row["name"] != "metaos-libero-harness"]
        provenance += [row for row in app_provenance if row["name"] == "metaos-libero-harness"]
        base_packages = {row["name"]: row for row in base_provenance}
        check("harness_uses_qualified_os_wheels", all(row["sha256"] == base_packages[row["name"]]["sha256"]
            for row in app_provenance if row["name"] in {"metaos-runtime", "metaos-contracts"}))
        matched = 0
        for row in provenance:
            if row.get("provenance") != "local-source-wheel":
                continue
            package = next(path for path in [*(project / "packages").iterdir(), *(project / "plugins").iterdir()]
                if (path / "pyproject.toml").exists() and f'name = "{row["name"]}"' in (path / "pyproject.toml").read_text())
            actual_sources = {str(p.relative_to(package)) for p in package.rglob("*") if p.is_file()
                and (p == package / "pyproject.toml" or p.suffix == ".py" and "src" in p.relative_to(package).parts)}
            if actual_sources != set(row["source_files"]):
                raise AssertionError("Current source inventory differs: " + row["name"])
            for relative, digest in row["source_files"].items():
                if sha(package / relative) != digest:
                    raise AssertionError("Current source differs from qualified wheel: " + row["name"] + "/" + relative)
            matched += 1
        check("current_source_matches_qualified_wheels", matched == 6, packages=matched)
        workers = [path for root in scan_roots for path in root.rglob("worker-process.json")]
        check("owned_workers_closed", all(not same_process(read(path)["identity"]) for path in workers), count=len(workers))
        for root in run_roots:
            for journal in root.rglob("native-runtime.json"):
                row = read(journal)
                env = dict(os.environ)
                if row.get("docker_host"):
                    env["DOCKER_HOST"] = row["docker_host"]
                result = subprocess.run(["docker", "ps", "-aq", "--no-trunc", "--filter", "name=^/" + row["name"] + "$"],
                    capture_output=True, text=True, env=env, timeout=30)
                check("owned_native_container_removed", result.returncode == 0 and not result.stdout.strip(), container_name=row["name"])
        secrets = set()
        for root in scan_roots:
            for path in [*root.rglob("credentials.json"), *root.rglob("worker-token.json")]:
                value = read(path)
                if "token" in value:
                    secrets.add(value["token"])
                secrets.update(row["token"] for row in value.get("clients", []))
        leaked = []
        for root in scan_roots:
            for pattern in ("*.log", "*.jsonl"):
                for path in root.rglob(pattern):
                    content = path.read_text(errors="replace")
                    if any(secret in content for secret in secrets):
                        leaked.append(str(path))
        check("credentials_absent_from_logs", not leaked, checked_secret_count=len(secrets))
        report.update(status="infrastructure_qualified_real_27b_blocked", artifacts={
            "producer_python": str(infrastructure / "producer/bin/python"), "os_python": base["deployment"]["cold_python"],
            "system_deployment": base["deployment"]["path"], "harness_python": app["cold_python"], "harness_deployment": app["deployment"],
            "infrastructure_summary": str(infrastructure / "acceptance-summary.json"), "harness_summary": str(harness / "acceptance-summary.json"),
            "native_harness_report": str(native_harness / "report.json"), "failure_harness_report": str(failure_harness / "report.json"),
            "agent_exit_harness_report": str(fault_roots["agent_exit"] / "report.json"),
            "model_timeout_harness_report": str(fault_roots["model_timeout"] / "report.json")})
    except Exception as exc:
        report.update(status="audit_failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"status": report["status"], "goal_complete": False, "output": str(args.output)}))


if __name__ == "__main__":
    main()
