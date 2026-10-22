"""真实 OpenCode CLI + 本地协议替身；不访问收费模型。"""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.config import validate
from adaptive_rsi.store import Store


@unittest.skipUnless(os.environ.get("RSI_OPENCODE_TESTS") == "1", "Enable RSI_OPENCODE_TESTS=1 with OpenCode installed")
class OpenCodeIntegrationTest(unittest.TestCase):
    def test_real_cli_reads_edits_and_returns_validated_result(self):
        requests = []
        with tempfile.TemporaryDirectory(prefix="rsi-real-opencode-") as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "calculator.py"
            source.write_text("def add(a,b): return a-b\n")
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(payload)
                    step = len(requests)
                    if step == 1:
                        name, args = "read", {"filePath": str(source)}
                    elif step == 2:
                        name, args = "edit", {"filePath": str(source), "oldString": "return a-b", "newString": "return a+b"}
                    if step <= 2:
                        delta = {"tool_calls": [{"index": 0, "id": "call_" + str(step), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                    else:
                        delta = {"content": '{"summary":"Fixed addition"}'}
                    chunks = [{"id": "chat-" + str(step), "object": "chat.completion.chunk", "model": "fixture", "created": 1,
                               "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                              {"id": "chat-" + str(step), "object": "chat.completion.chunk", "model": "fixture", "created": 1,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if step <= 2 else "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
                    raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            container = os.environ.get("RSI_DOCKER_TESTS") == "1"
            hostname = "host.docker.internal" if container else "127.0.0.1"
            connection = {"provider": {"fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                            "options": {"baseURL": "http://" + hostname + ":" + str(server.server_port) + "/v1", "apiKey": "fixture-key"},
                            "models": {"fixture": {"name": "Fixture", "limit": {"context": 64000, "output": 8192}}}}}}
            config = validate({"sandbox": {"backend": "docker" if container else "auto"},
                               "models": {"implementation": {"provider": "opencode", "model": "fixture/fixture", "effort": "default"}}})
            store = Store(root / "controller")
            began = time.monotonic()
            try:
                with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(connection)}):
                    result = CodexAgent(store).invoke("implementation", "Fix addition in calculator.py. Read before editing.", workspace,
                                                     "fixture", "run", config, cancel=lambda: time.monotonic() - began > 90)
                self.assertEqual(result, {"summary": "Fixed addition"})
                self.assertIn("return a+b", source.read_text())
                self.assertEqual(len(requests), 3)
                self.assertTrue(any("return a-b" in json.dumps(request["messages"]) for request in requests[1:]))
                call = store.one("SELECT state,tokens,unit,usage_known FROM calls")
                self.assertEqual(call, {"state": "completed", "tokens": 45, "unit": "opencode_session", "usage_known": 1})
                self.assertFalse(any(p.name == "auth.json" for p in workspace.rglob("*")))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
