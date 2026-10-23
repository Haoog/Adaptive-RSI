import json
import platform
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, compatibility, fingerprint, merge
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.context import code_map, related, terms
from adaptive_rsi.dashboard import records
from adaptive_rsi.discovery import related_task, value_priority
from adaptive_rsi.evolution import Evolution
from adaptive_rsi.gitops import git, snapshot
from adaptive_rsi.readiness import budget, inspect as readiness
from adaptive_rsi.recovery import failure
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.pipeline import _review_evidence, _stage_config
from adaptive_rsi.store import Store, encode
from adaptive_rsi.workflow import improvement
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
    def test_review_outage_reuses_plan_acceptance_and_patch_but_reruns_checks(self):
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
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "blocked")
            self.runtime.retry(task)
            second = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(second)
            self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "awaiting_approval")
        roles = [r[0] for r in self.runtime.agent.roles]
        self.assertEqual(roles.count("planning"), 1)
        self.assertEqual(roles.count("acceptance"), 1)
        self.assertEqual(roles.count("implementation"), 1)
        stages = {r["stage"] for r in self.store.query("SELECT stage FROM stages WHERE run=?", (second["id"],))}
        self.assertTrue({"planning", "acceptance", "implementation", "evaluation", "review"} <= stages)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_implementation_route_change_reuses_plan_and_acceptance(self):
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
        self.assertEqual([role for role, _ in self.runtime.agent.roles].count("acceptance"), 1)
        implementations = [route for role, route in self.runtime.agent.roles if role == "implementation"]
        self.assertEqual([route["effort"] for route in implementations], ["max", "high"])

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
                                  [{"number": 1, "updated": "2020-01-01T00:00:00Z"}])
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
        self.store.put_setting("task-proof-current:" + self.project, {"planning": {
            **proof, "at": time.time(), "base": git(self.repo, "rev-parse", "HEAD")}})
        result = readiness(self.runtime, self.runtime.project(self.project), config)
        evidence = {row["role"]: row for row in result["model_evidence"]}
        self.assertTrue(evidence["planning"]["configured"])
        self.assertTrue(evidence["planning"]["task_verified"])
        self.assertFalse(evidence["implementation"]["task_verified"])


class EvidenceContractTest(unittest.TestCase):
    def results(self, before, after, **extras):
        return {"acceptance_baseline": [{"passed": before, "error": "", "exit_code": 0 if before else 1, "stderr": "AssertionError", **extras}],
                "acceptance_candidate": [{"passed": after}]}

    def test_no_gain_and_environment_failure_are_not_improvements(self):
        self.assertEqual(improvement(self.results(True, True), {})["decision"], "not_requested")
        acceptance = {"change_kind": "behavior"}
        self.assertEqual(improvement(self.results(True, True), acceptance)["decision"], "insufficient_evidence")
        self.assertEqual(improvement(self.results(False, True), acceptance)["decision"], "pass")
        self.assertEqual(improvement(self.results(False, True, stderr="ModuleNotFoundError"), acceptance)["decision"], "insufficient_evidence")
        self.assertEqual(improvement(self.results(True, True), {"change_kind": "refactor"})["decision"], "pass")

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
        high = {"impact": "high", "effort": "low", "confidence": "high"}
        low = {"impact": "low", "effort": "high", "confidence": "low"}
        self.assertGreater(value_priority(high, "low"), value_priority(low, "high"))
