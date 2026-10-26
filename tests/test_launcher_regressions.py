import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import start


class LauncherRegressionTest(unittest.TestCase):
    SESSION = {"url": "http://127.0.0.1:123/?token=fixture"}

    def assert_lock_released(self, state):
        with (state / "launcher.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_rechecks_after_acquiring_lock_without_spawning_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(start, "live_session", side_effect=[None, self.SESSION]) as probe, \
                    patch.object(start.subprocess, "Popen") as spawn:
                self.assertEqual(start.ensure_session(state, state), self.SESSION)
                self.assertEqual(probe.call_count, 2)
                spawn.assert_not_called()
            self.assert_lock_released(state)

    def test_spawn_failure_releases_launcher_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(start, "live_session", return_value=None), \
                    patch.object(start.subprocess, "Popen", side_effect=OSError("spawn failed")):
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    start.ensure_session(state, state)
            self.assert_lock_released(state)

    def test_owner_and_contender_share_sixty_second_health_deadline(self):
        for contended in [False, True]:
            with self.subTest(contended=contended), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                clock = [100.0]
                def advance(seconds):
                    clock[0] += seconds
                def unhealthy(*args, timeout=2):
                    advance(timeout)
                    return None
                flock = start.fcntl.flock
                def acquire(fd, flags):
                    if contended:
                        raise BlockingIOError()
                    return flock(fd, flags)
                with patch.object(start.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(start.time, "sleep", side_effect=advance), \
                        patch.object(start, "live_session", side_effect=unhealthy), \
                        patch.object(start.fcntl, "flock", side_effect=acquire), \
                        patch.object(start.subprocess, "Popen") as spawn:
                    with self.assertRaises(SystemExit):
                        start.ensure_session(state, state)
                    # 初检的2秒不计入持锁/等待启动的60秒；网络请求不能突破截止时间。
                    self.assertAlmostEqual(clock[0], 162.0)
                    self.assertEqual(spawn.call_count, 0 if contended else 1)
                self.assert_lock_released(state)

    def test_ready_service_returns_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(start, "live_session", side_effect=[None, None, self.SESSION]), \
                    patch.object(start.subprocess, "Popen") as spawn:
                self.assertEqual(start.ensure_session(state, state), self.SESSION)
                spawn.assert_called_once()
            self.assert_lock_released(state)

    def test_contender_reuses_ready_service_without_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with (state / "launcher.lock").open("a+") as owner:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(start, "live_session", side_effect=[None, self.SESSION]), \
                        patch.object(start.subprocess, "Popen") as spawn:
                    self.assertEqual(start.ensure_session(state, state), self.SESSION)
                    spawn.assert_not_called()
            self.assert_lock_released(state)
