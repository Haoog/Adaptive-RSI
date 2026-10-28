"""Offline storage acceptance: no model, credential or remote operations."""
import json
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_rsi.store import Store
from adaptive_rsi import storage_tools


class StorageAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.addCleanup(self.store.close)
        self.store.execute("INSERT INTO projects VALUES ('p','private project','/private/source','{}',0,0)")

    def test_incompatible_migration_backup_never_evicts_recovery_point(self):
        import os
        import sqlite3
        good = storage_tools.backup(self.store)
        good_path = self.store.root / 'backups' / (good['id'] + '.sqlite3')
        os.utime(good_path, (1, 1))
        for corruption in ('future', 'checksum', 'gap'):
            with self.subTest(corruption=corruption):
                bad = storage_tools.backup(self.store)
                path = good_path.parent / (bad['id'] + '.sqlite3')
                with sqlite3.connect(path) as db:
                    if corruption == 'future':
                        db.execute("INSERT INTO schema_migrations VALUES (999,'future',0)")
                    elif corruption == 'checksum':
                        db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=3")
                    else:
                        db.execute("DELETE FROM schema_migrations WHERE version=2")
                with self.assertRaisesRegex(ValueError, 'migration'):
                    storage_tools.verify_backup(path)
                report = storage_tools.retain_backups(self.store, max_count=1)
                self.assertEqual(report['protected'], good_path.name)
                self.assertIn(path.name, report['invalid'])
                self.assertTrue(good_path.exists())
                destination = self.root / ('rejected-' + corruption)
                with self.assertRaisesRegex(ValueError, 'migration'):
                    storage_tools.restore(path, destination)
                self.assertFalse(destination.exists())
                self.assertFalse(storage_tools.rehearse_restore(path)['valid'])
                path.unlink()

    def test_restore_preserves_malformed_historical_results(self):
        for index, raw in enumerate(('[]', 'null', '42', '"scalar"', '{broken')):
            self.store.execute("INSERT INTO runs(id,project,state,result,ended) VALUES (?,'p','merged',?,1)",
                               ('bad' + str(index), raw))
        healthy = {'pr': 'fixture', 'review': {'approved': True}, 'feedback': {'state': 'old'}}
        self.store.execute("INSERT INTO runs(id,project,state,result,ended) VALUES ('healthy','p','pr_open',?,1)",
                           (json.dumps(healthy),))
        snapshot = storage_tools.backup(self.store)
        source = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        destination = self.root / 'restored'
        storage_tools.restore(source, destination)
        restored = Store(destination)
        self.addCleanup(restored.close)
        for index, raw in enumerate(('[]', 'null', '42', '"scalar"', '{broken')):
            result = json.loads(restored.one('SELECT result FROM runs WHERE id=?', ('bad' + str(index),))['result'])
            self.assertEqual(result['restore_invalidation']['original_result'], raw)
            self.assertEqual(result['feedback']['state'], 'insufficient_evidence')
        result = json.loads(restored.one("SELECT result FROM runs WHERE id='healthy'")['result'])
        self.assertEqual(result['review'], healthy['review'])
        self.assertEqual(result['feedback']['previous'], healthy['feedback'])
        self.assertTrue(storage_tools.rehearse_restore(source)['valid'])

    def test_corrupt_setting_isolated_and_typed_namespace_expires(self):
        self.store.execute("INSERT INTO settings VALUES ('broken','not-json')")
        self.assertEqual(self.store.setting('broken', {'fallback': True}), {'fallback': True})
        self.store.put_setting('healthy', 42)
        self.assertEqual(self.store.setting('healthy'), 42)
        with patch('adaptive_rsi.store.time.time', return_value=100):
            self.store.put_namespaced_setting('cache', 'one', {'ok': True}, version=2, ttl=10)
        self.assertEqual(self.store.namespaced_setting('cache', 'one', dict, version=2, now=109), {'ok': True})
        self.assertIsNone(self.store.namespaced_setting('cache', 'one', dict, version=2, now=110))
        self.assertEqual(self.store.namespaced_setting('cache', 'one', list, version=2, default=[] , now=109), [])
        self.assertIsNone(self.store.namespaced_setting('cache', 'one', dict, version=1, now=109))

    def test_restore_replaces_malformed_case_without_old_path(self):
        self.store.execute("INSERT INTO cases(id,project,spec) VALUES ('case','p',?)", (json.dumps('/private/source'),))
        snapshot = storage_tools.backup(self.store)
        destination = self.root / 'restored'
        storage_tools.restore(self.store.root / 'backups' / (snapshot['id'] + '.sqlite3'), destination)
        restored = Store(destination)
        self.addCleanup(restored.close)
        spec = json.loads(restored.one("SELECT spec FROM cases WHERE id='case'")['spec'])
        self.assertIsInstance(spec, dict)
        self.assertFalse(spec['available'])
        self.assertTrue(Path(spec['repository']).is_relative_to(destination.resolve()))
        self.assertNotIn('/private/source', json.dumps(spec))

    def test_business_rehearsal_is_isolated_and_reports_relationships(self):
        task = self.store.add_task('p', 'manual', 'one', 'private task')
        snapshot = storage_tools.backup(self.store)
        source = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        before = storage_tools.file_digest(source)
        report = storage_tools.rehearse_restore(source)
        self.assertTrue(report['valid'])
        self.assertEqual(report['tasks'], 1)
        self.assertEqual(report['projects'], 1)
        self.assertTrue(report['all_projects_paused'])
        self.assertEqual(report['remote_operations'], 0)
        self.assertEqual(storage_tools.file_digest(source), before)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')
        self.assertEqual(self.store.one("SELECT paused FROM projects WHERE id='p'")['paused'], 0)

    def test_diagnostic_preview_export_is_explicit_and_contains_no_user_text(self):
        secret = 'private-key-and-source-text'
        self.store.execute("INSERT INTO runs(id,project,state,result) VALUES ('r','p',?,?)",
                           (secret, json.dumps({'failure': {'code': secret}})))
        self.store.put_setting('secret', secret)
        preview = storage_tools.diagnostic_preview(self.store)
        self.assertNotIn(secret, json.dumps(preview))
        self.assertNotIn('/private/source', json.dumps(preview))
        destination = self.root / 'diagnostics.json'
        with self.assertRaises(ValueError):
            storage_tools.export_diagnostics(preview, destination, consent=False)
        self.assertFalse(destination.exists())
        storage_tools.export_diagnostics(preview, destination, consent=True)
        self.assertEqual(json.loads(destination.read_text()), preview)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            storage_tools.export_diagnostics(preview, destination, consent=True)

    def test_expired_auth_slow_close_does_not_block_other_session_reads(self):
        from adaptive_rsi.auth_sessions import AuthSessions
        sessions = AuthSessions(self.root / 'auth', factory=lambda backend: None)
        self.addCleanup(sessions.close)
        entered, release = threading.Event(), threading.Event()
        class Worker:
            def close(self):
                entered.set()
                release.wait(3)
        sessions.sessions = {
            'expired': {'id': 'expired', 'backend': 'codex', 'state': 'starting', 'expires_at': 0},
            'other': {'id': 'other', 'backend': 'opencode', 'state': 'starting', 'expires_at': time.time() + 60},
        }
        sessions.workers['expired'] = (Worker(), threading.Event())
        thread = threading.Thread(target=lambda: sessions.status('expired'))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            done = threading.Event()
            reader = threading.Thread(target=lambda: (sessions.cancel('other'), done.set()))
            reader.start()
            self.assertTrue(done.wait(1), 'slow worker.close held session lock')
            reader.join(2)
            self.assertEqual(sessions.status('other')['state'], 'cancelled')
        finally:
            release.set()
            thread.join(3)

    def test_backup_low_space_and_deadline_preserve_previous_recovery_point(self):
        snapshot = storage_tools.backup(self.store)
        old = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        digest = storage_tools.file_digest(old)
        with patch('adaptive_rsi.storage_tools.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(ValueError, 'space'):
                storage_tools.backup(self.store)
        with self.assertRaises(TimeoutError):
            storage_tools.backup(self.store, deadline=0)
        self.assertEqual(storage_tools.file_digest(old), digest)
        self.assertTrue(storage_tools.verify_backup(old)['valid'])
        self.assertEqual(list(old.parent.glob('.backup-*')), [])

    def test_backup_rejects_nonfinite_and_boolean_resource_budgets(self):
        for value in (float('nan'), float('inf'), True, '10'):
            with self.subTest(timeout=repr(value)), self.assertRaises(ValueError):
                storage_tools.backup(self.store, timeout=value)
        for value in (float('nan'), True, '10', -1):
            with self.subTest(reserve=repr(value)), self.assertRaises(ValueError):
                storage_tools.backup(self.store, reserve_bytes=value)

    def test_cleanup_preview_recycle_undo_and_holds(self):
        from adaptive_rsi.maintenance import preview_cleanup, recycle_cleanup, undo_cleanup
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'file').write_text('retained evidence')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))
        preview = preview_cleanup(self.store, 'r')
        self.assertEqual([item['name'] for item in preview['objects']], ['workspace'])
        self.store.put_setting('legal-hold:r', True)
        with self.assertRaises(ValueError):
            recycle_cleanup(self.store, preview)
        self.assertTrue((directory / 'workspace').exists())
        self.store.put_setting('legal-hold:r', False)
        receipt = recycle_cleanup(self.store, preview)
        self.assertFalse((directory / 'workspace').exists())
        undo_cleanup(self.store, receipt['id'])
        self.assertEqual((directory / 'workspace' / 'file').read_text(), 'retained evidence')
        self.store.execute("UPDATE runs SET ended=NULL,state='executing' WHERE id='r'")
        with self.assertRaises(ValueError):
            preview_cleanup(self.store, 'r')

    def test_cleanup_rejects_stale_preview_and_symlink(self):
        from adaptive_rsi.maintenance import preview_cleanup, recycle_cleanup
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))
        preview = preview_cleanup(self.store, 'r')
        (directory / 'workspace').rmdir()
        (directory / 'workspace').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            recycle_cleanup(self.store, preview)


    def test_business_rehearsal_reports_orphan_without_changing_snapshot(self):
        import sqlite3
        db = sqlite3.connect(self.store.path)
        try:
            with db:
                db.execute("INSERT INTO stages(run,stage,data) VALUES ('missing','test','{}')")
        finally:
            db.close()
        snapshot = storage_tools.backup(self.store)
        source = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        digest = storage_tools.file_digest(source)
        report = storage_tools.rehearse_restore(source)
        self.assertFalse(report['valid'])
        self.assertEqual(report['failure_stage'], 'snapshot_validation')
        self.assertEqual(storage_tools.file_digest(source), digest)

    def test_cleanup_undo_preserves_new_artifacts(self):
        from adaptive_rsi.maintenance import preview_cleanup, recycle_cleanup, undo_cleanup
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'old').write_text('old')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))
        receipt = recycle_cleanup(self.store, preview_cleanup(self.store, 'r'))
        (directory / 'workspace').mkdir()
        (directory / 'workspace' / 'new').write_text('new')
        with self.assertRaises(ValueError):
            undo_cleanup(self.store, receipt['id'])
        self.assertEqual((directory / 'workspace' / 'new').read_text(), 'new')
        self.assertEqual((self.store.root / 'recycle' / receipt['id'] / 'workspace' / 'old').read_text(), 'old')

    def test_namespace_rejects_values_that_cannot_roundtrip_type(self):
        with self.assertRaises(ValueError):
            self.store.put_namespaced_setting('cache', 'key', (1, 2))
        with self.assertRaises(ValueError):
            self.store.put_namespaced_setting('cache', 'key', float('nan'))

    def test_cleanup_corrupt_hold_fails_closed(self):
        from adaptive_rsi.maintenance import preview_cleanup
        directory = self.store.root / 'runs' / 'r'
        directory.mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))
        self.store.execute("INSERT INTO settings VALUES ('legal-hold:r','bad-json')")
        with self.assertRaises(ValueError):
            preview_cleanup(self.store, 'r')

    def test_backup_under_concurrent_writes_has_explicit_deadline(self):
        import sqlite3
        stop, started = threading.Event(), threading.Event()
        errors = []
        def write():
            db = sqlite3.connect(self.store.path, timeout=2)
            try:
                while not stop.is_set():
                    with db:
                        db.execute("INSERT OR REPLACE INTO settings VALUES ('writer',?)", (json.dumps(time.monotonic()),))
                    started.set()
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        writer = threading.Thread(target=write)
        writer.start()
        try:
            self.assertTrue(started.wait(2))
            snapshot = storage_tools.backup(self.store, timeout=2)
            self.assertTrue(storage_tools.verify_backup(self.store.root / 'backups' / (snapshot['id'] + '.sqlite3'))['valid'])
        finally:
            stop.set()
            writer.join(3)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()