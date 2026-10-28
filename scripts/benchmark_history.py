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

from adaptive_rsi import telemetry
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=int, default=10000)
    parser.add_argument('--samples', type=int, default=3)
    parser.add_argument('--budget-ms', type=float, default=5000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--enforce', action='store_true')
    arguments = parser.parse_args(argv)
    result = benchmark(arguments.tasks, arguments.samples, arguments.budget_ms)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text)
    print(text)
    return int(arguments.enforce and not result['passed'])


if __name__ == '__main__':
    sys.exit(main())
