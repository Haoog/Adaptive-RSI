import json
import platform
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import BUILTIN_PROVIDERS, DEFAULTS, compatibility, fingerprint, merge
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.context import _code_index, _code_map_cached, code_map, related, terms
from adaptive_rsi.dashboard import records
from adaptive_rsi.discovery import related_task, value_priority
from adaptive_rsi.evolution import Evolution
from adaptive_rsi.evaluator import CACHE_STDERR_LIMIT, Evaluator, _isolated_test_check, freeze_harness
from adaptive_rsi.gitops import git, snapshot
from adaptive_rsi.readiness import budget, inspect as readiness
from adaptive_rsi.recovery import failure
from adaptive_rsi.review import risk_review_required
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.pipeline import (_evaluation_source, _review_evidence, _reusable_evaluation,
                                   _stage_config, _uses_test_first)
from adaptive_rsi.store import Store, encode
from adaptive_rsi.workflow import change_kind, improvement, supports_test_first
from tests.test_runtime import CHECKS, FakeAgent, fixture


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)
        self.runtime.pause(self.project, False)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def claim(self, text="Fix addition"):
        task = self.runtime.goal(self.project, text)
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    def test_legacy_budget_settings_no_longer_block_readiness(self):
        self.runtime.configure({"limits": {"daily_calls": 3}}, self.project)
        self.assertTrue(budget(self.store, self.project, self.runtime.config(self.project), 400)["ready"])
        self.assertEqual(self.runtime.config(self.project)["limits"]["daily_calls"], 0)

    def test_missing_dependencies_block_before_planning(self):
        self.runtime.configure({"environment": {"seed_paths": ["missing-dependencies"]}}, self.project)
        task, run = self.claim()
        self.runtime.process(run)
        self.assertEqual(self.runtime.agent.roles, [])
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertFalse(next(i for i in result["readiness"]["items"] if i["code"] == "dependencies")["ready"])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_configuration_change_uses_test_first_without_independent_acceptance(self):
        class ConfigurationPlan(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["change_kind"] = "configuration"
                return result
        self.runtime.agent = ConfigurationPlan()
        task, run = self.claim("Configure the addition behavior")
        self.runtime.process(run)
        row = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        result = json.loads(row["result"])
        self.assertEqual(row["state"], "awaiting_approval", result)
        self.assertEqual(result["evaluation"]["improvement"]["kind"], "configuration")
        self.assertEqual(result["evaluation"]["improvement"]["decision"], "pass")
        self.assertNotIn("acceptance", [role for role, _ in self.runtime.agent.roles])

    def test_injected_transport_is_available_without_bypassing_real_codex_requirement(self):
        import shutil
        system_which = shutil.which
        with patch("adaptive_rsi.agent.shutil.which", side_effect=lambda name: None if name == "codex" else system_which(name)):
            simulated = readiness(self.runtime, self.runtime.project(self.project))
            self.assertTrue(all(i["ready"] for i in simulated["items"] if i["code"] == "model"))
            self.runtime.agent = CodexAgent(self.store)
            real = readiness(self.runtime, self.runtime.project(self.project))
            self.assertTrue(all(not i["ready"] for i in real["items"] if i["code"] == "model"))
            self.assertFalse(real["ready"])
        self.assertEqual(self.store.query("SELECT * FROM calls"), [])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    # 测试 ID 被 CI 矩阵固定（.github/workflows/checks.yml macos_sandbox_flows），
    # 行为已改为复用已完成评估：补丁与检查配置不变时不再重跑评价阶段。
    def test_review_outage_reuses_plan_and_patch_but_reruns_checks(self):
        class Outage(FakeAgent):
            unavailable = True
            def invoke(self, role, *args, **kwargs):
                if role == "review" and self.unavailable:
                    self.unavailable = False
                    raise RuntimeError("Provider connection timeout")
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Outage()
        task, run = self.claim()
        with patch.object(self.runtime.evaluator, "evaluate", wraps=self.runtime.evaluator.evaluate) as evaluate:
            self.runtime.process(run)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
            self.runtime.retry(task)
            second = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(second)
            # 恢复时补丁未变、检查配置一致，已完成的评价随候选一起复用。
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")
        roles = [r[0] for r in self.runtime.agent.roles]
        self.assertEqual(roles.count("planning"), 1)
        self.assertEqual(roles.count("acceptance"), 0)
        self.assertEqual(roles.count("implementation"), 1)
        stages = {r["stage"] for r in self.store.query("SELECT stage FROM stages WHERE run=?", (second["id"],))}
        self.assertTrue({"planning", "implementation", "evaluation", "review"} <= stages)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_implementation_route_change_reuses_plan_without_acceptance(self):
        class Outage(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                if role == "implementation":
                    route = kwargs.get("config", args[4] if len(args) > 4 else {})["models"][role]
                    self.roles.append((role, route))
                    raise RuntimeError("Provider connection timeout")
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Outage()
        task, run = self.claim()
        self.runtime.process(run)
        self.runtime.configure({"models": {"implementation": {"effort": "high"}}}, self.project)
        self.runtime.retry(task)
        second = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(second)
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("planning"), 1)
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("acceptance"), 0)
        implementations = [route for role, route in self.runtime.agent.roles if role == "implementation"]
        self.assertEqual([route["effort"] for route in implementations], ["max", "high"])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_completed_review_is_reused_after_a_post_review_failure(self):
        task, run = self.claim()
        triggered = [False]
        def cancel_after_review(current):
            if not triggered[0] and self.store.one("SELECT 1 FROM stages WHERE run=? AND stage='review'", (current["id"],)):
                triggered[0] = True
                return True
            return False
        with patch.object(self.runtime, "is_cancelled", side_effect=cancel_after_review):
            self.runtime.process(run)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "interrupted")
        self.runtime.retry(task)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("review"), 1)
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("implementation"), 1)

    def evaluation_sandbox(self):
        """Deterministic check launcher for hosts without a sandbox backend.

        Checks pass exactly when the tree carries the fixed implementation, so
        baseline rows fail and candidate rows pass without weakening any gate;
        the evaluator, its stages and the recovery cache stay real.
        """
        def fake_check(sandbox, check, workspace, *args, **kwargs):
            fixed = "return a + b" in (Path(workspace) / "calculator.py").read_text(encoding="utf-8")
            return {"name": check["name"], "argv": check["argv"], "exit_code": 0 if fixed else 1,
                    "stdout": "Ran 1 test\nOK" if fixed else "Ran 1 test\nFAILED (failures=1)\n",
                    "stderr": "", "seconds": 0.0, "error": None}
        return (patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="stub"),
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=fake_check),
                patch("adaptive_rsi.evaluator.run_process", return_value={
                    "exit_code": 0, "error": None, "stdout": "sha256:" + "a" * 64, "stderr": ""}),
                patch("adaptive_rsi.readiness.inspect",
                      return_value={"ready": True, "items": [], "checks": CHECKS}))

    def evaluation_reuse_fixture(self, interrupted_role):
        """One provider outage at ``interrupted_role`` on the first attempt only."""
        class Outage(FakeAgent):
            failed = False
            def invoke(self, role, *args, **kwargs):
                if role == interrupted_role and not self.failed:
                    self.failed = True
                    self.roles.append((role, {}))
                    raise RuntimeError("Provider connection timeout")
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Outage()
        return self.evaluation_sandbox()

    def interrupt(self, interrupted_role):
        """Run one attempt that stops at the injected provider outage."""
        self.runtime.configure({"sources": {"inspection": False, "issues": False}}, self.project)
        task, run = self.claim()
        with ExitStack() as stack:
            for scenario in self.evaluation_reuse_fixture(interrupted_role):
                stack.enter_context(scenario)
            self.runtime.process(run)
        return task, run

    def resume(self, task):
        """Retry the interrupted task and process the resumed run."""
        self.runtime.retry(task)
        with ExitStack() as stack:
            for scenario in self.evaluation_sandbox():
                stack.enter_context(scenario)
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(run)
        return run

    def run_recovery_pair(self, interrupted_role):
        """Run once to the outage, retry, and return both runs."""
        task, run = self.interrupt(interrupted_role)
        return task, run, self.resume(task)

    def test_resume_with_saved_evaluation_skips_evaluation_rerun(self):
        with patch.object(self.runtime.evaluator, "evaluate",
                          wraps=self.runtime.evaluator.evaluate) as evaluate:
            task, run, second = self.run_recovery_pair("review")
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (second["id"],))["result"])
        self.assertEqual(result["evaluation"]["decision"], "pass")
        self.assertEqual(result["review"]["decision"], "pass")
        events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (second["id"],))
        reused = [json.loads(row["data"])["stage"] for row in events if row["kind"] == "stage_reused"]
        self.assertIn("evaluation", reused)
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("implementation"), 1)
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("review"), 2)
        saved = json.loads(self.store.one("SELECT data FROM stages WHERE run=? AND stage='evaluation'",
                                          (second["id"],))["data"])
        self.assertEqual(saved["decision"], "pass")
        self.assertEqual(saved["source"], result["evaluation"]["source"])
        self.assertEqual(saved["source"]["patch_hash"], result["patch_hash"])

    def test_resume_without_saved_evaluation_reruns_evaluation(self):
        with patch.object(self.runtime.evaluator, "evaluate",
                          wraps=self.runtime.evaluator.evaluate) as evaluate:
            task, run, second = self.run_recovery_pair("implementation")
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (second["id"],))["result"])
        self.assertEqual(result["evaluation"]["decision"], "pass")
        events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (second["id"],))
        reused = [json.loads(row["data"])["stage"] for row in events if row["kind"] == "stage_reused"]
        self.assertNotIn("evaluation", reused)
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("implementation"), 2)

    def test_resume_with_saved_evaluation_reruns_after_checks_reconfiguration(self):
        task, run = self.interrupt("review")
        self.runtime.configure({"checks": CHECKS + [{"name": "extra probe", "cwd": ".",
                                                     "argv": ["python3", "-c", "assert True"]}]}, self.project)
        with patch.object(self.runtime.evaluator, "evaluate",
                          wraps=self.runtime.evaluator.evaluate) as evaluate:
            second = self.resume(task)
            self.assertEqual(evaluate.call_count, 1)
        events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (second["id"],))
        reused = [json.loads(row["data"])["stage"] for row in events if row["kind"] == "stage_reused"]
        self.assertNotIn("evaluation", reused)
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("implementation"), 2)

    def test_resume_with_incomplete_saved_evaluation_reruns_evaluation(self):
        task, run = self.interrupt("review")
        saved = json.loads(self.store.one("SELECT data FROM stages WHERE run=? AND stage='evaluation'",
                                          (run["id"],))["data"])
        saved["checks"] = {}
        self.store.execute("UPDATE stages SET data=? WHERE run=? AND stage='evaluation'",
                           (encode(saved), run["id"]))
        with patch.object(self.runtime.evaluator, "evaluate",
                          wraps=self.runtime.evaluator.evaluate) as evaluate:
            second = self.resume(task)
            self.assertEqual(evaluate.call_count, 1)
        events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (second["id"],))
        stages = [(row["kind"], json.loads(row["data"])["stage"]) for row in events
                  if row["kind"] in {"stage_reused", "stage_invalidated"}]
        self.assertNotIn(("stage_reused", "evaluation"), stages)
        self.assertIn(("stage_invalidated", "evaluation"), stages)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (second["id"],))["result"])
        self.assertEqual(result["evaluation"]["decision"], "pass")

    def test_evaluation_reuse_requires_matching_tree_checks_and_controls(self):
        config = self.runtime.config(self.project)
        source = _evaluation_source(config, "a" * 40, CHECKS, "candidate patch", "digest", None,
                                    True, "behavior")
        record = {"decision": "pass", "reason": "Regression and full candidate checks passed",
                  "harness_hash": "harness", "checks": {"candidate": [{"name": "regression",
                                                                       "passed": True}]},
                  "source": source}
        self.assertEqual(_reusable_evaluation(source)(record), record)
        for changed in ({**record, "decision": "insufficient_evidence"},
                        {**record, "checks": {}},
                        {key: value for key, value in record.items() if key != "harness_hash"},
                        {**record, "source": {**source, "base": "b" * 40}},
                        {**record, "source": {**source, "patch_hash": "other"}},
                        {**record, "source": {**source, "checks": []}},
                        {**record, "source": {**source, "environment": "other"}},
                        {**record, "source": {**source, "acceptance_hash": "other"}},
                        {**record, "source": {**source, "validation": {"controls": [{"id": "extra"}]}}}):
            with self.assertRaises(RuntimeError):
                _reusable_evaluation(source)(changed)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_changed_baseline_invalidates_recovery_cache(self):
        class Outage(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                if role == "implementation":
                    self.roles.append((role, {}))
                    raise RuntimeError("Provider timeout")
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Outage()
        task, run = self.claim()
        self.runtime.process(run)
        (self.repo / "context.txt").write_text("new baseline\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "new baseline")
        self.runtime.retry(task)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("planning"), 2)

    def test_project_scoped_pagination_keeps_old_project_and_latest_run_visible(self):
        other = self.runtime.add_project(fixture(self.root / "other"))
        task = self.store.add_task(self.project, "goal", "old", "old project")
        self.store.execute("INSERT INTO runs(id,task,project,state,config,started,ended) VALUES ('old-run',?,?,'failed','{}',1,2)", (task, self.project))
        for i in range(310):
            self.store.add_task(other, "goal", str(i), "Other project " + str(i))
        rows = records(self.store, self.project, limit=1)
        self.assertEqual([t["id"] for t in rows["tasks"]], [task])
        self.assertEqual(rows["runs"][0]["id"], "old-run")
        page = records(self.store, other, limit=20, offset=20)
        self.assertEqual(page["pagination"]["total"], 310)
        self.assertEqual(len(page["tasks"]), 20)
        self.assertTrue(all(t["project"] == other for t in page["tasks"]))

    def test_durable_issue_pages_reopen_and_preserve_active_revision(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "owner/repo", "max_issues": 1}})
        issue = {"number": 1, "title": "Addition", "body": None, "labels": [], "state": "open",
                 "updated_at": "2099-01-01T00:00:00Z", "html_url": "https://github.com/owner/repo/issues/1"}
        requests = []
        def gh(args, **kwargs):
            if args[1] != "api":
                return "[]"
            page = next(int(a.split("=", 1)[1]) for a in args if a.startswith("page="))
            requests.append(page)
            return encode([issue] if page == 1 else [])
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.assertEqual(requests, [1, 1])
            row = self.store.one("SELECT * FROM tasks WHERE source='issue'")
            self.store.execute("UPDATE tasks SET state='obsolete' WHERE id=?", (row["id"],))
            issue["updated_at"] = "2099-01-02T00:00:00Z"
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (row["id"],))["state"], "queued")
            self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (row["id"],))
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            issue["updated_at"] = "2099-01-03T00:00:00Z"
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            after = self.store.one("SELECT * FROM tasks WHERE id=?", (row["id"],))
            self.assertEqual(json.loads(after["evidence"])[0]["updated"], "2099-01-02T00:00:00Z")
            self.assertTrue(self.store.query("SELECT * FROM events WHERE kind='issue_revision'"))

    def test_budget_wait_is_in_attention_filter_and_not_history(self):
        task, run = self.claim()
        self.store.transition(run["id"], "waiting_budget", {}, True)
        self.assertEqual([t["id"] for t in records(self.store, self.project, group="attention")["tasks"]], [task])
        self.assertEqual(records(self.store, self.project, group="history")["tasks"], [])

    def test_invalid_issue_page_does_not_advance_cursor(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "owner/repo"}})
        with patch("adaptive_rsi.runtime.checked", return_value='{"error":"bad"}'):
            with self.assertRaisesRegex(RuntimeError, "Unexpected"):
                self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertIsNone(self.store.setting("issue-sync:" + self.project))

    def test_issue_revision_held_by_active_run_survives_sync_watermark(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "owner/repo"}})
        task = self.store.add_task(self.project, "issue", "issue:1", "Old issue", "old",
                                  [{"number": 1, "updated": "2020-01-01T00:00:00Z", "url": "https://github.com/owner/repo/issues/1"}])
        self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (task,))
        self.store.put_setting("issue-sync:" + self.project,
                               {"repository": "owner/repo", "policy": fingerprint({"labels": [], "exclude": config["github"]["exclude_labels"]}),
                                "since": "2025-01-01T00:00:00Z", "page": 1})
        issue = {"number": 1, "title": "Revised issue", "body": "new", "labels": [], "state": "open",
                 "updated_at": "2024-01-01T00:00:00Z", "html_url": "https://github.com/owner/repo/issues/1"}
        def gh(args, **kwargs):
            return encode([issue]) if args[1] == "api" else "[]"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.assertEqual(self.store.one("SELECT body FROM tasks WHERE id=?", (task,))["body"], "old")
            self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task,))
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual((row["state"], row["body"]), ("queued", "new"))

    def test_legacy_budget_wait_resumes_without_quota(self):
        task, run = self.claim()
        self.store.transition(run["id"], "waiting_budget", {"failure": {"code": "budget"}}, True)
        low = merge(self.runtime.config(self.project), {"limits": {"daily_calls": 3}})
        self.runtime.resume_budget(self.project, low)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
        self.runtime.resume_budget(self.project, self.runtime.config(self.project))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_value_assessment_yields_and_reuses_plan_without_spending_retry(self):
        first, run = self.claim("Fix addition first")
        second = self.runtime.goal(self.project, "Correct arithmetic boundary")
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (first,))
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        next_run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(next_run["task"], second)
        self.runtime.process(next_run)
        final = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(final["task"], first)
        self.runtime.process(final)
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("planning"), 2)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (first,))["state"], "awaiting_approval")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_high_risk_requires_additional_review_and_preserves_blocking_result(self):
        class HighRisk(FakeAgent):
            reviews = 0
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["risk"] = "high"
                    result["scope"] = ["calculator.py", "auth.py", "tests"]
                if role == "implementation":
                    (args[1] / "auth.py").write_text("PERMISSION_BOUNDARY = 'owner'\n")
                if role == "review":
                    self.reviews += 1
                    if self.reviews == 2:
                        result["assessments"][1]["decision"] = "insufficient_evidence"
                return result
        self.runtime.agent = HighRisk()
        task, run = self.claim()
        self.runtime.process(run)
        self.assertEqual(self.runtime.agent.reviews, 2)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual(result["risk_review"]["decision"], "insufficient_evidence")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "insufficient_evidence")
        self.assertFalse(self.store.query("SELECT * FROM stages WHERE run=? AND stage='validated_candidate'", (run["id"],)))

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_ordinary_high_risk_does_not_force_a_second_review(self):
        class HighRisk(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["risk"] = "high"
                return result
        self.runtime.agent = HighRisk()
        task, run = self.claim()
        self.runtime.process(run)
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("review"), 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")

    def test_code_map_reuses_content_index_and_invalidates_dirty_changes(self):
        before = _code_index.cache_info()
        mapping_before = _code_map_cached.cache_info()
        self.assertEqual(code_map(self.repo, "calculator", limit=1)["files"][0]["path"], "calculator.py")
        self.assertEqual(code_map(self.repo, "calculator", limit=1)["files"][0]["path"], "calculator.py")
        cached = _code_index.cache_info()
        mapping_cached = _code_map_cached.cache_info()
        self.assertEqual(cached.misses, before.misses + 1)
        self.assertEqual(mapping_cached.misses, mapping_before.misses + 1)
        self.assertGreater(mapping_cached.hits, mapping_before.hits)
        (self.repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        self.assertEqual(code_map(self.repo, "calculator", limit=1)["files"][0]["path"], "calculator.py")
        self.assertEqual(_code_index.cache_info().misses, cached.misses + 1)
        self.assertEqual(_code_map_cached.cache_info().misses, mapping_cached.misses + 1)

    def test_evaluation_baseline_cache_is_project_and_config_scoped(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        (candidate / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        image_id = "sha256:" + "a" * 64
        calls = []
        lock = threading.Lock()
        def check(_sandbox, check, workspace, *args, **kwargs):
            with lock:
                calls.append((Path(workspace).name, check["name"], _sandbox.config["sandbox"]["image"]))
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.0, "error": None}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="docker"), \
                patch("adaptive_rsi.evaluator.run_process", return_value={
                    "exit_code": 0, "error": None, "stdout": image_id, "stderr": ""}), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            for directory in ["one", "two"]:
                result = evaluator.evaluate(self.repo, candidate, CHECKS, self.root / directory, config,
                                            cache_scope="project-a", cache_base=base, environment_digest="deps-v1")
            self.assertTrue(result["candidate_full_reused"])
            self.assertEqual(sum(label == "baseline" for label, _, _ in calls), 1)
            changed_checks = [{"name": "different", "argv": ["python3", "-c", "print('ok')"]}]
            result = evaluator.evaluate(self.repo, candidate, changed_checks, self.root / "three", config,
                                        cache_scope="project-a", cache_base=base, environment_digest="deps-v1")
            self.assertFalse(result["candidate_full_reused"])
            self.assertEqual(sum(label == "baseline" for label, _, _ in calls), 2)
            evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "four", config,
                               cache_scope="project-b", cache_base=base, environment_digest="deps-v1")
            self.assertEqual(sum(label == "baseline" for label, _, _ in calls), 3)
            evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "five", config,
                               cache_scope="project-a", cache_base=base, environment_digest="deps-v2")
            self.assertEqual(sum(label == "baseline" for label, _, _ in calls), 4)
            self.assertEqual(sum(label == "candidate" for label, _, _ in calls), 5)
            full_runs = sum(label == "candidate_full" for label, _, _ in calls)
            self.assertGreater(full_runs, 0)
            self.assertLess(full_runs, sum(label == "candidate" for label, _, _ in calls))
            self.assertTrue(all(image == image_id for _, _, image in calls))

    def test_test_first_evaluation_blocks_changed_harness_before_running_checks(self):
        evaluator = Evaluator(self.store)
        with patch.object(evaluator, "_execution_environment", return_value=(self.runtime.config(self.project), None)), \
                patch("adaptive_rsi.evaluator.Sandbox.check") as check:
            result = evaluator.evaluate(
                self.repo, self.repo, CHECKS, self.root / "test-first", self.runtime.config(self.project),
                test_first=True, changed_files=["calculator.py", "pyproject.toml", "tests/test_behavior.py"])
        self.assertEqual(result["decision"], "insufficient_evidence")
        self.assertIn("test harness", result["reason"])
        check.assert_not_called()

    def test_performance_acceptance_remeasures_baseline_on_repeated_runs(self):
        acceptance = {"spec": {"files": [], "checks": []}, "checks": CHECKS, "criteria": [], "hash": "benchmark",
                      "change_kind": "performance", "contract": {"threshold": {
                          "metric": "latency", "unit": "ms", "target": 5, "tolerance": 1,
                          "direction": "lower"}}}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        identity = {"backend": "docker", "image_id": "sha256:" + "a" * 64}
        labels = []
        def stage(label, *args):
            labels.append(label)
            value = 10 if label == "acceptance_baseline" else 4
            return {"rows": [{"passed": True, "exit_code": 0, "error": None, "stderr": "",
                              "stdout": json.dumps({"metric": "latency", "unit": "ms", "value": value})}]}
        with patch.object(evaluator, "_execution_environment", return_value=(config, identity)), \
                patch.object(evaluator, "_acceptance_stage", side_effect=stage), \
                patch.object(evaluator, "_standard_stage", side_effect=stage):
            for name in ["first", "second"]:
                directory = self.root / name
                early = evaluator.evaluate_acceptance_baseline(
                    self.repo, acceptance, directory, config, cache_scope=self.project, cache_base="base")
                result = evaluator.evaluate(self.repo, self.repo, CHECKS, directory, config,
                    acceptance=acceptance, acceptance_baseline=early,
                    cache_scope=self.project, cache_base="base")
                self.assertEqual(result["decision"], "pass")
                self.assertEqual(result["improvement"]["before"], 10)
                self.assertEqual(result["improvement"]["after"], 4)
        self.assertEqual(labels.count("acceptance_baseline"), 2)

    def test_baseline_cache_invalidates_when_same_tag_image_id_changes(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        first = "sha256:" + "a" * 64
        second = "sha256:" + "b" * 64
        calls = []
        def check(_sandbox, check, workspace, *args, **kwargs):
            calls.append((Path(workspace).name, _sandbox.config["sandbox"]["image"]))
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.0, "error": None}
        images = iter([first, first, second])
        def inspect(*args, **kwargs):
            return {"exit_code": 0, "error": None, "stdout": next(images), "stderr": ""}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="docker"), \
                patch("adaptive_rsi.evaluator.run_process", side_effect=inspect), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            for directory in ["one", "two", "three"]:
                evaluator.evaluate(self.repo, candidate, CHECKS, self.root / directory, config,
                                   cache_scope="project", cache_base=base, environment_digest="deps")
        baselines = [image for label, image in calls if label == "baseline"]
        self.assertEqual(baselines, [first, second])

    def test_baseline_cache_is_disabled_for_unidentified_native_toolchains(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        calls = []
        def check(_sandbox, check, workspace, *args, **kwargs):
            calls.append(Path(workspace).name)
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.0, "error": None}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="seatbelt"), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            for directory in ["one", "two"]:
                result = evaluator.evaluate(self.repo, candidate, CHECKS, self.root / directory, config,
                                            cache_scope="project", cache_base=base, environment_digest="deps")
        self.assertEqual(calls.count("baseline"), 2)
        self.assertIsNone(result["execution_environment"])

    def test_baseline_cache_invalidates_when_evaluator_build_changes(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        image_id = "sha256:" + "a" * 64
        calls = []
        def check(_sandbox, check, workspace, *args, **kwargs):
            calls.append(Path(workspace).name)
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.0, "error": None}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="docker"), \
                patch("adaptive_rsi.evaluator.run_process", return_value={
                    "exit_code": 0, "error": None, "stdout": image_id, "stderr": ""}), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "one", config,
                               cache_scope="project", cache_base=base, environment_digest="deps")
            with patch("adaptive_rsi.evaluator.BUILD_ID", "different-build"):
                evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "two", config,
                                   cache_scope="project", cache_base=base, environment_digest="deps")
        self.assertEqual(calls.count("baseline"), 2)

    def test_early_acceptance_baseline_reruns_when_docker_image_id_changes(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        first = "sha256:" + "a" * 64
        second = "sha256:" + "b" * 64
        acceptance = {
            "spec": {"files": [], "checks": []},
            "checks": [{"name": "acceptance", "cwd": ".", "argv": ["true"]}],
            "criteria": [],
            "hash": "acceptance-hash",
        }
        calls = []
        def check(_sandbox, check, workspace, *args, **kwargs):
            label = Path(workspace).name
            calls.append((label, _sandbox.config["sandbox"]["image"]))
            failed = label.startswith("acceptance_baseline")
            return {"argv": check["argv"], "name": check["name"], "exit_code": 1 if failed else 0,
                    "stdout": "Ran 1 test\n", "stderr": "AssertionError" if failed else "",
                    "seconds": 0.0, "error": None}
        images = iter([first, second])
        def inspect(*args, **kwargs):
            return {"exit_code": 0, "error": None, "stdout": next(images), "stderr": ""}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        directory = self.root / "evaluation"
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="docker"), \
                patch("adaptive_rsi.evaluator.run_process", side_effect=inspect), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            early = evaluator.evaluate_acceptance_baseline(
                self.repo, acceptance, directory, config,
                cache_scope="project", cache_base=base, environment_digest="deps")
            result = evaluator.evaluate(
                self.repo, candidate, CHECKS, directory, config, acceptance=acceptance,
                acceptance_baseline=early, cache_scope="project", cache_base=base,
                environment_digest="deps")
        baseline_runs = [(label, image) for label, image in calls if label.startswith("acceptance_baseline")]
        self.assertEqual([image for _, image in baseline_runs], [first, second])
        self.assertTrue(baseline_runs[1][0].startswith("acceptance_baseline_"))
        self.assertEqual(result["decision"], "pass")

    def test_evaluation_runs_independent_stages_concurrently(self):
        candidate = snapshot(self.repo, self.root / "candidate")
        active = 0
        maximum = 0
        lock = threading.Lock()
        def check(_sandbox, check, workspace, *args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.05, "error": None}
        with patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            result = Evaluator(self.store).evaluate(
                self.repo, candidate, CHECKS, self.root / "parallel",
                merge(self.runtime.config(self.project), {"limits": {"concurrency": 2}}))
        self.assertEqual(result["decision"], "pass")
        self.assertGreaterEqual(maximum, 2)

    def test_evaluation_obeys_concurrency_one(self):
        candidate = snapshot(self.repo, self.root / "candidate")
        active = 0
        maximum = 0
        lock = threading.Lock()
        def check(_sandbox, check, workspace, *args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return {"argv": check["argv"], "name": check["name"], "exit_code": 0,
                    "stdout": "Ran 1 test\n", "stderr": "", "seconds": 0.03, "error": None}
        with patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            result = Evaluator(self.store).evaluate(
                self.repo, candidate, CHECKS, self.root / "serial",
                merge(self.runtime.config(self.project), {"limits": {"concurrency": 1}}))
        self.assertEqual(result["decision"], "pass")
        self.assertEqual(maximum, 1)

    def test_compact_baseline_cache_retains_failure_evidence_fields(self):
        evaluator = Evaluator(self.store)
        evaluator._cache_put("project", "key", [{
            "name": "acceptance", "argv": ["python3", "check.py"], "cwd": ".",
            "passed": False, "exit_code": 1, "error": None,
            "stdout": "x" * 100000, "stderr": "ModuleNotFoundError: missing dependency",
            "empty_tests": False, "seconds": 1.0}])
        cached = evaluator._cache_get("project", "key")
        self.assertNotIn("stdout", cached[0])
        self.assertEqual(cached[0]["exit_code"], 1)
        self.assertIn("ModuleNotFoundError", cached[0]["stderr"])
        self.assertEqual(improvement(
            {"acceptance_baseline": cached, "acceptance_candidate": [{"passed": True}]},
            {"change_kind": "behavior"})["decision"], "insufficient_evidence")
        evaluator._cache_put("project", "long-environment-error", [{
            "name": "acceptance", "argv": ["python3", "check.py"], "cwd": ".",
            "passed": False, "exit_code": 1, "error": None,
            "stdout": "ImportError: private module\n" + ("x" * (CACHE_STDERR_LIMIT + 1024)),
            "stderr": "", "empty_tests": False, "seconds": 1.0}])
        long_cached = evaluator._cache_get("project", "long-environment-error")
        self.assertTrue(long_cached[0]["environment_error"])
        self.assertEqual(improvement(
            {"acceptance_baseline": long_cached, "acceptance_candidate": [{"passed": True}]},
            {"change_kind": "behavior"})["decision"], "insufficient_evidence")
        evaluator._cache_put("project", "timed-out", [{
            "name": "slow", "argv": ["sleep"], "passed": False, "exit_code": 0,
            "error": "Command timed out", "stderr": "", "stdout": ""}])
        self.assertIsNone(evaluator._cache_get("project", "timed-out"))

    def test_cached_baseline_keeps_unavailable_candidate_detection_intact(self):
        base = git(self.repo, "rev-parse", "HEAD")
        candidate = snapshot(self.repo, self.root / "candidate")
        image_id = "sha256:" + "a" * 64
        unavailable = [False]
        calls = []
        def check(_sandbox, check, workspace, *args, **kwargs):
            label = Path(workspace).name
            calls.append(label)
            missing = unavailable[0] and label in ["candidate", "candidate_full"]
            return {"argv": check["argv"], "name": check["name"], "exit_code": 127 if missing else 0,
                    "stdout": "" if missing else "Ran 1 test\n",
                    "stderr": "execvp(): No such file or directory" if missing else "",
                    "seconds": 0.0, "error": None}
        evaluator = Evaluator(self.store)
        config = self.runtime.config(self.project)
        with patch("adaptive_rsi.evaluator.Sandbox.backend", return_value="docker"), \
                patch("adaptive_rsi.evaluator.run_process", return_value={
                    "exit_code": 0, "error": None, "stdout": image_id, "stderr": ""}), \
                patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=check):
            first = evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "available", config,
                                       cache_scope="project", cache_base=base, environment_digest="deps")
            self.assertEqual(first["decision"], "pass")
            unavailable[0] = True
            second = evaluator.evaluate(self.repo, candidate, CHECKS, self.root / "unavailable", config,
                                        cache_scope="project", cache_base=base, environment_digest="deps")
        self.assertEqual(second["decision"], "insufficient_evidence")
        self.assertIn("unavailable", second["reason"])
        self.assertEqual(calls.count("baseline"), 1)

    def import_cases(self):
        for split in ["validation", "holdout"]:
            Evolution(self.runtime).import_case(self.project, {"family": split, "split": split,
                "repository": str(self.repo), "base": "HEAD", "task": split + " addition", "checks": CHECKS, "source": "synthetic"})

    def test_experiment_preflight_does_not_spend_when_budget_or_cases_are_missing(self):
        task, run = self.claim()
        directory = self.root / "experiment"
        directory.mkdir()
        result = Evolution(self.runtime).propose_and_evaluate(self.runtime.project(self.project), self.repo, directory, run)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(self.runtime.agent.roles, [])
        self.assertEqual(self.store.query("SELECT * FROM calls"), [])

    def test_validation_failure_never_exposes_holdout_and_uses_production_path(self):
        self.import_cases()
        self.runtime.configure({"limits": {"daily_calls": 100}, "evolution": {"min_cases_per_split": 1, "max_calls": 100}}, self.project)
        task, run = self.claim()
        directory = self.root / "experiment"
        directory.mkdir()
        def execute(run, project, task, *args, **kwargs):
            self.assertTrue(kwargs["experiment"])
            self.assertIn("validation", task["body"])
            call = self.store.reserve_call(self.project, run["id"], "implementation", DEFAULTS["models"]["implementation"], json.loads(run["config"]))
            self.store.execute("UPDATE calls SET tokens=100,state='completed' WHERE id=?", (call,))
            return {"decision": "fail", "reason": "No demonstrated gain"}
        with patch.object(self.runtime, "execute_candidate", side_effect=execute) as pipeline:
            result = Evolution(self.runtime).propose_and_evaluate(self.runtime.project(self.project), self.repo, directory, run)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(pipeline.call_count, 4)
        self.assertEqual(self.store.one("SELECT uses FROM cases WHERE split='holdout'")["uses"], 0)

    def test_monitor_ignores_unrelated_and_infrastructure_failures(self):
        evidence = {"compatibility": compatibility(self.runtime.config(self.project)), "monitor": {"passed": 0, "failed": 0}}
        for candidate in ["used", "unused"]:
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,'stable','{}',?,?)",
                               (candidate, self.project, candidate, encode(evidence), time.time()))
        task, run = self.claim()
        self.store.execute("UPDATE runs SET strategy=? WHERE id=?", (encode([{"id": "used"}]), run["id"]))
        evolution = Evolution(self.runtime)
        evolution.monitor(self.project, False, run, attributable=False)
        self.assertEqual(len(self.store.query("SELECT * FROM candidates WHERE status='stable'")), 2)
        evolution.monitor(self.project, False, run, attributable=True)
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='used'")["status"], "rolled_back")
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='unused'")["status"], "stable")

    def test_context_and_deduplication_use_evidence_without_collapsing_unrelated_titles(self):
        mapping = code_map(self.repo, "calculator", limit=1)
        self.assertEqual(mapping["files"][0]["path"], "calculator.py")
        self.assertTrue(mapping["files"][0]["anchors"])
        rows = [{"content": "addition lesson", "triggers": ["add"], "created": 1}]
        self.assertEqual(len(related(rows, "add")), 1)
        finding = {"title": "Wrong addition", "path": "calculator.py", "quote": "a - b"}
        backlog = [{"id": "a", "title": "Wrong addition", "evidence": "[]"}]
        self.assertIsNone(related_task(backlog, finding))
        backlog[0]["evidence"] = encode([finding])
        self.assertEqual(related_task(backlog, finding), "a")

    def test_chinese_goals_find_relevant_code_by_content(self):
        (self.repo / "login.py").write_text("def login(user):\n    # 登录失败时清理旧会话\n    return user is not None\n")
        git(self.repo, "add", "login.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "login")
        self.assertIn("登录", terms("修复登录失败"))
        mapping = code_map(self.repo, "修复登录失败", limit=3)
        self.assertEqual(mapping["files"][0]["path"], "login.py")
        self.assertTrue(any(anchor["line"] == 2 for anchor in mapping["files"][0]["anchors"]))
        self.assertLessEqual(len(terms("登录失败" * 6000)), 512)

    def test_prepared_environment_is_reused_on_the_normal_pipeline_path(self):
        (self.repo / "deps").mkdir()
        (self.repo / "deps/value.txt").write_text("sealed dependency")
        self.runtime.configure({"environment": {"seed_paths": ["deps"]}}, self.project)
        task, run = self.claim("Fix addition")
        with patch.object(self.runtime.evaluator, "evaluate", return_value={
                "decision": "pass", "reason": "Fixed checks passed", "checks": {}}) as evaluate:
            self.runtime.process(run)
            second_task, second_run = self.claim("Correct addition again")
            self.runtime.process(second_run)
        self.assertEqual(evaluate.call_count, 2)
        messages = [row["message"] for row in self.store.query(
            "SELECT message FROM events WHERE project=? AND kind='environment' ORDER BY seq", (self.project,))]
        self.assertIn("Sealed dependency preparation frozen", messages)
        self.assertIn("Sealed dependency preparation reused", messages)
        self.assertEqual((Path(self.store.one("SELECT path FROM runs WHERE id=?", (run["id"],))["path"])
                          / "workspace/deps/value.txt").read_text(), "sealed dependency")
        self.assertEqual((Path(self.store.one("SELECT path FROM runs WHERE id=?", (run["id"],))["path"])
                          / "dependencies/deps/value.txt").read_text(), "sealed dependency")

    def test_cached_rejected_review_is_invalidated_without_reuse_event(self):
        from types import SimpleNamespace
        from adaptive_rsi.recovery import cached, save
        task, _ = self.claim()
        for identifier in ("prior-review-run", "next-review-run"):
            self.store.execute("INSERT INTO runs(id,task,project,state,config,started,ended) "
                               "VALUES (?,?,?,'failed','{}',1,2)", (identifier, task, self.project))
        runtime = SimpleNamespace(store=self.store)
        save(runtime, {"id": "prior-review-run"}, "review", {"decision": "fail"}, "review-signature")
        accept = lambda value: value.get("decision") == "pass"
        self.assertIsNone(cached(runtime, {"id": "next-review-run", "task": task, "project": self.project},
                                 "review", "review-signature", accept=accept))
        events = self.store.query("SELECT kind,data FROM events WHERE run='next-review-run' ORDER BY seq")
        self.assertNotIn("stage_reused", [row["kind"] for row in events])
        invalid = [json.loads(row["data"]) for row in events if row["kind"] == "stage_invalidated"]
        self.assertEqual(invalid[-1]["reason"], "result_requires_rework")
        save(runtime, {"id": "prior-review-run"}, "review", {"decision": "pass"}, "review-signature")
        self.assertEqual(cached(runtime, {"id": "next-review-run", "task": task, "project": self.project},
                                "review", "review-signature", accept=accept)["decision"], "pass")

    def test_reused_dirty_review_record_only_invalidates_its_own_stage(self):
        class RejectOnce(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "review" and not getattr(self, "reviewed", False):
                    self.reviewed = True
                    result["decision"] = "fail"
                    result["reason"] = "Rejected to create a retryable failed run"
                return result
        self.runtime.agent = RejectOnce()
        task, run = self.claim()
        baseline = (str(self.repo), git(self.repo, "rev-parse", "HEAD"), "main")
        # 恢复只复用完整且来源一致的评估记录，桩数据需具备真实记录的判定、
        # 检查明细和 harness 指纹，否则会被当作不完整记录重新评价。
        evaluation = {"decision": "pass", "reason": "Fixed checks passed", "harness_hash": "stub",
                      "checks": {"candidate": [{"name": "real unittest regression", "cwd": ".",
                                                "argv": ["python3", "-m", "unittest"], "passed": True,
                                                "exit_code": 0, "error": None}]}}
        with patch("adaptive_rsi.delivery.baseline", return_value=baseline), \
             patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
             patch.object(self.runtime.evaluator, "evaluate", return_value=evaluation):
            self.runtime.process(run)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "failed")
            result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
            self.assertEqual(result["review"]["decision"], "fail")
            # 模拟历史脏记录：schema 内但缺必需字段，归一化后仍无法通过校验；
            # accept 只看 decision，脏记录伪装成 pass 仍然会被复用路径拦下。
            dirty = {key: value for key, value in result["review"].items() if key != "findings"}
            dirty["decision"] = "pass"
            self.store.execute("UPDATE stages SET data=? WHERE run=? AND stage='review'", (encode(dirty), run["id"]))
            self.runtime.retry(task)
            second = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(second)
            # 单条脏记录只作废评审项；其余阶段照常复用，恢复不被中断。
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")
            events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (second["id"],))
            invalid = [json.loads(row["data"]) for row in events if row["kind"] == "stage_invalidated"]
            invalidated = [row for row in invalid if row["reason"] == "reused_record_invalid"]
            self.assertEqual([row["stage"] for row in invalidated], ["review"])
            self.assertTrue(invalidated[0]["from_run"])
            self.assertIn("schema", invalidated[0]["error"])
            reused = [json.loads(row["data"])["stage"] for row in events if row["kind"] == "stage_reused"]
            self.assertIn("planning", reused)
            # 脏记录在复用事件之前就被拦下，不会让用量统计虚增复用阶段。
            self.assertNotIn("review", reused)
            review = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (second["id"],))["result"])["review"]
            self.assertEqual(review["decision"], "pass")
            self.assertIn("findings", review)
        # 脏记录被作废后重新评审；取消候选后重试，修正后的记录照常复用，不再重跑评审角色。
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("review"), 2)
        self.runtime.cancel_task(task)
        self.runtime.retry(task)
        third = self.store.claim(self.project, self.runtime.config(self.project))
        with patch("adaptive_rsi.delivery.baseline", return_value=baseline), \
             patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
             patch.object(self.runtime.evaluator, "evaluate", return_value=evaluation):
            self.runtime.process(third)
        third_events = self.store.query("SELECT kind,data FROM events WHERE run=? ORDER BY seq", (third["id"],))
        third_reused = [json.loads(row["data"])["stage"] for row in third_events if row["kind"] == "stage_reused"]
        self.assertIn("review", third_reused)
        self.assertIn("validated_candidate", third_reused)
        self.assertNotIn("reused_record_invalid", [json.loads(row["data"]).get("reason") for row in third_events])
        self.assertEqual([r[0] for r in self.runtime.agent.roles].count("review"), 2)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")

    def test_stored_review_records_are_renormalized_before_reuse(self):
        from adaptive_rsi.recovery import enforce_cached_review
        from adaptive_rsi.review import enforce_review
        review = enforce_review(self.runtime.agent.invoke("review", "", None, None, None, DEFAULTS), DEFAULTS)
        self.assertEqual(review["decision"], "pass")
        # 落库形态（含派生字段）先被归一化回 schema 字段，再重新校验，健康记录照常复用。
        healthy = enforce_cached_review(review, DEFAULTS)
        self.assertEqual(healthy["decision"], "pass")
        self.assertEqual(healthy["reported_decision"], "pass")
        # 自称通过但含阻断性发现：重新推导的结论优先，旧记录不能放宽门禁。
        stale = {**review, "findings": [{"severity": "critical", "area": "correctness", "path": "calculator.py",
                                         "line": 2, "title": "Wrong addition", "reason": "add subtracts",
                                         "suggestion": "Use addition"}]}
        self.assertEqual(enforce_cached_review(stale, DEFAULTS)["decision"], "fail")
        # 无法重建的脏记录继续抛错，由 cached() 捕获后只作废该项。
        dirty = {key: value for key, value in review.items() if key != "findings"}
        with self.assertRaises(RuntimeError):
            enforce_cached_review(dirty, DEFAULTS)

    def test_documentation_plan_cannot_change_source_even_with_real_scope(self):
        class Drift(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                if role == "planning":
                    result = super().invoke(role, *args, **kwargs)
                    result.update(change_kind="documentation", scope=["calculator.py", "docs"])
                    return result
                if role == "implementation":
                    (args[1] / "docs").mkdir(exist_ok=True)
                    (args[1] / "docs/guide.md").write_text("guide\n")
                    (args[1] / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
                    return {"summary": "Documented the arithmetic contract"}
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Drift()
        task, run = self.claim("Update usage guide")
        with patch.object(self.runtime.evaluator, "evaluate", wraps=self.runtime.evaluator.evaluate) as evaluate:
            self.runtime.process(run)
        row = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        result = json.loads(row["result"])
        self.assertEqual(row["state"], "insufficient_evidence")
        self.assertIn("documentation", result["reason"].lower())
        self.assertEqual(evaluate.call_count, 0)
        self.assertNotIn("review", [role for role, _ in self.runtime.agent.roles])

    def test_documentation_change_still_runs_fixed_checks(self):
        class Docs(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                if role == "planning":
                    result = super().invoke(role, *args, **kwargs)
                    result.update(change_kind="documentation", scope=["docs"])
                    return result
                if role == "implementation":
                    (args[1] / "docs").mkdir(exist_ok=True)
                    (args[1] / "docs/guide.md").write_text("guide\n")
                    return {"summary": "Documented the arithmetic contract"}
                return super().invoke(role, *args, **kwargs)
        self.runtime.agent = Docs()
        task, run = self.claim("Update usage guide")
        with patch.object(self.runtime.evaluator, "evaluate", return_value={
                "decision": "pass", "reason": "Fixed checks passed", "checks": {}}) as evaluate:
            self.runtime.process(run)
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(evaluate.call_args.args[2], CHECKS)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"],
                         "awaiting_approval")

    def test_stage_cache_keys_only_change_for_relevant_settings(self):
        config = self.runtime.config(self.project)
        before, _ = _stage_config(config, "planning")
        changed = json.loads(json.dumps(config))
        changed["models"]["implementation"]["effort"] = "high"
        after, _ = _stage_config(changed, "planning")
        self.assertEqual(fingerprint(before), fingerprint(after))
        implementation_before, _ = _stage_config(config, "implementation")
        implementation_after, _ = _stage_config(changed, "implementation")
        self.assertNotEqual(fingerprint(implementation_before), fingerprint(implementation_after))

    def test_review_evidence_keeps_results_without_full_check_transcripts(self):
        evaluation = {"decision": "pass", "checks": {"candidate": [{
            "name": "tests", "argv": ["python3", "-m", "unittest"], "passed": True,
            "stdout": "x" * 100000, "stderr": "noise"}]}}
        evidence = _review_evidence(evaluation)
        self.assertTrue(evidence["checks"]["candidate"][0]["passed"])
        self.assertNotIn("stdout", evidence["checks"]["candidate"][0])
        self.assertNotIn("stderr", evidence["checks"]["candidate"][0])

    def test_documentation_commit_does_not_repeat_module_inspection(self):
        self.assertFalse(Runtime._module_changed(None, {"README.md", "docs/diagram.png"}))
        self.assertTrue(Runtime._module_changed(None, {"desktop/icon.svg"}))
        self.assertTrue(Runtime._module_changed(
            {"cwd": ".", "language": "Python", "manifest": "pyproject.toml"},
            {"scripts/install-server.sh"}))
        self.runtime.configure({"sources": {"inspection": True}, "evolution": {"enabled": False}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "light")
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        (self.repo / "README.md").write_text("documentation only\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "docs")
        self.runtime.request(self.project, "light")
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("analysis"), 1)

    def test_due_scan_yields_to_an_existing_repair_backlog(self):
        self.runtime.configure({"sources": {"inspection": True, "issues": False}}, self.project)
        self.store.add_task(self.project, "goal", "repair", "Repair existing defect")
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=100)
        task = self.store.one("SELECT * FROM tasks WHERE source='system_light'")
        self.assertEqual(task["priority"], 55)
        self.store.execute("UPDATE tasks SET state='completed' WHERE fingerprint='repair'")
        self.store.execute("UPDATE tasks SET state='completed' WHERE source='system_light'")
        self.store.execute("DELETE FROM schedules")
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=200)
        self.assertEqual(self.store.query("SELECT * FROM tasks WHERE source='system_light' ORDER BY created")[-1]["priority"], 90)

    def test_readiness_separates_configuration_tool_tests_and_real_tasks(self):
        config = self.runtime.config(self.project)
        proof = config["models"]["planning"]
        provider = next(item for item in BUILTIN_PROVIDERS if item["id"] == proof["provider"])
        self.store.put_setting("task-proof-current:" + self.project, {"planning": {
            **proof, "signature": self.runtime.model_center.signature(provider),
            "at": time.time(), "base": git(self.repo, "rev-parse", "HEAD")}})
        result = readiness(self.runtime, self.runtime.project(self.project), config)
        evidence = {row["role"]: row for row in result["model_evidence"]}
        self.assertTrue(evidence["planning"]["configured"])
        self.assertTrue(evidence["planning"]["task_verified"])
        self.assertFalse(evidence["implementation"]["task_verified"])

        changed_provider = {**provider, "base_url": "https://changed.example.invalid"}
        self.store.put_setting("task-proof-current:" + self.project, {"planning": {
            **proof, "signature": self.runtime.model_center.signature(changed_provider),
            "at": time.time(), "base": git(self.repo, "rev-parse", "HEAD")}})
        evidence = {row["role"]: row for row in readiness(
            self.runtime, self.runtime.project(self.project), config)["model_evidence"]}
        self.assertFalse(evidence["planning"]["task_verified"])


class EvidenceContractTest(unittest.TestCase):
    def test_behavior_configuration_and_ui_changes_use_test_first_unless_high_risk(self):
        for kind in ["behavior", "configuration", "ui"]:
            with self.subTest(kind=kind):
                self.assertTrue(_uses_test_first(kind, "low"))
                self.assertFalse(_uses_test_first(kind, "high"))
        for kind in ["performance", "documentation", "refactor"]:
            with self.subTest(kind=kind):
                self.assertFalse(_uses_test_first(kind, "low"))

    def results(self, before, after, **extras):
        return {"acceptance_baseline": [{"passed": before, "error": "", "exit_code": 0 if before else 1, "stderr": "AssertionError", **extras}],
                "acceptance_candidate": [{"passed": after}]}

    def test_documentation_paths_are_explicit_and_mixed_source_is_not_documentation(self):
        from adaptive_rsi.workflow import documentation_only
        self.assertTrue(documentation_only(["docs/guide.txt", "docs/image.svg", "README.md",
                                            "CHANGELOG.en.md", "LICENSE", "LICENSE.txt", "README"]))
        self.assertFalse(documentation_only([]))
        self.assertFalse(documentation_only(["calculator.py"]))
        self.assertFalse(documentation_only(["docs/guide.md", "calculator.py"]))
        self.assertFalse(documentation_only(["docs/../calculator.py"]))
        self.assertFalse(documentation_only(["/docs/guide.md"]))
        self.assertFalse(documentation_only(["docs\\calculator.py"]))
        for path in ["docs/calculator.py", "docs/build.js", "docs/config.toml", "README.py", "docs/Makefile"]:
            with self.subTest(path=path):
                self.assertFalse(documentation_only([path]))

    def test_documentation_boundary_sees_rename_source_paths(self):
        from adaptive_rsi.pipeline import _touched_paths
        from adaptive_rsi.workflow import documentation_only
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            (repo / "calculator.py").write_text("VALUE = 1\n")
            git(repo, "add", ".")
            git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
            (repo / "docs").mkdir()
            git(repo, "mv", "calculator.py", "docs/calculator.py")
            touched = _touched_paths(repo)
            self.assertIn("calculator.py", touched)
            self.assertIn("docs/calculator.py", touched)
            self.assertFalse(documentation_only(touched))

    def test_review_identity_excludes_timing_but_keeps_full_result_identity(self):
        from adaptive_rsi.pipeline import _review_key
        config = merge(DEFAULTS, {})
        task = {"title": "Fix addition", "body": "", "source": "goal", "evidence": "[]", "acceptance": "[]"}
        run = {"id": "review-run", "config": json.dumps(config)}
        plan = {"scope": ["calculator.py"]}
        def evaluation(seconds, executed):
            return {"decision": "pass", "reason": "Checks passed", "harness_hash": "harness",
                    "execution_environment": {"build": "build", "backend": "seatbelt"},
                    "host_tools": {"python3": {"state": "available", "version": "3.14.0",
                                               "binary_sha256": "binary"}},
                    "checks": {"candidate": [{
                        "name": "tests", "argv": ["python3", "-m", "unittest"], "cwd": ".",
                        "passed": True, "exit_code": 0, "error": None, "seconds": seconds,
                        "test_summary": {"format": "pytest_cli", "executed": 1},
                        "test_identities": {"state": "reported", "executed": executed, "skipped": []},
                        "artifacts": {"junit.xml": {"state": "collected", "bytes": 10,
                                                    "sha256": "artifact"}}}]}}
        first = _review_key(run, task, "base", [], config, plan,
                            evaluation(0.1, ["tests.test_add"]), "summary", "diff")
        second = _review_key(run, task, "base", [], config, plan,
                             evaluation(9.9, ["tests.test_add"]), "summary", "diff")
        self.assertEqual(first, second)
        changed_tests = _review_key(run, task, "base", [], config, plan,
                                    evaluation(0.1, ["tests.test_add", "tests.test_other"]),
                                    "summary", "diff")
        self.assertNotEqual(first, changed_tests)
        changed_patch = _review_key(run, task, "base", [], config, plan,
                                    evaluation(0.1, ["tests.test_add"]), "summary", "different")
        self.assertNotEqual(first, changed_patch)
        diagnostic = evaluation(0.1, ["tests.test_add"])
        diagnostic["checks"]["candidate"][0]["stderr"] = "new diagnostic"
        self.assertNotEqual(first, _review_key(run, task, "base", [], config, plan,
                                             diagnostic, "summary", "diff"))

    def test_rejected_review_cache_is_not_reusable(self):
        from adaptive_rsi.pipeline import _approved_review
        self.assertFalse(_approved_review(None))
        self.assertFalse(_approved_review({"decision": "fail"}))
        self.assertFalse(_approved_review({"decision": "insufficient_evidence"}))
        self.assertTrue(_approved_review({"decision": "pass"}))

    def test_no_gain_and_environment_failure_are_not_improvements(self):
        self.assertEqual(improvement(self.results(True, True), {})["decision"], "not_requested")
        acceptance = {"change_kind": "behavior"}
        self.assertEqual(improvement(self.results(True, True), acceptance)["decision"], "insufficient_evidence")
        self.assertEqual(improvement(self.results(False, True), acceptance)["decision"], "pass")
        self.assertEqual(improvement(self.results(False, True, stderr="ModuleNotFoundError"), acceptance)["decision"], "insufficient_evidence")
        self.assertEqual(improvement(self.results(True, True), {"change_kind": "refactor"})["decision"], "pass")

    def test_test_first_requires_a_real_isolated_test_exit_status_pair(self):
        before = {"passed": False, "empty_tests": False, "error": None, "exit_code": 1,
                  "stderr": "AssertionError", "name": "isolated regression: project tests",
                  "argv": ["python3", "-m", "unittest", "tests.test_behavior"], "cwd": "."}
        after = {**before, "passed": True, "exit_code": 0, "stderr": ""}
        contract = {"mode": "test_first", "change_kind": "behavior",
                    "regression_test_files": ["tests/test_behavior.py"]}
        focused = {"test_first_baseline": [before], "test_first_candidate": [after]}
        self.assertEqual(improvement(focused, contract)["decision"], "pass")
        self.assertEqual(improvement(focused, {**contract, "regression_test_files": []})["decision"],
                         "insufficient_evidence")
        unisolated = {"baseline": [{**before, "name": "project tests", "stderr":
                                    "FAIL: test_behavior (tests.test_behavior.BehaviorTests.test_behavior)"}],
                      "candidate": [{**after, "name": "project tests"}]}
        self.assertEqual(improvement(unisolated, contract)["decision"], "insufficient_evidence")
        mismatched = {"test_first_baseline": [before], "test_first_candidate": [{**after, "argv": ["pytest", "-q"]}]}
        self.assertEqual(improvement(mismatched, contract)["decision"], "insufficient_evidence")
        spoofed_success = {"test_first_baseline": [{**before, "exit_code": 0, "stderr": "FAIL: fake"}],
                           "test_first_candidate": [after]}
        self.assertEqual(improvement(spoofed_success, contract)["decision"], "insufficient_evidence")
        environment_error = {"test_first_baseline": [{**before, "stderr": "ModuleNotFoundError: pytest"}],
                             "test_first_candidate": [after]}
        self.assertEqual(improvement(environment_error, contract)["decision"], "insufficient_evidence")

    def test_isolated_test_check_builds_targeted_commands_only_for_supported_runners(self):
        root = Path("/project")
        self.assertEqual(_isolated_test_check(
            [{"name": "unit", "cwd": ".", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]}],
            root, "tests/test_new.py")["argv"],
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "tests", "-p", "test_new.py"])
        self.assertEqual(_isolated_test_check(
            [{"name": "node", "cwd": ".", "argv": ["node", "--test"]}],
            root, "tests/new.test.js")["argv"], ["node", "--test", "tests/new.test.js"])
        self.assertEqual(_isolated_test_check(
            [{"name": "node", "cwd": ".", "argv": ["node", "--test", "--require", "./preload.cjs", "tests"]}],
            root, "tests/new.test.js")["argv"],
            ["node", "--test", "--require", "./preload.cjs", "tests/new.test.js"])
        self.assertTrue(supports_test_first([{"name": "node", "argv": ["node", "--test", "--require", "./preload.cjs"]}]))
        self.assertFalse(supports_test_first([{"name": "node", "argv": ["node", "--test", "--test-name-pattern", "legacy"]}]))
        self.assertTrue(supports_test_first([{"name": "unit", "argv": ["python3", "-m", "unittest",
                                                                            "discover", "-s", "tests"]}]))
        self.assertFalse(supports_test_first([{"name": "unit", "argv": ["python3", "-m", "unittest",
                                                                             "discover", "--unknown"]}]))
        module_check = _isolated_test_check(
            [{"name": "backend", "cwd": "backend", "argv": ["python3", "-m", "pytest", "tests"]},
             {"name": "frontend", "cwd": "frontend", "argv": ["python3", "-m", "pytest", "tests"]}],
            root, "frontend/tests/test_new.py")
        self.assertEqual((module_check["cwd"], module_check["argv"]),
                         ("frontend", ["python3", "-m", "pytest", "tests/test_new.py"]))
        self.assertEqual(_isolated_test_check(
            [{"name": "python", "cwd": "backend", "argv": ["python3", "-m", "pytest", "tests"]},
             {"name": "frontend", "cwd": ".", "argv": ["node", "--test"]}],
            root, "frontend/tests/new.test.js")["argv"],
            ["node", "--test", "frontend/tests/new.test.js"])
        self.assertEqual(_isolated_test_check(
            [{"name": "go", "cwd": ".", "argv": ["go", "test", "./..."]}],
            root, "pkg/new_test.go", ["TestNew"])["argv"],
            ["go", "test", "-run", "^(TestNew)$", "./pkg"])
        self.assertEqual(_isolated_test_check(
            [{"name": "go", "cwd": ".", "argv": ["go", "test", "-tags", "integration", "./..."]}],
            root, "pkg/new_test.go", ["TestNew"])["argv"],
            ["go", "test", "-tags", "integration", "-run", "^(TestNew)$", "./pkg"])
        self.assertIsNone(_isolated_test_check(
            [{"name": "go", "cwd": ".", "argv": ["go", "test", "-unknown", "value", "./..."]}],
            root, "pkg/new_test.go", ["TestNew"]))
        for argv in [["go", "test", "-exec", "true"], ["go", "test", "-test.run=^Legacy$"],
                     ["go", "test", "-c"], ["go", "test", "-list", "."],
                     ["go", "test", "-unknown=value"], ["go", "test", "-skip", "^TestNew$"],
                     ["go", "test", "-skip=^TestNew$"], ["go", "test", "-count", "0"],
                     ["go", "test", "-count=0"], ["go", "test", "-short"]]:
            with self.subTest(argv=argv):
                self.assertFalse(supports_test_first([{"name": "go", "argv": argv}]))
        self.assertEqual(_isolated_test_check(
            [{"name": "phpunit", "cwd": ".", "argv": ["vendor/bin/phpunit", "tests"]}],
            root, "tests/NewTest.php")["argv"], ["vendor/bin/phpunit", "tests/NewTest.php"])

    def test_cargo_workspace_checks_fall_back_to_sealed_acceptance(self):
        for argv in [["cargo", "test", "--workspace"], ["cargo", "test", "--package", "calc"]]:
            with self.subTest(argv=argv):
                self.assertFalse(supports_test_first([{"name": "cargo", "argv": argv}]))

    def test_freeze_harness_restores_or_removes_cargo_runner_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            (baseline / ".cargo").mkdir(parents=True)
            (target / ".cargo").mkdir(parents=True)
            (baseline / ".cargo/config.toml").write_text('[target.x86_64-unknown-linux-gnu]\nrunner = "real"\n')
            (target / ".cargo/config.toml").write_text('[target.x86_64-unknown-linux-gnu]\nrunner = "true"\n')
            freeze_harness(baseline, target)
            self.assertEqual((target / ".cargo/config.toml").read_text(),
                             (baseline / ".cargo/config.toml").read_text())
            nested_config = Path("crates/calc/.cargo/config.toml")
            (baseline / nested_config).parent.mkdir(parents=True)
            (target / nested_config).parent.mkdir(parents=True)
            (baseline / nested_config).write_text('[target.x86_64-unknown-linux-gnu]\nrunner = "real"\n')
            (target / nested_config).write_text('[target.x86_64-unknown-linux-gnu]\nrunner = "true"\n')
            freeze_harness(baseline, target)
            self.assertEqual((target / nested_config).read_text(), (baseline / nested_config).read_text())
            baseline_without_config = root / "baseline-without-config"
            target_with_added_config = root / "target-with-added-config"
            baseline_without_config.mkdir()
            nested_added_config = target_with_added_config / nested_config
            nested_added_config.parent.mkdir(parents=True)
            nested_added_config.write_text('runner = "true"\n')
            freeze_harness(baseline_without_config, target_with_added_config)
            self.assertFalse(nested_added_config.exists())

    def test_test_first_runner_support_is_fail_closed_for_unrecognised_npm_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = {"cwd": ".", "argv": ["npm", "test"]}
            package = root / "package.json"
            package.write_text('{"scripts":{"test":"custom-runner"}}')
            self.assertFalse(supports_test_first([check], root))
            self.assertIsNone(_isolated_test_check([check], root, "tests/new.test.ts"))
            package.write_text('{"scripts":{"test":"vitest run tests"}}')
            self.assertFalse(supports_test_first([check], root))
            self.assertIsNone(_isolated_test_check([check], root, "tests/new.test.ts"))
            package.write_text('{"scripts":{"test":"vitest run"}}')
            self.assertTrue(supports_test_first([{"cwd": ".", "argv": ["npm", "test"]}], root))
            self.assertEqual(_isolated_test_check([check], root, "tests/new.test.ts")["argv"],
                             ["npm", "exec", "--", "vitest", "run", "tests/new.test.ts"])

    def test_unittest_discovery_isolates_one_file_without_losing_helper_imports(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "helpers.py").write_text("VALUE = 42\n")
            (tests / "test_new.py").write_text(
                "import unittest\nimport helpers\n"
                "class RegressionTest(unittest.TestCase):\n"
                "    def test_import_context(self):\n"
                "        self.assertEqual(helpers.VALUE, 42)\n")
            checks = [{"name": "unit", "cwd": ".", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"]}]
            isolated = _isolated_test_check(checks, root, "tests/test_new.py")
            self.assertEqual(isolated["argv"], ["python3", "-m", "unittest", "discover", "-s", "tests",
                                                "-t", "tests", "-p", "test_new.py"])
            result = subprocess.run(isolated["argv"], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_change_kind_keeps_docs_and_performance_contracts_distinct(self):
        self.assertEqual(change_kind({"title": "Fix slow query", "body": "Optimize latency"}), "performance")
        self.assertEqual(change_kind({"title": "Update guide"}, {"change_kind": "documentation"}),
                         "documentation")

    def test_tiny_or_unrepeated_experiments_cannot_promote(self):
        def trials(count, repetitions):
            return [{"case": str(i), "split": "validation" if i % 2 else "holdout", "repetition": r,
                     "stable": {"pass": False, "tokens": 100, "seconds": 1},
                     "candidate": {"pass": True, "tokens": 100, "seconds": 1}}
                    for i in range(count) for r in range(repetitions)]
        self.assertEqual(Evolution.decide(trials(2, 10), merge(DEFAULTS, {"evolution": {"repeats": 10}}))["decision"], "insufficient_evidence")
        self.assertEqual(Evolution.decide(trials(6, 1), DEFAULTS)["decision"], "insufficient_evidence")
        self.assertEqual(Evolution.decide(trials(6, 2), DEFAULTS)["decision"], "pass")
        regressed = trials(6, 2)
        regressed[0]["stable"]["pass"] = True
        regressed[0]["candidate"]["pass"] = False
        self.assertEqual(Evolution.decide(regressed, DEFAULTS)["decision"], "fail")

    def test_failure_codes_and_value_ordering_are_explicit(self):
        self.assertEqual(failure(RuntimeError("Model call budget exhausted"))["code"], "budget")
        self.assertEqual(failure(RuntimeError("Provider HTTP 503"))["action"], "retry_later")
        self.assertEqual(failure(RuntimeError("opencode_incompatible_2.1.3"))["code"], "environment")
        high = {"impact": "high", "effort": "low", "confidence": "high"}
        low = {"impact": "low", "effort": "high", "confidence": "low"}
        self.assertGreater(value_priority(high, "low"), value_priority(low, "high"))

    def test_high_risk_second_review_covers_manifests_and_core_paths(self):
        high_risk = {"risk": "high"}
        passing_review = {"decision": "pass", "assessments": [{"decision": "pass"}]}
        insufficient_review = {"decision": "insufficient_evidence", "assessments": []}
        self.assertFalse(risk_review_required(high_risk, passing_review, ["calculator.py"], "return a + b"))
        for path in ["adaptive_rsi/auth.py", "pyproject.toml", "setup.py", "setup.cfg", "requirements.in",
                     "adaptive_rsi/pipeline.py", "adaptive_rsi/evaluator.py", ".cargo/config.toml",
                     ".github/workflows/release.yml"]:
            with self.subTest(path=path):
                self.assertTrue(risk_review_required(high_risk, passing_review, [path], ""))
        self.assertTrue(risk_review_required(high_risk, insufficient_review))
        self.assertFalse(risk_review_required({"risk": "low"}, insufficient_review))
