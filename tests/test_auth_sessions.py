import json
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from adaptive_rsi.auth_sessions import AuthError, AuthSessions, CodexAuth, OpenCodeAuth
from adaptive_rsi.cli_worker import CODEX_AUTH_LOCK
from adaptive_rsi.opencode import AUTH_LOCK, OpenCodeCapabilities, load_auth_export, merge_auth_export


@contextlib.contextmanager
def held_by_model(lock):
    held, release = threading.Event(), threading.Event()

    def model():
        with lock:
            held.set()
            release.wait(5)
    thread = threading.Thread(target=model)
    thread.start()
    if not held.wait(1):
        raise AssertionError("Model did not acquire credential lock")
    try:
        yield
    finally:
        release.set()
        thread.join(2)


class FakeAuth:
    def __init__(self, backend, gate=None, failure=None, method="auto"):
        self.backend = backend
        self.gate = gate or threading.Event()
        self.failure = failure
        self.method = method
        self.temp = tempfile.TemporaryDirectory()
        self.credentials = Path(self.temp.name) / "auth.json"
        self.closed = False
        self.provider = None
        self.code = None
        self.inputs = None

    def providers(self):
        return [{"id": "openai", "name": "openai", "methods": [
            {"index": 0, "label": "device", "type": "oauth", "supported": True},
            {"index": 1, "label": "API key", "type": "api", "supported": True}]}]

    def start(self, provider=None, method=0, inputs=None):
        self.provider, self.inputs = provider, inputs
        if self.failure:
            raise AuthError(self.failure)
        return {"verification_url": "https://example.test/device?private=authorization-link", "user_code": "SENSITIVE-CODE",
                "method": self.method, "instructions": "SENSITIVE-INSTRUCTIONS"}

    def _finish(self, stop, deadline):
        while not self.gate.wait(0.01):
            if self.closed or stop.is_set() or time.time() >= deadline:
                return False
        value = {"tokens": {"refresh_token": "SECRET-REFRESH", "access_token": "SECRET-ACCESS"}} if self.backend == "codex" else {
            self.provider: {"type": "oauth", "refresh": "SECRET-REFRESH", "access": "SECRET-ACCESS", "expires": 1234}}
        self.credentials.write_text(json.dumps(value))
        return True

    def wait(self, stop, deadline):
        return self._finish(stop, deadline)

    def complete(self, code="", timeout=600):
        self.code = code
        return self._finish(threading.Event(), time.time() + timeout)

    def close(self):
        self.closed = True
        # 测试保留临时凭据到fixture销毁，以免与读写线程竞争。


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"), "XDG_DATA_HOME": str(self.root / "data")})
        self.env.start()
        self.created = []
        self.gate = threading.Event()
        self.options = {}

        def factory(backend):
            worker = FakeAuth(backend, self.gate, **self.options)
            self.created.append(worker)
            return worker
        self.factory = factory
        self.manager = AuthSessions(self.root / "state", factory=factory, ttl=2)

    def tearDown(self):
        self.manager.close()
        for worker in self.created:
            worker.temp.cleanup()
        self.env.stop()
        self.temp.cleanup()

    def wait_state(self, session, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.manager.status(session["id"])
            if result["state"] == state:
                return result
            time.sleep(0.005)
        self.fail("Expected " + state + ": " + repr(result))

    def test_corrupt_credentials_preserve_healthy_backend_snapshot(self):
        for broken in ("codex", "opencode"):
            with self.subTest(backend=broken):
                def read(path):
                    if ("codex" in str(path)) == (broken == "codex"):
                        raise AuthError("invalid_credentials")
                    return {"tokens": {"refresh_token": "fixture"}, "openai": {"type": "api"}}
                with patch("adaptive_rsi.auth_sessions._read_json", side_effect=read):
                    result = self.manager.snapshot()
                self.assertEqual(result[broken]["error"], "invalid_credentials")
                self.assertEqual(result[broken]["state"], "signed_out")
                self.assertEqual(result["opencode" if broken == "codex" else "codex"]["state"], "authenticated")

    def test_initialization_never_starts_cli(self):
        self.assertEqual(self.created, [])
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.snapshot()["codex"]["state"], "signed_out")

    def test_container_snapshot_reads_v1_auth_without_host_opencode_binary(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "PRIVATE-REFRESH"}}))
        manager = AuthSessions(self.root / "container-state", config={"sandbox": {"backend": "docker"}})
        try:
            with patch("adaptive_rsi.auth_sessions.shutil.which",
                       side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None), patch(
                    "adaptive_rsi.auth_sessions.run_process", return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.auth_sessions.detect_opencode",
                    return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))):
                snapshot = manager.snapshot()
        finally:
            manager.close()
        self.assertEqual(snapshot["opencode"]["state"], "authenticated")
        self.assertEqual(snapshot["opencode"]["transport"], "docker")
        self.assertEqual(snapshot["opencode"]["authenticated_providers"], ["openai"])
        self.assertNotIn("PRIVATE-REFRESH", json.dumps(snapshot))

    def test_container_logout_removes_persisted_v2_export_without_host_cli(self):
        merge_auth_export([{"integrationID": "opencode-go", "id": "credential",
                            "value": {"type": "key", "key": "PRIVATE-KEY"}}])
        manager = AuthSessions(self.root / "container-logout", config={"sandbox": {"backend": "docker"}})
        try:
            with patch("adaptive_rsi.auth_sessions.shutil.which",
                       side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None):
                manager.logout("opencode", "opencode-go")
        finally:
            manager.close()
        self.assertEqual(load_auth_export(), [])

    def test_codex_device_completion_persists_compatible_private_credentials(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.assertEqual(self.manager.busy, 1)
        self.gate.set()
        result = self.wait_state(session, "completed")
        path = self.root / "codex/auth.json"
        self.assertEqual(json.loads(path.read_text())["tokens"]["refresh_token"], "SECRET-REFRESH")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("user_code", result)
        self.assertNotIn("verification_url", result)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.snapshot()["codex"]["state"], "authenticated")

    def test_pending_authorization_material_never_enters_metadata_or_snapshot(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        stored = self.manager.file.read_text() + json.dumps(self.manager.snapshot())
        for secret in ("SENSITIVE", "authorization-link", "example.test", "SECRET"):
            self.assertNotIn(secret, stored)
        self.assertEqual(self.manager.file.stat().st_mode & 0o777, 0o600)

    def test_cancel_prevents_late_credentials_and_allows_relogin(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.assertEqual(self.manager.cancel(session["id"])["state"], "cancelled")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())
        replacement = self.manager.start("codex")
        self.wait_state(replacement, "completed")

    def test_rejects_concurrent_backend_login(self):
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        with self.assertRaisesRegex(AuthError, "auth_busy"):
            self.manager.start("opencode", "github-copilot")
        self.assertEqual(self.manager.busy, 1)

    def test_busy_expires_without_browser_polling(self):
        self.manager.ttl = 0.03
        session = self.manager.start("codex")
        time.sleep(0.05)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.status(session["id"])["state"], "expired")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())

    def test_start_tolerates_expiry_between_reserve_and_launch(self):
        # 注入时钟，确定性地复现 reserve→launch 之间 ttl 耗尽的窗口，不依赖 sleep 撞运气。
        clock = [1000.0]
        self.manager.now = lambda: clock[0]
        self.manager.ttl = 5
        launch = self.manager.launch

        def slow_launch(session_id, method=0, inputs=None):
            clock[0] += 6  # 调度停顿：launch 之前已越过 expires_at
            return launch(session_id, method, inputs)

        with patch.object(self.manager, "launch", slow_launch):
            session = self.manager.start("codex")
        self.assertEqual((session["state"], session["error"]), ("expired", "auth_expired"))
        self.assertNotIn("_reserved", session)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.status(session["id"])["state"], "expired")
        self.assertEqual(self.created, [])  # worker 从未派发，凭据不会被写入
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())
        # 过期会话不能借 complete() 写凭据；既有 invalid_auth_state 语义不变。
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "SECRET-CODE")

    def test_launch_returns_expired_state_for_unlaunched_reserved_session(self):
        clock = [2000.0]
        self.manager.now = lambda: clock[0]
        self.manager.ttl = 5
        session = self.manager.reserve("codex")
        self.assertEqual(self.manager.busy, 1)
        clock[0] += 6
        # launch 自己发现窗口已过期：返回终态而不是抛 invalid_auth_state。
        result = self.manager.launch(session["id"])
        self.assertEqual((result["state"], result["error"]), ("expired", "auth_expired"))
        self.assertNotIn("_reserved", result)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.created, [])
        # 只 reserve、永远不会 launch 的会话（准入被拦下）仍由过期扫描兜底回收。
        abandoned = self.manager.reserve("codex")
        self.assertEqual(self.manager.busy, 1)
        clock[0] += 6
        self.assertEqual(self.manager.status(abandoned["id"])["state"], "expired")
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.launch(abandoned["id"])["state"], "expired")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())

    def test_restart_marks_active_session_interrupted_without_restarting_cli(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        second = AuthSessions(self.manager.root, factory=lambda backend: self.fail("CLI restarted"))
        try:
            state = second.status(session["id"])
            self.assertEqual(state["state"], "interrupted")
            self.assertNotIn("user_code", state)
        finally:
            second.close()

    def test_opencode_merges_only_selected_provider(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"other": {"type": "api", "key": "OTHER-KEY"}}))
        session = self.manager.start("opencode", "openai", inputs={"deploymentType": "github.com"})
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.wait_state(session, "completed")
        data = json.loads(path.read_text())
        self.assertEqual(data["other"]["key"], "OTHER-KEY")
        self.assertEqual(data["openai"]["type"], "oauth")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.created[0].inputs, {"deploymentType": "github.com"})

    def test_code_completion_is_private_and_validates_state(self):
        self.options["method"] = "code"
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        with self.assertRaisesRegex(AuthError, "invalid_auth_code"):
            self.manager.complete(session["id"], "")
        self.assertEqual(self.manager.complete(session["id"], " SECRET-CALLBACK ")["state"], "completing")
        self.gate.set()
        self.wait_state(session, "completed")
        self.assertEqual(self.created[0].code, "SECRET-CALLBACK")
        self.assertNotIn("SECRET-CALLBACK", self.manager.file.read_text())
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "again")

    def test_specific_provider_key_and_logout_preserve_other_accounts(self):
        self.manager.set_api_key("opencode", "openai", " PRIVATE-KEY ")
        path = self.root / "data/opencode/auth.json"
        self.assertEqual(json.loads(path.read_text())["openai"]["key"], "PRIVATE-KEY")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE-KEY", json.dumps(self.manager.snapshot()))
        self.manager.logout("opencode", "openai")
        self.assertEqual(json.loads(path.read_text()), {})
        with self.assertRaisesRegex(AuthError, "unsupported_auth_method"):
            self.manager.set_api_key("opencode", "unknown", "key")

    def test_failure_codes_do_not_reflect_provider_error_or_secrets(self):
        self.options["failure"] = "device_auth_unavailable"
        session = self.manager.start("codex")
        result = self.wait_state(session, "failed")
        self.assertEqual(result["error"], "device_auth_unavailable")
        self.assertNotIn("user_code", result)

    def test_close_interrupts_active_sessions(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.manager.close()
        self.assertEqual(self.manager.status(session["id"])["state"], "interrupted")
        self.assertTrue(self.created[0].closed)
        with self.assertRaisesRegex(AuthError, "service_closed"):
            self.manager.start("codex")

    def test_reserve_registers_busy_session_before_any_worker_launch(self):
        session = self.manager.reserve("codex")
        self.assertEqual(session["state"], "starting")
        # 登记返回后 busy 立即计入 starting 会话，更新准入不能再 commit；
        # 慢的 worker 启动尚未发生，留待 launch 在临界区外执行。
        self.assertEqual(self.manager.busy, 1)
        self.assertEqual(self.created, [])
        self.manager.launch(session["id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.created:
            time.sleep(0.005)
        self.assertEqual(len(self.created), 1)
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.wait_state(session, "completed")

    def test_launch_rejects_unknown_or_terminal_sessions(self):
        with self.assertRaisesRegex(AuthError, "unknown_session"):
            self.manager.launch("missing")
        session = self.manager.reserve("codex")
        self.manager.close()
        self.assertEqual(self.manager.status(session["id"])["state"], "interrupted")
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.launch(session["id"])

    def test_rejects_invalid_provider_method_and_inputs(self):
        for kwargs in ({"backend": "unknown"}, {"backend": []}, {"backend": "opencode", "provider": "../oops"},
                       {"backend": "codex", "method": True}, {"backend": "codex", "inputs": {"x": []}}):
            with self.assertRaises(AuthError):
                self.manager.start(**kwargs)

    def test_waiting_for_model_credentials_does_not_block_cancel_status_or_close(self):
        for backend, lock in (("codex", CODEX_AUTH_LOCK), ("opencode", AUTH_LOCK)):
            for operation in ("cancel", "status", "close"):
                with self.subTest(backend=backend, operation=operation):
                    self.manager.close()
                    self.manager = AuthSessions(self.root / (backend + operation), factory=self.factory, ttl=3)
                    self.gate.set()
                    completed, result = threading.Event(), []
                    with held_by_model(lock):
                        session = self.manager.start(backend, "openai" if backend == "opencode" else None)
                        self.wait_state(session, "completing")

                        def request():
                            if operation == "close":
                                self.manager.close()
                            else:
                                result.append(getattr(self.manager, operation)(session["id"]))
                            completed.set()
                        thread = threading.Thread(target=request)
                        thread.start()
                        self.assertTrue(completed.wait(0.5), "Account controls waited for the active model")
                        if operation == "status":
                            self.assertEqual(result[0]["state"], "completing")
                            self.manager.cancel(session["id"])
                    thread.join(2)
                    self.assertIn(self.manager.status(session["id"])["state"], {"cancelled", "interrupted"})
                    self.assertFalse((self.root / "codex/auth.json").exists())
                    self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_snapshot_and_mutations_never_wait_for_running_model_credential_lock(self):
        for backend, lock in (("codex", CODEX_AUTH_LOCK), ("opencode", AUTH_LOCK)):
            with self.subTest(backend=backend), held_by_model(lock):
                result, completed = [], threading.Event()
                thread = threading.Thread(target=lambda: (result.append(self.manager.snapshot()), completed.set()))
                thread.start()
                self.assertTrue(completed.wait(0.5), "Status waited for the active model")
                thread.join(1)
                self.assertEqual(result[0][backend]["state"], "signed_out")
                with self.assertRaisesRegex(AuthError, "auth_credentials_busy"):
                    self.manager.logout(backend, "openai" if backend == "opencode" else None)
                if backend == "opencode":
                    with self.assertRaisesRegex(AuthError, "auth_credentials_busy"):
                        self.manager.set_api_key("opencode", "openai", "PRIVATE-KEY")

    def test_delayed_provider_lookup_cannot_save_key_after_service_closes(self):
        discovered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def providers(backend):
            discovered.set()
            release.wait(2)
            return [{"id": "openai", "methods": [{"type": "api"}]}]

        def save():
            try:
                self.manager.set_api_key("opencode", "openai", "PRIVATE-KEY")
            except AuthError as exc:
                errors.append(exc.code)
            finally:
                finished.set()
        with patch.object(self.manager, "providers", providers):
            thread = threading.Thread(target=save)
            thread.start()
            self.assertTrue(discovered.wait(1))
            self.manager.close()
            release.set()
            self.assertTrue(finished.wait(1))
            thread.join(1)
        self.assertEqual(errors, ["service_closed"])
        self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_service_close_stops_active_provider_discovery_process(self):
        entered, released, finished = threading.Event(), threading.Event(), threading.Event()

        class Discovery(FakeAuth):
            def providers(worker):
                entered.set()
                released.wait(2)
                return super(Discovery, worker).providers()

            def close(worker):
                released.set()
                super(Discovery, worker).close()
        worker = Discovery("opencode")
        self.created.append(worker)
        with patch.object(self.manager, "factory", return_value=worker):
            thread = threading.Thread(target=lambda: (self.manager.providers(), finished.set()))
            thread.start()
            self.assertTrue(entered.wait(1))
            self.manager.close()
            self.assertTrue(finished.wait(0.5))
            thread.join(1)
        self.assertTrue(worker.closed)

    def test_status_expiry_closes_worker_outside_lock(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingClose(FakeAuth):
            def close(worker_self):
                entered.set()
                release.wait(2)
                super().close()
        created = []

        def factory(backend):
            worker = BlockingClose(backend)
            created.append(worker)
            return worker
        manager = AuthSessions(self.root / "expiry-lock", factory=factory, ttl=0.5)
        try:
            session = manager.start("opencode", "openai")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and manager.status(session["id"])["state"] == "starting":
                time.sleep(0.005)
            self.assertEqual(manager.status(session["id"])["state"], "awaiting_authorization")
            time.sleep(0.6)
            thread = threading.Thread(target=manager.status, args=(session["id"],))
            thread.start()
            self.assertTrue(entered.wait(1), "Expired worker was not closed")
            # 引用已在锁内摘除，慢的 close() 不再持有会话锁。
            self.assertNotIn(session["id"], manager.workers)
            self.assertTrue(manager.lock.acquire(timeout=0.5), "Expiry close blocked the session lock")
            manager.lock.release()
            release.set()
            thread.join(2)
            self.assertTrue(created[0].closed)
            self.assertEqual(manager.status(session["id"])["state"], "expired")
        finally:
            manager.close()
            for worker in created:
                worker.temp.cleanup()

    def test_providers_cache_hit_and_invalidation(self):
        manager = AuthSessions(self.root / "providers-cache", ttl=5)
        made = []

        def factory(backend, cancel=None):
            worker = FakeAuth(backend, self.gate)
            made.append(worker)
            return worker
        try:
            with patch.object(manager, "factory", side_effect=factory):
                first = manager.providers("opencode")
                second = manager.providers("opencode")
                self.assertEqual(first, second)
                self.assertEqual(len(made), 1, "Repeated discovery within TTL should hit the cache")
                session = manager.start("opencode", "openai")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and manager.status(session["id"])["state"] == "starting":
                    time.sleep(0.005)
                self.assertEqual(manager.status(session["id"])["state"], "awaiting_authorization")
                manager.providers("opencode")
                # 会话 worker 之外，start 失效后重新发现了一次。
                self.assertEqual(len(made), 3, "Cache should be invalidated after start")
                manager.cancel(session["id"])
                # 避免本机真实 opencode CLI 影响快照断言。
                with patch("adaptive_rsi.auth_sessions.shutil.which", return_value=None):
                    manager.set_api_key("opencode", "openai", "PRIVATE-KEY")
                    self.assertNotIn("opencode", manager.providers_cache)
                    manager.providers("opencode")
                    self.assertIn("opencode", manager.providers_cache)
                    manager.logout("opencode", "openai")
                    self.assertNotIn("opencode", manager.providers_cache)
        finally:
            manager.close()
            for worker in made:
                worker.temp.cleanup()
        self.assertEqual(manager.providers_cache, {}, "close should clear the providers cache")

    def test_oauth_completion_invalidates_caches_populated_during_authorization(self):
        self.manager.close()
        self.manager = AuthSessions(self.root / "oauth-completion-cache", ttl=5)
        with patch.object(self.manager, "factory", side_effect=lambda backend, cancel=None: self.factory(backend)), patch(
                "adaptive_rsi.auth_sessions.shutil.which", return_value=None):
            session = self.manager.start("opencode", "openai")
            self.wait_state(session, "awaiting_authorization")
            self.assertEqual(self.manager.snapshot()["opencode"]["state"], "signed_out")
            self.manager.providers("opencode")
            self.assertIn("opencode", self.manager.status_cache)
            self.assertIn("opencode", self.manager.providers_cache)

            self.gate.set()
            self.wait_state(session, "completed")
            self.assertNotIn("opencode", self.manager.status_cache)
            self.assertNotIn("opencode", self.manager.providers_cache)
            account = self.manager.snapshot()["opencode"]
            self.assertEqual(account["state"], "authenticated")
            self.assertEqual(account["authenticated_providers"], ["openai"])

    def test_inflight_discovery_cannot_repopulate_caches_after_auth_changes(self):
        for operation in ("save", "logout", "set_api_key", "start"):
            with self.subTest(operation=operation):
                self.manager.close()
                self.manager = AuthSessions(self.root / ("inflight-" + operation), ttl=5)
                self.gate = threading.Event()
                path = self.root / "data/opencode/auth.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"openai": {"type": "api", "key": "TEST-KEY"}}
                                          if operation == "logout" else {}))
                entered = {kind: threading.Event() for kind in ("status", "providers")}
                release = threading.Event()
                calls = {kind: 0 for kind in entered}
                errors = []

                def discover(kind):
                    calls[kind] += 1
                    connected = "openai" in json.loads(path.read_text())
                    if calls[kind] == 1:
                        entered[kind].set()
                        if not release.wait(3):
                            raise AssertionError("Discovery was not released")
                    return connected

                class Discovery(FakeAuth):
                    def providers(worker):
                        rows = super().providers()
                        rows[0]["connected"] = discover("providers")
                        return rows

                def factory(backend, cancel=None):
                    worker = Discovery(backend, self.gate)
                    self.created.append(worker)
                    return worker

                def lookup(kind):
                    try:
                        if kind == "status":
                            self.manager.snapshot()
                        else:
                            self.manager.providers("opencode")
                    except Exception as exc:
                        errors.append(exc)

                with patch.object(self.manager, "factory", side_effect=factory), patch(
                        "adaptive_rsi.auth_sessions.shutil.which", return_value=None), patch(
                        "adaptive_rsi.auth_sessions.auth_providers",
                        side_effect=lambda *args: ["openai"] if discover("status") else []):
                    if operation == "save":
                        session = self.manager.start("opencode", "openai")
                        self.wait_state(session, "awaiting_authorization")
                    threads = [threading.Thread(target=lookup, args=(kind,)) for kind in entered]
                    try:
                        for thread in threads:
                            thread.start()
                        for event in entered.values():
                            self.assertTrue(event.wait(1), "Discovery did not capture the old credentials")
                        if operation == "save":
                            self.gate.set()
                            self.wait_state(session, "completed")
                        elif operation == "logout":
                            self.manager.logout("opencode", "openai")
                        elif operation == "set_api_key":
                            self.manager.set_api_key("opencode", "openai", "TEST-NEW-KEY")
                        else:
                            session = self.manager.start("opencode", "openai")
                            self.wait_state(session, "awaiting_authorization")

                        expected = operation in {"save", "set_api_key"}
                        self.assertEqual(bool(self.manager.snapshot()["opencode"]["authenticated_providers"]), expected)
                        self.assertEqual(self.manager.providers("opencode")[0]["connected"], expected)
                        fresh_calls = dict(calls)
                        fresh_status = self.manager.status_cache["opencode"]
                        fresh_providers = self.manager.providers_cache["opencode"]
                        # 先发布新结果，再允许旧发现返回，验证旧结果不能覆盖新缓存。
                        release.set()
                        for thread in threads:
                            thread.join(1)
                            self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                        self.assertIs(self.manager.status_cache["opencode"], fresh_status)
                        self.assertIs(self.manager.providers_cache["opencode"], fresh_providers)
                        self.assertEqual(bool(self.manager.snapshot()["opencode"]["authenticated_providers"]), expected)
                        self.assertEqual(self.manager.providers("opencode")[0]["connected"], expected)
                        self.assertEqual(calls, fresh_calls, "Fresh discoveries should remain cached")
                    finally:
                        release.set()
                        for thread in threads:
                            thread.join(2)
                        self.manager.close()

    def test_custom_factory_providers_skip_cache(self):
        first = self.manager.providers("opencode")
        second = self.manager.providers("opencode")
        self.assertEqual(first, second)
        self.assertEqual(len(self.created), 2, "Custom factory should never use the providers cache")
        self.assertEqual(self.manager.providers_cache, {})


class OpenCodeServeTests(unittest.TestCase):
    def make_worker(self):
        worker = object.__new__(OpenCodeAuth)
        worker.container = False
        worker.cancel = threading.Event()
        worker.control = None
        worker.capabilities = OpenCodeCapabilities("1.19.3", (1, 19, 3))
        worker.base = "http://127.0.0.1:41001"
        return worker

    def test_dead_process_gets_one_new_port_retry(self):
        worker = self.make_worker()
        launches, checks, alive = [], [], {"value": False}

        def launch(argv):
            launches.append(list(argv))
            # 第一次启动的进程立即死亡（如端口被抢占），重试后存活。
            alive["value"] = len(launches) > 1
        worker.launch = launch
        worker.alive = lambda: alive["value"]

        def http(method, path, body=None, timeout=25):
            checks.append(path)
            if len(checks) == 1:
                raise AuthError("auth_protocol_error")
            return {}
        worker.http = http

        worker._serve("fake-opencode", 41001)
        self.assertEqual(len(launches), 2, "Dead process should get exactly one retry on a new port")
        self.assertEqual(launches[0], ["fake-opencode", "serve", "--pure", "--log-level", "ERROR",
                                       "--hostname", "127.0.0.1", "--port", "41001"])
        self.assertNotEqual(launches[1][-1], "41001")
        self.assertTrue(worker.base.endswith(":" + launches[1][-1]))
        self.assertEqual(checks, ["/global/health", "/global/health"])

    def test_retry_happens_only_once(self):
        worker = self.make_worker()
        launches = []
        worker.launch = lambda argv: launches.append(list(argv))
        worker.alive = lambda: False

        def http(method, path, body=None, timeout=25):
            raise AuthError("auth_protocol_error")
        worker.http = http
        with self.assertRaisesRegex(AuthError, "auth_protocol_error"):
            worker._serve("fake-opencode", 41002)
        self.assertEqual(len(launches), 2, "Retry must not loop")

    def test_alive_process_polls_until_deadline_without_retry(self):
        worker = self.make_worker()
        launches, sleeps = [], []
        worker.launch = lambda argv: launches.append(list(argv))
        worker.alive = lambda: True

        def http(method, path, body=None, timeout=25):
            raise AuthError("auth_protocol_error")
        worker.http = http
        clock = {"now": 0.0}

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            with self.assertRaisesRegex(AuthError, "auth_protocol_error"):
                worker._serve("fake-opencode", 41003)
        self.assertEqual(len(launches), 1, "Alive process must never be relaunched")
        self.assertAlmostEqual(clock["now"], 25.0, delta=0.1)

    def test_v2_complete_polls_with_backoff_intervals(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        clock = {"now": 0.0}
        calls, sleeps = [], []

        def fake_http(method, path, body=None, timeout=25):
            calls.append((clock["now"], path))
            if len(calls) >= 6:
                return {"data": {"status": "complete"}}
            return {"data": {"status": "pending"}}
        worker.http = fake_http

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            self.assertTrue(worker._v2_complete(timeout=600))
        intervals = [round(calls[index + 1][0] - calls[index][0], 6) for index in range(len(calls) - 1)]
        # 阶梯退避 0.5/1/2/3 封顶；同样 9.5 秒内线性 0.2s 轮询约需 48 次请求。
        self.assertEqual(intervals, [0.5, 1.0, 2.0, 3.0, 3.0])
        self.assertEqual(len(calls), 6)

    def test_v2_complete_times_out_at_deadline_with_backoff(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        clock = {"now": 0.0}
        calls, sleeps = [], []

        def fake_http(method, path, body=None, timeout=25):
            calls.append(clock["now"])
            return {"data": {"status": "pending"}}
        worker.http = fake_http

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            with self.assertRaisesRegex(AuthError, "auth_timeout"):
                worker._v2_complete(timeout=10)
        # 10 秒窗口内 6 次请求（线性 0.2s 方案约 50 次），最后一次 sleep 不越过截止时间。
        self.assertEqual(len(calls), 6)
        self.assertEqual(sleeps[-1], 0.5)
        self.assertAlmostEqual(clock["now"], 10.0, places=6)


class RealAuthChecks:
    def make(self, cls):
        return cls()

    def test_codex_account_read_is_isolated_and_never_logs_in(self):
        worker = self.make(CodexAuth)
        try:
            result = worker.request("account/read", {"refreshToken": False})
            self.assertIsNone(result["account"])
            self.assertFalse(worker.credentials.exists())
            self.assertEqual(worker.environment["CODEX_HOME"], str(worker.home / ".codex"))
        finally:
            worker.close()

    def test_opencode_lists_builtin_methods_without_authorization(self):
        worker = self.make(OpenCodeAuth)
        try:
            rows = worker.providers()
            openai = next(row for row in rows if row["id"] == "openai")
            self.assertTrue(any(item["supported"] and "headless" in item["label"] for item in openai["methods"]))
            self.assertTrue(any(not item["supported"] and "browser" in item["label"] for item in openai["methods"]))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(worker.base + "/provider/auth")
            self.assertEqual(caught.exception.code, 401)
            self.assertFalse(worker.credentials.exists())
            self.assertEqual(worker.environment["OPENCODE_PURE"], "true")
            self.assertEqual(worker.environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
            self.assertNotIn("OPENCODE_CONFIG", worker.environment)
        finally:
            worker.close()


@unittest.skipUnless(os.environ.get("RSI_AUTH_CLI_TESTS") == "1", "Enable RSI_AUTH_CLI_TESTS=1 for read-only CLI checks")
class RealAuthCLITests(RealAuthChecks, unittest.TestCase):
    pass


@unittest.skipUnless(os.environ.get("RSI_AUTH_DOCKER_TESTS") == "1", "Enable RSI_AUTH_DOCKER_TESTS=1 for read-only Docker checks")
class RealAuthDockerTests(RealAuthChecks, unittest.TestCase):
    def make(self, cls):
        return cls({"sandbox": {"backend": "docker", "worker_image": os.environ.get("RSI_WORKER_IMAGE", "adaptive-rsi-worker:local")}})

    def test_parent_crash_removes_container_and_temporary_credentials(self):
        script = """import json,os
from adaptive_rsi.auth_sessions import OpenCodeAuth
worker=OpenCodeAuth({'sandbox':{'backend':'docker','worker_image':os.environ.get('RSI_WORKER_IMAGE','adaptive-rsi-worker:local')}})
print(json.dumps({'home':str(worker.home),'container':worker.container_name}),flush=True)
os._exit(0)
"""
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            details = json.loads(process.stdout.readline())
            process.wait(timeout=35)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                inspection = subprocess.run(["docker", "container", "inspect", details["container"]],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                if inspection.returncode and not Path(details["home"]).exists():
                    break
                time.sleep(0.1)
            self.assertNotEqual(inspection.returncode, 0)
            self.assertFalse(Path(details["home"]).exists())
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


_PROTOCOL_FIXTURE = r'''#!/usr/bin/env python3
import base64,json,os,pathlib,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
if sys.argv[1:]==['--version']:
 print('opencode v1.19.3')
 raise SystemExit(0)
home=pathlib.Path(os.environ['HOME'])
assert os.environ['CODEX_HOME']==str(home/'.codex')
assert 'OPENAI_API_KEY' not in os.environ
if pathlib.Path(sys.argv[0]).name=='codex':
 for line in sys.stdin:
  msg=json.loads(line)
  method=msg['method']
  if method=='initialized': continue
  result={}
  if method=='account/login/start':
   assert msg['params']=={'type':'chatgptDeviceCode'}
   result={'type':'chatgptDeviceCode','loginId':'fixture-login','verificationUrl':'https://example.test/device','userCode':'PRIVATE-USER-CODE'}
   print(json.dumps({'id':msg['id'],'result':result}),flush=True)
   (home/'.codex/auth.json').write_text(json.dumps({'tokens':{'refresh_token':'FIXTURE-REFRESH','access_token':'FIXTURE-ACCESS'}}))
   print(json.dumps({'method':'account/login/completed','params':{'loginId':'unrelated','success':False}}),flush=True)
   print(json.dumps({'method':'account/login/completed','params':{'loginId':'fixture-login','success':True}}),flush=True)
   continue
  if method=='account/read': result={'account':{'type':'chatgpt'}}
  print(json.dumps({'id':msg['id'],'result':result}),flush=True)
else:
 assert os.environ['OPENCODE_PURE']=='true'
 assert os.environ['OPENCODE_DISABLE_PROJECT_CONFIG']=='true'
 assert 'OPENCODE_CONFIG' not in os.environ
 assert '--pure' in sys.argv
 assert sys.argv[1]=='serve'
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args): pass
  def request(self):
   expected='Basic '+base64.b64encode((os.environ['OPENCODE_SERVER_USERNAME']+':'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
   if self.headers.get('Authorization')!=expected:
    self.send_response(401);self.end_headers();return
   value=True
   if self.path=='/global/health': value={'healthy':True,'python':list(sys.version_info[:2])}
   if self.path=='/provider/auth': value={'openai':[{'type':'oauth','label':'ChatGPT (headless)'},{'type':'api','label':'API key'}]}
   if self.command=='POST':
    body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
    assert body['method']==0
    if self.path.endswith('/authorize'): value={'url':'https://example.test/device','method':'auto','instructions':'Enter code PRIVATE-USER-CODE'}
    if self.path.endswith('/callback'):
     path=home/'.local/share/opencode/auth.json';path.parent.mkdir(parents=True,exist_ok=True)
     path.write_text(json.dumps({'openai':{'type':'oauth','refresh':'FIXTURE-REFRESH','access':'FIXTURE-ACCESS','expires':123}}))
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
  do_GET=request
  do_POST=request
 class LoopbackServer(HTTPServer):
  def server_bind(self):
   TCPServer.server_bind(self)
   self.server_name='localhost'
   self.server_port=self.server_address[1]
 port=int(sys.argv[sys.argv.index('--port')+1])
 LoopbackServer(('127.0.0.1',port),Handler).serve_forever()
'''


_PROTOCOL_V2_FIXTURE = r'''#!/usr/bin/env python3
import base64,json,os,pathlib,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
args=sys.argv[1:]
if args==['--version']:
 print('opencode v2.1.3')
 raise SystemExit(0)
home=pathlib.Path(os.environ['HOME'])
data_home=pathlib.Path(os.environ['XDG_DATA_HOME'])
auth_file=data_home/'opencode/fake-auth.json'
if args[:1]==['api']:
 assert args==['api','GET','/api/model','--standalone']
 print(json.dumps({'location':{},'data':[{'id':'fixture','modelID':'fixture','providerID':'opencode-go','name':'Fixture','capabilities':{'tools':True},'variants':[{'id':'low'}]}]}))
 raise SystemExit(0)
def credentials():
 if not auth_file.is_file(): return []
 value=json.loads(auth_file.read_text())
 return value if isinstance(value,list) else []
def save(rows):
 auth_file.parent.mkdir(parents=True,exist_ok=True)
 auth_file.write_text(json.dumps(rows));auth_file.chmod(0o600)
if args[:2]==['auth','list']:
 rows=[]
 for item in credentials():
  rows.append({'id':item['integrationID'],'name':item['integrationID'],'connections':[{'type':'credential','id':item['id'],'label':item.get('label',''), 'method':item.get('value',{}).get('type','key')}]})
 print(json.dumps(rows));raise SystemExit(0)
if args[:2]==['auth','export']:
 target=args[3] if len(args)>3 else None
 rows=[item for item in credentials() if not target or item['integrationID']==target]
 print(json.dumps(rows));raise SystemExit(0)
if args[:2]==['auth','import']:
 source=pathlib.Path(args[3])
 incoming=json.loads(source.read_text())
 current={item['integrationID']:item for item in credentials()}
 for item in incoming: current[item['integrationID']]=item
 save(list(current.values()));raise SystemExit(0)
if args[:2]==['auth','logout']:
 target=args[3]
 save([item for item in credentials() if item['integrationID']!=target]);raise SystemExit(0)
assert args[0]=='serve' and '--pure' not in args and '--log-level' not in args
assert os.environ['OPENCODE_PURE']=='true'
assert os.environ['OPENCODE_SERVER_USERNAME']=='opencode'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def request(self):
  expected='Basic '+base64.b64encode((os.environ['OPENCODE_SERVER_USERNAME']+':'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
  if self.headers.get('Authorization')!=expected:
   self.send_response(401);self.end_headers();return
  value={}
  if self.path=='/api/info': value={'status':'ok'}
  if self.path=='/api/integration':
   status=[]
   for item in credentials():
    status.append({'type':'credential','id':item['id'],'label':item.get('label',''), 'method':item.get('value',{}).get('type','key')})
   value={'location':{},'data':[
    {'id':'openai','name':'OpenAI','methods':[{'type':'key'},{'id':'chatgpt-browser','type':'oauth','label':'Codex browser (legacy)'},{'id':'chatgpt-headless','type':'oauth','label':'Codex device code'}],'connections':[]},
    {'id':'opencode-go','name':'OpenCode Go','methods':[{'type':'key'}],'connections':[item for item in status if item['id']=='v2-key']}]}
  if self.path=='/api/integration/openai/connect/oauth' and self.command=='POST':
   body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
   assert body['methodID']=='chatgpt-headless'
   value={'location':{},'data':{'attemptID':'attempt-1','url':'https://example.test/device','instructions':'Complete in browser','mode':'auto','time':{'created':1,'expires':9999999999}}}
  if self.path=='/api/integration/openai/connect/oauth/attempt-1' and self.command=='GET':
   rows=credentials()
   if not any(item['integrationID']=='openai' for item in rows):
    rows.append({'id':'credential-oauth','integrationID':'openai','label':'OAuth','active':True,'value':{'type':'oauth','refresh':'V2-REFRESH','access':'V2-ACCESS'}})
    save(rows)
   value={'location':{},'data':{'status':'complete','time':{'created':1,'expires':9999999999}}}
  if self.path=='/api/integration/opencode-go/connect/key' and self.command=='POST':
   body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
   rows=credentials()
   rows.append({'id':'v2-key','integrationID':'opencode-go','label':'API key','active':True,'value':{'type':'key','key':body['key']}})
   save(rows)
   self.send_response(204);self.end_headers();return
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
 do_GET=request
 do_POST=request
class LoopbackServer(HTTPServer):
 def server_bind(self):
  TCPServer.server_bind(self);self.server_name='localhost';self.server_port=self.server_address[1]
port=int(args[args.index('--port')+1])
LoopbackServer(('127.0.0.1',port),Handler).serve_forever()
'''


class AuthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for binary in ("codex", "opencode"):
            script = self.root / binary
            script.write_text(_PROTOCOL_FIXTURE.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
            script.chmod(0o700)
        self.original_which = __import__("shutil").which
        self.which = patch("adaptive_rsi.auth_sessions.shutil.which", side_effect=lambda name:
                           str(self.root / name) if name in {"codex", "opencode"} else self.original_which(name))
        self.which.start()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "saved-codex"),
            "XDG_DATA_HOME": str(self.root / "saved-data"), "OPENCODE_CONFIG": "/untrusted/project/plugin.json",
            "OPENAI_API_KEY": "DO-NOT-INHERIT"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.which.stop()
        self.temp.cleanup()

    def test_codex_protocol_uses_device_code_and_matches_completion_id(self):
        manager = AuthSessions(self.root / "state")
        try:
            session = manager.start("codex")
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                result = manager.status(session["id"])
                if result["state"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "completed", result)
            data = json.loads((self.root / "saved-codex/auth.json").read_text())
            self.assertEqual(data["tokens"]["refresh_token"], "FIXTURE-REFRESH")
        finally:
            manager.close()

    def test_opencode_http_protocol_is_authenticated_and_persists_callback(self):
        worker = OpenCodeAuth()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(worker.base + "/provider/auth")
            self.assertEqual(caught.exception.code, 401)
            rows = worker.providers()
            self.assertEqual(rows[0]["id"], "openai")
            self.assertTrue(rows[0]["methods"][0]["supported"])
            fields = worker.start("openai", 0)
            self.assertEqual(fields["method"], "auto")
            self.assertTrue(worker.complete())
            self.assertEqual(json.loads(worker.credentials.read_text())["openai"]["refresh"], "FIXTURE-REFRESH")
        finally:
            home = worker.home
            worker.close()
        self.assertFalse(home.exists())

    def test_opencode_v1_session_save_and_api_key_use_auth_json_not_v2_integration_api(self):
        manager = AuthSessions(self.root / "v1-state")
        try:
            session = manager.start("opencode", "openai", 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = manager.status(session["id"])
                if result["state"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "completed", result)
            path = self.root / "saved-data/opencode/auth.json"
            self.assertEqual(json.loads(path.read_text())["openai"]["refresh"], "FIXTURE-REFRESH")
            self.assertFalse((self.root / "saved-data/opencode/fake-auth.json").exists())

            manager.set_api_key("opencode", "openai", "V1-PRIVATE-KEY")
            self.assertEqual(json.loads(path.read_text())["openai"], {"type": "api", "key": "V1-PRIVATE-KEY"})
            self.assertFalse((self.root / "saved-data/opencode/fake-auth.json").exists())
        finally:
            manager.close()

    def test_loopback_fixture_skips_reverse_dns_and_uses_current_python(self):
        script = self.root / "opencode"
        fixture = script.read_text().replace(
            "from socketserver import TCPServer",
            "from socketserver import TCPServer\nimport socket\n"
            "def forbidden_lookup(*args): raise OSError('reverse DNS unavailable')\n"
            "socket.getfqdn=forbidden_lookup", 1)
        script.write_text(fixture)
        worker = OpenCodeAuth()
        try:
            self.assertEqual(worker.http("GET", "/global/health")["python"], list(sys.version_info[:2]))
            self.assertEqual(worker.providers()[0]["id"], "openai")
        finally:
            worker.close()

    def test_invalid_protocol_input_never_starts_authorization(self):
        worker = OpenCodeAuth()
        try:
            with self.assertRaisesRegex(AuthError, "invalid_auth_inputs"):
                worker.start("openai", 0, {"unknown": "PRIVATE"})
            with self.assertRaisesRegex(AuthError, "unsupported_auth_method"):
                worker.start("openai", 1)
            self.assertFalse(worker.credentials.exists())
        finally:
            worker.close()

    def test_parent_crash_removes_isolated_home_and_child_server(self):
        script = """import json,os,shutil
from adaptive_rsi.auth_sessions import OpenCodeAuth
original=shutil.which
shutil.which=lambda name: %r if name=='opencode' else original(name)
worker=OpenCodeAuth()
print(json.dumps({'home':str(worker.home),'base':worker.base}),flush=True)
os._exit(0)
""" % str(self.root / "opencode")
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            details = json.loads(process.stdout.readline())
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while Path(details["home"]).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Path(details["home"]).exists())
            with self.assertRaises(urllib.error.URLError):
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(details["base"] + "/global/health", timeout=1)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


class OpenCodeV2AuthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root / "opencode"
        script.write_text(_PROTOCOL_V2_FIXTURE.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
        script.chmod(0o700)
        self.original_which = __import__("shutil").which
        self.which = patch("adaptive_rsi.auth_sessions.shutil.which", side_effect=lambda name:
                           str(script) if name == "opencode" else self.original_which(name))
        self.which.start()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "saved-codex"),
            "XDG_DATA_HOME": str(self.root / "saved-data"),
            "XDG_CONFIG_HOME": str(self.root / "saved-config"),
            "XDG_CACHE_HOME": str(self.root / "saved-cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.which.stop()
        self.temp.cleanup()

    def wait_state(self, manager, session, state):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = manager.status(session["id"])
            if result["state"] in {"completed", "failed", "expired"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["state"], state, result)
        return result

    def test_v2_oauth_key_snapshot_and_logout_use_integration_api_and_import(self):
        manager = AuthSessions(self.root / "state")
        try:
            initial = manager.snapshot()["opencode"]
            self.assertEqual(initial["version"], "2.1.3")
            self.assertEqual(initial["state"], "signed_out")
            providers = manager.providers("opencode")
            openai = next(row for row in providers if row["id"] == "openai")
            self.assertFalse(next(method for method in openai["methods"] if "browser" in method["label"])["supported"])
            oauth = next(method for method in openai["methods"] if method["type"] == "oauth" and method["supported"])
            session = manager.start("opencode", "openai", oauth["index"])
            result = self.wait_state(manager, session, "completed")
            self.assertNotIn("verification_url", result)
            stored = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            self.assertEqual(stored[0]["integrationID"], "openai")
            self.assertEqual(manager.snapshot()["opencode"]["authenticated_providers"], ["openai"])

            manager.set_api_key("opencode", "opencode-go", "V2-PRIVATE-KEY")
            stored = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            key = next(item for item in stored if item["integrationID"] == "opencode-go")
            self.assertEqual(key["value"], {"type": "key", "key": "V2-PRIVATE-KEY"})
            self.assertEqual(manager.snapshot()["opencode"]["authenticated_providers"], ["openai", "opencode-go"])
            connected = next(row for row in manager.providers("opencode") if row["id"] == "opencode-go")
            self.assertTrue(connected["connected"])

            manager.logout("opencode", "openai")
            remaining = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            self.assertEqual([item["integrationID"] for item in remaining], ["opencode-go"])
        finally:
            manager.close()

    def test_v2_worker_install_writes_legacy_auth_when_host_cli_is_v1(self):
        worker = object.__new__(OpenCodeAuth)
        worker.home = self.root / "worker-home"
        credentials = [{"id": "credential", "integrationID": "provider", "label": "Provider",
                        "active": True, "value": {"type": "oauth", "refresh": "private-refresh"}}]
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "fallback-data")}), patch(
                "adaptive_rsi.auth_sessions.shutil.which", return_value="/fake/opencode"), patch(
                "adaptive_rsi.auth_sessions.detect_opencode",
                return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))), patch(
                "adaptive_rsi.auth_sessions.import_auth") as imported:
            worker.install_credential("provider", credentials)
            stored = json.loads((self.root / "fallback-data/opencode/auth.json").read_text())
            exported = load_auth_export()
        imported.assert_not_called()
        self.assertEqual(stored["provider"]["refresh"], "private-refresh")
        self.assertEqual(exported, [])
