"""Start a paused, temporary console for browser checks. No real models or GitHub."""
import tempfile
import threading
from pathlib import Path

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture, CHECKS


def main():
    with tempfile.TemporaryDirectory(prefix="rsi-browser-") as directory:
        root = Path(directory).resolve()
        runtime = Runtime(Store(root / "state"), FakeAgent())
        server = None
        try:
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
            if server:
                server.shutdown()
                server.server_close()
            runtime.close()


if __name__ == "__main__":
    main()
