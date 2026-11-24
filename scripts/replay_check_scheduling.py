#!/usr/bin/env python3
"""Compare priority-only and full coverage using saved evaluator report.json files."""
import argparse
import json
import math
from pathlib import Path


TIME_BASIS = 'shared_evaluation_dispatch_monotonic_seconds'


def failure_summary(rows, time_basis):
    failed = [row for row in rows if row['passed'] is False]
    times = [row.get('verdict_available_seconds') for row in failed]
    known = (time_basis == TIME_BASIS and
             all(type(value) in (float, int) and 0 <= value < math.inf for value in times))
    return {
        'checks': len(rows),
        'failures': [{'name': row['name'], 'cwd': row.get('cwd') or '.', 'full_index': row['full_index']}
                     for row in failed],
        'first_failure_seconds': min(times) if failed and known else None,
        'first_failure_timing': 'observed' if failed and known else 'unknown' if failed else 'no_failure',
    }


def replay(report):
    if not isinstance(report, dict):
        return {'status': 'unavailable', 'reason': 'evaluator report object required'}
    analysis = report.get('check_dependency_analysis') or {}
    checks = report.get('checks')
    if not isinstance(analysis, dict) or not isinstance(checks, dict):
        return {'status': 'unavailable', 'reason': 'check evidence and dependency analysis required'}
    rows = checks.get('candidate_full')
    priority = analysis.get('scheduled')
    if priority is None:
        priority = analysis.get('affected')
    if (report.get('interrupted') or report.get('setup') or not isinstance(rows, list) or not rows
            or not isinstance(priority, list) or not priority
            or any(not isinstance(name, str) for name in priority)
            or any(not isinstance(row, dict) or not isinstance(row.get('name'), str)
                   or type(row.get('passed')) is not bool for row in rows)
            or not set(priority).issubset({row['name'] for row in rows})):
        return {'status': 'unavailable', 'reason': 'complete candidate_full and priority membership required'}
    # The evaluator selects by name, including every same-name instance. The
    # merged full surface already contains reused rows exactly once per check.
    full = [dict(row, full_index=index) for index, row in enumerate(rows)]
    chosen = set(priority)
    time_basis = analysis.get('time_basis')
    return {
        'status': 'compared', 'gate_decision': report.get('decision'),
        'priority_source': 'scheduled' if analysis.get('scheduled') is not None else 'affected',
        'priority': failure_summary([row for row in full if row['name'] in chosen], time_basis),
        'full': failure_summary(full, time_basis),
        'outside_priority': failure_summary([row for row in full if row['name'] not in chosen], time_basis),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', type=Path, nargs='+')
    args = parser.parse_args()
    results = [replay(json.loads(path.read_text(encoding='utf-8'))) for path in args.reports]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
