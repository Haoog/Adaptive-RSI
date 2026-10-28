import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent, model_capabilities
from adaptive_rsi.config import validate
from adaptive_rsi.gitops import git
from adaptive_rsi.native import Conversation, NativeAgent, WorkspaceTools, tools_for
from adaptive_rsi.store import Store


class ProtocolFixture:
    """Real HTTP transport; responses emulate each protocol, never a paid model."""
    def __init__(self, protocol, behavior="normal"):
        self.protocol, self.behavior, self.requests = protocol, behavior, []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append((self.path, dict(self.headers), payload))
                if fixture.behavior == "unavailable":
                    self.send_response(429)
                    self.end_headers()
                    return
                step = len(fixture.requests)
                name, args = [("read_file", {"path": "source.py", "start_line": 1, "end_line": 10}),
                              ("replace_text", {"path": "source.py", "old": "return a-b", "new": "return a+b"}),
                              ("finish", {"summary": "Fixed addition"})][min(step - 1, 2)]
                if fixture.behavior == "long_loop" and step <= 35:
                    name, args = "read_file", {"path": "source.py", "start_line": 1, "end_line": 10}
                if fixture.behavior == "bad_finish":
                    # One invalid finish, then the model corrects itself on the next turn.
                    name, args = ("finish", {}) if step == 1 else ("finish", {"summary": "Fixed addition"})
                if fixture.behavior == "bad_arguments":
                    # Arguments that are not valid JSON; the agent must report them back instead of failing the run.
                    name, args = ("read_file", "{\"path") if step == 1 else ("finish", {"summary": "Fixed addition"})
                if fixture.behavior == "text_only":
                    data = {"choices": [{"message": {"role": "assistant", "content": "I am done"}}]}
                elif protocol == "responses":
                    data = {"output": [{"type": "function_call", "id": "fc" + str(step), "call_id": "call" + str(step), "name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}], "usage": {"input_tokens": 10, "output_tokens": 5}}
                elif protocol in ["chat_completions", "ollama"]:
                    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "call" + str(step), "type": "function", "function": {"name": name, "arguments": args if (protocol == "ollama" or isinstance(args, str)) else json.dumps(args)}}]}
                    data = {"message": message, "prompt_eval_count": 10, "eval_count": 5} if protocol == "ollama" else {"choices": [{"message": message}], "usage": {"total_tokens": 15}}
                elif protocol == "anthropic":
                    data = {"role": "assistant", "content": [{"type": "tool_use", "id": "call" + str(step), "name": name, "input": args}], "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}}
                else:
                    data = {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}, "thoughtSignature": "opaque-test-signature"}]}}], "usageMetadata": {"totalTokenCount": 15}}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)


class NativeProtocolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_text("def add(a,b): return a-b\n")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, fixture, protocol, **limits):
        return validate({"providers": [{"id": "native", "name": "Fixture", "base_url": fixture.url,
                                        "env_key": "RSI_PROTOCOL_TEST_KEY", "protocol": protocol}],
                         "models": {"implementation": {"provider": "native", "model": "test-model", "effort": "default"}},
                         "limits": limits})

    def test_all_protocols_perform_real_http_tool_round_trips_without_codex(self):
        for protocol in ["responses", "chat_completions", "anthropic", "gemini", "ollama"]:
            with self.subTest(protocol=protocol), ProtocolFixture(protocol) as fixture:
                (self.workspace / "source.py").write_text("def add(a,b): return a-b\n")
                config = self.config(fixture, protocol)
                with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}), patch("adaptive_rsi.agent.shutil.which", return_value=None):
                    result = CodexAgent(self.store).invoke("implementation", "Fix source", self.workspace, protocol, protocol, config)
                self.assertEqual(result, {"summary": "Fixed addition"})
                self.assertIn("return a+b", (self.workspace / "source.py").read_text())
                self.assertEqual(len(fixture.requests), 3)
                calls = self.store.query("SELECT * FROM calls WHERE project=?", (protocol,))
                self.assertEqual([c["state"] for c in calls], ["completed"] * 3)
                self.assertEqual(sum(c["tokens"] for c in calls), 51 if protocol == "anthropic" else 45)
                path, headers, body = fixture.requests[0]
                auth = {k.lower(): v for k, v in headers.items()}
                self.assertEqual(auth.get({"anthropic": "x-api-key", "gemini": "x-goog-api-key"}.get(protocol, "authorization")), "test-only-secret" if protocol in ["anthropic", "gemini"] else "Bearer test-only-secret")
                self.assertNotIn("test-only-secret", json.dumps(body))
                self.assertNotIn("test-only-secret", path)
                second = fixture.requests[1][2]
                if protocol == "gemini":
                    self.assertEqual(second["contents"][1]["parts"][0]["thoughtSignature"], "opaque-test-signature")
                    self.assertIn("functionResponse", second["contents"][2]["parts"][0])
                elif protocol == "anthropic":
                    self.assertEqual(second["messages"][2]["content"][0]["tool_use_id"], "call1")
                    self.assertEqual(auth["anthropic-version"], "2023-06-01")
                elif protocol == "responses":
                    self.assertEqual(second["input"][2]["type"], "function_call_output")
                else:
                    self.assertEqual(second["messages"][2]["role"], "tool")

    def test_unsupported_results_fail_closed(self):
        with ProtocolFixture("chat_completions", "text_only") as fixture:
            config = self.config(fixture, "chat_completions", run_calls=1)
            with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}), self.assertRaises(RuntimeError):
                CodexAgent(self.store).invoke("implementation", "task", self.workspace, "text_only", "text_only", config)
            self.assertEqual(len(fixture.requests), 1)
            state = self.store.one("SELECT state FROM calls WHERE project=?", ("text_only",))["state"]
            self.assertEqual(state, "failed")

    def test_invalid_finish_arguments_are_reported_back_and_retried(self):
        for protocol in ["responses", "chat_completions"]:
            project = "bad-finish-" + protocol
            with self.subTest(protocol=protocol), ProtocolFixture(protocol, "bad_finish") as fixture:
                config = self.config(fixture, protocol)
                with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}):
                    result = CodexAgent(self.store).invoke("implementation", "task", self.workspace, project, project, config)
                self.assertEqual(result, {"summary": "Fixed addition"})
                self.assertEqual(len(fixture.requests), 2)
                retry = fixture.requests[1][2]
                if protocol == "responses":
                    self.assertEqual(retry["input"][2]["type"], "function_call_output")
                    self.assertEqual(retry["input"][2]["call_id"], "call1")
                    error = retry["input"][2]["output"]
                else:
                    self.assertEqual(retry["messages"][2]["role"], "tool")
                    error = retry["messages"][2]["content"]
                self.assertIn("finish arguments failed schema validation", error)
                states = [c["state"] for c in self.store.query("SELECT state FROM calls WHERE project=? ORDER BY created", (project,))]
                self.assertEqual(states, ["completed", "completed"])

    def test_unparseable_tool_arguments_are_reported_back_and_retried(self):
        for protocol in ["responses", "chat_completions", "anthropic", "gemini", "ollama"]:
            with self.subTest(protocol=protocol), ProtocolFixture(protocol, "bad_arguments") as fixture:
                config = self.config(fixture, protocol)
                with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}):
                    result = CodexAgent(self.store).invoke("implementation", "task", self.workspace, "bad-args-" + protocol, "bad-args", config)
                self.assertEqual(result, {"summary": "Fixed addition"})
                self.assertEqual(len(fixture.requests), 2)
                retry = fixture.requests[1][2]
                if protocol == "responses":
                    self.assertEqual(retry["input"][2]["type"], "function_call_output")
                    self.assertEqual(retry["input"][2]["call_id"], "call1")
                elif protocol in ["chat_completions", "ollama"]:
                    self.assertEqual(retry["messages"][2]["role"], "tool")
                elif protocol == "anthropic":
                    self.assertEqual(retry["messages"][2]["content"][0]["tool_use_id"], "call1")
                else:
                    self.assertEqual(retry["contents"][2]["parts"][0]["functionResponse"]["name"], "read_file")
                self.assertIn("not valid JSON", json.dumps(retry))
                states = [c["state"] for c in self.store.query("SELECT state FROM calls WHERE project=? ORDER BY created", ("bad-args-" + protocol,))]
                self.assertEqual(states, ["completed", "completed"])

    def test_probe_uses_short_timeout_while_other_roles_allow_long_generation(self):
        captured = []

        def fake_post_json(url, payload, headers, timeout, max_bytes):
            captured.append(timeout)
            return {"output": [{"type": "function_call", "call_id": "call1", "name": "finish", "arguments": json.dumps({"summary": "RSI_OK"})}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        provider = {"id": "native", "name": "Fixture", "protocol": "responses", "base_url": "http://127.0.0.1:9", "env_key": ""}
        route = {"model": "test-model", "effort": "default"}
        config = validate({})
        with patch("adaptive_rsi.native.post_json", side_effect=fake_post_json):
            probe = NativeAgent(self.store).invoke("probe", "Probe the loop", self.workspace, "timeout-probe", "timeout", config, route, provider)
            implementation = NativeAgent(self.store).invoke("implementation", "Implement it", self.workspace, "timeout-implementation", "timeout", config, route, provider)
        self.assertEqual(probe, {"summary": "RSI_OK"})
        self.assertEqual(implementation, {"summary": "RSI_OK"})
        # 实现角色常触发长推理：预算放宽到 900 秒（探针仍是 30 秒）。
        self.assertEqual(captured, [30, 900])

    def test_tool_loop_continues_past_old_call_and_turn_limits(self):
        with ProtocolFixture("chat_completions", "long_loop") as fixture:
            config = self.config(fixture, "chat_completions", run_calls=1, daily_calls=1)
            with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}):
                result = CodexAgent(self.store).invoke("implementation", "task", self.workspace, "long", "long", config)
            self.assertEqual(result, {"summary": "Fixed addition"})
            self.assertEqual(len(fixture.requests), 36)

    def test_cross_protocol_fallback_records_both_routes(self):
        with ProtocolFixture("responses", "unavailable") as first, ProtocolFixture("anthropic") as second:
            config = self.config(first, "responses")
            config["providers"].append({"id": "fallback", "name": "Fallback", "protocol": "anthropic", "base_url": second.url, "env_key": ""})
            config["models"]["implementation"]["fallback"] = {"provider": "fallback", "model": "fallback-model", "effort": "default"}
            with patch.dict(os.environ, {"RSI_PROTOCOL_TEST_KEY": "test-only-secret"}):
                CodexAgent(self.store).invoke("implementation", "task", self.workspace, "project", "run", validate(config))
            calls = self.store.query("SELECT provider,state FROM calls ORDER BY created")
            self.assertEqual(calls[0], {"provider": "native", "state": "failed"})
            self.assertEqual(calls[-1], {"provider": "fallback", "state": "completed"})

    def test_codex_fallback_is_allowed_only_when_primary_left_workspace_unchanged(self):
        workspace = self.root / "fallback-workspace"
        workspace.mkdir()
        source = workspace / "source.py"
        source.write_text("original\n")
        git(workspace, "init", "--quiet")
        git(workspace, "add", "source.py")
        git(workspace, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "base")
        config = validate({"models": {"implementation": {
            "provider": "openai", "model": "primary", "effort": "default",
            "fallback": {"provider": "openai", "model": "fallback", "effort": "default"}}}})
        agent = CodexAgent(self.store)
        with patch.object(CodexAgent, "_invoke", side_effect=[
                RuntimeError("Codex call failed: rate limit exceeded"), {"summary": "fallback result"}]) as call:
            result = agent.invoke("implementation", "task", workspace, "fallback-safe", "no-edit", config)
        self.assertEqual(result, {"summary": "fallback result"})
        self.assertEqual(call.call_count, 2)

        def partial_edit(role, prompt, current, *args, **kwargs):
            source.write_text("partial primary edit\n")
            raise RuntimeError("Codex call failed: rate limit exceeded")

        with patch.object(CodexAgent, "_invoke", side_effect=partial_edit) as call:
            with self.assertRaisesRegex(RuntimeError, "fallback refused"):
                agent.invoke("implementation", "task", workspace, "fallback-safe", "partial-edit", config)
        call.assert_called_once()
        self.assertEqual(source.read_text(), "partial primary edit\n")

    def test_workspace_tools_cannot_escape_or_write_during_review(self):
        config = validate({})
        tools = WorkspaceTools(self.workspace, "review", self.store.root, config, None, [])
        (self.workspace / "escape").symlink_to(self.root)
        (self.workspace / "metadata").symlink_to(self.workspace / ".git")
        for path in ["../outside", "escape/state/state.sqlite3", ".git/config", "metadata/config", ".rsi-agent/result.json"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                tools.execute("read_file", {"path": path, "start_line": 1, "end_line": 1})
        with self.assertRaisesRegex(ValueError, "not available"):
            tools.execute("write_file", {"path": "source.py", "content": "changed"})
        with self.assertRaisesRegex(ValueError, "not available"):
            tools.execute("run_command", {"argv": ["touch", "source.py"], "cwd": "."})
        self.assertIn("return a-b", (self.workspace / "source.py").read_text())

    def test_vendor_reasoning_is_not_silently_translated(self):
        for protocol in ["anthropic", "gemini", "ollama"]:
            with self.assertRaisesRegex(RuntimeError, "effort=default"):
                Conversation(protocol, "model", "prompt", tools_for("review"), "high").request()

    def test_conversation_sends_the_model_output_limit(self):
        for protocol, key in (("responses", "max_output_tokens"), ("chat_completions", "max_tokens")):
            with self.subTest(protocol=protocol):
                _, body = Conversation(protocol, "model", "prompt", [], "default", output_tokens=1234).request()
                self.assertEqual(body[key], 1234)
        _, body = Conversation("chat_completions", "o3", "prompt", [], "default", output_tokens=1234).request()
        self.assertEqual(body["max_completion_tokens"], 1234)
        self.assertNotIn("max_tokens", body)
        _, body = Conversation("ollama", "model", "prompt", [], "default", output_tokens=1234).request()
        self.assertEqual(body["options"]["num_predict"], 1234)
        _, body = Conversation("anthropic", "claude-sonnet", "prompt", [], "default", output_tokens=1234).request()
        self.assertEqual(body["max_tokens"], 1234)
        # Vendors cap their own maximum: never exceed the historical envelope.
        _, body = Conversation("anthropic", "claude-opus", "prompt", [], "default", output_tokens=32000).request()
        self.assertEqual(body["max_tokens"], 4096)

    def test_known_gpt_families_use_documented_output_ceilings(self):
        self.assertEqual(model_capabilities("gpt-4o", "chat_completions")["max_output_tokens"], 16384)
        self.assertEqual(model_capabilities("gpt-4.1", "responses")["max_output_tokens"], 32768)
        self.assertEqual(model_capabilities("o3", "responses")["max_output_tokens"], 100000)
        self.assertEqual(model_capabilities("gpt-future", "responses")["max_output_tokens"], 16384)


if __name__ == "__main__":
    unittest.main()
