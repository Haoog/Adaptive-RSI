import json
import contextlib
import hashlib
import io
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from email.message import Message
import tempfile
import time
import threading
import urllib.error
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import Mock, patch

from adaptive_rsi import __version__
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.updates import RELEASE_API, UpdateManager, atomic_json, commit_installation, installed_root, supervise_restart, valid_backup


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
        # 假时钟跨过 RESTART_HEALTH_SECONDS（60s）预算即退出等待循环。
        with patch("adaptive_rsi.updates.process_present", return_value=True), patch("adaptive_rsi.updates.candidate_health", return_value=False), patch("adaptive_rsi.updates.time.monotonic", side_effect=[0, 61]), patch("adaptive_rsi.updates.subprocess.Popen") as spawn:
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


class UpdatePointerCrashRecoveryTest(unittest.TestCase):
    def test_commit_crash_boundaries_keep_original_rollback_pointers(self):
        class Crash(BaseException):
            pass

        # Cover both first managed installation and an upgrade of a managed installation.
        for managed in [False, True]:
            for boundary in ["journal", "active", "current", "committed"]:
                with self.subTest(managed=managed, boundary=boundary), tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / "state")
                    state = store.root
                    installs = state / "installations"
                    installs.mkdir()
                    previous, candidate = installs / "previous", installs / "candidate"
                    for root in [previous, candidate]:
                        (root / "adaptive_rsi").mkdir(parents=True)
                        (root / "adaptive_rsi/cli.py").write_text("")
                    original_active = {"path": str(previous), "version": __version__, "previous": "older"}
                    original_link = "previous"  # Preserve relative links exactly, too.
                    if managed:
                        atomic_json(installs / "active.json", original_active)
                        (installs / "current").symlink_to(original_link)
                    store.put_setting("update-status", {"state": "restarting", "version": "99.0.0"})
                    store.put_setting("sentinel", "preserve")
                    with contextlib.closing(sqlite3.connect(store.path)) as source, contextlib.closing(sqlite3.connect(installs / "state-before-update.sqlite3")) as destination:
                        source.backup(destination)
                    plan = {"candidate": str(candidate), "previous": str(previous), "target": "99.0.0",
                            "pid": -1, "candidate_pid": 987654321, "phase": "health_check", "state_schema": 1,
                            "backup_ready": True, "session": {"origin": "http://127.0.0.1:9999", "cookie": "fixture", "token": "fixture"}}
                    atomic_json(installs / "restart.json", plan)
                    real_replace = os.replace

                    def crash_after_replace(source, destination):
                        real_replace(source, destination)
                        name = Path(destination).name
                        if name == "restart.json":
                            phase = json.loads(Path(destination).read_text())["phase"]
                            stage = "committed" if phase == "committed" else "journal"
                        else:
                            stage = {"active.json": "active", "current": "current"}.get(name)
                        if stage == boundary:
                            raise Crash()

                    with patch("adaptive_rsi.updates.os.replace", side_effect=crash_after_replace):
                        with self.assertRaises(Crash):
                            commit_installation(state, plan)
                    saved = json.loads((installs / "restart.json").read_text())
                    self.assertEqual(saved["previous_pointers"], {"active": original_active if managed else None,
                                                                "current": original_link if managed else None})
                    failed_candidate, restored = Mock(pid=101), Mock(pid=102)
                    failed_candidate.poll.return_value = 1
                    restored.poll.return_value = None
                    response = Mock()
                    response.__enter__ = Mock(return_value=io.StringIO(json.dumps({"version": __version__, "pid": 102})))
                    response.__exit__ = Mock(return_value=False)

                    def complete(state, plan):
                        store.put_setting("update-status", {"state": "rolled_back", "current": __version__})
                        return True

                    with patch("adaptive_rsi.updates.process_present", return_value=False), patch("adaptive_rsi.updates.candidate_health", return_value=False), patch("adaptive_rsi.updates.subprocess.Popen", side_effect=[failed_candidate, restored]) as spawn, patch("adaptive_rsi.updates.urllib.request.urlopen", return_value=response), patch("adaptive_rsi.updates.complete_rollback", side_effect=complete):
                        supervise_restart(state)
                    self.assertEqual(store.setting("sentinel"), "preserve")
                    if boundary == "committed":
                        # A durable commit never authorizes database rollback after a crash.
                        spawn.assert_not_called()
                        self.assertEqual(store.setting("update-status")["state"], "recovery_required")
                        self.assertTrue((installs / "restart.json").exists())
                        self.assertEqual(installed_root(state, previous), candidate)
                    else:
                        self.assertEqual(store.setting("update-status")["state"], "rolled_back")
                        self.assertEqual([call.kwargs["cwd"] for call in spawn.call_args_list], [str(candidate), str(previous)])
                        self.assertEqual(installed_root(state, previous), previous)
                        self.assertFalse((installs / "restart.json").exists())
                        if managed:
                            self.assertEqual(json.loads((installs / "active.json").read_text()), original_active)
                            self.assertEqual(os.readlink(installs / "current"), original_link)
                        else:
                            self.assertFalse((installs / "active.json").exists())
                            self.assertFalse((installs / "current").is_symlink())
                    store.close()


class UpdateCheckLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(self.temp.name), object())
        self.manager = self.runtime.updates
        self.target = "99.0.0"
        self.runtime.configure({"updates": {"mode": "notify"}})

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def feed(self, target=None):
        target = target or self.target
        name = "adaptive-rsi-" + target + "-plugin.zip"
        prefix = "https://github.com/Haoog/Adaptive-RSI/releases/download/v" + target + "/"
        return json.dumps({"tag_name": "v" + target, "assets": [
            {"name": name, "browser_download_url": prefix + name},
            {"name": name + ".sha256", "browser_download_url": prefix + name + ".sha256"}]}).encode()

    def pending(self):
        return {"state": "waiting_idle", "requested": True, "current": __version__,
                "version": self.target, "checked": 1}

    def test_async_check_publishes_before_worker_starts_and_does_not_block_work(self):
        previous = {"state": "error", "reason": "old error", "checked": 1}
        self.runtime.store.put_setting("update-status", previous)
        entered, finish = threading.Event(), threading.Event()
        workers = []

        def download(*args):
            workers.append(threading.current_thread())
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test did not release feed")
            return self.feed()

        real_thread = threading.Thread

        def make_thread(*args, **kwargs):
            # This assertion runs before Thread.start(), independently of scheduling.
            status = self.manager.snapshot()
            self.assertEqual(status["state"], "checking")
            self.assertEqual(status["previous_status"], previous)
            self.assertEqual(status["checked"], 1)
            self.assertGreater(status["check_started"], 1)
            self.assertFalse(status["busy"])
            self.manager.require_ready()
            return real_thread(*args, **kwargs)

        with patch("adaptive_rsi.updates.download", side_effect=download), patch("adaptive_rsi.updates.threading.Thread", side_effect=make_thread):
            try:
                self.assertTrue(self.manager.check_async())
                self.assertTrue(entered.wait(2))
                self.assertFalse(self.manager.check_async())
                self.assertFalse(self.manager.check())
                self.runtime.configure({"schedule": {"light_minutes": 240}})
                self.assertEqual(self.runtime.config()["schedule"]["light_minutes"], 240)
            finally:
                finish.set()
                for worker in workers:
                    worker.join(5)
        self.assertEqual(self.manager.status()["state"], "available")
        self.assertNotIn("previous_status", self.manager.status())
        self.assertFalse(self.manager.lock.locked())

    def test_failed_thread_start_restores_status_and_releases_lock(self):
        previous = self.pending()
        self.runtime.store.put_setting("update-status", previous)
        with patch("adaptive_rsi.updates.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start")
            with self.assertRaisesRegex(RuntimeError, "cannot start"):
                self.manager.check_async()
        self.assertEqual(self.manager.status(), previous)
        self.assertFalse(self.manager.lock.locked())

    def test_transient_errors_preserve_confirmed_target_and_same_release_resumes(self):
        for error in [OSError("offline"), urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)]:
            with self.subTest(error=type(error).__name__):
                self.runtime.store.put_setting("update-status", self.pending())
                with patch("adaptive_rsi.updates.download", side_effect=error):
                    self.manager.check()
                    self.manager.check()  # Repeated failure must retain the original consent.
                status = self.manager.status()
                self.assertEqual((status["state"], status["requested"], status["version"]),
                                 ("error", True, self.target))
                self.assertNotIn("previous_status", status)
                with patch("adaptive_rsi.updates.download", return_value=self.feed()):
                    self.manager.check()
                self.assertEqual(self.manager.status()["state"], "waiting_idle")
                with patch.object(self.manager, "apply_async") as apply:
                    self.manager.tick()
                apply.assert_called_once_with(requested=True)

    def test_changed_target_or_no_release_clears_old_consent_after_failure(self):
        for result in [self.feed("99.0.1"), urllib.error.HTTPError("https://example.invalid", 404, "missing", {}, None)]:
            with self.subTest(result=type(result).__name__):
                self.runtime.store.put_setting("update-status", self.pending())
                with patch("adaptive_rsi.updates.download", side_effect=OSError("offline")):
                    self.manager.check()
                options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                with patch("adaptive_rsi.updates.download", **options):
                    self.manager.check()
                self.assertNotIn("requested", self.manager.status())
                with patch.object(self.manager, "apply_async") as apply:
                    self.manager.tick()
                apply.assert_not_called()

    def test_restart_during_check_restores_pending_intent_without_blocking_work(self):
        previous = self.pending()
        self.runtime.store.put_setting("update-status", previous)
        self.assertEqual(self.manager.begin_check(), previous)
        self.manager.lock.release()  # A crashed process cannot retain its in-memory lock.
        self.manager.restore_startup()
        self.assertEqual(self.manager.status()["state"], "error")
        self.assertTrue(self.manager.status()["requested"])
        self.assertEqual(self.manager.status()["version"], self.target)
        self.manager.require_ready()


class UpdateRateLimitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(self.temp.name), object())
        self.manager = self.runtime.updates
        self.manager.shutdown = Mock()
        self.runtime.configure({"updates": {"mode": "notify"}})
        self.now = 1792996474
        self.reset = 1792998274
        self.clock = patch("adaptive_rsi.updates.time.time", side_effect=lambda: self.now)
        self.clock.start()
        # Any unmocked network operation fails without opening a connection.
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.clock.stop()
        self.runtime.close()
        self.temp.cleanup()

    def error(self, code=403, remaining="0", reset=None, url=RELEASE_API):
        headers = Message()
        if remaining is not None:
            headers["x-ratelimit-remaining"] = remaining
        if reset is not None:
            headers["x-ratelimit-reset"] = str(reset)
        headers["Retry-After"] = "999999999"  # Does not broaden the primary-limit contract.
        return urllib.error.HTTPError(url, code, "Forbidden", headers,
                                      io.BytesIO(b'{"message":"API rate limit exceeded for fixture"}'))

    def pending(self, failures=0):
        return {"state": "waiting_idle", "version": "99.0.0", "current": __version__,
                "requested": True, "checked": self.now, "failures": failures}

    def inline_thread(self, target, args=(), **kwargs):
        return SimpleNamespace(start=lambda: target(*args))

    def test_check_cooldown_guards_all_entry_points_and_survives_manager_restart(self):
        previous = self.pending(failures=1)
        self.runtime.store.put_setting("update-status", previous)
        with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=self.reset)) as request:
            self.assertTrue(self.manager.check())
            status = self.manager.status()
            self.assertEqual(status["reason_code"], "github_primary_rate_limit")
            self.assertEqual(status["rate_limit_until"], self.reset)
            for key in ["version", "requested", "failures"]:
                self.assertEqual(status[key], previous[key])
            self.assertNotIn("retry_after", status)
            # A replacement manager uses the persisted cooldown, not an in-memory timer.
            self.manager = UpdateManager(self.runtime)
            self.manager.shutdown = Mock()
            for moment in [self.now, self.now + 120, self.reset - 1]:
                self.now = moment
                self.assertFalse(self.manager.check())
                self.assertFalse(self.manager.check_async())
                with self.assertRaisesRegex(ValueError, "rate limit reset"):
                    self.manager.apply_async()
                self.manager.tick()
                self.assertTrue(self.manager.lock.acquire(False))
                self.manager.apply(status)  # Worker guard also preserves the status.
                self.assertFalse(self.manager.lock.locked())
                self.assertEqual(self.manager.status(), status)
            request.assert_called_once()
            self.assertFalse(self.manager.installing)
            self.manager.require_ready()
        self.now = self.reset
        # The preserved installation remains eligible immediately at the reset.
        with patch.object(self.manager, "apply_async") as apply:
            self.manager.tick()
        apply.assert_called_once_with(requested=True)

    def test_install_cooldown_does_not_spend_retry_budget_and_resumes_at_reset(self):
        previous = self.pending(failures=2)
        self.runtime.store.put_setting("update-status", previous)
        with patch("adaptive_rsi.updates.threading.Thread", side_effect=self.inline_thread), patch(
                "adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=self.reset)) as request:
            self.manager.apply_async()
            status = self.manager.status()
            self.assertEqual(status["failures"], 2)
            self.assertTrue(status["requested"])
            self.assertEqual(status["version"], previous["version"])
            self.assertEqual(status["rate_limit_until"], self.reset)
            for moment in [self.now + 120, self.now + 360, self.reset - 1]:
                self.now = moment
                self.manager.tick()
            request.assert_called_once()
        self.now = self.reset
        # A different error after the deadline spends the next generic retry normally.
        with patch("adaptive_rsi.updates.threading.Thread", side_effect=self.inline_thread), patch(
                "adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(remaining="1", reset=self.reset + 100)) as request:
            self.manager.tick()
            request.assert_called_once()
        status = self.manager.status()
        self.assertEqual(status["failures"], 3)
        self.assertEqual(status["retry_after"], self.now + 480)
        self.assertNotIn("reason_code", status)
        self.assertNotIn("rate_limit_until", status)
        self.assertFalse(self.manager.installing)
        self.manager.shutdown.assert_not_called()

    def test_notify_checks_resume_at_reset_but_manual_checks_require_a_click(self):
        for mode in ["notify", "manual"]:
            with self.subTest(mode=mode):
                self.now = self.reset - 1800
                self.runtime.configure({"updates": {"mode": mode}})
                self.runtime.store.put_setting("update-status", {"state": "unchecked"})
                with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=self.reset)) as request:
                    self.manager.check()
                    self.now = self.reset - 1
                    self.manager.tick()
                    request.assert_called_once()
                self.now = self.reset
                with patch.object(self.manager, "check_async") as check:
                    self.manager.tick()
                    self.assertEqual(check.call_count, int(mode == "notify"))
                with patch("adaptive_rsi.updates.download", return_value=b'{"tag_name":"v0.0.1"}') as download:
                    self.assertTrue(self.manager.check())
                    download.assert_called_once()
                self.assertEqual(self.manager.status()["state"], "up_to_date")
                self.assertNotIn("rate_limit_until", self.manager.status())

    def test_nonrate_and_malformed_responses_keep_existing_check_and_install_behavior(self):
        controls = [
            {"remaining": None, "reset": self.reset},
            {"remaining": "1", "reset": self.reset},
            {"reset": None}, {"reset": "not-a-time"}, {"reset": "NaN"},
            {"reset": "1792998274.0"}, {"reset": "9" * 5000},
            {"reset": self.now}, {"reset": self.now - 1}, {"reset": self.now + 3601},
            {"code": 429, "reset": self.reset},
            {"url": "https://github.com/fixture", "reset": self.reset},
        ]
        for options in controls:
            with self.subTest(options={k: str(v)[:40] for k, v in options.items()}):
                self.runtime.store.put_setting("update-status", self.pending())
                with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(**options)) as request:
                    self.manager.check()
                    self.assertTrue(self.manager.check())
                    self.assertEqual(request.call_count, 2)
                checked = self.manager.status()
                self.assertEqual(checked["reason"], "GitHub HTTP " + str(options.get("code", 403)))
                self.assertNotIn("reason_code", checked)
                self.assertNotIn("rate_limit_until", checked)
                with patch("adaptive_rsi.updates.threading.Thread", side_effect=self.inline_thread), patch(
                        "adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(**options)):
                    self.manager.apply_async()
                failed = self.manager.status()
                self.assertEqual(failed["failures"], 1)
                self.assertEqual(failed["retry_after"], self.now + 120)
                self.assertNotIn("rate_limit_until", failed)

    def test_repeated_primary_limits_extend_wait_without_exhausting_install_retries(self):
        self.runtime.store.put_setting("update-status", self.pending())
        with patch("adaptive_rsi.updates.threading.Thread", side_effect=self.inline_thread):
            for _ in range(4):
                deadline = self.now + 3600  # Inclusive maximum accepted primary window.
                with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=deadline)) as request:
                    self.manager.tick()
                    request.assert_called_once()
                self.assertEqual(self.manager.status()["failures"], 0)
                self.assertEqual(self.manager.status()["rate_limit_until"], deadline)
                self.now = deadline
        with patch.object(self.manager, "apply_async") as apply:
            self.manager.tick()
        apply.assert_called_once_with(requested=True)

    def test_automatic_target_keeps_authorization_and_notify_target_rechecks_after_reset(self):
        major, minor, patch_version = map(int, __version__.split("."))
        previous = {**self.pending(), "state": "available", "requested": False,
                    "version": f"{major}.{minor}.{patch_version + 1}"}
        self.runtime.configure({"updates": {"mode": "automatic"}})
        self.runtime.store.put_setting("update-status", previous)
        with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=self.reset)):
            self.manager.check()
        self.assertFalse(self.manager.status()["requested"])
        self.assertEqual(self.manager.status()["version"], previous["version"])
        self.now = self.reset
        with patch.object(self.manager, "apply_async") as apply:
            self.manager.tick()
        apply.assert_called_once_with(requested=False)
        self.runtime.configure({"updates": {"mode": "notify"}})
        with patch.object(self.manager, "check_async") as check, patch.object(self.manager, "apply_async") as apply:
            self.manager.tick()
        check.assert_called_once()
        apply.assert_not_called()

    def test_successful_check_after_reset_keeps_only_same_target_consent(self):
        for target in ["99.0.0", "99.0.1"]:
            with self.subTest(target=target):
                self.now = self.reset - 1800
                self.runtime.store.put_setting("update-status", self.pending())
                with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error(reset=self.reset)):
                    self.manager.check()
                self.now = self.reset
                name = "adaptive-rsi-" + target + "-plugin.zip"
                prefix = "https://github.com/Haoog/Adaptive-RSI/releases/download/v" + target + "/"
                feed = json.dumps({"tag_name": "v" + target, "assets": [
                    {"name": name, "browser_download_url": prefix + name},
                    {"name": name + ".sha256", "browser_download_url": prefix + name + ".sha256"}]}).encode()
                with patch("adaptive_rsi.updates.download", return_value=feed):
                    self.assertTrue(self.manager.check())
                status = self.manager.status()
                self.assertEqual(status["state"], "waiting_idle" if target == "99.0.0" else "available")
                self.assertEqual(bool(status.get("requested")), target == "99.0.0")
                self.assertNotIn("reason_code", status)
                self.assertNotIn("rate_limit_until", status)

    def test_expired_cooldown_does_not_survive_an_unrelated_check_error(self):
        previous = {**self.pending(), "reason_code": "github_primary_rate_limit", "rate_limit_until": self.now}
        self.runtime.store.put_setting("update-status", previous)
        with patch("adaptive_rsi.updates.urllib.request.urlopen", side_effect=self.error()):
            self.manager.check()
        self.assertNotIn("reason_code", self.manager.status())
        self.assertNotIn("rate_limit_until", self.manager.status())
        self.assertTrue(self.manager.status()["requested"])


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
