"""Isolated unittest worker with phase, thread and child-process diagnostics.

The worker is the only process that can see why one selector group stops: the
parent only forwards its output.  Diagnostics therefore stay inside this
process and are written to a raw file descriptor, so a replaced or closed
``sys.stdout``/``sys.stderr`` object cannot hide them.  Nothing here imports or
starts the test runner again, reads configuration, or prints environment
values.
"""
import argparse
from collections import defaultdict
import faulthandler
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid


# ``ps`` columns deliberately exclude argv: command lines of sandbox children
# may embed tokens, and diagnostics must never echo credentials.
CHILD_COLUMNS = "pid=,ppid=,stat=,comm="
MAX_REPORTED_THREADS = 32
MAX_REPORTED_FRAMES = 8
MAX_REPORTED_CHILDREN = 20
MAX_COMMAND_CHARS = 120


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


def write_line(descriptor, line):
    """Write one diagnostic line to a descriptor, ignoring a closed or blocked fd."""
    data = (str(line).rstrip("\n") + "\n").encode("utf-8", "replace")
    while data:
        try:
            written = os.write(descriptor, data)
        except (OSError, ValueError):
            return False
        if written <= 0:
            return False
        data = data[written:]
    return True


def emit_fd(descriptor):
    """Return an emitter bound to a descriptor rather than a Python stream."""
    def emit(line):
        write_line(descriptor, line)
    return emit


def _frames(frame, limit=MAX_REPORTED_FRAMES):
    lines = []
    while frame is not None and len(lines) < limit:
        code = frame.f_code
        lines.append('[worker]   File "{}", line {} in {}'.format(
            code.co_filename, frame.f_lineno, code.co_name))
        frame = frame.f_back
    if frame is not None:
        lines.append("[worker]   ...")
    return lines


def describe_threads(limit=MAX_REPORTED_FRAMES, maximum=MAX_REPORTED_THREADS):
    """Name every live thread and show the frames it is currently running.

    Frames are formatted from code objects, not traceback.TracebackException, so
    no source file is read while the worker may hold unrelated file locks.
    """
    frames = sys._current_frames()
    listed = list(threading.enumerate())
    lines = []
    for thread in listed[:maximum]:
        lines.append("[worker] thread {} ident={} daemon={}".format(
            thread.name or "?", thread.ident, thread.daemon))
        frame = frames.get(thread.ident) if thread.ident is not None else None
        if frame is not None:
            lines.extend(_frames(frame, limit))
    if len(listed) > maximum:
        lines.append("[worker] threads {} more".format(len(listed) - maximum))
    return lines


def describe_children(pid=None, timeout=5, maximum=MAX_REPORTED_CHILDREN):
    """List direct children of this worker by pid, parent, state and executable.

    ``ps`` is invoked directly with a fixed column list; no shell, no Python, and
    no test selector is involved, so a stalled worker cannot dispatch itself
    again. A timeout kills and reaps the probe, leaving no orphan behind.
    """
    pid = os.getpid() if pid is None else pid
    try:
        completed = subprocess.run(["ps", "-axo", CHILD_COLUMNS], capture_output=True,
                                   text=True, timeout=timeout, check=False)
    except Exception as error:  # ps missing, killed or timed out
        return ["[worker] children unavailable: {}".format(type(error).__name__)]
    lines = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            child, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if parent != pid:
            continue
        if len(lines) >= maximum:
            lines.append("[worker] children truncated")
            break
        lines.append("[worker] child pid={} ppid={} state={} comm={}".format(
            child, parent, parts[2][:8], parts[3].strip()[:MAX_COMMAND_CHARS]))
    return lines or ["[worker] children none"]


