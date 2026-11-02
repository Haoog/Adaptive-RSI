"""计划保存交错与时区窗口的确定性回归；只使用临时状态，不启动 worker。"""
import datetime
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


class ScheduleConsistencyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rsi-schedule-consistency-")
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, object())
        self.store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                           ("p", "Project", str(self.root / "unused-project"), "{}", 0, 0))
        self.now = datetime.datetime(2026, 10, 25, 10, tzinfo=datetime.timezone.utc).timestamp()
        self.clock = patch("adaptive_rsi.runtime.time.time", return_value=self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.runtime.close()
        self.temp.cleanup()

    def due(self, kind):
        return self.store.one("SELECT due FROM schedules WHERE project='p' AND kind=?", (kind,))["due"]

    def test_save_during_schedule_keeps_latest_deadline(self):
        for kind in ["light", "issues"]:
            for before, after in [(60, 240), (240, 60)]:
                for scope in ["project", "global", "inherit"]:
                    with self.subTest(kind=kind, before=before, after=after, scope=scope):
                        self.store.execute("DELETE FROM schedules")
                        self.store.execute("UPDATE projects SET config='{}' WHERE id='p'")
                        self.store.put_setting("config", {})
                        field = kind + "_minutes"
                        initial = {"github": {"repository": "owner/project"},
                                   "sources": {"inspection": kind == "light", "issues": kind == "issues"},
                                   "schedule": {field: before}}
                        self.runtime.configure(initial, None if scope == "global" else "p")
                        if scope == "inherit":
                            self.runtime.configure({"schedule": {field: after}})
                        self.store.execute("INSERT INTO schedules(project,kind,due) VALUES (?,?,?)",
                                           ("p", kind, self.now))
                        stale = self.runtime.config("p")
                        inside_request = threading.Event()
                        release_tick = threading.Event()
                        errors = []
                        original_request = self.runtime.request
                        original_lock = self.runtime.dispatch_lock

                        # 无锁的原实现先完成保存、再回写旧 due；修复后保存等待 tick
                        # 释放锁。两条路径都有确定事件，不依靠 sleep 或超时判定顺序。
                        class ObservedLock:
                            def __enter__(self):
                                if not original_lock.acquire(blocking=False):
                                    release_tick.set()
                                    original_lock.acquire()
                                return self

                            def __exit__(self, *args):
                                original_lock.release()
                                if threading.current_thread().name == "config-save":
                                    release_tick.set()

                        def request(*args, **kwargs):
                            inside_request.set()
                            if not release_tick.wait(5):
                                raise AssertionError("save never reached the schedule lock")
                            return original_request(*args, **kwargs)

                        def tick():
                            self.runtime.schedule("p", stale, self.now)

                        def save():
                            if scope == "inherit":
                                self.runtime.configure({}, "p", ["schedule." + field])
                            else:
                                self.runtime.configure({"schedule": {field: after}},
                                                       None if scope == "global" else "p")

                        def capture(fn):
                            try:
                                fn()
                            except BaseException as exc:
                                errors.append(exc)
                                release_tick.set()

                        with patch.object(self.runtime, "dispatch_lock", ObservedLock()), \
                                patch.object(self.runtime, "request", side_effect=request):
                            worker = threading.Thread(target=capture, args=(tick,), daemon=True)
                            saver = threading.Thread(target=capture, args=(save,), name="config-save", daemon=True)
                            worker.start()
                            try:
                                self.assertTrue(inside_request.wait(5))
                                saver.start()
                                worker.join(5)
                                saver.join(5)
                                self.assertFalse(worker.is_alive(), "schedule deadlocked")
                                self.assertFalse(saver.is_alive(), "configuration save deadlocked")
                            finally:
                                release_tick.set()
                                worker.join(5)
                                if saver.ident is not None:
                                    saver.join(5)
                        self.assertEqual(errors, [])
                        self.assertEqual(self.runtime.config("p")["schedule"][field], after)
                        self.assertEqual(self.due(kind), self.now + after * 60)

    def test_stale_snapshot_before_first_schedule_uses_saved_intervals(self):
        self.runtime.configure({"github": {"repository": "owner/project"}}, "p")
        stale = self.runtime.config("p")
        self.runtime.configure({"schedule": {"light_minutes": 240, "issues_minutes": 120}}, "p")
        # 调用者也可能已持有 dispatch_lock；同一线程嵌套不能死锁。
        with self.runtime.dispatch_lock:
            self.runtime.schedule("p", stale, self.now)
        self.assertEqual(self.due("light"), self.now + 240 * 60)
        self.assertEqual(self.due("issues"), self.now + 120 * 60)

    def test_stale_tick_preserves_saved_deadline_then_uses_new_interval(self):
        self.runtime.configure({"sources": {"inspection": True, "issues": False}}, "p")
        self.runtime.schedule("p", self.runtime.config("p"), self.now)
        stale = self.runtime.config("p")
        self.runtime.configure({"schedule": {"light_minutes": 240}}, "p")
        self.runtime.schedule("p", stale, self.now + 3600)
        self.assertEqual(self.due("light"), self.now + 14400)
        self.runtime.schedule("p", stale, self.now + 14400)
        self.assertEqual(self.due("light"), self.now + 28800)

    def test_stale_snapshot_cannot_reenable_disabled_or_unconfigured_sources(self):
        for scope in ["project", "global", "inherit", "repository"]:
            with self.subTest(scope=scope):
                self.store.execute("DELETE FROM tasks")
                self.store.execute("DELETE FROM schedules")
                self.store.execute("UPDATE projects SET config='{}' WHERE id='p'")
                self.store.put_setting("config", {})
                self.runtime.configure({"github": {"repository": "owner/project"}}, "p")
                if scope == "inherit":
                    self.runtime.configure({"sources": {"inspection": False, "issues": False}})
                    self.runtime.configure({"sources": {"inspection": True, "issues": True}}, "p")
                stale = self.runtime.config("p")
                if scope == "repository":
                    self.runtime.configure({"github": {"repository": ""}, "sources": {"inspection": False}}, "p")
                elif scope == "inherit":
                    self.runtime.configure({}, "p", ["sources.inspection", "sources.issues"])
                else:
                    self.runtime.configure({"sources": {"inspection": False, "issues": False}},
                                           None if scope == "global" else "p")
                self.runtime.schedule("p", stale, self.now)
                self.assertEqual(self.store.query("SELECT * FROM schedules"), [])
                self.assertEqual(self.store.query("SELECT * FROM tasks"), [])

    def test_next_schedule_is_first_eligible_instant_across_gaps_and_folds(self):
        cases = [
            ("missing-window", "America/New_York", [2, 3], "2026-03-08T00:00:00-05:00", "2026-03-09T02:00:00-04:00"),
            ("previous-day", "America/New_York", [2, 3], "2026-03-07T03:00:00-05:00", "2026-03-09T02:00:00-04:00"),
            ("gap-opening", "America/New_York", [2, 4], "2026-03-08T00:00:00-05:00", "2026-03-08T03:00:00-04:00"),
            ("half-hour-gap", "Australia/Lord_Howe", [2, 3], "2026-10-04T00:00:00+10:30", "2026-10-04T02:30:00+11:00"),
            ("half-hour-fold", "Australia/Lord_Howe", [2, 3], "2026-04-05T01:45:00+10:30", "2026-04-05T02:00:00+10:30"),
            ("fold-first", "America/New_York", [1, 2], "2026-11-01T00:30:00-04:00", "2026-11-01T01:00:00-04:00"),
            ("fold-second", "America/New_York", [1, 2], "2026-11-01T01:30:00-05:00", "2026-11-01T01:30:00-05:00"),
            ("fold-reentry", "Antarctica/Troll", [1, 2], "2026-10-25T02:30:00+02:00", "2026-10-25T01:00:00+00:00"),
            ("overnight", "Asia/Shanghai", [9, 17], "2026-12-31T17:00:00+08:00", "2027-01-01T09:00:00+08:00"),
            ("all-day", "Asia/Shanghai", [0, 24], "2026-12-31T23:59:59+08:00", "2026-12-31T23:59:59+08:00"),
            ("skipped-date", "Pacific/Apia", [9, 17], "2011-12-29T17:00:00-10:00", "2011-12-31T09:00:00+14:00"),
        ]
        for label, name, hours, value, expected in cases:
            with self.subTest(case=label):
                self.runtime.configure({"schedule": {"timezone": name, "working_hours": hours}}, "p")
                due = datetime.datetime.fromisoformat(value).timestamp()
                want = datetime.datetime.fromisoformat(expected).timestamp()
                actual = self.runtime.next_effective_schedule("p", due, now=due)
                self.assertEqual(actual, want)
                local = datetime.datetime.fromtimestamp(actual, ZoneInfo(name))
                self.assertTrue(hours[0] <= local.hour < hours[1])
                self.assertGreaterEqual(actual, due)
                if actual > due:
                    previous = datetime.datetime.fromtimestamp(actual - 1, ZoneInfo(name))
                    self.assertFalse(hours[0] <= previous.hour < hours[1])

    def test_past_due_and_fractional_seconds_preserve_now_inside_repeated_hour(self):
        self.runtime.configure({"schedule": {"timezone": "America/New_York", "working_hours": [1, 2]}}, "p")
        due = datetime.datetime.fromisoformat("2026-11-01T00:00:00-04:00").timestamp()
        now = datetime.datetime.fromisoformat("2026-11-01T01:30:00.123456-05:00").timestamp()
        self.assertEqual(self.runtime.next_effective_schedule("p", due, now), now)


class TaskScheduleEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = Runtime(Store(self.root / "state"), FakeAgent())
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.project = self.runtime.add_project(fixture(self.root / "project"))
        self.runtime.configure({"sources": {"inspection": False, "issues": False},
                                "evolution": {"enabled": False}}, self.project)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.tmp.cleanup()

    def headers(self):
        return {"Cookie": self.server.cookie + "=" + self.server.token}

    def request(self, path, body):
        return urllib.request.Request(
            self.server.origin + path, json.dumps(body).encode(),
            {**self.headers(), "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
             "Content-Type": "application/json"})

    def post(self, path, body):
        with urllib.request.urlopen(self.request(path, body)) as response:
            return json.load(response)

    def post_error(self, path, body):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.request(path, body))
        try:
            payload = json.load(denied.exception)
        finally:
            denied.exception.close()
        self.assertEqual(denied.exception.code, 400)
        return payload

    def queued_task(self, text="Later work"):
        return self.runtime.goal(self.project, text)

    def test_nonexistent_gap_time_is_rejected_without_touching_the_task(self):
        task = self.queued_task()
        error = self.post_error("/api/task/schedule-time",
                                {"task": task, "local_time": "2027-03-14T02:30:00", "timezone": "America/New_York"})
        self.assertEqual(error["error"], "Scheduled local time is nonexistent")
        self.assertEqual(error["error_code"], "validation_error")
        self.assertEqual(self.runtime.store.one("SELECT available FROM tasks WHERE id=?", (task,))["available"], 0)
        self.assertIsNone(self.runtime.store.setting("task-schedule:" + task))

    def test_ambiguous_repeated_hour_requires_an_explicit_fold(self):
        task = self.queued_task()
        error = self.post_error("/api/task/schedule-time",
                                {"task": task, "local_time": "2027-11-07T01:30:00", "timezone": "America/New_York"})
        self.assertEqual(error["error"], "Scheduled local time is ambiguous; specify fold")
        first = self.post("/api/task/schedule-time",
                          {"task": task, "local_time": "2027-11-07T01:30:00", "timezone": "America/New_York",
                           "fold": 0})
        second = self.post("/api/task/schedule-time",
                           {"task": task, "local_time": "2027-11-07T01:30:00", "timezone": "America/New_York",
                            "fold": 1})
        self.assertEqual(second["at"] - first["at"], 3600)
        self.assertEqual(self.runtime.store.setting("task-schedule:" + task)["fold"], 1)
        self.assertEqual(self.runtime.store.one("SELECT available FROM tasks WHERE id=?", (task,))["available"],
                         second["at"])

    def test_malformed_requests_receive_deterministic_guidance(self):
        task = self.queued_task()
        cases = [
            ({"task": task, "local_time": "2027-01-01T00:00:00+02:00", "timezone": "UTC"},
             "Use a naive local time and fold 0 or 1"),
            ({"task": task, "local_time": "2027-01-01T00:00:00", "timezone": "UTC", "fold": 2},
             "Use a naive local time and fold 0 or 1"),
            ({"task": task, "local_time": "2027-01-01T00:00:00", "timezone": "Mars/Olympus"},
             "Unknown time zone"),
            ({"task": task},
             "Use a naive local time and fold 0 or 1"),
            ({"local_time": "2027-01-01T00:00:00", "timezone": "UTC"}, "Only queued inactive tasks can be scheduled"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                error = self.post_error("/api/task/schedule-time", body)
                self.assertEqual(error["error"], message)
                self.assertEqual(error["error_code"], "validation_error")

    def test_past_times_are_rejected(self):
        task = self.queued_task()
        error = self.post_error("/api/task/schedule-time",
                                {"task": task, "local_time": "2020-01-01T00:00:00", "timezone": "UTC"})
        self.assertEqual(error["error"], "Scheduled time must be in the future")

    def test_active_or_missing_tasks_are_rejected(self):
        self.queued_task("Reserved work")
        missing = self.post_error("/api/task/schedule-time",
                                  {"task": "no-such-task", "local_time": "2027-01-01T00:00:00", "timezone": "UTC"})
        self.assertEqual(missing["error"], "Only queued inactive tasks can be scheduled")
        self.runtime.pause(self.project, False)
        claimed = self.runtime.store.claim(self.project, self.runtime.config(self.project))
        active = self.post_error("/api/task/schedule-time",
                                 {"task": claimed["task"], "local_time": "2027-01-01T00:00:00", "timezone": "UTC"})
        self.assertEqual(active["error"], "Only queued inactive tasks can be scheduled")

    def test_state_reports_the_reservation_with_its_absolute_instant(self):
        task = self.queued_task("Reserved work")
        other = self.queued_task("Unreserved work")
        result = self.post("/api/task/schedule-time",
                           {"task": task, "local_time": "2027-11-07T01:30:00", "timezone": "America/New_York",
                            "fold": 1})
        with urllib.request.urlopen(urllib.request.Request(
                self.server.origin + "/api/state", headers=self.headers())) as response:
            state = json.load(response)
        rows = {row["id"]: row for row in state["tasks"]}
        self.assertEqual(rows[task]["schedule"]["at"], result["at"])
        self.assertEqual(rows[task]["schedule"]["timezone"], "America/New_York")
        self.assertEqual(rows[task]["schedule"]["local_time"], "2027-11-07T01:30:00")
        self.assertNotIn("schedule", rows[other])

    def test_schedule_endpoint_requires_a_session(self):
        request = urllib.request.Request(self.server.origin + "/api/task/schedule-time", data=b"{}",
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        try:
            self.assertEqual(denied.exception.code, 401)
        finally:
            denied.exception.close()

    def test_revise_task_clears_reservation_setting_and_removes_from_state(self):
        task = self.queued_task("Task to revise")
        result = self.post("/api/task/schedule-time",
                           {"task": task, "local_time": "2027-11-07T01:30:00", "timezone": "America/New_York",
                            "fold": 1})
        self.assertIsNotNone(self.runtime.store.setting("task-schedule:" + task))
        with urllib.request.urlopen(urllib.request.Request(
                self.server.origin + "/api/state", headers=self.headers())) as response:
            state = json.load(response)
        rows = {row["id"]: row for row in state["tasks"]}
        self.assertEqual(rows[task]["schedule"]["at"], result["at"])

        self.runtime.revise_task(task, "Extra requirement", reason="user_supplement")

        self.assertIsNone(self.runtime.store.setting("task-schedule:" + task))
        self.assertEqual(self.runtime.store.one("SELECT available FROM tasks WHERE id=?", (task,))["available"], 0)
        with urllib.request.urlopen(urllib.request.Request(
                self.server.origin + "/api/state", headers=self.headers())) as response:
            state = json.load(response)
        rows = {row["id"]: row for row in state["tasks"]}
        self.assertNotIn("schedule", rows[task])

    def test_stale_reservation_setting_is_ignored_by_state_endpoint(self):
        task_zero = self.queued_task("Zero available task")
        self.assertEqual(self.runtime.store.one("SELECT available FROM tasks WHERE id=?", (task_zero,))["available"], 0)
        stale_data = {"local_time": "2027-11-07T01:30:00", "timezone": "America/New_York", "fold": 1, "at": 1825656600.0}
        self.runtime.store.put_setting("task-schedule:" + task_zero, stale_data)
        self.assertIsNotNone(self.runtime.store.setting("task-schedule:" + task_zero))

        task_done = self.queued_task("Completed task")
        self.runtime.store.execute("UPDATE tasks SET state='completed', available=1825656600.0 WHERE id=?", (task_done,))
        self.runtime.store.put_setting("task-schedule:" + task_done, stale_data)
        self.assertIsNotNone(self.runtime.store.setting("task-schedule:" + task_done))

        with urllib.request.urlopen(urllib.request.Request(
                self.server.origin + "/api/state", headers=self.headers())) as response:
            state = json.load(response)
        rows = {row["id"]: row for row in state["tasks"]}
        self.assertNotIn("schedule", rows[task_zero])
        self.assertNotIn("schedule", rows[task_done])


if __name__ == "__main__":
    unittest.main()
