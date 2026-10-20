import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.model_center import CenterError
from adaptive_rsi.native import ProviderUnavailable
from adaptive_rsi.provider_health import ProviderHealth, ProviderPaused
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.telemetry import quality_summary
from tests.test_model_center import DirectoryServer


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
            with self.assertRaises(CenterError) as caught:
                self.center.probe("gateway", "coder-a", "default")
            self.assertEqual(caught.exception.code, "budget_exhausted")
            model = self.center.directory(self.center.provider("gateway"))["models"][0]
            self.assertEqual(model["probes"]["default"]["state"], "pass")
            self.assertEqual(model["last_attempt"]["code"], "budget_exhausted")

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
