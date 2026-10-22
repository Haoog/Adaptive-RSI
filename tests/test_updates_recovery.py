import json
import contextlib
import hashlib
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import urllib.request
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


class LegacySupervisorCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(Path(self.temp.name) / "state"))
        self.manager = self.runtime.updates
        self.installs = self.runtime.store.root / "installations"
        self.installs.mkdir()
        self.plan_path = self.installs / "restart.json"
        self.plan_path.write_text(json.dumps({"candidate": "unused", "previous": "unused", "target": __version__, "pid": -1, "session": {}}))
        self.runtime.store.put_setting("update-status", {"state": "restarting", "version": __version__, "checked": 0})
        self.runtime.store.put_setting("sentinel", "preserved")
        self.manager.shutdown = Mock()

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_legacy_journal_waits_without_lock_and_never_spawns_a_replacement(self):
        with patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            self.manager.restore_startup()
            for _ in range(5):
                self.assertTrue(self.manager.is_busy())
                self.assertFalse(self.manager.recovering)
        spawn.assert_not_called()
        self.manager.shutdown.assert_not_called()
        self.assertEqual(self.runtime.store.setting("sentinel"), "preserved")

    def test_legacy_timeout_requires_manual_recovery_without_replaying_database(self):
        self.manager.restore_startup()
        self.manager.legacy_deadline = 0
        with patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
            self.assertTrue(self.manager.is_busy())
        spawn.assert_not_called()
        self.manager.shutdown.assert_not_called()
        self.assertEqual(self.manager.status()["state"], "recovery_required")
        self.assertEqual(self.runtime.store.setting("sentinel"), "preserved")
        self.assertTrue(self.plan_path.exists())

    def test_installed_clears_stale_recovery_before_reading_deleted_journal(self):
        self.manager.restore_startup()
        self.manager.recovering = True
        self.plan_path.unlink()
        self.runtime.store.put_setting("update-status", {"state": "installed", "current": __version__})
        with patch.object(self.manager, "restart_plan", side_effect=AssertionError("must inspect completion first")):
            self.assertFalse(self.manager.is_busy())
        self.assertFalse(self.manager.recovering)
        self.assertIsNone(self.manager.legacy_deadline)
        self.manager.shutdown.assert_not_called()

    def test_completion_racing_missing_journal_is_rechecked(self):
        self.manager.restore_startup()
        def deleted_after_completion():
            self.runtime.store.put_setting("update-status", {"state": "installed", "current": __version__})
            self.plan_path.unlink()
            return None
        with patch.object(self.manager, "restart_plan", side_effect=deleted_after_completion):
            self.assertFalse(self.manager.is_busy())
        self.assertEqual(self.manager.status()["state"], "installed")

    def test_previous_installed_status_cannot_unlock_a_new_download(self):
        self.manager.installing = True
        self.runtime.store.put_setting("update-status", {"state": "installed", "current": __version__})
        self.assertTrue(self.manager.is_busy())


