"""Bounded parallel execution of an explicitly selected test set."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_parallel import run_groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--durations", type=Path)
    parser.add_argument("selectors", nargs="+")
    args = parser.parse_args()
    if len(set(args.selectors)) != len(args.selectors) or not 1 <= args.timeout <= 900:
        parser.error("Selectors must be unique; timeout must be 1..900 seconds")
    if any(not name.startswith("tests.") or not all(part.isidentifier() for part in name.split(".")) for name in args.selectors):
        parser.error("Expected explicit tests.* selectors")
    timings = {}
    if args.durations and args.durations.is_file():
        timings = json.loads(args.durations.read_text())
    groups = [[] for _ in range(min(args.workers, len(args.selectors)))]
    weights = [0.0] * len(groups)
    for name in sorted(args.selectors, key=lambda name: (-timings.get(name, 1), name)):
        target = min(range(len(groups)), key=weights.__getitem__)
        groups[target].append(name)
        weights[target] += timings.get(name, 1)
    return run_groups(groups, timeout=args.timeout, durations_file=args.durations)


if __name__ == "__main__":
    sys.exit(main())
