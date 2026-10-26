"""小时调度、无额度用量、OpenCode 和 Linux 容器执行的回归验证。"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent, SCHEMAS, validate_output
from adaptive_rsi.cli_worker import cli_home, run_cli, codex_home, sync_codex_oauth
from adaptive_rsi.config import DEFAULTS, merge, validate
from adaptive_rsi.credentials import Credentials
from adaptive_rsi.discovery import sync
from adaptive_rsi.gitops import git
from adaptive_rsi.opencode import jsonc, parse_events, prepare_home, sync_oauth, check_provider_errors
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store, encode
from adaptive_rsi.usage import native_usage, opencode_usage
from tests.test_runtime import FakeAgent, fixture, CHECKS


class ServerExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.repo = fixture(self.root / "project")
        self.project = self.runtime.add_project(self.repo)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_only_two_hourly_schedules_and_legacy_deep_coalesces(self):
        self.runtime.configure({"github": {"repository": "owner/project"}}, self.project)
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=100)
        rows = self.store.query("SELECT kind,due FROM schedules ORDER BY kind")
        self.assertEqual(rows, [{"kind": "issues", "due": 3700.0}, {"kind": "light", "due": 3700.0}])
        self.assertEqual(self.runtime.request(self.project, "deep"), self.runtime.request(self.project, "light"))
        self.runtime.schedule(self.project, self.runtime.config(self.project), now=3800)
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 2)
        self.store.add_task(self.project, "issue", "backlog", "Existing issue", priority=60)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertIn(self.store.one("SELECT source FROM tasks WHERE id=?", (run["task"],))["source"],
                      ["system_light", "system_issues"])

    def test_issue_request_without_repository_is_rejected_before_queueing(self):
        self.runtime.configure({"github": {"repository": ""}, "sources": {"issues": True}},
                               self.project)
        with self.assertRaisesRegex(ValueError, "^No GitHub repository configured$"):
            self.runtime.request(self.project, "issues")
        self.assertEqual(self.store.query("SELECT id FROM tasks WHERE source='system_issues'"), [])

    def test_manual_requests_stay_available_while_sources_are_disabled(self):
        self.runtime.configure({"github": {"repository": "owner/project"},
                                "sources": {"inspection": False, "issues": False}}, self.project)
        light = self.runtime.request(self.project, "light")
        issues = self.runtime.request(self.project, "issues")
        self.assertEqual(
            {self.store.one("SELECT source FROM tasks WHERE id=?", (task,))["source"]
             for task in (light, issues)},
            {"system_light", "system_issues"})

    def test_unlimited_calls_and_legacy_tasks_recover(self):
        config = validate({"limits": {"daily_calls": 1, "run_calls": 1, "run_seconds": 1, "max_changed_files": 1}})
        task = self.runtime.goal(self.project, "record usage")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        for _ in range(105):
            call = self.store.reserve_call(self.project, run["id"], "planning", config["models"]["planning"], config)
            self.store.record_usage(call, run["id"], native_usage("responses", {"usage": {"input_tokens": 10, "output_tokens": 5}}), 0.1)
        self.assertEqual(self.store.one("SELECT calls,tokens FROM runs WHERE id=?", (run["id"],)), {"calls": 105, "tokens": 1575})
        self.store.transition(run["id"], "waiting_budget", {}, True)
        self.runtime.resume_budget(self.project, config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_inspection_keeps_more_than_five_evidenced_findings(self):
        findings = [{"title": "Defect " + str(i), "body": "A concrete defect", "path": "calculator.py",
                     "quote": "return a - b" + "\n" * i, "acceptance": ["Addition is correct"], "priority": 50}
                    for i in range(8)]
        # 不同源锚点保留为独立问题。
        (self.repo / "calculator.py").write_text("\n".join("def bug_%d(a,b): return a-b" % i for i in range(8)) + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "findings")
        for i, item in enumerate(findings):
            item["quote"] = "def bug_%d(a,b): return a-b" % i
        with patch.object(self.runtime, "invoke", return_value={"findings": findings}):
            self.runtime.inspect(self.runtime.project(self.project), self.repo, "light", {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='inspection'")), 8)
        validate_output({"findings": findings * 20}, SCHEMAS["analysis"])

    def test_inspection_batch_triage_keeps_evidence_and_only_queues_valuable_findings(self):
        findings = [
            {"title": "Cosmetic naming " + str(i), "body": "A low-value naming preference",
             "path": "calculator.py", "quote": "return a - b", "acceptance": ["Names are clearer"],
             "priority": priority}
            for i, priority in enumerate([20, 30, 40, 80])
        ]
        findings.append({**findings[0], "body": "Duplicate report at the same anchor"})
        calls = []
        def invoke(role, prompt, workspace, run):
            calls.append(role)
            if role == "analysis":
                return {"findings": findings}
            if role == "triage":
                return {"findings": [
                    {"index": 0, "decision": "rejected", "confidence": "high", "reason": "Cosmetic churn"},
                    {"index": 1, "decision": "deferred", "confidence": "medium", "reason": "Weak current value"},
                    {"index": 2, "decision": "ready", "confidence": "high", "reason": "Concrete maintenance benefit"},
                    {"index": 3, "decision": "rejected", "confidence": "high", "reason": "Should be protected by priority"},
                ]}
            raise AssertionError(role)
        with patch.object(self.runtime, "invoke", side_effect=invoke):
            self.runtime.inspect(
                self.runtime.project(self.project), self.repo, "light",
                {"id": "inspection", "config": encode(self.runtime.config(self.project))}, [])
        rows = self.store.query("SELECT id,title,state,evidence FROM tasks WHERE source='inspection' ORDER BY title")
        self.assertEqual(calls.count("analysis"), 1)
        self.assertEqual(calls.count("triage"), 1)
        states = {row["title"].split()[-1]: row["state"] for row in rows}
        self.assertEqual(states, {"0": "rejected", "1": "deferred", "2": "queued", "3": "queued"})
        self.assertTrue(all(json.loads(row["evidence"])[0]["quote"] == "return a - b" for row in rows))
        deferred = next(row for row in rows if row["title"].endswith("1"))
        config = self.runtime.config(self.project)
        head = git(self.repo, "rev-parse", "HEAD")
        self.runtime.reassess_deferred(self.project, config, head)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (deferred["id"],))["state"], "deferred")
        (self.repo / "calculator.py").write_text((self.repo / "calculator.py").read_text() + "\n# new baseline\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "new baseline")
        self.runtime.reassess_deferred(self.project, config, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (deferred["id"],))["state"], "queued")
        validate_output({"findings": [
            {"index": 0, "decision": "ready", "confidence": "high", "reason": "Concrete defect"}]},
            SCHEMAS["triage"])

    def test_issues_paginate_past_old_total_limit(self):
        config = merge(DEFAULTS, {"github": {"repository": "owner/project", "max_issues": 1}})
        pages = []
        def fetch(argv, **kwargs):
            page = int(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("page=")))
            pages.append(page)
            start, end = (page - 1) * 100 + 1, min(page * 100 + 1, 206)
            return encode([{"number": i, "title": str(i), "body": "", "state": "open", "labels": [],
                            "updated_at": "2026-01-01T00:00:00Z", "html_url": "https://github.com/owner/project/issues/" + str(i)}
                           for i in range(start, end)])
        sync(self.runtime, self.runtime.project(self.project), config, fetch)
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 205)

    def test_file_credentials_survive_restart_and_never_enter_config(self):
        provider = self.runtime.model_center.save_provider({"name": "Server", "protocol": "responses", "base_url": "https://example.com/v1", "env_key": ""}, {"mode": "file", "value": "server-test-secret"})
        item = self.runtime.model_center.provider(provider["id"])
        restarted = Credentials(self.store.root)
        self.assertEqual(restarted.get(item), "server-test-secret")
        self.assertEqual((self.store.root / "credentials" / item["secret_ref"]).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("server-test-secret", encode(self.runtime.config()))
        self.assertNotIn("server-test-secret", encode(self.runtime.model_center.snapshot()))

    def test_public_origin_is_explicit_and_health_remains_local(self):
        for address in ["http://example.com", "https://example.com/path", "https://user:pass@example.com"]:
            with self.assertRaises(ValueError):
                Server(self.runtime, public_url=address)
        server = Server(self.runtime, public_url="https://rsi.example.com")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from scripts.service import live_session
            session = {"origin": server.origin, "local_origin": server.local_origin,
                       "cookie": server.cookie, "token": server.token, "pid": os.getpid()}
            (self.store.root / "server.json").write_text(encode(session))
            self.assertEqual(live_session(self.store.root)["pid"], os.getpid())
            session["pid"] = -1
            (self.store.root / "server.json").write_text(encode(session))
            with self.assertRaisesRegex(ValueError, "process changed"):
                live_session(self.store.root)
            headers = {"Host": "rsi.example.com", "Cookie": server.cookie + "=" + server.token}
            with urllib.request.urlopen(urllib.request.Request(server.local_origin + "/api/state", headers=headers)) as response:
                self.assertEqual(response.status, 200)
            headers["Host"] = "untrusted.example"
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(urllib.request.Request(server.local_origin + "/api/state", headers=headers))
            self.assertEqual(rejected.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class UsageAndOpenCodeTest(unittest.TestCase):
    def test_codex_container_oauth_refresh_survives_the_temporary_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "login/auth.json"
            source.parent.mkdir()
            original = {"tokens": {"access_token": "old", "refresh_token": "old-refresh"}, "last_refresh": "old-date"}
            source.write_text(encode(original))
            with patch.dict(os.environ, {"CODEX_HOME": str(source.parent)}), cli_home(root) as home:
                copied = codex_home(home)
                target = home / ".codex/auth.json"
                target.write_text(encode({"tokens": {"access_token": "new", "refresh_token": "new-refresh", "extra": "bad"},
                                         "last_refresh": "new-date", "extra": "bad"}))
                sync_codex_oauth(home, copied)
                saved = json.loads(source.read_text())
                self.assertEqual(saved, {"tokens": {"access_token": "new", "refresh_token": "new-refresh"}, "last_refresh": "new-date"})
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                saved["tokens"]["access_token"] = "external-login"
                source.write_text(encode(saved))
                sync_codex_oauth(home, copied)
                self.assertEqual(json.loads(source.read_text())["tokens"]["access_token"], "external-login")

    def test_opencode_availability_error_is_classified_without_echoing_credentials(self):
        from adaptive_rsi.native import ProviderUnavailable
        from adaptive_rsi.provider_health import error_code
        with self.assertRaises(ProviderUnavailable) as failure:
            check_provider_errors(encode({"type": "error", "error": {"name": "APIError", "data": {
                "statusCode": 429, "message": "private-key-value"}}}))
        self.assertEqual(error_code(failure.exception), "rate_limit")
        self.assertNotIn("private-key-value", str(failure.exception))
        with self.assertRaises(RuntimeError) as failure:
            check_provider_errors(encode({"type": "error", "error": {"name": "ProviderAuthError"}}))
        self.assertEqual(error_code(failure.exception), "auth")

    def test_opencode_isolates_config_and_refreshes_only_selected_oauth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data/opencode/auth.json"
            source.parent.mkdir(parents=True)
            original = {"selected": {"type": "oauth", "access": "old-access-token", "refresh": "old-refresh-token", "expires": 1},
                        "other": {"type": "api", "key": "unrelated-secret"}}
            source.write_text(encode(original))
            config = root / "config/opencode/opencode.jsonc"
            config.parent.mkdir(parents=True)
            config.write_text(encode({"provider": {"selected": {"options": {"baseURL": "https://example.com"}}, "other": {}},
                                      "plugin": ["untrusted-plugin"], "mcp": {"untrusted": {}}, "instructions": ["untrusted"]}))
            home = root / "isolated"
            home.mkdir()
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
                                         "OPENCODE_CONFIG_CONTENT": "{}"}):
                with patch.dict(os.environ, clear=False):
                    os.environ.pop("OPENCODE_CONFIG", None)
                    environment, secrets = prepare_home(home, "selected/model")
                imported = json.loads((home / ".config/opencode/opencode.json").read_text())
                self.assertEqual(set(imported["provider"]), {"selected"})
                self.assertEqual(imported["plugin"], [])
                self.assertEqual(imported["mcp"], {})
                self.assertNotIn("unrelated-secret", secrets)
                self.assertEqual(environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
                target = home / ".local/share/opencode/auth.json"
                refreshed = {"selected": {**original["selected"], "access": "new-access-token", "expires": 2,
                                          "injected": "not-accepted"}, "injected": {"type": "api", "key": "bad"}}
                target.write_text(encode(refreshed))
                sync_oauth(home, "selected/model", original)
                saved = json.loads(source.read_text())
                self.assertEqual(saved["other"], original["other"])
                self.assertEqual(saved["selected"]["access"], "new-access-token")
                self.assertNotIn("injected", saved)
                self.assertNotIn("injected", saved["selected"])
                self.assertEqual(source.stat().st_mode & 0o777, 0o600)
                saved["selected"]["access"] = "external-newer-token"
                source.write_text(encode(saved))
                sync_oauth(home, "selected/model", original)
                self.assertEqual(json.loads(source.read_text())["selected"]["access"], "external-newer-token")

    def test_cache_usage_is_not_counted_twice_and_missing_is_unknown(self):
        details = native_usage("responses", {"usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 70}}})
        self.assertEqual((details["tokens"], details["input_tokens"], details["cache_read_tokens"]), (120, 30, 70))
        self.assertIsNone(native_usage("responses", {}))
        self.assertEqual(opencode_usage([{"input": 10, "output": 5, "cache": {"read": 20, "write": 2}}])["tokens"], 37)
        # 网关可能返回 null 或字符串计数；无法确认时按未知处理，不否决成功调用。
        self.assertIsNone(native_usage("chat_completions", {"usage": {"prompt_tokens": None, "completion_tokens": 3, "total_tokens": None}}))
        self.assertIsNone(native_usage("anthropic", {"usage": {"input_tokens": None, "output_tokens": 1}}))
        self.assertIsNone(opencode_usage([{"input": None, "output": 3}]))
        tolerated = native_usage("chat_completions", {"usage": {"prompt_tokens": "12", "completion_tokens": "5", "total_tokens": "17"}})
        self.assertEqual(tolerated["tokens"], 17)
        self.assertEqual(tolerated["input_tokens"], 12)
        self.assertEqual(tolerated["output_tokens"], 5)

    def test_opencode_jsonl_variants_and_jsonc_urls(self):
        data, steps = parse_events('\n'.join([encode({"type": "step_finish", "part": {"id": "step", "tokens": {"input": 5}}}),
                                              encode({"type": "text", "part": {"text": '```json\n{"summary":"done"}\n```'}})]))
        self.assertEqual(data, {"summary": "done"})
        self.assertEqual(len(steps), 1)
        self.assertEqual(jsonc('{/*c*/"url":"https://x/a//b", // hi\n"items":[1,],}'), {"url": "https://x/a//b", "items": [1]})
        config = validate({"models": {"implementation": {"provider": "opencode", "model": "provider/model", "effort": "custom_variant"}}})
        self.assertEqual(config["models"]["implementation"]["effort"], "custom_variant")


@unittest.skipUnless(os.environ.get("RSI_DOCKER_TESTS") == "1", "Enable RSI_DOCKER_TESTS=1 with the worker image built")
class DockerExecutionTest(unittest.TestCase):
    setUp = ServerExecutionTest.setUp
    tearDown = ServerExecutionTest.tearDown
    def test_linux_native_pipeline_uses_real_isolated_checks(self):
        self.runtime.configure({"sandbox": {"backend": "docker", "image": "adaptive-rsi-worker:local"},
                                "checks": CHECKS, "sources": {"inspection": False, "issues": False},
                                "evolution": {"enabled": False}}, self.project)
        self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        row = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "awaiting_approval", row["result"])
        self.assertIn("return a - b", (self.repo / "calculator.py").read_text())

    def test_container_hides_controller_and_other_projects_and_enforces_readonly(self):
        config = validate({"sandbox": {"backend": "docker"}})
        secret = self.store.root / "hidden.txt"
        secret.write_text("hidden evaluation data")
        began = time.monotonic()
        with cli_home(self.repo) as home:
            script = ("from pathlib import Path; import os; "
                      "assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'; "
                      "assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'; "
                      "quota,period=map(int,Path('/sys/fs/cgroup/cpu.max').read_text().split()); assert quota == 2*period; "
                      "tmp=os.statvfs('/tmp'); assert tmp.f_blocks*tmp.f_frsize <= 256*1024*1024; "
                      "assert not Path(" + repr(str(secret)) + ").exists(); "
                      "assert not Path('/var/run/docker.sock').exists(); Path('calculator.py').write_text('forbidden')")
            result = run_cli(["python3", "-c", script], self.repo, home, config, lambda: time.monotonic() - began > 30,
                             read_only=True, state=self.store.root)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("Read-only file system", result["stderr"])
        self.assertIn("return a - b", (self.repo / "calculator.py").read_text())


if __name__ == "__main__":
    unittest.main()