@unittest.skipUnless(os.environ.get("RSI_LEGACY_SOURCE"), "Set RSI_LEGACY_SOURCE to a read-only installed 0.3.0 source")
class InstalledLegacySupervisorTest(unittest.TestCase):
    def test_real_legacy_supervisor_preserves_candidate_through_early_state_polling(self):
        legacy = Path(os.environ["RSI_LEGACY_SOURCE"]).resolve()
        source_hash = hashlib.sha256((legacy / "adaptive_rsi/updates.py").read_bytes()).hexdigest()
        self.assertEqual(source_hash, "bfedbff0acbcee0d29a79791367029a976d0791e7936607985ca769bdcd8dab0")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "state")
            installs = store.root / "installations"
            candidate = installs / "candidate"
            candidate.mkdir(parents=True)
            shutil.copytree(Path(__file__).resolve().parents[1] / "adaptive_rsi", candidate / "adaptive_rsi", ignore=shutil.ignore_patterns("__pycache__"))
            major, minor, patch_version = map(int, __version__.split("."))
            target = f"{major}.{minor}.{patch_version + 1}"
            metadata = candidate / "adaptive_rsi/__init__.py"
            metadata.write_text(metadata.read_text().replace('__version__ = "' + __version__ + '"', '__version__ = "' + target + '"'))
            # Observe subprocess calls without replacing the actual old supervisor or HTTP checks.
            hooks = root / "hooks"
            hooks.mkdir()
            calls = root / "spawn.jsonl"
            (hooks / "sitecustomize.py").write_text(
                "import json,os,subprocess\n"
                "_spawn = subprocess.Popen\n"
                "def observed(*args, **kwargs):\n"
                "    with open(os.environ['RSI_SPAWN_LOG'], 'a') as log: log.write(json.dumps(args[0])+ '\\n')\n"
                "    return _spawn(*args, **kwargs)\n"
                "subprocess.Popen = observed\n")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = "http://127.0.0.1:" + str(port)
            session = {"origin": origin, "cookie": "rsi_" + str(port), "token": "isolated-legacy-test", "csrf": "isolated-csrf", "url": origin + "/?token=isolated-legacy-test", "pid": -1}
            plan = {"candidate": str(candidate), "previous": str(legacy), "target": target, "pid": -1, "session": session}
            (installs / "restart.json").write_text(json.dumps(plan))
            store.put_setting("update-status", {"state": "restarting", "version": target, "checked": time.time()})
            store.put_setting("sentinel", {"preserve": "legacy-upgrade"})
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(hooks), "RSI_SPAWN_LOG": str(calls)}
            child_pid = None
            samples = []
            with (root / "supervisor.log").open("wb") as output:
                supervisor = subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(store.root), "update-worker"], cwd=legacy, env=env, stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 35
                    completed_at = None
                    while time.monotonic() < deadline:
                        try:
                            def get(path):
                                request = urllib.request.Request(origin + path, headers={"Cookie": session["cookie"] + "=" + session["token"]})
                                with urllib.request.urlopen(request, timeout=1) as response:
                                    return json.load(response)
                            health = get("/api/health")
                            child_pid = health["pid"]
                            self.assertEqual(health["version"], target)
                            state = get("/api/state")
                            samples.append(state["update"])
                            if supervisor.poll() is not None and state["update"]["state"] == "installed" and not state["update"]["busy"]:
                                completed_at = completed_at or time.monotonic()
                                if time.monotonic() - completed_at >= 1:
                                    break
                        except OSError:
                            pass
                        time.sleep(0.01)
                    evidence = {"old_source_sha256": source_hash, "supervisor_exit": supervisor.poll(), "state_samples": samples,
                                "status": store.setting("update-status"), "log": (store.root / "server.log").read_text() if (store.root / "server.log").exists() else ""}
                    self.assertIsNotNone(completed_at, evidence)
                    self.assertEqual(supervisor.wait(timeout=5), 0, evidence)
                    self.assertTrue(any(item["busy"] for item in samples), evidence)
                    self.assertEqual(json.loads((installs / "active.json").read_text())["path"], str(candidate))
                    self.assertEqual(store.setting("sentinel"), {"preserve": "legacy-upgrade"})
                    self.assertFalse((installs / "restart.json").exists())
                    spawned = [json.loads(line) for line in calls.read_text().splitlines()]
                    self.assertEqual(len(spawned), 1, spawned)
                    self.assertIn("serve", spawned[0])
                    print("Legacy supervisor real-source evidence: " + json.dumps({"source_sha256": source_hash, "candidate_test_version": target, "polls": len(samples), "busy_polls": sum(bool(item["busy"]) for item in samples), "spawned": len(spawned), "installed": True, "sentinel_preserved": True}))
                finally:
                    if supervisor.poll() is None:
                        supervisor.terminate()
                        supervisor.wait(timeout=10)
                    if child_pid is None and (store.root / "server.json").exists():
                        child_pid = json.loads((store.root / "server.json").read_text())["pid"]
                    if child_pid:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(child_pid, signal.SIGTERM)
                        deadline = time.monotonic() + 10
                        while (store.root / "server.json").exists() and time.monotonic() < deadline:
                            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
