"""A/B/C 审计边界回归：临时仓库和协议响应，不调用付费模型。"""
import copy
import csv
import io
import json
import platform
import shutil
import sys
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from adaptive_rsi.auth_sessions import _Process
from adaptive_rsi.cli_worker import run_cli
from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.delivery import publish
from adaptive_rsi.gitops import changes, git, snapshot
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.native import Conversation, NativeAgent, post_json, tools_for
from adaptive_rsi.provider_health import ModelFailure, ProviderHealth, cli_failure, error_code
from adaptive_rsi.opencode import OpenCodeAgent, OpenCodeCapabilities, parse_events
from adaptive_rsi.process import Cancelled, ProcessError, Sandbox, checked, run_process
from adaptive_rsi.recovery import BudgetExhausted, FAILURE_ACTIONS, failure
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.usage import opencode_usage
from tests.test_runtime import FakeAgent, fixture


class CodexExecutableTest(unittest.TestCase):
    @unittest.skipUnless(platform.system() == "Darwin" and shutil.which("codex"), "installed macOS Codex")
    def test_symlinked_codex_can_start_itself_inside_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            executable = Path(shutil.which("codex")).resolve()
            alias = root / "codex"
            alias.symlink_to(executable)
            store = Store(root / "state")
            self.addCleanup(store.close)
            commands = []

            def local_process(argv, cwd, *args):
                commands.append(argv)
                profile = []
                for index, argument in enumerate(argv[:-1]):
                    if argument == "-c":
                        profile.extend([argument, argv[index + 1]])
                result = run_process([argv[0], "sandbox", "-P", "rsi", *profile,
                                      "-C", str(workspace), argv[0], "--version"], workspace, timeout=15)
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIn("codex-cli", result["stdout"])
                Path(argv[argv.index("--output-last-message") + 1]).write_text('{"summary":"local startup verified"}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": ""}

            original_which = shutil.which
            with patch("adaptive_rsi.agent.shutil.which", side_effect=lambda name: str(alias) if name == "codex" else original_which(name)), \
                    patch("adaptive_rsi.agent.run_process", side_effect=local_process):
                config = copy.deepcopy(DEFAULTS)
                config["models"]["probe"] = config["models"]["analysis"]
                result = CodexAgent(store).invoke("probe", "local startup only", workspace, "project", "run", config)
            self.assertEqual(commands[0][0], str(executable))
            self.assertEqual(result["summary"], "local startup verified")


class AuditRuntimeBoundariesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.add_project(self.repo, enabled=True)

    def task_state(self, task):
        return self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"]

    def test_start_preserves_interrupted_work_and_only_resumes_publish(self):
        historical = self.runtime.goal(self.project, "Previously stopped work")
        historical_run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(historical_run["id"], "interrupted", {"reason": "Project paused"}, True)
        active = self.runtime.goal(self.project, "Work interrupted by restart")
        active_run = self.store.claim(self.project, self.runtime.config(self.project))
        cancelled = self.runtime.goal(self.project, "Cancelled work")
        self.runtime.cancel_task(cancelled)
        approval = self.store.add_task(self.project, "system_publish", "publish:approved", "Publish approved work")
        self.store.execute("UPDATE tasks SET state='interrupted' WHERE id=?", (approval,))
        with patch.object(self.runtime, "loop"), patch.object(self.runtime.updates, "restore_startup"):
            self.runtime.start()
        self.assertEqual(self.task_state(historical), "interrupted")
        self.assertEqual(self.task_state(active), "interrupted")
        self.assertEqual(self.task_state(cancelled), "cancelled")
        self.assertEqual(self.task_state(approval), "queued")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (active_run["id"],))["state"], "interrupted")
        self.runtime.retry(active)
        self.assertEqual(self.task_state(active), "queued")

    def test_setup_output_cannot_trigger_provider_budget_or_infrastructure_retry(self):
        for index, message in enumerate(("timeout connection failed", "quota exceeded unauthorized HTTP 429")):
            with self.subTest(message=message):
                self.runtime.configure({"environment": {"setup": [{"name": "setup", "argv": ["false"], "cwd": "."}]}}, self.project)
                task = self.runtime.goal(self.project, "Setup failure " + str(index))
                run = self.store.claim(self.project, self.runtime.config(self.project))
                result = {"exit_code": 1, "error": None, "stderr": message, "stdout": "", "error_kind": "exit"}
                with patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
                        patch("adaptive_rsi.process.Sandbox.check", return_value=result):
                    self.runtime.process(run)
                failed = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
                self.assertEqual(failed["state"], "blocked")
                self.assertEqual(json.loads(failed["result"])["failure"]["code"], "execution")
                row = self.store.one("SELECT state,infra_retries,budget_waits FROM tasks WHERE id=?", (task,))
                self.assertEqual(row, {"state": "blocked", "infra_retries": 0, "budget_waits": 0})
                self.assertFalse(self.store.query("SELECT * FROM settings WHERE key LIKE 'provider-health:%'"))

    def prepare_publish(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"},
                                "delivery": {"base_branch": "main"}}, self.project)
        config = self.runtime.config(self.project)
        task_id = self.runtime.goal(self.project, "Verified change")
        run = self.store.claim(self.project, config)
        base = git(self.repo, "rev-parse", "HEAD")
        workspace = snapshot(self.repo, self.root / "candidate")
        (workspace / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        changes(workspace)
        outcome = {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}
        self.store.execute("UPDATE runs SET base=? WHERE id=?", (base, run["id"]))
        self.store.transition(run["id"], "awaiting_approval", outcome, True)
        self.store.put_setting("run-baseline:" + run["id"],
                               {"repository": "example/project", "branch": "main", "sha": base})
        approval = self.runtime.deliver(run["id"])
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (approval,))
        return (self.runtime.project(self.project), self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,)),
                self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],)), workspace, config)

    def test_publish_preserves_existing_branch_at_a_different_commit(self):
        args = self.prepare_publish()
        workspace, run = args[3], args[2]
        branch = "feat/rsi-" + run["id"]
        git(workspace, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "candidate")
        head = git(workspace, "rev-parse", "HEAD")
        git(workspace, "branch", branch, run["base"])
        with patch("adaptive_rsi.delivery.baseline", return_value=("unused", run["base"], "main")), \
                patch("adaptive_rsi.delivery.checked") as external, \
                patch("adaptive_rsi.delivery.github_git") as push:
            with self.assertRaisesRegex(RuntimeError, "branch"):
                publish(self.runtime, *args)
        self.assertEqual(git(workspace, "rev-parse", branch), run["base"])
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), head)
        external.assert_not_called()
        push.assert_not_called()

    def test_publish_retries_after_push_and_reuses_branch_without_reset(self):
        args = self.prepare_publish()
        workspace, run = args[3], args[2]
        branch = "feat/rsi-" + run["id"]
        url = "https://github.com/example/project/pull/1"
        with patch("adaptive_rsi.delivery.baseline", return_value=("unused", run["base"], "main")), \
                patch("adaptive_rsi.delivery.checked", side_effect=["[]", RuntimeError("PR create failed"), "[]", url]), \
                patch("adaptive_rsi.delivery.github_git") as push:
            with self.assertRaisesRegex(RuntimeError, "PR create failed"):
                publish(self.runtime, *args)
            head = git(workspace, "rev-parse", branch)
            # 模拟重试前切到同一已验证提交的 detached HEAD。
            git(workspace, "checkout", "--detach", head)
            self.assertEqual(publish(self.runtime, *args), url)
        self.assertEqual(git(workspace, "rev-parse", branch), head)
        self.assertEqual(git(workspace, "branch", "--show-current"), branch)
        self.assertEqual(push.call_count, 2)


