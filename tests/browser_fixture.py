"""Start a paused, temporary console for browser checks. No real models or GitHub."""
import tempfile
import threading
import json
import sys
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.auth_sessions import AuthSessions
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture, CHECKS


class DirectoryServer(ThreadingHTTPServer):
    def server_bind(self):
        # 临时目录服务也使用回环地址，不需要反向 DNS 查询。
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class BrowserAuthWorker:
    """Replace only the external CLI, keeping real session and HTTP validation."""
    def __init__(self, backend):
        self.home = tempfile.TemporaryDirectory(prefix="rsi-browser-auth-")
        self.credentials = Path(self.home.name) / "auth.json"
        self.closed = threading.Event()

    def start(self, provider=None, method=0, inputs=None):
        return {"verification_url": "https://example.test/device", "user_code": "HTTP-TEST-CODE",
                "method": "auto", "instructions": "Browser integration fixture"}

    def wait(self, stop, deadline):
        while not stop.wait(0.01) and not self.closed.is_set() and time.time() < deadline:
            pass
        return False

    def providers(self):
        return []

    def close(self):
        self.closed.set()
        self.home.cleanup()


def main():
    with tempfile.TemporaryDirectory(prefix="rsi-browser-") as directory:
        root = Path(directory).resolve()
        os.environ["CODEX_HOME"] = str(root / "credentials/codex")
        os.environ["XDG_DATA_HOME"] = str(root / "credentials/data")
        runtime = Runtime(Store(root / "state"), FakeAgent())
        runtime.auth_sessions.close()
        runtime.auth_sessions = AuthSessions(root / "state", factory=BrowserAuthWorker)
        class Directory(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"data": [{"id": "fixture-planner"}, {"id": "fixture-coder"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        print("Browser fixture: starting local model directory", file=sys.stderr, flush=True)
        directory = DirectoryServer(("127.0.0.1", 0), Directory)
        threading.Thread(target=directory.serve_forever, daemon=True).start()
        server = None
        try:
            connection = runtime.model_center.save_provider(
                {"name": "测试目录服务", "protocol": "responses",
                 "base_url": "http://127.0.0.1:" + str(directory.server_port), "env_key": ""}, {"mode": "none"})
            runtime.model_center.fetch(connection["id"])
            print("Browser fixture: preparing isolated task evidence", file=sys.stderr, flush=True)
            project = runtime.add_project(fixture(root / "project"), "浏览器验证项目")
            runtime.configure({"checks": CHECKS, "sources": {"inspection": False, "issues": False},
                               "evolution": {"enabled": False}}, project)
            runtime.goal(project, "Fix addition")
            runtime.pause(project, False)
            runtime.process(runtime.store.claim(project, runtime.config(project)))
            runtime.pause(project, True)
            other = runtime.add_project(fixture(root / "other"), "另一个独立项目")
            for index in range(55):
                runtime.store.add_task(project, "goal", str(index), "历史任务 " + str(index))
            runtime.store.add_task(other, "goal", "other", "另一个项目专属任务")
            failed_ci = runtime.store.add_task(project, "goal", "failed-ci", "等待修复线上检查")
            runtime.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (failed_ci,))
            runtime.store.execute("INSERT INTO runs(id,task,project,state,config,result,started,ended) "
                                  "VALUES ('browser-ci',?,?,'pr_open','{}',?,1,2)",
                                  (failed_ci, project, json.dumps({"delivery_status": "ci_failed"})))
            if os.environ.get("RSI_BROWSER_OBSOLETE_ONLY"):
                for name in ["过时任务重试入口", "过时任务补充入口"]:
                    obsolete = runtime.store.add_task(project, "goal", name, name)
                    runtime.store.execute("UPDATE tasks SET state='obsolete' WHERE id=?", (obsolete,))
            runtime.store.event("issue_revision", "Issue closed or excluded during execution; recheck before delivery", project)
            server = Server(runtime)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print("Browser fixture: console ready", file=sys.stderr, flush=True)
            print(server.url, flush=True)
            # Browser regressions may inspect committed fixture rows without adding
            # a test-only HTTP endpoint or exposing the temporary state directory.
            for line in sys.stdin:
                if not line.strip():
                    break
                texts = json.loads(line)["goal_evidence"]
                placeholders = ",".join("?" for _ in texts)
                print(json.dumps({
                    "tasks": runtime.store.query(
                        "SELECT id,project,body,state,attempts FROM tasks "
                        "WHERE source='goal' AND body IN (" + placeholders + ")", texts),
                    "submissions": runtime.store.query(
                        "SELECT s.project,s.request,s.task FROM submissions s "
                        "JOIN tasks t ON t.id=s.task WHERE t.body IN (" + placeholders + ")", texts),
                    "projects": runtime.store.query("SELECT id,paused FROM projects"),
                    "calls": runtime.store.one("SELECT count(*) AS n FROM calls")["n"],
                }, ensure_ascii=False), flush=True)
        finally:
            directory.shutdown()
            directory.server_close()
            if server:
                server.shutdown()
                server.server_close()
            runtime.close()


if __name__ == "__main__":
    main()
