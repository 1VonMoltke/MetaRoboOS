#!/usr/bin/env python3
"""Run installed-package tests and retain actual per-test outcomes as JSON."""

import argparse
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=None):
        self.records.append({"id": test.id(), "status": status,
            "duration_s": round(time.monotonic() - self.started, 4), **({"detail": detail} if detail else {})})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped", reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sdk-only", action="store_true")
    args = parser.parse_args()
    if args.sdk_only and importlib.util.find_spec("metaos_runtime") is not None:
        raise RuntimeError("SDK-only test environment contains MetaOS runtime")
    suite = unittest.defaultTestLoader.discover(str(args.tests), pattern="test_contracts.py" if args.sdk_only else "test_*.py")
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    document = {"status": "passed" if result.wasSuccessful() and not result.skipped else "failed",
        "python": sys.executable, "sdk_only": args.sdk_only, "count": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
        "tests": result.records}
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    return 0 if document["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
