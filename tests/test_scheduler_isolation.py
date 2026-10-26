import concurrent.futures
import json
import tempfile
import threading
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

    def iteration(self, join_maintenance=True):
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
        if join_maintenance and self.runtime.maintenance_thread:
            # 清理已移入后台线程；默认等待其收尾，让后续断言保持确定性。
            self.runtime.maintenance_thread.join(5)

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

    def test_retention_rejects_symlinked_run_and_malicious_paths(self):
        runs = self.store.root / "runs"
        runs.mkdir()
        outside = self.root / "outside"
        (outside / "candidate").mkdir(parents=True)
        marker = outside / "candidate" / "keep.txt"
        marker.write_text("must remain")
        sibling = self.historical_run("healthy", "awaiting_approval")
        sibling_marker = Path(sibling["path"]) / "candidate" / "keep.txt"
        sibling_marker.parent.mkdir(parents=True)
        sibling_marker.write_text("another run must remain")
        linked = self.historical_run("broken", "awaiting_approval")
        Path(linked["path"]).symlink_to(outside, target_is_directory=True)
        malicious_paths = [
            runs / ".." / ".." / "outside",
            Path(sibling["path"]),
            runs,
            runs / "nested" / "run",
            Path("relative-run"),
        ]
        bad_runs = [linked]
        for path in malicious_paths:
            bad_runs.append(self.historical_run("broken", "awaiting_approval", path))
        # 单独保留健康运行，确认拒绝坏路径不会阻塞正常清理。
        good = self.historical_run("broken", "awaiting_approval")
        (Path(good["path"]) / "candidate").mkdir(parents=True)
        self.store.execute("UPDATE runs SET ended=? WHERE id=?", (time.time(), sibling["id"]))

        self.runtime.maintenance()

        self.assertEqual(marker.read_text(), "must remain")
        self.assertEqual(sibling_marker.read_text(), "another run must remain")
        self.assertTrue(Path(linked["path"]).is_symlink())
        for run in bad_runs:
            with self.subTest(path=run["path"]):
                self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "awaiting_approval")
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (run["task"],))["state"], "awaiting_approval")
        self.assertEqual({event["run"] for event in self.errors("maintenance_run")},
                         {run["id"] for run in bad_runs})
        self.assertFalse((Path(good["path"]) / "candidate").exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (good["id"],))["state"], "expired")

    def test_retention_rejects_symlinked_runs_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.store.root / "runs").symlink_to(outside, target_is_directory=True)
        run = self.historical_run("broken", "awaiting_approval")
        marker = outside / run["id"] / "candidate" / "keep.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("must remain")

        self.runtime.maintenance()

        self.assertEqual(marker.read_text(), "must remain")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "awaiting_approval")
        self.assertEqual(self.errors("maintenance_run")[0]["run"], run["id"])

    def test_invalid_maintenance_project_does_not_block_other_project_cleanup(self):
        run = self.historical_run("healthy", "awaiting_approval")
        (Path(run["path"]) / "candidate").mkdir(parents=True)
        self.store.execute("UPDATE projects SET config='{' WHERE id='broken'")

        self.runtime.maintenance()

        self.assertFalse((Path(run["path"]) / "candidate").exists())
        self.assertEqual(self.errors("maintenance_project")[0]["project"], "broken")

    def test_maintenance_cleanup_runs_in_background_without_blocking_dispatch(self):
        run = self.historical_run("healthy", "awaiting_approval")
        (Path(run["path"]) / "candidate").mkdir(parents=True)
        healthy_task = self.task("healthy")
        release = threading.Event()
        started = threading.Event()
        real = Runtime.maintenance

        def slow_maintenance(rt):
            started.set()
            if not release.wait(5):
                raise AssertionError("maintenance worker was not released in time")
            real(rt)

        with patch.object(Runtime, "maintenance", slow_maintenance):
            self.iteration(join_maintenance=False)
            # 维护线程被阻塞在慢清理里时，调度循环已完成本轮派发，未被 rmtree 拖住。
            self.assertIn(healthy_task, [item["task"] for item in self.dispatched])
            self.assertTrue(started.wait(5))
            release.set()
        self.runtime.maintenance_thread.join(5)
        self.assertFalse(self.runtime.maintenance_thread.is_alive())
        # 后台清理仍然真实发生。
        self.assertFalse((Path(run["path"]) / "candidate").exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "expired")

    def test_maintenance_worker_does_not_reenter_while_previous_cleanup_runs(self):
        entered = []
        release = threading.Event()

        def slow_maintenance(rt):
            entered.append(threading.current_thread())
            self.assertTrue(release.wait(5))

        with patch.object(Runtime, "maintenance", slow_maintenance):
            self.runtime.maintenance_async()
            first = self.runtime.maintenance_thread
            self.runtime.maintenance_async()
            # 上一轮清理未结束时，重入请求被直接跳过。
            self.assertIs(self.runtime.maintenance_thread, first)
            release.set()
            first.join(5)
            # 上一轮结束后，下一轮调度仍能再次启动清理。
            self.runtime.maintenance_async()
            self.assertIsNot(self.runtime.maintenance_thread, first)
            release.set()
            self.runtime.maintenance_thread.join(5)
        self.assertEqual(len(entered), 2)

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
