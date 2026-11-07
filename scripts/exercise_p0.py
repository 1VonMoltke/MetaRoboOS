#!/usr/bin/env python3
"""Human-reviewable commands against an already running P0 gateway."""

import argparse
import json
import time
from pathlib import Path

from metaos_contracts import SystemError
from metaos_runtime.client import Client


def exercise(client, mode):
    resource, capability = "robots/mock-arm", "manipulation.move_joint/v1"
    cases = []
    for selected in (["normal", "cancel", "expire"] if mode == "all" else [mode]):
        lease = client.acquire(resource, 0.45 if selected == "expire" else 5.0)
        command = client.build_command(lease, capability, {"positions": [1.0, -1.0],
            "duration_s": 0.1 if selected == "normal" else 3.0})
        try:
            accepted = client.submit(command)
            if selected == "cancel":
                client.cancel(command["command_id"])
            result = client.wait(command["command_id"])
            expected = ("succeeded", None) if selected == "normal" else ("cancelled", "client_cancelled" if selected == "cancel" else "lease_expired")
            if (result["status"], result["reason"]) != expected:
                raise RuntimeError("Unexpected command outcome: " + json.dumps(result))
            state = client.state(resource)
            if state["data"]["moving"]:
                raise RuntimeError("Terminal command still reports motion")
            if selected != "normal" and result["result"]["stop"].get("stopped") is not True:
                raise RuntimeError("Cancellation has no device stop confirmation")
            cases.append({"case": selected, "lease": Client.reference(lease),
                "request": command, "accepted": accepted, "result": result, "state": state})
            print(f"PASS {selected}: {result['status']} {result['reason'] or ''}", flush=True)
        finally:
            try:
                client.release(lease)
            except SystemError as exc:
                if exc.code != "stale_lease":
                    raise
    return {"status": "passed", "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--caller", default="operator-a")
    parser.add_argument("--mode", choices=["normal", "cancel", "expire", "all"], default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ready = json.loads(args.ready_file.read_text())
    result = exercise(Client(ready["endpoint"], args.credentials, args.caller), args.mode)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
