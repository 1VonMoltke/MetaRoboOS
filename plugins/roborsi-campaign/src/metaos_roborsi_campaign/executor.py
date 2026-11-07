"""Run the composed outer bundle once per task; never invoke RoboRSI eval-suite."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys

from metaos_harness.graph import digest
from .panel import INDEX_ENV, validate
from .storage import atomic, read


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def input_identity(config):
    bundle_dir = Path(config["bundle"])
    bundle = read(bundle_dir / "bundle.json")
    if digest({k: v for k, v in bundle.items() if k != "fingerprint"}) != bundle["fingerprint"]:
        raise ValueError("Episode bundle fingerprint mismatch")
    app = bundle["components"][bundle["application"]]
    assets = bundle["components"][app["bindings"]["assets"]]
    panel_path = Path(assets["paths"]["experiment"]["path"])
    panel = validate(read(panel_path))
    if INDEX_ENV not in app["descriptor"].get("env", []) or "metaos_roborsi_campaign.episode" not in app["descriptor"]["command"]:
        raise ValueError("Use prepare_roborsi.py --panel to build a campaign-capable episode bundle")
    native_path = Path(bundle["components"][bundle["system"]]["paths"]["native-runtime"]["path"])
    native = read(native_path)
    if set(native["native_config"]["tasks"]) != set(panel["tasks"]):
        raise ValueError("Panel and native task allowlist differ")
    paths = [bundle_dir / "bundle.json", bundle_dir / "system.lock.json", panel_path, native_path,
             *(Path(config[k]) for k in ["composer", "composer_script", "verifier"])]
    return {"bundle_fingerprint": bundle["fingerprint"], "panel": panel,
        "run_root": native["native_config"]["run_root"],
        "inputs": {str(p): sha(p) for p in paths}}


def process_alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


class EpisodeCancelled(BaseException):
    def __init__(self, result):
        self.result = result


class LocalExecutor:
    def __init__(self, config, bindings, runtime):
        self.config = config
        self.expected = input_identity(config)

    def identity(self):
        current = input_identity(self.config)
        if current != self.expected:
            raise ValueError("Episode inputs changed during the campaign")
        return current

    def panel(self):
        return self.expected["panel"]

    def validate_output(self, output):
        if not Path(output).resolve().is_relative_to(Path(self.expected["run_root"]).resolve()):
            raise ValueError("Campaign output must be below the episode bundle's declared run_root")

    def preflight(self):
        self.identity()
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required before starting a campaign")
        checked = subprocess.run([sys.executable, "-I", "-B", self.config["composer"], "verify", self.config["bundle"]],
            capture_output=True, text=True, timeout=3600)
        if checked.returncode != 0:
            raise ValueError("Episode bundle byte verification failed; rebuild before running")

    def recover(self, index, run):
        self.identity()
        run = Path(run)
        outer_path = run / "run.json"
        if not outer_path.exists():
            if run.exists():
                return {"status": "interrupted", "cleanup_confirmed": False, "reason": "missing_run_report"}
            # Launcher completed before creating any owned runtime resources.
            return {"status": "infrastructure_error", "cleanup_confirmed": True, "reason": "run_not_created"}
        outer = read(outer_path)
        for process in outer.get("processes", []):
            if process_alive(process["pid"]):
                return {"status": "interrupted", "cleanup_confirmed": False, "reason": "owned_process_still_running"}
        if not outer.get("cleanup_confirmed"):
            return {"status": "interrupted", "cleanup_confirmed": False, "reason": "cleanup_unconfirmed"}
        base = {"cleanup_confirmed": True, "outer_status": outer["status"]}
        if outer["status"] != "application_completed":
            error_path = run / "results/harness-error.json"
            if error_path.exists():
                error = read(error_path).get("error_type")
                if error in {"TypeError", "ValueError", "KeyError", "AttributeError", "AssertionError",
                             "NameError", "SyntaxError", "ImportError", "ModuleNotFoundError"}:
                    return {**base, "status": "implementation_error", "reason": error}
            return {**base, "status": "infrastructure_error"}
        evidence = run.parent / "audit.json"
        # The same independent single-task audit checks native score, real
        # roles, Gateway identities, artifacts, video and process retirement.
        try:
            with (run.parent / "audit.log").open("w") as log:
                audited = subprocess.run([sys.executable, "-I", "-B", self.config["verifier"],
                    "--run", str(run), "--output", str(evidence), "--campaign-episode"],
                    stdout=log, stderr=subprocess.STDOUT, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            return {**base, "status": "verification_error", "reason": type(exc).__name__}
        if audited.returncode != 0 or not evidence.exists():
            return {**base, "status": "verification_error", "reason": "episode_audit_failed"}
        report = read(evidence)
        panel = self.panel()
        if (report.get("verified") is not True or report.get("task") != panel["tasks"][index]
                or report.get("seed") != panel["seed"] or report.get("bundle_fingerprint") != self.expected["bundle_fingerprint"]
                or type(report.get("task_success")) is not bool):
            return {**base, "status": "verification_error", "reason": "episode_identity_mismatch"}
        return {**base, "status": "success" if report["task_success"] else "task_failure",
            "evidence": str(evidence), "evidence_sha256": sha(evidence),
            "physics_ticks": report["physics_ticks"], "tool_calls": report["tool_calls"]}

    def execute(self, index, run, *, lock_fd):
        self.identity()
        self.validate_output(run)
        run = Path(run)
        run.parent.mkdir(parents=True, exist_ok=False)
        env = dict(os.environ)
        env[INDEX_ENV] = str(index)
        with (run.parent / "launcher.log").open("w") as log:
            try:
                process = subprocess.Popen([sys.executable, "-I", "-B", self.config["composer"], "run",
                    self.config["bundle"], "--output", str(run), "--timeout", str(self.config.get("timeout_s", 14400))],
                    env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                    # A killed scheduler cannot race a resumed campaign with
                    # its still-running launcher: both retain the same flock.
                    pass_fds=(lock_fd,))
            except OSError as exc:
                return {"status": "infrastructure_error", "cleanup_confirmed": True, "reason": type(exc).__name__}
            try:
                atomic(run.parent / "launcher.json", {"pid": process.pid, "index": index})
                process.wait()
            except BaseException as exc:
                if process.poll() is None:
                    # Signal the composer, which owns and cleans its workers.
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(130)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)
                result = self.recover(index, run)
                result["status"] = "interrupted"
                result["reason"] = type(exc).__name__
                raise EpisodeCancelled(result)
        return self.recover(index, run)
