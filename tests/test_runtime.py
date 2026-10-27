import concurrent.futures
import fcntl
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.adapters import detect
from adaptive_rsi.agent import CodexAgent, REVIEW_AREAS, provider_arguments
from adaptive_rsi.config import DEFAULTS, compatibility, fingerprint, merge, validate
from adaptive_rsi.evaluator import Evaluator, empty_test_run
from adaptive_rsi.evolution import Evolution
from adaptive_rsi.gitops import git, snapshot
from adaptive_rsi.process import _PROBE_CACHE, _PROBE_INFLIGHT, Sandbox, _cached_probe, checked, run_process
from adaptive_rsi.review import enforce_review, risk_review_required
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Handler, Server
from adaptive_rsi.store import Store, encode, uid


CHECKS = [{"name": "real unittest regression", "cwd": ".", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]}]


def fixture(root):
    root.mkdir(exist_ok=True)
    checked(["git", "init", "--quiet", str(root)])
    (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests").mkdir()
    (root / "tests/test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass Regression(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.0.1"\n')
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "fixture")
    return root


class FakeAgent:
    """Only the model is substituted; Git isolation and evaluation remain real."""
    @staticmethod
    def available(provider):
        return True

    def __init__(self, malicious=False):
        self.roles = []
        self.malicious = malicious

    def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        self.roles.append((role, config["models"][role]))
        if role == "planning":
            return {"decision": "ready", "reason": "Verified arithmetic defect", "scope": ["calculator.py", "tests"],
                    "acceptance": ["2 + 3 equals 5"], "risk": "low", "value": {
                        "impact": "medium", "effort": "low", "confidence": "high", "benefit": "Restore arithmetic correctness",
                        "cost": "One operator change and regression verification", "inaction": "Addition continues returning wrong values",
                        "evidence": ["calculator.py:2 subtracts; 2 + 3 returns -1"]}}
        if role == "acceptance":
            return {"files": [{"path": ".rsi-acceptance/check.py", "content": "from calculator import add\nassert add(2, 3) == 5\n"}],
                    "checks": [{"name": "independent addition", "argv": ["python3", "-c", "exec(open('.rsi-acceptance/check.py').read())"], "criteria": [0]}]}
        if role == "implementation":
            (workspace / "tests/test_addition_regression.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class AddedRegression(unittest.TestCase):\n"
                "    def test_addition(self):\n        self.assertEqual(add(2, 3), 5)\n")
            if self.malicious:
                (workspace / "tests/test_calculator.py").write_text("import unittest\nclass Fake(unittest.TestCase):\n    def test_fake(self): pass\n")
            else:
                (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
            return {"summary": "Corrected addition"}
        if role == "review":
            return {"decision": "pass", "reason": "Narrow change meets acceptance", "findings": [],
                    "assessments": [{"area": area, "decision": "pass", "reason": "Checked staged change and surrounding code"} for area in REVIEW_AREAS]}
        if role == "analysis":
            return {"findings": [{"title": "Addition subtracts", "body": "add(2, 3) returns -1",
                                  "path": "calculator.py", "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 60}]}
        if role == "evolution":
            return {"kind": "strategy", "guidance": "Reproduce arithmetic boundary cases before editing", "triggers": ["add"], "rationale": "Observed subtraction error"}
        raise AssertionError(role)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="adaptive-rsi-test-")
        self.root = Path(self.tmp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "controller")
        self.agent = FakeAgent()
        self.runtime = Runtime(self.store, self.agent)
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "sources": {"inspection": False, "issues": False},
                                "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def claim(self, text="Fix addition"):
        task = self.runtime.goal(self.project, text)
        self.runtime.pause(self.project, False)
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_green_tests_cannot_bypass_blocking_maintainability_review(self):
        class ContradictoryReviewer(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "review":
                    result["findings"] = [{"severity": "medium", "area": "maintainability",
                        "path": "calculator.py", "line": 2, "title": "Duplicated business logic",
                        "reason": "New implementation diverges from shared arithmetic behavior",
                        "suggestion": "Use the existing shared implementation"}]
                return result
        self.runtime.agent = ContradictoryReviewer()
        task, run = self.claim()
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        result = json.loads(row["result"])
        self.assertEqual(row["state"], "failed", result)
        self.assertEqual(result["evaluation"]["decision"], "pass")
        self.assertEqual(result["review"]["reported_decision"], "pass")
        self.assertEqual(result["review"]["decision"], "fail")
        self.assertTrue((Path(row["path"]) / "review.json").exists())
        self.assertNotIn("pr", result)

    def test_settings_precedence_and_frozen_run(self):
        self.runtime.configure({"schedule": {"light_minutes": 240}})
        self.runtime.configure({"schedule": {"light_minutes": 360}}, self.project)
        task = self.runtime.goal(self.project, "Fix addition", {"schedule": {"light_minutes": 480}})
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.configure({"schedule": {"light_minutes": 600}}, self.project)
        self.assertEqual(json.loads(run["config"])["schedule"]["light_minutes"], 480)
        self.assertEqual(self.runtime.config(self.project)["schedule"]["light_minutes"], 600)

    def test_scan_inheritance_removes_only_requested_project_overrides(self):
        self.runtime.configure({"schedule": {"light_minutes": 360, "deep_minutes": 10080},
                                "delivery": {"mode": "pr"}}, self.project)
        self.runtime.configure({}, self.project, ["schedule.light_minutes", "sources.inspection"])
        self.runtime.configure({"schedule": {"light_minutes": 720}})
        config = self.runtime.config(self.project)
        self.assertEqual(config["schedule"]["light_minutes"], 720)
        self.assertEqual(config["schedule"]["deep_minutes"], 10080)
        self.assertTrue(config["sources"]["inspection"])
        self.assertFalse(config["sources"]["issues"])
        self.assertEqual(config["delivery"]["mode"], "pr")
        for project, paths, values in [(None, ["schedule.light_minutes"], {}),
                                       (self.project, ["delivery.auto_merge"], {}),
                                       (self.project, "schedule.light_minutes", {}),
                                       (self.project, ["schedule.light_minutes"], {"schedule": {"light_minutes": 5}})]:
            with self.assertRaises(ValueError):
                self.runtime.configure(values, project, paths)

    def test_global_schedule_change_preserves_overridden_and_unrelated_deadlines(self):
        self.runtime.configure({"sources": {"inspection": True, "issues": True},
                                "schedule": {"light_minutes": 360},
                                "github": {"repository": "owner/project"}}, self.project)
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=100)
        before = {row["kind"]: row["due"] for row in self.store.query("SELECT * FROM schedules")}
        self.runtime.configure({"schedule": {"light_minutes": 720, "issues_minutes": 240}})
        after = {row["kind"]: row["due"] for row in self.store.query("SELECT * FROM schedules")}
        self.assertEqual(after["light"], before["light"])
        self.assertNotIn("deep", after)
        self.assertGreater(after["issues"], before["issues"])

    def test_claim_is_atomic_and_project_locked(self):
        self.runtime.goal(self.project, "one")
        self.runtime.goal(self.project, "two")
        self.runtime.pause(self.project, False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: self.store.claim(self.project, self.runtime.config(self.project)), range(4)))
        self.assertEqual(sum(r is not None for r in runs), 1)

    def test_resume_coalesces_missed_intervals(self):
        self.runtime.configure({"sources": {"inspection": True}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.schedule(self.project, config, now=100)
        self.runtime.schedule(self.project, config, now=1000000)
        self.runtime.schedule(self.project, config, now=1000000)
        rows = self.store.query("SELECT * FROM tasks WHERE source LIKE 'system_%'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.store.query("SELECT * FROM schedules")), 1)

    def test_recovery_retains_artifacts_and_requires_fresh_retry(self):
        task, run = self.claim()
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "interrupted")
        self.runtime.retry(task)
        new = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertNotEqual(new["id"], run["id"])

    def test_start_failure_releases_the_state_directory_lock(self):
        with patch.object(self.runtime.updates, "restore_startup", side_effect=RuntimeError("startup failed")):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                self.runtime.start()
        self.assertIsNone(self.runtime.lockfile)
        # 锁已随异常路径关闭释放：新的文件描述符可以立即取得排它锁。
        probe = (self.store.root / "runtime.lock").open("a+")
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            probe.close()

    def test_active_strategies_fetches_evidence_without_extra_candidate_queries(self):
        key = compatibility(self.runtime.config(self.project))
        for candidate, evidence in [("a", {"compatibility": key}), ("b", {"compatibility": "other"}),
                                    ("c", {"compatibility": None})]:
            self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", None, "stable",
                                encode({"guidance": "Reproduce arithmetic boundary cases"}), encode(evidence), time.time()))
        original_query, original_one = self.store.query, self.store.one
        statements = []

        def record_query(sql, *args, **kwargs):
            statements.append(sql)
            return original_query(sql, *args, **kwargs)

        def record_one(sql, *args, **kwargs):
            statements.append(sql)
            return original_one(sql, *args, **kwargs)

        with patch.object(self.store, "query", side_effect=record_query), \
                patch.object(self.store, "one", side_effect=record_one):
            strategies = self.runtime.active_strategies(self.project)
        # evidence 随首查取出，不再对每个 candidate 追加查询；兼容性过滤行为不变。
        self.assertEqual([s["id"] for s in strategies], ["a", "c"])
        self.assertEqual(strategies[0]["content"]["guidance"], "Reproduce arithmetic boundary cases")
        self.assertEqual([sql for sql in statements if "FROM candidates" in sql],
                         ["SELECT id,kind,content,evidence FROM candidates WHERE project=? AND status='stable'"])

    def test_inspection_reads_each_finding_path_only_once(self):
        findings = {"findings": [
            {"title": "Subtraction defect", "body": "add(2, 3) returns -1", "path": "calculator.py",
             "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 60},
            {"title": "Signature drift", "body": "Function signature lacks type hints", "path": "calculator.py",
             "quote": "def add(a, b):", "acceptance": ["typing preserved"], "priority": 60}]}

        def invoke(role, *args, **kwargs):
            if role == "analysis":
                return findings
            if role == "triage":
                return {"findings": [{"index": 0, "decision": "ready", "confidence": "high", "reason": "Concrete defect"},
                                     {"index": 1, "decision": "ready", "confidence": "high", "reason": "Concrete defect"}]}
            raise AssertionError(role)

        reads = []
        original_read = Path.read_text
        target = str(self.repo / "calculator.py")

        def counting_read(path_self, *args, **kwargs):
            if str(path_self) == target:
                reads.append(str(path_self))
            return original_read(path_self, *args, **kwargs)

        with patch.object(self.runtime, "invoke", side_effect=invoke), \
                patch.object(Path, "read_text", counting_read):
            self.runtime.inspect(self.runtime.project(self.project), self.repo, "light",
                                 {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        # 两条 finding 指向同一文件：全文只读取一次，引文包含检查复用缓存。
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source='inspection'")), 2)

    def test_batch_triage_decisions_land_in_task_decisions_and_stay_visible(self):
        findings = {"findings": [
            {"title": "Weak evidence duplicate", "body": "add(2, 3) returns -1", "path": "calculator.py",
             "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 40},
            {"title": "Comment style churn", "body": "Switch comments to docstrings", "path": "tests/test_calculator.py",
             "quote": "def test_add(self):", "acceptance": ["style unified"], "priority": 40}]}
        reasons = ["Evidence is weak: the quoted line does not match the report.",
                   "Cosmetic churn with disproportionate cost."]

        def invoke(role, *args, **kwargs):
            if role == "analysis":
                return findings
            if role == "triage":
                return {"findings": [
                    {"index": 0, "decision": "deferred", "confidence": "high", "reason": reasons[0]},
                    {"index": 1, "decision": "rejected", "confidence": "high", "reason": reasons[1]}]}
            raise AssertionError(role)

        with patch.object(self.runtime, "invoke", side_effect=invoke):
            self.runtime.inspect(self.runtime.project(self.project), self.repo, "light",
                                 {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        base = git(self.repo, "rev-parse", "HEAD").strip()
        deferred = self.store.one("SELECT * FROM tasks WHERE title='Weak evidence duplicate'")
        rejected = self.store.one("SELECT * FROM tasks WHERE title='Comment style churn'")
        self.assertEqual(deferred["state"], "deferred")
        self.assertEqual(rejected["state"], "rejected")
        # 批量初筛的决定与理由直接进入 task_decisions：/api/task 与暂缓复核共用这份记录，
        # 没有运行记录的不采纳/暂缓任务也能在列表与详情看到理由。
        records = {}
        for task, decision, reason in [(deferred, "deferred", reasons[0]), (rejected, "rejected", reasons[1])]:
            row = self.store.one("SELECT data FROM task_decisions WHERE task=?", (task["id"],))
            self.assertIsNotNone(row, decision + " decision must be recorded for the task detail")
            payload = json.loads(row["data"])
            records[decision] = payload
            self.assertEqual(payload["decision"], decision)
            self.assertEqual(payload["reason"], reason)
            self.assertEqual(payload["base"], base)
            self.assertTrue(payload.get("triage_input"))
        # 暂缓记录带唤醒条件；不采纳不承诺自动唤醒，只能手动重新考虑。
        self.assertEqual(records["deferred"].get("wake_condition"), "inputs_policy_or_relevant_baseline_change")
        self.assertNotIn("wake_condition", records["rejected"])
        # 决定落库后不再依赖内部 triage-state 设置键。
        self.assertEqual(self.store.query("SELECT key FROM settings WHERE key LIKE 'triage-state:%'"), [])
        # 保留期清理不删除决定记录：理由在 90 天后仍然可见。
        stale = time.time() - 120 * 86400
        self.store.execute("UPDATE tasks SET updated=? WHERE id IN (?,?)", (stale, deferred["id"], rejected["id"]))
        self.runtime.maintenance()
        for task in [deferred, rejected]:
            self.assertIsNotNone(self.store.one("SELECT data FROM task_decisions WHERE task=?", (task["id"],)))
        # 唤醒语义不变：基线变化后暂缓任务重新入队，不采纳任务保持原状。
        (self.repo / "notes.txt").write_text("baseline moved\n")
        git(self.repo, "add", "notes.txt")
        git(self.repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
            "commit", "--quiet", "-m", "Move baseline")
        self.runtime.reassess_deferred(self.project, self.runtime.config(self.project),
                                       git(self.repo, "rev-parse", "HEAD").strip())
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (deferred["id"],))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (rejected["id"],))["state"], "rejected")

    def test_calls_are_recorded_without_daily_quotas(self):
        config = merge(self.runtime.config(self.project), {"limits": {"daily_calls": 1}})
        route = config["models"]["planning"]
        self.store.reserve_call(self.project, "run1", "planning", route, config)
        self.store.reserve_call(self.project, "run2", "planning", route, config)
        self.assertEqual(len(self.store.query("SELECT * FROM calls")), 2)

    def test_read_only_registration_does_not_enable_project(self):
        self.assertEqual(self.runtime.project(self.project)["paused"], 1)
        self.runtime.goal(self.project, "a goal")
        self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))

    def test_duplicate_inspection_and_unchanged_scan_no_model_call(self):
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "light")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        self.runtime.request(self.project, "light")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        self.assertEqual(sum(r[0] == "analysis" for r in self.agent.roles), 1)
        self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source='inspection'")), 1)

    def test_rediscovered_failure_requeues_instead_of_being_suppressed(self):
        from adaptive_rsi.discovery import finding_identity
        finding = {"title": "Addition subtracts", "body": "add(2, 3) returns -1",
                   "path": "calculator.py", "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 60}
        task = self.runtime.goal(self.project, "Addition subtracts")
        self.store.execute("UPDATE tasks SET fingerprint=?,source='inspection',state='failed',attempts=2 WHERE id=?",
                           (finding_identity(finding), task))
        with patch.object(self.runtime, "invoke", return_value={"findings": [finding]}):
            self.runtime.inspect(self.runtime.project(self.project), self.repo, "light",
                                 {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(json.loads(row["evidence"])[0]["source"], "rediscovered")

    def test_rejected_and_cancelled_findings_stay_suppressed(self):
        from adaptive_rsi.discovery import finding_identity
        finding = {"title": "Addition subtracts", "body": "add(2, 3) returns -1",
                   "path": "calculator.py", "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 60}
        # obsolete 是规划得出的结论而非失败：同样不被重新发现，但可手动重试或补充需求。
        for state in ["rejected", "cancelled", "obsolete"]:
            with self.subTest(state=state):
                title = finding["title"] + " " + state
                key = finding_identity({**finding, "title": title})
                task = self.runtime.goal(self.project, title)
                self.store.execute("UPDATE tasks SET fingerprint=?,source='inspection',state=?,evidence=? WHERE id=?",
                                   (key, state, encode([{"path": finding["path"], "quote": finding["quote"]}]), task))
                before = len(self.store.query("SELECT id FROM tasks WHERE source NOT LIKE 'system_%'"))
                with patch.object(self.runtime, "invoke", return_value={"findings": [{**finding, "title": title}]}):
                    self.runtime.inspect(self.runtime.project(self.project), self.repo, "light",
                                         {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], state)
                # 没有为同一条发现创建新任务。
                self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source NOT LIKE 'system_%'")), before)
        # 被判 obsolete 的目标仍有出路：手动重试与补充需求都可用。
        obsolete_task = self.runtime.goal(self.project, "Judged obsolete")
        self.store.execute("UPDATE tasks SET state='obsolete' WHERE id=?", (obsolete_task,))
        self.runtime.retry(obsolete_task)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (obsolete_task,))["state"], "queued")
        self.store.execute("UPDATE tasks SET state='obsolete' WHERE id=?", (obsolete_task,))
        self.runtime.revise_task(obsolete_task, "New evidence arrived")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (obsolete_task,))["state"], "queued")

    def test_failing_system_scan_gets_circuit_breaker_backoff(self):
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "light")
        task = self.store.one("SELECT id FROM tasks WHERE fingerprint='system:light'")["id"]
        for round_index in range(3):
            self.store.execute("UPDATE tasks SET state='queued',available=0 WHERE id=?", (task,))
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.store.transition(run["id"], "blocked", {"reason": "gh not authenticated"}, True)
            # 退避期内 request 不会重新入队，避免每周期产生新的失败 run。
            self.runtime.request(self.project, "light")
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "blocked")
        self.store.execute("UPDATE runs SET ended=ended-86400 WHERE task=?", (task,))
        self.runtime.request(self.project, "light")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_manual_retry_can_pull_a_backing_off_task_forward(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.store.execute("UPDATE tasks SET state='queued',available=?,infra_retries=0 WHERE id=?",
                           (time.time() + 3600, task))
        self.runtime.retry(task)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual((row["state"], row["available"]), ("queued", 0))
        # 手动重试清零基础设施退避计数：之后的瞬时故障仍可自动重试。
        self.store.execute("UPDATE tasks SET state='blocked',infra_retries=3 WHERE id=?", (task,))
        self.runtime.retry(task)
        self.assertEqual(self.store.one("SELECT infra_retries FROM tasks WHERE id=?", (task,))["infra_retries"], 0)
        # 普通排队任务（没有未来 available）不能提前重试，避免界面出现无操作入口。
        self.store.execute("UPDATE tasks SET state='queued',available=0 WHERE id=?", (task,))
        with self.assertRaisesRegex(ValueError, "This task cannot be retried"):
            self.runtime.retry(task)

    def test_cancel_cannot_rewrite_terminal_states(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.cancel_task(task)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "cancelled")
        other = self.runtime.goal(self.project, "Second")
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (other,))
        with self.assertRaises(ValueError):
            self.runtime.cancel_task(other)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (other,))["state"], "merged")

    def test_dead_dependency_releases_the_waiting_task(self):
        first = self.runtime.goal(self.project, "First")
        dependent = self.runtime.goal(self.project, "Dependent")
        self.runtime.set_dependency(dependent, first)
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (first,))
        self.runtime.release_dead_dependencies()
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (dependent,))
        self.assertEqual(row["state"], "blocked")
        self.assertIsNone(self.store.one("SELECT dependency FROM task_dependencies WHERE task=?", (dependent,)))
        # 解除等待后手动重试即可独立运行。
        self.runtime.retry(dependent)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (dependent,))["state"], "queued")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_real_git_and_sandbox_end_to_end_without_user_tree_changes(self):
        (self.repo / "notes.txt").write_text("uncommitted user work")
        task, run = self.claim()
        self.runtime.process(run)
        result = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(result["state"], "awaiting_approval", result["result"])
        evaluation = json.loads(result["result"])["evaluation"]
        self.assertFalse(evaluation["checks"]["baseline"][0]["passed"])
        self.assertTrue(evaluation["checks"]["candidate"][0]["passed"])
        self.assertEqual((self.repo / "notes.txt").read_text(), "uncommitted user work")
        self.assertIn("a - b", (self.repo / "calculator.py").read_text())
        self.assertEqual([r[0] for r in self.agent.roles], ["planning", "implementation", "review"])
        self.assertEqual(self.agent.roles[1][1]["model"], "gpt-6-luna")
        self.assertEqual(self.agent.roles[1][1]["effort"], "max")
        evaluation = json.loads(result["result"])["evaluation"]
        self.assertEqual(evaluation["improvement"]["decision"], "pass")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_fake_agent_success_cannot_replace_original_tests(self):
        self.runtime.agent = FakeAgent(malicious=True)
        task, run = self.claim()
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "blocked", row["result"])
        self.assertIn("Test-first behavior changes may only add a regression test", json.loads(row["result"])["reason"])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_dependency_seed_is_frozen_for_both_checks_and_preserves_original(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor/value.txt").write_text("sealed dependency")
        with (self.repo / "tests/test_calculator.py").open("a") as stream:
            stream.write("    def test_dependency(self):\n        from pathlib import Path\n        self.assertEqual(Path('vendor/value.txt').read_text(), 'sealed dependency')\n")
        git(self.repo, "add", "tests")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "dependency check")
        class SeedEditingAgent(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["scope"].append("vendor")
                if role == "implementation":
                    (Path(args[1]) / "vendor/value.txt").write_text("candidate attempted dependency change")
                return result
        self.runtime.agent = SeedEditingAgent()
        self.runtime.configure({"environment": {"seed_paths": ["vendor"]}}, self.project)
        task, run = self.claim()
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "awaiting_approval", row["result"])
        self.assertEqual((self.repo / "vendor/value.txt").read_text(), "sealed dependency")
        self.assertEqual((Path(row["path"]) / "evaluation/candidate/vendor/value.txt").read_text(), "sealed dependency")

    def test_acceptance_baseline_runs_in_parallel_with_implementation(self):
        class ParallelImplementation(FakeAgent):
            acceptance_started = threading.Event()
            def invoke(self, role, prompt, workspace, *args, **kwargs):
                if role == "implementation":
                    self_test.assertTrue(self.acceptance_started.wait(3),
                                        "acceptance baseline should start before implementation finishes")
                result = super().invoke(role, prompt, workspace, *args, **kwargs)
                if role == "planning":
                    result["risk"] = "high"
                return result
        self_test = self
        agent = ParallelImplementation()
        self.runtime.agent = agent
        def check(_sandbox, check, workspace, *args, **kwargs):
            label = Path(workspace).name
            if label == "acceptance_baseline":
                agent.acceptance_started.set()
            fail = (label == "baseline" and check["name"] == "real unittest regression") or \
                   (label == "acceptance_baseline" and check["name"] == "independent addition")
            return {"argv": check["argv"], "name": check["name"], "exit_code": 1 if fail else 0,
                    "stdout": "Ran 1 test\n", "stderr": "AssertionError" if fail else "",
                    "seconds": 0.01, "error": None}
        with patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            task, run = self.claim()
            self.runtime.process(run)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")

    def test_acceptance_baseline_failure_aggregates_without_running_candidate_checks(self):
        class HighRisk(FakeAgent):
            def invoke(self, role, prompt, workspace, *args, **kwargs):
                result = super().invoke(role, prompt, workspace, *args, **kwargs)
                if role == "planning":
                    result["risk"] = "high"
                return result
        self.runtime.agent = HighRisk()
        self.runtime.configure({"environment": {"setup": [
            {"name": "prepare acceptance", "cwd": ".", "argv": ["true"]}]}}, self.project)
        def check(_sandbox, check, workspace, *args, **kwargs):
            label = Path(workspace).name
            failed = label == "acceptance_baseline" and check["name"] == "prepare acceptance"
            return {"argv": check["argv"], "name": check["name"], "exit_code": 1 if failed else 0,
                    "stdout": "", "stderr": "acceptance setup failed" if failed else "",
                    "seconds": 0.01, "error": None}
        with patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            task, run = self.claim()
            self.runtime.process(run)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual(result["decision"], "insufficient_evidence")
        self.assertEqual(result["reason"], "Independent acceptance environment preparation failed")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "insufficient_evidence")
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("implementation"), 1)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_sandbox_hides_controller_prevents_network_and_harness_writes(self):
        workspace = self.store.root / "runs" / "probe"
        workspace.mkdir(parents=True)
        secret = self.store.root / "holdout.txt"
        secret.write_text("protected")
        testfile = workspace / "fixed-test.txt"
        testfile.write_text("original")
        script = """import pathlib,socket
def denied(fn):
 try: fn()
 except OSError: return
 raise RuntimeError('isolation bypassed')
denied(lambda: pathlib.Path(%r).read_text())
denied(lambda: pathlib.Path(%r).write_text('tampered'))
denied(lambda: socket.create_connection(('127.0.0.1', 9), timeout=1))
print('isolated')
""" % (str(secret), str(testfile))
        check = {"name": "isolation", "argv": ["python3", "-c", script]}
        result = Sandbox(self.store.root, self.runtime.config(self.project)).check(check, workspace, protected=[testfile])
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(testfile.read_text(), "original")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_read_only_model_cannot_mutate_sealed_candidate(self):
        workspace = self.store.root / "runs" / "sealed-probe"
        output = workspace / ".rsi-agent"
        output.mkdir(parents=True)
        codefile = workspace / "code.py"
        codefile.write_text("original")
        script = """from pathlib import Path
try: Path(%r).write_text('tamper')
except PermissionError: pass
else: raise RuntimeError('readonly bypassed')
Path(%r).write_text('allowed output')
""" % (str(codefile), str(output / "result.txt"))
        args = Sandbox(self.store.root, self.runtime.config(self.project)).worker(
            ["python3", "-c", script], workspace, read_only=True, output_dir=output)
        result = run_process(args, cwd=workspace)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(codefile.read_text(), "original")

    def test_missing_checks_never_passes(self):
        evaluation = self.runtime.evaluator.evaluate(self.repo, self.repo, [], self.root / "eval", DEFAULTS)
        self.assertEqual(evaluation["decision"], "insufficient_evidence")

    def test_snapshot_excludes_future_fixes_and_git_objects(self):
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "hidden future solution")
        future = git(self.repo, "rev-parse", "HEAD")
        copy = snapshot(self.repo, self.root / "shallow-snapshot", base)
        self.assertEqual(git(copy, "rev-list", "--count", "HEAD"), "1")
        self.assertIn("a - b", (copy / "calculator.py").read_text())
        with self.assertRaises(RuntimeError):
            git(copy, "cat-file", "-e", future)

    def test_empty_suite_keeps_proposal_out_of_stable_strategy(self):
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "evolve")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        candidates = self.store.query("SELECT * FROM candidates")
        self.assertEqual(candidates[0]["status"], "insufficient_evidence")
        self.assertEqual(self.runtime.active_strategies(self.project), [])

    def test_exhausted_holdout_exposure_gets_an_actionable_reason(self):
        self.runtime.configure({"evolution": {"enabled": True, "min_cases_per_split": 3,
                                              "max_holdout_uses": 3}}, self.project)
        now = time.time()
        for index, split in enumerate(["validation"] * 3 + ["holdout"] * 3):
            self.store.execute("INSERT INTO cases(id,project,family,split,spec,uses,created) "
                               "VALUES (?,?,?,?,?,?,?)",
                               ("case-" + str(index), self.project, "family-" + str(index), split, "{}", 3, now))
        project = self.runtime.project(self.project)
        result = Evolution(self.runtime).propose_and_evaluate(
            project, self.root / "workspace", self.root / "experiments",
            {"id": "exhausted-run", "config": encode(self.runtime.config(self.project))})
        reason = "Validation or holdout exposure limit reached; import new cases or raise evolution.max_holdout_uses"
        self.assertEqual(result["reason"], reason)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(json.loads(self.store.one("SELECT evidence FROM candidates")["evidence"])["reason"], reason)

    def test_case_families_cannot_leak_between_splits(self):
        manifest = {"family": "addition", "split": "validation", "repository": str(self.repo), "base": "HEAD",
                    "task": "Correct add", "checks": CHECKS, "source": "synthetic"}
        Evolution(self.runtime).import_case(self.project, manifest)
        with self.assertRaisesRegex(ValueError, "family"):
            Evolution(self.runtime).import_case(self.project, {**manifest, "split": "holdout"})
        with self.assertRaisesRegex(ValueError, "family"):
            Evolution(self.runtime).import_case(self.project, {**manifest, "family": "renamed", "split": "holdout"})
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 0)

    def test_rollback_restores_parent_and_invalidates_skill(self):
        for candidate, parent, status in [("a", None, "superseded"), ("b", "a", "stable")]:
            self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,created) VALUES (?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", parent, status, "{}", time.time()))
        self.store.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                           (uid(), self.project, None, "verified", '["add"]', "lesson", '{"candidate":"b"}', time.time()+100, time.time()))
        self.runtime.rollback_strategy("b")
        self.assertEqual(self.runtime.active_strategies(self.project)[0]["id"], "a")
        self.assertEqual(self.runtime.memories(self.project, "add"), [])

    def test_manual_promotion_requires_ready_and_writes_verified_skill(self):
        content = {"guidance": "Reproduce arithmetic boundary cases before editing", "triggers": ["add"]}
        evidence = {"reason": "Repeated paired improvement", "trials": [{"case": "case-a"}]}
        for candidate, parent, status in [("old", None, "stable"), ("ready", "old", "ready"),
                                          ("evaluating", None, "evaluating")]:
            self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", parent, status, encode(content),
                                encode(evidence), time.time()))
        result = self.runtime.promote_strategy("ready")
        self.assertEqual(result, {"candidate": "ready", "status": "stable"})
        promoted = self.store.one("SELECT * FROM candidates WHERE id='ready'")
        self.assertEqual(promoted["status"], "stable")
        self.assertIsNotNone(promoted["promoted"])
        self.assertEqual(json.loads(promoted["evidence"]), evidence, "manual promotion preserves evaluation evidence")
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='old'")["status"], "superseded")
        skill = self.store.one("SELECT * FROM skills")
        config = self.runtime.config(self.project)
        # 自动晋升与手动晋升使用同一种技能行（verified、30 天过期、含候选与配置指纹）。
        expected = Evolution.skill_values(self.project, "ready", content, config, skill["created"])
        self.assertEqual((skill["project"], skill["task"], skill["status"], skill["triggers"],
                          skill["content"], skill["evidence"], skill["expires"], skill["created"]),
                         expected[1:])
        self.assertAlmostEqual(skill["expires"] - skill["created"], 30 * 86400)
        self.assertEqual(json.loads(skill["evidence"]),
                         {"candidate": "ready", "config_hash": fingerprint(config)})
        self.assertEqual(promoted["parent"], "old", "promotion rebinds to the stable it replaced")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["ready"])
        event = self.store.one("SELECT * FROM events WHERE kind='promotion' ORDER BY seq DESC")
        self.assertEqual(json.loads(event["data"]), {"candidate": "ready"})
        with self.assertRaisesRegex(ValueError, "Only a verified ready candidate can be promoted"):
            self.runtime.promote_strategy("ready")
        with self.assertRaisesRegex(ValueError, "Only a verified ready candidate can be promoted"):
            self.runtime.promote_strategy("evaluating")
        with self.assertRaisesRegex(ValueError, "Candidate not found"):
            self.runtime.promote_strategy("missing")
        self.runtime.rollback_strategy("ready")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["old"])
        self.assertEqual(self.store.one("SELECT status FROM skills")["status"], "invalidated")
        self.assertEqual(len(self.store.query("SELECT * FROM skills")), 1)
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='promotion'")), 1)

    def test_manual_promotion_rejects_incompatible_evidence_without_side_effects(self):
        content = {"guidance": "Check arithmetic boundaries", "triggers": ["add"]}
        previous = self.runtime.config(self.project)
        self.runtime.configure({"review": {"block_on": "high"}}, self.project)
        current = self.runtime.config(self.project)
        self.assertNotEqual(compatibility(previous), compatibility(current))
        for kind in ("strategy", "meta_strategy", "adapter"):
            for suffix, status, config in [("old", "stable", current), ("ready", "ready", previous)]:
                candidate = kind + "-" + suffix
                self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                                   "VALUES (?,?,?,?,?,?,?)",
                                   (candidate, self.project, kind, status, encode(content),
                                    encode({"compatibility": compatibility(config)}), time.time()))
            self.store.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                               Evolution.skill_values(self.project, kind + "-old", content, current, time.time()))
        before = {table: self.store.query("SELECT * FROM " + table) for table in ("candidates", "skills", "events")}
        active = self.runtime.active_strategies(self.project)
        memories = self.runtime.memories(self.project, "add")
        self.assertEqual(len(active), 3)
        self.assertTrue(memories)
        for kind in ("strategy", "meta_strategy", "adapter"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "^Candidate evidence is incompatible with current configuration$"):
                    self.runtime.promote_strategy(kind + "-ready")
                for table, rows in before.items():
                    self.assertEqual(self.store.query("SELECT * FROM " + table), rows, table)
                self.assertEqual(self.runtime.active_strategies(self.project), active)
                self.assertEqual(self.runtime.memories(self.project, "add"), memories)

    def test_manual_promotion_uses_active_strategy_compatibility_rules(self):
        previous = self.runtime.config(self.project)
        self.runtime.configure({"schedule": {"light_minutes": 600}}, self.project)
        current = self.runtime.config(self.project)
        self.assertNotEqual(fingerprint(previous), fingerprint(current))
        self.assertEqual(compatibility(previous), compatibility(current))
        for candidate, evidence in [("matching", {"compatibility": compatibility(previous)}),
                                    ("legacy", {}), ("null", {"compatibility": None}),
                                    ("empty", {"compatibility": ""})]:
            with self.subTest(candidate=candidate):
                evidence["config_hash"] = fingerprint(previous)
                self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                                   "VALUES (?,?,?,?,?,?,?)",
                                   (candidate, self.project, "strategy", "ready",
                                    encode({"guidance": "Check arithmetic boundaries", "triggers": ["add"]}),
                                    encode(evidence), time.time()))
                self.assertEqual(self.runtime.promote_strategy(candidate),
                                 {"candidate": candidate, "status": "stable"})
                self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], [candidate])
                self.assertEqual(json.loads(self.store.one("SELECT evidence FROM candidates WHERE id=?",
                                                          (candidate,))["evidence"]), evidence)

    def test_auto_activation_rebinds_parent_and_persists_evidence(self):
        content = {"guidance": "Check arithmetic boundaries", "triggers": ["add"]}
        for candidate, status, created in [("a", "stable", 1), ("auto", "evaluating", 2)]:
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", status, encode(content), "{}", created))
        evidence = {"decision": "pass", "trials": [{"case": "case-a"}]}
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = Evolution.activate(db, "auto", now, evidence)
        self.assertEqual(parent, "a")
        row = self.store.one("SELECT * FROM candidates WHERE id='auto'")
        self.assertEqual((row["status"], row["parent"], row["promoted"]), ("stable", "a", now))
        self.assertEqual(json.loads(row["evidence"]), evidence)
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='a'")["status"], "superseded")

    def test_consecutive_manual_promotions_roll_back_through_previous_stables(self):
        content = {"guidance": "Check arithmetic boundaries", "triggers": ["add"]}
        for candidate, status, created in [("a", "stable", 1), ("r1", "ready", 2), ("r2", "ready", 3)]:
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", status, encode(content), "{}", created))
        self.runtime.promote_strategy("r1")
        self.assertEqual(self.store.one("SELECT parent FROM candidates WHERE id='r1'")["parent"], "a")
        self.runtime.promote_strategy("r2")
        self.assertEqual(self.store.one("SELECT parent FROM candidates WHERE id='r2'")["parent"], "r1")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["r2"])
        self.runtime.rollback_strategy("r2")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["r1"])
        self.runtime.rollback_strategy("r1")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["a"])

    def test_two_manual_promotions_without_prior_stable_roll_back_to_the_first(self):
        content = {"guidance": "Check arithmetic boundaries", "triggers": ["add"]}
        for candidate, created in [("r1", 1), ("r2", 2)]:
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", "ready", encode(content), "{}", created))
        self.runtime.promote_strategy("r1")
        self.assertIsNone(self.store.one("SELECT parent FROM candidates WHERE id='r1'")["parent"])
        self.runtime.promote_strategy("r2")
        self.assertEqual(self.store.one("SELECT parent FROM candidates WHERE id='r2'")["parent"], "r1")
        self.runtime.rollback_strategy("r2")
        self.assertEqual([item["id"] for item in self.runtime.active_strategies(self.project)], ["r1"])
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='r1'")["status"], "stable")

    def test_manual_promotion_rejects_invalid_project_kind_and_content(self):
        self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                           "VALUES ('missing-project','absent','strategy','ready',?,?,?)",
                           (encode({"guidance": "g", "triggers": []}), '{}', time.time()))
        with self.assertRaisesRegex(ValueError, "Project not found"):
            self.runtime.promote_strategy("missing-project")
        self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                           "VALUES ('bad-kind',?,'other','ready',?,?,?)",
                           (self.project, encode({"guidance": "g", "triggers": []}), '{}', time.time()))
        with self.assertRaisesRegex(ValueError, "Unknown candidate kind"):
            self.runtime.promote_strategy("bad-kind")
        self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                           "VALUES ('bad-content',?,'strategy','ready',?,?,?)",
                           (self.project, encode({"guidance": " ", "triggers": []}), '{}', time.time()))
        with self.assertRaisesRegex(ValueError, "Candidate content is incomplete"):
            self.runtime.promote_strategy("bad-content")
        self.assertEqual(self.store.query("SELECT * FROM skills"), [])
        self.assertEqual(self.store.query("SELECT * FROM events WHERE kind='promotion'"), [])

    def test_manual_promotion_is_idempotent_under_concurrent_clicks(self):
        content = {"guidance": "Reproduce arithmetic boundary cases before editing", "triggers": ["add"]}
        self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                           "VALUES ('race',?,'strategy','ready',?,?,?)",
                           (self.project, encode(content), encode({"reason": "verified"}), time.time()))
        barrier = threading.Barrier(4)
        def click():
            barrier.wait()
            try:
                return self.runtime.promote_strategy("race")
            except ValueError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: click(), range(4)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual([result for result in results if isinstance(result, str)],
                         ["Only a verified ready candidate can be promoted"] * 3)
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='race'")["status"], "stable")
        self.assertIsNone(self.store.one("SELECT parent FROM candidates WHERE id='race'")["parent"])
        self.assertEqual(len(self.store.query("SELECT * FROM skills")), 1)
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='promotion'")), 1)

    def test_github_sync_uses_incremental_cursor_dedupes_and_closes(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "owner/repo"}})
        issue = {"number": 1, "title": "Fix addition", "body": "task data", "labels": [], "state": "OPEN", "updated_at": "2099-10-19", "html_url": "https://github.com/owner/repo/issues/1"}
        calls = []
        def gh(args, **kwargs):
            calls.append(args)
            return json.dumps([issue]) if args[1] == "api" else "[]"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='issue'")), 1)
            self.assertIn("page=1", [args for args in calls if args[1] == "api"][1])
            self.assertTrue(self.store.setting("issue-sync:" + self.project)["since"])
            self.store.execute("UPDATE tasks SET state='deferred' WHERE source='issue'")
            issue["state"] = "CLOSED"
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE source='issue'")["state"], "obsolete")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_paired_evolution_promotes_retrieves_and_rolls_back_verified_guidance(self):
        second = fixture(self.root / "multiply")
        (second / "calculator.py").write_text("def multiply(a, b):\n    return a / b\n")
        (second / "tests/test_calculator.py").write_text("import unittest\nfrom calculator import multiply\nclass Regression(unittest.TestCase):\n    def test_multiply(self):\n        self.assertEqual(multiply(2, 3), 6)\n")
        git(second, "add", ".")
        git(second, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "independent multiply defect")
        cases = [("addition", "validation", self.repo, "Correct addition"),
                 ("multiply", "holdout", second, "Protected multiply acceptance task")]
        for index, (name, expression, expected) in enumerate([
                ("subtract", "a + b", "-1"), ("divide", "a * b", "2 / 3"),
                ("maximum", "min(a, b)", "3"), ("minimum", "max(a, b)", "2")]):
            repo = fixture(self.root / name)
            (repo / "calculator.py").write_text("def " + name + "(a, b):\n    return " + expression + "\n")
            (repo / "tests/test_calculator.py").write_text("import unittest\nfrom calculator import " + name +
                "\nclass Regression(unittest.TestCase):\n    def test_operation(self):\n        self.assertEqual(" + name + "(2, 3), " + expected + ")\n")
            git(repo, "add", ".")
            git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", name)
            cases.append((name, "validation" if index % 2 else "holdout", repo, "Correct " + name))
        for family, split, repo, text in cases:
            Evolution(self.runtime).import_case(self.project, {"family": family, "split": split,
                "repository": str(repo), "base": "HEAD", "task": text, "checks": CHECKS, "source": "synthetic"})
        store = self.store
        proposal_prompts = []
        class GuidedProvider(FakeAgent):
            reject_reviews = False
            def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
                call = store.reserve_call(project, run, role, config["models"][role], config)
                store.execute("UPDATE calls SET state='completed',tokens=100 WHERE id=?", (call,))
                if role == "evolution":
                    proposal_prompts.append(prompt)
                    return super().invoke(role, prompt, workspace, project, run, config, cancel, protected)
                if role == "implementation":
                    if "Reproduce arithmetic boundary cases before editing" in prompt:
                        path = workspace / "calculator.py"
                        function = path.read_text().split("(")[0].split()[1]
                        expression = {"add": "a + b", "multiply": "a * b", "subtract": "a - b", "divide": "a / b",
                                      "maximum": "max(a, b)", "minimum": "min(a, b)"}[function]
                        path.write_text("def " + function + "(a, b):\n    return " + expression + "\n")
                    (workspace / "tests/test_strategy_regression.py").write_text(
                        (workspace / "tests/test_calculator.py").read_text())
                    return {"summary": "Applied supplied strategy"}
                result = super().invoke(role, prompt, workspace, project, run, config, cancel, protected)
                if role == "acceptance":
                    function = (workspace / "calculator.py").read_text().split("(")[0].split()[1]
                    expected = {"add": "5", "multiply": "6", "subtract": "-1", "divide": "2 / 3", "maximum": "3", "minimum": "2"}[function]
                    result["files"][0]["content"] = "from calculator import " + function + "\nassert " + function + "(2, 3) == " + expected + "\n"
                if role == "review" and self.reject_reviews:
                    result["assessments"][2]["decision"] = "fail"
                return result
        self.runtime.agent = GuidedProvider()
        self.runtime.configure({"limits": {"daily_calls": 400, "run_seconds": 1800},
                                "evolution": {"enabled": True, "min_cases_per_split": 3, "max_calls": 200,
                                              "repeats": 2, "max_cost_ratio": 10}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "evolve")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        candidates = self.store.query("SELECT * FROM candidates")
        self.assertEqual(candidates[0]["status"], "stable", candidates)
        self.assertNotIn("Protected multiply acceptance task", proposal_prompts[0])
        self.assertEqual(len(json.loads(candidates[0]["evidence"])["trials"]), 12)
        self.assertEqual(len(self.runtime.memories(self.project, "add")), 1)
        self.runtime.agent.reject_reviews = True
        self.runtime.request(self.project, "evolve")
        rejected_run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(rejected_run)
        rejected = self.store.one("SELECT * FROM candidates WHERE id<>?", (candidates[0]["id"],))
        self.assertNotEqual(rejected["status"], "stable")
        evidence = json.loads(rejected["evidence"])
        self.assertTrue(evidence["trials"])
        self.assertTrue(all(not trial["candidate"]["pass"] and trial["candidate"]["review"]["decision"] == "fail" for trial in evidence["trials"]))
        self.runtime.configure({"models": {"implementation": {"model": "another-model"}}}, self.project)
        self.assertEqual(self.runtime.active_strategies(self.project), [])
        self.assertEqual(self.runtime.memories(self.project, "add"), [])
        self.runtime.configure({"models": {"implementation": {"model": "gpt-6-luna"}}}, self.project)
        self.assertEqual(len(self.runtime.active_strategies(self.project)), 1)
        self.store.execute("UPDATE runs SET strategy=? WHERE id=?", (encode([{"id": candidates[0]["id"]}]), run["id"]))
        # Monitoring is tied to the actual run's strategy and execution compatibility.
        self.store.execute("UPDATE runs SET config=? WHERE id=?", (encode(self.runtime.config(self.project)), run["id"]))
        self.runtime.monitor_strategy(self.project, False, run, attributable=True)
        self.assertEqual(self.runtime.active_strategies(self.project), [])
        self.assertEqual(self.runtime.memories(self.project, "add"), [])


class ConfigurationAndAdapterTest(unittest.TestCase):
    def test_existing_call_history_migrates_without_losing_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            with sqlite3.connect(str(Path(temp) / "state.sqlite3")) as db:
                db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, project TEXT, run TEXT, role TEXT, model TEXT, effort TEXT, state TEXT, tokens INTEGER, seconds REAL, created REAL)")
                db.execute("INSERT INTO calls VALUES ('old','p','r','review','model','high','completed',100,1,0)")
            store = Store(temp)
            self.assertEqual(store.one("SELECT provider,tokens FROM calls WHERE id='old'"), {"provider": "openai", "tokens": 100})
            Store(temp)
            self.assertEqual(len(store.query("SELECT * FROM calls")), 1)

    def test_review_requires_every_area_and_preserves_nonblocking_findings(self):
        review = FakeAgent().invoke("review", "", None, None, None, DEFAULTS)
        review["assessments"][0]["decision"] = "insufficient_evidence"
        self.assertEqual(enforce_review(review, DEFAULTS)["decision"], "insufficient_evidence")
        review["assessments"][0]["decision"] = "pass"
        review["findings"] = [{"severity": "low", "area": "maintainability", "path": "source.py",
            "line": 1, "title": "Naming", "reason": "Name obscures units", "suggestion": "Include unit in name"}]
        self.assertEqual(enforce_review(review, DEFAULTS)["decision"], "pass")
        self.assertEqual(enforce_review(review, merge(DEFAULTS, {"review": {"block_on": "low"}}))["decision"], "fail")
        review["assessments"].pop()
        with self.assertRaisesRegex(RuntimeError, "every review area"):
            enforce_review(review, DEFAULTS)

    def test_risk_review_is_limited_to_sensitive_changes_or_missing_evidence(self):
        review = FakeAgent().invoke("review", "", None, None, None, DEFAULTS)
        plan = {"risk": "high"}
        self.assertFalse(risk_review_required(plan, review, ["calculator.py"], "+def add(a, b):"))
        self.assertTrue(risk_review_required(plan, review, ["auth/session.py"], ""))
        self.assertTrue(risk_review_required(plan, review, ["adaptive_rsi/auth_sessions.py"], ""))
        self.assertTrue(risk_review_required(plan, review, ["adaptive_rsi/updates.py"], ""))
        self.assertTrue(risk_review_required(plan, review, ["adaptive_rsi/delivery.py"], ""))
        self.assertTrue(risk_review_required(plan, review, ["adaptive_rsi/process.py"], "- --network=none"))
        self.assertTrue(risk_review_required(plan, review, ["adaptive_rsi/server.py"], "- origin check"))
        self.assertFalse(risk_review_required(plan, review, ["docs/authors.py"], ""))
        self.assertTrue(risk_review_required(plan, review, ["calculator.py"], "+permissions = ['admin']"))
        review["assessments"][0]["decision"] = "insufficient_evidence"
        self.assertTrue(risk_review_required(plan, review, ["calculator.py"], ""))
        review["assessments"][0]["decision"] = "pass"
        review["decision"] = "insufficient_evidence"
        self.assertTrue(risk_review_required(plan, review, ["calculator.py"], ""))
        self.assertFalse(risk_review_required({"risk": "low"}, review, ["auth/session.py"], ""))

    def test_docker_backend_and_image_probes_use_a_short_lived_cache(self):
        calls = []
        def probe(argv, **kwargs):
            calls.append(argv)
            return {"exit_code": 0, "error": None, "stdout": "ok", "stderr": ""}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state"
            config = validate({"sandbox": {"backend": "docker", "image": "worker:one"}})
            _PROBE_CACHE.clear()
            try:
                with patch("adaptive_rsi.process.platform.system", return_value="Linux"), \
                        patch("adaptive_rsi.process.shutil.which", return_value="/usr/bin/docker"), \
                        patch("adaptive_rsi.process.run_process", side_effect=probe):
                    sandbox = Sandbox(state, config)
                    self.assertEqual([sandbox.backend(), sandbox.backend()], ["docker", "docker"])
                    sandbox.command(["true"], workspace)
                    sandbox.command(["true"], workspace)
                    changed = validate({"sandbox": {"backend": "docker", "image": "worker:two"}})
                    Sandbox(state, changed).command(["true"], workspace)
            finally:
                _PROBE_CACHE.clear()
        self.assertEqual(sum(call[:2] == ["docker", "info"] for call in calls), 1)
        self.assertEqual(sum(call[:3] == ["docker", "image", "inspect"] for call in calls), 2)

    def test_concurrent_probes_wait_for_the_first_result_instead_of_duplicating_it(self):
        calls = []
        def probe():
            calls.append("probe")
            time.sleep(0.2)
            return {"exit_code": 0, "error": None, "stdout": "ok", "stderr": ""}
        key = ("backend", "auto", "Linux", "/usr/bin/docker")
        _PROBE_CACHE.clear()
        try:
            barrier = threading.Barrier(4)
            results, errors = [], []
            def call():
                try:
                    barrier.wait(5)
                    results.append(_cached_probe(key, probe))
                except Exception as exc:  # 收集任意线程失败后在主线程统一断言
                    errors.append(exc)
            threads = [threading.Thread(target=call) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            _PROBE_CACHE.clear()
            _PROBE_INFLIGHT.clear()
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual([result["exit_code"] for result in results], [0, 0, 0, 0])

    def test_concurrent_probe_failures_share_the_first_error_without_recaching_it(self):
        calls = []
        def probe():
            calls.append("probe")
            time.sleep(0.1)
            raise RuntimeError("docker daemon unreachable")
        key = ("backend", "auto", "Linux", "/usr/bin/docker")
        _PROBE_CACHE.clear()
        try:
            barrier = threading.Barrier(4)
            errors = []
            def call():
                barrier.wait(5)
                try:
                    _cached_probe(key, probe)
                except RuntimeError as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=call) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            _PROBE_CACHE.clear()
            _PROBE_INFLIGHT.clear()
        # 失败结果传播给等待者且不写缓存：之后的调用方仍会重新探测。
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(errors), 4)
        self.assertEqual(_PROBE_CACHE.get(key), None)

    def test_provider_validation_and_strategy_compatibility(self):
        provider = {"id": "gateway", "name": "Gateway", "base_url": "https://models.example/v1", "env_key": "RSI_MODEL_KEY"}
        config = validate({"providers": [provider], "models": {"implementation": {
            "provider": "gateway", "model": "qwen/coder:latest", "effort": "default"}}})
        self.assertNotEqual(compatibility(config), compatibility(DEFAULTS))
        self.assertNotEqual(compatibility(DEFAULTS), compatibility(merge(DEFAULTS, {"review": {"block_on": "high"}})))
        for bad in [{**provider, "api_key": "secret"}, {**provider, "env_key": "sk-secret"},
                    {**provider, "base_url": "https://key@models.example/v1"},
                    {**provider, "base_url": "https://models.example/v1?key=secret"},
                    {**provider, "base_url": "http://models.example/v1"}, {**provider, "id": "openai"}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate({"providers": [bad]})
        with self.assertRaises(ValueError):
            validate({"models": {"review": {"provider": "missing"}}})
        with self.assertRaisesRegex(RuntimeError, "environment variable"):
            with patch.dict(os.environ, {}, clear=True):
                provider_arguments(config["models"]["implementation"], config)

    def test_rust_integration_tests_are_not_rejected_for_empty_doc_tests(self):
        self.assertFalse(empty_test_run("running 0 tests\nrunning 3 tests\n3 passed; 0 failed\nrunning 0 tests"))
        self.assertTrue(empty_test_run("running 0 tests\n0 passed; 0 failed"))
        self.assertTrue(empty_test_run("No examples found.\n0 examples, 0 failures"))
        self.assertTrue(empty_test_run("# tests 0\n# pass 0"))
        self.assertTrue(empty_test_run("\u2139 tests 0\n\u2139 pass 0"))
        self.assertTrue(empty_test_run("0 passing (5ms)"))
        self.assertFalse(empty_test_run("\u2139 tests 2\n\u2139 pass 2"))
    def test_configuration_rejects_typos_escapes_and_invalid_model_effort(self):
        for override in [{"scheduel": {}}, {"models": {"implementation": {"effort": "ultra"}}},
                         {"checks": [{"name": "escape", "cwd": "../", "argv": ["true"]}]},
                         {"schedule": {"light_minutes": 0}}, {"delivery": {"auto_merge": True}},
                         {"limits": {"daily_calls": True}}]:
            with self.assertRaises(ValueError, msg=str(override)):
                validate(override)

    def test_command_seconds_is_a_safety_timeout_not_a_retired_quota(self):
        self.assertEqual(validate({})["limits"]["command_seconds"], 3600)
        # 旧版本把它当作额度字段清零；升级后 0 或缺省必须回到安全超时，避免被测命令永不超时。
        self.assertEqual(validate({"limits": {"command_seconds": 0}})["limits"]["command_seconds"], 3600)
        self.assertEqual(validate({"limits": {"command_seconds": 7200}})["limits"]["command_seconds"], 7200)
        with self.assertRaises(ValueError):
            validate({"limits": {"command_seconds": -1}})

    def test_polyglot_manifest_detection_and_honest_environment_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {"java/pom.xml": "", "kotlin/build.gradle.kts": "", "js/package.json": '{"scripts":{"test":"node --test"}}',
                     "py/pyproject.toml": "", "go/go.mod": "module example", "rs/Cargo.toml": "", "cpp/CMakeLists.txt": "",
                     "cs/sample.csproj": "", "php/composer.json": "{}", "ruby/Gemfile": "", "swift/Package.swift": "",
                     "flutter/pubspec.yaml": "flutter:\n", "sql/example.sql": "", "shell/example.sh": "", "html/index.html": ""}
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with patch("adaptive_rsi.adapters.shutil.which", return_value=None):
                data = detect(root)
            self.assertEqual(len(data["modules"]), 12)
            self.assertTrue(all(m["status"] == "environment_blocked" for m in data["modules"]))
            self.assertIn("SQL", data["auxiliary"])

    def test_promotion_requires_quality_and_known_cost_on_both_splits(self):
        def trial(split, before, after, tokens=100):
            return {"split": split, "stable": {"pass": before, "tokens": 100, "seconds": 10},
                    "candidate": {"pass": after, "tokens": tokens, "seconds": 10}}
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", False, True)], DEFAULTS)["decision"], "insufficient_evidence")
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", True, False)], DEFAULTS)["decision"], "fail")
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", True, True)], DEFAULTS)["decision"], "insufficient_evidence")
        self.assertEqual(Evolution.decide([trial("validation", False, True, 0), trial("holdout", False, True, 0)], DEFAULTS)["decision"], "insufficient_evidence")

    def test_subprocess_timeout_and_output_cap(self):
        result = run_process(["python3", "-c", "import time; time.sleep(5)"], timeout=0.1)
        self.assertEqual(result["error"], "Command timed out")

    @patch("adaptive_rsi.cli_worker.platform.system", new=lambda: "Darwin")
    def test_custom_and_local_models_route_without_forced_reasoning_or_secret_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "controller")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            commands = []
            def provider(argv, *args, **kwargs):
                commands.append(argv)
                Path(argv[argv.index("--output-last-message") + 1]).write_text('{"summary":"done"}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": ""}
            config = validate({"providers": [{"id": "gateway", "name": "Gateway", "base_url": "https://models.example/v1", "env_key": "RSI_MODEL_KEY"}],
                "models": {"implementation": {"provider": "gateway", "model": "qwen/coder:latest", "effort": "default"}}})
            with patch.dict(os.environ, {"RSI_MODEL_KEY": "test-secret-never-in-arguments"}), \
                 patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=["-c", 'default_permissions="rsi"']), \
                 patch("adaptive_rsi.agent.run_process", side_effect=provider), \
                 patch("adaptive_rsi.native.post_json", return_value={"message": {"tool_calls": [{"function": {"name": "finish", "arguments": {"summary": "done"}}}]}}):
                agent = CodexAgent(store)
                agent.invoke("implementation", "task", workspace, "project", "run", config)
                local = merge(config, {"models": {"implementation": {"provider": "ollama"}}})
                agent.invoke("implementation", "task", workspace, "project", "run", local)
            self.assertIn('model_provider="gateway"', commands[0])
            self.assertIn('model_providers.gateway.wire_api="responses"', commands[0])
            self.assertIn('model_providers.gateway.env_key="RSI_MODEL_KEY"', commands[0])
            self.assertFalse(any("model_reasoning_effort" in arg for arg in commands[0]))
            self.assertNotIn("test-secret-never-in-arguments", json.dumps(commands))
            self.assertEqual(len(commands), 1)
            self.assertEqual([c["provider"] for c in store.query("SELECT provider FROM calls ORDER BY rowid")], ["gateway", "ollama"])

    @patch("adaptive_rsi.cli_worker.platform.system", new=lambda: "Darwin")
    def test_provider_fallback_and_failed_setup_are_accounted_for(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "controller")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            commands = []
            def provider(argv, *args, **kwargs):
                commands.append(argv)
                if len(commands) == 1:
                    return {"exit_code": 1, "error": None, "stderr": "model not found", "stdout": ""}
                Path(argv[argv.index("--output-last-message") + 1]).write_text('{"summary":"done"}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": ""}
            config = validate({"models": {"implementation": {"fallback": {"provider": "lmstudio", "model": "other-coder", "effort": "default"}}}})
            with patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=["-c", 'default_permissions="rsi"']), \
                 patch("adaptive_rsi.agent.run_process", side_effect=provider), \
                 patch("adaptive_rsi.native.post_json", return_value={"choices": [{"message": {"tool_calls": [{"id": "done", "function": {"name": "finish", "arguments": '{"summary":"done"}'}}]}}]}):
                CodexAgent(store).invoke("implementation", "task", workspace, "project", "run", config)
            self.assertEqual([c["state"] for c in store.query("SELECT state FROM calls ORDER BY rowid")], ["failed", "completed"])
            self.assertEqual(len(commands), 1)
            with patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.codex_permissions", side_effect=RuntimeError("sandbox unavailable")):
                with self.assertRaisesRegex(RuntimeError, "sandbox unavailable"):
                    CodexAgent(store).invoke("implementation", "task", workspace, "project", "run", DEFAULTS)
            self.assertEqual(store.query("SELECT state FROM calls ORDER BY rowid")[-1]["state"], "failed")
            def malformed_output(argv, *args, **kwargs):
                Path(argv[argv.index("--output-last-message") + 1]).write_text('{"summary":42}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": ""}
            with patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=["-c", 'default_permissions="rsi"']), \
                 patch("adaptive_rsi.agent.run_process", side_effect=malformed_output):
                with self.assertRaisesRegex(RuntimeError, "invalid structured output"):
                    CodexAgent(store).invoke("implementation", "task", workspace, "project", "run", DEFAULTS)
            self.assertEqual(store.query("SELECT state FROM calls ORDER BY rowid")[-1]["state"], "failed")

    @patch("adaptive_rsi.cli_worker.platform.system", new=lambda: "Darwin")
    def test_codex_command_really_routes_model_and_effort_and_records_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "controller")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            commands = []
            def provider(argv, *args, **kwargs):
                commands.append(argv)
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text('{"summary":"done"}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":10}}\n'}
            with patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=["-c", 'default_permissions="rsi"']), \
                 patch("adaptive_rsi.agent.run_process", side_effect=provider):
                result = CodexAgent(store).invoke("implementation", "task", workspace, "project", "run", DEFAULTS)
            self.assertEqual(result, {"summary": "done"})
            args = commands[0]
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
            self.assertIn('model_reasoning_effort="max"', args)
            self.assertIn('default_permissions="rsi"', args)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
            self.assertIn("--ignore-user-config", args)
            self.assertEqual(store.one("SELECT tokens FROM calls")["tokens"], 60)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(self.tmp.name), FakeAgent())
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.tmp.cleanup()

    def headers(self):
        return {"Cookie": self.server.cookie + "=" + self.server.token}

    def post(self, path, body):
        request = urllib.request.Request(
            self.server.origin + path, json.dumps(body).encode(),
            {**self.headers(), "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
             "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_project_api_reports_duplicate_path_and_normalizes_blank_names(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = fixture(Path(temp) / "repo")
            created = self.post("/api/projects", {"path": str(repo), "name": "  Named project  ", "enabled": False})
            self.assertFalse(created["existing"])
            self.assertEqual(created["name"], "Named project")
            self.assertTrue(created["paused"])
            duplicate = self.post("/api/projects", {"path": str(repo), "name": "Different", "enabled": True})
            self.assertTrue(duplicate["existing"])
            self.assertEqual(duplicate["id"], created["id"])
            self.assertEqual(duplicate["name"], "Named project")
            self.assertTrue(duplicate["paused"])
            project = self.runtime.project(created["id"])
            self.assertEqual((project["name"], project["paused"]), ("Named project", 1))
            # 旧状态记录中的空白名称也回退到目录名，不显示空白选项。
            self.runtime.store.execute("UPDATE projects SET name='   ' WHERE id=?", (created["id"],))
            with urllib.request.urlopen(urllib.request.Request(
                    self.server.origin + "/api/state", headers=self.headers())) as response:
                state = json.load(response)
            shown = next(item for item in state["projects"] if item["id"] == created["id"])
            self.assertEqual(shown["name"], "repo")
            self.assertEqual(shown["path"], str(repo.resolve()))

    def test_aborted_response_does_not_escape_as_server_error(self):
        class ClosedConnection:
            close_connection = False
            wfile = None

            def send_response(self, code):
                self.code = code

            def headers_common(self):
                pass

            def send_header(self, name, value):
                pass

            def end_headers(self):
                raise BrokenPipeError("browser cancelled request")

        handler = ClosedConnection()
        Handler.respond(handler, {"ok": True})
        self.assertTrue(handler.close_connection)

    def test_missing_browser_session_has_safe_recovery_page_but_api_stays_private(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.server.origin + "/")
        self.assertEqual(denied.exception.code, 401)
        page = denied.exception.read().decode()
        self.assertIn("需要重新登录", page)
        self.assertIn("python3 scripts/rsi open", page)
        self.assertNotIn(self.server.token, page)
        self.assertNotIn(self.server.csrf, page)
        self.assertIn("default-src 'self'", denied.exception.headers["Content-Security-Policy"])
        with urllib.request.urlopen(self.server.origin + "/login.css") as response:
            self.assertIn("text/css", response.headers["Content-Type"])
        for path in ["/api/state", "/app.js", "/style.css"]:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(self.server.origin + path)
            self.assertEqual(denied.exception.code, 401)
            self.assertEqual(json.load(denied.exception)["error_code"], "session_required")

    def test_stale_token_link_falls_back_to_a_valid_session(self):
        # 收藏的旧启动地址携带已轮换 token；已登录的浏览器应直接回到控制台而不是 403。
        request = urllib.request.Request(self.server.origin + "/?token=stale-token", headers=self.headers())
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)

    def test_session_rotation_invalidates_the_old_token_and_returns_the_new_link(self):
        old_token = self.server.token
        request = urllib.request.Request(self.server.origin + "/api/session/rotate",
                                         data=json.dumps({"confirm": True}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Origin": self.server.local_origin,
                                                  "Cookie": self.server.cookie + "=" + self.server.token,
                                                  "X-RSI-CSRF": self.server.csrf})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        self.assertNotEqual(self.server.token, old_token)
        self.assertIn("?token=" + self.server.token, result["url"])
        self.assertEqual(result["csrf"], self.server.csrf)
        # 旧 token 的链接和旧会话立即失效。
        for credentials in ["?token=" + old_token, None]:
            headers = {} if credentials else {"Cookie": self.server.cookie + "=" + old_token}
            url = self.server.origin + "/" + (credentials or "")
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(urllib.request.Request(url, headers=headers))
            self.assertEqual(denied.exception.code, 401)
        # 新链接可登录（303 并下发新会话 cookie），轮换持久化到 server.json。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self.server.origin + "/?token=" + self.server.token)
            self.fail("expected the token link to redirect")
        except urllib.error.HTTPError as redirect:
            self.assertEqual(redirect.code, 303)
            self.assertIn(self.server.cookie + "=" + self.server.token, redirect.headers.get("Set-Cookie", ""))
        with urllib.request.urlopen(urllib.request.Request(self.server.origin + "/",
                headers={"Cookie": self.server.cookie + "=" + self.server.token})) as response:
            self.assertEqual(response.status, 200)
        session = json.loads((Path(self.tmp.name) / "server.json").read_text())
        self.assertEqual(session["token"], self.server.token)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.server.origin + "/?token=stale-token")
        self.assertEqual(denied.exception.code, 401)

    def test_non_ascii_tokens_are_rejected_without_raising(self):
        # compare_digest 对非 ASCII str 抛 TypeError；必须按不匹配处理而不是让连接炸成 traceback。
        # HTTP 头无法直接携带非 ASCII（latin-1 限制），因此直接验证比较辅助方法。
        self.assertFalse(Handler._token_matches("非ASCII令牌", self.server.token))
        self.assertFalse(Handler._token_matches(None, self.server.token))
        self.assertTrue(Handler._token_matches(self.server.token, self.server.token))

    def test_update_gate_rejects_before_mutation_with_a_retryable_code(self):
        self.runtime.updates.installing = True
        headers = {**self.headers(), "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
                   "Content-Type": "application/json"}
        before = self.runtime.config()["schedule"]["light_minutes"]
        for path, body in [("config", {"values": {"schedule": {"light_minutes": 240}}}),
                           ("projects/import", {"url": "https://github.com/example/project"}),
                           ("model-center/provider", {})]:
            with self.subTest(path=path), patch("adaptive_rsi.runtime.checked") as clone:
                request = urllib.request.Request(self.server.origin + "/api/" + path, json.dumps(body).encode(), headers)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(json.load(caught.exception)["error_code"], "update_in_progress")
                caught.exception.close()
                clone.assert_not_called()
        self.assertEqual(self.runtime.config()["schedule"]["light_minutes"], before)
        self.assertEqual(self.runtime.store.query("SELECT * FROM projects"), [])

    def test_restored_session_imports_project_and_saves_settings_on_the_same_server(self):
        self.runtime.updates.restore_startup()
        headers = {**self.headers(), "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
                   "Content-Type": "application/json"}
        with tempfile.TemporaryDirectory() as temp:
            source = fixture(Path(temp) / "source")
            def clone(argv, **kwargs):
                destination = argv[4]
                checked(["git", "clone", "--quiet", str(source), destination])
                git(destination, "remote", "set-url", "origin", "https://github.com/example/project.git")
                return ""
            with patch("adaptive_rsi.runtime.checked", side_effect=clone):
                request = urllib.request.Request(self.server.origin + "/api/projects/import",
                    json.dumps({"url": "https://github.com/example/project", "directory": str(Path(temp) / "managed")}).encode(), headers)
                with urllib.request.urlopen(request) as response:
                    project = json.load(response)["id"]
            request = urllib.request.Request(self.server.origin + "/api/config",
                json.dumps({"project": project, "values": {"schedule": {"light_minutes": 240, "weight": 3}}}).encode(), headers)
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
                self.assertTrue(result["saved"])
                self.assertEqual(result["config"]["schedule"]["weight"], 3)
            with urllib.request.urlopen(urllib.request.Request(self.server.origin + "/api/health", headers=self.headers())) as response:
                self.assertEqual(json.load(response)["pid"], os.getpid())
            self.assertEqual(self.runtime.config(project)["schedule"]["weight"], 3)
            self.assertEqual(self.runtime.config(project)["schedule"]["light_minutes"], 240)
            self.assertTrue(self.runtime.project(project)["paused"])

    def test_loopback_startup_does_not_depend_on_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=OSError("reverse DNS unavailable")):
            server = Server(self.runtime)
        try:
            self.assertEqual(server.server_name, "localhost")
            self.assertGreater(server.server_port, 0)
            self.assertEqual(server.origin, "http://127.0.0.1:" + str(server.server_port))
        finally:
            server.server_close()

    def test_local_api_requires_authentication_origin_and_csrf(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.server.origin + "/api/state")
        self.assertEqual(denied.exception.code, 401)
        headers = self.headers()
        with urllib.request.urlopen(urllib.request.Request(self.server.origin + "/api/state", headers=headers)) as response:
            data = json.load(response)
            self.assertEqual(data["csrf"], self.server.csrf)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.server.origin + "/api/config", b'{"values":{}}', headers)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        self.assertEqual(denied.exception.code, 403)
        headers.update({"Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf})
        request = urllib.request.Request(self.server.origin + "/api/config", b'{"values":{"schedule":{"light_minutes":240}}}', headers)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.runtime.config()["schedule"]["light_minutes"], 240)


if __name__ == "__main__":
    unittest.main()
