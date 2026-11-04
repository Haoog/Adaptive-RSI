import concurrent.futures
import contextlib
import datetime
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import maintenance as maintenance_module
from adaptive_rsi import pipeline
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.config import BUILTIN_PROVIDERS, DEFAULTS, merge
from adaptive_rsi.delivery import schedule as schedule_delivery
from adaptive_rsi.gitops import git
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


def lock_held(lock):
    """RLock 在 Python 3.14 之前没有公开的 locked()；_is_owned() 只对持锁线程返回 True。

    追踪回调里抛异常会被 sqlite 吞掉，断言值就会一直停在哨兵上，所以这里必须先
    选一条两个版本都成立的取状态路径。
    """
    if hasattr(lock, "locked"):
        return lock.locked()
    return lock._is_owned()


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

    def iteration(self, join_maintenance=True, complete_runs=True):
        test = self

        class Pool:
            def submit(self, fn, run):
                test.dispatched.append(run)
                if complete_runs:
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

    def test_save_between_eligibility_and_claim_refreshes_snapshot_and_invoke(self):
        self.runtime.agent = CodexAgent(self.store)
        provider = {"id": "fixture_api", "name": "Fixture", "base_url": "https://example.invalid/v1",
                    "protocol": "chat_completions", "env_key": "", "credential": "none"}
        active_task = self.task("broken")
        active = self.store.claim("broken", self.runtime.config("broken"))
        healthy = self.task("healthy")
        dispatch = self.runtime.dispatch

        def save_then_dispatch(eligible):
            self.assertIn("healthy", [p["id"] for p, _ in eligible])
            self.runtime.configure({"providers": [provider],
                                    "models": {"implementation": {"provider": "fixture_api", "model": "new-planner",
                                                                    "effort": "default"}},
                                    "limits": {"command_seconds": 7200}})
            self.runtime.configure({"limits": {"max_output_mb": 3}}, "healthy")
            self.assertFalse(self.store.one("SELECT 1 FROM runs WHERE project='healthy'"))
            dispatch(eligible)

        with patch.object(self.runtime, "dispatch", side_effect=save_then_dispatch):
            self.iteration(complete_runs=False)

        run = next(r for r in self.dispatched if r["task"] == healthy)
        config = json.loads(run["config"])
        self.assertEqual(config["models"]["implementation"]["model"], "new-planner")
        self.assertEqual(config["limits"]["command_seconds"], 7200)
        self.assertEqual(config["limits"]["max_output_mb"], 3)
        self.assertEqual(self.store.one("SELECT config FROM runs WHERE id=?", (active["id"],))["config"], active["config"])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (active_task,))["state"], "planning")
        # 真实 invoke 路由与调用记录，只替代远端响应，不执行模型或网络。
        self.runtime.stop.clear()
        response = {"choices": [{"message": {"tool_calls": [{"id": "finish-1", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"summary": "fixture"})}}]}}]}
        with patch("adaptive_rsi.native.post_json", return_value=response) as post:
            self.assertEqual(self.runtime.invoke("implementation", "fixture", self.root, run), {"summary": "fixture"})
        self.assertEqual(post.call_args.args[1]["model"], "new-planner")
        self.assertEqual(self.store.one("SELECT model FROM calls WHERE run=?", (run["id"],))["model"], "new-planner")

    def test_dispatch_rechecks_hours_timezone_pause_weight_and_concurrency(self):
        # 两个时区的固定时刻：UTC 在新窗口内，上海在新窗口外。
        instant = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.timezone.utc)
        for change in ["hours", "timezone", "pause", "weight", "concurrency"]:
            with self.subTest(change=change):
                self.runtime.futures.clear()
                self.dispatched.clear()
                for project in ["broken", "healthy"]:
                    self.runtime.pause(project, False)
                    self.runtime.configure({"schedule": {"timezone": "UTC", "working_hours": [0, 24], "weight": 1}}, project)
                self.runtime.configure({"limits": {"concurrency": 2}})
                self.task("broken")
                self.task("healthy")
                dispatch = self.runtime.dispatch

                def save_then_dispatch(eligible):
                    if change == "hours":
                        self.runtime.configure({"schedule": {"working_hours": [0, 1]}}, "broken")
                    elif change == "timezone":
                        self.runtime.configure({"schedule": {"timezone": "Asia/Shanghai", "working_hours": [12, 13]}}, "broken")
                    elif change == "pause":
                        self.runtime.pause("broken", True)
                    elif change == "weight":
                        self.runtime.configure({"schedule": {"weight": 4}}, "broken")
                    else:
                        self.runtime.configure({"limits": {"concurrency": 1}})
                    dispatch(eligible)

                self.store.put_setting("fair-service", {})
                self.store.put_setting("fair-waiting", {})
                self.store.put_setting("fair-clock", 0)
                with patch("adaptive_rsi.runtime.datetime.datetime") as clock, \
                        patch.object(self.runtime, "dispatch", side_effect=save_then_dispatch):
                    clock.now.side_effect = lambda zone: instant.astimezone(zone)
                    self.iteration()
                projects = [r["project"] for r in self.dispatched]
                if change in ["hours", "timezone", "pause"]:
                    self.assertEqual(projects, ["healthy"])
                    self.assertFalse(self.store.one("SELECT 1 FROM runs WHERE project='broken'"))
                elif change == "concurrency":
                    self.assertEqual(len(projects), 1)
                else:
                    self.assertEqual(set(projects), {"broken", "healthy"})
                    self.assertEqual(self.store.setting("fair-service")["broken"], 0.25)
                self.store.execute("DELETE FROM phase_times")
                self.store.execute("DELETE FROM runs")
                self.store.execute("DELETE FROM tasks")

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
        # 确定性拒绝留 settings 证据与事件，不记 scheduler_error、不周期重试。
        mark = self.store.setting("retention_path_rejected:" + bad["id"])
        self.assertEqual(mark["path"], str(outside))
        event = self.store.one("SELECT * FROM events WHERE kind='retention_path_rejected' AND run=?", (bad["id"],))
        self.assertEqual(event["project"], "broken")
        self.assertEqual(self.errors("maintenance_run"), [])
        self.store.put_setting("maintenance-last", 0)
        self.iteration()
        self.assertEqual(self.errors("maintenance_run"), [])

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
        # 目录名与运行一致但父目录不在 runs/ 下，覆盖「越界拒绝」分支。
        outside_named = self.historical_run("broken", "awaiting_approval")
        self.store.execute("UPDATE runs SET path=? WHERE id=?",
                           (str(outside / outside_named["id"]), outside_named["id"]))
        bad_runs.append(outside_named)
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
                self.assertTrue(self.store.setting("retention_path_rejected:" + run["id"]))
        self.assertEqual(self.errors("maintenance_run"), [])
        self.assertEqual({event["run"] for event in self.store.query(
            "SELECT run FROM events WHERE kind='retention_path_rejected'")},
            {run["id"] for run in bad_runs})
        self.assertFalse((Path(good["path"]) / "candidate").exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (good["id"],))["state"], "expired")
        # 被拒路径不再进入候选：下个周期不重新 stat、不再产生事件。
        self.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assertEqual(self.errors("maintenance_run"), [])
        self.assertEqual(len(self.store.query(
            "SELECT run FROM events WHERE kind='retention_path_rejected'")), len(bad_runs))

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

    def test_retention_resolve_oserror_retries_without_rejection_mark(self):
        outside = self.root / "outside"
        outside.mkdir()
        bad = self.historical_run("broken", "awaiting_approval")
        # 目录名与运行一致但父目录不在 runs/ 下：唯有越过名称预检才会触发 resolve()。
        target = outside / bad["id"]
        target.mkdir()
        self.store.execute("UPDATE runs SET path=? WHERE id=?", (str(target), bad["id"]))
        (target / "candidate").mkdir()
        marker = target / "candidate" / "keep.txt"
        marker.write_text("must remain")
        real = Path.resolve

        def flaky(self):
            if self == outside:
                raise OSError("runs root unreadable")
            return real(self)

        with patch.object(Path, "resolve", flaky):
            self.runtime.maintenance()

        self.assertEqual(self.errors("maintenance_run")[0]["run"], bad["id"])
        self.assertIsNone(self.store.setting("retention_path_rejected:" + bad["id"]))
        # 环境恢复后同一运行重新评估并落入确定性拒绝路径。
        self.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assertTrue(self.store.setting("retention_path_rejected:" + bad["id"]))
        self.assertTrue(marker.exists())

    def test_retention_revalidates_rejected_symlink_after_filesystem_repair(self):
        runs = self.store.root / "runs"
        runs.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.historical_run("broken", "awaiting_approval")
        directory = Path(linked["path"])
        directory.symlink_to(outside, target_is_directory=True)

        self.runtime.maintenance()
        mark = self.store.setting("retention_path_rejected:" + linked["id"])
        self.assertEqual(mark["reason"], "unsafe run path")

        # 运维把链接换成同名真实目录：runs.path 未变，重验窗口未到期仍跳过。
        directory.unlink()
        (directory / "candidate").mkdir(parents=True)
        self.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (linked["id"],))["state"],
                         "awaiting_approval")
        self.assertTrue((directory / "candidate").exists())

        # 窗口到期后重新检查：路径已合法，运行回到清理流水线，过期标记被清除。
        self.store.put_setting("retention_path_rejected:" + linked["id"], dict(
            mark, rejected=time.time() - maintenance_module.RETENTION_REJECT_REVALIDATE_SECONDS - 1))
        self.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()

        self.assertFalse((directory / "candidate").exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (linked["id"],))["state"],
                         "expired")
        self.assertIsNone(self.store.setting("retention_path_rejected:" + linked["id"]))

    def test_retention_rejected_path_still_unsafe_is_marked_again(self):
        runs = self.store.root / "runs"
        runs.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.historical_run("broken", "awaiting_approval")
        directory = Path(linked["path"])
        directory.symlink_to(outside, target_is_directory=True)

        self.runtime.maintenance()
        mark = self.store.setting("retention_path_rejected:" + linked["id"])
        first = mark["rejected"]

        # 窗口到期而链接仍在：重写标记顺延窗口，事件每窗口至多一条。
        self.store.put_setting("retention_path_rejected:" + linked["id"], dict(
            mark, rejected=time.time() - maintenance_module.RETENTION_REJECT_REVALIDATE_SECONDS - 1))
        self.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()

        renewed = self.store.setting("retention_path_rejected:" + linked["id"])
        self.assertGreater(renewed["rejected"], first)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (linked["id"],))["state"],
                         "awaiting_approval")
        self.assertTrue(directory.is_symlink())
        events = self.store.query(
            "SELECT run FROM events WHERE kind='retention_path_rejected' AND run=?", (linked["id"],))
        self.assertEqual(len(events), 2)

    def test_automatic_retention_honors_run_and_project_holds(self):
        self.store.execute("INSERT INTO projects VALUES ('held','held project',?, '{}', 0, 9)",
                           (str(self.root / "held"),))
        held_run = self.historical_run("broken", "awaiting_approval")
        held_project = self.historical_run("held", "awaiting_approval")
        corrupt = self.historical_run("healthy", "awaiting_approval")
        unheld = self.historical_run("healthy", "awaiting_approval")
        cases = {"run": held_run, "project": held_project, "corrupt": corrupt}
        markers = {}
        for run in [*cases.values(), unheld]:
            directory = Path(run["path"]) / "workspace"
            directory.mkdir(parents=True)
            markers[run["id"]] = directory / "keep.txt"
            markers[run["id"]].write_text("retained evidence")
        self.store.put_setting("legal-hold:" + held_run["id"], True)
        self.store.put_setting("legal-hold-project:held", True)
        self.store.execute("INSERT INTO settings VALUES (?,?)",
                           ("legal-hold:" + corrupt["id"], "not-json"))

        self.runtime.maintenance()

        for label, run in cases.items():
            with self.subTest(hold=label):
                self.assertEqual(markers[run["id"]].read_text(), "retained evidence")
                self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"],
                                 "awaiting_approval")
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (run["task"],))["state"],
                                 "awaiting_approval")
        self.assertFalse((Path(unheld["path"]) / "workspace").exists())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (unheld["id"],))["state"], "expired")

    def test_retention_hold_committed_after_the_precheck_blocks_every_deletion(self):
        run = self.historical_run("healthy", "awaiting_approval")
        directory = Path(run["path"])
        for name in ("workspace", "candidate"):
            (directory / name).mkdir(parents=True)
            (directory / name / "keep.txt").write_text("retained evidence")
        real = maintenance_module.retention_hold
        prechecks = []

        def stale_precheck(store, run_id, project):
            # 预检读到无 hold 之后、删除事务取得写锁之前，另一连接提交 hold。
            if not prechecks:
                prechecks.append(run_id)
                store.put_setting("legal-hold:" + run_id, True)
                return None
            return real(store, run_id, project)

        with patch.object(maintenance_module, "retention_hold", side_effect=stale_precheck):
            self.runtime.maintenance()

        for name in ("workspace", "candidate"):
            self.assertEqual((directory / name / "keep.txt").read_text(), "retained evidence")
        self.assertTrue(self.store.setting("legal-hold:" + run["id"]))
        self.assertEqual(self.store.one("SELECT path FROM runs WHERE id=?", (run["id"],))["path"], str(directory))
        self.assertEqual(self.errors("maintenance_run"), [])

    def test_hold_written_while_an_artifact_is_deleted_waits_for_that_deletion(self):
        run = self.historical_run("healthy", "awaiting_approval")
        directory = Path(run["path"])
        (directory / "workspace").mkdir(parents=True)
        (directory / "workspace" / "keep.txt").write_text("retained evidence")
        deleting, attempted, written = threading.Event(), threading.Event(), threading.Event()
        outcome = {}
        real = maintenance_module._remove_artifact

        def observed_removal(parent, name):
            deleting.set()
            self.assertTrue(attempted.wait(5), "hold writer never started")
            # 写线程已经发出 hold 写入；它必须阻塞在本删除事务的写锁之后。
            outcome["committed_during_deletion"] = written.wait(0.5)
            real(parent, name)

        def write_hold():
            try:
                self.assertTrue(deleting.wait(5), "deletion never started")
                attempted.set()
                self.store.put_setting("legal-hold:" + run["id"], True)
            except BaseException as error:
                outcome["error"] = error
            finally:
                written.set()

        writer = threading.Thread(target=write_hold, name="hold-writer")
        writer.start()
        try:
            with patch.object(maintenance_module, "_remove_artifact", side_effect=observed_removal):
                self.runtime.maintenance()
        finally:
            writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertNotIn("error", outcome)
        self.assertIs(outcome.get("committed_during_deletion"), False,
                      "hold committed before the in-flight deletion finished")
        self.assertTrue(self.store.setting("legal-hold:" + run["id"]))
        self.assertFalse((directory / "workspace").exists())

    def test_automatic_retention_removes_read_only_prepared_dependencies(self):
        source = self.root / "project"
        source.mkdir()
        git(source, "init", "-q")
        (source / "deps").mkdir()
        (source / "deps" / "fixture").write_text("Frozen dependency")
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("must remain")
        outside.chmod(0o555)
        config = merge(DEFAULTS, {"environment": {"seed_paths": ["deps"]},
                                  "checks": [{"name": "fixture", "argv": ["python3", "--version"]}]})
        task = self.task("healthy")
        run = self.store.claim("healthy", config)
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True)
        with patch("adaptive_rsi.evaluator.host_tool_versions", return_value={"python": "fixture-only"}):
            pipeline._prepare_environment(self.runtime, run, {"id": "healthy", "path": str(source)},
                                          config, directory, directory / "workspace", config["checks"])
        dependencies = directory / "dependencies"
        # 复现 B05 前提：真实准备入口把依赖副本冻结为只读。
        self.assertFalse(os.access(dependencies / "deps", os.W_OK))
        # 只读树内的链接必须在清理时只被解除；不得沿链接修改或删除外部目标。
        (dependencies / "deps").chmod(0o755)
        (dependencies / "deps" / "link").symlink_to(outside, target_is_directory=True)
        (dependencies / "deps").chmod(0o555)
        self.store.transition(run["id"], "completed", {}, True)
        old = time.time() - 400 * 86400
        self.store.execute("UPDATE runs SET path=?,started=?,ended=? WHERE id=?",
                           (str(directory), old, old, run["id"]))

        self.runtime.maintenance()

        try:
            self.assertFalse(dependencies.exists())
            self.assertFalse((directory / "workspace").exists())
            self.assertEqual(self.errors("maintenance_run"), [])
            # 只读依赖内的符号链接只被解除，未被沿链接修改或删除外部目标。
            self.assertEqual(sentinel.read_text(), "must remain")
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o555)
        finally:
            outside.chmod(0o755)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "completed")

    def test_automatic_retention_clears_purged_run_path_and_skips_it_next_pass(self):
        purged = self.historical_run("healthy", "completed")
        held = self.historical_run("healthy", "awaiting_approval")
        for run in (purged, held):
            (Path(run["path"]) / "workspace").mkdir(parents=True)
            (Path(run["path"]) / "workspace" / "evidence.txt").write_text("run evidence")
        self.store.put_setting("legal-hold:" + held["id"], True)

        self.runtime.maintenance()

        self.assertFalse((Path(purged["path"]) / "workspace").exists())
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id=?", (purged["id"],))["path"])
        # 受 hold 保护而未被清理的运行保留路径，下一轮仍可重新评估。
        self.assertEqual(self.store.one("SELECT path FROM runs WHERE id=?", (held["id"],))["path"],
                         held["path"])
        # 已解绑路径的运行不再进入下一轮清理循环：不获取 dispatch_lock、不 stat 目录。
        self.store.put_setting("maintenance-last", 0)
        with patch.object(maintenance_module, "_remove_run_artifacts") as remove:
            self.runtime.maintenance()
        remove.assert_not_called()

    def test_retained_patch_can_be_reverted_after_run_path_is_cleared(self):
        from tests.test_runtime import FakeAgent, fixture, CHECKS
        repo = fixture(self.root / 'healthy')
        self.runtime.agent = FakeAgent()
        self.runtime.configure({'checks': CHECKS, 'evolution': {'enabled': False}}, 'healthy')
        prior = self.historical_run('healthy', 'merged')
        directory = Path(prior['path'])
        directory.mkdir(parents=True)
        (repo / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
        (directory / 'candidate.patch').write_text(git(repo, 'diff', '--binary') + '\n')
        git(repo, 'add', '.')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'fixed')
        self.store.execute('UPDATE runs SET result=? WHERE id=?',
                           (json.dumps({'files': ['calculator.py'], 'monitoring_completed': True}), prior['id']))
        self.runtime.maintenance()
        self.assertIsNone(self.store.one('SELECT path FROM runs WHERE id=?', (prior['id'],))['path'])
        self.assertTrue((directory / 'candidate.patch').is_file())
        task = self.runtime.revert_code(prior['id'])
        run = self.store.claim('healthy', self.runtime.config('healthy'))
        with patch('adaptive_rsi.readiness.inspect', return_value={'ready': True, 'items': []}), \
                patch.object(pipeline, '_prepare_environment', return_value=({'sha256': 'fixture'}, directory / 'dependencies')), \
                patch.object(self.runtime.evaluator, 'evaluate', return_value={'decision': 'pass', 'checks': {}}):
            self.runtime.process(run)
        result = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(result['state'], 'awaiting_approval', result['result'])
        self.assertEqual((Path(result['path']) / 'candidate/calculator.py').read_text(),
                         'def add(a, b):\n    return a - b\n')
        self.assertEqual(run['task'], task)

    def test_undo_cleanup_rebinds_run_path_cleared_by_retention(self):
        run = self.historical_run("healthy", "completed")
        directory = Path(run["path"])
        (directory / "workspace").mkdir(parents=True)
        (directory / "workspace" / "evidence.txt").write_text("restored evidence")
        receipt = maintenance_module.recycle_cleanup(
            self.store, maintenance_module.preview_cleanup(self.store, run["id"]))
        # 保留周期清理已解绑该运行后再撤销回收：恢复的工件必须重新挂钩，
        # 否则后续保留策略与手动清理预览都会跳过它。
        self.store.execute("UPDATE runs SET path=NULL WHERE id=?", (run["id"],))

        maintenance_module.undo_cleanup(self.store, receipt["id"])

        self.assertEqual((directory / "workspace" / "evidence.txt").read_text(), "restored evidence")
        self.assertEqual(self.store.one("SELECT path FROM runs WHERE id=?", (run["id"],))["path"],
                         str(directory))

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

    def test_failed_maintenance_records_error_and_skips_immediate_retry(self):
        attempts = []

        def failing(rt):
            attempts.append(threading.current_thread())
            raise OSError("maintenance storage unavailable")

        with patch.object(Runtime, "maintenance", failing):
            self.runtime.maintenance_async()
            worker = self.runtime.maintenance_thread
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.runtime.maintenance_lock.locked())
        event, = self.store.query("SELECT * FROM events WHERE kind='maintenance_failed'")
        self.assertIn("OSError: maintenance storage unavailable", event["message"])
        self.assertEqual(json.loads(event["data"])["error_type"], "OSError")
        # 崩溃后不得在下一 tick 重新拉起线程：进程内退避与持久化记录都要生效。
        with patch.object(Runtime, "maintenance", failing):
            self.runtime.maintenance_async()
        self.assertIs(self.runtime.maintenance_thread, worker)
        self.runtime.maintenance_retry_at = 0
        with patch.object(Runtime, "maintenance", failing):
            self.runtime.maintenance_async()
        self.assertIs(self.runtime.maintenance_thread, worker)
        self.assertEqual(len(attempts), 1)

    def test_maintenance_retries_after_backoff_window(self):
        with patch.object(Runtime, "maintenance",
                          side_effect=OSError("maintenance storage unavailable")):
            self.runtime.maintenance_async()
            first = self.runtime.maintenance_thread
            first.join(5)
        self.assertFalse(first.is_alive())
        record = self.store.setting("maintenance-retry")
        self.assertEqual(record["failures"], 1)
        self.assertGreater(record["retry_at"], time.time())
        # 退避到期（显式拨回过去）后允许再次触发，成功后清除退避记录。
        self.store.put_setting("maintenance-retry", dict(record, retry_at=time.time() - 1))
        self.runtime.maintenance_retry_at = 0
        with patch.object(Runtime, "maintenance") as maintenance:
            self.runtime.maintenance_async()
            retried = self.runtime.maintenance_thread
        self.assertIsNot(retried, first)
        retried.join(5)
        self.assertFalse(retried.is_alive())
        maintenance.assert_called_once_with()
        self.assertEqual(self.store.setting("maintenance-retry", {}), {})

    def test_model_center_cleanup_retains_a_provider_enabled_after_scan(self):
        provider = {'id': 'rebound', 'name': 'Rebound', 'protocol': 'chat_completions',
                    'base_url': 'https://example.test/v1', 'env_key': ''}
        key = self.runtime.model_center.key(provider, None)
        self.store.put_setting(key, {'fetched': time.time() - 400 * 86400})
        query = self.store.query
        changed = []
        def scan(sql, args=()):
            rows = query(sql, args)
            if sql.startswith('SELECT key,value FROM settings WHERE key LIKE') and 'model-center:' in sql:
                self.runtime.configure({'providers': [provider]})
                changed.append(True)
            return rows
        with patch.object(self.store, 'query', side_effect=scan):
            self.runtime.maintenance()
        self.assertEqual(changed, [True])
        self.assertIsNotNone(self.store.setting(key))

    def test_model_center_cleanup_releases_dispatch_lock_before_batch_delete(self):
        stale = time.time() - 400 * 86400
        kept = self.runtime.model_center.key(BUILTIN_PROVIDERS[0], None)
        self.store.put_setting(kept, {"fetched": stale, "models": []})
        doomed = ["model-center:global:retired:%032x" % index for index in range(200)]
        for key in doomed:
            self.store.put_setting(key, {"fetched": stale, "models": []})
        observed = {"scan_locked": None, "deletes_locked": 0, "connections": set(),
                    "concurrent_acquire": None, "armed": False}
        borrows = iter(range(1 << 20))
        original = self.store.connect

        @contextlib.contextmanager
        def traced():
            with original() as db:
                borrow = next(borrows)

                def trace(sql):
                    normalized = " ".join(sql.split()).upper()
                    if not observed["armed"]:
                        if "MODEL-CENTER:%" in normalized:
                            observed["armed"] = True
                            observed["scan_locked"] = lock_held(self.runtime.dispatch_lock)
                        return
                    if not normalized.startswith("DELETE FROM SETTINGS"):
                        return
                    observed["connections"].add(borrow)
                    if lock_held(self.runtime.dispatch_lock):
                        observed["deletes_locked"] += 1
                    if observed["concurrent_acquire"] is None:
                        observed["concurrent_acquire"] = self.acquire_dispatch_lock_elsewhere()

                db.set_trace_callback(trace)
                try:
                    yield db
                finally:
                    db.set_trace_callback(None)

        with patch.object(self.store, "connect", traced):
            self.runtime.maintenance()
        # 4000 行候选扫描与批量删除都不在 dispatch_lock 内。
        self.assertIs(observed["scan_locked"], False)
        self.assertEqual(observed["deletes_locked"], 0)
        # 删除期间其他线程仍能取得调度锁。
        self.assertTrue(observed["concurrent_acquire"])
        # 批量删除只借还一次连接，而不是每行一次自动提交事务。
        self.assertEqual(len(observed["connections"]), 1)
        remaining = [row["key"] for row in self.store.query(
            "SELECT key FROM settings WHERE key LIKE 'model-center:%'")]
        self.assertEqual(remaining, [kept])

    def acquire_dispatch_lock_elsewhere(self, timeout=2):
        acquired = []

        def worker():
            if self.runtime.dispatch_lock.acquire(timeout=timeout):
                acquired.append(True)
                self.runtime.dispatch_lock.release()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout + 1)
        return bool(acquired)

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
