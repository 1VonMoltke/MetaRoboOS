#!/usr/bin/env python3
"""Independently audit a single real composed RoboRSI/LIBERO run."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess


def read(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def identity_alive(identity):
    if not identity:
        return False
    try:
        fields = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == identity["start_ticks"] and (
            Path("/proc/sys/kernel/random/boot_id").read_text().strip() == identity["boot_id"].strip())
    except (FileNotFoundError, ProcessLookupError):
        return False


def verify_media(path):
    """Probe and actually decode the native video, retaining compact evidence."""
    probe, decoder = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not path or not probe or not decoder:
        return {"verified": False, "reason": "Video, ffprobe or ffmpeg unavailable"}
    try:
        result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=codec_name,width,height,nb_frames", "-of", "json", path],
            capture_output=True, text=True, timeout=180, check=True)
        metadata = json.loads(result.stdout)
        decoded = subprocess.run([decoder, "-v", "error", "-i", path, "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180)
        streams = metadata.get("streams", [])
        valid = bool(streams) and all(s.get("width", 0) > 0 and s.get("height", 0) > 0 for s in streams)
        return {"verified": valid and decoded.returncode == 0 and not decoded.stderr.strip(),
            "metadata": metadata, "decode_exit_code": decoded.returncode, "decode_errors": decoded.stderr[-1000:]}
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        return {"verified": False, "reason": type(exc).__name__}


def verify(run, *, campaign_episode=False):
    outer, result = read(run / "run.json"), read(run / "results/result.json")
    commands = lines(run / "results/gateway-commands.jsonl")
    events = lines(run / "results/model-events.jsonl")
    score = read(run / "results/native/score.json")
    assertions = {}
    assertions["outer_completed"] = outer["status"] == "application_completed"
    assertions["owned_process_cleanup"] = outer["cleanup_confirmed"] is True
    native_processes = [read(p) for p in (run / "managed").rglob("native-process.json")]
    assertions["native_owned_process_closed"] = len(native_processes) == 1 and all(p["closed"] and p["exit_code"] is not None for p in native_processes)
    assertions["native_finalized_without_force"] = len(native_processes) == 1 and all(
        p["exit_code"] == 0 and p.get("reason") == "episode_finalized" and p.get("forced") is False
        for p in native_processes)
    worker_shutdowns = [read(p) for p in (run / "managed").rglob("worker-shutdown.json")]
    assertions["worker_handles_closed"] = len(worker_shutdowns) == 1 and all(
        p["handles_closed"] is True and p["active_leases"] == 0 for p in worker_shutdowns)
    owned = [p["identity"] for p in native_processes]
    owned.extend(read(p)["identity"] for p in (run / "managed").rglob("worker-process.json"))
    owned.extend({"pid": p["pid"], "start_ticks": p["identity"][0], "boot_id": p["identity"][1]}
        for p in outer["processes"])
    assertions["owned_identities_exited"] = bool(owned) and not any(identity_alive(p) for p in owned)
    assertions["owned_pyroki_closed"] = read(run / "pyroki-process.json")["closed"] is True
    assertions["harness_lease_released"] = read(run / "results/harness-cleanup.json")["lease_released"] is True
    assertions["one_reset"] = sum(c["capability"] == "reset" for c in commands) == 1
    assertions["one_final_adjudication"] = sum(c["capability"] == "adjudicate" for c in commands) == 1 and commands[-1]["capability"] == "adjudicate"
    assertions["gateway_exercised_tools"] = any(c["capability"] == "tool" and c["status"] == "succeeded" for c in commands)
    authority = {}
    for path in (run / "managed").rglob("state.sqlite3"):
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            for identifier, checksum, record in database.execute("SELECT id,digest,record FROM commands"):
                authority[identifier] = (checksum, json.loads(record))
    assertions["worker_authority_matches_commands"] = bool(authority) and set(authority) == {c["command_id"] for c in commands} and all(
        authority[c["command_id"]][0] == c["request_sha256"] and authority[c["command_id"]][1]["status"] == "succeeded" for c in commands)
    assertions["planner_real_response"] = any(e["kind"] == "role:planner" and e["status"] == "response" for e in events)
    assertions["model_real_chat_responses"] = sum(e["kind"] == "chat" and e["status"] == "response" for e in events) >= 2
    assertions["reviewer_real_usage"] = result["usage"]["reviewer"]["vlm_calls"] > 0
    assertions["engineer_real_usage"] = result["usage"]["engineer"]["vlm_calls"] > 0
    if campaign_episode:
        # A real agent can legitimately fail before its first reachable motion.
        # Excluding that failure would bias a campaign's success denominator.
        assertions["native_tick_count_valid"] = type(score["physics_ticks"]) is int and score["physics_ticks"] >= 0
    else:
        assertions["native_physics_exercised"] = score["physics_ticks"] > 0
    assertions["joint_control_preserved"] = score["controller"] == "JOINT_POSITION" and score["action_dim"] == 8
    assertions["native_predicate_once"] = score["final_adjudications"] == 1
    assertions["honest_native_verdict"] = result["success"] == score["success"] and score["score"] == float(result["success"])
    assertions["frozen_one_task"] = result["scope"] == "one_task_frozen_evaluation" and result["experiment"]["run_mode"] == "frozen"
    assertions["video_present"] = bool(score["video"]) and Path(score["video"]).stat().st_size > 0
    media = verify_media(score["video"])
    assertions["video_decodes"] = media["verified"]
    assertions["os_artifact_channel_verified"] = result["native_artifact_transport_verified"] is True
    assertions["artifact_hashes_match"] = all(
        hashlib.sha256((run / "results" / ref).read_bytes()).hexdigest() == info["sha256"]
        for ref, info in result["artifacts"].items())
    # The public tool trace must not contain a final native verdict inserted by
    # the transport. Native verdict is separately written only after Reviewer.
    assertions["no_native_verdict_in_role_trace"] = True
    for trace in (run / "results/episode/rollout").glob("round_*/*/trace.json"):
        for entry in read(trace):
            value = entry.get("result") or {}
            if isinstance(value, dict) and any(k in value for k in ["predicate_check", "native_score", "native_success"]):
                assertions["no_native_verdict_in_role_trace"] = False
    report = {"api": "metaos.roborsi-evidence/v1", "run": str(run.absolute()),
        "bundle_fingerprint": outer["bundle_fingerprint"], "harness_identity": read(run / "results/harness-identity.json"),
        "verified": all(assertions.values()), "checks": assertions, "task_success": score["success"],
        "task": score["task"], "seed": score["seed"], "physics_ticks": score["physics_ticks"],
        "tool_calls": result["tool_calls"], "model_responses": sum(e["status"] == "response" for e in events),
        "video_validation": media,
        "result_sha256": hashlib.sha256((run / "results/result.json").read_bytes()).hexdigest(),
        "score_sha256": hashlib.sha256((run / "results/native/score.json").read_bytes()).hexdigest(),
        "limitations": ["One task and seed only", "External API model alias is mutable", "No campaign or statistical reproduction claim"]}
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-episode", action="store_true",
        help="Accept a scored task failure with zero motion; retain all other episode checks")
    args = parser.parse_args()
    report = verify(args.run, campaign_episode=args.campaign_episode)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["verified"] else 1)
