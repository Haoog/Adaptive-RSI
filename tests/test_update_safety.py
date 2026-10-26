import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from adaptive_rsi import __version__
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.updates import atomic_json, download, supervise_restart


class UpdateSafetyTest(unittest.TestCase):
    def test_atomic_json_is_private_before_replace_and_ignores_fixed_temp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "session.json"
            unrelated = root / "unrelated"
            unrelated.write_text("preserve")
            destination.with_suffix(".json.tmp").symlink_to(unrelated)
            real_replace = os.replace
            observed = []

            def replace(source, target):
                observed.append(Path(source).stat().st_mode & 0o777)
                self.assertEqual(json.loads(Path(source).read_text()), {"token": "fixture"})
                real_replace(source, target)

            with patch("adaptive_rsi.updates.os.replace", side_effect=replace):
                atomic_json(destination, {"token": "fixture"})
            self.assertEqual(observed, [0o600])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(unrelated.read_text(), "preserve")

    def test_atomic_json_failure_preserves_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "session.json"
            atomic_json(destination, {"old": True})
            with patch("adaptive_rsi.updates.os.replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    atomic_json(destination, {"new": True})
            self.assertEqual(json.loads(destination.read_text()), {"old": True})
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_download_total_deadline_stops_slow_chunks(self):
        response = Mock()
        response.url = "https://github.com/Haoog/Adaptive-RSI/releases/download/v1.0.0/fixture"
        response.headers = {}
        response.read1.return_value = b"chunk"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("adaptive_rsi.updates.urllib.request.urlopen", return_value=response), patch("adaptive_rsi.updates.time.monotonic", side_effect=[0, 1, 901]):
            with self.assertRaisesRegex(ValueError, "total time budget"):
                download(response.url, 1024)
        response.read1.assert_called_once_with(65536)
        response.read.assert_not_called()

    def test_rollback_completion_unlocks_only_after_health_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Store(directory))
            try:
                runtime.store.put_setting("update-status", {"state": "restarting", "version": __version__})
                runtime.updates.restore_startup()
                self.assertTrue(runtime.updates.is_busy())
                runtime.store.put_setting("update-status", {"state": "rolled_back", "current": __version__})
                self.assertFalse(runtime.updates.is_busy())
            finally:
                runtime.close()

    def test_replacement_supervisor_does_not_shut_down_restored_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Store(directory))
            installs = runtime.store.root / "installations"
            installs.mkdir()
            atomic_json(installs / "restart.json", {
                "phase": "rollback_health_check", "state_schema": 1,
                "candidate_pid": -1, "rollback_pid": os.getpid(), "previous": directory,
            })
            runtime.store.put_setting("update-status", {"state": "restarting", "version": __version__, "checked": 0})
            runtime.updates.shutdown = Mock()
            try:
                with patch("adaptive_rsi.updates.supervisor_active", return_value=False), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
                    runtime.updates.restore_startup()
                    self.assertTrue(runtime.updates.is_busy())
                spawn.assert_called_once()
                runtime.updates.shutdown.assert_not_called()
            finally:
                runtime.close()

    def test_rollback_ack_recovery_never_discards_journal_as_successful_install(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            installs = store.root / "installations"
            installs.mkdir()
            atomic_json(installs / "restart.json", {
                "pid": -1, "phase": "rollback_ack", "rollback_pid": 12345,
                "session": {}, "target": "9.0.0", "state_schema": 1,
            })
            store.put_setting("update-status", {"state": "installed", "current": __version__})
            def complete(state, plan):
                store.put_setting("update-status", {"state": "rolled_back", "current": __version__})
                return True
            with patch("adaptive_rsi.updates.process_present", return_value=True), patch("adaptive_rsi.updates.candidate_health", return_value=True), patch("adaptive_rsi.updates.complete_rollback", side_effect=complete) as completion, patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
                supervise_restart(store.root)
            completion.assert_called_once()
            spawn.assert_not_called()
            self.assertEqual(store.setting("update-status")["state"], "rolled_back")
            store.close()

    def test_recovered_rollback_verifies_existing_process_without_spawning_or_restore(self):
        for healthy in [True, False]:
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                installs = store.root / "installations"
                installs.mkdir()
                store.put_setting("sentinel", "keep")
                store.put_setting("update-status", {"state": "restarting", "version": __version__})
                atomic_json(installs / "restart.json", {
                    "pid": -1, "phase": "rollback_health_check", "rollback_pid": 12345,
                    "session": {}, "target": "9.0.0", "state_schema": 1,
                })
                def complete(state, plan):
                    store.put_setting("update-status", {"state": "rolled_back", "current": __version__})
                    return True

                with patch("adaptive_rsi.updates.process_present", return_value=healthy), patch("adaptive_rsi.updates.candidate_health", return_value=healthy), patch("adaptive_rsi.updates.complete_rollback", side_effect=complete) as completion, patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
                    supervise_restart(store.root)
                self.assertEqual(completion.call_count, int(healthy))
                spawn.assert_not_called()
                self.assertEqual(store.setting("sentinel"), "keep")
                self.assertEqual(store.setting("update-status")["state"], "rolled_back" if healthy else "recovery_required")
                self.assertEqual((installs / "restart.json").exists(), not healthy)
                store.close()
