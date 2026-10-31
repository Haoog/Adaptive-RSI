"""Task feedback records observations without changing acceptance or ranking."""
import copy
import tempfile
import time
import unittest
import json
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

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

    def test_real_evaluator_result_survives_persist_decode_and_summary(self):
        # 生产者在没有可执行检查时返回空的检查容器；持久化后解码不能把
        # summary/files/reason 一起丢掉，只应把检查计数当成空。
        import copy
        from adaptive_rsi.dashboard import records
        from adaptive_rsi.evaluator import Evaluator
        from adaptive_rsi.store import decode_evidence
        root = Path(self.temp.name)
        evaluation = Evaluator(self.store).evaluate(root, root, [], root / "checks", copy.deepcopy(DEFAULTS))
        self.assertEqual(evaluation["checks"], {})
        task = self.store.add_task("p", "manual", "no-checks", "No executable checks")
        record = self.store.claim("p", copy.deepcopy(DEFAULTS))
        self.store.transition(record["id"], "insufficient_evidence", {
            "reason": evaluation["reason"], "summary": "No executable checks",
            "files": ["app.py"], "evaluation": evaluation}, True)
        raw = self.store.one("SELECT result FROM runs WHERE id=?", (record["id"],))["result"]
        decoded = decode_evidence(raw)
        self.assertNotIn("evidence_error", decoded)
        self.assertIn("No executable acceptance checks", decoded["reason"])
        report = task_feedback.summary(self.store, record["id"])
        self.assertEqual((report["summary"], report["files"], report["checks"]),
                         ("No executable checks", ["app.py"], {}))
        row = next(item for item in records(self.store, "p")["runs"] if item["id"] == record["id"])
        self.assertIsNone(row["evidence_error"])
        self.assertEqual(row["summary"], "No executable checks")

    def test_summary_accepts_a_legacy_empty_checks_list(self):
        # 历史数据里合法的空列表仍表示“没有检查”，不能当成坏证据。
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "reason": "No executable acceptance checks", "summary": "Legacy no-check run",
            "files": ["legacy.py"], "evaluation": {"decision": "insufficient_evidence", "checks": []}}),
            self.record["id"]))
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual((report["summary"], report["files"], report["checks"]),
                         ("Legacy no-check run", ["legacy.py"], {}))

    def test_empty_checks_pass_is_downgraded_in_every_read_model(self):
        # 空列表兼容只说明“没有可执行检查”；同一形状上的 pass 没有可验证证据，
        # 读模型必须降级为 insufficient_evidence，不能计成 passed/accepted。
        from adaptive_rsi.store import decode_evidence
        from adaptive_rsi.telemetry import quality_summary, workflow_metrics
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (self.task,))
        self.store.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (json.dumps({
            "summary": "Legacy pass without checks", "files": ["legacy.py"], "reason": "historical",
            "review": {"decision": "pass"},
            "evaluation": {"decision": "pass", "checks": []}}), self.record["id"]))
        raw = self.store.one("SELECT result FROM runs WHERE id=?", (self.record["id"],))["result"]
        decoded = decode_evidence(raw)
        self.assertNotIn("evidence_error", decoded)
        self.assertEqual((decoded["summary"], decoded["files"], decoded["reason"]),
                         ("Legacy pass without checks", ["legacy.py"], "historical"))
        self.assertEqual(decoded["evaluation"], {"decision": "insufficient_evidence", "checks": {}})
        quality = quality_summary(self.store, "p")[0]
        self.assertEqual((quality["passed"], quality["evaluated"], quality["reviewed"]), (0, 1, 1))
        metrics = workflow_metrics(self.store, "p")
        self.assertEqual((metrics["accepted"], metrics["first_pass"]), (0, 0))
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual((report["summary"], report["files"], report["checks"]),
                         ("Legacy pass without checks", ["legacy.py"], {}))
        self.assertEqual(self.store.one("SELECT result FROM runs WHERE id=?", (self.record["id"],))["result"], raw)

    def test_non_empty_checks_list_is_rejected_without_erasing_other_evidence(self):
        from adaptive_rsi.store import decode_evidence
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "summary": "Corrupt checks", "files": ["app.py"],
            "evaluation": {"decision": "pass", "checks": [{"passed": True}]}}), self.record["id"]))
        decoded = decode_evidence(self.store.one("SELECT result FROM runs WHERE id=?", (self.record["id"],))["result"])
        self.assertEqual(decoded["evidence_error"], "invalid_evidence_shape:checks")
        self.assertNotIn("evaluation", decoded)
        self.assertEqual((decoded["summary"], decoded["files"]), ("Corrupt checks", ["app.py"]))
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual((report["summary"], report["checks"]), ("Corrupt checks", {}))

    def test_malformed_historical_merge_record_does_not_break_metrics(self):
        from adaptive_rsi.telemetry import workflow_metrics
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps({
            "feedback": {"merge": "broken"}}), self.record["id"]))
        self.assertEqual(workflow_metrics(self.store, "p")["postmerge_verified"], 0)

    def test_merge_attribution_agrees_across_quality_and_workflow_metrics(self):
        # 原合并已验证、后续主线回归：各统计都必须按同一归属给出 1/0 与主线回归 1。
        from adaptive_rsi.telemetry import quality_summary, workflow_metrics
        self.store.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (json.dumps({
            "merged_sha": "a" * 40,
            "feedback": {"state": "regressed", "head": "b" * 40,
                         "merge": {"sha": "a" * 40, "state": "verified"}}}), self.record["id"]))
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (self.task,))
        quality = quality_summary(self.store, "p")[0]
        metrics = workflow_metrics(self.store, "p")
        report = task_feedback.summary(self.store, self.record["id"])
        self.assertEqual((quality["postmerge_verified"], quality["postmerge_regressed"]), (1, 0))
        self.assertEqual((metrics["postmerge_verified"], metrics["postmerge_regressed"]), (1, 0))
        self.assertEqual((quality["mainline_regressed"], metrics["mainline_regressed"]), (1, 1))
        self.assertEqual((report["postmerge_state"], report["mainline_state"]), ("verified", "regressed"))

    def test_legacy_feedback_is_only_about_the_original_merge_at_its_sha(self):
        from adaptive_rsi.telemetry import quality_summary, workflow_metrics

        def measure(result):
            self.store.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(result), self.record["id"]))
            quality = quality_summary(self.store, "p")[0]
            metrics = workflow_metrics(self.store, "p")
            return ((quality["postmerge_verified"], quality["postmerge_regressed"],
                     quality["mainline_regressed"]),
                    (metrics["postmerge_verified"], metrics["postmerge_regressed"],
                     metrics["mainline_regressed"]))

        # 历史记录 head 恰好是合并提交时仍归属原合并。
        self.assertEqual(measure({"merged_sha": "a" * 40, "feedback": {"state": "verified", "head": "a" * 40}}),
                         ((1, 0, 0), (1, 0, 0)))
        # head 已在合并之后：只能算后续主线回归，不能归给原合并。
        self.assertEqual(measure({"merged_sha": "a" * 40, "feedback": {"state": "regressed", "head": "b" * 40}}),
                         ((0, 0, 1), (0, 0, 1)))

    def test_workflow_metrics_reports_one_committed_snapshot(self):
        # 在两次查询之间用独立事务原子写入任务与运行；响应必须对应某一已提交快照，
        # 不能出现“新运行 + 旧任务”的混合值。
        from adaptive_rsi.telemetry import workflow_metrics
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('q','Q','/q','{}',0,1)")
        task = self.store.add_task("q", "manual", "snapshot", "Snapshot task")
        before = workflow_metrics(self.store, "q")
        self.assertEqual((before["tasks"], before["accepted"], before["delivered"]), (1, 0, 0))
        original, writes = self.store.query, []

        def interleaved(sql, args=()):
            rows = original(sql, args)
            if sql.startswith("SELECT * FROM tasks WHERE id IN") and not writes:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE tasks SET state='merged' WHERE id=?", (task,))
                    db.execute("INSERT INTO runs(id,task,project,state,started,ended,result) "
                               "VALUES ('q-run',?,?,?,?,?,?)",
                               (task, "q", "merged", time.time(), time.time(), json.dumps({
                                   "evaluation": {"decision": "pass", "checks": {}},
                                   "review": {"decision": "pass"}})))
                writes.append(True)
            return rows

        with patch.object(self.store, "query", side_effect=interleaved):
            during = workflow_metrics(self.store, "q")
        self.assertTrue(writes)
        self.assertEqual((during["tasks"], during["accepted"], during["delivered"]), (1, 0, 0))
        after = workflow_metrics(self.store, "q")
        self.assertEqual((after["tasks"], after["accepted"], after["delivered"]), (1, 1, 1))
        # 嵌套在已有快照内调用不会重新开事务，也不改变结果。
        with self.store.snapshot():
            nested = workflow_metrics(self.store, "q")
        self.assertEqual((nested["accepted"], nested["delivered"]), (1, 1))

    def test_quality_summary_and_dashboard_usage_report_one_committed_snapshot(self):
        # quality_summary 和 dashboard_usage 的多次读取必须共享同一只读事务：
        # 并发写入只能让响应整体对应新快照或旧快照，不能出现半新半旧的混合计数。
        from adaptive_rsi.task_feedback import dashboard_usage
        from adaptive_rsi.telemetry import quality_summary
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('q','Q','/q','{}',0,1)")
        task = self.store.add_task("q", "manual", "snapshot", "Snapshot task")
        record = self.store.claim("q", copy.deepcopy(DEFAULTS))
        assert record is not None
        run = record["id"]
        self.store.transition(run, "awaiting_approval", {
            "summary": "Snapshot run", "files": ["app.py"],
            "evaluation": {"decision": "pass", "checks": {"candidate": [{"passed": True}]}},
            "review": {"decision": "pass"}}, True)
        before_usage = dashboard_usage(self.store)
        self.assertEqual(before_usage, {"usage": [], "model_usage": []})
        original, writes = self.store.query, []

        def insert_call(sql, args=()):
            rows = original(sql, args)
            # 恰在 dashboard_usage 第一条聚合查询之后、第二条之前，用一个独立写事务
            # 原子插入 calls 行：旧快照整体为空，新快照整体包含该行，混合响应即缺陷。
            if not writes:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO calls(id,project,run,tokens,usage_known,unit_price,created) "
                               "VALUES ('q-call',?,?,?,?,?,?)", ("q", run, 200, 1, 5, time.time()))
                writes.append(True)
            return rows

        def merge_task(sql, args=()):
            rows = original(sql, args)
            # 恰在 quality_summary 取回行之后追加合并反馈：唯一一条查询已被旧快照
            # 填充，响应必须仍是旧值；新快照整体可见 postmerge_verified=1。
            if not writes:
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE tasks SET state='merged' WHERE id=?", (task,))
                    db.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (json.dumps({
                        "summary": "Snapshot run", "files": ["app.py"],
                        "evaluation": {"decision": "pass", "checks": {"candidate": [{"passed": True}]}},
                        "review": {"decision": "pass"}, "merged_sha": "a" * 40,
                        "feedback": {"state": "verified", "head": "a" * 40}}), run))
                writes.append(True)
            return rows

        with patch.object(self.store, "query", side_effect=insert_call):
            during_usage = dashboard_usage(self.store)
        self.assertTrue(writes)
        self.assertEqual(during_usage, {"usage": [], "model_usage": []})
        after_usage = dashboard_usage(self.store)
        usage = next(item for item in after_usage["usage"] if item["project"] == "q")
        self.assertEqual((usage["calls"], usage["priced_calls"], usage["unpriced"]), (1, 1, 0))
        self.assertAlmostEqual(usage["estimate"], 0.001)
        writes.clear()
        before_quality = quality_summary(self.store, "q")
        self.assertEqual((before_quality[0]["runs"], before_quality[0]["passed"],
                          before_quality[0]["postmerge_verified"]), (1, 1, 0))
        with patch.object(self.store, "query", side_effect=merge_task):
            during_quality = quality_summary(self.store, "q")
        self.assertTrue(writes)
        quality = during_quality[0]
        self.assertEqual((quality["runs"], quality["passed"], quality["postmerge_verified"]), (1, 1, 0))
        after_quality = quality_summary(self.store, "q")
        quality = after_quality[0]
        self.assertEqual((quality["runs"], quality["passed"], quality["postmerge_verified"]), (1, 1, 1))

    def test_one_snapshot_decorators_reuse_outer_snapshot(self):
        # 嵌套在已有快照内调用时不再开新事务，语义与 workflow_metrics 一致。
        from adaptive_rsi.task_feedback import dashboard_usage
        from adaptive_rsi.telemetry import quality_summary
        connections = []

        original_query = self.store.query

        def query(sql, args=()):
            connections.append(getattr(self.store._read_snapshot, "connection", None))
            return original_query(sql, args)

        self.store.query = query
        try:
            quality_summary(self.store, "p")
            dashboard_usage(self.store)
        finally:
            self.store.query = original_query
        self.assertTrue(connections)
        self.assertTrue(all(connection is not None for connection in connections))
        self.assertEqual(len({id(connection) for connection in connections}), 1)
