#!/usr/bin/env python3
"""Bind a local RoboCasa recipe to the installed gateway Harness."""

import argparse
import json
import signal
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("endpoint", "credentials", "task", "agent-config", "store"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--case-timeout", type=float, default=14400.)
    args = parser.parse_args()
    task = json.loads(Path(args.task).read_text())
    from metaos_robocasa_harness import main as harness_main
    from metaos_robocasa_harness.agent_service import load_config

    config = load_config(Path(args.agent_config))

    def interrupted(*unused):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupted)
    return harness_main(["--endpoint", args.endpoint, "--credentials", args.credentials,
        "--task-id", task["task_id"], "--seed", str(task["seed"]),
        "--agent-python", sys.executable, "--agent-config", args.agent_config,
        "--agent-version", "robocasa-config-sha256:" + config.fingerprint,
        "--store", args.store, "--case-timeout", str(args.case_timeout), "--operation-timeout", "3590"])


if __name__ == "__main__":
    raise SystemExit(main())
