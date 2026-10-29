"""Offline regression evidence for runtime handoff boundaries."""
import datetime
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import validate
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_runtime import FakeAgent, fixture


class RuntimeHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / 'project'))
        self.runtime.configure({'sources': {'inspection': False, 'issues': False},
                                'evolution': {'enabled': False}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def claim(self):
        task = self.runtime.goal(self.project, 'Check boundary')
        self.runtime.pause(self.project, False)
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    def test_shutdown_progress_is_readable_while_worker_drains(self):
        gate, began = threading.Event(), threading.Event()
        self.runtime.futures.add(self.runtime.pool.submit(lambda: (began.set(), gate.wait(5))))
        began.wait(1)
        closer = threading.Thread(target=self.runtime.close)
        closer.start()
        try:
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            status = self.runtime.shutdown_status()
            self.assertEqual(status['phase'], 'workers')
            self.assertEqual(status['remaining_workers'], 1)
        finally:
            gate.set()
            closer.join(5)
        self.assertEqual(self.runtime.shutdown_status()['phase'], 'completed')

    def test_shutdown_rejects_claim_once_the_admission_boundary_closes(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, 'Work that must not be claimed while closing')
        self.runtime._closing = True
        self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)

    def test_shutdown_cancels_unstarted_worker_and_requeues_claimed_task(self):
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Queued across shutdown')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            self.assertEqual(self.runtime.shutdown_status()['phase'], 'workers')
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            saved = self.store.one('SELECT state,ended FROM runs WHERE id=?', (run['id'],))
            self.assertEqual(saved['state'], 'interrupted')
            self.assertIsNotNone(saved['ended'])
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')
            self.assertEqual(self.runtime._future_runs, {})
        finally:
            gate.set()

    def test_shutdown_cancel_before_start_does_not_requeue_cancelled_task(self):
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Cancelled while shutting down')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            self.runtime.cancel_task(task)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'cancelled')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'cancelled')
        finally:
            gate.set()

    def test_shutdown_pause_before_start_keeps_task_interrupted(self):
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Paused while shutting down')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            self.runtime.pause(self.project, True)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'interrupted')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'interrupted')
        finally:
            gate.set()

    def test_preparation_deadline_stops_real_subprocess_with_stage_receipt(self):
        import sys
        from adaptive_rsi.process import checked
        self.runtime.configure({'deadlines': {'preparation_seconds': 1}}, self.project)
        task, run = self.claim()
        def slow(*args):
            checked([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=20)
        started = time.monotonic()
        with patch('adaptive_rsi.delivery.baseline', side_effect=slow):
            self.runtime.process(run)
        self.assertLess(time.monotonic() - started, 4)
        row = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(row['state'], 'blocked')
        self.assertEqual(json.loads(row['result'])['failure']['stage'], 'preparation')

    def test_trial_phase_clock_ignores_parent_planning_and_propagates_timeout(self):
        _, parent = self.claim()
        parent['_deadline_started'] = time.monotonic() - 5000
        parent['_deadline_stage'] = 'preparation'
        trial = self.runtime.new_trial_context(parent)
        self.runtime.begin_phase(trial, 'evaluation')
        self.assertFalse(self.runtime.is_cancelled(trial))
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (parent['id'],))['state'], 'planning')
        trial['_deadline_started'] -= 4000
        self.assertTrue(self.runtime.is_cancelled(trial))
        self.runtime.propagate_deadline(parent, trial)
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')
        self.assertNotIn('_timed_out_stage', self.runtime.new_trial_context(parent))

    def test_postprocess_does_not_reload_completed_history_after_migration(self):
        for index in range(1000):
            self.store.put_setting('postprocess:history-' + str(index), {'state': 'completed', 'steps': []})
        self.runtime.drain_postprocess()
        loaded = []
        original = self.store.query
        def observe(sql, args=()):
            rows = original(sql, args)
            if 'settings' in sql:
                loaded.extend(rows)
            return rows
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime.drain_postprocess()
        self.assertEqual(loaded, [])

    def test_postprocess_recovers_without_duplicate_observation(self):
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified outcome'}, True)
        with patch.object(self.runtime, 'maybe_evolve', side_effect=RuntimeError('crash')):
            self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['last_error'], 'crash')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 1)
        self.runtime.drain_postprocess()
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 1)
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')

    def test_postprocess_rotates_past_a_full_batch_of_transient_failures(self):
        from adaptive_rsi.runtime import POSTPROCESS_BATCH
        for index in range(POSTPROCESS_BATCH + 1):
            self.runtime.queue_postprocess({'id': f'{index:04d}'})
        seen = []
        def process(key, identifier, record):
            seen.append(identifier)
            if identifier != f'{POSTPROCESS_BATCH:04d}':
                raise RuntimeError('temporary failure')
            self.store.put_setting(key, {**record, 'state': 'completed'})
        with patch.object(self.runtime, '_postprocess_run', side_effect=process):
            self.runtime.drain_postprocess()
            self.runtime.drain_postprocess()
        self.assertIn(f'{POSTPROCESS_BATCH:04d}', seen)

    def test_corrupt_postprocess_records_do_not_block_healthy_work(self):
        for identifier, value in [('corrupt', '{'), ('array', '[]'),
                                  ('bad-steps', '{"state":"pending","steps":null}')]:
            self.store.execute('INSERT INTO settings VALUES (?,?)', ('postprocess:' + identifier, value))
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        self.assertEqual(self.store.one("SELECT value FROM settings WHERE key='postprocess:corrupt'")['value'], '{')

    def test_postprocess_archived_project_does_not_block_another_project(self):
        other = self.runtime.add_project(fixture(self.root / 'other'))
        runs = []
        for project in (self.project, other):
            self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
            self.runtime.goal(project, 'Pending delivery')
            self.runtime.pause(project, False)
            run = self.store.claim(project, self.runtime.config(project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
            runs.append(run)
        self.runtime.archive_project(self.project, 'archive-project')
        with patch.object(self.runtime, 'deliver') as deliver:
            self.runtime.drain_postprocess()
        deliver.assert_called_once_with(runs[1]['id'], automatic=True)
        retired = self.store.setting('postprocess:' + runs[0]['id'])
        self.assertEqual(retired['state'], 'obsolete')
        self.assertIn('archived', retired['reason'])
        self.assertEqual(self.store.setting('postprocess:' + runs[1]['id'])['state'], 'completed')

    def test_postprocess_transient_failure_is_isolated_and_retryable(self):
        other = self.runtime.add_project(fixture(self.root / 'other'))
        runs = []
        for project in (self.project, other):
            self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
            self.runtime.goal(project, 'Pending delivery')
            self.runtime.pause(project, False)
            run = self.store.claim(project, self.runtime.config(project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
            runs.append(run)
        def transient(run_id, automatic=False):
            if run_id == runs[0]['id']:
                raise RuntimeError('temporary failure')
        with patch.object(self.runtime, 'deliver', side_effect=transient):
            self.runtime.drain_postprocess()
        pending = self.store.setting('postprocess:' + runs[0]['id'])
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['steps'], [])
        self.assertEqual(pending['last_error'], 'temporary failure')
        self.assertEqual(self.store.setting('postprocess:' + runs[1]['id'])['state'], 'completed')
        with patch.object(self.runtime, 'deliver') as deliver:
            self.runtime.drain_postprocess()
        deliver.assert_called_once_with(runs[0]['id'], automatic=True)
        self.assertEqual(self.store.setting('postprocess:' + runs[0]['id'])['state'], 'completed')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 2)

    def test_postprocess_pending_index_is_bounded_and_range_scanned(self):
        from adaptive_rsi import runtime as runtime_module
        batch = getattr(runtime_module, 'POSTPROCESS_BATCH', 20)
        self.runtime.pause(self.project, False)
        total = batch + 3
        for index in range(total):
            self.runtime.goal(self.project, 'Postprocess item ' + str(index))
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        statements = []
        original = self.store.query
        def observe(sql, args=()):
            statements.append(sql)
            return original(sql, args)
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime.drain_postprocess()
        pending_sql = [sql for sql in statements if 'FROM settings' in sql and 'ORDER BY key LIMIT' in sql]
        self.assertTrue(pending_sql)
        self.assertTrue(all('LIKE' not in sql for sql in pending_sql))
        self.assertTrue(any('LIMIT' in sql and 'key>' in sql for sql in pending_sql))
        completed = self.store.one("SELECT count(*) AS n FROM settings WHERE key LIKE 'postprocess:%' "
                                   "AND json_valid(value) AND json_extract(value,'$.state')='completed'")['n']
        self.assertEqual(completed, batch)
        markers = self.store.one("SELECT count(*) AS n FROM settings WHERE key>=? AND key<?",
                                 ('postprocess-pending:', 'postprocess-pending;'))['n']
        self.assertEqual(markers, total - batch)
        plan = self.store.query("EXPLAIN QUERY PLAN SELECT key FROM settings WHERE key>=? AND key<? "
                                "ORDER BY key LIMIT ?", ('postprocess-pending:', 'postprocess-pending;', batch))
        detail = ' '.join(str(row.get('detail', '')) for row in plan).upper()
        self.assertTrue('PRIMARY KEY' in detail or 'INDEX' in detail)
        for _ in range(total):
            self.runtime.drain_postprocess()
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM settings WHERE key>=? AND key<?",
                                        ('postprocess-pending:', 'postprocess-pending;'))['n'], 0)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM settings WHERE key LIKE 'postprocess:%' "
                                        "AND json_valid(value) AND json_extract(value,'$.state')='completed'")['n'], total)

    def test_legacy_pending_postprocess_is_indexed_for_upgrade(self):
        task, run = self.claim()
        self.store.put_setting('postprocess:' + run['id'], {'state': 'pending', 'steps': [], 'created': time.time()})
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime._index_postprocess_pending()
        self.assertEqual(self.store.setting('postprocess-pending-index'), 2)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        statements = []
        original = self.store.query
        def observe(sql, args=()):
            statements.append(sql)
            return original(sql, args)
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime._index_postprocess_pending()
        self.assertEqual([sql for sql in statements if 'key>=?' in sql], [])

    def test_indexed_corrupt_postprocess_is_retired_without_blocking_healthy_work(self):
        self.store.execute("INSERT INTO settings VALUES ('postprocess:0000000000000000', '{')")
        self.store.execute("INSERT INTO settings VALUES ('postprocess-pending:0000000000000000', ?)",
                           (encode({}),))
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        self.assertEqual(self.store.one("SELECT value FROM settings WHERE key='postprocess:0000000000000000'")['value'], '{')
        self.assertIsNone(self.store.setting('postprocess-pending:0000000000000000'))

    def test_operation_crash_after_effect_is_unknown_and_never_reexecuted(self):
        operation = '0123456789abcdef'
        self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                           (operation, 'import', 'running', time.time()))
        self.runtime.operations.recover()
        row = self.runtime.operations.get(operation)
        self.assertEqual(row['state'], 'interrupted')
        self.assertEqual(row['result']['outcome'], 'unknown')

    def test_issue_sync_retains_original_diff_and_source_without_permission_change(self):
        task = self.store.add_task(self.project, 'issue', 'issue:1', 'Original', 'Original body',
                                   [{'number': 1, 'url': 'https://github.com/a/b/issues/1', 'updated': 'old'}])
        def sync(*args, **kwargs):
            self.store.execute('UPDATE tasks SET body=?,evidence=? WHERE id=?',
                ('New body', encode([{'number': 1, 'url': 'https://github.com/a/b/issues/1', 'updated': 'new'}]), task))
        self.runtime.configure({'github': {'repository': 'a/b'}}, self.project)
        with patch('adaptive_rsi.discovery.sync', side_effect=sync), patch('adaptive_rsi.runtime.checked', return_value='[]'):
            self.runtime.sync_issues(self.runtime.project(self.project), self.runtime.config(self.project))
        row = self.store.one("SELECT data FROM events WHERE kind='issue_requirements_diff'")
        record = json.loads(row['data'])
        self.assertEqual(record['before']['body'], 'Original body')
        self.assertEqual(record['after']['body'], 'New body')
        self.assertEqual(record['source']['updated'], 'new')
        self.assertIn('-Original body', record['diff'])
        self.assertEqual(self.runtime.config(self.project)['delivery']['mode'], 'local')

    def test_idle_projects_use_deadline_cache_and_wake_invalidates_it(self):
        self.runtime.pause(self.project, False)
        self.assertEqual(len(self.runtime.scheduler_projects(now=100)), 1)
        self.runtime.defer_project_tick(self.project, self.runtime.config(self.project), now=100)
        self.runtime.wake.clear()
        self.assertEqual(self.runtime.scheduler_projects(now=104), [])
        self.runtime.goal(self.project, 'New user work')
        self.assertEqual(len(self.runtime.scheduler_projects(now=104)), 1)

    def test_model_deadline_cancels_actual_command(self):
        import sys
        from adaptive_rsi.process import Cancelled, checked
        self.runtime.configure({'deadlines': {'model_seconds': 1}}, self.project)
        task, run = self.claim()
        def slow(role, text, workspace, project, run_id, config, cancel, protected):
            checked([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=20, cancel=cancel)
        with patch.object(self.runtime.agent, 'invoke', side_effect=slow):
            with self.assertRaises(Cancelled):
                self.runtime.invoke('planning', 'work', self.root, run)
        self.assertEqual(run['_timed_out_stage'], 'model')

    def test_evaluation_deadline_has_its_own_clock(self):
        task, run = self.claim()
        run['_deadline_started'], run['_deadline_stage'] = time.monotonic() - 5000, 'preparation'
        self.store.transition(run['id'], 'evaluating')
        self.assertFalse(self.runtime.is_cancelled(run))
        run['_deadline_started'] -= 4000
        self.assertTrue(self.runtime.is_cancelled(run))
        self.assertEqual(run['_timed_out_stage'], 'evaluation')

    def test_idle_deadline_selection_scales_to_thousand_projects(self):
        import time as clock
        now = clock.time()
        for count in (100, 1000):
            with self.store.connect() as db:
                db.executemany('INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?,?)',
                    [('load' + str(i), 'Load', '/offline/load/' + str(i), '{}', 0, now) for i in range(count)])
            self.runtime._project_deadlines = {'load' + str(i): now + 3600 for i in range(count)}
            self.runtime._project_deadlines[self.project] = now + 3600
            self.runtime.wake.clear()
            began = clock.process_time()
            for _ in range(10):
                self.assertEqual(self.runtime.scheduler_projects(now), [])
            print('deadline_selection', {'projects': count, 'ticks': 10, 'queries': 10,
                                         'cpu_seconds': clock.process_time() - began})
            self.runtime._project_deadlines['load0'] = now
            self.assertEqual([row['id'] for row in self.runtime.scheduler_projects(now)], ['load0'])

    def test_operation_effect_receipt_crash_recovers_receipt_without_repeating_effect(self):
        operation = 'fedcba9876543210'
        self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                           (operation, 'import', 'queued', time.time()))
        effect = self.root / 'effect'
        def execute(*args):
            effect.write_text('one effect')
            return {'id': 'local-evidence'}
        original = self.store.execute
        def crash(sql, *args, **kwargs):
            if sql.startswith('UPDATE operations SET state=?,result='):
                raise OSError('database unavailable after effect')
            return original(sql, *args, **kwargs)
        with patch.object(self.runtime.operations, '_execute', side_effect=execute) as call, patch.object(self.store, 'execute', side_effect=crash):
            self.runtime.operations._run(operation, 'import', {}, threading.Event())
            self.assertEqual(call.call_count, 1)
        self.runtime.operations.recover()
        self.assertEqual(self.runtime.operations.get(operation)['state'], 'completed')
        self.assertEqual(effect.read_text(), 'one effect')

    def test_shutdown_rejects_new_worker_admission(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, 'Do not start after stop')
        self.runtime.stop.set()
        self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)

    def test_auto_evolution_skips_unchanged_failed_inputs_until_inputs_change(self):
        from adaptive_rsi.evolution import Evolution
        self.runtime.configure({'evolution': {'enabled': True, 'every_completed': 1}}, self.project)
        done = self.store.add_task(self.project, 'goal', 'done-1', 'Done once', 'done once')
        self.store.execute('INSERT OR IGNORE INTO task_completions VALUES (?,?,?)',
                           (done, self.project, time.time()))
        config = self.runtime.config(self.project)
        signature = Evolution(self.runtime).attempt_due(self.project, config)[1]
        self.store.put_setting('evolution-attempt:' + self.project,
                               {'signature': signature, 'status': 'insufficient_evidence', 'at': time.time()})
        self.runtime.maybe_evolve(self.project, config)
        self.assertFalse(self.store.query("SELECT 1 FROM tasks WHERE source='system_evolve'"))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='evolution_not_repeated'"))
        self.assertEqual(self.store.setting('last-evolution:' + self.project), 1)
        again = self.store.add_task(self.project, 'goal', 'done-2', 'Done twice', 'done twice')
        self.store.execute('INSERT OR IGNORE INTO task_completions VALUES (?,?,?)',
                           (again, self.project, time.time()))
        self.runtime.configure({'triage': {'policy': 'prefer verifiable evidence'}}, self.project)
        self.runtime.maybe_evolve(self.project, self.runtime.config(self.project))
        self.assertTrue(self.store.one("SELECT 1 FROM tasks WHERE source='system_evolve' AND state='queued'"))

    def test_evolution_force_retry_marker_is_consumed_and_task_is_retryable(self):
        from adaptive_rsi.gitops import git
        self.runtime.configure({'evolution': {'enabled': True}}, self.project)
        task = self.runtime.request(self.project, 'evolve')
        self.runtime.pause(self.project, False)
        calls = []
        def fake_propose(evolution, project, workspace, directory, run, force=False):
            calls.append(force)
            return {'status': 'insufficient_evidence', 'reason': 'No fresh holdout'}
        path = self.runtime.project(self.project)['path']
        base = git(path, 'rev-parse', 'HEAD')
        with patch('adaptive_rsi.delivery.baseline', return_value=(path, base, 'main')), \
                patch('adaptive_rsi.evolution.Evolution.propose_and_evaluate',
                      autospec=True, side_effect=fake_propose):
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(run)
            self.assertEqual(calls, [False])
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'],
                             'insufficient_evidence')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'],
                             'insufficient_evidence')
            self.runtime.retry(task, force=True)
            self.assertIsNotNone(self.store.setting('evolution-force:' + task))
            fresh = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(fresh)
            self.assertEqual(calls, [False, True])
            self.assertIsNone(self.store.setting('evolution-force:' + task))
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (fresh['id'],))['state'],
                             'insufficient_evidence')

    def test_forced_retry_is_rejected_for_non_evolution_tasks(self):
        task = self.store.add_task(self.project, 'goal', 'forced-goal', 'Goal', 'Goal')
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        with self.assertRaisesRegex(ValueError, 'evolution'):
            self.runtime.retry(task, force=True)

    def test_stage_limits_reject_invalid_values(self):
        self.assertEqual(validate({'deadlines': {'preparation_seconds': 2}})['deadlines']['preparation_seconds'], 2)
        for value in [0, -1, True, 86401]:
            with self.assertRaises(ValueError):
                validate({'deadlines': {'preparation_seconds': value}})

    def test_scheduled_wall_time_rejects_gap_and_requires_fold(self):
        task, _ = self.claim()
        # Use a separate waiting task; active work cannot be rescheduled.
        task = self.runtime.goal(self.project, 'Later work')
        with self.assertRaisesRegex(ValueError, 'nonexistent'):
            self.runtime.schedule_task(task, '2027-03-14T02:30:00', 'America/New_York')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York')
        first = self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York', fold=0)
        second = self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York', fold=1)
        self.assertEqual(second['at'] - first['at'], 3600)
        self.runtime.configure({'schedule': {'timezone': 'UTC'}}, self.project)
        self.assertEqual(self.store.one('SELECT available FROM tasks WHERE id=?', (task,))['available'], second['at'])
        self.assertEqual(self.store.setting('task-schedule:' + task)['timezone'], 'America/New_York')
