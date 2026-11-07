#!/usr/bin/env python3
"""Start the installed original Agent HTTP service and reset real ICL; never act."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from metaos_runtime.process import alive, process_identity, terminate
from roborsi.evaluation.http_agent import HttpAgentLauncher
from roborsi.evaluation.messages import Task
from roborsi.utils.daemon import pick_free_port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("packages", "tokenizer", "task", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    task = Task.from_dict(json.loads(args.task.read_text()))
    port = pick_free_port()
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "reset-only-no-inference"
    argv = [sys.executable, "-I", "-B", "-m", "metaos_libero_harness.agent_service", "--packages", str(args.packages.resolve()),
        "--tokenizer", str(args.tokenizer.resolve()), "--case-dir", str(output / "agent"), "--media-dir", str(output / "media"),
        "--port", str(port), "--model", "reset-only-no-inference", "--model-base-url", "http://127.0.0.1:9/v1"]
    report = {"status": "running", "scope": "original_agent_service_reset_only", "model_inference": False,
        "python": sys.executable, "argv": argv, "task_id": task.task_id}
    process = identity = None
    try:
        with (output / "agent-service.log").open("w") as stream:
            process = subprocess.Popen(argv, env=env, stdout=stream, stderr=subprocess.STDOUT, cwd=output, start_new_session=True)
        identity = process_identity(process.pid)
        with HttpAgentLauncher(f"http://127.0.0.1:{port}", startup_timeout_s=90.).start(output, media_dir=output / "media") as session:
            session.client.reset(task, "reset-only-episode", timeout_s=60.)
            session.client.close()
        context = json.loads((output / "agent/context-package.json").read_text())
        runtime = json.loads((output / "agent/agent-runtime.json").read_text())
        if Path(runtime["prefix"]).absolute() != Path(sys.prefix).absolute():
            raise AssertionError("Original Agent lost the requested virtual environment")
        if context["task_id"] != task.task_id:
            raise AssertionError("Original Agent selected another task's ICL")
        report.update(status="passed", selected_context=context, runtime=runtime,
            tokenizer_sha256=hashlib.sha256(args.tokenizer.read_bytes()).hexdigest())
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if process is not None:
            report["cleanup"] = terminate(identity, 5.)
            process.wait(5)
            report["agent_exit_code"] = process.returncode
            report["process_closed"] = not alive(identity)
            if not report["process_closed"]:
                report["status"] = "failed"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "model_inference": False, "report": str(output / "report.json")}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
