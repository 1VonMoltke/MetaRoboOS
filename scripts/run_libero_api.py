#!/usr/bin/env python3
"""Run the original installed RoboRSI Agent against an explicit API model."""

import argparse
import json
import os
import signal
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("endpoint", "credentials", "model-name", "model-base-url", "task", "packages", "tokenizer", "store"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--case-timeout", type=int, default=7200)
    parser.add_argument("--agent-set", action="append", default=[])
    args = parser.parse_args()
    if not os.environ.get(args.api_key_env):
        raise ValueError("Set the API credential in environment variable " + args.api_key_env)
    os.environ["OPENAI_API_KEY"] = os.environ[args.api_key_env]
    task = json.loads(Path(args.task).read_text())
    from metaos_libero_harness import main as harness_main
    def interrupted(*unused):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    return harness_main(["--endpoint", args.endpoint, "--credentials", args.credentials,
        "--task-id", task["task_id"], "--agent-python", sys.executable,
        "--packages", args.packages, "--tokenizer", args.tokenizer,
        "--model", args.model_name, "--model-base-url", args.model_base_url,
        "--agent-version", "original-roborsi-policy-api-planner", "--store", args.store,
        "--case-timeout", str(args.case_timeout), "--operation-timeout", "290",
        *["--agent-set=" + value for value in args.agent_set]])


if __name__ == "__main__":
    raise SystemExit(main())
