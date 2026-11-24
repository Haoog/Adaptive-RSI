"""Replay a deterministic large history and report bounded read-model budgets."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time
import types
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adaptive_rsi import queue_replay, telemetry, usage_report
from adaptive_rsi.dashboard import records
from adaptive_rsi.store import Store, encode


ANCHOR = 1700000000


def seed(store, count):
    specification = {'format': 1, 'tasks': count, 'runs_per_task': 2, 'anchor': ANCHOR,
                     'projects': 4, 'seed': 'ordinal-modulo-5', 'evidence_padding_bytes': 512}
    evidence = encode({'summary': 'x' * 512, 'evaluation': {'decision': 'pass'},
                       'review': {'decision': 'pass'}, 'merged_at': ANCHOR - 1})
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.executemany("INSERT INTO projects VALUES (?,?,?,'{}',1,?)",
                       [(str(i), 'fixture-' + str(i), str(store.root / str(i)), ANCHOR - 10000) for i in range(4)])
        db.executemany('INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,acceptance,state,created,updated) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       ((str(i), str(i % 4), 'goal', 'f-' + str(i), 'Task ' + str(i), 'Fixture', '{}', '[]',
                         'merged' if i % 5 else 'failed', ANCHOR - 10000 + i % 1000, ANCHOR - 1) for i in range(count)))
        db.executemany('INSERT INTO runs(id,task,project,state,started,ended,result) VALUES (?,?,?,?,?,?,?)',
                       ((str(i) + '-' + str(attempt), str(i), str(i % 4), 'merged' if attempt else 'failed',
                         ANCHOR - 1000 + i % 100, ANCHOR - 10, evidence if attempt else '{"failure":{"code":"execution"}}')
                        for i in range(count) for attempt in range(2)))
    specification['sha256'] = hashlib.sha256(encode(specification).encode()).hexdigest()
    return specification


def benchmark(count=10000, samples=3, budget_ms=5000, max_response_bytes=8 * 1024 * 1024):
    if type(count) is not int or not 100 <= count <= 100000 or type(samples) is not int or not 1 <= samples <= 10:
        raise ValueError('History benchmark requires 100..100000 tasks and 1..10 samples')
    if type(budget_ms) not in (int, float) or not math.isfinite(budget_ms) or budget_ms <= 0:
        raise ValueError('History benchmark budget must be finite and positive')
    if type(max_response_bytes) is not int or max_response_bytes <= 0:
        raise ValueError('History response budget must be positive')
    with tempfile.TemporaryDirectory(prefix='rsi-history-benchmark-') as directory:
        store = Store(Path(directory) / 'state')
        try:
            specification = seed(store, count)
            results = {}
            # Only the telemetry module clock is replaced, keeping this fixed
            # historical dataset in its exact declared 30-day analysis window.
            with patch.object(telemetry, 'time', types.SimpleNamespace(time=lambda: ANCHOR)):
                for name, read in [('task_page', lambda: records(store, '0', limit=50)),
                                   ('workflow_metrics', lambda: telemetry.workflow_metrics(store, '0')),
                                   ('quality_summary', lambda: telemetry.quality_summary(store, '0'))]:
                    timings, sizes, queries = [], [], []
                    for _ in range(samples):
                        calls = [0]
                        original_query = store.query
                        def counted(function):
                            def invoke(*args, **kwargs):
                                calls[0] += 1
                                return function(*args, **kwargs)
                            return invoke
                        with patch.object(store, 'query', counted(original_query)):
                            began = time.perf_counter()
                            payload = encode(read()).encode()
                            timings.append((time.perf_counter() - began) * 1000)
                            sizes.append(len(payload))
                        queries.append(calls[0])
                    p95 = sorted(timings)[math.ceil(.95 * len(timings)) - 1]
                    results[name] = {'milliseconds': timings, 'p95_ms': p95, 'response_bytes': max(sizes),
                                     'query_calls': queries, 'passed': p95 <= budget_ms and max(sizes) <= max_response_bytes}
            return {'format': 1, 'dataset': specification, 'environment': {'python': platform.python_version(),
                    'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version}, 'samples': samples,
                    'budget_ms': budget_ms, 'max_response_bytes': max_response_bytes, 'operations': results,
                    'passed': all(row['passed'] for row in results.values()),
                    'limitations': 'Synthetic local read-model and JSON serialization measurements; no HTTP transport, real models, production load or cold-disk guarantee.'}
        finally:
            store.close()


def read_policy_history(path, start, end, project=None):
    # Immutable mode is for a completed standalone snapshot only. Reading a live
    # WAL database this way could silently miss committed records.
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(path) + suffix).exists():
            raise ValueError('Use a standalone SQLite snapshot with no WAL, SHM or journal sidecars')
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return usage_report.policy_history(db, start, end, project)
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--policy-replay', type=Path, help='read a redacted queue trace JSON')
    modes.add_argument('--policy-history', type=Path, help='read a standalone local SQLite snapshot')
    parser.add_argument('--start', type=float, help='history window start (inclusive Unix seconds)')
    parser.add_argument('--end', type=float, help='history window end (exclusive Unix seconds)')
    parser.add_argument('--project', help='limit historical cohorts to a project ID')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--planning-slots', type=int, default=1)
    parser.add_argument('--old-after', type=float, default=86400)
    parser.add_argument('--old-quota', type=int, default=1)
    parser.add_argument('--urgent-quota', type=int, default=1)
    parser.add_argument('--quota-window', type=float, default=3600)
    parser.add_argument('--tasks', type=int, default=10000)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--budget-ms', type=float, default=5000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--enforce', action='store_true')
    arguments = parser.parse_args(argv)
    try:
        if arguments.policy_replay:
            trace = json.loads(arguments.policy_replay.read_text(encoding='utf-8'))
            result = queue_replay.evaluate(trace, workers=arguments.workers, planning_slots=arguments.planning_slots,
                                           old_after=arguments.old_after, old_quota=arguments.old_quota,
                                           urgent_quota=arguments.urgent_quota, quota_window=arguments.quota_window)
        elif arguments.policy_history:
            if arguments.start is None or arguments.end is None:
                parser.error('--policy-history requires --start and --end')
            result = read_policy_history(arguments.policy_history.resolve(), arguments.start, arguments.end,
                                         arguments.project)
        else:
            result = benchmark(arguments.tasks, arguments.samples, arguments.budget_ms)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))
    if arguments.enforce and (arguments.policy_replay or arguments.policy_history):
        parser.error('--enforce applies only to the performance benchmark')
    text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    if arguments.output:
        if arguments.policy_replay or arguments.policy_history:
            output = arguments.output.resolve()
            if arguments.policy_history and output in [Path(str(arguments.policy_history.resolve()) + suffix)
                                                        for suffix in ('-wal', '-shm', '-journal')]:
                parser.error('Choose an output outside the SQLite snapshot sidecar paths')
            try:
                with output.open('x', encoding='utf-8') as stream:
                    stream.write(text)
            except OSError as exc:
                parser.error(str(exc))
        else:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(text)
    print(text)
    return int(arguments.enforce and not result['passed'])


if __name__ == '__main__':
    sys.exit(main())
