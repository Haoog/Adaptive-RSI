"""The history benchmark is replayable and budget violations fail explicitly."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from adaptive_rsi.store import Store
from scripts.benchmark_history import ANCHOR, benchmark, main, seed


class HistoryBudgetTest(unittest.TestCase):
    def test_fixed_fixture_has_identical_rows_and_manifest_on_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            for name in ('first', 'second'):
                store = Store(Path(directory) / name)
                try:
                    manifest = seed(store, 100)
                    snapshots.append((manifest, store.query('SELECT * FROM tasks ORDER BY id'),
                                      store.query('SELECT * FROM runs ORDER BY id')))
                finally:
                    store.close()
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(len(snapshots[0][1]), 100)
            self.assertEqual(len(snapshots[0][2]), 200)
            self.assertEqual(snapshots[0][0]['anchor'], ANCHOR)

    def test_report_measures_operations_and_budget_failures_are_explicit(self):
        result = benchmark(count=100, samples=2, budget_ms=5000)
        self.assertTrue(result['passed'])
        self.assertEqual(set(result['operations']), {'task_page', 'workflow_metrics', 'quality_summary'})
        for row in result['operations'].values():
            self.assertEqual(len(row['milliseconds']), 2)
            self.assertGreater(row['response_bytes'], 0)
            self.assertGreater(row['query_calls'][0], 0)
            self.assertEqual(row['p95_ms'], max(row['milliseconds']))
        failed = benchmark(count=100, samples=1, max_response_bytes=1)
        self.assertFalse(failed['passed'])
        self.assertTrue(all(not row['passed'] for row in failed['operations'].values()))

    def test_cli_enforcement_is_opt_in_and_writes_reviewable_report(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            path = Path(directory) / 'report.json'
            arguments = ['--tasks', '100', '--samples', '1', '--budget-ms', '0.000000001', '--output', str(path)]
            self.assertEqual(main(arguments), 0)
            self.assertFalse(json.loads(path.read_text())['passed'])
            self.assertEqual(main([*arguments, '--enforce']), 1)

    def test_invalid_budgets_are_rejected(self):
        for arguments in ({'count': 99}, {'count': 100001}, {'samples': 0}, {'samples': 11},
                          {'budget_ms': float('nan')}, {'budget_ms': -1}, {'max_response_bytes': 0}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                benchmark(**arguments)
