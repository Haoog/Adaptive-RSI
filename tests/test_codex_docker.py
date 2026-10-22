"""真实 Linux Codex CLI + 本地 Responses SSE 替身，不消耗付费模型。"""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.config import validate
from adaptive_rsi.store import Store
from tests.test_runtime import fixture


@unittest.skipUnless(os.environ.get("RSI_DOCKER_TESTS") == "1", "Enable RSI_DOCKER_TESTS=1 with the CLI worker image")
class CodexDockerTest(unittest.TestCase):
    def test_real_cli_validates_schema_and_records_usage(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                message = {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
                           "content": [{"type": "output_text", "text": '{"summary":"Linux executor ready"}', "annotations": []}]}
                response = {"id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
                            "model": "fixture", "output": [message], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                          {"type": "response.output_item.done", "output_index": 0, "item": message},
                          {"type": "response.completed", "response": response}]
                raw = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="rsi-codex-linux-") as directory:
                root = Path(directory).resolve()
                workspace = fixture(root / "project")
                config = validate({"sandbox": {"backend": "docker"}, "providers": [{"id": "fixture", "name": "Local fixture", "protocol": "codex",
                                   "base_url": "http://127.0.0.1:" + str(server.server_port) + "/v1", "env_key": ""}],
                                   "models": {"implementation": {"provider": "fixture", "model": "fixture", "effort": "default"}}})
                store = Store(root / "controller")
                began = time.monotonic()
                result = CodexAgent(store).invoke("implementation", "Return the structured summary.", workspace, "fixture", "run", config,
                                                 cancel=lambda: time.monotonic() - began > 60)
                self.assertEqual(result, {"summary": "Linux executor ready"})
                self.assertTrue(requests)
                self.assertEqual(store.one("SELECT state,tokens,unit FROM calls"), {"state": "completed", "tokens": 15, "unit": "codex_session"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
