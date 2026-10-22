import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.delivery import schedule as schedule_delivery
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class SchedulerIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, object())
        for created, project in enumerate(["broken", "healthy", "paused"]):
            self.store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                               (project, project, str(self.root / project), "{}", project == "paused", created))
        self.runtime.configure({"schedule": {"working_hours": [0, 24]},
                                "sources": {"inspection": False, "issues": False}})
        self.dispatched = []

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def task(self, project):
        return self.store.add_task(project, "goal", str(time.time_ns()), "local regression")

    def historical_run(self, project, state="completed", path=None):
        self.task(project)
        run = self.store.claim(project, self.runtime.config(project))
        self.store.transition(run["id"], state, {}, True)
        old = time.time() - 400 * 86400
        directory = path or self.store.root / "runs" / run["id"]
        self.store.execute("UPDATE runs SET path=?,started=?,ended=? WHERE id=?",
                           (str(directory), old, old, run["id"]))
        return self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))

    def iteration(self):
        test = self

        class Pool:
            def submit(self, fn, run):
                test.dispatched.append(run)
                test.store.transition(run["id"], "completed", {}, True)
                future = concurrent.futures.Future()
                future.set_result(None)
                return future

        self.runtime.stop.clear()
        with patch.object(self.runtime.updates, "tick"), \
                patch.object(self.runtime.updates, "is_busy", return_value=False), \
                patch.object(self.runtime.wake, "wait", side_effect=lambda timeout: self.runtime.stop.set()), \
                patch.object(self.runtime, "pool", Pool()):
            self.runtime.loop()

    def errors(self, phase):
        return [row for row in self.store.query("SELECT * FROM events WHERE kind='scheduler_error'")
                if json.loads(row["data"]).get("phase") == phase]

    def test_invalid_project_configuration_does_not_block_healthy_work_or_unpause_projects(self):
        broken = self.task("broken")
        healthy = self.task("healthy")
        paused = self.task("paused")
        self.store.execute("UPDATE projects SET config='{' WHERE id='broken'")

        self.iteration()

        self.assertEqual([run["task"] for run in self.dispatched], [healthy])
        for task in [broken, paused]:
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
        self.assertEqual(self.store.one("SELECT paused FROM projects WHERE id='paused'")["paused"], 1)
        self.assertEqual(self.errors("project")[0]["project"], "broken")

    def test_project_schedule_failure_does_not_block_healthy_project(self):
        self.store.put_setting("maintenance-last", time.time())
        healthy = self.task("healthy")
        schedule = self.runtime.schedule

        def project_schedule(project, config):
            if project == "broken":
                raise OSError("project schedule unavailable")
            return schedule(project, config)

        with patch.object(self.runtime, "schedule", side_effect=project_schedule):
            self.iteration()

        self.assertEqual([run["task"] for run in self.dispatched], [healthy])
        self.assertEqual(self.errors("project")[0]["project"], "broken")

    def test_failed_background_stages_do_not_block_dispatch(self):
        for phase, target in [("maintenance", "adaptive_rsi.runtime.Runtime.maintenance"),
                              ("delivery", "adaptive_rsi.delivery.schedule")]:
            with self.subTest(phase=phase):
                self.runtime.futures.clear()
                healthy = self.task("healthy")
                with patch(target, side_effect=OSError("background unavailable")):
                    self.iteration()
                self.assertIn(healthy, [run["task"] for run in self.dispatched])
                self.assertEqual(len(self.errors(phase)), 1)

    def test_bad_retention_path_is_reported_without_deleting_outside_or_blocking_cleanup(self):
        outside = self.root / "outside"
        (outside / "candidate").mkdir(parents=True)
        marker = outside / "candidate" / "keep.txt"
        marker.write_text("must remain")
        bad = self.historical_run("broken", "awaiting_approval", outside)
        good = self.historical_run("broken", "awaiting_approval")
        (Path(good["path"]) / "candidate").mkdir(parents=True)
        healthy = self.historical_run("healthy", "awaiting_approval")
        (Path(healthy["path"]) / "candidate").mkdir(parents=True)
        healthy_task = self.task("healthy")

        self.iteration()

        self.assertTrue(marker.exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (bad["id"],))["state"], "awaiting_approval")
        for run in [good, healthy]:
            self.assertFalse((Path(run["path"]) / "candidate").exists())
            self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "expired")
        self.assertEqual(self.dispatched[-1]["task"], healthy_task)
        event = self.errors("maintenance_run")[0]
        self.assertEqual((event["project"], event["run"]), ("broken", bad["id"]))

    def test_invalid_maintenance_project_does_not_block_other_project_cleanup(self):
        run = self.historical_run("healthy", "awaiting_approval")
        (Path(run["path"]) / "candidate").mkdir(parents=True)
        self.store.execute("UPDATE projects SET config='{' WHERE id='broken'")

        self.runtime.maintenance()

        self.assertFalse((Path(run["path"]) / "candidate").exists())
        self.assertEqual(self.errors("maintenance_project")[0]["project"], "broken")

    def test_invalid_delivery_run_does_not_block_other_tracking_jobs(self):
        bad = self.historical_run("broken", "merged")
        healthy = self.historical_run("healthy", "pr_open")
        self.store.execute("UPDATE runs SET result='{' WHERE id=?", (bad["id"],))

        schedule_delivery(self.runtime)

        jobs = self.store.query("SELECT project FROM tasks WHERE source='system_delivery'")
        self.assertEqual(jobs, [{"project": healthy["project"]}])
        event = self.errors("delivery_run")[0]
        self.assertEqual((event["project"], event["run"]), ("broken", bad["id"]))

    def test_repeated_project_errors_are_suppressed_until_recovery(self):
        self.store.put_setting("maintenance-last", time.time())
        self.store.execute("UPDATE projects SET config='{' WHERE id='broken'")
        self.iteration()
        self.iteration()
        self.assertEqual(len(self.errors("project")), 1)

        self.store.execute("UPDATE projects SET config='{}' WHERE id='broken'")
        self.iteration()
        self.store.execute("UPDATE projects SET config='{' WHERE id='broken'")
        self.iteration()
        self.assertEqual(len(self.errors("project")), 2)

    def test_repeated_run_error_is_not_cleared_by_successful_outer_stage(self):
        run = self.historical_run("broken", "merged")
        self.store.execute("UPDATE runs SET result='{' WHERE id=?", (run["id"],))
        now = time.time()
        schedule_delivery(self.runtime, now)
        schedule_delivery(self.runtime, now + 61)
        self.assertEqual(len(self.errors("delivery_run")), 1)

        self.store.execute("UPDATE runs SET result='{}' WHERE id=?", (run["id"],))
        schedule_delivery(self.runtime, now + 122)
        self.store.execute("UPDATE runs SET result='{' WHERE id=?", (run["id"],))
        schedule_delivery(self.runtime, now + 183)
        self.assertEqual(len(self.errors("delivery_run")), 2)


if __name__ == "__main__":
    unittest.main()
