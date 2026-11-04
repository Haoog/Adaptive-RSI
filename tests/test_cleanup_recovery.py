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

    def test_recover_cleanup_bounded_wait_when_cleanup_lock_is_held(self):
        # 持锁方卡死（NFS/坏盘）时 recover_cleanup 必须限时返回错误而非永久等待；
        # 锁释放后下一轮恢复照常进行（幂等重试语义）。
        import fcntl
        (self.store.root / 'recycle').mkdir()
        holder = os.open(self.store.root / 'recycle' / '.cleanup.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(maintenance, 'CLEANUP_LOCK_RETRY_SECONDS', 0.01):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    maintenance.recover_cleanup(self.store)
                self.assertLess(time.monotonic() - start, 5)
            fcntl.flock(holder, fcntl.LOCK_UN)
            maintenance.recover_cleanup(self.store)
        finally:
            os.close(holder)

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

    def _make_valid_recycle(self, identifier, committed=True, expires=None):
        """Rebuild one recycle: durable journal, moved artifact, optionally the committed row."""
        import os
        import time
        preview = maintenance.preview_cleanup(self.store, 'r')
        folder = self.store.root / 'recycle' / identifier
        folder.mkdir(parents=True, mode=0o700)
        receipt = {**preview, 'id': identifier,
                   'expires': expires if expires is not None else time.time() + 7 * 86400}
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

    def test_terminal_task_pr_feedback_and_handled_keys_are_retired(self):
        # pr-feedback:/handled: 键按任务前缀累积；任务终态超过保留期后由维护清理，
        # 而仍处于活跃窗口或 pr_open 的任务前缀必须完整保留（supersede 判读依赖）。
        from adaptive_rsi.delivery import _pr_feedback_prefix
        stale = time.time() - 91 * 86400
        for task_id, state, updated, expected in (
                ('t-done', 'merged', stale, False),      # 终态且超期：清理
                ('t-open', 'pr_open', stale, True),      # 未终态：保留
                ('t-fresh', 'merged', time.time(), True)):  # 终态但在保留期内：保留
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p',?,0,?)", (task_id, state, updated))
            prefix = _pr_feedback_prefix('p', task_id)
            self.store.execute("INSERT INTO settings VALUES (?,?)", (prefix + 'obs1', '{}'))
            self.store.execute("INSERT INTO settings VALUES (?,?)", ('handled:' + prefix + 'obs1', '1'))
        # 不属于本任务命名空间的对照键永不受任务清理影响。
        self.store.execute("INSERT INTO settings VALUES ('handled:unrelated','1')")
        self.store.execute("INSERT INTO settings VALUES ('pr-feedback:other:obs','{}')")

        from adaptive_rsi.runtime import Runtime
        rt = Runtime(self.store, agent=object())
        rt.maintenance()

        for task_id, expected in (('t-done', False), ('t-open', True), ('t-fresh', True)):
            prefix = _pr_feedback_prefix('p', task_id)
            self.assertEqual(bool(self.store.one(
                "SELECT 1 FROM settings WHERE key=?", (prefix + 'obs1',))), expected, task_id)
            self.assertEqual(bool(self.store.one(
                "SELECT 1 FROM settings WHERE key=?", ('handled:' + prefix + 'obs1',))), expected,
                'handled:' + task_id)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM settings WHERE key='handled:unrelated'"))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM settings WHERE key='pr-feedback:other:obs'"))

    def test_pr_feedback_retirement_cursor_covers_backlog_beyond_batch(self):
        # 超过 200 个终态任务带键时游标逐批推进；无键任务不占配额。201 个带键
        # 任务两轮内应全部清理（旧实现 LIMIT 200 会永远留下最后一个）。
        from adaptive_rsi.delivery import _pr_feedback_prefix
        stale = time.time() - 91 * 86400
        total = 201
        for index in range(total):
            task_id = 't-backlog-%03d' % index
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p','merged',0,?)", (task_id, stale))
            prefix = _pr_feedback_prefix('p', task_id)
            self.store.execute("INSERT INTO settings VALUES (?,?)", (prefix + 'obs', '{}'))
        # 200 个无键任务穿插其中：它们不占每轮 200 的清理配额。
        for index in range(200):
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p','merged',0,?)", ('t-keyless-%03d' % index, stale))

        from adaptive_rsi.runtime import Runtime
        rt = Runtime(self.store, agent=object())

        def remaining():
            return self.store.one("SELECT COUNT(*) AS n FROM settings WHERE key LIKE 'pr-feedback:%'")["n"]

        rt.maintenance()
        self.assertGreater(remaining(), 0)          # 一轮最多 200 个带键任务
        self.store.put_setting('maintenance-last', 0)
        rt.maintenance()
        self.assertEqual(remaining(), 0)            # 游标推进后剩余任务全部被覆盖

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

    def test_deep_nested_dependency_tree_is_removed_without_recursion(self):
        # 深层嵌套（node_modules 式依赖树）超过默认递归上限；此前递归删除
        # 触发 RecursionError 且运行永久卡在保留清理里。目录构造与后续删除
        # 一样走 dir_fd 相对路径，避免绝对路径超出 macOS PATH_MAX。
        depth = 1200
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_text('must remain')
        (self.directory / 'dependencies').mkdir()
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self.directory / 'dependencies', flags)
        try:
            for level in range(depth):
                os.mkdir('d', dir_fd=descriptor)
                deeper = os.open('d', flags, dir_fd=descriptor)
                if level:
                    # 除根目录外每层封成只读，覆盖依赖副本冻结读权限的删除路径；
                    # 下一层目录已经建好才封父目录，构造本身不需要上行。
                    os.chmod('..', 0o555, dir_fd=deeper)
                os.close(descriptor)
                descriptor = deeper
            fd = os.open('evidence.txt', os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=descriptor)
            os.close(fd)
            os.symlink(str(outside), 'link', dir_fd=descriptor)
        finally:
            os.close(descriptor)

        self.assertTrue(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))

        self.assertFalse((self.directory / 'dependencies').exists())
        self.assertFalse((self.directory / 'workspace').exists())
        row = self.store.one('SELECT path FROM runs WHERE id=?', ('r',))
        self.assertIsNotNone(row)
        self.assertIsNone(row['path'])
        # 深层只读树中的符号链接只被解除，不得沿链接修改或删除外部目标。
        self.assertEqual(sentinel.read_text(), 'must remain')

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

    def test_expired_recycle_purges_artifacts_and_row_but_keeps_journal(self):
        identifier = 'a' * 32
        receipt = self._make_valid_recycle(identifier, expires=time.time() - 1)
        folder = self.store.root / 'recycle' / identifier
        maintenance.recover_cleanup(self.store)
        self.assertFalse((folder / 'workspace').exists())
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('recycle:' + identifier,)))
        # receipt.json stays behind as the audit trail distinguishing "purged"
        # from "never recycled".
        self.assertTrue((folder / 'receipt.json').exists())
        event = self.store.one("SELECT message,data FROM events WHERE kind='recycle_purged'")
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event['data'])['receipt_id'], identifier)
        # Purging is idempotent: a second pass removes nothing else and emits
        # no duplicate evidence.
        maintenance.recover_cleanup(self.store)
        self.assertEqual(self.store.one("SELECT count(*) AS c FROM events "
                                        "WHERE kind='recycle_purged'")['c'], 1)
        self.assertTrue((folder / 'receipt.json').exists())
        # Undo still refuses the expired identifier even though the receipt
        # journal is retained on disk.
        with self.assertRaisesRegex(ValueError, 'missing or expired'):
            maintenance.undo_cleanup(self.store, receipt['id'])

    def test_unexpired_recycle_untouched_by_purge_and_undoable(self):
        identifier = 'b' * 32
        receipt = self._make_valid_recycle(identifier)
        folder = self.store.root / 'recycle' / identifier
        maintenance.recover_cleanup(self.store)
        self.assertEqual((folder / 'workspace' / 'evidence').read_text(), 'original')
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('recycle:' + identifier,)))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE kind='recycle_purged'"))
        maintenance.undo_cleanup(self.store, receipt['id'])
        self.assertEqual((self.directory / 'workspace' / 'evidence').read_text(), 'original')

    def test_expired_uncommitted_recycle_removes_artifacts_silently(self):
        # Crash remnant: journal and artifacts committed to disk, settings row
        # never committed.  The declared TTL still applies, so the pass drops
        # the artifacts instead of resurrecting them into the run directory.
        identifier = 'c' * 32
        self._make_valid_recycle(identifier, committed=False, expires=time.time() - 1)
        folder = self.store.root / 'recycle' / identifier
        maintenance.recover_cleanup(self.store)
        self.assertFalse((folder / 'workspace').exists())
        self.assertTrue((folder / 'receipt.json').exists())
        self.assertFalse((self.directory / 'workspace').exists())
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE kind='recycle_purged'"))

    def test_expired_recycle_artifact_mismatch_blocks_purge(self):
        import shutil
        identifier = 'd' * 32
        self._make_valid_recycle(identifier, expires=time.time() - 1)
        folder = self.store.root / 'recycle' / identifier
        # Someone swapped the recorded artifact for a different directory:
        # the inode no longer matches the receipt, so nothing may be deleted.
        shutil.rmtree(folder / 'workspace')
        (folder / 'workspace').mkdir()
        (folder / 'workspace' / 'foreign').write_text('not ours')
        maintenance.recover_cleanup(self.store)
        self.assertEqual((folder / 'workspace' / 'foreign').read_text(), 'not ours')
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('recycle:' + identifier,)))
        evidence = self.store.one('SELECT value FROM settings WHERE key=?',
                                  ('recycle-purge-blocked:' + identifier,))
        self.assertIsNotNone(evidence)
        self.assertEqual(json.loads(evidence['value'])['blocked_on'], 'workspace')
        maintenance.recover_cleanup(self.store)
        self.assertEqual(self.store.one("SELECT count(*) AS c FROM events "
                                        "WHERE kind='recycle_purge_blocked'")['c'], 1)
        # Once the foreign directory is removed by hand, the next pass purges.
        shutil.rmtree(folder / 'workspace')
        maintenance.recover_cleanup(self.store)
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('recycle:' + identifier,)))
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('recycle-purge-blocked:' + identifier,)))
        self.assertTrue((folder / 'receipt.json').exists())
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='recycle_purged'"))

    def test_expired_orphan_receipt_row_removed_when_folder_absent(self):
        # Rows whose folder was already deleted (or never created) carry no
        # artifacts; they are metadata-only residue and are swept directly.
        gone, live = 'e' * 32, 'f' * 32
        self.store.execute("INSERT INTO settings VALUES (?,?)",
                           ('recycle:' + gone, json.dumps({'id': gone, 'run': 'r', 'objects': [],
                                                           'expires': time.time() - 1})))
        self.store.execute("INSERT INTO settings VALUES (?,?)",
                           ('recycle:' + live, json.dumps({'id': live, 'run': 'r', 'objects': [],
                                                           'expires': time.time() + 7 * 86400})))
        maintenance.recover_cleanup(self.store)
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('recycle:' + gone,)))
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('recycle:' + live,)))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='recycle_purged'"))
