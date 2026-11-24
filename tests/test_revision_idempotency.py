import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.schema_migrations import SCRIPTS
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


class RevisionIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        self.addCleanup(self.runtime.close)
        project = self.runtime.add_project(fixture(self.root / 'repo'))
        self.task = self.runtime.goal(project, 'Original')
        self.other = self.runtime.goal(project, 'Other')

    def test_concurrency_restart_conflicts_and_new_intent(self):
        other_store = Store(self.root / 'state')
        other_runtime = Runtime(other_store, FakeAgent())
        self.addCleanup(other_runtime.close)
        barrier = threading.Barrier(2)

        def submit(runtime):
            barrier.wait()
            return runtime.revise_task(self.task, 'extra', request_id='shared')

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, runtime) for runtime in (self.runtime, other_runtime)]
            self.assertEqual([future.result() for future in futures],
                             [{'saved': True, 'pending': False}] * 2)
        self.assertEqual(len(self.store.query('SELECT * FROM task_revisions')), 1)
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='requirements_revised'")), 1)
        other_runtime.close()
        other_runtime = Runtime(Store(self.root / 'state'), FakeAgent())
        self.addCleanup(other_runtime.close)
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (self.task,))
        self.assertEqual(other_runtime.revise_task(self.task, 'extra', request_id='shared'),
                         {'saved': True, 'pending': False})
        for task, text, reason in [(self.other, 'extra', None), (self.task, 'changed', None),
                                   (self.task, 'extra', 'changed')]:
            with self.assertRaises(ValueError):
                other_runtime.revise_task(task, text, reason, request_id='shared')
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.task,))
        self.runtime.revise_task(self.task, 'extra', request_id='new')
        self.runtime.revise_task(self.task, 'extra')
        self.assertEqual(len(self.store.query('SELECT * FROM task_revisions')), 3)
        self.assertEqual(self.store.one('SELECT body FROM tasks WHERE id=?', (self.task,))['body'].count('extra'), 3)

    def test_retry_returns_original_pending_receipt_after_run_finishes(self):
        self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (self.task,))
        self.store.execute("INSERT INTO runs(id,task,state) VALUES (?,?,?)", ('run-1', self.task, 'executing'))
        receipt = self.runtime.revise_task(self.task, 'extra', request_id='pending')
        self.assertEqual(receipt, {'saved': True, 'pending': True})
        self.store.execute("UPDATE runs SET ended=1 WHERE id='run-1'")
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.task,))
        self.assertEqual(self.runtime.revise_task(self.task, 'extra', request_id='pending'), receipt)
        self.assertEqual(len(self.store.query('SELECT * FROM task_revisions')), 1)

    def test_receipt_survives_revision_history_pruning(self):
        receipt = self.runtime.revise_task(self.task, 'extra', request_id='retained')
        self.store.execute('DELETE FROM task_revision_snapshots')
        self.store.execute('DELETE FROM task_revisions')
        self.assertEqual(self.runtime.revise_task(self.task, 'extra', request_id='retained'), receipt)
        self.assertEqual(self.store.one('SELECT body FROM tasks WHERE id=?', (self.task,))['body'].count('extra'), 1)

    def test_failure_rolls_back_request_and_revision(self):
        self.store.execute("CREATE TRIGGER fail_revision_request BEFORE INSERT ON revision_requests BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.revise_task(self.task, 'extra', request_id='retry')
        self.assertEqual(len(self.store.query('SELECT * FROM task_revisions')), 0)
        self.assertEqual(len(self.store.query('SELECT * FROM revision_requests')), 0)
        self.assertNotIn('extra', self.store.one('SELECT body FROM tasks WHERE id=?', (self.task,))['body'])
        self.store.execute('DROP TRIGGER fail_revision_request')
        self.assertEqual(self.runtime.revise_task(self.task, 'extra', request_id='retry'),
                         {'saved': True, 'pending': False})


class RevisionRequestMigrationTest(unittest.TestCase):
    def test_upgrade_keeps_task_recovery_migration_and_existing_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            with patch('adaptive_rsi.schema_migrations.SCRIPTS', SCRIPTS[:-1]):
                previous = Store(state)
                with previous.connect() as db:
                    db.execute("INSERT INTO projects VALUES ('p','fixture','/fixture','{}',0,0)")
                task = previous.add_task('p', 'goal', 'existing', 'Existing task')
                recovery = previous.one('SELECT checksum FROM schema_migrations WHERE version=11')['checksum']
                previous.close()
            upgraded = Store(state)
            self.addCleanup(upgraded.close)
            self.assertEqual(upgraded.one('SELECT checksum FROM schema_migrations WHERE version=11')['checksum'], recovery)
            self.assertIsNotNone(upgraded.one('SELECT version FROM schema_migrations WHERE version=12'))
            self.assertEqual(upgraded.one('SELECT title FROM tasks WHERE id=?', (task,))['title'], 'Existing task')
            self.assertEqual(upgraded.query('SELECT * FROM revision_requests'), [])
