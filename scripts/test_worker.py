"""Isolated unittest worker with real selector timings and bounded test diagnostics."""
import argparse
from collections import defaultdict
import faulthandler
import json
import os
import platform
from pathlib import Path
import sys
import time
import unittest
import uuid


def ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from ids(test)
        else:
            yield test.id()


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def suites(selectors):
    """Group selectors by test class so one class fixture runs exactly once.

    Each group is ``(selectors, suite)``. Empty and overlapping selections are
    rejected before anything runs: a selector that also contains another selector
    would re-enter the runner and recurse.
    """
    loader = unittest.defaultTestLoader
    owner, planned, membership = {}, defaultdict(list), defaultdict(list)
    for selector in selectors:
        loaded = list(flatten(loader.loadTestsFromName(selector)))
        if not loaded:
            raise ValueError("Selector selected no tests: {}".format(selector))
        for test in loaded:
            test_id = test.id()
            if test_id in owner:
                raise ValueError("Overlapping selection: {} chosen by {} and {}".format(
                    test_id, owner[test_id], selector))
            owner[test_id] = selector
            group = test_id.rsplit(".", 1)[0]
            planned[group].append(test)
            if selector not in membership[group]:
                membership[group].append(selector)
    return [(membership[group], unittest.TestSuite(planned[group])) for group in sorted(planned)]


def run(selectors, destination, warn_after=60):
    observed, failed = {}, False
    for chosen, suite in suites(selectors):
        selector = " ".join(chosen)
        began = time.monotonic()
        stream = sys.__stderr__
        if stream is not None:
            faulthandler.dump_traceback_later(warn_after, repeat=True, file=stream)
        try:
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            faulthandler.cancel_dump_traceback_later()
        seconds = time.monotonic() - began
        share = seconds / len(chosen)
        for name in chosen:
            observed[name] = observed.get(name, 0.0) + share
        failed |= not result.wasSuccessful()
        if not result.wasSuccessful():
            failure_path = Path(destination).with_suffix(".failures.json")
            try:
                receipts = json.loads(failure_path.read_text())
            except (OSError, ValueError):
                receipts = []
            if not isinstance(receipts, list):
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


def watch_parent(parent):
    """Exit when the runner is gone; detached workers must not outlive their parent."""
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os._exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timings", required=True)
    parser.add_argument("selectors", nargs="+")
    arguments = parser.parse_args()
    os.environ["RSI_TEST_RUNNING"] = ",".join(sorted({
        ".".join(selector.split(".")[:2]) for selector in arguments.selectors}))
    if os.environ.get("RSI_TEST_ACTIVE") == "1":
        import threading
        threading.Thread(target=watch_parent, args=(os.getppid(),), daemon=True).start()
    sys.exit(run(arguments.selectors, arguments.timings))
