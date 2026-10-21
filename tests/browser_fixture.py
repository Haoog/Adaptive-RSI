"""Start a paused, temporary console for browser checks. No real models or GitHub."""
import tempfile
import threading
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture, CHECKS


def main():
    with tempfile.TemporaryDirectory(prefix="rsi-browser-") as directory:
        root = Path(directory).resolve()
        runtime = Runtime(Store(root / "state"), FakeAgent())
        class Directory(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"data": [{"id": "fixture-planner"}, {"id": "fixture-coder"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        directory = ThreadingHTTPServer(("127.0.0.1", 0), Directory)
        threading.Thread(target=directory.serve_forever, daemon=True).start()
        server = None
        try:
            connection = runtime.model_center.save_provider(
                {"name": "测试目录服务", "protocol": "responses",
                 "base_url": "http://127.0.0.1:" + str(directory.server_port), "env_key": ""}, {"mode": "none"})
            runtime.model_center.fetch(connection["id"])
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
            server = Server(runtime)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(server.url, flush=True)
            input()
        finally:
            directory.shutdown()
            directory.server_close()
            if server:
                server.shutdown()
                server.server_close()
            runtime.close()


if __name__ == "__main__":
    main()
