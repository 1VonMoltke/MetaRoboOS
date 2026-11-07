"""Exercise the installed example's binding and cleanup, without a simulator."""
import argparse
import json
from pathlib import Path

from metaos_harness import Host
from metaos_harness.cli import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    resolved, lock = verify(args.bundle)
    with Host(resolved, expected_lock=lock) as report:
        result = report.run({"increment": 3})
        if result != {"value": 5}:
            raise RuntimeError("Bound counter did not produce the expected result")
    if not report.closed or not report.counter.closed:
        raise RuntimeError("Host did not close both owned components")
    try:
        report.counter.add(1)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Closed counter accepted work")
    print(json.dumps({"result": result, "bindings_verified": True, "cleanup_verified": True}))


if __name__ == "__main__":
    main()
