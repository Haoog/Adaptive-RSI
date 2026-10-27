#!/usr/bin/env python3
"""Run complete unittest discovery in at most four isolated Python processes."""
import argparse
import json
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


def discover_groups(root=ROOT, workers=MAX_WORKERS, durations=None):
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
    weights = {name: max(0.01, float((durations or {}).get(name, count))) for name, count in modules.items()}
    for module in sorted(modules, key=lambda name: (-weights[name], name)):
        index = min(range(len(groups)), key=lambda i: (sizes[i], i))
        groups[index].append(module)
        sizes[index] += weights[module]
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


def run_groups(groups, root=ROOT, timeout=900, durations_file=None):
    if not groups or len(groups) > MAX_WORKERS or any(not group for group in groups):
        raise ValueError("Expected one to four nonempty test groups")
    processes = []
    failed = False
    began, offsets, recent = {}, {}, {}
    observed = {}
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
                began[index] = recent[index] = time.monotonic()
                offsets[index] = 0
            while pending:
                for item in pending[:]:
                    index, process, log = item
                    result = process.poll()
                    log.seek(offsets[index])
                    for line in log:
                        print("[group {}] {}".format(index, line.rstrip("\n")), flush=True)
                    offsets[index] = log.tell()
                    now = time.monotonic()
                    if result is None and now - began[index] >= timeout:
                        print("[group {}] timeout after {} seconds".format(index, timeout), flush=True)
                        stop_workers([process])
                        result = process.returncode or 1
                    if result is None and now - recent[index] >= 30:
                        print("[group {}] running for {:.0f}s".format(index, now - began[index]), flush=True)
                        recent[index] = now
                    if result is None:
                        continue
                    log.seek(offsets[index])
                    for line in log:
                        print("[group {}] {}".format(index, line.rstrip("\n")), flush=True)
                    print("[group {}] exit {}".format(index, result), flush=True)
                    failed = failed or result != 0
                    pending.remove(item)
                    if result == 0:
                        share = (now - began[index]) / len(groups[index - 1])
                        observed.update({module: share for module in groups[index - 1]})
                if pending:
                    time.sleep(0.1)
        finally:
            stop_workers(processes)
    if durations_file and observed:
        target = Path(durations_file)
        try:
            previous = json.loads(target.read_text())
        except (OSError, ValueError):
            previous = {}
        previous.update(observed)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(previous, sort_keys=True, indent=2) + "\n")
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=range(1, MAX_WORKERS + 1), default=MAX_WORKERS)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--durations", type=Path)
    args = parser.parse_args(argv)

    def cancel(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, cancel)
    try:
        if not 1 <= args.timeout <= 86400:
            raise ValueError("timeout must be 1..86400 seconds")
        durations = {}
        if args.durations and args.durations.is_file():
            durations = json.loads(args.durations.read_text())
        return run_groups(discover_groups(workers=args.workers, durations=durations),
                          timeout=args.timeout, durations_file=args.durations)
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
