import json
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

from adaptive_rsi.auth_sessions import AuthError, AuthSessions
from adaptive_rsi.dashboard import records
from adaptive_rsi.discovery import finding_identity, related_task, related_work
from adaptive_rsi.lifecycle import readiness
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store, encode
from tests.test_runtime import CHECKS, FakeAgent, fixture


class RelatedWorkTest(unittest.TestCase):
    def store(self, directory):
        store = Store(directory)
        for name in ["first", "second"]:
            store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                          (name, name, str(Path(directory) / name), "{}", 1, time.time()))
        return store

    def test_same_code_can_contain_distinct_problems_and_error_numbers(self):
        first = {"title": "Handle HTTP 401 response", "path": "client.py", "quote": "return response"}
        second = {**first, "title": "Handle HTTP 429 response"}
        self.assertNotEqual(finding_identity(first), finding_identity(second))
        rows = [{"id": "first", "title": first["title"], "evidence": encode([first])}]
        self.assertIsNone(related_task(rows, second))
        self.assertEqual(related_task(rows, {**first, "title": "  Handle HTTP 401   response "}), "first")

    def test_near_identical_titles_are_deduplicated_without_merging_error_variants(self):
        backlog = [{"id": "retry", "title": "Fix failed login retry loop",
                    "evidence": encode([{"path": "login.py", "quote": "redirect()"}])}]
        self.assertEqual(related_task(backlog, {
            "title": "Failed login retry loop", "path": "login.py", "quote": "retry()"}), "retry")
        self.assertIsNone(related_task(backlog, {
            "title": "Fix failed login retry status 429", "path": "login.py", "quote": "retry()"}))
        self.assertIsNone(related_task(backlog, {
            "title": "Fix failed login retry loop", "path": "login.py", "quote": "retry()"}))

    def test_current_related_results_are_project_scoped_and_invalidate_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            evidence = [{"path": "client.py", "quote": "return response"}]
            task_id = store.add_task("first", "issue", "issue:1", "Handle HTTP response", evidence=evidence)
            other = store.add_task("first", "inspection", "inspection:1", "Handle timeout", evidence=evidence)
            store.add_task("second", "issue", "issue:1", "Handle HTTP response", evidence=evidence)
            task = store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
            before = related_work(store, "first", task)
            self.assertEqual([row["id"] for row in before], [other])
            store.execute("UPDATE tasks SET state='merged' WHERE id=?", (other,))
            store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'merged',?,?,?)",
                          ("merged", other, "first", encode({"summary": "Fixed timeout", "base": "abc", "pr": "https://github.com/example/project/pull/1"}), time.time(), time.time()))
            after = related_work(store, "first", task)
            self.assertNotEqual(before, after)
            self.assertEqual(after[0]["summary"], "Fixed timeout")
            self.assertEqual(after[0]["shared_paths"], ["client.py"])

    def test_inspection_preserves_distinct_findings_at_one_source_location(self):
        class Findings(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "analysis":
                    finding = result["findings"][0]
                    result["findings"].append({**finding, "title": "Second distinct arithmetic boundary",
                                               "body": "A different acceptance condition at this source anchor"})
                return result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = Runtime(Store(root / "state"), Findings())
            try:
                project = runtime.add_project(fixture(root / "project"))
                runtime.configure({"checks": CHECKS, "evolution": {"enabled": False}}, project)
                runtime.request(project, "light")
                runtime.pause(project, False)
                runtime.process(runtime.store.claim(project, runtime.config(project)))
                findings = runtime.store.query("SELECT title,fingerprint,evidence FROM tasks WHERE project=? AND source='inspection'", (project,))
                self.assertEqual(len(findings), 2)
                self.assertNotEqual(findings[0]["fingerprint"], findings[1]["fingerprint"])
                self.assertEqual(json.loads(findings[0]["evidence"])[0]["quote"],
                                 json.loads(findings[1]["evidence"])[0]["quote"])
            finally:
                runtime.close()

    def test_changed_related_result_invalidates_retry_planning_cache(self):
        class Outage(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                if role == "implementation":
                    self.roles.append((role, {}))
                    raise RuntimeError("Provider timeout")
                return super().invoke(role, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Store(root / "state")
            runtime = Runtime(store, Outage())
            try:
                project = runtime.add_project(fixture(root / "project"))
                runtime.configure({"checks": CHECKS, "limits": {"retries": 0}, "evolution": {"enabled": False}}, project)
                runtime.pause(project, False)
                related = store.add_task(project, "issue", "related", "Fix addition timeout")
                store.execute("UPDATE tasks SET state='merged' WHERE id=?", (related,))
                store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'merged',?,1,2)",
                              ("related-run", related, project, encode({"summary": "Earlier related result"})))
                task = runtime.goal(project, "Fix addition")
                runtime.process(store.claim(project, runtime.config(project)))
                self.assertEqual([role for role, _ in runtime.agent.roles].count("planning"), 1)
                self.assertEqual(store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
                self.assertGreater(store.one("SELECT available FROM tasks WHERE id=?", (task,))["available"], time.time())
                store.execute("UPDATE runs SET result=? WHERE id='related-run'",
                              (encode({"summary": "New related evidence", "pr": "https://github.com/example/project/pull/7"}),))
                runtime.retry(task)
                runtime.process(store.claim(project, runtime.config(project)))
                self.assertEqual([role for role, _ in runtime.agent.roles].count("planning"), 2)
                self.assertEqual(store.query("SELECT * FROM calls"), [])
            finally:
                runtime.close()

    def test_workspace_results_keep_automation_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            task = store.add_task("first", "goal", "fix", "Fix issue")
            store.execute("UPDATE tasks SET state='merged' WHERE id=?", (task,))
            outcome = {"automation": {"release": {"state": "workflow_succeeded", "sha": "a" * 40}}}
            store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'merged',?,1,2)",
                          ("run", task, "first", encode(outcome)))
            self.assertEqual(records(store, "first")["runs"][0]["automation"], outcome["automation"])
            self.assertEqual(records(store, "second")["runs"], [])
            outcome["automation"]["release"]["state"] = "running"
            store.execute("UPDATE runs SET result=? WHERE id='run'", (encode(outcome),))
            self.assertEqual(records(store, "first", group="active")["tasks"][0]["display_state"], "releasing")
            outcome["automation"]["release"]["state"] = "failed"
            store.execute("UPDATE runs SET result=? WHERE id='run'", (encode(outcome),))
            self.assertEqual(records(store, "first", group="attention")["tasks"][0]["display_state"], "release_failed")
            self.assertEqual(records(store, "first", group="history")["tasks"], [])
            outcome["automation"]["release"]["state"] = "workflow_succeeded"
            for state in ["waiting_target", "waiting_verification", "waiting_release"]:
                with self.subTest(state=state):
                    outcome["automation"]["deploy"] = {"state": state}
                    store.execute("UPDATE runs SET result=? WHERE id='run'", (encode(outcome),))
                    self.assertEqual(records(store, "first", group="active")["tasks"][0]["display_state"], "deploying")
                    self.assertEqual(records(store, "first", group="history")["tasks"], [])

    def test_dashboard_exposes_latest_stage_and_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            task = store.add_task("first", "goal", "stage", "Observe stage")
            store.execute("UPDATE tasks SET state='evaluating' WHERE id=?", (task,))
            store.execute("INSERT INTO runs(id,task,project,state,config,started) VALUES ('stage-run',?,'first','evaluating','{}',10)", (task,))
            store.execute("INSERT INTO stages(run,stage,signature,data,created) VALUES ('stage-run','evaluation','s','{}',11)")
            run = records(store, "first")["runs"][0]
            self.assertEqual((run["last_stage"], run["last_stage_at"]), ("evaluation", 11))


class ConnectionAndReleaseRoutesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Runtime(Store(self.root / "state"), FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / "project"))
        self.runtime.auth_sessions.close()
        self.auth = Mock()
        self.runtime.auth_sessions = self.auth
        self.auth.snapshot.return_value = {"codex": {"state": "signed_out"}}
        self.auth.reserve.return_value = {"id": "login", "state": "starting"}
        self.auth.launch.return_value = {"id": "login", "state": "awaiting_authorization"}
        self.auth.status.return_value = {"id": "login", "state": "cancelled"}
        self.auth.set_api_key.return_value = {"saved": True}
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.headers = {"Cookie": self.server.cookie + "=" + self.server.token,
                        "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
                        "Content-Type": "application/json"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.server.origin + path, body,
                                         self.headers if headers is None else headers)
        return urllib.request.urlopen(request)

    def test_authentication_and_csrf_are_required_before_login_side_effects(self):
        for path in ["/api/auth/status", "/api/auth/session?id=login", "/api/auth/providers?backend=opencode", "/api/release-workflows?project=" + self.project]:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as denied:
                self.request(path, headers={})
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
        for headers in [{k: v for k, v in self.headers.items() if k != "X-RSI-CSRF"},
                        {**self.headers, "Origin": "https://untrusted.example"}]:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self.request("/api/auth/start", {"backend": "codex"}, headers)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
        self.auth.start.assert_not_called()

    def test_dynamic_login_parameters_are_passed_without_task_or_event_storage(self):
        with self.request("/api/auth/start", {"backend": "opencode", "provider": "example", "method": 2,
                                               "inputs": {"tenant": "work"}}) as response:
            self.assertEqual(json.load(response)["id"], "login")
        self.auth.reserve.assert_called_once_with("opencode", "example", 2, {"tenant": "work"})
        self.auth.launch.assert_called_once_with("login", 2, {"tenant": "work"})
        with self.request("/api/auth/key", {"backend": "opencode", "provider": "example", "key": "private-key"}):
            pass
        self.auth.set_api_key.assert_called_once_with("opencode", "example", "private-key")
        self.assertNotIn("private-key", encode(self.runtime.store.query("SELECT * FROM events")))
        self.assertEqual(self.runtime.store.query("SELECT * FROM tasks"), [])

    def test_update_window_rejects_login_before_creating_a_session(self):
        self.runtime.updates.installing = True
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/auth/start", {"backend": "codex"})
        self.assertEqual(denied.exception.code, 503)
        self.assertEqual(json.load(denied.exception)["error_code"], "update_in_progress")
        self.auth.reserve.assert_not_called()
        self.auth.launch.assert_not_called()

    def test_auth_writes_do_not_hold_dispatch_lock_while_running(self):
        # BUG-02 回归：认证调用可能同步等待 worker 启动或凭据写入，端点不得在
        # 此期间持有 dispatch_lock，否则任务领取与全部写端点一起排队。
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def slow_set_api_key(backend, provider, key):
            entered.set()
            release.wait(timeout=5)
            return {"saved": True}

        self.auth.set_api_key.side_effect = slow_set_api_key

        def post():
            try:
                with self.request("/api/auth/key", {"backend": "opencode", "provider": "example", "key": "private-key"}):
                    pass
            except Exception as exc:
                errors.append(exc)
            finally:
                release.set()

        thread = threading.Thread(target=post, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            time.sleep(0.05)
            self.assertTrue(self.runtime.dispatch_lock.acquire(timeout=2),
                            "auth write endpoint held dispatch_lock while the auth call was running")
            self.runtime.dispatch_lock.release()
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertEqual(errors, [])

    def test_slow_auth_write_does_not_block_task_claim(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_set_api_key(backend, provider, key):
            entered.set()
            release.wait(timeout=5)
            return {"saved": True}

        self.auth.set_api_key.side_effect = slow_set_api_key
        self.runtime.pause(self.project, False)
        self.runtime.store.add_task(self.project, "goal", "claim", "Claim while auth write is busy")

        def post():
            try:
                with self.request("/api/auth/key", {"backend": "opencode", "provider": "example", "key": "private-key"}):
                    pass
            finally:
                release.set()

        thread = threading.Thread(target=post, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            deadline = time.monotonic() + 2
            run = None
            while time.monotonic() < deadline:
                if self.runtime.dispatch_lock.acquire(timeout=0.2):
                    try:
                        run = self.runtime.store.claim(self.project, self.runtime.config(self.project))
                    finally:
                        self.runtime.dispatch_lock.release()
                    if run:
                        break
                time.sleep(0.02)
            self.assertTrue(run, "task claim stayed blocked by dispatch_lock while an auth write was running")
        finally:
            release.set()
            thread.join(timeout=5)

    def test_auth_key_write_still_rejected_during_update_window(self):
        self.runtime.updates.installing = True
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/auth/key", {"backend": "opencode", "provider": "example", "key": "private-key"})
        self.assertEqual(denied.exception.code, 503)
        self.assertEqual(json.load(denied.exception)["error_code"], "update_in_progress")
        self.auth.set_api_key.assert_not_called()

    def test_login_reservation_admitted_atomically_with_update_gate(self):
        # BUG-02 跟进：require_ready 放行后、登记前的窗口里，桌面更新可以在
        # busy==0 时 commit，随后请求又创建出 starting 会话。回归要求在
        # 准入检查与登记之间插入更新准入检查时，看到的是 busy>0 的已登记会话。
        # 用真实 AuthSessions（经探针包装）验证：reserve 在 dispatch_lock 内执行、
        # 登记返回即 busy>0、慢的 worker 启动发生在锁外。
        self.runtime.auth_sessions.close()
        real = AuthSessions(self.root / "atomic-auth", factory=lambda backend: self.fail("placeholder"), ttl=60)
        phase_a_started = threading.Event()
        phase_a_release = threading.Event()
        factory_entered = threading.Event()
        factory_release = threading.Event()
        observed = {}
        errors = []

        def stuck_factory(backend):
            factory_entered.set()
            factory_release.wait(timeout=5)
            raise AuthError("auth_timeout")

        real.factory = stuck_factory
        observations_done = threading.Event()

        class Probe:
            # readiness() 经 getattr(auth_sessions, "busy") 读取忙碌计数，必须透传。
            busy = property(lambda self: real.busy)

            def reserve(self, *args):
                phase_a_started.set()
                phase_a_release.wait(timeout=5)
                return real.reserve(*args)

            def launch(self, *args):
                result = real.launch(*args)
                # 拖住 HTTP 响应，避免请求结束后 factory 被提前释放，
                # 会话转入 failed 让探针的忙碌观测失真。
                if not observations_done.wait(timeout=5):
                    errors.append(AssertionError("probe never finished observations"))
                return result

        wrapper = Probe()

        def probe():
            try:
                if not phase_a_started.wait(timeout=5):
                    errors.append(AssertionError("reserve was not entered"))
                    return
                # 服务端此刻停在 reserve 调用内部，dispatch_lock 必然被它持有。
                observed["lock_free_during_reserve"] = self.runtime.dispatch_lock.acquire(timeout=0.5)
                if observed["lock_free_during_reserve"]:
                    self.runtime.dispatch_lock.release()
                observed["busy_before_register"] = real.busy
                phase_a_release.set()
                if not factory_entered.wait(timeout=5):
                    errors.append(AssertionError("worker launch never entered the factory"))
                    return
                # worker 工厂已在锁外执行；会话已登记为 starting，busy 立即 > 0。
                observed["lock_free_during_factory"] = self.runtime.dispatch_lock.acquire(timeout=0.5)
                if observed["lock_free_during_factory"]:
                    self.runtime.dispatch_lock.release()
                observed["busy_after_register"] = real.busy
                with self.runtime.dispatch_lock:
                    observed["reasons"] = readiness(self.runtime)["reasons"]
            except Exception as exc:
                errors.append(exc)
            finally:
                phase_a_release.set()
                factory_release.set()
                observations_done.set()

        original_auth = self.runtime.auth_sessions
        self.runtime.auth_sessions = wrapper
        probe_thread = threading.Thread(target=probe, daemon=True)
        probe_thread.start()
        try:
            with self.request("/api/auth/start", {"backend": "opencode", "provider": "example"}):
                pass
        finally:
            factory_release.set()
            observations_done.set()
            probe_thread.join(timeout=5)
            real.close()
            self.runtime.auth_sessions = original_auth
        self.assertEqual(errors, [])
        self.assertIs(observed["lock_free_during_reserve"], False,
                      "reserve must run inside the dispatch_lock critical section")
        self.assertEqual(observed["busy_before_register"], 0)
        self.assertIs(observed["lock_free_during_factory"], True,
                      "worker launch must run outside the dispatch_lock critical section")
        self.assertEqual(observed["busy_after_register"], 1)
        self.assertIn("authentication", observed["reasons"],
                      "update admission must observe the reserved session as busy")

    def test_release_status_cannot_read_another_project_run(self):
        other = self.runtime.add_project(fixture(self.root / "other"))
        task = self.runtime.store.add_task(other, "goal", "other", "Private project change")
        self.runtime.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'merged','{}',1,2)",
                                   ("private", task, other))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/api/release-status?project=" + self.project + "&run=private")
        self.assertEqual(denied.exception.code, 400)
        denied.exception.close()


if __name__ == "__main__":
    unittest.main()