class AuditUsageBoundariesTest(unittest.TestCase):
    def test_opencode_keeps_equal_usage_for_distinct_idless_steps(self):
        tokens = {"input": 8, "output": 2, "cache": {"read": 3}}
        events = [{"type": "step_finish", "part": {"tokens": tokens}, "timestamp": stamp}
                  for stamp in (1000, 2000)]
        events.append({"type": "text", "part": {"text": '{"summary":"done"}'}})
        data, steps = parse_events("\n".join(map(json.dumps, events)))
        self.assertEqual(data, {"summary": "done"})
        self.assertEqual(opencode_usage(steps)["tokens"], 26)

    def test_opencode_deduplicates_only_identified_steps_and_keeps_failure_usage(self):
        tokens = {"input": 8, "output": 2}
        identified = {"type": "step_finish", "part": {"id": "step1", "tokens": tokens}}
        anonymous = {"type": "step_finish", "part": {"tokens": tokens}}
        events = [identified, identified, anonymous, anonymous, {"type": "error"}]
        data, steps = parse_events("\n".join(map(json.dumps, events)), strict=False)
        self.assertIsNone(data)
        self.assertEqual(opencode_usage(steps)["tokens"], 30)

    @staticmethod
    def finish_response(protocol, usage):
        args = {"summary": "RSI_OK"}
        if protocol == "responses":
            return {"output": [{"type": "function_call", "call_id": "1", "name": "finish", "arguments": json.dumps(args)}], "usage": usage}
        if protocol in ("chat_completions", "ollama"):
            message = {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "finish", "arguments": json.dumps(args)}}]}
            return {"message": message, "usage": usage} if protocol == "ollama" else {"choices": [{"message": message}], "usage": usage}
        if protocol == "anthropic":
            return {"content": [{"type": "tool_use", "id": "1", "name": "finish", "input": args}], "usage": usage}
        return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "finish", "args": args}}]}}], "usageMetadata": usage}

    def test_native_null_usage_completes_call_and_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            self.addCleanup(store.close)
            workspace = root / "workspace"
            workspace.mkdir()
            for protocol in ("responses", "chat_completions", "anthropic", "gemini", "ollama"):
                with self.subTest(protocol=protocol):
                    config = copy.deepcopy(DEFAULTS)
                    provider = {"id": "native", "protocol": protocol, "base_url": "http://127.0.0.1:9", "credential": "none", "env_key": ""}
                    route = {"provider": "native", "model": "test", "effort": "default"}
                    response = self.finish_response(protocol, None)
                    with patch("adaptive_rsi.native.post_json", return_value=response):
                        result = NativeAgent(store).invoke("probe", "test", workspace, protocol, protocol, config, route, provider)
                    self.assertEqual(result, {"summary": "RSI_OK"})
                    call = store.one("SELECT state,usage_known FROM calls WHERE project=?", (protocol,))
                    self.assertEqual(call, {"state": "completed", "usage_known": 0})

    def test_cancelled_cli_calls_keep_completed_usage_and_redacted_events(self):
        secret = "audit-cancellation-private-secret"
        for backend in ("Codex", "OpenCode"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                store = Store(root / "state")
                self.addCleanup(store.close)
                ready = root / "usage-ready"
                event = ({"type": "turn.completed", "usage": {"input_tokens": 40, "output_tokens": 10}} if backend == "Codex" else
                         {"type": "step_finish", "part": {"id": "step", "tokens": {"input": 40, "output": 10}}})
                script = ("import pathlib,time; print(" + repr(json.dumps(event)) + ",flush=True); "
                          "print(" + repr(secret) + ",flush=True); pathlib.Path(" + repr(str(ready)) + ").touch(); time.sleep(10)")
                def execute(*args, **kwargs):
                    return run_process([sys.executable, "-c", script], timeout=5, cancel=ready.exists)
                config = copy.deepcopy(DEFAULTS)
                route = {"provider": "openai" if backend == "Codex" else "opencode", "model": "test", "effort": "default"}
                if backend == "Codex":
                    route["provider"] = "private"
                    config["providers"] = [{"id": "private", "name": "Private", "protocol": "codex",
                                             "base_url": "https://fixture.invalid", "credential": "session",
                                             "secret_ref": "fixture", "env_key": ""}]
                    config["models"]["probe"] = route
                    with patch("adaptive_rsi.credentials.Credentials.get", return_value=secret), \
                            patch("adaptive_rsi.agent.platform.system", return_value="Darwin"), \
                            patch("adaptive_rsi.agent.shutil.which", return_value="/fake/codex"), \
                            patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=[]), \
                            patch("adaptive_rsi.agent.run_process", side_effect=execute):
                        with self.assertRaises(Cancelled) as raised:
                            CodexAgent(store).invoke("probe", "test", workspace, "p", "cancel", config)
                else:
                    config["models"]["implementation"] = route
                    with patch("adaptive_rsi.opencode.platform.system", return_value="Darwin"), \
                            patch("adaptive_rsi.opencode.shutil.which", return_value="/fake/opencode"), \
                            patch("adaptive_rsi.opencode.detect_opencode", return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))), \
                            patch("adaptive_rsi.opencode._load_auth", return_value={}), \
                            patch("adaptive_rsi.opencode.prepare_home", return_value=({}, [secret])), \
                            patch("adaptive_rsi.opencode.sync_oauth", return_value=[]), \
                            patch("adaptive_rsi.opencode.run_cli", side_effect=execute):
                        with self.assertRaises(Cancelled) as raised:
                            OpenCodeAgent(store).invoke("probe", "test", workspace, "p", "cancel", config, route)
                self.assertEqual(raised.exception.result["error_kind"], "cancelled")
                self.assertEqual(store.one("SELECT state,tokens,usage_known FROM calls"),
                                 {"state": "failed", "tokens": 50, "usage_known": 1})
                events = list(workspace.glob(".rsi-agent/*/events.jsonl"))
                self.assertEqual(len(events), 1)
                self.assertIn(json.dumps(event), events[0].read_text())
                self.assertNotIn(secret, events[0].read_text())
                self.assertNotIn(secret, raised.exception.result["stdout"])
                self.assertFalse(store.query("SELECT * FROM events WHERE kind='fallback'"))
                self.assertFalse(store.query("SELECT * FROM settings WHERE key LIKE 'provider-health:%'"))

    def test_gemini_blocked_response_preserves_billable_usage_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            self.addCleanup(store.close)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = {"id": "native", "protocol": "gemini", "base_url": "http://127.0.0.1:9", "credential": "none", "env_key": ""}
            response = {"candidates": [], "usageMetadata": {"totalTokenCount": 37}}
            with patch("adaptive_rsi.native.post_json", return_value=response), \
                    patch.object(store, "record_usage", wraps=store.record_usage) as record:
                with self.assertRaisesRegex(RuntimeError, "no candidates"):
                    NativeAgent(store).invoke("probe", "test", workspace, "p", "blocked", copy.deepcopy(DEFAULTS),
                                              {"model": "test", "effort": "default"}, provider)
            record.assert_called_once()
            self.assertEqual(store.one("SELECT state,tokens,usage_known FROM calls"),
                             {"state": "failed", "tokens": 37, "usage_known": 1})

    def test_native_anthropic_counts_cache_read_input_tokens(self):
        response = self.finish_response("anthropic", {"input_tokens": 10, "output_tokens": 5,
                                                       "cache_creation_input_tokens": 4, "cache_read_input_tokens": 7})
        _, tokens = Conversation("anthropic", "test", "test", tools_for("probe"), "default").receive(response)
        self.assertEqual(tokens, 26)


