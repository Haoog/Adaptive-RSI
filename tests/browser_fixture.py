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
            if os.environ.get("RSI_BROWSER_PROJECT_IDENTITY"):
                # 同名但不同路径的两个项目，以及一个尚未接入的新仓库，供项目标识回归使用。
                runtime.add_project(fixture(root / "identity-a"), "同名仓库")
                runtime.add_project(fixture(root / "identity-b"), "同名仓库")
                fixture(root / "identity-new")
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
            if os.environ.get("RSI_BROWSER_TASK_DETAIL"):
                # 任务详情专项：多运行历史懒加载、待交付取消、等待额度/过时任务详情内重试。
                now = time.time()
                detail_config = json.dumps({"models": {}, "checks": CHECKS})
                detail_task = runtime.store.add_task(project, "goal", "detail-history", "详情历史任务")
                runtime.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (detail_task,))
                for run_id, state, started, result in [
                    ("detail-h1", "merged", now - 9000, {"summary": "第一次修复已合并", "delivery_status": "merged"}),
                    ("detail-h2", "failed", now - 6000, {"reason": "中间一次没有通过固定检查"}),
                    ("detail-h3", "failed", now - 3000, {"reason": "最新一次仍未通过固定检查",
                                                          "failure": {"code": "checks", "message": "unittest failed"},
                                                          "files": ["calculator.py"]}),
                ]:
                    runtime.store.execute(
                        "INSERT INTO runs(id,task,project,state,config,base,path,calls,tokens,started,ended,result) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, detail_task, project, state, detail_config, "basemain", None, 3, 1200,
                         started, started + 60, json.dumps(result)))
                approval_task = runtime.store.add_task(project, "goal", "detail-approval", "待交付详情任务")
                runtime.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (approval_task,))
                runtime.store.execute(
                    "INSERT INTO runs(id,task,project,state,config,base,path,calls,tokens,started,ended,result) "
                    "VALUES ('detail-appr',?,?,'awaiting_approval',?,'basemain',NULL,3,1200,?,?,?)",
                    (approval_task, project, detail_config, now - 1200, now - 1100,
                     json.dumps({"summary": "修复已通过独立检查，等待批准", "reason": "验证过的算术缺陷"})))
                for name, state in [("额度等待详情任务", "waiting_budget"), ("过时详情任务", "obsolete")]:
                    retry_task = runtime.store.add_task(project, "goal", "detail-retry-" + state, name)
                    runtime.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, retry_task))
                    runtime.store.execute(
                        "INSERT INTO runs(id,task,project,state,config,base,path,calls,tokens,started,ended,result) "
                        "VALUES (?,?,?,'failed',?,'basemain',NULL,3,1200,?,?,?)",
                        ("detail-" + state, retry_task, project, detail_config, now - 900, now - 800,
                         json.dumps({"reason": "早前一次尝试没有通过"})))
            if os.environ.get("RSI_BROWSER_QUEUE_ONLY"):
                child = runtime.store.add_task(project, "goal", "queue-child", "排队草稿验证")
                for index in range(200):
                    runtime.store.add_task(project, "goal", "queue-page-" + str(index), "依赖分页任务 " + str(index))
                parent = runtime.store.add_task(project, "goal", "queue-old-parent", "旧失败前置任务")
                runtime.store.execute("UPDATE tasks SET state='failed',updated=1 WHERE id=?", (parent,))
                runtime.store.execute("UPDATE tasks SET urgency=1 WHERE id=?", (child,))
            if os.environ.get("RSI_BROWSER_TRIAGE_VISIBILITY"):
                # 批量初筛决定可见性：新决定直接落库 task_decisions；
                # 存量 triage-state 键经 reassess 迁移后同样可见。全程无模型调用。
                from adaptive_rsi.config import fingerprint
                from adaptive_rsi.gitops import git
                from adaptive_rsi.store import encode as dump
                triage_config = runtime.config(project)
                head = git(Path(runtime.project(project)["path"]), "rev-parse", "HEAD").strip()

                def triage_signature(body, evidence):
                    return fingerprint({"body": body, "evidence": dump(evidence),
                                        "policy": triage_config["triage"]["policy"]})
                rejected_body = "把注释从 // 风格改为文档字符串，属于外观层面的修改。"
                deferred_body = "加法实现的证据较弱，引用行与描述不完全一致。"
                legacy_body = "与现有任务可能重叠，等待基线变化后重评。"
                rejected = runtime.store.add_task(
                    project, "inspection", "browser-triage-rejected", "初筛不采纳任务", rejected_body,
                    [{"path": "calculator.py", "quote": "def add(a, b):", "base": head, "source": "inspection"}], 40)
                deferred = runtime.store.add_task(
                    project, "inspection", "browser-triage-deferred", "初筛暂缓任务", deferred_body,
                    [{"path": "tests/test_calculator.py", "quote": "class Regression", "base": head,
                      "source": "inspection"}], 40)
                legacy = runtime.store.add_task(
                    project, "inspection", "browser-triage-legacy", "存量暂缓任务", legacy_body,
                    [{"path": "calculator.py", "quote": "return a - b", "base": head, "source": "inspection"}], 40)
                rejected_evidence = [{"path": "calculator.py", "quote": "def add(a, b):", "base": head,
                                      "source": "inspection"}]
                deferred_evidence = [{"path": "tests/test_calculator.py", "quote": "class Regression", "base": head,
                                      "source": "inspection"}]
                legacy_evidence = [{"path": "calculator.py", "quote": "return a - b", "base": head,
                                    "source": "inspection"}]
                runtime.store.decide(rejected, "rejected", {
                    "base": head, "triage_input": triage_signature(rejected_body, rejected_evidence),
                    "reason": "Cosmetic churn: comment style change with disproportionate cost.",
                    "decision": "rejected"})
                runtime.store.decide(deferred, "deferred", {
                    "base": head, "triage_input": triage_signature(deferred_body, deferred_evidence),
                    "reason": "Evidence is weak: the quoted line does not fully match the report.",
                    "wake_condition": "inputs_policy_or_relevant_baseline_change", "decision": "deferred"})
                # 存量键：只有 settings 里的 triage-state，没有决定记录。
                runtime.store.execute("UPDATE tasks SET state='deferred' WHERE id=?", (legacy,))
                runtime.store.put_setting("triage-state:" + legacy, {
                    "base": head, "triage_input": triage_signature(legacy_body, legacy_evidence),
                    "reason": "Weak evidence: overlaps an existing queued finding.",
                    "decision": "deferred"})
                # 与正式 reassess 同一条迁移路径：存量键生成决定记录，新决定保持不变。
                runtime.reassess_deferred(project, triage_config, head)
                for task, state in [(rejected, "rejected"), (deferred, "deferred"), (legacy, "deferred")]:
                    current = runtime.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"]
                    assert current == state, (task, current, state)
                    assert runtime.store.one("SELECT data FROM task_decisions WHERE task=?", (task,)), task
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
