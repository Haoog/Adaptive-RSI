"""Require every discovered Python test to have an ordinary CI selector."""
from collections import Counter
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_parallel import ROOT, test_ids


def matrix_groups(path):
    """One selector list per `modules:` line of the CI matrix."""
    groups = []
    for line in Path(path).read_text().splitlines():
        if line.strip().startswith("modules:"):
            groups.append(line.split(":", 1)[1].split())
    return groups


def overlap_report(groups):
    """Measure intended cross-group redundancy in the CI matrix.

    Returns (identical shard groups, groups reusing an earlier group's
    selector). Repeating selectors across jobs is deliberate interpreter
    and platform sharding; repeating them inside one job is still an error.
    """
    seen_groups = set()
    seen_selectors = set()
    shards = 0
    shared = 0
    for group in groups:
        key = tuple(group)
        if key in seen_groups:
            shards += 1
        elif seen_selectors.intersection(group):
            shared += 1
        seen_groups.add(key)
        seen_selectors.update(group)
    return shards, shared


def check():
    loader = unittest.TestLoader()
    expected = set(test_ids(loader.discover(str(ROOT), top_level_dir=str(ROOT))))
    if loader.errors:
        raise ValueError("Test discovery failed")
    covered = set()
    for group in matrix_groups(ROOT / ".github/workflows/checks.yml"):
        group_loader = unittest.TestLoader()
        counts = Counter()
        for selector in group:
            loaded = list(test_ids(group_loader.loadTestsFromName(selector)))
            if group_loader.errors:
                raise ValueError("Invalid CI selector " + selector + ":\n" + "\n".join(group_loader.errors))
            if not loaded:
                raise ValueError("CI selector expands to no test: " + selector)
            counts.update(loaded)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError("CI selector group repeats tests:\n" + "\n".join(duplicates))
        unexpected = sorted(set(counts) - expected)
        if unexpected:
            raise ValueError("CI selectors add tests outside discovery:\n" + "\n".join(unexpected))
        covered.update(counts)
    missing = sorted(expected - covered)
    if missing:
        raise ValueError("Tests missing from CI matrix:\n" + "\n".join(missing))
    return len(expected)


if __name__ == "__main__":
    print("CI test selector coverage passed:", check())
    shards, shared = overlap_report(matrix_groups(ROOT / ".github/workflows/checks.yml"))
    print("Intended overlap: identical shard groups:", shards,
          "; groups reusing earlier selectors:", shared)
