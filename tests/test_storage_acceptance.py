"""Offline storage acceptance: no model, credential or remote operations."""
from contextlib import closing
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
                with closing(sqlite3.connect(path)) as db, db:
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

        # 存量旧格式惰性升级：无前缀既有键
        self.store.execute("INSERT INTO settings VALUES ('cache:legacy','{\"migrated\": true}')")
        self.assertEqual(self.store.namespaced_setting('cache', 'legacy', dict), {'migrated': True})
        migrated_row = self.store.one("SELECT value FROM settings WHERE key='namespace:cache:legacy'")
        assert migrated_row is not None
        migrated_envelope = json.loads(migrated_row['value'])
        self.assertEqual(migrated_envelope['version'], 1)
        self.assertEqual(migrated_envelope['type'], 'dict')
        self.assertEqual(migrated_envelope['value'], {'migrated': True})
        self.assertIsNone(migrated_envelope['expires'])
        self.assertIsNone(self.store.one("SELECT 1 FROM settings WHERE key='cache:legacy'"))

        # 存量旧格式惰性升级：已带 namespace: 前缀但未包信封的原始 JSON 值
        self.store.execute("INSERT INTO settings VALUES ('namespace:cache:raw_num','88')")
        self.assertIsNone(self.store.namespaced_setting('cache', 'raw_num', dict))
        # 类型不匹配时不破坏底层原始值
        raw_row = self.store.one("SELECT value FROM settings WHERE key='namespace:cache:raw_num'")
        assert raw_row is not None
        self.assertEqual(raw_row['value'], '88')
        self.assertEqual(self.store.namespaced_setting('cache', 'raw_num', int), 88)
        raw_row2 = self.store.one("SELECT value FROM settings WHERE key='namespace:cache:raw_num'")
        assert raw_row2 is not None
        raw_env = json.loads(raw_row2['value'])
        self.assertEqual(raw_env, {'version': 1, 'type': 'int', 'value': 88, 'expires': None})

        # 写路径统一新格式并清理旧格式残留
        self.store.execute("INSERT INTO settings VALUES ('cache:write_target','\"old_wire\"')")
        self.store.put_namespaced_setting('cache', 'write_target', {'rewritten': True}, version=3, ttl=30)
        self.assertIsNone(self.store.one("SELECT 1 FROM settings WHERE key='cache:write_target'"))
        target_row = self.store.one("SELECT value FROM settings WHERE key='namespace:cache:write_target'")
        assert target_row is not None
        rewritten_env = json.loads(target_row['value'])
        self.assertEqual(rewritten_env['version'], 3)
        self.assertEqual(rewritten_env['type'], 'dict')
        self.assertEqual(rewritten_env['value'], {'rewritten': True})

        # 损坏单一 namespace 时只隔离自身，不影响其他功能
        self.store.execute("INSERT INTO settings VALUES ('namespace:damaged:corrupt','{broken-json')")
        self.store.execute("INSERT INTO settings VALUES ('damaged:legacy_corrupt','not-json')")
        self.store.execute("INSERT INTO settings VALUES ('namespace:damaged:bad_envelope','{\"version\":\"NaN\",\"type\":\"str\",\"value\":\"x\"}')")
        self.assertEqual(self.store.namespaced_setting('damaged', 'corrupt', dict, default={'fallback': True}), {'fallback': True})
        self.assertIsNone(self.store.namespaced_setting('damaged', 'legacy_corrupt', str))
        self.assertIsNone(self.store.namespaced_setting('damaged', 'bad_envelope', str))
        # 其它 namespace 与全局设置仍正常可用
        self.store.put_namespaced_setting('isolated', 'healthy', 'good')
        self.assertEqual(self.store.namespaced_setting('isolated', 'healthy', str), 'good')
        self.assertEqual(self.store.setting('healthy'), 42)

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

    def test_cleanup_preview_applies_project_hold_and_corrupt_value(self):
        from adaptive_rsi.maintenance import preview_cleanup
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))
        self.store.put_setting('legal-hold-project:p', True)
        with self.assertRaises(ValueError):
            preview_cleanup(self.store, 'r')
        self.store.put_setting('legal-hold-project:p', False)
        self.assertEqual([item['name'] for item in preview_cleanup(self.store, 'r')['objects']], ['workspace'])
        self.store.execute("INSERT OR REPLACE INTO settings VALUES ('legal-hold-project:p','{')")
        with self.assertRaises(ValueError):
            preview_cleanup(self.store, 'r')

    def test_cleanup_recycle_undo_linux_renameat2_and_fallback(self):
        import sys
        import ctypes
        from adaptive_rsi.maintenance import preview_cleanup, recycle_cleanup, undo_cleanup, _rename_cleanup
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'file').write_text('linux evidence')
        self.store.execute("INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')", (str(directory),))

        # 1. 真实执行路径：在本机（Linux）跑通回收/撤销闭环真实执行
        preview = preview_cleanup(self.store, 'r')
        receipt = recycle_cleanup(self.store, preview)
        self.assertFalse((directory / 'workspace').exists())
        self.assertTrue((self.store.root / 'recycle' / receipt['id'] / 'workspace' / 'file').exists())
        undo_cleanup(self.store, receipt['id'])
        self.assertEqual((directory / 'workspace' / 'file').read_text(), 'linux evidence')

        if sys.platform == 'linux':
            # 2. 如 libc 无 renameat2 则验证 fallback 分支（通过 syscall）并留证据，不得静默跳过
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
                        raise AttributeError("simulated libc without renameat2")
                    return getattr(self._lib, name)

            calls = []
            def fake_cdll(name, use_errno=False):
                lib = real_cdll(name, use_errno=use_errno)
                return LibcWithoutRenameat2(lib)

            (directory / 'workspace' / 'file').write_text('fallback evidence')
            preview_fallback = preview_cleanup(self.store, 'r')
            with patch('ctypes.CDLL', side_effect=fake_cdll):
                receipt_fallback = recycle_cleanup(self.store, preview_fallback)
                self.assertFalse((directory / 'workspace').exists())
                self.assertGreater(len(calls), 0, "libc 无 renameat2 时必须触发 syscall fallback")
                undo_cleanup(self.store, receipt_fallback['id'])
                self.assertEqual((directory / 'workspace' / 'file').read_text(), 'fallback evidence')

            # 3. 验证 fallback 分支在目标已存在时拒绝覆盖
            target = directory / 'workspace'
            candidate = directory / 'candidate'
            candidate.mkdir(parents=True)
            (candidate / 'file').write_text('collision')
            with patch('ctypes.CDLL', side_effect=fake_cdll):
                with self.assertRaises(FileExistsError):
                    _rename_cleanup(candidate, target)
            (candidate / 'file').unlink()
            candidate.rmdir()

            # 4. 验证当 renameat2 与 syscall 均不可用时抛出明确异常，绝不静默跳过
            class LibcUnavailable:
                def __getattr__(self, name):
                    raise AttributeError(name)
            with patch('ctypes.CDLL', return_value=LibcUnavailable()):
                with self.assertRaisesRegex(OSError, 'Atomic no-replace cleanup rename unavailable'):
                    _rename_cleanup(directory / 'workspace', directory / 'other')

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

    def test_backup_validation_rejects_cross_project_run_ownership(self):
        task = self.store.add_task('p', 'manual', 'owned-task', 'owned task')
        self.store.execute("INSERT INTO projects VALUES ('q','other project','/other/source','{}',0,0)")
        self.store.execute("INSERT INTO runs(id,task,project,state,result) VALUES ('cross',?, 'q','merged','{}')",
                           (task,))
        snapshot = storage_tools.backup(self.store)
        source = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        with self.assertRaisesRegex(ValueError, 'ownership'):
            storage_tools.verify_backup(source)
        destination = self.root / 'cross-project-restore'
        with self.assertRaisesRegex(ValueError, 'ownership'):
            storage_tools.restore(source, destination)
        self.assertFalse(destination.exists())
        report = storage_tools.rehearse_restore(source)
        self.assertFalse(report['valid'])
        self.assertEqual(report['failure_stage'], 'snapshot_validation')

    def test_backup_validation_rejects_run_without_project(self):
        self.store.execute("INSERT INTO runs(id,project,state,result) VALUES ('ghost','missing','merged','{}')")
        snapshot = storage_tools.backup(self.store)
        source = self.store.root / 'backups' / (snapshot['id'] + '.sqlite3')
        with self.assertRaisesRegex(ValueError, 'ownership'):
            storage_tools.verify_backup(source)
        destination = self.root / 'ghost-restore'
        with self.assertRaisesRegex(ValueError, 'ownership'):
            storage_tools.restore(source, destination)
        self.assertFalse(destination.exists())

    def test_rotation_never_keeps_only_an_unrestorable_recovery_point(self):
        import os
        task = self.store.add_task('p', 'manual', 'rotation-task', 'owned task')
        good = storage_tools.backup(self.store)
        good_path = self.store.root / 'backups' / (good['id'] + '.sqlite3')
        os.utime(good_path, (1, 1))
        self.store.execute("INSERT INTO projects VALUES ('q','other project','/other/source','{}',0,0)")
        self.store.execute("INSERT INTO runs(id,task,project,state,result) VALUES ('cross-rotation',?, 'q','merged','{}')",
                           (task,))
        bad = storage_tools.backup(self.store)
        bad_path = self.store.root / 'backups' / (bad['id'] + '.sqlite3')
        os.utime(bad_path, (time.time() + 10, time.time() + 10))
        report = storage_tools.retain_backups(self.store, max_count=1, max_age_days=3650)
        self.assertEqual(report['protected'], good_path.name)
        self.assertIn(bad_path.name, report['invalid'])
        self.assertTrue(good_path.exists())
        self.assertTrue(storage_tools.verify_backup(good_path)['valid'])

    def test_overview_reuses_digest_by_regular_file_identity_and_invalidates_change(self):
        import os
        first = storage_tools.backup(self.store)
        storage_tools.backup(self.store)
        files = {path.stem: path for path in (self.store.root / 'backups').glob('*.sqlite3')}
        target = files[first['id']]

        def sha(snapshot, path):
            return next(item['sha256'] for item in snapshot['backup_manifest']
                        if item['path'].endswith(path.name))

        with patch('adaptive_rsi.storage_tools.file_digest', wraps=storage_tools.file_digest) as digest:
            initial = storage_tools.overview(self.store, refresh=True)
            self.assertEqual(digest.call_count, 2)
            setattr(self.store, "_storage_snapshot", None)
            cached = storage_tools.overview(self.store)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(cached['backup_manifest'], initial['backup_manifest'])
            unchanged = sha(initial, target)
            stat = target.stat()
            os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            setattr(self.store, "_storage_snapshot", None)
            touched = storage_tools.overview(self.store)
            self.assertEqual(digest.call_count, 3)
            self.assertEqual(sha(touched, target), unchanged)
            target.write_bytes(target.read_bytes() + b'changed-content')
            setattr(self.store, "_storage_snapshot", None)
            changed = storage_tools.overview(self.store)
            self.assertEqual(digest.call_count, 4)
            self.assertNotEqual(sha(changed, target), unchanged)

    def test_overview_digest_cache_drops_files_outside_the_current_manifest(self):
        first = storage_tools.backup(self.store)
        storage_tools.backup(self.store)
        storage_tools.overview(self.store, refresh=True)
        self.assertEqual(len(getattr(self.store, "_storage_digests")), 2)
        (self.store.root / 'backups' / (first['id'] + '.sqlite3')).unlink()
        setattr(self.store, "_storage_snapshot", None)
        storage_tools.overview(self.store)
        self.assertEqual(len(getattr(self.store, "_storage_digests")), 1)


if __name__ == '__main__':
    unittest.main()