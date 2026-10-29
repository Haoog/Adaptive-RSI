"""Task feedback records observations without changing acceptance or ranking."""
import copy
import tempfile
import unittest
import json
import threading
import urllib.request
from pathlib import Path

from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.store import Store
from adaptive_rsi import task_feedback


class TaskValueFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES (?,?,?,?,?,?)",
                           ("p", "Project", str(Path(self.temp.name) / "project"), "{}", 0, 1))
        self.task = self.store.add_task("p", "manual", "fix", "Repair", "Original request")
        self.record = self.store.claim("p", copy.deepcopy(DEFAULTS))
        assert self.record is not None
        self.store.transition(self.record["id"], "awaiting_approval", {
            "summary": "Repair boundary", "files": ["app.py"],
            "evaluation": {"decision": "pass", "checks": {"candidate": [{"passed": True}]}},
            "review": {"decision": "pass"}, "merged_sha": "a" * 40}, True)

    def test_feedback_is_run_scoped_revisable_and_observational(self):
        before = self.store.one("SELECT * FROM tasks WHERE id=?", (self.task,))
        result = task_feedback.save(self.store, "p", self.task, self.record["id"], "helpful", "保留原文")
        self.assertEqual(result["rating"], "helpful")
        self.assertEqual(task_feedback.read(self.store, self.record["id"])["note"], "保留原文")
        task_feedback.save(self.store, "p", self.task, self.record["id"], "unnecessary", "Revised")
        self.assertEqual(task_feedback.read(self.store, self.record["id"])["rating"], "unnecessary")
        self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (self.task,)), before)
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='value_feedback'")), 2)
        task_feedback.save(self.store, "p", self.task, self.record["id"], None, "")
        self.assertIsNone(task_feedback.read(self.store, self.record["id"]))

    def test_wrong_ownership_and_invalid_input_do_not_write(self):
        for project, task, run, rating, note in [
            ("q", self.task, self.record["id"], "helpful", ""),
            ("p", "missing", self.record["id"], "helpful", ""),
            ("p", self.task, "missing", "helpful", ""),
            ("p", self.task, self.record["id"], "accepted", ""),
            ("p", self.task, self.record["id"], "helpful", "x" * 2001),
            ("p", self.task, self.record["id"], "helpful", []),
        ]:
            with self.subTest(project=project, task=task, run=run, rating=rating), self.assertRaises(ValueError):
                task_feedback.save(self.store, project, task, run, rating, note)
        self.assertIsNone(task_feedback.read(self.store, self.record["id"]))

    def test_maintainer_controls_are_validated_without_a_model_stage(self):
        from adaptive_rsi.config import validate
        control = {"id": "wrong_value", "criterion": "value remains one", "source": "maintainer:regression",
                   "files": [{"path": "value.py", "content": "VALUE = 2\n"}]}
        self.assertEqual(validate({"validation": {"controls": [control]}})["validation"]["controls"], [control])
        for invalid in [{**control, "files": [{"path": "../outside", "content": "x"}]},
                        {**control, "id": ""}, {**control, "files": []}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate({"validation": {"controls": [invalid]}})

    def test_evidence_summary_uses_persisted_checks_and_exact_delivery_sha(self):
        self.store.event("stage_reused", "Reused", "p", self.record["id"], {"stage": "planning"})
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual(report["files"], ["app.py"])
        self.assertEqual(report["checks"]["candidate"], {"passed": 1, "total": 1})
        self.assertEqual(report["delivery_sha"], "a" * 40)
        self.assertEqual(report["reused_stages"], ["planning"])
        self.assertIsNone(report["value_feedback"])
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(report["interventions"], 0)

    def test_corrupt_feedback_is_unavailable_without_erasing_evidence(self):
        key = "value-feedback:" + self.record["id"]
        self.store.execute("INSERT INTO settings VALUES (?,?)", (key, "{broken"))
        self.assertIsNone(task_feedback.read(self.store, self.record["id"]))
        self.assertEqual(self.store.one("SELECT value FROM settings WHERE key=?", (key,))["value"], "{broken")

    def test_http_feedback_reads_back_exact_target_and_summary(self):
        from types import SimpleNamespace
        from adaptive_rsi.server import Server
        runtime = SimpleNamespace(store=self.store, dispatch_lock=threading.RLock(),
                                  updates=SimpleNamespace(require_ready=lambda: None))
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            payload = {"project": "p", "task": self.task, "run": self.record["id"],
                       "rating": "helpful", "note": "Real HTTP"}
            request = urllib.request.Request(server.local_origin + "/api/task/value-feedback",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                "Cookie": server.cookie + "=" + server.token,
                "Origin": server.local_origin, "X-RSI-CSRF": server.csrf})
            with opener.open(request, timeout=5) as response:
                self.assertEqual(json.load(response)["rating"], "helpful")
            request = urllib.request.Request(server.local_origin + "/api/run?id=" + self.record["id"],
                                            headers={"Cookie": server.cookie + "=" + server.token})
            with opener.open(request, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result["evidence_summary"]["value_feedback"]["note"], "Real HTTP")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_workflow_metrics_count_latest_ratings_and_postmerge_separately(self):
        from adaptive_rsi.telemetry import workflow_metrics
        task_feedback.save(self.store, "p", self.task, self.record["id"], "helpful")
        task_feedback.save(self.store, "p", self.task, self.record["id"], "false_positive")
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "feedback": {"state": "regressed", "head": "b" * 40,
                         "merge": {"sha": "a" * 40, "state": "verified"}}}), self.record["id"]))
        report = workflow_metrics(self.store, "p")
        self.assertEqual(report["value_feedback"], {"helpful": 0, "unnecessary": 0, "false_positive": 1})
        self.assertEqual(report["rated_runs"], 1)
        self.assertEqual(report["unrated_runs"], 0)
        self.assertEqual(report["postmerge_verified"], 1)
        self.assertEqual(report["mainline_regressed"], 1)

    def test_dashboard_uses_component_prices_and_unknown_usage_stays_unpriced(self):
        from adaptive_rsi.task_feedback import dashboard_usage
        import time
        self.store.execute("INSERT INTO calls(id,project,run,tokens,input_tokens,output_tokens,usage_known,"
                           "input_price,output_price,created) VALUES ('priced','p',?,150,100,50,1,1,2,?)",
                           (self.record["id"], time.time()))
        self.store.execute("INSERT INTO calls(id,project,run,tokens,usage_known,unit_price,created) "
                           "VALUES ('unknown','p',?,100,0,10,?)", (self.record["id"], time.time()))
        item = dashboard_usage(self.store)["usage"][0]
        self.assertAlmostEqual(item["estimate"], .0002)
        self.assertEqual(item["unpriced"], 1)

    def test_controls_change_invalidates_acceptance_and_strategy_compatibility(self):
        from adaptive_rsi.config import compatibility, merge
        control = {"id": "wrong", "source": "maintainer", "criterion": "value", "files": [{"path": "value.py", "content": "bad"}]}
        self.assertNotEqual(compatibility(DEFAULTS), compatibility(merge(DEFAULTS, {"validation": {"controls": [control]}})))

    def test_summary_separates_original_merge_from_mainline_and_counts_task_interventions(self):
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "merged_sha": "a" * 40, "feedback": {"state": "regressed", "head": "b" * 40,
            "merge": {"sha": "a" * 40, "state": "verified"}}}), self.record["id"]))
        self.store.event("manual_retry", "Retry", "p", data={"task": self.task})
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual(report["postmerge_state"], "verified")
        self.assertEqual(report["mainline_state"], "regressed")
        self.assertEqual(report["interventions"], 1)

    def test_summary_accepts_a_no_checks_evaluation(self):
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "evaluation": {"decision": "insufficient_evidence", "checks": []}}), self.record["id"]))
        self.assertEqual(task_feedback.summary(self.store, self.record["id"])["checks"], {})

    def test_malformed_historical_merge_record_does_not_break_metrics(self):
        from adaptive_rsi.telemetry import workflow_metrics
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "feedback": {"merge": "broken"}}), self.record["id"]))
        self.assertEqual(workflow_metrics(self.store, "p")["postmerge_verified"], 0)
