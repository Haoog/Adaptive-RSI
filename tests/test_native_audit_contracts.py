"""Offline contracts for native tool indexing and response boundaries."""
import copy
import json
import subprocess
import tempfile
import unittest
import urllib.error
import http.client
from pathlib import Path
from unittest.mock import patch
from email.message import Message

from adaptive_rsi.agent import model_capabilities, request_output_budget
from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.native import Conversation, NativeAgent, WorkspaceTools, tools_for, post_json, ProviderUnavailable, execute_tools
from adaptive_rsi.provider_health import ModelFailure
from adaptive_rsi.recovery import failure
from adaptive_rsi.store import Store


class NativeAuditContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = WorkspaceTools(self.root, "review", self.root / "state", {"limits": {"command_seconds": 30}}, None, ())

    def test_capability_override_has_provenance_and_rejects_invalid_limits(self):
        from adaptive_rsi.agent import model_capabilities
        result = model_capabilities("unknown", "responses", {"context_tokens": 4096,
            "max_output_tokens": 1024, "updated_at": "2026-10-28"})
        self.assertEqual(result["context_tokens"], 4096)
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["updated_at"], "2026-10-28")
        self.assertFalse(result["streaming_implemented"])
        self.assertEqual(result["metadata_version"], 1)
        for override in [{"context_tokens": True}, {"max_output_tokens": -1},
                         {"context_tokens": 4096, "max_output_tokens": 8192}]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                model_capabilities("unknown", "responses", override)
        heuristic = model_capabilities("gpt-future-unverified", "responses")
        self.assertEqual(heuristic["confidence"], "heuristic")
        self.assertIsNone(heuristic["updated_at"])

    def test_transport_receipts_distinguish_rejected_and_ambiguous_billing(self):
        errors = [
            (urllib.error.URLError(ConnectionRefusedError()), "not_sent", False),
            (http.client.RemoteDisconnected("response lost"), "unknown", True),
            (urllib.error.HTTPError("http://localhost", 429, "limited", Message(), None), "rejected", False),
        ]
        for error, outcome, possible in errors:
            with self.subTest(outcome=outcome), patch("adaptive_rsi.native.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ProviderUnavailable) as caught:
                    post_json("http://localhost", {}, {}, 1, 1000)
                self.assertEqual(caught.exception.request_outcome, outcome)
                self.assertEqual(caught.exception.possibly_billed, possible)
                self.assertEqual(caught.exception.retry_may_duplicate, possible)

    def test_malformed_protocol_responses_do_not_mutate_history(self):
        fixtures = {
            "responses": {"output": [{"type": "function_call", "call_id": [], "name": "search", "arguments": "{}"}]},
            "chat_completions": {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": [], "function": {"name": "search", "arguments": "{}"}}]}}]},
            "ollama": {"message": {"role": "assistant", "tool_calls": [{"id": [], "function": {"name": "search", "arguments": {}}}]}},
            "anthropic": {"content": [{"type": "tool_use", "id": [], "name": "search", "input": {}}]},
            "gemini": {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"id": [], "name": "search", "args": {}}}]}}]},
        }
        for protocol, response in fixtures.items():
            with self.subTest(protocol=protocol):
                conversation = Conversation(protocol, "model", "task", tools_for("review"), "default")
                before = json.dumps(conversation.history)
                with self.assertRaisesRegex(RuntimeError, "invalid tool response structure"):
                    conversation.receive(response)
                self.assertEqual(json.dumps(conversation.history), before)

    def test_regex_search_and_invalid_patterns(self):
        (self.root / "source.py").write_text("item 12\nitem no\nITEM 34\n")
        result = self.tools.execute("search", {"text": r"item \d+", "regex": True, "case_sensitive": False})
        self.assertEqual([row["line"] for row in result["matches"]], [1, 3])
        with self.assertRaisesRegex(ValueError, "Invalid regular expression"):
            self.tools.execute("search", {"text": "[", "regex": True})

    def test_regex_pathological_pattern_is_bounded(self):
        (self.root / "source.py").write_text("a" * 10000 + "!\n")
        result = self.tools.execute("search", {"text": "(a+)+$", "regex": True})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reason"], "regex time limit")
        self.assertIsNotNone(result["next_cursor"])

    def test_regex_pages_and_invalid_search_inputs(self):
        (self.root / "a.py").write_text("item 1\nitem 2\nitem 3\n")
        first = self.tools.execute("search", {"text": r"item \d", "regex": True, "limit": 1})
        second = self.tools.execute("search", {"text": r"item \d", "regex": True, "limit": 2,
                                                "cursor": first["next_cursor"]})
        self.assertEqual([row["line"] for row in second["matches"]], [2, 3])
        self.assertFalse(second["truncated"])
        for extra in [{"text": "x" * 1001}, {"limit": 0}, {"path": "../*"},
                      {"cursor": "[]"}, {"text": "different", "cursor": first["next_cursor"]}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.tools.execute("search", {"text": "item", **extra})

    def test_regex_cancellation_terminates_worker(self):
        from adaptive_rsi.process import Cancelled
        (self.root / "a.py").write_text("a" * 10000 + "!\n")
        import time
        started = time.monotonic()
        self.tools.cancel = lambda: time.monotonic() - started > 0.2
        with self.assertRaises(Cancelled):
            self.tools.execute("search", {"text": "(a+)+$", "regex": True})

    def test_skipped_files_are_reported(self):
        (self.root / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))
        (self.root / "binary.py").write_bytes(b"\xff")
        result = self.tools.execute("search", {"text": "x"})
        self.assertEqual(result["skipped_files"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reason"], "unreadable or oversized files")

    def test_unicode_search_budget_keeps_results_and_continuation(self):
        (self.root / "a.py").write_text(("中" * 1000 + "\n") * 100)
        result = execute_tools(self.tools, [("one", "search", {"text": "中"})])[0][2]
        self.assertGreater(len(result["matches"]), 0)
        self.assertLess(len(json.dumps(result).encode()), 128000)
        self.assertEqual(result["reason"], "result byte limit")
        self.assertIsNotNone(result["next_cursor"])

    def test_search_scopes_case_and_resumes_matches(self):
        (self.root / "a.py").write_text("HELLO 1\nhello 2\nhello 3\n")
        (self.root / "a.txt").write_text("hello excluded\n")
        first = self.tools.execute("search", {"text": "hello", "path": "*.py", "case_sensitive": False, "limit": 2})
        self.assertEqual([row["line"] for row in first["matches"]], [1, 2])
        self.assertEqual(first["reason"], "match limit")
        second = self.tools.execute("search", {"text": "hello", "path": "*.py", "case_sensitive": False,
                                                "cursor": first["next_cursor"], "limit": 2})
        self.assertEqual([row["line"] for row in second["matches"]], [3])
        self.assertFalse(second["truncated"])

    def test_git_index_honors_nested_ignore_and_tracked_inclusion(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("*.log\n!keep.log\n")
        (self.root / "a.log").write_text("ignored")
        (self.root / "keep.log").write_text("included")
        (self.root / "tracked.log").write_text("tracked")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "tracked.log"], check=True)
        (self.root / "sub").mkdir()
        (self.root / "sub/.gitignore").write_text("private.py\n")
        (self.root / "sub/private.py").write_text("ignored")
        (self.root / "sub/new.py").write_text("source")
        (self.root / ".rsiignore").write_text("excluded.py\n")
        (self.root / "excluded.py").write_text("user excluded")
        files = self.tools.files()
        self.assertNotIn("a.log", files)
        self.assertNotIn("sub/private.py", files)
        self.assertNotIn("excluded.py", files)
        self.assertTrue({"keep.log", "tracked.log", "sub/new.py"}.issubset(files))

    def test_list_pages_can_be_resumed_without_loss(self):
        for name in ["a.py", "b.py", "c.py"]:
            (self.root / name).write_text("line\n")
        first = self.tools.execute("list_files", {"limit": 2})
        self.assertEqual(first["files"], ["a.py", "b.py"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["reason"], "page limit")
        second = self.tools.execute("list_files", {"offset": first["next_offset"], "limit": 2})
        self.assertEqual(second["files"], ["c.py"])
        self.assertIsNone(second["next_offset"])

    def native_store(self, name="state"):
        store = Store(self.root / name)
        self.addCleanup(store.close)
        return store

    @staticmethod
    def anthropic_provider():
        return {"id": "native", "protocol": "anthropic", "base_url": "http://127.0.0.1:9",
                "credential": "none", "env_key": ""}

    def anthropic_invoke(self, store, workspace, model="claude-sonnet-4-5"):
        route = {"provider": "native", "model": model, "effort": "default"}
        return NativeAgent(store).invoke("probe", "test", workspace, "p", "budget-run",
                                         copy.deepcopy(DEFAULTS), route, self.anthropic_provider())

    def test_anthropic_request_budget_is_separate_from_capability(self):
        # O03：max_output_tokens 是模型能力上限；单次请求预算由一处策略给出且不得超过能力。
        expected = {"claude-opus-4-1": 16384, "claude-sonnet-4-5": 16384, "claude-haiku-4-5": 8192,
                    "claude-3-5-sonnet": 8192, "claude-opus": 4096, "claude-sonnet": 8192,
                    "claude-2.1": 4096, "unknown-model": 8192}
        for model, budget in expected.items():
            with self.subTest(model=model):
                capability = model_capabilities(model, "anthropic")
                self.assertEqual(capability["request_output_tokens"], budget)
                self.assertEqual(request_output_budget(model, "anthropic"), budget)
                self.assertEqual(request_output_budget(model, "anthropic", capability["max_output_tokens"]), budget)
                self.assertLess(capability["request_output_tokens"], capability["max_output_tokens"])
                self.assertEqual(capability["max_output_tokens"], 32000 if "claude" in model else 16384)
        # 旧型号保持 4096 的历史包络，新型号只在能力之内提高预算。
        _, body = Conversation("anthropic", "claude-opus", "prompt", [], "default", output_tokens=32000).request()
        self.assertEqual(body["max_tokens"], 4096)
        # 手工能力覆盖更小时，预算随能力收缩。
        override = model_capabilities("claude-sonnet-4-5", "anthropic", {"max_output_tokens": 2048})
        self.assertEqual(override["request_output_tokens"], 2048)
        # 请求预算不得超过能力，也不得超过调用方传入的能力上限。
        capability = model_capabilities("claude-sonnet-4-5", "anthropic")
        for model, output_tokens, requested, limit in [
                ("claude-sonnet-4-5", capability["max_output_tokens"], capability["request_output_tokens"], 16384),
                ("claude-sonnet-4-5", 1024, capability["request_output_tokens"], 1024),
                ("claude-opus", capability["max_output_tokens"], None, 4096)]:
            with self.subTest(model=model, output_tokens=output_tokens, requested=requested):
                _, body = Conversation("anthropic", model, "prompt", [], "default",
                                       output_tokens=output_tokens, request_output_tokens=requested).request()
                self.assertEqual(body["max_tokens"], limit)
                self.assertLessEqual(body["max_tokens"], capability["max_output_tokens"])

    def test_anthropic_legacy_claude3_named_models_keep_conservative_budget(self):
        # O03：claude-3-opus/sonnet/haiku（含日期后缀与供应商前缀）历史上就是 4096 请求预算，
        # 更具体的规则必须先于 claude-3 通配命中；3-5/3-7 与 4.x 的新预算不受影响。
        for model in ["claude-3-opus", "claude-3-opus-20240229", "anthropic/claude-3-opus-20240229",
                      "claude-3-sonnet", "claude-3-sonnet-20240229",
                      "claude-3-haiku", "us.anthropic.claude-3-haiku-20240307-v1:0"]:
            with self.subTest(model=model):
                self.assertEqual(request_output_budget(model, "anthropic"), 4096)
                capability = model_capabilities(model, "anthropic")
                self.assertEqual(capability["request_output_tokens"], 4096)
                _, body = Conversation("anthropic", model, "prompt", [], "default",
                                       output_tokens=capability["max_output_tokens"]).request()
                self.assertEqual(body["max_tokens"], 4096)
        for model, budget in [("claude-3-5-sonnet", 8192), ("claude-3-5-sonnet-20241022", 8192),
                              ("claude-3-7-sonnet-20250219", 8192), ("claude-3-5-haiku-20241022", 8192),
                              ("claude-sonnet-4-5-20250929", 16384), ("claude-opus-4-1", 16384),
                              ("anthropic.claude-opus-4-1-20250805-v1:0", 16384),
                              ("claude-haiku-4-5-20251001", 8192)]:
            with self.subTest(model=model):
                self.assertEqual(request_output_budget(model, "anthropic"), budget)

    def test_native_anthropic_request_sends_policy_budget_within_capability(self):
        store = self.native_store()
        workspace = self.root / "workspace"
        workspace.mkdir()
        response = {"content": [{"type": "tool_use", "id": "1", "name": "finish", "input": {"summary": "RSI_OK"}}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2}}
        with patch("adaptive_rsi.native.post_json", return_value=response) as post:
            self.assertEqual(self.anthropic_invoke(store, workspace), {"summary": "RSI_OK"})
        payload = post.call_args.args[1]
        capability = model_capabilities("claude-sonnet-4-5", "anthropic")
        self.assertEqual(payload["max_tokens"], capability["request_output_tokens"])
        self.assertLessEqual(payload["max_tokens"], capability["max_output_tokens"])
        self.assertEqual(post.call_count, 1)

    def test_output_limit_truncated_finish_is_retried_once_and_keeps_usage(self):
        store = self.native_store()
        workspace = self.root / "workspace"
        workspace.mkdir()
        truncated = [{"content": [{"type": "tool_use", "id": call, "name": "finish",
                                   "input": "{\"summary\": \"RSI_OK\""}],
                      "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 5}}
                     for call in ("1", "2")]
        with patch("adaptive_rsi.native.post_json", side_effect=truncated) as post:
            with self.assertRaises(ModelFailure) as raised:
                self.anthropic_invoke(store, workspace)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(raised.exception.failure_code, "execution")
        self.assertEqual(raised.exception.code, "tool_format")
        self.assertEqual(raised.exception.stop_reason, "output_limit")
        self.assertEqual(failure(raised.exception)["action"], "adjust_output_limit")
        capability = model_capabilities("claude-sonnet-4-5", "anthropic")
        self.assertEqual([call.args[1]["max_tokens"] for call in post.call_args_list],
                         [capability["request_output_tokens"]] * 2)
        calls = store.query("SELECT state,tokens,usage_known FROM calls ORDER BY created")
        self.assertEqual([row["state"] for row in calls], ["completed", "failed"])
        self.assertEqual([row["tokens"] for row in calls], [15, 15])
        self.assertEqual([row["usage_known"] for row in calls], [1, 1])

    def test_output_limit_repair_budget_is_cumulative_for_the_whole_call(self):
        # O03：一次 call 只允许一次截断修复；普通工具轮不得把计数清零后再付费重试，
        # 且失败提示必须是可执行的“缩短/拆分”，而不是不可达的“提高输出预算”。
        store = self.native_store()
        workspace = self.root / "workspace"
        workspace.mkdir()

        def truncated(call):
            return {"content": [{"type": "tool_use", "id": call, "name": "finish",
                                 "input": "{\"summary\": \"RSI_OK\""}],
                    "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 5}}

        tool_turn = {"content": [{"type": "tool_use", "id": "tool", "name": "list_files", "input": {}}],
                     "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}
        responses = [truncated("1"), tool_turn, truncated("2"), truncated("3")]
        with patch("adaptive_rsi.native.post_json", side_effect=responses) as post:
            with self.assertRaises(ModelFailure) as raised:
                self.anthropic_invoke(store, workspace)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(raised.exception.stop_reason, "output_limit")
        message = str(raised.exception)
        self.assertIn("shorten", message)
        self.assertIn("split", message)
        self.assertNotIn("raise the model output budget", message)
        calls = store.query("SELECT state,tokens,usage_known FROM calls ORDER BY created")
        self.assertEqual([row["state"] for row in calls], ["completed", "completed", "failed"])
        self.assertEqual([row["tokens"] for row in calls], [15, 15, 15])
        self.assertEqual([row["usage_known"] for row in calls], [1, 1, 1])

    def test_malformed_tool_arguments_are_not_bounded_as_output_limit(self):
        # 普通工具参数格式问题仍会回传模型并允许纠正，不因输出上限规则永久失败。
        for stop in ("end_turn", "max_tokens"):
            with self.subTest(stop_reason=stop):
                store = self.native_store("state-" + stop)
                workspace = self.root / ("workspace-" + stop)
                workspace.mkdir()
                malformed = {"content": [{"type": "tool_use", "id": "1", "name": "search", "input": "{\"text\":"}],
                             "stop_reason": stop, "usage": {"input_tokens": 4, "output_tokens": 1}}
                completed = {"content": [{"type": "tool_use", "id": "2", "name": "finish",
                                          "input": {"summary": "RSI_OK"}}],
                             "stop_reason": "end_turn", "usage": {"input_tokens": 4, "output_tokens": 2}}
                with patch("adaptive_rsi.native.post_json", side_effect=[malformed, completed]) as post:
                    self.assertEqual(self.anthropic_invoke(store, workspace), {"summary": "RSI_OK"})
                self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
