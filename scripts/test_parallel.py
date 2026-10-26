#!/usr/bin/env python3
"""Run complete unittest discovery in at most four isolated Python processes."""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAX_WORKERS = 4


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def discover_groups(root=ROOT, workers=MAX_WORKERS):
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError("workers must be between 1 and 4")
    loader = unittest.TestLoader()
    suite = loader.discover(str(root), top_level_dir=str(root))
    discovered = Counter(test_ids(suite))
    if loader.errors:
        raise ValueError("Test discovery failed:\n" + "\n".join(loader.errors))
    if not discovered or any(count != 1 for count in discovered.values()):
        raise ValueError("Discovery must contain tests with unique IDs")
    # Keep module fixtures and process-global patches within one worker.
    modules = defaultdict(int)
    for test_id in discovered:
        modules[test_id.rsplit(".", 2)[0]] += 1
    selected = Counter(test_ids(loader.loadTestsFromNames(sorted(modules))))
    if loader.errors or selected != discovered:
        raise ValueError("Module selectors do not exactly reproduce discovery")
    groups = [[] for _ in range(min(workers, len(modules)))]
    sizes = [0] * len(groups)
    for module in sorted(modules, key=lambda name: (-modules[name], name)):
        index = min(range(len(groups)), key=lambda i: (sizes[i], i))
        groups[index].append(module)
        sizes[index] += modules[module]
    return groups


def stop_workers(processes):
    # Never poll/wait until the final group signal: an unreaped leader reserves
    # its PID (also our PGID), even if it exits before its descendants. Already
    # reaped workers no longer own that ID and must not receive group signals.
    owned = [process for process in processes if process.returncode is None]
    for process in owned:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if owned:
        time.sleep(1)
    for process in owned:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for process in owned:
        process.wait()


def run_groups(groups, root=ROOT):
    if not groups or len(groups) > MAX_WORKERS or any(not group for group in groups):
        raise ValueError("Expected one to four nonempty test groups")
    processes = []
    failed = False
    with ExitStack() as stack:
        try:
            pending = []
            for index, selectors in enumerate(groups, 1):
                log = stack.enter_context(tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace"))
                argv = [sys.executable, "-u", "-m", "unittest", "-v", *selectors]
                print("[group {}] {}".format(index, " ".join(selectors)), flush=True)
                process = subprocess.Popen(argv, cwd=root, shell=False, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                processes.append(process)
                pending.append((index, process, log))
            while pending:
                for item in pending[:]:
                    index, process, log = item
                    result = process.poll()
                    if result is None:
                        continue
                    log.seek(0)
                    for line in log:
                        print("[group {}] {}".format(index, line.rstrip("\n")), flush=True)
                    print("[group {}] exit {}".format(index, result), flush=True)
                    failed = failed or result != 0
                    pending.remove(item)
                if pending:
                    time.sleep(0.1)
        finally:
            stop_workers(processes)
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=range(1, MAX_WORKERS + 1), default=MAX_WORKERS)
    args = parser.parse_args(argv)

    def cancel(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        return run_groups(discover_groups(workers=args.workers))
    except KeyboardInterrupt:
        print("Parallel tests cancelled", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
