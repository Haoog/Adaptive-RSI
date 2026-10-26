import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.cli import doctor
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
        with self.assertRaises(ProviderPaused):
            ProviderHealth(Store(self.store.root)).require(config, "openai", "first")
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
