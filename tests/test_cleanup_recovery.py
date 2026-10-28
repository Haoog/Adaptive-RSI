"""Cleanup filesystem/SQLite commit boundary regressions; entirely offline."""
import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.store import Store
from adaptive_rsi import maintenance


class CleanupRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.directory = self.store.root / 'runs' / 'r'
        (self.directory / 'workspace').mkdir(parents=True)
        (self.directory / 'workspace' / 'evidence').write_text('original')
        self.store.execute("INSERT INTO projects VALUES ('p','test','/unused','{}',0,0)")
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(self.directory),))

    @contextlib.contextmanager
    def failing_commit(self, committed=False):
        from adaptive_rsi.store import PooledConnection
        connect, commit = self.store.connect, PooledConnection.commit
        def fail(db):
            if getattr(db, '_inject_cleanup_commit', False):
                db._inject_cleanup_commit = False
                if committed:
                    commit(db)
                raise sqlite3.OperationalError('injected commit failure')
            return commit(db)
        @contextlib.contextmanager
        def broken():
            with connect() as db:
                before = db.total_changes
                yield db
                db._inject_cleanup_commit = db.total_changes != before
        with patch.object(self.store, 'connect', broken), patch.object(PooledConnection, 'commit', fail):
            yield

    def test_recycle_commit_failure_restores_original_workspace(self):
        preview = maintenance.preview_cleanup(self.store, 'r')
        with self.failing_commit(), self.assertRaises(sqlite3.OperationalError):
            maintenance.recycle_cleanup(self.store, preview)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNone(self.store.one("SELECT 1 FROM settings WHERE key LIKE 'recycle:%'"))

    def test_undo_commit_failure_remains_recoverable(self):
        receipt = maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        with self.failing_commit(), self.assertRaises(sqlite3.OperationalError):
            maintenance.undo_cleanup(self.store, receipt['id'])
        maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_committed_recycle_error_preserves_receipt_and_can_undo(self):
        with self.failing_commit(committed=True), self.assertRaises(sqlite3.OperationalError):
            maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        row = self.store.one("SELECT value FROM settings WHERE key LIKE 'recycle:%'")
        receipt = json.loads(row['value'])
        self.assertFalse((self.directory / 'workspace').exists())
        maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_committed_undo_error_does_not_move_workspace_back(self):
        receipt = maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        with self.failing_commit(committed=True), self.assertRaises(sqlite3.OperationalError):
            maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?', ('recycle:' + receipt['id'],)))
        self.assertFalse((self.store.root / 'recycle' / receipt['id']).exists())

    def test_unknown_outcome_retains_disk_receipt_until_restart(self):
        recover = maintenance._recover_cleanup_locked
        calls = 0
        def unavailable(store):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise sqlite3.OperationalError('database unavailable')
            return recover(store)
        with patch.object(maintenance, '_recover_cleanup_locked', unavailable), self.failing_commit():
            with self.assertRaisesRegex(sqlite3.OperationalError, 'unavailable'):
                maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        journals = list((self.store.root / 'recycle').glob('*/receipt.json'))
        self.assertEqual(len(journals), 1)
        self.assertEqual((journals[0].parent / 'workspace' / 'evidence').read_text(), 'original')
        root = self.store.root
        self.store.close()
        restarted = Store(root)
        self.addCleanup(restarted.close)
        maintenance.recover_cleanup(restarted)
        maintenance.recover_cleanup(restarted)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertFalse(journals[0].exists())

    def test_unknown_undo_outcome_reconciles_after_restart(self):
        receipt = maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        recover = maintenance._recover_cleanup_locked
        calls = 0
        def unavailable(store):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise sqlite3.OperationalError('database unavailable')
            return recover(store)
        with patch.object(maintenance, '_recover_cleanup_locked', unavailable), self.failing_commit():
            with self.assertRaises(sqlite3.OperationalError):
                maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        root = self.store.root
        self.store.close()
        restarted = Store(root)
        self.addCleanup(restarted.close)
        maintenance.recover_cleanup(restarted)
        maintenance.undo_cleanup(restarted, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_recovery_conflict_keeps_both_workspaces_and_journal(self):
        recover = maintenance._recover_cleanup_locked
        calls = 0
        def conflict(store):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.directory / 'workspace').mkdir()
                (self.directory / 'workspace' / 'new').write_text('new')
            return recover(store)
        with patch.object(maintenance, '_recover_cleanup_locked', conflict), self.failing_commit():
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        journal, = (self.store.root / 'recycle').glob('*/receipt.json')
        self.assertEqual((self.directory / 'workspace' / 'new').read_text(), 'new')
        self.assertEqual((journal.parent / 'workspace' / 'evidence').read_text(), 'original')
        (self.directory / 'workspace' / 'new').unlink()
        (self.directory / 'workspace').rmdir()
        maintenance.recover_cleanup(self.store)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_process_exit_before_commit_recovers_on_reopen(self):
        import subprocess
        import sys
        script = '''
import os
import sys
from adaptive_rsi.store import Store, PooledConnection
from adaptive_rsi.maintenance import preview_cleanup, recycle_cleanup
store = Store(sys.argv[1])
commit = PooledConnection.commit

def crash(db):
    if db.execute("SELECT 1 FROM settings WHERE key LIKE 'recycle:%'").fetchone():
        os._exit(73)
    return commit(db)
PooledConnection.commit = crash
recycle_cleanup(store, preview_cleanup(store, 'r'))
'''
        self.store.close()
        result = subprocess.run([sys.executable, '-c', script, str(self.store.root)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 73, result.stderr)
        journal, = (self.store.root / 'recycle').glob('*/receipt.json')
        restarted = Store(self.store.root)
        self.addCleanup(restarted.close)
        maintenance.recover_cleanup(restarted)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertFalse(journal.exists())

    def test_maintenance_reconciles_before_hourly_gate(self):
        owner = maintenance.Maintenance()
        setattr(owner, 'store', self.store)
        import time
        self.store.put_setting('maintenance-last', time.time())
        (self.store.root / 'recycle').mkdir()
        with patch.object(maintenance, 'recover_cleanup') as recover:
            maintenance.Maintenance.maintenance(owner)
        recover.assert_called_once_with(self.store)

    def test_atomic_rename_refuses_even_empty_destination(self):
        source, target = self.directory / 'workspace', self.directory / 'candidate'
        target.mkdir()
        with self.assertRaises(FileExistsError):
            maintenance._rename_cleanup(source, target)
        self.assertEqual((source / 'evidence').read_text(), 'original')
        self.assertTrue(target.is_dir())

    def test_partial_recycle_move_failure_restores_all_artifacts(self):
        (self.directory / 'candidate').mkdir()
        (self.directory / 'candidate' / 'evidence').write_text('candidate')
        rename = maintenance._rename_cleanup
        calls = 0
        def fail(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected move failure')
            return rename(source, target)
        with patch.object(maintenance, '_rename_cleanup', fail), self.assertRaises(OSError):
            maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertEqual((self.directory / 'candidate' / 'evidence').read_text(), 'candidate')
