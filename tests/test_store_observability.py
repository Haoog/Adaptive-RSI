"""Measure real contention and preserve orphan evidence under bounded maintenance."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.store import Store


class StoreObservabilityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.addCleanup(self.store.close)

    def test_slow_sql_has_context_but_no_bound_values_and_excludes_pool_wait(self):
        secret = 'PRIVATE_BINDING_SHOULD_NOT_APPEAR'
        with self.store.connect() as db:
            db.create_function('slow_fixture', 1, lambda value: time.sleep(.12) or 1)
        with self.store.query_scope(request='request-123'):
            with self.store.query_scope(run='run-456', task='task-789'):
                self.store.query('SELECT slow_fixture(?) AS result', (secret,))
        self.store.query('SELECT slow_fixture(?)', (secret,))
        rows = self.store.metrics()['slow_queries']
        self.assertEqual(rows[-2]['context'], {'request': 'request-123', 'run': 'run-456', 'task': 'task-789'})
        self.assertEqual(rows[-1]['context'], {})
        self.assertGreaterEqual(rows[-1]['seconds'], .1)
        self.assertGreaterEqual(rows[-1]['total_seconds'], rows[-1]['seconds'])
        self.assertNotIn(secret, json.dumps(rows))
        self.assertNotIn('slow_fixture', json.dumps(rows))
        self.store._connection_capacity = threading.BoundedSemaphore(1)
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.store.connect():
                held.set()
                release.wait(2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(hold)
            self.assertTrue(held.wait(1))
            pending = pool.submit(self.store.query, 'SELECT 1')
            time.sleep(.15)
            release.set()
            pending.result(timeout=2)
            holder.result(timeout=2)
        self.assertEqual(len(self.store.metrics()['slow_queries']), 2)

    def test_saturation_quantiles_include_actual_waits(self):
        self.store._connection_capacity = threading.BoundedSemaphore(1)
        gate = threading.Event()
        def borrow():
            gate.set()
            began = time.monotonic()
            with self.store.connect():
                return time.monotonic() - began
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            with self.store.connect():
                pending = [pool.submit(borrow) for _ in range(4)]
                self.assertTrue(gate.wait(1))
                time.sleep(.08)
            waits = [future.result(timeout=2) for future in pending]
        metrics = self.store.metrics()
        self.assertGreaterEqual(metrics['wait_p95_seconds'], .06)
        self.assertLessEqual(metrics['wait_p95_seconds'], max(waits) + .03)
        self.assertEqual(metrics['wait_quantile'], 'nearest_rank')
        self.assertEqual(metrics['borrowed'], 0)

    def test_timeouts_are_in_wait_samples_and_not_only_the_success_population(self):
        class ShortBudget:
            def acquire(self, timeout):
                time.sleep(.05)
                return False
        prior_samples = len(self.store._wait_samples)
        began = time.monotonic()
        with patch.object(self.store, '_connection_capacity', ShortBudget()):
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.connect():
                    self.fail('capacity must remain closed')
        elapsed = time.monotonic() - began
        metrics = self.store.metrics()
        self.assertEqual(metrics['timeouts'], 1)
        self.assertEqual(len(self.store._wait_samples), prior_samples + 2)
        self.assertAlmostEqual(metrics['wait_p95_seconds'], elapsed, delta=.025)
        self.assertEqual(metrics['waiting'], 0)
        self.assertEqual(metrics['borrowed'], 0)

    def test_stage_quarantine_is_bounded_preserves_every_column_and_handles_nulls(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)',
                           [('missing', str(index), 'signature'+str(index), json.dumps({'original': index}), index) for index in range(205)] +
                           [(None, None, 'null-one', 'one', 1), (None, None, 'null-two', 'two', 2)])
        original = self.store.query('SELECT * FROM stages')
        first = self.store.quarantine_orphans()
        self.assertEqual(first['stages_without_run'], 100)
        self.assertTrue(first['has_more'])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM stages')['n'], 107)
        self.store.quarantine_orphans()
        last = self.store.quarantine_orphans()
        self.assertFalse(last['has_more'])
        restored = [json.loads(row['data']) for row in self.store.query('SELECT data FROM relationship_quarantine')]
        self.assertCountEqual(restored, original)
        self.assertFalse(self.store.query('SELECT * FROM stages'))

    def test_failed_quarantine_rolls_back_every_removed_row(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("INSERT INTO stages VALUES ('missing','one','sig','data',1)")
        self.store.execute("CREATE TRIGGER quarantine_failure BEFORE INSERT ON relationship_quarantine BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.quarantine_orphans()
        self.assertEqual(self.store.one('SELECT data FROM stages')['data'], 'data')
        self.assertFalse(self.store.query('SELECT * FROM relationship_quarantine'))

    def test_relationship_audit_covers_evidence_links_and_narrow_probe_exceptions(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for project in ('p', 'q'):
                db.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,1)", (project, project, '/' + project))
            for task, project in (('t1','p'), ('t2','q'), ('bad-task','missing')):
                db.execute("INSERT INTO tasks(id,project,state) VALUES (?,?,'queued')", (task, project))
            db.execute("INSERT INTO runs(id,task,project) VALUES ('r','t1','q')")
            db.execute("INSERT INTO runs(id,task,project) VALUES ('orphan-run','missing-task','missing')")
            db.execute("INSERT INTO events(project,run) VALUES ('missing','missing-run')")
            db.execute("INSERT INTO events(project,run) VALUES ('model-center','model-test-123')")
            db.execute("INSERT INTO events(project,run) VALUES ('p','r')")
            db.execute("INSERT INTO events(project,run) VALUES (NULL,'r')")
            db.execute("INSERT INTO candidates(id,project,parent,kind) VALUES ('candidate','missing','missing-parent','strategy')")
            db.execute("INSERT INTO cases(id,project) VALUES ('case','missing')")
            db.execute("INSERT INTO skills(id,project,task) VALUES ('skill','missing','t2')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('call','missing','missing-run')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('mismatch','p','r')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('probe','model-center','model-test-123')")
            db.execute("INSERT INTO schedules(project,kind,due) VALUES ('missing','light',1)")
            db.execute("INSERT INTO task_decisions VALUES ('missing-task','{}',1)")
            db.execute("INSERT INTO task_revisions(task) VALUES ('missing-task')")
            db.execute("INSERT INTO task_revision_snapshots VALUES (999,'body','{}')")
            db.execute("INSERT INTO task_dependencies VALUES ('t1','t2')")
            db.execute("INSERT INTO task_group_members(group_task,original_task) VALUES ('t1','t2')")
            db.execute("INSERT INTO submissions VALUES ('p','request','signature','t2')")
            db.execute("INSERT INTO task_completions VALUES ('t1','q',1)")
            db.execute("INSERT INTO phase_times(run) VALUES ('missing-run')")
            db.execute("INSERT INTO remote_waits(run) VALUES ('missing-run')")
            db.execute("INSERT INTO stages VALUES ('missing-run','phase','sig','{}',1)")
        before = self.store.query('SELECT * FROM tasks')
        report = self.store.relationship_audit()
        for key, values in report.items():
            if not key.startswith('_'):
                self.assertTrue(values, key)
        self.assertEqual(len(report['events_without_project']), 1)
        self.assertEqual(len(report['event_project_mismatch']), 1)
        self.assertNotIn('probe', report['calls_without_project'])
        self.assertNotIn('probe', report['calls_without_run'])
        self.assertEqual(self.store.query('SELECT * FROM tasks'), before)

    def test_relationship_limits_disclose_truncation(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('missing', str(index), '', '{}', 1) for index in range(105)])
        report = self.store.relationship_audit()
        self.assertEqual(len(report['stages_without_run']), 100)
        self.assertTrue(report['_truncated']['stages_without_run'])
        report = self.store.relationship_audit(limit=200)
        self.assertEqual(len(report['stages_without_run']), 105)
        self.assertFalse(report['_truncated']['stages_without_run'])

    def test_http_request_id_is_available_to_slow_query_diagnostics(self):
        import urllib.request
        from adaptive_rsi.runtime import Runtime
        from adaptive_rsi.server import Server
        runtime = Runtime(self.store, agent=object())
        self.addCleanup(runtime.close)
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original, injected = self.store.query, []
        def slow_query(sql, args=()):
            if not injected:
                injected.append(True)
                with self.store.connect() as db:
                    db.create_function('slow_http_fixture', 1, lambda value: time.sleep(.12) or 1)
                original('SELECT slow_http_fixture(?)', ('private-query-binding',))
            return original(sql, args)
        request = urllib.request.Request(server.local_origin + '/api/events?secret=private-query-value',
                                         headers={'Cookie': server.cookie + '=' + server.token})
        with patch.object(self.store, 'query', side_effect=slow_query):
            with urllib.request.urlopen(request, timeout=3) as response:
                identity = response.headers['X-RSI-Request-ID']
                self.assertEqual(response.status, 200)
        report = self.store.metrics()['slow_queries'][-1]
        self.assertEqual(report['context'], {'request': identity})
        self.assertNotIn('private-query', json.dumps(report))

    def test_light_metrics_do_not_scan_large_history_but_explicit_integrity_finds_orphans(self):
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',1,1)")
        task = self.store.add_task('p','goal','valid','valid')
        self.store.execute("INSERT INTO runs(id,task,project) VALUES ('valid-run',?,'p')", (task,))
        with self.store.connect() as db:
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('valid-run', str(index), '', '{}', 1) for index in range(50000)])
            db.execute('PRAGMA foreign_keys=OFF')
        # Foreign key mode must change outside an active transaction.
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('missing', str(index), '', '{}', 1) for index in range(120)])
        began = time.monotonic()
        for _ in range(5):
            self.assertEqual(self.store.metrics()['foreign_key_check'], 'not_requested')
        self.assertLess(time.monotonic() - began, 1.0)
        integrity = self.store.metrics(integrity=True)
        self.assertEqual(integrity['foreign_key_violations'], 101)
        self.assertTrue(integrity['foreign_key_truncated'])
        audit = self.store.relationship_audit(limit=200)
        self.assertEqual(len(audit['stages_without_run']), 120)
        self.assertFalse(audit['_truncated']['stages_without_run'])