class Progress:
    """Track the current phase and selector, and report a stalled worker.

    ``clock`` defaults to the monotonic clock captured when the reporter is
    created, so the worker's own timings do not depend on later clock patches.
    """

    def __init__(self, emit=None, clock=None):
        self.emit = emit
        self.clock = clock or time.monotonic
        self.phase = "starting"
        self.selector = ""
        self.began = self.since = self.clock()
        self._lock = threading.Lock()

    def set(self, phase, selector="", detail=""):
        with self._lock:
            self.phase, self.selector, self.since = phase, selector, self.clock()
        suffix = " " + detail if detail else ""
        self._write("[worker] phase={} selector={}{}".format(phase, selector or "-", suffix))

    def stalled(self, reason="stalled"):
        """Write one self-contained report; never raise into the watchdog thread."""
        with self._lock:
            phase, selector = self.phase, self.selector
            since, began = self.since, self.began
        now = self.clock()
        lines = ["[worker] diagnostic reason={} phase={} selector={} phase_seconds={:.1f} "
                 "elapsed_seconds={:.1f} pid={} ppid={}".format(
                     reason, phase, selector or "-", now - since, now - began,
                     os.getpid(), os.getppid())]
        for collector in (describe_threads, describe_children):
            try:
                lines.extend(collector())
            except Exception as error:  # diagnostics must never stop the worker
                lines.append("[worker] {} unavailable: {}".format(
                    collector.__name__, type(error).__name__))
        for line in lines:
            self._write(line)

    def _write(self, line):
        if self.emit is None:
            return
        try:
            self.emit(line)
        except Exception:  # a broken log sink must not fail tests
            pass


class _IdleProgress:
    """No-op reporter used when run() is called without diagnostics."""

    def set(self, phase, selector="", detail=""):
        pass

    def stalled(self, reason="stalled"):
        pass


IDLE = _IdleProgress()


def start_stall_watch(progress, interval=60.0, stop=None):
    """Start a daemon that reports the worker's state every ``interval`` seconds.

    The watcher runs through the whole process lifetime, including the completion
    phase after the last selector, so a worker that never exits still explains
    itself.
    """
    if not 0 < interval <= 3600:
        raise ValueError("diagnose interval must be between 0 and 3600 seconds")
    stop = threading.Event() if stop is None else stop

    def watch():
        while not stop.wait(interval):
            progress.stalled("stalled")

    thread = threading.Thread(target=watch, name="rsi-test-stall-watch", daemon=True)
    thread.start()
    return thread


def install_crash_dumps(seconds, stream=None):
    """Keep one repeating faulthandler timer for the whole worker lifetime.

    A per-group timer is cancelled exactly in the window where the worker was
    last seen alive; arming it once keeps C-level stacks available for the
    completion phase as well. Returns False when faulthandler cannot be armed.
    """
    if not 0 < seconds <= 3600:
        raise ValueError("diagnose interval must be between 0 and 3600 seconds")
    stream = sys.__stderr__ if stream is None else stream
    if stream is None:
        return False
    try:
        faulthandler.dump_traceback_later(seconds, repeat=True, file=stream)
    except Exception:
        return False
    return True


def run(selectors, destination, progress=None):
    reporter = IDLE if progress is None else progress
    observed, failed = {}, False
    reporter.set("loading")
    grouped = suites(selectors)
    for chosen, suite in grouped:
        selector = " ".join(chosen)
        reporter.set("running", selector)
        began = time.monotonic()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        seconds = time.monotonic() - began
        reporter.set("finished", selector, "failed={}".format(0 if result.wasSuccessful() else 1))
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
        reporter.set("timings", selector)
    reporter.set("completed", detail="groups={}".format(len(grouped)))
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
    parser.add_argument("--diagnose-after", type=float, default=60.0)
    parser.add_argument("selectors", nargs="+")
    arguments = parser.parse_args()
    if not 1 <= arguments.diagnose_after <= 3600:
        parser.error("--diagnose-after must be between 1 and 3600 seconds")
    os.environ["RSI_TEST_RUNNING"] = ",".join(sorted({
        ".".join(selector.split(".")[:2]) for selector in arguments.selectors}))
    progress = Progress(emit=emit_fd(2))
    start_stall_watch(progress, arguments.diagnose_after)
    if not install_crash_dumps(arguments.diagnose_after):
        progress._write("[worker] crash dumps unavailable")
    if os.environ.get("RSI_TEST_ACTIVE") == "1":
        threading.Thread(target=watch_parent, args=(os.getppid(),), daemon=True).start()
    progress.set("starting", detail="selectors={}".format(len(arguments.selectors)))
    try:
        code = run(arguments.selectors, arguments.timings, progress=progress)
    except BaseException:
        progress.set("failed")
        raise
    progress.set("exiting", detail="code={}".format(code))
    faulthandler.cancel_dump_traceback_later()
    sys.exit(code)