class AuditProcessBoundariesTest(unittest.TestCase):
    def test_completed_commands_do_not_wait_for_a_fixed_polling_sleep(self):
        process = Mock(returncode=0)
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        with patch("adaptive_rsi.process.subprocess.Popen", return_value=process), \
                patch("adaptive_rsi.process.os.killpg"), \
                patch("adaptive_rsi.process.time.sleep", side_effect=AssertionError("Completed commands must wake the waiter")):
            result = run_process(["completed-command"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])

    @staticmethod
    def mounts(argv):
        return [next(csv.reader(io.StringIO(argv[i + 1]))) for i, value in enumerate(argv) if value == "--mount"]

    def test_docker_sandbox_mounts_round_trip_full_csv_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ('plain', 'comma,readonly', 'quote"comma,', 'line\nbreak,'):
                with self.subTest(name=name):
                    workspace = root / name
                    workspace.mkdir()
                    protected = workspace / 'protected,"path'
                    protected.write_text("sealed")
                    sandbox = Sandbox(root / "state", copy.deepcopy(DEFAULTS))
                    with patch.object(sandbox, "backend", return_value="docker"), \
                            patch("adaptive_rsi.process.shutil.which", return_value="/fake/docker"), \
                            patch("adaptive_rsi.process._cached_probe", return_value={"exit_code": 0}):
                        argv, _ = sandbox.command(["true"], workspace, protected=[protected])
                    self.assertEqual(self.mounts(argv), [
                        ["type=bind", "src=" + str(workspace), "dst=/workspace"],
                        ["type=bind", "src=" + str(protected), 'dst=/workspace/protected,"path', "readonly"]])

    def test_cli_worker_mounts_round_trip_csv_and_preserve_role_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ('plain', 'comma,readonly', 'quote"comma,', 'line\nbreak,'):
                for read_only in (False, True):
                    with self.subTest(name=name, read_only=read_only):
                        workspace = root / name
                        metadata = workspace / ".git"
                        metadata.mkdir(parents=True, exist_ok=True)
                        internal = workspace / ".rsi-agent"
                        home = internal / "home"
                        home.mkdir(parents=True, exist_ok=True)
                        config = copy.deepcopy(DEFAULTS)
                        config["sandbox"]["backend"] = "docker"
                        result = {"exit_code": 0, "error": None, "stdout": "done"}
                        with patch("adaptive_rsi.cli_worker.run_process", return_value=result) as run:
                            self.assertIs(run_cli(["/host/bin/opencode", "run"], workspace, home,
                                                  config, read_only=read_only, timeout=42), result)
                        calls = run.call_args_list
                        self.assertEqual(calls[0].args[0],
                                         ["docker", "image", "inspect", config["sandbox"]["worker_image"]])
                        argv = calls[1].args[0]
                        expected = [["type=bind", "src=" + str(workspace), "dst=" + str(workspace)] +
                                    (["readonly"] if read_only else [])]
                        if read_only:
                            expected.append(["type=bind", "src=" + str(internal), "dst=" + str(internal)])
                        expected.append(["type=bind", "src=" + str(metadata), "dst=" + str(metadata), "readonly"])
                        self.assertEqual(self.mounts(argv), expected)
                        self.assertEqual(argv[argv.index("-w") + 1], str(workspace))
                        self.assertEqual(argv[-3:], [config["sandbox"]["worker_image"], "opencode", "run"])
                        self.assertEqual(calls[1].kwargs["timeout"], 42)
                        self.assertEqual(calls[2].args[0],
                                         ["docker", "rm", "--force", argv[argv.index("--name") + 1]])

    def test_auth_docker_inherits_secret_environment_without_argv_values(self):
        with patch("adaptive_rsi.auth_sessions.shutil.which", return_value="/fake/docker"):
            worker = _Process("opencode", {"sandbox": {"backend": "docker"}})
        self.addCleanup(worker.close)
        worker.image = "worker:test"
        worker.environment["OPENCODE_SERVER_PASSWORD"] = "audit-test-private-password"
        worker.home = worker.home / 'auth,"directory'
        worker.home.mkdir()
        process = Mock()
        process.poll.return_value = 0
        process.stdin = process.stdout = None
        with patch("adaptive_rsi.auth_sessions.shutil.which", return_value="/fake/docker"), \
                patch("adaptive_rsi.auth_sessions.subprocess.Popen", return_value=process) as popen:
            worker.launch(["opencode", "serve"])
        argv = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        env_flags = [argv[i + 1] for i, value in enumerate(argv) if value == "--env"]
        self.assertIn("OPENCODE_SERVER_PASSWORD", env_flags)
        self.assertNotIn("audit-test-private-password", " ".join(argv))
        self.assertEqual(environment["OPENCODE_SERVER_PASSWORD"], "audit-test-private-password")
        self.assertEqual(self.mounts(argv), [["type=bind", "src=" + str(worker.home), "dst=" + str(worker.home)]])
        # 测试没有启动真实进程或容器；仅关闭管道与临时目录。
        worker.process = None


