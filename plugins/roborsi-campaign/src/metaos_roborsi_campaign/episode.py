"""Select exactly one episode from a content-locked panel, then use the SDK."""
import argparse
import json
import os
from pathlib import Path
import signal

from .panel import INDEX_ENV, experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["panel", "bundle", "output"]:
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ["endpoint", "credentials"]:
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raw = os.environ.get(INDEX_ENV, "")
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("A nonnegative episode index must be explicitly supplied")
    selected = experiment(json.loads(args.panel.read_text()), int(raw))
    selection = args.output.parent / "selected-experiment.json"
    with selection.open("x") as stream:
        json.dump(selected, stream, indent=2)
    from metaos_harness.cli import run
    result = run(args.bundle, selection, args.endpoint, args.credentials, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
