#!/usr/bin/env python3
"""Bind local files to the already installed original LIBERO Harness CLI."""

import argparse
import json
import signal
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("endpoint", "credentials", "model-name", "model-base-url", "task", "packages", "tokenizer", "store"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--agent-set", action="append", default=[])
    args = parser.parse_args()
    if args.model_name.startswith("SET_"):
        raise ValueError("Set the actual served model identity in the model resource descriptor")
    task = json.loads(Path(args.task).read_text())
    from metaos_libero_harness import main as harness_main
    def interrupted(*unused):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    return harness_main(["--endpoint", args.endpoint, "--credentials", args.credentials,
        "--task-id", task["task_id"], "--agent-python", sys.executable,
        "--packages", args.packages, "--tokenizer", args.tokenizer,
        "--model", args.model_name, "--model-base-url", args.model_base_url,
        "--agent-version", "local-pinned-original-agent", "--store", args.store,
        "--case-timeout", "7000", "--operation-timeout", "290",
        *["--agent-set=" + value for value in args.agent_set]])


if __name__ == "__main__":
    raise SystemExit(main())
