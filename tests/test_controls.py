import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, merge, validate
from adaptive_rsi.gitops import git, github_url
from adaptive_rsi.process import Sandbox, checked
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from adaptive_rsi.updates import RELEASE_API, extract_bundle, release_info
from tests.test_runtime import CHECKS, FakeAgent, fixture


class ValueAndIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project-a")
        self.other = fixture(self.root / "project-b")
        self.store = Store(self.root / "state")
        self.agent = FakeAgent()
        self.runtime = Runtime(self.store, self.agent)
        self.project = self.runtime.add_project(self.repo)
        self.project_b = self.runtime.add_project(self.other)
        self.runtime.configure({"checks": CHECKS, "sources": {"inspection": False, "issues": False}, "limits": {"retries": 0}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def process(self, decision="ready", **value):
        class Triage(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["decision"] = decision
                    result["value"].update(value)
                return result
        self.runtime.agent = Triage()
        task = self.runtime.goal(self.project, "Consider a proposed change")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        return task, self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))

    def test_deferred_and_rejected_never_execute_and_keep_value_evidence(self):
        for decision in ["deferred", "rejected"]:
            with self.subTest(decision=decision):
                task, run = self.process(decision)
                self.assertEqual(run["state"], decision)
                self.assertEqual([role for role, _ in self.runtime.agent.roles], ["planning"])
                self.assertIn("inaction", json.loads(run["result"])["value"])
                if decision == "rejected":
                    self.runtime.retry(task)
                    self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_model_cannot_override_insufficient_value(self):
        task, run = self.process("ready", impact="low", effort="high")
        self.assertEqual(run["state"], "deferred")
        self.assertEqual([role for role, _ in self.runtime.agent.roles], ["planning"])
        self.assertIn("disproportionate", json.loads(run["result"])["reason"])

    def test_empty_evidence_and_project_registration_during_active_run_are_blocked(self):
        _, run = self.process("ready", evidence=["  "])
        self.assertEqual(run["state"], "deferred")
        self.runtime.goal(self.project, "Pending task")
        active = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertIsNotNone(active)
        with self.assertRaisesRegex(ValueError, "active runs"):
            self.runtime.add_project(fixture(self.root / "project-c"))
        with patch("adaptive_rsi.runtime.checked") as clone:
            with self.assertRaisesRegex(ValueError, "active runs"):
                self.runtime.import_project("https://github.com/example/third")
            clone.assert_not_called()
        self.assertEqual(self.runtime.add_project(self.other), self.project_b)

    def test_deferred_work_only_reopens_after_new_evidence(self):
        task, run = self.process("deferred")
        config = self.runtime.config(self.project)
        now = time.time()
        self.runtime.schedule(self.project, config, now)
        recheck = self.store.claim(self.project, config)
        self.runtime.process(recheck)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "deferred")
        (self.repo / "calculator.py").write_text("def add(a,b): return a+b\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "new evidence")
        self.runtime.schedule(self.project, config, now + config["schedule"]["light_minutes"] * 60 + 1)
        recheck = self.store.claim(self.project, config)
        self.runtime.process(recheck)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
        self.assertEqual([role for role, _ in self.runtime.agent.roles], ["planning"])

    def test_project_context_and_worker_protected_paths_are_isolated(self):
        a = self.store.add_task(self.project, "issue", "issue:1", "Project A only")
        b = self.store.add_task(self.project_b, "issue", "issue:1", "Project B only")
        self.assertNotEqual(a, b)
        captured = []
        class Capture:
            def invoke(self, *args):
                captured.append(args)
                return {}
        self.runtime.agent = Capture()
        self.runtime.invoke("analysis", "Project A", self.repo, {"project": self.project, "id": "test", "config": encode(self.runtime.config(self.project))})
        self.assertEqual(set(captured[0][-1]), {str(self.repo), str(self.other)})
        self.assertEqual(captured[0][3], self.project)
        self.assertEqual(self.runtime.memories(self.project, "Project B"), [])
        self.assertEqual(self.runtime.config(self.project_b)["checks"], [])
        with self.assertRaisesRegex(ValueError, "contain"):
            self.runtime.add_project(fixture(self.repo / "nested"))

    @unittest.skipUnless(platform.system() == "Darwin", "Seatbelt filesystem enforcement")
    def test_worker_and_native_command_cannot_read_other_project(self):
        (self.other / "private.txt").write_text("project-b-secret")
        workspace = self.root / "worker"
        workspace.mkdir()
        program = "from pathlib import Path; print(Path(" + repr(str(self.other / "private.txt")) + ").read_text())"
        sandbox = Sandbox(self.store.root, DEFAULTS)
        from adaptive_rsi.process import run_process
        result = run_process(sandbox.worker([sys.executable, "-c", program], workspace,
                                            [self.repo, self.other]), workspace)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn("project-b-secret", result["stdout"])
        with patch.dict(os.environ, {"PRIVATE_OTHER_KEY": "do-not-inherit"}):
            result = sandbox.check({"name": "project isolation", "cwd": ".", "argv": [sys.executable, "-c", program]}, workspace,
                                   hidden=[self.repo, self.other])
        self.assertNotEqual(result["exit_code"], 0)
        env = sandbox.check({"name": "env isolation", "cwd": ".", "argv": [sys.executable, "-c", "import os; print(os.environ.get('PRIVATE_OTHER_KEY','absent'))"]}, workspace)
        self.assertEqual(env["exit_code"], 0, env["stderr"])
        self.assertEqual(env["stdout"].strip(), "absent")

    def test_github_import_registers_separate_paused_projects_without_network(self):
        def fake_clone(argv, **kwargs):
            repo, destination = argv[3], argv[4]
            checked(["git", "clone", "--quiet", str(self.repo), destination])
            git(destination, "remote", "set-url", "origin", "https://github.com/" + repo + ".git")
            return ""
        with patch("adaptive_rsi.runtime.checked", side_effect=fake_clone):
            first = self.runtime.import_project("https://github.com/example/first.git")
            second = self.runtime.import_project("https://github.com/example/second")
            self.assertEqual(self.runtime.import_project("https://github.com/example/first"), first)
        self.assertNotEqual(first, second)
        self.assertTrue(self.runtime.project(first)["paused"])
        self.assertEqual(self.runtime.config(first)["github"]["repository"], "example/first")
        self.assertEqual(self.runtime.config(second)["github"]["repository"], "example/second")
        for bad in ["https://github.com/example/repo/issues", "https://token@github.com/a/b", "https://other.example/a/b", "--help", "https://github.com/../repo"]:
            with self.assertRaises(ValueError):
                github_url(bad)

    def test_automatic_delivery_is_explicit_per_project_and_persists(self):
        with self.assertRaises(ValueError):
            self.runtime.configure({"delivery": {"mode": "pr", "auto_merge": True}})
        with self.assertRaises(ValueError):
            self.runtime.configure({"delivery": {"auto_merge": True}}, self.project)
        with self.assertRaisesRegex(RuntimeError, "explicit approval"):
            self.runtime.publish(self.runtime.project(self.project), {}, {"id": "fake", "state": "awaiting_approval"}, self.repo, DEFAULTS)
        self.runtime.configure({"delivery": {"mode": "pr", "auto_merge": True}}, self.project)
        Store(self.store.root)
        self.assertEqual(self.runtime.config(self.project)["delivery"]["mode"], "pr")
        self.assertTrue(self.runtime.config(self.project)["delivery"]["auto_merge"])
        self.assertEqual(self.runtime.config()["delivery"]["mode"], "local")
        with self.assertRaises(ValueError):
            self.runtime.goal(self.project, "test", {"delivery": {"mode": "pr"}})


def release(target="0.2.2"):
    prefix = "https://github.com/Haoog/Adaptive-RSI/releases/download/v" + target + "/"
    name = "adaptive-rsi-" + target + "-plugin.zip"
    return {"tag_name": "v" + target, "assets": [{"name": n, "browser_download_url": prefix + n} for n in [name, name + ".sha256"]]}


def bundle(target="0.2.2", extra=None):
    data = io.BytesIO()
    files = {"update.json": json.dumps({"format": 1, "version": target, "python_min": [3, 9], "state_schema": 1}),
             "plugin.json": json.dumps({"version": target}), "adaptive_rsi/cli.py": "", "scripts/start.py": ""}
    with zipfile.ZipFile(data, "w") as archive:
        for path, value in files.items():
            archive.writestr("adaptive-rsi/" + path, value)
        if extra:
            archive.writestr(*extra)
    return data.getvalue()


class UpdatesTest(unittest.TestCase):
    def test_restored_login_is_writable_without_an_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp))
            try:
                runtime.store.put_setting("update-status", {"state": "no_release"})
                runtime.updates.restore_startup()
                runtime.updates.require_ready()
                self.assertFalse(runtime.updates.snapshot()["busy"])
                runtime.configure({"schedule": {"light_minutes": 240}})
                self.assertEqual(runtime.config()["schedule"]["light_minutes"], 240)
            finally:
                runtime.close()

    def test_upgrade_unlocks_from_committed_result_without_a_ready_marker(self):
        from adaptive_rsi import __version__
        from adaptive_rsi.updates import UpdateInProgress
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp))
            try:
                runtime.store.put_setting("update-status", {"state": "restarting", "version": __version__})
                runtime.updates.restore_startup()
                ready = runtime.store.root / "update-ready"
                ready.write_text("stale marker")
                with self.assertRaises(UpdateInProgress):
                    runtime.updates.require_ready()
                self.assertTrue(runtime.updates.snapshot()["busy"])
                ready.unlink()
                runtime.store.put_setting("update-status", {"state": "installed", "current": "wrong-version"})
                with self.assertRaises(UpdateInProgress):
                    runtime.updates.require_ready()
                runtime.store.put_setting("update-status", {"state": "installed", "current": __version__})
                runtime.updates.require_ready()
                self.assertFalse(runtime.updates.snapshot()["busy"])
            finally:
                runtime.close()

    def test_ready_marker_does_not_unlock_an_active_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp))
            try:
                runtime.updates.installing = True
                (runtime.store.root / "update-ready").write_text("stale marker")
                self.assertTrue(runtime.updates.is_busy())
                self.assertTrue(runtime.updates.installing)
            finally:
                runtime.close()

    def test_bootstrap_selects_managed_version_for_explicit_state_directory(self):
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "selected-state"
            managed = state / "installations/version-a"
            package = managed / "adaptive_rsi"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "cli.py").write_text("def main(): print('selected-managed-version')\n")
            (state / "installations/active.json").write_text(json.dumps({"path": str(managed)}))
            entry = Path(__file__).resolve().parents[1] / "scripts/rsi"
            env = {**os.environ, "ADAPTIVE_RSI_HOME": str(root / "another-state")}
            result = subprocess.run([sys.executable, str(entry), "--state-dir", str(state), "doctor"],
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "selected-managed-version")

    def test_supervised_upgrade_and_failed_start_restore_real_service_and_database(self):
        from adaptive_rsi import __version__
        from adaptive_rsi.updates import supervise_restart
        import urllib.request
        source = Path(__file__).resolve().parents[1]
        for healthy in [True, False]:
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                state = root / "state"
                store = Store(state)
                store.put_setting("config", {"updates": {"mode": "manual"}})
                store.put_setting("update-status", {"state": "restarting", "version": "0.2.2", "current": __version__})
                store.put_setting("sentinel", {"project": "keep-me", "settings": [1, 2, 3]})
                installs = state / "installations"
                installs.mkdir()
                candidate = installs / "candidate"
                shutil.copytree(source / "adaptive_rsi", candidate / "adaptive_rsi", ignore=shutil.ignore_patterns("__pycache__"))
                (candidate / "adaptive_rsi/__init__.py").write_text('__version__ = "0.2.2"\n')
                if not healthy:
                    (candidate / "adaptive_rsi/__main__.py").write_text(
                        "import sqlite3, os\nfrom pathlib import Path\n"
                        "state=Path(os.environ['RSI_TEST_RESTART_STATE'])\n"
                        "with sqlite3.connect(state/'state.sqlite3') as db:\n"
                        " db.execute(\"DELETE FROM settings WHERE key='sentinel'\")\n"
                        "raise SystemExit(3)\n")
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                session = {"origin": "http://127.0.0.1:" + str(port), "cookie": "rsi_" + str(port),
                           "token": "fixture-only-token", "csrf": "fixture-only-csrf", "url": "http://127.0.0.1:" + str(port) + "/?token=fixture-only-token", "pid": -1}
                (installs / "restart.json").write_text(json.dumps({"candidate": str(candidate), "previous": str(source), "target": "0.2.2", "pid": -1, "session": session}))
                children = []
                real_popen = subprocess.Popen
                def spawn(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child
                try:
                    with patch.dict(os.environ, {"RSI_TEST_RESTART_STATE": str(state)}), patch("adaptive_rsi.updates.subprocess.Popen", side_effect=spawn):
                        supervise_restart(state)
                    expected = "0.2.2" if healthy else __version__
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            req = urllib.request.Request(session["origin"] + "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
                            with urllib.request.urlopen(req, timeout=1) as response:
                                health = json.load(response)
                            break
                        except OSError as error:
                            if time.monotonic() > deadline:
                                self.fail(str({"health_error": str(error), "children": [child.poll() for child in children],
                                               "update": store.setting("update-status"),
                                               "log": (state / "server.log").read_text()[-12000:]}))
                            time.sleep(0.1)
                    self.assertEqual(health["version"], expected)
                    self.assertEqual(store.setting("sentinel"), {"project": "keep-me", "settings": [1, 2, 3]})
                    self.assertEqual(store.setting("update-status")["state"], "installed" if healthy else "rolled_back")
                    self.assertEqual((installs / "active.json").exists(), healthy)
                    self.assertEqual((installs / "current").exists(), healthy)
                    request = urllib.request.Request(session["origin"] + "/api/config",
                        json.dumps({"values": {"schedule": {"light_minutes": 240}}}).encode(),
                        {"Cookie": session["cookie"] + "=" + session["token"], "Origin": session["origin"],
                         "X-RSI-CSRF": session["csrf"], "Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=3) as response:
                        self.assertTrue(json.load(response)["saved"])
                    self.assertEqual(store.setting("config")["schedule"]["light_minutes"], 240)
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()

    def test_release_policy_and_asset_identity(self):
        self.assertTrue(release_info(release())["patch"])
        self.assertFalse(release_info(release("0.3.0"))["patch"])
        self.assertEqual(release_info(release("0.1.0"))["state"], "up_to_date")
        data = release()
        data["assets"][0]["browser_download_url"] = "https://github.com/other/repo/releases/download/v0.2.2/bundle.zip"
        with self.assertRaisesRegex(ValueError, "another repository"):
            release_info(data)
        with self.assertRaises(ValueError):
            release_info({"tag_name": "v0.2.2"})

    def test_archive_verifies_hash_paths_and_migration_contract(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = bundle()
            installed = extract_bundle(data, hashlib.sha256(data).hexdigest(), root / "valid", "0.2.2")
            self.assertTrue((installed / "update.json").is_file())
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                extract_bundle(data, "0" * 64, root / "bad-hash", "0.2.2")
            malicious = bundle(extra=("adaptive-rsi/../../escaped", "bad"))
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                extract_bundle(malicious, hashlib.sha256(malicious).hexdigest(), root / "bad-path", "0.2.2")
            self.assertFalse((root / "escaped").exists())
            with self.assertRaisesRegex(ValueError, "manifest"):
                extract_bundle(data, hashlib.sha256(data).hexdigest(), root / "wrong-version", "0.3.0")

    def test_install_waits_for_idle_and_automatic_mode_only_applies_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp), FakeAgent())
            try:
                manager = runtime.updates
                manager.shutdown = lambda: None
                runtime.configure({"updates": {"mode": "automatic"}})
                for target, count in [("0.2.2", 1), ("0.3.0", 0)]:
                    runtime.store.put_setting("update-status", {**release_info(release(target)), "checked": time.time()})
                    with patch.object(manager, "apply_async") as apply:
                        manager.tick()
                        self.assertEqual(apply.call_count, count)
                runtime.store.execute("INSERT INTO runs(id,state) VALUES ('busy','planning')")
                available = {**release_info(release()), "checked": time.time(), "requested": True}
                with patch("adaptive_rsi.updates.download") as download:
                    manager.lock.acquire()
                    manager.apply(available)
                    download.assert_not_called()
                self.assertEqual(manager.status()["state"], "waiting_idle")
                runtime.store.execute("UPDATE runs SET ended=1 WHERE id='busy'")
                runtime.configure({"updates": {"mode": "manual"}})
                with patch.object(manager, "apply_async") as apply:
                    manager.tick()
                    apply.assert_called_once()
            finally:
                runtime.close()

    def test_invalid_release_never_triggers_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(Store(temp), FakeAgent())
            try:
                available = {**release_info(release()), "checked": time.time()}
                with patch("adaptive_rsi.updates.download", return_value=b'{}'), patch.object(runtime.updates, "shutdown") as shutdown:
                    runtime.updates.lock.acquire()
                    runtime.updates.apply(available)
                    shutdown.assert_not_called()
                self.assertEqual(runtime.updates.status()["state"], "error")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
