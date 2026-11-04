"""Admission recovery must observe installer death, never just elapsed wall time."""
import errno
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.lifecycle import KEY, barrier, readiness
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class UpdateAdmissionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.runtime.updates.desktop_managed = True
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',1,?)", (temporary.name, time.time()))
        self.task = self.store.add_task('p', 'goal', 'reserved', 'Keep this task')
        self.store.execute('UPDATE tasks SET available=? WHERE id=?', (time.time() + 86400, self.task))
        self.before = self.store.one('SELECT * FROM tasks WHERE id=?', (self.task,))

    def commit(self, owner):
        prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
        self.runtime.updates.desktop_owner_pid = owner
        return readiness(self.runtime, 'commit', lease=prepared['lease'])['barrier']

    @unittest.skipUnless(os.name == 'posix', 'Process-zero liveness follows POSIX semantics')
    def test_real_installer_exit_reopens_admission_without_altering_tasks(self):
        owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        def cleanup():
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
        self.addCleanup(cleanup)
        state = self.commit(owner.pid)
        self.assertEqual(state['phase'], 'installing')
        self.assertIsNotNone(barrier(self.runtime))
        owner.terminate()
        owner.wait(timeout=5)
        self.assertIsNone(barrier(self.runtime))
        self.assertEqual(self.store.one('SELECT * FROM tasks WHERE id=?', (self.task,)), self.before)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='desktop_update_recovered'")['n'], 1)
        self.assertIsNone(barrier(self.runtime))

    def test_live_installer_remains_guarded_even_after_drain_expiry(self):
        state = self.commit(os.getpid())
        with patch('adaptive_rsi.lifecycle.time.time', return_value=state['expires'] + 86400):
            self.assertEqual(barrier(self.runtime)['phase'], 'installing')

    def test_unobservable_installer_does_not_reopen_admission(self):
        self.commit(os.getpid())
        for error in (PermissionError(errno.EPERM, 'denied'), OSError(errno.EIO, 'unknown')):
            with self.subTest(error=error), patch('adaptive_rsi.lifecycle.os.kill', side_effect=error):
                self.assertIsNotNone(barrier(self.runtime))

    def test_recovery_audit_failure_keeps_barrier_and_tasks(self):
        self.commit(os.getpid())
        self.store.execute("CREATE TRIGGER fail_recovery BEFORE INSERT ON events WHEN NEW.kind='desktop_update_recovered' "
                           "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with patch('adaptive_rsi.lifecycle.os.kill', side_effect=ProcessLookupError(errno.ESRCH, 'gone')):
            with self.assertRaises(sqlite3.IntegrityError):
                barrier(self.runtime)
        self.assertIsNotNone(self.store.setting(KEY))
        self.assertEqual(self.store.one('SELECT * FROM tasks WHERE id=?', (self.task,)), self.before)

    def test_legacy_installing_barrier_requires_explicit_owned_recovery(self):
        state = self.commit(os.getpid())
        state.pop('owner_pid')
        self.store.put_setting(KEY, state)
        with patch('adaptive_rsi.lifecycle.time.time', return_value=state['expires'] + 86400):
            self.assertIsNotNone(barrier(self.runtime))
        readiness(self.runtime, 'cancel', lease=state['lease'])
        self.assertIsNone(barrier(self.runtime))

    def test_onefile_bootloader_parent_does_not_define_installer_liveness(self):
        state = self.commit(os.getpid())
        with patch('adaptive_rsi.lifecycle.os.getppid', return_value=2147483647):
            self.assertEqual(barrier(self.runtime)['owner_pid'], os.getpid())
            self.assertEqual(barrier(self.runtime)['lease'], state['lease'])

    def test_inflight_model_catalog_blocks_commit_until_it_finishes(self):
        self.runtime.model_center.catalog_inflight.add(('global', 'openai'))
        prepared = readiness(self.runtime, 'prepare', '9.0.0')
        self.assertFalse(prepared['ready'])
        self.assertIn('model_catalog', prepared['reasons'])
        with self.assertRaisesRegex(ValueError, 'active operations'):
            readiness(self.runtime, 'commit', lease=prepared['barrier']['lease'])
        self.runtime.model_center.catalog_inflight.clear()
        committed = readiness(self.runtime, 'commit', lease=prepared['barrier']['lease'])
        self.assertEqual(committed['barrier']['phase'], 'installing')

    def test_catalog_fetch_refuses_during_update_and_releases_registration(self):
        from adaptive_rsi.model_center import CenterError
        with patch.object(self.runtime.updates, 'is_busy', return_value=True):
            with self.assertRaises(CenterError):
                self.runtime.model_center.fetch('openai')
        self.assertFalse(self.runtime.model_center.catalog_inflight)

    def test_native_owner_environment_is_bounded_and_observable(self):
        from adaptive_rsi.desktop import installer_owner
        for value in ('', '-1', '1', '2147483648', '9' * 10000, '１２３'):
            with self.subTest(value=value[:20]), patch.dict(os.environ, {'ADAPTIVE_RSI_DESKTOP_OWNER_PID': value}):
                self.assertIsNone(installer_owner())
        with patch.dict(os.environ, {'ADAPTIVE_RSI_DESKTOP_OWNER_PID': str(os.getpid())}):
            self.assertEqual(installer_owner(), os.getpid())

    def recovered_events(self):
        return [json.loads(row['data']) for row in self.store.query(
            "SELECT data FROM events WHERE kind='desktop_update_recovered' ORDER BY rowid")]

    def test_corrupt_barrier_records_are_recovered_instead_of_key_errors(self):
        """缺 version/lease/phase、非映射或 expires 不可比较的记录必须自愈。

        旧实现在 state["version"]/state["lease"] 处 KeyError、在 .get 或
        expires 比较处 AttributeError/TypeError，经 server 通用 except 变成
        无诊断 500 并锁死准入。
        """
        shapes = (
            ('missing_version', {'lease': 'a' * 48, 'phase': 'draining', 'expires': time.time() + 3600},
             'invalid_record'),
            ('missing_lease', {'version': '9.0.0', 'phase': 'draining', 'expires': time.time() + 3600},
             'invalid_record'),
            ('missing_phase', {'version': '9.0.0', 'lease': 'a' * 48, 'expires': time.time() + 3600},
             'invalid_record'),
            ('not_a_mapping', ['draining'], 'invalid_record'),
            ('scalar', 'draining', 'invalid_record'),
            ('bad_expires', {'version': '9.0.0', 'lease': 'a' * 48, 'phase': 'draining',
                             'expires': 'not-a-timestamp'}, 'drain_expired'),
        )
        for name, state, reason in shapes:
            with self.subTest(name):
                before = len(self.recovered_events())
                self.store.put_setting(KEY, state)
                self.assertIsNone(barrier(self.runtime))
                events = self.recovered_events()
                self.assertEqual(len(events), before + 1)
                self.assertEqual(events[-1]['reason'], reason)
                self.assertEqual(events[-1]['version'],
                                 state.get('version') if isinstance(state, dict) else None)
                self.assertIsNone(self.store.setting(KEY))
                self.assertIsNone(readiness(self.runtime, 'status')['barrier'])
                prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
                self.assertEqual(prepared['version'], '9.0.0')
                self.assertNotEqual(prepared['lease'], 'a' * 48)
                readiness(self.runtime, 'cancel', lease=prepared['lease'])
                self.assertIsNone(self.store.setting(KEY))

    def test_corrupt_barrier_commit_reports_lease_change_instead_of_key_error(self):
        self.store.put_setting(KEY, {'version': '9.0.0', 'phase': 'installing'})
        with self.assertRaisesRegex(ValueError, 'lease changed'):
            readiness(self.runtime, 'commit', lease='not-the-saved-lease')
