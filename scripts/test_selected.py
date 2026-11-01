"""Bounded parallel execution of an explicitly selected test set.

Selectors are expanded before any process starts. A selector that names a whole
class and another that names one of its methods select the same test twice; such
overlaps are rejected here instead of being handed to a worker. Without that
check a worker re-runs the very test that started the runner and spawns another
generation, which exhausts the machine (see audit 2026-10-28). Exact duplicate
selectors are idempotent and run their tests once.
"""
import argparse
from collections import defaultdict
import importlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_parallel import run_groups, test_ids, timing_cache


class SelectionError(ValueError):
    """A selector set that cannot be executed exactly once."""


def overlapping(left, right):
    """True when one selector strictly contains the other, without importing anything."""
    return left.startswith(right + ".") or right.startswith(left + ".")


def expand(selectors):
    """Return every concrete test id exactly once, or raise SelectionError."""
    selectors = list(dict.fromkeys(selectors))
    for index, name in enumerate(selectors):
        if any(overlapping(name, other) for other in selectors[index + 1:]):
            raise SelectionError("Overlapping selectors name the same tests: {}".format(name))
    loader = unittest.TestLoader()
    expanded, owner = [], {}
    for name in selectors:
        try:
            importlib.import_module(".".join(name.split(".")[:2]))
        except ImportError:
            # Synthetic selectors (for example tests.fast in planning tests) do
            # not exist on disk; the prefix check above proved they cannot
            # overlap anything, so keep them verbatim.
            expanded.append(name)
            continue
        try:
            suite = loader.loadTestsFromName(name)
        except (ImportError, AttributeError, ValueError):
            expanded.append(name)
            continue
        if loader.errors:
            raise SelectionError("Cannot load selector {}:\n{}".format(
                name, "\n".join(str(error) for error in loader.errors)))
        ids = list(test_ids(suite))
        if not ids:
            raise SelectionError("Selector selected no tests: {}".format(name))
        for test_id in ids:
            if test_id in owner:
                raise SelectionError("Overlapping selection: {} chosen by {} and {}".format(
                    test_id, owner[test_id], name))
            owner[test_id] = name
            expanded.append(test_id)
    return list(dict.fromkeys(expanded))


def group_by_module(selected, workers, timings):
    """Keep every module's fixtures in one worker and balance by known duration."""
    modules = defaultdict(list)
    for test_id in selected:
        parts = test_id.split(".")
        modules[".".join(parts[:2])].append(test_id)
    groups = [[] for _ in range(max(1, min(workers, len(modules))))]
    weights = {name: max(0.01, sum(timings.get(test_id, 1) for test_id in ids))
               for name, ids in modules.items()}
    sizes = [0.0] * len(groups)
    for module in sorted(modules, key=lambda name: (-weights[name], name)):
        target = min(range(len(groups)), key=lambda index: (sizes[index], index))
        groups[target].extend(modules[module])
        sizes[target] += weights[module]
    return groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--durations", type=Path)
    parser.add_argument("selectors", nargs="+")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 900:
        parser.error("Timeout must be 1..900 seconds")
    if any(not name.startswith("tests.") or not all(part.isidentifier() for part in name.split(".")) for name in args.selectors):
        parser.error("Expected explicit tests.* selectors")
    try:
        selected = expand(args.selectors)
    except SelectionError as error:
        parser.error(str(error))
    timings = {}
    if args.durations and args.durations.is_file():
        try:
            timings = timing_cache(json.loads(args.durations.read_text()))
        except (OSError, ValueError):
            print("Ignoring invalid or unreadable timing cache; all selected tests remain required", file=sys.stderr)
    try:
        return run_groups(group_by_module(selected, args.workers, timings),
                          timeout=args.timeout, durations_file=args.durations)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