class AuditFailureBoundariesTest(unittest.TestCase):
    def test_local_exit_timeout_and_output_limit_have_typed_execution_semantics(self):
        noise = "timeout connection quota exceeded unauthorized HTTP 429"
        commands = [
            ([sys.executable, "-c", "import sys; sys.stderr.write(" + repr(noise) + "); sys.exit(1)"], {}, "exit"),
            ([sys.executable, "-c", "import time; time.sleep(5)"], {"timeout": 0.1}, "timeout"),
            ([sys.executable, "-c", "import time; print('x'*2000000, flush=True); time.sleep(5)"], {"output_mb": 1}, "output_limit"),
        ]
        for argv, options, kind in commands:
            with self.subTest(kind=kind):
                result = run_process(argv, **options)
                self.assertEqual(result["error_kind"], kind)
                problem = ProcessError(result)
                self.assertEqual(failure(problem)["code"], "execution")
                self.assertEqual(error_code(problem), "process_" + kind)
        with self.assertRaises(ProcessError) as raised:
            checked(commands[0][0])
        self.assertEqual(failure(raised.exception)["code"], "execution")

    def test_cli_adapter_uses_limits_and_error_events_but_ignores_project_stdout(self):
        noise = "timeout connection quota exceeded unauthorized HTTP 429"
        for backend in ("Codex", "OpenCode"):
            with self.subTest(backend=backend):
                result = {"exit_code": 1, "error": None, "stderr": "", "stdout": "\n".join([
                    noise, json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": noise}}),
                    json.dumps({"type": "text", "part": {"text": noise}})])}
                problem = cli_failure(backend, result)
                self.assertEqual(failure(problem)["code"], "execution")
                self.assertEqual(error_code(problem), "execution")
                for control, kind, code in [("Command timed out", "timeout", "timeout"),
                                             ("Command output limit exceeded", "output_limit", "execution")]:
                    problem = cli_failure(backend, {**result, "error": control, "error_kind": kind, "stderr": noise})
                    self.assertEqual(error_code(problem), code)
                event = {"type": "error", "error": {"data": {"statusCode": "503", "message": "private quota exceeded"}}}
                problem = cli_failure(backend, {**result, "stdout": json.dumps(event), "stderr": noise})
                self.assertEqual(error_code(problem), "connection")
                self.assertEqual(failure(problem)["code"], "provider")
                self.assertNotIn("private quota", str(problem))
                problem = cli_failure(backend, {**result, "stdout": json.dumps({"type": "turn.failed", "error": {"message": "rate limit exceeded"}})})
                self.assertEqual(error_code(problem), "rate_limit")
                problem = cli_failure(backend, {**result, "stdout": json.dumps({"type": "error", "message": "rate limit exceeded"})})
                self.assertEqual(error_code(problem), "rate_limit")
                for text, code in [("rate limit exceeded", "rate_limit"), ("connection failed", "connection"),
                                   ("request timed out", "timeout"), ("quota exceeded", "budget_exhausted"),
                                   ("unauthorized HTTP 401", "auth"), ("model not found", "model_missing")]:
                    self.assertEqual(error_code(cli_failure(backend, {**result, "stderr": text})), code)

    def test_native_http_status_and_transport_types_override_exception_text(self):
        for status, code in [(401, "auth"), (429, "rate_limit"), (503, "connection"), (504, "timeout")]:
            with self.subTest(status=status), patch("adaptive_rsi.native.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError("http://fixture", status, "quota exceeded timeout", {}, None)
                with self.assertRaises(RuntimeError) as raised:
                    post_json("http://fixture", {}, {}, 1, 1024)
                self.assertEqual(error_code(raised.exception), code)
        with patch("adaptive_rsi.native.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.URLError("quota exceeded")
            with self.assertRaises(RuntimeError) as raised:
                post_json("http://fixture", {}, {}, 1, 1024)
            self.assertEqual(error_code(raised.exception), "connection")

    def test_known_codes_control_fallback_and_circuit_even_with_conflicting_text(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state")
            self.addCleanup(store.close)
            config = copy.deepcopy(DEFAULTS)
            config["models"]["probe"] = {"provider": "openai", "model": "primary", "effort": "default",
                                          "fallback": {"provider": "opencode", "model": "fallback", "effort": "default"}}
            agent = CodexAgent(store)
            for code in ("execution", "auth", "budget_exhausted", "connection", "timeout", "model_missing"):
                with self.subTest(code=code):
                    problem = ModelFailure("Codex call failed: timeout connection quota exceeded HTTP 429", code)
                    health = ProviderHealth(store)
                    with patch.object(agent, "_invoke", side_effect=[problem, {"summary": "fallback"}]) as invoke:
                        if code in {"connection", "timeout", "model_missing"}:
                            self.assertEqual(agent.invoke("probe", "test", directory, code, code, config), {"summary": "fallback"})
                            self.assertEqual(invoke.call_count, 2)
                        else:
                            with self.assertRaises(ModelFailure):
                                agent.invoke("probe", "test", directory, code, code, config)
                            invoke.assert_called_once()
                    circuit = health.status(config, "openai", code, "primary")
                    self.assertEqual(circuit["failures"], int(code in {"auth", "connection", "timeout"}))

    def test_known_structured_failure_overrides_conflicting_text(self):
        for code, action in FAILURE_ACTIONS.items():
            with self.subTest(code=code):
                exc = RuntimeError("quota exceeded; unauthorized 401; sandbox connection timeout")
                exc.failure_code = code
                self.assertEqual(failure(exc), {"code": code, "action": action, "detail": str(exc)})

    def test_failure_type_and_text_remain_available_without_known_code(self):
        self.assertEqual(failure(BudgetExhausted("output limit"))["code"], "budget")
        exc = RuntimeError("quota exceeded")
        exc.failure_code = "future_code"
        self.assertEqual(failure(exc)["code"], "budget")
        self.assertEqual(failure(RuntimeError("unauthorized 401"))["code"], "credentials")
