"""模型中心移除确认与连接步骤专项检查的暂停控制台：临时状态目录、本地 mock 模型目录、零付费。

基于 tests/browser_fixture.py 的模式，仅预置多种凭据形态的连接，
不调用外部 CLI，不产生真实模型调用。
"""
import json
import sys
import tempfile
import threading
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.auth_sessions import AuthSessions
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


class MockAuthWorker:
    """替换外部 CLI 的假认证 worker：立即返回可授权状态，凭据写入临时 CODEX_HOME。"""

    def __init__(self, backend):
        self.backend = backend
        self.home = tempfile.TemporaryDirectory(prefix="rsi-mc-browser-auth-")
        self.credentials = Path(self.home.name) / "auth.json"
        self.credentials.write_text(json.dumps({
            "tokens": {"access_token": "browser-access", "refresh_token": "browser-refresh"}}))
        self.closed = threading.Event()

    def start(self, provider=None, method=0, inputs=None):
        return {"verification_url": "https://example.test/device", "user_code": "PROBE-CODE-01",
                "method": "auto", "instructions": "浏览器专项假登录流程"}

    def wait(self, stop, deadline):
        # 模拟用户 1.5 秒后在官方页面完成授权。
        return not stop.wait(1.5) and time.time() < deadline

    def providers(self):
        return []

    def close(self):
        self.closed.set()
        self.home.cleanup()


class DirectoryServer(ThreadingHTTPServer):
    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def main():
    with tempfile.TemporaryDirectory(prefix="rsi-model-center-browser-") as directory:
        root = Path(directory).resolve()
        os.environ["CODEX_HOME"] = str(root / "credentials/codex")
        os.environ["XDG_DATA_HOME"] = str(root / "credentials/data")
        # 避免本机真实 codex/opencode CLI 参与目录获取。
        os.environ["PATH"] = "/usr/bin:/bin"
        runtime = Runtime(Store(root / "state"), FakeAgent())
        runtime.auth_sessions.close()
        runtime.auth_sessions = AuthSessions(root / "state", factory=MockAuthWorker)

        class Directory(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"data": [
                    {"id": "fixture-mini"},
                    {"id": "fixture-pro", "displayName": "Fixture Pro"},
                ]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        print("Model center browser fixture: starting local model directory", file=sys.stderr, flush=True)
        directory = DirectoryServer(("127.0.0.1", 0), Directory)
        threading.Thread(target=directory.serve_forever, daemon=True).start()
        server = None
        try:
            base = "http://127.0.0.1:" + str(directory.server_port)
            # A：无需密钥的本地目录，已获取目录。
            a = runtime.model_center.save_provider(
                {"name": "本地目录服务", "protocol": "chat_completions",
                 "base_url": base, "env_key": ""}, {"mode": "none"})
            runtime.model_center.fetch(a["id"])
            # B：文件密钥中转，已获取目录；全局 planning 角色引用其模型（移除应被服务端拒绝）。
            b = runtime.model_center.save_provider(
                {"name": "引用中转", "protocol": "chat_completions",
                 "base_url": base, "env_key": ""}, {"mode": "file", "value": "sk-fixture-file-key"})
            runtime.model_center.fetch(b["id"])
            models = runtime.config()["models"]
            models["planning"] = {"provider": b["id"], "model": "fixture-pro",
                                  "effort": "default", "fallback": None}
            runtime.model_center.save_routes(models)
            # C：会话密钥中转，已获取目录；供确认后移除。
            c = runtime.model_center.save_provider(
                {"name": "会话中转", "protocol": "chat_completions",
                 "base_url": base, "env_key": ""}, {"mode": "session", "value": "sk-fixture-session-key"})
            runtime.model_center.fetch(c["id"])
            # D：文件密钥中转，未获取目录；用于核对第 2、3 步的先后状态。
            runtime.model_center.save_provider(
                {"name": "未获取中转", "protocol": "chat_completions",
                 "base_url": base, "env_key": ""}, {"mode": "file", "value": "sk-fixture-pending-key"})
            project = runtime.add_project(fixture(root / "project"), "模型中心验证项目")
            runtime.pause(project, True)
            server = Server(runtime)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print("Model center browser fixture: console ready", file=sys.stderr, flush=True)
            print(server.url, flush=True)
            for line in sys.stdin:
                if not line.strip():
                    break
        finally:
            if server:
                server.shutdown()


if __name__ == "__main__":
    main()
