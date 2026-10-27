"""Require every discovered Python test to have an ordinary CI selector."""
from collections import Counter
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_parallel import ROOT, test_ids


def check():
    loader = unittest.TestLoader()
    expected = set(test_ids(loader.discover(str(ROOT), top_level_dir=str(ROOT))))
    if loader.errors:
        raise ValueError("Test discovery failed")
    selectors = []
    for line in (ROOT / ".github/workflows/checks.yml").read_text().splitlines():
        if line.strip().startswith("modules:"):
            selectors.extend(line.split(":", 1)[1].split())
    actual = Counter(test_ids(loader.loadTestsFromNames(selectors)))
    if loader.errors:
        raise ValueError("Invalid CI selector")
    missing = sorted(expected - set(actual))
    if missing:
        raise ValueError("Tests missing from CI matrix:\n" + "\n".join(missing))
    return len(expected)


if __name__ == "__main__":
    print("CI test selector coverage passed:", check())
