"""Isolated unittest worker with real selector timings and bounded test diagnostics."""
import argparse
import faulthandler
import json
import os
import platform
from pathlib import Path
import sys
import time
import unittest
import uuid


def run(selectors, destination, warn_after=60):
    observed, failed = {}, False
    for selector in selectors:
        began = time.monotonic()
        suite = unittest.defaultTestLoader.loadTestsFromName(selector)
        faulthandler.dump_traceback_later(warn_after, repeat=True)
        try:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            faulthandler.cancel_dump_traceback_later()
        seconds = time.monotonic() - began
        observed[selector] = seconds
        failed |= not result.wasSuccessful()
        if not result.wasSuccessful():
            failure_path = Path(destination).with_suffix(".failures.json")
            try:
                receipts = json.loads(failure_path.read_text())
            except (OSError, ValueError):
                receipts = []
            receipts.append({"attempt": uuid.uuid4().hex, "selector": selector,
                             "failed_tests": [test.id() for test, _ in result.failures + result.errors],
                             "python": platform.python_version(), "platform": platform.platform(),
                             "hash_seed": os.environ.get("PYTHONHASHSEED", "random"),
                             "created": time.time(), "review_by": time.time() + 14 * 86400,
                             "classification": "unclassified", "quarantined": False})
            failure_path.write_text(json.dumps(receipts, sort_keys=True, indent=2) + "\n")
        print("[selector] {} {:.3f}s {}".format(selector, seconds, "failed" if not result.wasSuccessful() else "passed"), flush=True)
        Path(destination).write_text(json.dumps(observed, sort_keys=True) + "\n")
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timings", required=True)
    parser.add_argument("selectors", nargs="+")
    arguments = parser.parse_args()
    sys.exit(run(arguments.selectors, arguments.timings))
