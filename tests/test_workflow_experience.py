import json
import platform
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from adaptive_rsi.config import fingerprint
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import CHECKS, FakeAgent, fixture


class ExperienceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / "repo"))

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_submission_retry_returns_same_task_but_intentional_repeat_is_new(self):
        first = self.runtime.goal(self.project, "Fix addition", request_id="request-1")
        self.assertEqual(first, self.runtime.goal(self.project, "Fix addition", request_id="request-1"))
        self.assertNotEqual(first, self.runtime.goal(self.project, "Fix addition", request_id="request-2"))
        with self.assertRaises(ValueError):
            self.runtime.goal(self.project, "Different content", request_id="request-1")

    def test_expired_candidate_can_retry_from_fresh_run(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.store.execute("UPDATE tasks SET state='expired' WHERE id=?", (task,))
        self.runtime.retry(task)
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_new_deferral_without_run_survives_reassessment(self):
        task = self.runtime.goal(self.project, "Need evidence")
        config = self.runtime.config(self.project)
        signature = fingerprint({"body": "Need evidence", "evidence": '[{"source": "user"}]',
                                 "policy": config["triage"]["policy"]})
        self.store.defer(task, {"base": "same-sha", "triage_input": signature, "reason": "Need evidence"})
        self.runtime.reassess_deferred(self.project, config, "same-sha")
        self.assertEqual("deferred", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.runtime.reassess_deferred(self.project, config, "changed-sha")
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_requirement_revision_keeps_task_and_invalidates_inputs(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.revise_task(task, "Also cover negative numbers")
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertIn("Also cover negative numbers", row["body"])
        self.assertEqual(1, len(self.store.query("SELECT * FROM task_revisions WHERE task=?", (task,))))

    def test_dependency_wait_does_not_block_independent_work(self):
        first = self.runtime.goal(self.project, "First")
        dependent = self.runtime.goal(self.project, "Dependent")
        independent = self.runtime.goal(self.project, "Independent")
        self.runtime.set_dependency(dependent, first)
        self.runtime.pause(self.project, False)
        self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (first,))
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(independent, run["task"])

    def test_retry_feedback_includes_findings_without_acceptance_source(self):
        from adaptive_rsi.experience import retry_feedback
        result = {"review": {"decision": "fail", "findings": [{"path": "calc.py", "line": 2,
                   "reason": "Negative input fails", "suggestion": "Handle negative input"}]},
                  "acceptance": {"source": "SEALED_SECRET"}}
        feedback = retry_feedback({"id": "run-1", "base": "sha", "state": "failed", "result": json.dumps(result)})
        self.assertIn("Handle negative input", json.dumps(feedback))
        self.assertNotIn("SEALED_SECRET", json.dumps(feedback))

    def test_transient_failure_schedules_fresh_run_without_using_code_retry(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        with patch.object(self.runtime, "execute_candidate", side_effect=RuntimeError("HTTP 503 connection failed")):
            self.runtime.process(run)
        current = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual("queued", current["state"])
        self.assertEqual(1, current["infra_retries"])
        self.assertEqual(0, current["attempts"])
        self.assertGreater(current["available"], time.time())

    def test_dependency_cycles_are_rejected(self):
        first = self.runtime.goal(self.project, "First")
        second = self.runtime.goal(self.project, "Second")
        self.runtime.set_dependency(first, second)
        with self.assertRaises(ValueError):
            self.runtime.set_dependency(second, first)

    def test_backup_is_consistent_restore_is_paused_and_never_overwrites(self):
        from adaptive_rsi.storage_tools import backup, restore, diagnostics
        task = self.runtime.goal(self.project, "PRIVATE task secret=DO_NOT_EXPORT")
        saved = backup(self.store)
        destination = self.root / "restored"
        restore(self.store.root / "backups" / (saved["id"] + ".sqlite3"), destination)
        recovered = Store(destination)
        self.assertEqual(task, recovered.one("SELECT id FROM tasks")["id"])
        self.assertEqual(1, recovered.one("SELECT paused FROM projects")["paused"])
        with self.assertRaises(ValueError):
            restore(self.store.path, destination)
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(diagnostics(self.store)))
        self.assertNotIn(str(self.root), json.dumps(diagnostics(self.store)))

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_review_rejection_reaches_next_implementation_without_sealed_source(self):
        class Reviewer(FakeAgent):
            rejected = False
            prompts = []

            def invoke(self, role, prompt, *args, **kwargs):
                if role == "implementation":
                    self.prompts.append(prompt)
                result = super().invoke(role, prompt, *args, **kwargs)
                if role == "review" and not self.rejected:
                    self.rejected = True
                    result.update(decision="fail", reason="Boundary issue", findings=[{
                        "severity": "high", "area": "correctness", "path": "calculator.py", "line": 2,
                        "title": "Handle boundary", "reason": "Boundary input is wrong", "suggestion": "RETRY_REVIEW_MARKER"}])
                return result

        agent = Reviewer()
        self.runtime.agent = agent
        self.runtime.configure({"checks": CHECKS, "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual("failed", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.runtime.retry(task)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual(2, len(agent.prompts))
        self.assertIn("RETRY_REVIEW_MARKER", agent.prompts[1])
        self.assertNotIn("assert add(2, 3) == 5", agent.prompts[1])
        self.assertEqual("awaiting_approval", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_provider_resume_is_scoped_to_revision_and_failure(self):
        from adaptive_rsi.provider_health import connection_key
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Connection failure")
        cancelled = self.runtime.goal(self.project, "Cancelled task")
        self.runtime.pause(self.project, False)
        connection = connection_key(self.runtime.config(self.project), "openai", self.project)
        for index, identifier in enumerate([task, cancelled]):
            self.store.execute("UPDATE tasks SET state=? WHERE id=?", ("blocked" if identifier == task else "cancelled", identifier))
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'blocked',?,1,2)",
                               ("resume-" + str(index), identifier, self.project, encode({"failure": {"code": "provider", "action": "retry_later", "connection": connection, "recoverable": True}})))
        self.assertEqual(0, self.runtime.resume_provider_tasks("opencode", self.project))
        self.assertEqual(1, self.runtime.resume_provider_tasks("openai", self.project))
        self.assertEqual("cancelled", self.store.one("SELECT state FROM tasks WHERE id=?", (cancelled,))["state"])
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_auth_failure_does_not_auto_retry(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        with patch.object(self.runtime, "execute_candidate", side_effect=RuntimeError("HTTP 401 unauthorized")):
            self.runtime.process(run)
        current = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual("blocked", current["state"])
        self.assertEqual(0, current["infra_retries"])

    def test_visible_queue_matches_claim_urgency_value_and_dependency(self):
        from adaptive_rsi.dashboard import records
        low = self.runtime.goal(self.project, "Low value")
        high = self.runtime.goal(self.project, "High value")
        urgent = self.runtime.goal(self.project, "Urgent")
        self.store.execute("UPDATE tasks SET priority=1 WHERE id=?", (low,))
        self.runtime.set_dependency(urgent, urgency=1)
        displayed = records(self.store, self.project, group="active")["tasks"]
        self.assertEqual([urgent, high, low], [task["id"] for task in displayed])
        self.runtime.pause(self.project, False)
        self.assertEqual(urgent, self.store.claim(self.project, self.runtime.config(self.project))["task"])

    def test_model_cannot_grant_behavior_preservation_exception(self):
        from adaptive_rsi.workflow import change_kind
        self.assertEqual("behavior", change_kind({"title": "整理接口", "body": "Handle the reported defect"}, {"change_kind": "refactor"}))
        self.assertEqual("behavior", change_kind({"title": "Refactor module", "body": "Also fix incorrect output"}, {"change_kind": "refactor"}))
        self.assertEqual("refactor", change_kind({"title": "整理模块并保持行为不变", "body": ""}, {"change_kind": "refactor"}))

    def test_dependency_only_project_does_not_starve_another_project(self):
        from concurrent.futures import Future
        parent = self.runtime.goal(self.project, "Parent")
        dependent = self.runtime.goal(self.project, "Dependent")
        self.runtime.set_dependency(dependent, parent)
        self.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (parent,))
        other = self.runtime.add_project(fixture(self.root / "other"))
        independent = self.runtime.goal(other, "Independent work")
        self.runtime.pause(self.project, False)
        self.runtime.pause(other, False)
        with patch.object(self.runtime.pool, "submit", return_value=Future()) as submit:
            self.runtime.dispatch([(self.runtime.project(p), self.runtime.config(p)) for p in [self.project, other]])
        self.assertEqual(independent, submit.call_args[0][1]["task"])

    def test_feedback_excludes_normalized_sealed_paths_and_keeps_new_test_failure(self):
        from adaptive_rsi.experience import retry_feedback
        result = {"reason": "SEALED_SAMPLE", "failure": {"code": "review", "detail": "SEALED_SAMPLE"},
                  "review": {"findings": [{"path": "./.rsi-acceptance/check.py", "suggestion": "SEALED_SAMPLE"},
                                           {"path": "calculator.py", "reason": "Handle sample 12345"}]},
                  "evaluation": {"checks": {"candidate_full": [{"name": "new boundary", "passed": False, "exit_code": 1, "stderr": "AssertionError"}]}}}
        feedback = retry_feedback({"result": json.dumps(result)}, protected=["12345"])
        self.assertNotIn("SEALED_SAMPLE", json.dumps(feedback))
        self.assertNotIn("12345", json.dumps(feedback))
        self.assertEqual("candidate_full", feedback["failed_regression_checks"][0]["stage"])

    def test_global_resume_resets_only_matching_project_circuit(self):
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Temporary connection failure")
        self.runtime.pause(self.project, False)
        config = self.runtime.config(self.project)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "openai", self.project, RuntimeError("HTTP 503 connection failed"))
        self.assertTrue(health.status(config, "openai", self.project)["paused"])
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task,))
        self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES ('global-resume',?,?,'blocked',?,1,2)",
                           (task, self.project, encode({"failure": {"code": "provider", "action": "retry_later", "connection": connection_key(config, "openai", self.project), "recoverable": True}})))
        self.assertEqual(1, self.runtime.model_center.resume_provider("openai")["resumed_tasks"])
        self.assertFalse(health.status(config, "openai", self.project)["paused"])

    def test_performance_contract_uses_real_measurements_and_tolerance(self):
        from adaptive_rsi.workflow import evidence_contract, improvement
        task = {"source": "goal", "title": "降低接口延迟"}
        with self.assertRaises(ValueError):
            evidence_contract(task, {"change_kind": "performance"})
        plan = {"change_kind": "performance", "performance_threshold": {"metric": "latency", "unit": "ms", "target": 80, "tolerance": 2, "direction": "lower"}}
        contract = evidence_contract(task, plan)
        def sample(value):
            return {"passed": True, "stdout": json.dumps({"metric": "latency", "unit": "ms", "value": value})}
        acceptance = {"change_kind": "performance", "contract": contract}
        checks = {"acceptance_baseline": [sample(100)], "acceptance_candidate": [sample(79)]}
        self.assertEqual("pass", improvement(checks, acceptance)["decision"])
        checks["acceptance_candidate"] = [sample(99)]
        self.assertEqual("insufficient_evidence", improvement(checks, acceptance)["decision"])

    def test_metrics_count_logical_interventions_and_same_failure_only(self):
        from adaptive_rsi.telemetry import workflow_metrics
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Fix addition")
        for index, name in enumerate(["a", "b"]):
            evidence = {"failure": {"code": "checks"}, "evaluation": {"checks": {"candidate": [{"name": name, "passed": False, "exit_code": 1}]}}}
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'failed',?,?,?)",
                               ("metric-" + name, task, self.project, encode(evidence), time.time() + index, time.time() + index + 1))
        self.assertEqual(0, workflow_metrics(self.store, self.project)["repeated_failures"])
        self.store.execute("UPDATE runs SET result=(SELECT result FROM runs WHERE id='metric-a') WHERE id='metric-b'")
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        self.runtime.retry(task)
        report = workflow_metrics(self.store, self.project)
        self.assertEqual(1, report["repeated_failures"])
        self.assertEqual(1, report["interventions"])

    def test_long_partial_and_escaped_sealed_samples_are_removed_before_truncation(self):
        from adaptive_rsi.experience import retry_feedback
        secret = "PRIVATE_SEALED_" + "ABC0123456789" * 70
        for quoted in [secret, secret[15:200], json.dumps("私密样本" * 20)[1:-1]]:
            row = {"result": json.dumps({"review": {"findings": [{"path": "app.py", "reason": "Boundary fails on " + quoted}]}})}
            feedback = retry_feedback(row, protected=[secret, "私密样本" * 20])
            self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_javascript_acceptance_numeric_samples_are_not_retry_feedback(self):
        from adaptive_rsi.experience import protected_feedback_values, retry_feedback
        from adaptive_rsi.recovery import save
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        spec = {"files": [{"path": "check.js", "content": 'const input = 31415926;\nif (add(input,1)!==31415927) throw Error("bad");'}]}
        save(self.runtime, run, "acceptance", spec, "signature")
        feedback = retry_feedback({"result": json.dumps({"review": {"findings": [{"path": "app.js", "reason": "For input 31415926 expected 31415927"}]}})}, protected_feedback_values(self.store, task))
        self.assertNotIn("31415926", json.dumps(feedback))
        self.assertNotIn("31415927", json.dumps(feedback))

    def test_shrinking_redactions_cannot_expose_truncated_sealed_samples(self):
        from adaptive_rsi.experience import retry_feedback
        secret = "TOPSECRETVALUE1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        prefixes = ["https://x/" + "a" * 700, "token=" + "a" * 700,
                    "Bearer " + "a" * 700, "sk-" + "a" * 700]
        for prefix in prefixes:
            with self.subTest(prefix=prefix[:12]):
                row = {"result": json.dumps({"review": {"findings": [
                    {"path": "app.py", "reason": prefix + " " + secret}]}})}
                feedback = retry_feedback(row, protected=[secret])
                self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_sealed_literals_and_embedded_urls_cannot_shrink_past_the_scan(self):
        from adaptive_rsi.experience import retry_feedback
        short = "0123456789ABCDEFGH"
        secret = "UNIQUE_SEALED_PRIVATE_SAMPLE_0123456789ABCDEFGHIJ"
        embedded_url = "INPUT https://example.com/long/private/path END_SECRET_79"
        contextual = "A" * 40 + " PRIVATE_TAIL"
        for reason, samples in [((short + " ") * 37 + secret, [short, secret]),
                                (embedded_url, [embedded_url]),
                                ("Bearer " + contextual, [contextual]),
                                ("https://x/" + "b" * 750 + " Bearer " + contextual,
                                 ["PREFIX_" + contextual + "_SUFFIX"])]:
            with self.subTest(reason=reason[:30]):
                row = {"result": json.dumps({"review": {"findings": [
                    {"path": "app.py", "reason": reason}]}})}
                feedback = retry_feedback(row, protected=samples)
                self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_large_sealed_fixtures_do_not_build_an_unbounded_fragment_index(self):
        import hashlib
        import tracemalloc
        from adaptive_rsi.experience import retry_feedback
        # Eight valid-sized minified fixtures have mostly unique overlapping windows.
        samples = ["".join(hashlib.sha256(str(index * 1000 + n).encode()).hexdigest() for n in range(1000)) for index in range(8)]
        row = {"result": json.dumps({"review": {"findings": [{"path": "app.py", "reason": "Handle the empty boundary case"}]}})}
        tracemalloc.start()
        try:
            feedback = retry_feedback(row, samples)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIn("empty boundary", feedback["review_findings"][0]["reason"])
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_legacy_triage_decision_is_migrated_and_scope_is_not_complete_dependencies(self):
        from adaptive_rsi.gitops import git
        task = self.runtime.goal(self.project, "Wait for backend behavior")
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        config = self.runtime.config(self.project)
        repo = Path(self.runtime.project(self.project)["path"])
        base = git(repo, "rev-parse", "HEAD")
        self.store.put_setting("triage-state:" + task, {"base": base, "scope": ["calc.py"],
            "triage_input": fingerprint({"body": row["body"], "evidence": row["evidence"], "policy": config["triage"]["policy"]})})
        self.store.execute("UPDATE tasks SET state='deferred' WHERE id=?", (task,))
        self.runtime.reassess_deferred(self.project, config, base)
        self.assertEqual("deferred", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.assertIsNotNone(self.store.one("SELECT * FROM task_decisions WHERE task=?", (task,)))
        (repo / "backend.py").write_text("API_VERSION = 2\n")
        git(repo, "add", "backend.py")
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "Change backend dependency")
        self.runtime.reassess_deferred(self.project, config, git(repo, "rev-parse", "HEAD"))
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_planning_yield_is_not_a_code_attempt(self):
        from adaptive_rsi.telemetry import workflow_metrics
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Fix addition")
        passed = {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}
        for index, (state, evidence) in enumerate([("assessed", {}), ("awaiting_approval", passed)]):
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,?,?,?,?)",
                ("first-pass-" + str(index), task, self.project, state, encode(evidence), time.time() + index, time.time() + index + 1))
        self.assertEqual(1, workflow_metrics(self.store, self.project)["first_pass"])

    def test_cancel_during_infrastructure_failure_cannot_be_requeued(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        transition = self.store.transition
        def concurrent_cancel(run_id, state, result=None, terminal=False):
            if state == "blocked":
                self.runtime.cancel_task(task)
            return transition(run_id, state, result, terminal)
        with patch.object(self.store, "transition", side_effect=concurrent_cancel), \
                patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
                patch.object(self.runtime, "invoke", side_effect=RuntimeError("HTTP 503 connection failed")):
            self.runtime.process(run)
        self.assertEqual("cancelled", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_paused_implementation_provider_is_not_attributed_to_planner(self):
        from adaptive_rsi.agent import CodexAgent
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        task = self.runtime.goal(self.project, "Fix addition")
        config = self.runtime.config(self.project)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        config["models"]["implementation"]["provider"] = "opencode"
        run["config"] = json.dumps(config)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "opencode", self.project, RuntimeError("HTTP 503"))
        self.store.reserve_call(self.project, run["id"], "planning", config["models"]["planning"], config)
        agent = CodexAgent(self.store)
        def invoke(*args, **kwargs):
            return agent.invoke("implementation", "Implement", self.root, self.project, run["id"], config)
        with patch.object(self.runtime, "invoke", side_effect=invoke), patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}):
            self.runtime.process(run)
        evidence = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertIn("connection", evidence.get("failure", {}), evidence)
        self.assertEqual(connection_key(config, "opencode", self.project), evidence["failure"]["connection"])
        self.assertTrue(evidence["failure"]["recoverable"])

    def test_readiness_circuit_failure_resumes_with_connection_without_model_call(self):
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        task = self.runtime.goal(self.project, "Fix addition")
        config = self.runtime.config(self.project)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        config["models"]["implementation"]["provider"] = "opencode"
        run["config"] = json.dumps(config)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "opencode", self.project, RuntimeError("HTTP 503"))
        with patch.object(self.runtime, "invoke", side_effect=AssertionError("Readiness must stop before a model call")):
            self.runtime.process(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        evidence = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual("blocked", row["state"])
        self.assertTrue(evidence["failure"]["recoverable"])
        self.assertEqual(connection_key(config, "opencode", self.project), evidence["failure"]["connection"])
        self.assertEqual(1, self.runtime.resume_provider_tasks("opencode", self.project))
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
