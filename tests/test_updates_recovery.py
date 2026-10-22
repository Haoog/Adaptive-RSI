import json
import contextlib
import sqlite3
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from adaptive_rsi import __version__
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.updates import supervise_restart, valid_backup


class UpdateJournalRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.installs = self.store.root / "installations"
        self.installs.mkdir()
        self.candidate = self.installs / "candidate"
        self.candidate.mkdir()
        self.store.put_setting("sentinel", "current-database")
        self.store.put_setting("update-status", {"state": "restarting", "version": "9.0.0"})
        self.plan = {"candidate": str(self.candidate), "previous": str(Path(__file__).resolve().parents[1]),
                     "target": "9.0.0", "pid": -1, "candidate_pid": 987654321, "state_schema": 1,
                     "session": {"origin": "http://127.0.0.1:9999", "cookie": "fixture", "token": "fixture"}}

    def tearDown(self):
        self.temp.cleanup()

    def write_plan(self, phase):
        self.plan["phase"] = phase
        (self.installs / "restart.json").write_text(json.dumps(self.plan))

    def test_committed_healthy_candidate_is_adopted_without_restart_or_database_restore(self):
        self.write_plan("committed")
        with patch("adaptive_rsi.updates.candidate_health", return_value=True), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("update-status")["state"], "installed")
        self.assertEqual(self.store.setting("sentinel"), "current-database")
        self.assertEqual(json.loads((self.installs / "active.json").read_text())["path"], str(self.candidate))
        self.assertFalse((self.installs / "restart.json").exists())

    def test_committed_unverified_candidate_requires_recovery_and_preserves_database(self):
        self.write_plan("committed")
        with patch("adaptive_rsi.updates.candidate_health", return_value=False), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("update-status")["state"], "recovery_required")
        self.assertEqual(self.store.setting("sentinel"), "current-database")
        self.assertTrue((self.installs / "restart.json").exists())

    def test_installed_state_never_replays_an_old_healthcheck_journal(self):
        self.write_plan("health_check")
        self.store.put_setting("update-status", {"state": "installed", "current": "9.0.0"})
        with patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("sentinel"), "current-database")
        self.assertFalse((self.installs / "restart.json").exists())

    def test_existing_healthy_child_is_adopted_before_waiting_for_its_runtime_lock(self):
        self.write_plan("health_check")
        with patch("adaptive_rsi.updates.process_present", return_value=True), patch("adaptive_rsi.updates.candidate_health", return_value=True), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("update-status")["state"], "installed")
        self.assertEqual(self.store.setting("sentinel"), "current-database")

    def test_unverified_living_child_is_not_killed_or_overwritten(self):
        self.write_plan("health_check")
        with patch("adaptive_rsi.updates.process_present", return_value=True), patch("adaptive_rsi.updates.candidate_health", return_value=False), patch("adaptive_rsi.updates.time.monotonic", side_effect=[0, 26]), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("update-status")["state"], "recovery_required")
        self.assertEqual(self.store.setting("sentinel"), "current-database")

    def test_missing_original_backup_and_unsupported_migration_stop_safely(self):
        for schema in [1, 2]:
            with self.subTest(schema=schema):
                self.store.put_setting("update-status", {"state": "restarting", "version": "9.0.0"})
                self.plan.update(backup_ready=True, state_schema=schema)
                self.write_plan("prepared")
                with patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
                    supervise_restart(self.store.root)
                spawn.assert_not_called()
                self.assertEqual(self.store.setting("update-status")["state"], "recovery_required")
                self.assertEqual(self.store.setting("sentinel"), "current-database")

    def test_corrupt_backup_cannot_replace_the_current_database(self):
        self.plan["backup_ready"] = True
        self.write_plan("prepared")
        (self.installs / "state-before-update.sqlite3").write_bytes(b"invalid sqlite backup")
        with patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            supervise_restart(self.store.root)
        spawn.assert_not_called()
        self.assertEqual(self.store.setting("update-status")["state"], "recovery_required")
        self.assertEqual(self.store.setting("sentinel"), "current-database")

    def test_static_wal_database_backup_is_valid_without_creating_sidecars(self):
        backup = self.installs / "state-before-update.sqlite3"
        with contextlib.closing(sqlite3.connect(self.store.path)) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        self.assertTrue(valid_backup(backup))
        self.assertFalse(Path(str(backup) + "-wal").exists())
        self.assertFalse(Path(str(backup) + "-shm").exists())


class UpdateIdleAndRetryTest(unittest.TestCase):
    def test_authentication_is_busy_and_automatic_update_policy_is_respected(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp))
            try:
                major, minor, _ = map(int, __version__.split("."))
                status = {"state": "available", "current": __version__, "version": f"{major}.{minor + 1}.0", "checked": time.time()}
                runtime.store.put_setting("update-status", status)
                runtime.configure({"updates": {"mode": "automatic", "auto_policy": "patch"}})
                with patch.object(runtime.updates, "apply_async") as apply:
                    runtime.updates.tick()
                    apply.assert_not_called()
                runtime.configure({"updates": {"auto_policy": "minor"}})
                with patch.object(runtime.updates, "apply_async") as apply:
                    runtime.updates.tick()
                    apply.assert_called_once_with(requested=False)
                with patch.object(runtime, "auth_sessions", SimpleNamespace(busy=1, close=lambda: None)), patch.object(runtime.updates, "apply_async") as apply:
                    runtime.updates.tick()
                    apply.assert_not_called()
            finally:
                runtime.close()

    def test_interrupted_download_recovers_and_retry_backoff_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp))
            try:
                runtime.store.put_setting("update-status", {"state": "downloading", "version": "9.0.0", "current": __version__, "requested": True, "checked": time.time()})
                runtime.updates.restore_startup()
                self.assertEqual(runtime.updates.status()["state"], "error")
                runtime.store.put_setting("update-status", {**runtime.updates.status(), "failures": 3, "retry_after": 0})
                with patch.object(runtime.updates, "apply_async") as apply:
                    runtime.updates.tick()
                    apply.assert_not_called()
                runtime.store.put_setting("update-status", {**runtime.updates.status(), "failures": 2, "retry_after": time.time() + 300})
                with patch.object(runtime.updates, "apply_async") as apply:
                    runtime.updates.tick()
                    apply.assert_not_called()
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
