import concurrent.futures
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.cli import doctor
from adaptive_rsi.config import fingerprint
from adaptive_rsi.model_center import CenterError
from adaptive_rsi.native import ProviderUnavailable
from adaptive_rsi.opencode import OpenCodeCapabilities, OpenCodeCompatibilityError
from adaptive_rsi.provider_health import ProviderHealth, ProviderPaused
from adaptive_rsi.readiness import inspect
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.telemetry import quality_summary, workflow_metrics
from tests.test_model_center import DirectoryServer
from tests.test_runtime import fixture


class OperabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(Path(self.temp.name) / "state"))
        self.center = self.runtime.model_center
        self.store = self.runtime.store

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def save(self, fixture):
        self.center.save_provider(fixture.provider, {"mode": "none"})

    def test_three_provider_failures_stop_network_and_budget_until_explicit_resume(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            with patch("adaptive_rsi.native.post_json", side_effect=ProviderUnavailable("Model API HTTP 429")) as post:
                for _ in range(3):
                    with self.assertRaises(CenterError) as caught:
                        self.center.probe("gateway", "coder-a")
                    self.assertEqual(caught.exception.code, "rate_limit")
                with self.assertRaises(CenterError) as caught:
                    self.center.probe("gateway", "coder-a")
                self.assertEqual(caught.exception.code, "provider_paused")
                self.assertEqual(post.call_count, 3)
                self.assertEqual(len(self.store.query("SELECT * FROM calls")), 3)
            self.assertTrue(self.center.snapshot()["providers"][-1]["health"]["paused"])
            self.center.resume_provider("gateway")
            self.center.probe("gateway", "coder-a")
            self.assertFalse(self.center.snapshot()["providers"][-1]["health"]["paused"])

    def test_circuit_is_atomic_scoped_persistent_and_excludes_nonprovider_failures(self):
        config = self.runtime.config()
        health = ProviderHealth(self.store)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: health.record(config, "openai", "first", RuntimeError("HTTP 503")), range(3)))
        self.assertEqual(health.status(config, "openai", "first")["failures"], 3)
        reopened = Store(self.store.root)
        self.addCleanup(reopened.close)
        with self.assertRaises(ProviderPaused):
            ProviderHealth(reopened).require(config, "openai", "first")
        health.require(config, "openai", "second")
        for reason in ["Model call budget exhausted", "Agent returned invalid structured output", "sandbox unavailable"]:
            health.record(config, "openai", "second", RuntimeError(reason))
        self.assertEqual(health.status(config, "openai", "second")["failures"], 0)
        health.record(config, "openai", "first")
        self.assertTrue(health.status(config, "openai", "first")["paused"])

    def test_paused_primary_uses_only_explicit_fallback(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            config = self.runtime.config()
            route = {"provider": "gateway", "model": "coder-a", "effort": "default"}
            config["models"]["probe"] = {"provider": "openai", "model": "unused", "effort": "default", "fallback": route}
            health = ProviderHealth(self.store)
            for _ in range(3):
                health.record(config, "openai", "model-center", RuntimeError("HTTP 429"))
            with patch("adaptive_rsi.agent.shutil.which", side_effect=AssertionError("Paused Codex launched")):
                result = CodexAgent(self.store).invoke("probe", "test", self.temp.name, "model-center", "probe-fallback", config)
            self.assertEqual(result, {"summary": "RSI_OK"})
            self.assertEqual(self.store.one("SELECT provider FROM calls")["provider"], "gateway")

    def test_probe_results_are_per_effort_failures_invalidate_only_matching_proof(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            self.center.probe("gateway", "coder-a", "default")
            self.center.probe("gateway", "coder-a", "high")
            fixture.tool = False
            with self.assertRaises(CenterError) as caught:
                self.center.probe("gateway", "coder-a", "high")
            self.assertEqual(caught.exception.code, "tool_format")
            model = self.center.directory(self.center.provider("gateway"))["models"][0]
            self.assertEqual(model["probes"]["default"]["state"], "pass")
            self.assertEqual(model["probes"]["high"]["state"], "fail")
            self.assertNotIn("verified", model)
            self.assertFalse(self.center.snapshot()["providers"][-1]["health"]["paused"])
            self.runtime.configure({"limits": {"daily_calls": 3}})
            fixture.tool = True
            self.center.probe("gateway", "coder-a", "default")
            model = self.center.directory(self.center.provider("gateway"))["models"][0]
            self.assertEqual(model["probes"]["default"]["state"], "pass")

    def test_outage_after_edit_counts_failure_without_using_fallback(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            config = self.runtime.config()
            config["models"]["implementation"] = {"provider": "gateway", "model": "coder-a", "effort": "default",
                "fallback": {"provider": "openai", "model": "unused", "effort": "high"}}
            response = {"choices": [{"message": {"tool_calls": [{"id": "edit", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "source.py", "content": "changed"})}}]}}]}
            workspace = Path(self.temp.name) / "workspace"
            workspace.mkdir()
            with patch("adaptive_rsi.native.post_json", side_effect=[response, ProviderUnavailable("Model API HTTP 429")]) as post:
                with self.assertRaisesRegex(RuntimeError, "fallback refused"):
                    CodexAgent(self.store).invoke("implementation", "test", workspace, "p", "edited-run", config)
                self.assertEqual(post.call_count, 2)
            self.assertEqual((workspace / "source.py").read_text(), "changed")
            health = ProviderHealth(self.store).status(config, "gateway", "p")
            self.assertEqual((health["failures"], health["code"]), (1, "rate_limit"))
            self.assertFalse(self.store.query("SELECT * FROM events WHERE kind='fallback'"))

    def test_price_is_frozen_per_call_and_preferences_survive_refresh(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            self.center.fetch("gateway")
            self.center.edit_model("gateway", {"id": "coder-a", "favorite": True, "unit_price": 4})
            self.center.probe("gateway", "coder-a")
            self.center.edit_model("gateway", {"id": "coder-a", "unit_price": 8})
            self.center.probe("gateway", "coder-a")
            self.assertEqual([r["unit_price"] for r in self.store.query("SELECT unit_price FROM calls ORDER BY created")], [4, 8])
            self.center.fetch("gateway")
            model = self.center.directory(self.center.provider("gateway"))["models"][0]
            self.assertEqual(model["unit_price"], 8)
            self.assertTrue(model["favorite"])
            for invalid in [-1, True, float("nan"), float("inf"), "4"]:
                with self.assertRaises(CenterError):
                    self.center.edit_model("gateway", {"id": "coder-a", "unit_price": invalid})

    def test_connection_change_clears_proofs_and_circuit_but_preserves_preferences(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            self.center.probe("gateway", "coder-a")
            self.center.edit_model("gateway", {"id": "coder-a", "favorite": True, "unit_price": 2})
            config = self.runtime.config()
            for _ in range(3):
                ProviderHealth(self.store).record(config, "gateway", "model-center", RuntimeError("HTTP 401"))
            self.center.save_provider(fixture.provider, {"mode": "session", "value": "replacement-key"})
            model = self.center.directory(self.center.provider("gateway"))["models"][0]
            self.assertTrue(model["favorite"])
            self.assertEqual(model["unit_price"], 2)
            self.assertNotIn("probes", model)
            self.assertFalse(self.center.snapshot()["providers"][-1]["health"]["paused"])

    def test_plan_evidence_survives_failure_and_quality_excludes_system_runs(self):
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('p','fixture',?,'{}',1,?)", (self.temp.name, time.time()))
        task = self.store.add_task("p", "goal", "goal", "test")
        system = self.store.add_task("p", "system_light", "inspection", "inspect")
        plan = {"value": {"benefit": "fix defect"}, "scope": ["file.py"], "plan_decision": "ready", "plan_reason": "evidence"}
        for run, task_id in [("first", task), ("second", task), ("passed", task), ("system", system)]:
            self.store.execute("INSERT INTO runs(id,task,project,state,config,started) VALUES (?,?, 'p','planning','{}',?)", (run, task_id, time.time()))
            self.store.transition(run, "executing", plan)
            self.store.transition(run, "evaluating")
            if run in ["first", "second"]:
                self.store.transition(run, "failed", {"evaluation": {"decision": "pass"}, "review": {"decision": "fail"}}, True)
            else:
                self.store.transition(run, "awaiting_approval", {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id='first'")["result"])
        self.assertEqual(result["scope"], ["file.py"])
        self.assertEqual(result["plan_reason"], "evidence")
        quality = quality_summary(self.store)[0]
        self.assertEqual((quality["runs"], quality["evaluated"], quality["passed"]), (3, 3, 1))
        self.assertEqual((quality["reviewed"], quality["review_blocked"], quality["repeat_failures"]), (3, 2, 1))

    def test_doctor_reports_opencode_version_and_compatibility_without_raw_output(self):
        capabilities = OpenCodeCapabilities(version="2.1.3", release=(2, 1, 3))
        with patch("adaptive_rsi.cli.shutil.which",
                   side_effect=lambda name: "/fake/opencode" if name == "opencode" else None), patch(
                "adaptive_rsi.opencode.detect_opencode", return_value=capabilities):
            result = doctor()
        self.assertEqual(result["opencode_version"], "2.1.3")
        self.assertEqual(result["opencode_compatibility"], "supported")
        self.assertEqual(result["opencode_commands"]["run"], "model#variant")

        failure = OpenCodeCompatibilityError("opencode_incompatible_3.0.0", "3.0.0")
        with patch("adaptive_rsi.cli.shutil.which",
                   side_effect=lambda name: "/fake/opencode" if name == "opencode" else None), patch(
                "adaptive_rsi.opencode.detect_opencode", side_effect=failure):
            result = doctor()
        self.assertEqual(result["opencode_version"], "3.0.0")
        self.assertEqual(result["opencode_compatibility"], "unsupported")
        self.assertIn("OpenCode 1.19.3", result["opencode_diagnostic"])
        self.assertIn("does not upgrade or downgrade", result["opencode_diagnostic"])

    def test_doctor_uses_docker_worker_when_host_opencode_is_absent(self):
        capabilities = OpenCodeCapabilities(version="2.1.3", release=(2, 1, 3))

        def run(argv, *args, **kwargs):
            if argv[:2] == ["docker", "info"] or argv[:3] == ["docker", "image", "inspect"]:
                return {"exit_code": 0, "error": None}
            raise AssertionError(argv)

        with patch("adaptive_rsi.cli.shutil.which",
                   side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None), patch(
                "adaptive_rsi.cli.run_process", side_effect=run), patch(
                "adaptive_rsi.opencode.detect_opencode", return_value=capabilities) as detect:
            result = doctor()
        self.assertEqual(result["opencode_transport"], "docker")
        self.assertTrue(result["opencode_worker_available"])
        self.assertEqual(result["opencode_version"], "2.1.3")
        self.assertEqual(result["opencode_compatibility"], "supported")
        self.assertEqual(detect.call_args.kwargs["argv"], [
            "/usr/bin/docker", "run", "--rm", "adaptive-rsi-worker:local", "opencode", "--version"])

    def test_readiness_reports_specific_opencode_compatibility_diagnostic(self):
        project = self.runtime.add_project(fixture(Path(self.temp.name) / "project"))
        self.runtime.configure({"models": {"planning": {
            "provider": "opencode", "model": "opencode-go/coder", "effort": "high"}}}, project)
        original_which = __import__("shutil").which
        failure = OpenCodeCompatibilityError("opencode_incompatible_3.0.0", "3.0.0")
        with patch("adaptive_rsi.readiness.platform.system", return_value="Darwin"), patch(
                "adaptive_rsi.readiness.shutil.which",
                   side_effect=lambda name: "/fake/opencode" if name == "opencode" else original_which(name)), patch(
                "adaptive_rsi.opencode.detect_opencode", side_effect=failure), patch(
                "adaptive_rsi.cli_worker.worker_available", return_value=True), patch.object(
                self.center, "directory", return_value={"models": [], "fetched": False, "truncated": False}):
            result = inspect(self.runtime, self.runtime.project(project))
        planning = next(item for item in result["items"] if item.get("role") == "planning")
        self.assertFalse(planning["ready"])
        self.assertEqual(planning["reason"], "opencode_incompatible_3.0.0")
        self.assertEqual(planning["opencode_version"], "3.0.0")
        self.assertIn("OpenCode 1.19.3", planning["detail"])

    def test_retention_cleanup_removes_stale_derived_data_but_keeps_active_delivery(self):
        store = self.store
        stale = time.time() - 120 * 86400
        store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                      ("proj01", "fixture", str(Path(self.temp.name) / "project"), "{}", 1, stale))
        for run, state in [("runold01", "completed"), ("runlive1", "merged"), ("runnew01", "completed")]:
            store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,attempts,overrides,available,created,updated) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                          ("task" + run, "proj01", "goal", "fp" + run, "title", "body", "[]", "[]", state, 50, 0, "{}", stale, stale))
            ended = stale if run != "runnew01" else time.time()
            store.execute("INSERT INTO runs(id,task,project,state,config,started,ended,result) VALUES (?,?,?,?,?,?,?,?)",
                          (run, "task" + run, "proj01", state, "{}", stale, ended, "{}"))
        store.put_setting("run-baseline:runold01", {"sha": "old"})
        store.put_setting("delivery:runold01", {"head": "old"})
        store.put_setting("run-baseline:runlive1", {"sha": "merged"})
        store.put_setting("delivery:runlive1", {"head": "merged"})
        store.put_setting("run-baseline:runnew01", {"sha": "fresh"})
        store.put_setting("triage-state:taskrunold01", {"decision": "deferred"})
        store.put_setting("task-proof:proj01:abc", {"at": stale})
        store.put_setting("task-proof:proj01:def", {"at": time.time()})
        store.put_setting("provider-health:proj01:p1:sig", {"paused": False, "at": stale})
        store.put_setting("provider-health:proj01:p2:sig", {"paused": True, "at": stale})
        store.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                      ("skillold", "proj01", None, "verified", "[]", "content", "{}", stale, stale))
        self.runtime.maintenance()
        keys = {row["key"] for row in store.query("SELECT key FROM settings")}
        self.assertNotIn("run-baseline:runold01", keys)
        self.assertNotIn("delivery:runold01", keys)
        self.assertNotIn("triage-state:taskrunold01", keys)
        self.assertNotIn("task-proof:proj01:abc", keys)
        self.assertNotIn("provider-health:proj01:p1:sig", keys)
        # 交付与合并后监控仍在生命周期的运行保留全部键。
        self.assertIn("run-baseline:runlive1", keys)
        self.assertIn("delivery:runlive1", keys)
        self.assertIn("run-baseline:runnew01", keys)
        self.assertIn("task-proof:proj01:def", keys)
        self.assertIn("provider-health:proj01:p2:sig", keys)
        store.put_setting("issue-comments:proj01:7", {"checked_at": stale, "comments": []})
        store.put_setting("issue-comments:proj01:8", {"checked_at": time.time(), "comments": []})
        # maintenance 每小时节流一次；清理新插入的键需要重置门控再跑一轮。
        store.execute("DELETE FROM settings WHERE key='maintenance-last'")
        self.runtime.maintenance()
        keys = {row["key"] for row in store.query("SELECT key FROM settings")}
        self.assertNotIn("issue-comments:proj01:7", keys)
        self.assertIn("issue-comments:proj01:8", keys)
        # 新鲜条目占满一批时，过期条目仍在本轮被删除（过滤先于 LIMIT）。
        for index in range(450):
            store.put_setting("task-proof:proj01:fresh%03d" % index, {"at": time.time()})
        store.put_setting("task-proof:proj01:stale-old", {"at": stale})
        store.execute("DELETE FROM settings WHERE key='maintenance-last'")
        self.runtime.maintenance()
        keys = {row["key"] for row in store.query("SELECT key FROM settings")}
        self.assertNotIn("task-proof:proj01:stale-old", keys)
        self.assertEqual(store.one("SELECT count(*) AS n FROM skills WHERE id='skillold'")["n"], 0)

    def test_retention_cleanup_skips_unreadable_merged_result_without_aborting(self):
        store = self.store
        stale = time.time() - 120 * 86400
        store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                      ("proj01", "fixture", str(Path(self.temp.name) / "project"), "{}", 1, stale))
        runs_root = Path(self.temp.name) / "state" / "runs"
        cases = [("runbad01", "merged", "'{"), ("runold02", "completed", "{}"),
                 ("rundone03", "merged", '{"monitoring_completed": true}'), ("runlist04", "merged", "[1,2]")]
        for run, state, result in cases:
            store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,attempts,overrides,available,created,updated) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                          ("task" + run, "proj01", "goal", "fp" + run, "title", "body", "[]", "[]", state, 50, 0, "{}", stale, stale))
            store.execute("INSERT INTO runs(id,task,project,state,config,started,ended,result) VALUES (?,?,?,?,?,?,?,?)",
                          (run, "task" + run, "proj01", state, "{}", stale, stale, result))
            store.execute("UPDATE runs SET path=? WHERE id=?", (str(runs_root / run), run))
        for run in [case[0] for case in cases]:
            store.put_setting("run-baseline:" + run, {"sha": run})
        kept = [runs_root / run / name for run in ("runbad01", "runlist04") for name in ("workspace", "baseline")]
        removed = [runs_root / run / name for run in ("runold02", "rundone03") for name in ("workspace", "baseline")]
        for directory in kept + removed:
            (directory / "nested").mkdir(parents=True)
        # 损坏或非对象 result 的过期 merged 运行按已保留处理：整轮不抛错，可清理的照常清理。
        self.runtime.maintenance()
        self.assertTrue(all(directory.exists() for directory in kept))
        self.assertFalse(any(directory.exists() for directory in removed))
        states = {row["id"]: row["state"] for row in store.query("SELECT id,state FROM runs")}
        self.assertEqual(states, {"runbad01": "merged", "runold02": "completed",
                                  "rundone03": "merged", "runlist04": "merged"})
        keys = {row["key"] for row in store.query("SELECT key FROM settings")}
        self.assertIn("run-baseline:runbad01", keys)
        self.assertIn("run-baseline:runlist04", keys)
        self.assertNotIn("run-baseline:runold02", keys)
        # merged 运行的派生键在交付生命周期结束前按设计保留，与监控是否完成无关。
        self.assertIn("run-baseline:rundone03", keys)
        # maintenance-last 正常推进，不再因同一损坏行每 5 秒重试整轮维护。
        self.assertGreater(store.setting("maintenance-last", 0), stale)
        # 损坏结果按「已保留」的常规决策跳过，不作为调度故障写入事件。
        self.assertEqual(store.query("SELECT 1 FROM events WHERE kind='scheduler_error'"), [])

    def test_retention_settings_batches_drain_remaining_runs(self):
        store = self.store
        now = 2_000_000_000
        stale = now - 120 * 86400
        protected = ["pr_open", "merged", "unverified_merge", "releasing", "deploying", "pr_closed"]
        remaining_keys = set()
        preserved_keys = set()
        with store.connect() as db:
            # 已清理运行排在前面时，也必须继续找到仍有派生键的运行。
            for index in range(450):
                run = "old-%03d" % index
                db.execute("INSERT INTO runs(id,state,started,ended) VALUES (?,'completed',?,?)", (run, stale, stale))
                if index < 200:
                    continue
                for prefix, suffix in [("run-baseline:", ""), ("delivery:", ""),
                                       ("automation:", ":release"), ("automation:", ":deploy")]:
                    key = prefix + run + suffix
                    remaining_keys.add(key)
                    db.execute("INSERT INTO settings VALUES (?, '{}')", (key,))
            for state in protected + ["fresh", "active"]:
                run = "keep-" + state
                ended = None if state == "active" else now if state == "fresh" else stale
                db.execute("INSERT INTO runs(id,state,started,ended) VALUES (?,?,?,?)",
                           (run, state if state in protected else "completed", stale, ended))
                for prefix, suffix in [("run-baseline:", ""), ("delivery:", ""), ("automation:", ":release")]:
                    key = prefix + run + suffix
                    preserved_keys.add(key)
                    db.execute("INSERT INTO settings VALUES (?, '{}')", (key,))
            # 相似前缀不是这个运行的派生键。
            for key in ["delivery:old-200-extra", "unrelated:old-200", "automation:orphan:release"]:
                preserved_keys.add(key)
                db.execute("INSERT INTO settings VALUES (?, '{}')", (key,))

        for passes, expected_remaining in [(0, 200), (1, 0), (2, 0)]:
            with self.subTest(passes=passes + 1), patch("adaptive_rsi.runtime.time.time", return_value=now + passes * 3601):
                self.runtime.maintenance()
                keys = {row["key"] for row in store.query("SELECT key FROM settings")}
                self.assertEqual(len(keys & remaining_keys), expected_remaining)
                self.assertTrue(preserved_keys <= keys)
        self.assertEqual(store.one("SELECT count(*) AS n FROM runs")["n"], 458)

    def test_retention_settings_uses_indexed_lookups_with_large_clean_history(self):
        store = self.store
        now = 2_000_000_000
        stale = now - 120 * 86400
        prefixes = ("run-baseline:", "automation:", "delivery:")
        remaining_keys = set()
        unrelated_keys = {"unrelated:%04d" % index for index in range(2000)}
        with store.connect() as db:
            db.executemany("INSERT INTO runs(id,state,started,ended) VALUES (?,'completed',?,?)",
                           [("history-%04d" % index, stale, stale) for index in range(5000)])
            db.executemany("INSERT INTO settings VALUES (?, '{}')", [(key,) for key in unrelated_keys])
            # 201 runs with all six key shapes: the first pass must still stop at 200 runs.
            for index in range(4799, 5000):
                for prefix in prefixes:
                    for suffix in ("", ":release"):
                        key = prefix + "history-%04d" % index + suffix
                        remaining_keys.add(key)
                        db.execute("INSERT INTO settings VALUES (?, '{}')", (key,))

        original_query = store.query
        plans = []

        def checked_query(sql, args=()):
            if not sql.startswith("SELECT r.id FROM runs r"):
                return original_query(sql, args)
            with store.connect() as db:
                plan = [row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args)]
                searches = [detail for detail in plan if "SEARCH s USING" in detail]
                self.assertEqual(len(searches), 6, plan)
                self.assertTrue(all("INDEX" in detail for detail in searches), plan)
                self.assertFalse(any("SCAN s" in detail for detail in plan), plan)
                self.assertEqual(sum("key=?" in detail for detail in searches), 3, plan)
                self.assertEqual(sum("key>? AND key<?" in detail for detail in searches), 3, plan)
                plans.append(plan)
                return [dict(row) for row in db.execute(sql, args)]

        with patch.object(store, "query", side_effect=checked_query):
            for passes, expected in [(0, 6), (1, 0)]:
                with self.subTest(passes=passes + 1), patch("adaptive_rsi.runtime.time.time", return_value=now + passes * 3601):
                    self.runtime.maintenance()
                    keys = {row["key"] for row in original_query("SELECT key FROM settings")}
                    self.assertEqual(len(keys & remaining_keys), expected)
                    self.assertTrue(unrelated_keys <= keys)
        self.assertEqual(len(plans), 2)
        self.assertEqual(store.one("SELECT count(*) AS n FROM runs")["n"], 5000)

    def test_retention_settings_matches_literal_run_id_and_colon_suffix_only(self):
        store = self.store
        now = 2_000_000_000
        stale = now - 120 * 86400
        run = "old_%Case"
        removed = set()
        preserved = set()
        with store.connect() as db:
            db.execute("INSERT INTO runs(id,state,started,ended) VALUES (?,'completed',?,?)", (run, stale, stale))
            for prefix in ("run-baseline:", "automation:", "delivery:"):
                removed.update(prefix + run + suffix for suffix in ("", ":", ":release", ":nested:deploy"))
                preserved.update(prefix + other for other in (run + "-extra", run + ";", run + "0", "oldABCase:release", "old_%case:release"))
            db.executemany("INSERT INTO settings VALUES (?, '{}')", [(key,) for key in removed | preserved])
        with patch("adaptive_rsi.runtime.time.time", return_value=now):
            self.runtime.maintenance()
        keys = {row["key"] for row in store.query("SELECT key FROM settings")}
        self.assertFalse(keys & removed)
        self.assertTrue(preserved <= keys)

    def test_workflow_metrics_scales_past_sql_variable_limit(self):
        # 运行数超过连接变量上限时聚合查询不能失败（评审实测回归）。
        store = self.store
        now = time.time()
        store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)", ("proj01", "fixture", str(Path(self.temp.name) / "project"), "{}", 1, now))
        for index in range(1100):
            task = "t%05d" % index
            run = "r%05d" % index
            store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,attempts,overrides,available,created,updated) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                          (task, "proj01", "goal", "fp" + task, "t", "b", "[]", "[]", "merged", 50, 0, "{}", now, now))
            store.execute("INSERT INTO runs(id,task,project,state,config,started,ended,result) VALUES (?,?,?,?,?,?,?,?)",
                          (run, task, "proj01", "completed", "{}", now, now,
                           json.dumps({"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}, "merged_at": now})))
            store.execute("INSERT INTO calls(id,project,run,role,model,effort,state,tokens,seconds,created,provider,unit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          ("c" + run, "proj01", run, "implementation", "m", "high", "completed", 10, 1.0, now, "openai", "http_request"))
        result = workflow_metrics(store)
        self.assertEqual(result["tasks"], 1100)
        self.assertEqual(result["accepted"], 1100)
        self.assertEqual(result["accepted_tokens"], 11000)

    @staticmethod
    def diagnostic_keys(store, config, project, provider, model):
        key = ProviderHealth(store).key(config, provider, project)
        return key, key + ":" + fingerprint({"model": model})

    def detail_failure(self, detail_key, attempted):
        """只让健康明细（detail）写入失败，准入状态与用量写入照常。"""
        original = Store.put_setting.__get__(self.store)

        def put(setting_key, value, *args, **kwargs):
            attempted.append(setting_key)
            if setting_key == detail_key:
                raise sqlite3.OperationalError("Injected diagnostic detail write failure")
            return original(setting_key, value, *args, **kwargs)
        return put

    def test_diagnostic_detail_write_failure_keeps_successful_model_result(self):
        # B06：健康明细只是诊断；调用用量已落盘后，明细写入失败不能丢弃成功结果。
        self.runtime.configure({"recovery": {"provider_retry_minutes": 5}})
        config = self.runtime.config()
        config["models"]["analysis"] = {"provider": "openai", "model": "fixture-model", "effort": "default",
                                        "fallback": {"provider": "openai", "model": "fallback-model", "effort": "default"}}
        key, detail = self.diagnostic_keys(self.store, config, "diagnostic", "openai", "fixture-model")
        now = time.time()
        self.store.put_setting(key, {"failures": 3, "paused": True, "code": "rate_limit", "at": now - 3600,
                                     "retry_at": 0, "generation": 1})
        agent = CodexAgent(self.store)
        invocations = []

        def fake_invoke(role, prompt, workspace, project, run, effective, cancel=None, protected=()):
            invocations.append(role)
            call = self.store.reserve_call(project, run, role, effective["models"][role], effective)
            self.store.record_usage(call, run, {"tokens": 15, "input_tokens": 10, "output_tokens": 5,
                                                "cache_read_tokens": 0, "cache_write_tokens": 0,
                                                "usage_known": True}, 0.1)
            self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
            return {"summary": "Successful fake model output"}

        attempted = []
        with patch.object(self.store, "put_setting",
                          side_effect=self.detail_failure(detail, attempted)), \
                patch.object(agent, "_invoke", side_effect=fake_invoke), \
                self.assertLogs("adaptive_rsi.provider_health", level="WARNING") as logs:
            result = agent.invoke("analysis", "fixture prompt", Path(self.temp.name), "diagnostic", "run-1", config)
        self.assertEqual(result, {"summary": "Successful fake model output"})
        self.assertEqual(invocations, ["analysis"])
        self.assertIn(detail, attempted)
        self.assertIn("diagnostic", " ".join(logs.output).lower())
        self.assertFalse(self.store.query("SELECT * FROM events WHERE kind='fallback'"))
        self.assertEqual(self.store.one("SELECT state,tokens FROM calls"), {"state": "completed", "tokens": 15})
        status = ProviderHealth(self.store).status(config, "openai", "diagnostic")
        self.assertEqual((status["failures"], status["paused"]), (0, False))
        self.assertNotIn("half_open_owner", status)
        self.assertNotIn("half_open_until", status)

    def test_diagnostic_detail_failure_preserves_admission_and_provider_error(self):
        # 失败路径：明细写入失败仍要留下准入状态、释放租约，并保持原始供应商错误。
        self.runtime.configure({"recovery": {"provider_retry_minutes": 5}})
        config = self.runtime.config()
        config["models"]["analysis"] = {"provider": "openai", "model": "fixture-model", "effort": "default"}
        key, detail = self.diagnostic_keys(self.store, config, "failed-diagnostic", "openai", "fixture-model")
        now = time.time()
        self.store.put_setting(key, {"failures": 3, "paused": True, "code": "rate_limit", "at": now - 3600,
                                     "retry_at": 0, "generation": 1})
        agent = CodexAgent(self.store)

        def fail(*args, **kwargs):
            raise ProviderUnavailable("Model API HTTP 401")

        attempted = []
        with patch.object(self.store, "put_setting",
                          side_effect=self.detail_failure(detail, attempted)), \
                patch.object(agent, "_invoke", side_effect=fail), \
                self.assertLogs("adaptive_rsi.provider_health", level="WARNING"):
            with self.assertRaises(ProviderUnavailable) as raised:
                agent.invoke("analysis", "fixture prompt", Path(self.temp.name), "failed-diagnostic", "run-2", config)
        self.assertIn(detail, attempted)
        self.assertEqual(raised.exception.code, "auth")
        status = ProviderHealth(self.store).status(config, "openai", "failed-diagnostic")
        self.assertEqual((status["failures"], status["code"]), (4, "auth"))
        self.assertNotIn("half_open_owner", status)

    def test_admission_state_write_failure_is_not_hidden_by_diagnostic_isolation(self):
        # 只有诊断明细可以隔离；准入状态本身写不进去时必须上抛，不能虚报已持久化。
        config = self.runtime.config()
        with patch.object(self.store, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                ProviderHealth(self.store).record(config, "openai", "db-failure")
