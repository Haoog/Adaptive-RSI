"""Endpoint-level contracts for reserving a task at an explicit wall time."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


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
        self.assertEqual(denied.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
