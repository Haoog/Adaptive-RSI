"""Cleanup filesystem/SQLite commit boundary regressions; entirely offline."""
import contextlib
import json
import os
import sqlite3
import tempfile
import time
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
            with self.assertRaises(sqlite3.OperationalError):
                maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        journal, = (self.store.root / 'recycle').glob('*/receipt.json')
        self.assertEqual((self.directory / 'workspace' / 'new').read_text(), 'new')
        self.assertEqual((journal.parent / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='cleanup_recovery_conflict'"))
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

    def test_linux_rename_fallback_when_libc_lacks_renameat2(self):
        import sys
        if sys.platform != 'linux':
            self.skipTest('Linux-specific fallback test')
        import ctypes
        real_cdll = ctypes.CDLL

        class LibcWithoutRenameat2:
            def __init__(self, lib):
                self._lib = lib
            def syscall(self, *args):
                calls.append(args)
                orig = getattr(self._lib, 'syscall', None)
                if orig is not None:
                    return orig(*args)
                raise AttributeError('syscall')
            def __getattr__(self, name):
                if name == 'renameat2':
                    raise AttributeError('simulated libc without renameat2')
                return getattr(self._lib, name)

        calls = []
        def fake_cdll(name, use_errno=False):
            return LibcWithoutRenameat2(real_cdll(name, use_errno=use_errno))

        source, target = self.directory / 'workspace', self.directory / 'candidate'
        target.mkdir()
        with patch('ctypes.CDLL', side_effect=fake_cdll):
            with self.assertRaises(FileExistsError):
                maintenance._rename_cleanup(source, target)
            self.assertGreater(len(calls), 0)
            target.rmdir()
            maintenance._rename_cleanup(source, target)
            self.assertTrue(target.exists())
            self.assertEqual((target / 'evidence').read_text(), 'original')
            self.assertFalse(source.exists())
            maintenance._rename_cleanup(target, source)
            self.assertTrue(source.exists())

    def test_linux_rename_unavailable_raises_when_no_primitive(self):
        import sys
        if sys.platform != 'linux':
            self.skipTest('Linux-specific fallback test')
        class LibcWithoutAny:
            def __getattr__(self, name):
                raise AttributeError(name)
        with patch('ctypes.CDLL', return_value=LibcWithoutAny()):
            with self.assertRaisesRegex(OSError, 'Atomic no-replace cleanup rename unavailable'):
                maintenance._rename_cleanup(self.directory / 'workspace', self.directory / 'candidate')

    def _make_valid_recycle(self, identifier, committed=True):
        """Rebuild one recycle: durable journal, moved artifact, optionally the committed row."""
        import os
        import time
        preview = maintenance.preview_cleanup(self.store, 'r')
        folder = self.store.root / 'recycle' / identifier
        folder.mkdir(parents=True, mode=0o700)
        receipt = {**preview, 'id': identifier, 'expires': time.time() + 7 * 86400}
        (folder / 'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        os.rename(self.directory / 'workspace', folder / 'workspace')
        if committed:
            self.store.execute("INSERT INTO settings VALUES (?,?)",
                               ('recycle:' + identifier, json.dumps(receipt)))
        return receipt

    def test_corrupt_receipt_is_quarantined_and_other_receipts_still_recovered(self):
        import hashlib
        self._make_valid_recycle('b' * 32, committed=False)
        corrupt = self.store.root / 'recycle' / ('0' * 32)
        corrupt.mkdir()
        (corrupt / 'receipt.json').write_text('{broken', encoding='utf-8')
        maintenance.recover_cleanup(self.store)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertFalse((self.store.root / 'recycle' / ('b' * 32)).exists())
        quarantined = self.store.root / 'recycle' / ('receipt.invalid-' + '0' * 32)
        self.assertEqual((quarantined / 'receipt.json').read_text(encoding='utf-8'), '{broken')
        self.assertFalse((corrupt / 'receipt.json').exists())
        evidence = json.loads(self.store.one(
            'SELECT value FROM settings WHERE key=?',
            ('cleanup-receipt-quarantine:' + '0' * 32,))['value'])
        self.assertEqual(evidence['error_type'], 'JSONDecodeError')
        self.assertEqual(evidence['receipt_sha256'], hashlib.sha256(b'{broken').hexdigest())
        event = self.store.one("SELECT message,data FROM events WHERE kind='cleanup_receipt_quarantined'")
        self.assertIn('0' * 32, event['message'])
        self.assertEqual(json.loads(event['data'])['folder'], '0' * 32)
        maintenance.recover_cleanup(self.store)
        self.assertEqual(self.store.one("SELECT count(*) AS c FROM events "
                                        "WHERE kind='cleanup_receipt_quarantined'")['c'], 1)

    def test_each_corrupt_receipt_is_isolated_individually(self):
        payloads = [
            ('1' * 32, '{"id":"' + '1' * 32 + '","run":"../escape","objects":[]}'),
            ('2' * 32, '[1,2]'),
            ('3' * 32, '{"id":"' + '4' * 32 + '","run":"r","objects":[]}'),
            ('4' * 32, '{"id":"' + '4' * 32 + '","run":"r"}'),
        ]
        for identifier, payload in payloads:
            folder = self.store.root / 'recycle' / identifier
            folder.mkdir(parents=True)
            (folder / 'receipt.json').write_text(payload, encoding='utf-8')
        self._make_valid_recycle('f' * 32, committed=False)
        maintenance.recover_cleanup(self.store)
        for identifier, payload in payloads:
            quarantined = self.store.root / 'recycle' / ('receipt.invalid-' + identifier)
            self.assertEqual((quarantined / 'receipt.json').read_text(encoding='utf-8'), payload, identifier)
            self.assertIsNotNone(self.store.one(
                'SELECT 1 FROM settings WHERE key=?',
                ('cleanup-receipt-quarantine:' + identifier,)), identifier)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_quarantined_receipt_unblocks_recycle_and_undo(self):
        corrupt = self.store.root / 'recycle' / ('0' * 32)
        corrupt.mkdir(parents=True)
        (corrupt / 'receipt.json').write_text('{broken', encoding='utf-8')
        maintenance.recover_cleanup(self.store)
        receipt = maintenance.recycle_cleanup(self.store, maintenance.preview_cleanup(self.store, 'r'))
        self.assertFalse((self.directory / 'workspace').exists())
        maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_disk_and_database_disagreement_is_quarantined_with_row_kept(self):
        identifier = 'c' * 32
        receipt = self._make_valid_recycle(identifier)
        self.store.execute("UPDATE settings SET value=? WHERE key=?",
                           (json.dumps({**receipt, 'objects': []}), 'recycle:' + identifier))
        maintenance.recover_cleanup(self.store)
        quarantined = self.store.root / 'recycle' / ('receipt.invalid-' + identifier)
        self.assertTrue((quarantined / 'receipt.json').exists())
        self.assertIsNotNone(self.store.one('SELECT value FROM settings WHERE key=?',
                                            ('recycle:' + identifier,)))
        evidence = json.loads(self.store.one(
            'SELECT value FROM settings WHERE key=?',
            ('cleanup-receipt-quarantine:' + identifier,))['value'])
        self.assertEqual(evidence['error_type'], 'ValueError')
        self.assertIn('disagree', evidence['reason'])

    def test_invalid_database_row_recovers_from_disk_journal(self):
        identifier = 'd' * 32
        self._make_valid_recycle(identifier)
        self.store.execute("UPDATE settings SET value='not-json' WHERE key=?", ('recycle:' + identifier,))
        maintenance.recover_cleanup(self.store)
        row = self.store.one('SELECT value FROM settings WHERE key=?', ('recycle:' + identifier,))
        disk = (self.store.root / 'recycle' / identifier / 'receipt.json').read_text(encoding='utf-8')
        self.assertEqual(row['value'], disk)
        event = self.store.one("SELECT data FROM events WHERE kind='cleanup_receipt_row_invalid'")
        self.assertEqual(json.loads(event['data'])['folder'], identifier)
        maintenance.undo_cleanup(self.store, identifier)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_placement_conflict_is_isolated_and_quarantine_persists(self):
        self._make_valid_recycle('e' * 32, committed=False)
        corrupt = self.store.root / 'recycle' / ('0' * 32)
        corrupt.mkdir()
        (corrupt / 'receipt.json').write_text('{broken', encoding='utf-8')
        original = maintenance._place_cleanup
        def conflicting(store, receipt, recycled):
            (self.directory / 'workspace').mkdir()
            (self.directory / 'workspace' / 'new').write_text('new')
            return original(store, receipt, recycled)
        with patch.object(maintenance, '_place_cleanup', conflicting):
            maintenance.recover_cleanup(self.store)
        self.assertTrue((self.store.root / 'recycle' / ('e' * 32) / 'receipt.json').exists())
        self.assertEqual((self.directory / 'workspace' / 'new').read_text(), 'new')
        self.assertTrue((self.store.root / 'recycle' / ('receipt.invalid-' + '0' * 32) / 'receipt.json').exists())
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('cleanup-receipt-quarantine:' + '0' * 32,)))
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('cleanup-recovery-conflict:' + 'e' * 32,)))
        self.assertEqual(self.store.one("SELECT count(*) AS c FROM events WHERE kind='cleanup_recovery_conflict'")['c'], 1)
        (self.directory / 'workspace' / 'new').unlink()
        (self.directory / 'workspace').rmdir()
        maintenance.recover_cleanup(self.store)
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('cleanup-recovery-conflict:' + 'e' * 32,)))

    def test_conflict_isolation_allows_other_receipts_to_recover_and_unblocks_operations(self):
        d1 = self.directory
        d2 = self.store.root / 'runs' / 'r2'
        (d2 / 'workspace').mkdir(parents=True)
        (d2 / 'workspace' / 'evidence').write_text('evidence-two')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r2','p','failed',?,1,'{}')", (str(d2),))

        d3 = self.store.root / 'runs' / 'r3'
        (d3 / 'workspace').mkdir(parents=True)
        (d3 / 'workspace' / 'evidence').write_text('evidence-three')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r3','p','failed',?,1,'{}')", (str(d3),))

        d4 = self.store.root / 'runs' / 'r4'
        (d4 / 'workspace').mkdir(parents=True)
        (d4 / 'workspace' / 'evidence').write_text('evidence-four')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r4','p','failed',?,1,'{}')", (str(d4),))

        # r1 in recycle (uncommitted), sorted first: '0'*31 + '1'
        folder1 = self.store.root / 'recycle' / ('0' * 31 + '1')
        folder1.mkdir(parents=True)
        p1 = maintenance.preview_cleanup(self.store, 'r')
        receipt1 = {**p1, 'id': '0' * 31 + '1', 'expires': time.time() + 7 * 86400}
        (folder1 / 'receipt.json').write_text(json.dumps(receipt1), encoding='utf-8')
        os.rename(d1 / 'workspace', folder1 / 'workspace')

        # Conflict created at d1 / workspace
        (d1 / 'workspace').mkdir(parents=True)
        (d1 / 'workspace' / 'conflict_file').write_text('blocking')

        # r2 in recycle (uncommitted), sorted second: '0'*31 + '2'
        folder2 = self.store.root / 'recycle' / ('0' * 31 + '2')
        folder2.mkdir(parents=True)
        p2 = maintenance.preview_cleanup(self.store, 'r2')
        receipt2 = {**p2, 'id': '0' * 31 + '2', 'expires': time.time() + 7 * 86400}
        (folder2 / 'receipt.json').write_text(json.dumps(receipt2), encoding='utf-8')
        os.rename(d2 / 'workspace', folder2 / 'workspace')

        # r3 in recycle (committed), sorted third: '0'*31 + '3'
        folder3 = self.store.root / 'recycle' / ('0' * 31 + '3')
        folder3.mkdir(parents=True)
        p3 = maintenance.preview_cleanup(self.store, 'r3')
        receipt3 = {**p3, 'id': '0' * 31 + '3', 'expires': time.time() + 7 * 86400}
        (folder3 / 'receipt.json').write_text(json.dumps(receipt3), encoding='utf-8')
        os.rename(d3 / 'workspace', folder3 / 'workspace')
        self.store.execute("INSERT INTO settings VALUES (?,?)",
                           ('recycle:' + receipt3['id'], json.dumps(receipt3)))

        # 1. Recover cleanup isolates r1 conflict, allows r2 to recover, and doesn't crash
        maintenance.recover_cleanup(self.store)
        self.assertTrue((folder1 / 'receipt.json').exists())
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('cleanup-recovery-conflict:' + ('0' * 31 + '1'),)))
        self.assertEqual(self.store.one("SELECT count(*) AS c FROM events WHERE kind='cleanup_recovery_conflict'")['c'], 1)
        # r2 is successfully recovered back to d2
        self.assertEqual((d2 / 'workspace' / 'evidence').read_text(), 'evidence-two')
        self.assertFalse((folder2 / 'receipt.json').exists())

        # 2. Existing conflict doesn't block undo_cleanup of other receipts
        maintenance.undo_cleanup(self.store, receipt3['id'])
        self.assertEqual((d3 / 'workspace' / 'evidence').read_text(), 'evidence-three')
        self.assertFalse((folder3 / 'receipt.json').exists())

        # 3. Existing conflict doesn't block new recycle_cleanup
        p4 = maintenance.preview_cleanup(self.store, 'r4')
        new_receipt = maintenance.recycle_cleanup(self.store, p4)
        self.assertFalse((d4 / 'workspace').exists())
        self.assertTrue((self.store.root / 'recycle' / new_receipt['id'] / 'receipt.json').exists())

        # 4. Once conflict at d1 is cleared, subsequent recover_cleanup succeeds and clears conflict setting
        (d1 / 'workspace' / 'conflict_file').unlink()
        (d1 / 'workspace').rmdir()
        maintenance.recover_cleanup(self.store)
        self.assertEqual((d1 / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('cleanup-recovery-conflict:' + ('0' * 31 + '1'),)))

    def test_maintenance_continues_despite_cleanup_recovery_conflict(self):
        from adaptive_rsi.runtime import Runtime
        rt = Runtime(self.store, agent=object())
        d1 = self.directory
        folder1 = self.store.root / 'recycle' / ('0' * 31 + '1')
        folder1.mkdir(parents=True)
        p1 = maintenance.preview_cleanup(self.store, 'r')
        receipt1 = {**p1, 'id': '0' * 31 + '1', 'expires': time.time() + 7 * 86400}
        (folder1 / 'receipt.json').write_text(json.dumps(receipt1), encoding='utf-8')
        os.rename(d1 / 'workspace', folder1 / 'workspace')
        (d1 / 'workspace').mkdir(parents=True)
        (d1 / 'workspace' / 'conflict_file').write_text('conflict')

        # Run maintenance
        rt.maintenance()

        # Maintenance completed and advanced maintenance-last
        self.assertGreater(self.store.setting('maintenance-last', 0), 0)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='cleanup_recovery_conflict'"))

        # Defense-in-depth: even if recover_cleanup raises an unexpected error, maintenance survives
        self.store.put_setting('maintenance-last', 0)
        with patch.object(maintenance, 'recover_cleanup', side_effect=RuntimeError('injected recovery failure')):
            rt.maintenance()
        self.assertGreater(self.store.setting('maintenance-last', 0), 0)

    def test_second_quarantine_of_same_receipt_keeps_both_original_journals(self):
        identifier = '9' * 32
        self.store.execute("INSERT INTO settings VALUES (?,?)",
                           ('recycle:' + identifier,
                            '{"id":"' + identifier + '","run":"r","objects":[]}'))
        corrupt = self.store.root / 'recycle' / identifier
        corrupt.mkdir(parents=True)
        (corrupt / 'receipt.json').write_text('{"id":"' + identifier + '","run":"../escape","objects":[]}',
                                              encoding='utf-8')
        maintenance.recover_cleanup(self.store)
        tombstone = self.store.root / 'recycle' / ('receipt.invalid-' + identifier)
        first = (tombstone / 'receipt.json').read_text(encoding='utf-8')
        corrupt.mkdir(parents=True, exist_ok=True)
        (corrupt / 'receipt.json').write_text('{"id":"' + identifier + '","run":"other","objects":[]}',
                                              encoding='utf-8')
        maintenance.recover_cleanup(self.store)
        self.assertEqual((tombstone / 'receipt.json').read_text(encoding='utf-8'), first)
        self.assertEqual((tombstone / 'receipt.1.json').read_text(encoding='utf-8'),
                         '{"id":"' + identifier + '","run":"other","objects":[]}')

    def test_quarantine_evidence_is_replayed_after_lost_commit(self):
        corrupt = self.store.root / 'recycle' / ('0' * 32)
        corrupt.mkdir(parents=True)
        (corrupt / 'receipt.json').write_text('{broken', encoding='utf-8')
        maintenance.recover_cleanup(self.store)
        self.store.execute("DELETE FROM settings WHERE key=?", ('cleanup-receipt-quarantine:' + '0' * 32,))
        self.store.execute("DELETE FROM events WHERE kind='cleanup_receipt_quarantined'")
        maintenance.recover_cleanup(self.store)
        evidence = json.loads(self.store.one(
            'SELECT value FROM settings WHERE key=?',
            ('cleanup-receipt-quarantine:' + '0' * 32,))['value'])
        self.assertEqual(evidence['error_type'], 'ValueError')
        self.assertIn('lost before commit', evidence['reason'])
        self.assertEqual((self.store.root / 'recycle' / ('receipt.invalid-' + '0' * 32) / 'receipt.json')
                         .read_text(encoding='utf-8'), '{broken')

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

    def _merged_run(self, run_id, result, polls=('1111-aaaa', '2222-bbbb')):
        """Insert a merged run whose feedback directory holds two poll snapshots."""
        directory = self.store.root / 'runs' / run_id
        for name in polls:
            (directory / 'feedback' / name / 'baseline').mkdir(parents=True)
            (directory / 'feedback' / name / 'baseline' / 'head').write_text('sha-' + name)
        (directory / 'workspace').mkdir()
        (directory / 'workspace' / 'file').write_text('merged evidence')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES (?,?,?,?,1,?)",
                           (run_id, 'p', 'merged', str(directory), json.dumps(result)))
        return directory

    def test_merged_monitoring_completed_feedback_recycles_and_undoes(self):
        result = {"merged_sha": "a" * 40, "monitoring_completed": True,
                  "feedback": {"state": "verified", "head": "a" * 40, "checked_at": 1,
                               "merge": {"sha": "a" * 40, "state": "verified", "checked_at": 1}}}
        directory = self._merged_run('m1', result)
        preview = maintenance.preview_cleanup(self.store, 'm1')
        self.assertEqual([item['name'] for item in preview['objects']], ['workspace', 'feedback'])
        receipt = maintenance.recycle_cleanup(self.store, preview)
        self.assertFalse((directory / 'feedback').exists())
        self.assertFalse((directory / 'workspace').exists())
        maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((directory / 'feedback' / '1111-aaaa' / 'baseline' / 'head').read_text(),
                         'sha-1111-aaaa')
        self.assertEqual((directory / 'feedback' / '2222-bbbb' / 'baseline' / 'head').read_text(),
                         'sha-2222-bbbb')
        self.assertFalse((self.store.root / 'recycle' / receipt['id']).exists())

    def test_merged_run_without_completed_monitoring_stays_protected(self):
        result = {"merged_sha": "a" * 40,
                  "feedback": {"state": "verified", "head": "a" * 40,
                               "merge": {"sha": "a" * 40, "state": "verified"}}}
        directory = self._merged_run('m2', result)
        with self.assertRaises(ValueError):
            maintenance.preview_cleanup(self.store, 'm2')
        self.assertTrue((directory / 'feedback' / '1111-aaaa' / 'baseline' / 'head').exists())

    def test_merged_run_with_corrupt_result_stays_protected(self):
        directory = self.store.root / 'runs' / 'm3'
        (directory / 'feedback' / 'poll').mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('m3','p','merged',?,1,'{')",
                           (str(directory),))
        with self.assertRaises(ValueError):
            maintenance.preview_cleanup(self.store, 'm3')
        self.assertTrue((directory / 'feedback' / 'poll').exists())

    def test_merged_run_with_legal_hold_stays_protected(self):
        self._merged_run('m4', {"monitoring_completed": True})
        self.store.put_setting('legal-hold:m4', True)
        with self.assertRaises(ValueError):
            maintenance.preview_cleanup(self.store, 'm4')
        self.store.put_setting('legal-hold:m4', False)
        preview = maintenance.preview_cleanup(self.store, 'm4')
        self.assertEqual([item['name'] for item in preview['objects']], ['workspace', 'feedback'])

    def test_merged_expiration_shortcut_result_is_previewable(self):
        directory = self.store.root / 'runs' / 'm5'
        (directory / 'feedback' / 'poll').mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('m5','p','merged',?,1,?)",
                           (str(directory), json.dumps({"monitoring_completed": True})))
        preview = maintenance.preview_cleanup(self.store, 'm5')
        self.assertEqual([item['name'] for item in preview['objects']], ['feedback'])

    def test_new_feedback_poll_between_preview_and_recycle_blocks_recycle(self):
        preview = maintenance.preview_cleanup(self.store, 'r')
        (self.directory / 'feedback' / '9999-late').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'preview changed'):
            maintenance.recycle_cleanup(self.store, preview)
        self.assertTrue((self.directory / 'feedback' / '9999-late').exists())
