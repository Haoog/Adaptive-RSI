"""模型调用不再全程占用凭据锁；宿主凭据读写段仍然互斥。"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import opencode as opencode_module
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.cli_worker import CODEX_AUTH_LOCK, codex_home, sync_codex_oauth
from adaptive_rsi.config import validate
from adaptive_rsi.opencode import AUTH_LOCK, OpenCodeAgent
from adaptive_rsi.store import Store
from tests.test_opencode_integration import version_fixture


def self_holds(lock):
    """RLock 没有公开的 locked()；_is_owned() 只对持锁线程返回 True。"""
    return lock._is_owned()


def opencode_result():
    return {"exit_code": 0, "error": None, "stderr": "",
            "stdout": json.dumps({"type": "text", "part": {"text": '{"summary":"ok"}'}}) + "\n"}


def codex_result(argv):
    output = Path(argv[argv.index("--output-last-message") + 1])
    output.write_text(json.dumps({"findings": []}), encoding="utf-8")
    return {"exit_code": 0, "error": None, "stderr": "",
            "stdout": json.dumps({"type": "turn.completed",
                                  "usage": {"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0}}) + "\n"}


class LockScopeTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CACHE_HOME": str(self.root / "cache"), "CODEX_HOME": str(self.root / "codex")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def wait_until(self, condition, seconds=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not condition():
            time.sleep(0.01)
        return condition()


class OpenCodeLockScopeTest(LockScopeTestBase):
    def invoke_config(self):
        return validate({"models": {"implementation": {
            "provider": "opencode", "model": "provider/model", "effort": "default"}}})

    def spy(self, real, name, spies):
        def wrapper(*args, **kwargs):
            spies.setdefault(name, []).append(self_holds(AUTH_LOCK))
            return real(*args, **kwargs)
        return wrapper

    def opencode_patches(self, script, run_cli, spies=None):
        return [patch("adaptive_rsi.opencode.platform.system", return_value="Darwin"),
                patch("adaptive_rsi.opencode.shutil.which",
                      side_effect=lambda name: str(script) if name == "opencode" else None),
                patch("adaptive_rsi.opencode.run_cli", side_effect=run_cli),
                patch("adaptive_rsi.opencode.export_auth",
                      side_effect=self.spy(opencode_module.export_auth, "export_auth", spies)),
                patch("adaptive_rsi.opencode.sync_oauth",
                      side_effect=self.spy(opencode_module.sync_oauth, "sync_oauth", spies))]

    def test_model_call_runs_outside_credential_lock(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        script = version_fixture(self.root, "2.1.3")
        store = Store(self.root / "state")
        config = self.invoke_config()
        spies, model_calls = {}, []

        def run_cli(argv, *args, **kwargs):
            model_calls.append(self_holds(AUTH_LOCK))
            return opencode_result()

        with patch("adaptive_rsi.opencode.platform.system", return_value="Darwin"), patch(
                "adaptive_rsi.opencode.shutil.which",
                side_effect=lambda name: str(script) if name == "opencode" else None), patch(
                "adaptive_rsi.opencode.run_cli", side_effect=run_cli), patch(
                "adaptive_rsi.opencode.export_auth",
                side_effect=self.spy(opencode_module.export_auth, "export_auth", spies)), patch(
                "adaptive_rsi.opencode.sync_oauth",
                side_effect=self.spy(opencode_module.sync_oauth, "sync_oauth", spies)):
            value = OpenCodeAgent(store).invoke(
                "probe", "Return a valid result", workspace, "p", "run", config,
                config["models"]["implementation"])
        self.assertEqual(value, {"summary": "ok"})
        # 模型调用段不持锁；宿主凭据导出与 OAuth 回写仍在锁内。
        self.assertEqual(model_calls, [False])
        self.assertEqual(sorted(spies), ["export_auth", "sync_oauth"])
        self.assertTrue(spies["export_auth"] and all(spies["export_auth"]))
        self.assertTrue(spies["sync_oauth"] and all(spies["sync_oauth"]))

    def test_model_calls_overlap_while_credential_sections_stay_locked(self):
        script = version_fixture(self.root, "2.1.3")
        barrier = threading.Barrier(2, timeout=10)
        results, errors = [], []

        def run_cli(argv, *args, **kwargs):
            # 两个线程必须同时位于模型调用段；若退化为全程串行将在此超时失败。
            barrier.wait()
            return opencode_result()

        def invoke(name):
            try:
                root = self.root / name
                store = Store(root / "state")
                workspace = root / "workspace"
                workspace.mkdir(parents=True)
                config = self.invoke_config()
                results.append(OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"]))
            except Exception as exc:  # 记录后交由断言失败
                errors.append(exc)

        with patch("adaptive_rsi.opencode.platform.system", return_value="Darwin"), patch(
                "adaptive_rsi.opencode.shutil.which",
                side_effect=lambda name: str(script) if name == "opencode" else None), patch(
                "adaptive_rsi.opencode.run_cli", side_effect=run_cli):
            threads = [threading.Thread(target=invoke, args=("worker-" + str(index),)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"summary": "ok"}, {"summary": "ok"}])

    def test_credential_snapshot_blocks_until_foreign_lock_releases(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        script = version_fixture(self.root, "2.1.3")
        store = Store(self.root / "state")
        config = self.invoke_config()
        spies, model_calls = {}, []

        def run_cli(argv, *args, **kwargs):
            model_calls.append(self_holds(AUTH_LOCK))
            return opencode_result()

        held, release = threading.Event(), threading.Event()

        def holder():
            with AUTH_LOCK:
                held.set()
                release.wait(5)
        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(held.wait(1), "Holder did not acquire the credential lock")
        try:
            with patch("adaptive_rsi.opencode.platform.system", return_value="Darwin"), patch(
                    "adaptive_rsi.opencode.shutil.which",
                    side_effect=lambda name: str(script) if name == "opencode" else None), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run_cli), patch(
                    "adaptive_rsi.opencode.export_auth",
                    side_effect=self.spy(opencode_module.export_auth, "export_auth", spies)), patch(
                    "adaptive_rsi.opencode.sync_oauth",
                    side_effect=self.spy(opencode_module.sync_oauth, "sync_oauth", spies)):
                worker = threading.Thread(target=lambda: OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"]))
                worker.start()
                self.assertFalse(self.wait_until(lambda: bool(spies.get("export_auth") or model_calls)),
                                 "Credential or model section ran while another thread held the lock")
                release.set()
                worker.join(15)
                self.assertFalse(worker.is_alive())
        finally:
            release.set()
            holder_thread.join(5)
        # 凭据段被外部锁阻塞后仍互斥执行；宿主凭据导出与回写全部发生在锁内。
        self.assertTrue(spies.get("export_auth"))
        self.assertTrue(all(spies["export_auth"]))
        self.assertTrue(all(spies.get("sync_oauth", [True])))
        self.assertEqual(model_calls, [False])


class CodexLockScopeTest(LockScopeTestBase):
    def invoke_config(self):
        return validate({"models": {"analysis": {
            "provider": "openai", "model": "gpt-fixture", "effort": "default"}}})

    def write_host_auth(self):
        codex = self.root / "codex"
        codex.mkdir(parents=True)
        (codex / "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": "HOST-ACCESS", "refresh_token": "HOST-REFRESH"},
             "last_refresh": "2026-01-01T00:00:00Z"}))

    def invoke(self, name):
        store = Store(self.root / name / "state")
        workspace = self.root / name / "workspace"
        workspace.mkdir(parents=True)
        config = self.invoke_config()
        return CodexAgent(store).invoke(
            "analysis", "Return a valid result", workspace, "p", "run", config)

    def test_model_call_runs_outside_credential_lock(self):
        self.write_host_auth()
        spies = {"codex_home": [], "sync_codex_oauth": []}
        model_calls = []
        real_codex_home, real_sync = codex_home, sync_codex_oauth

        def codex_home_spy(home):
            spies["codex_home"].append(self_holds(CODEX_AUTH_LOCK))
            return real_codex_home(home)

        def sync_spy(home, original):
            spies["sync_codex_oauth"].append(self_holds(CODEX_AUTH_LOCK))
            return real_sync(home, original)

        def run_cli(argv, *args, **kwargs):
            model_calls.append(self_holds(CODEX_AUTH_LOCK))
            return codex_result(argv)

        with patch("adaptive_rsi.agent.platform.system", return_value="Linux"), patch(
                "adaptive_rsi.cli_worker.codex_home", side_effect=codex_home_spy), patch(
                "adaptive_rsi.cli_worker.sync_codex_oauth", side_effect=sync_spy), patch(
                "adaptive_rsi.cli_worker.run_cli", side_effect=run_cli):
            value = self.invoke("single")
        self.assertEqual(value, {"findings": []})
        # 模型调用段不持锁；宿主凭据拷贝与 OAuth 回写都在锁内。
        self.assertEqual(model_calls, [False])
        self.assertEqual(spies["codex_home"], [True])
        self.assertEqual(spies["sync_codex_oauth"], [True])

    def test_model_calls_overlap_while_credential_sections_stay_locked(self):
        self.write_host_auth()
        barrier = threading.Barrier(2, timeout=10)
        results, errors = [], []

        def run_cli(argv, *args, **kwargs):
            # 两个线程必须同时位于模型调用段；若退化为全程串行将在此超时失败。
            barrier.wait()
            return codex_result(argv)

        def invoke(name):
            try:
                results.append(self.invoke(name))
            except Exception as exc:  # 记录后交由断言失败
                errors.append(exc)

        with patch("adaptive_rsi.agent.platform.system", return_value="Linux"), patch(
                "adaptive_rsi.cli_worker.run_cli", side_effect=run_cli):
            threads = [threading.Thread(target=invoke, args=("worker-" + str(index),)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"findings": []}, {"findings": []}])

    def test_credential_copy_blocks_until_foreign_lock_releases(self):
        self.write_host_auth()
        spies = {"codex_home": [], "sync_codex_oauth": []}
        model_calls = []
        real_codex_home, real_sync = codex_home, sync_codex_oauth

        def codex_home_spy(home):
            spies["codex_home"].append(self_holds(CODEX_AUTH_LOCK))
            return real_codex_home(home)

        def sync_spy(home, original):
            spies["sync_codex_oauth"].append(self_holds(CODEX_AUTH_LOCK))
            return real_sync(home, original)

        def run_cli(argv, *args, **kwargs):
            model_calls.append(self_holds(CODEX_AUTH_LOCK))
            return codex_result(argv)

        held, release = threading.Event(), threading.Event()

        def holder():
            with CODEX_AUTH_LOCK:
                held.set()
                release.wait(5)
        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(held.wait(1), "Holder did not acquire the credential lock")
        try:
            with patch("adaptive_rsi.agent.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.cli_worker.codex_home", side_effect=codex_home_spy), patch(
                    "adaptive_rsi.cli_worker.sync_codex_oauth", side_effect=sync_spy), patch(
                    "adaptive_rsi.cli_worker.run_cli", side_effect=run_cli):
                worker = threading.Thread(target=lambda: self.invoke("blocked"))
                worker.start()
                self.assertFalse(self.wait_until(lambda: bool(spies["codex_home"] or model_calls)),
                                 "Credential or model section ran while another thread held the lock")
                release.set()
                worker.join(15)
                self.assertFalse(worker.is_alive())
        finally:
            release.set()
            holder_thread.join(5)
        # 凭据拷贝被外部锁阻塞后仍互斥执行；回写也发生在锁内。
        self.assertEqual(spies["codex_home"], [True])
        self.assertEqual(spies["sync_codex_oauth"], [True])
        self.assertEqual(model_calls, [False])


if __name__ == "__main__":
    unittest.main()
