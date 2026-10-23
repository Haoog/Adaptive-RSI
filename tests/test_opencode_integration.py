"""真实 OpenCode CLI + 本地协议替身；不访问收费模型。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.auth_sessions import OpenCodeAuth
from adaptive_rsi.config import validate
from adaptive_rsi.opencode import OpenCodeAgent, OpenCodeCapabilities, auth_providers, merge_auth_export, remove_auth_export
from adaptive_rsi.store import Store


def version_fixture(directory, version):
    script = Path(directory) / "opencode"
    script.write_text("#!" + sys.executable + "\n" + '''import sys
if sys.argv[1:] == ["--version"]:
    print("opencode v" + %r)
    raise SystemExit(0)
if sys.argv[1:3] == ["auth", "export"]:
    print("[]")
    raise SystemExit(0)
raise SystemExit(3)
''' % version)
    script.chmod(0o700)
    return script


def modern_auth_fixture(directory, version="2.1.3"):
    script = Path(directory) / "opencode-modern-host"
    script.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["XDG_DATA_HOME"]) / "opencode"
path = root / "auth-export.json"
if args == ["--version"]:
    print("opencode v" + %r)
    raise SystemExit(0)
def rows():
    return json.loads(path.read_text()) if path.is_file() else []
if args[:2] == ["auth", "export"]:
    target = args[3] if len(args) > 3 else None
    print(json.dumps([item for item in rows() if not target or item["integrationID"] == target]))
    raise SystemExit(0)
if args[:2] == ["auth", "import"]:
    incoming = json.loads(Path(args[3]).read_text())
    current = {item["integrationID"]: item for item in rows()}
    current.update({item["integrationID"]: item for item in incoming})
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(current.values())))
    path.chmod(0o600)
    raise SystemExit(0)
raise SystemExit(3)
''' % version)
    script.chmod(0o700)
    return script


class OpenCodeCommandCompatibilityTest(unittest.TestCase):
    def invoke(self, version, effort="high"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            script = version_fixture(root, version)
            store = Store(root / "state")
            config = validate({"models": {"implementation": {
                "provider": "opencode", "model": "provider/model", "effort": effort}}})
            captured = []
            result = {
                "exit_code": 0, "error": None, "stderr": "",
                "stdout": "\n".join([
                    json.dumps({"type": "step_finish", "part": {"id": "step", "tokens": {"input": 2, "output": 1}}}),
                    json.dumps({"type": "text", "part": {"text": '{"summary":"ok"}'}}),
                ]),
            }

            def run(argv, *args, **kwargs):
                captured.append(argv)
                return result

            env = {"XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
                   "XDG_CACHE_HOME": str(root / "cache")}
            with patch.dict(os.environ, env, clear=False), patch(
                    "adaptive_rsi.opencode.shutil.which",
                    side_effect=lambda name: str(script) if name == "opencode" else None), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run):
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
            return value, captured[0]

    def test_v1_and_v2_run_commands_use_version_specific_variant_syntax(self):
        value, legacy = self.invoke("1.19.3")
        self.assertEqual(value, {"summary": "ok"})
        self.assertIn("--pure", legacy)
        self.assertNotIn("--standalone", legacy)
        self.assertEqual(legacy[legacy.index("--variant") + 1], "high")
        self.assertEqual(legacy[legacy.index("--model") + 1], "provider/model")

        value, modern = self.invoke("2.1.3")
        self.assertEqual(value, {"summary": "ok"})
        self.assertNotIn("--pure", modern)
        self.assertNotIn("--variant", modern)
        self.assertIn("--standalone", modern)
        self.assertEqual(modern[modern.index("--model") + 1], "provider/model#high")

    def test_run_failure_redacts_provider_secret_from_retained_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            script = version_fixture(root, "2.1.3")
            store = Store(root / "state")
            config = validate({"models": {"implementation": {
                "provider": "opencode", "model": "selected/model", "effort": "default"}}})
            content = json.dumps({"provider": {"selected": {"options": {"apiKey": "private-key-value"}}}})
            with patch.dict(os.environ, {
                    "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
                    "XDG_CACHE_HOME": str(root / "cache"), "OPENCODE_CONFIG_CONTENT": content}, clear=False), patch(
                    "adaptive_rsi.opencode.shutil.which",
                    side_effect=lambda name: str(script) if name == "opencode" else None), patch(
                    "adaptive_rsi.opencode.run_cli", return_value={
                        "exit_code": 1, "error": None, "stderr": "private-key-value",
                        "stdout": '{"type":"text","part":{"text":"private-key-value"}}\n'}):
                with self.assertRaisesRegex(RuntimeError, "redacted execution record"):
                    OpenCodeAgent(store).invoke(
                        "probe", "Return a valid result", workspace, "p", "run", config,
                        config["models"]["implementation"])
            events = list(workspace.glob(".rsi-agent/*/events.jsonl"))
            self.assertEqual(len(events), 1)
            self.assertNotIn("private-key-value", events[0].read_text())

    def test_linux_container_probes_worker_cli_and_runs_inside_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = Store(root / "state")
            config = validate({
                "sandbox": {"backend": "docker", "worker_image": "fixture-worker"},
                "models": {"implementation": {
                    "provider": "opencode", "model": "provider/model", "effort": "high"}},
            })
            captured = []
            capabilities = OpenCodeCapabilities(version="1.19.3", release=(1, 19, 3))
            result = {
                "exit_code": 0, "error": None, "stderr": "",
                "stdout": "\n".join([
                    json.dumps({"type": "step_finish", "part": {"id": "step", "tokens": {"input": 1, "output": 1}}}),
                    json.dumps({"type": "text", "part": {"text": '{"summary":"container"}'}}),
                ]),
            }

            def which(name):
                return "/usr/bin/docker" if name == "docker" else None

            def run(argv, *args, **kwargs):
                captured.append(argv)
                return result

            with patch("adaptive_rsi.opencode.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.opencode.shutil.which", side_effect=which), patch(
                    "adaptive_rsi.opencode.detect_opencode", return_value=capabilities) as detect, patch(
                    "adaptive_rsi.opencode.run_process", return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run):
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
            self.assertEqual(value, {"summary": "container"})
            self.assertEqual(detect.call_args.kwargs["argv"], [
                "/usr/bin/docker", "run", "--rm", "fixture-worker", "opencode", "--version"])
            self.assertEqual(captured[0][0], "opencode")
            self.assertIn("--pure", captured[0])

    def test_host_and_worker_versions_are_resolved_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            script = version_fixture(root, "2.1.3")
            store = Store(root / "state")
            config = validate({
                "sandbox": {"backend": "docker", "worker_image": "fixture-worker"},
                "models": {"implementation": {
                    "provider": "opencode", "model": "provider/model", "effort": "high"}},
            })
            captured = []
            auth_seen = []
            host = OpenCodeCapabilities(version="2.1.3", release=(2, 1, 3))
            worker = OpenCodeCapabilities(version="1.19.3", release=(1, 19, 3))
            result = {"exit_code": 0, "error": None, "stderr": "",
                      "stdout": "\n".join([
                          json.dumps({"type": "text", "part": {"text": '{"summary":"worker"}'}}),
                      ])}
            def which(name):
                return str(script) if name == "opencode" else "/usr/bin/docker" if name == "docker" else None
            def detect(binary="opencode", argv=None, env=None):
                return worker if argv else host
            def run_cli(argv, *args, **kwargs):
                if argv[0] == "opencode" and "--pure" in argv:
                    auth_seen.append((Path(args[1]) / ".local/share/opencode/auth.json").read_text())
                captured.append(argv)
                return result
            with patch("adaptive_rsi.opencode.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.opencode.shutil.which", side_effect=which), patch(
                    "adaptive_rsi.opencode.detect_opencode", side_effect=detect), patch(
                    "adaptive_rsi.opencode.run_process", return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.opencode.export_auth", return_value=[
                        {"integrationID": "provider", "value": {"type": "oauth", "refresh": "host-refresh"}}]), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run_cli):
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
            self.assertEqual(value, {"summary": "worker"})
            self.assertEqual(captured[0][0], "opencode")
            self.assertIn("--pure", captured[0])
            self.assertNotIn("--standalone", captured[0])
            self.assertIn("host-refresh", auth_seen[0])

    def test_host_v2_worker_v1_oauth_refresh_is_imported_into_host_v2_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            host = modern_auth_fixture(root)
            data = root / "data/opencode"
            data.mkdir(parents=True)
            export_path = data / "auth-export.json"
            original = [{"id": "credential-1", "integrationID": "provider", "label": "Provider",
                         "active": True, "value": {"type": "oauth", "refresh": "old-refresh",
                                                   "access": "old-access", "expires": 1}}]
            export_path.write_text(json.dumps(original))
            store = Store(root / "state")
            config = validate({
                "sandbox": {"backend": "docker", "worker_image": "fixture-worker"},
                "models": {"implementation": {
                    "provider": "opencode", "model": "provider/model", "effort": "default"}},
            })
            worker_capabilities = OpenCodeCapabilities("1.19.3", (1, 19, 3))
            host_capabilities = OpenCodeCapabilities("2.1.3", (2, 1, 3))
            calls = []
            result = {"exit_code": 0, "error": None, "stderr": "",
                      "stdout": '{"type":"text","part":{"text":"{\\"summary\\":\\"ok\\"}"}}\n'}
            real_run_process = __import__("adaptive_rsi.process", fromlist=["run_process"]).run_process

            def detect(binary="opencode", argv=None, env=None):
                return worker_capabilities if argv else host_capabilities

            def process(argv, *args, **kwargs):
                if argv[:3] == ["/usr/bin/docker", "image", "inspect"]:
                    return {"exit_code": 0, "error": None}
                return real_run_process(argv, *args, **kwargs)

            def run_cli(argv, *args, **kwargs):
                calls.append(argv)
                home = Path(args[1])
                auth = home / ".local/share/opencode/auth.json"
                if "--pure" in argv:
                    value = json.loads(auth.read_text())
                    value["provider"]["refresh"] = "new-refresh"
                    value["provider"]["access"] = "new-access"
                    value["provider"]["expires"] = 2
                    auth.write_text(json.dumps(value))
                return result

            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data"),
                                         "XDG_CONFIG_HOME": str(root / "config")}), patch(
                    "adaptive_rsi.opencode.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.opencode.shutil.which",
                    side_effect=lambda name: str(host) if name == "opencode" else
                    "/usr/bin/docker" if name == "docker" else None), patch(
                    "adaptive_rsi.opencode.detect_opencode", side_effect=detect), patch(
                    "adaptive_rsi.opencode.run_process", side_effect=process), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run_cli):
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
            self.assertEqual(value, {"summary": "ok"})
            self.assertIn("--pure", calls[0])
            saved = json.loads(export_path.read_text())[0]
            self.assertEqual(saved["id"], "credential-1")
            self.assertEqual(saved["value"]["refresh"], "new-refresh")
            self.assertEqual(saved["value"]["access"], "new-access")
            self.assertFalse((root / "data/opencode/auth.json").exists())

    def test_host_v1_worker_v2_refresh_writes_auth_json_without_host_v2_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            host = version_fixture(root, "1.19.3")
            data = root / "data/opencode"
            data.mkdir(parents=True)
            auth_path = data / "auth.json"
            auth_path.write_text(json.dumps({"other": {"type": "api", "key": "other-key"}}))
            store = Store(root / "state")
            config = validate({
                "sandbox": {"backend": "docker", "worker_image": "fixture-worker"},
                "models": {"implementation": {
                    "provider": "opencode", "model": "provider/model", "effort": "default"}},
            })
            worker_capabilities = OpenCodeCapabilities("2.1.3", (2, 1, 3))
            host_capabilities = OpenCodeCapabilities("1.19.3", (1, 19, 3))
            calls = []
            result = {"exit_code": 0, "error": None, "stderr": "",
                      "stdout": '{"type":"text","part":{"text":"{\\"summary\\":\\"ok\\"}"}}\n'}

            def detect(binary="opencode", argv=None, env=None):
                return worker_capabilities if argv else host_capabilities

            def run_cli(argv, *args, **kwargs):
                calls.append(argv)
                home = Path(args[1])
                credential = home / ".local/share/opencode/auth-export.json"
                if argv[:3] == ["opencode", "auth", "import"]:
                    credential.parent.mkdir(parents=True, exist_ok=True)
                    credential.write_text(Path(argv[4]).read_text())
                elif argv[:2] == ["opencode", "run"]:
                    rows = json.loads(credential.read_text())
                    rows[0]["value"].update(refresh="new-refresh", access="new-access", expires=2)
                    credential.write_text(json.dumps(rows))
                elif argv[:3] == ["opencode", "auth", "export"]:
                    rows = json.loads(credential.read_text())
                    return {**result, "stdout": json.dumps(rows) + "\n"}
                return dict(result)

            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data"),
                                         "XDG_CONFIG_HOME": str(root / "config")}), patch(
                    "adaptive_rsi.opencode.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.opencode.shutil.which",
                    side_effect=lambda name: str(host) if name == "opencode" else
                    "/usr/bin/docker" if name == "docker" else None), patch(
                    "adaptive_rsi.opencode.detect_opencode", side_effect=detect), patch(
                    "adaptive_rsi.opencode.run_process",
                    return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.opencode.export_auth",
                    side_effect=AssertionError("host v2 export must not be used")), patch(
                    "adaptive_rsi.opencode.import_auth",
                    side_effect=AssertionError("host v2 import must not be used")), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run_cli), patch(
                    "adaptive_rsi.auth_sessions.shutil.which", return_value=str(host)), patch(
                    "adaptive_rsi.auth_sessions.detect_opencode", return_value=host_capabilities):
                installer = object.__new__(OpenCodeAuth)
                installer.home = root / "auth-home"
                installer.install_credential("provider", [{
                    "id": "credential", "integrationID": "provider", "label": "Provider", "active": True,
                    "value": {"type": "oauth", "refresh": "old-refresh", "access": "old-access", "expires": 1}}])
                self.assertEqual(auth_providers(str(host), host_capabilities), ["other", "provider"])
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
            self.assertEqual(value, {"summary": "ok"})
            self.assertEqual([call[:3] for call in calls], [
                ["opencode", "auth", "import"], ["opencode", "run", "--standalone"],
                ["opencode", "auth", "export"]])
            saved = json.loads(auth_path.read_text())["provider"]
            self.assertEqual(saved["refresh"], "new-refresh")
            self.assertEqual(saved["access"], "new-access")
            self.assertEqual(json.loads(auth_path.read_text())["other"]["key"], "other-key")
            self.assertFalse((data / "auth-export.json").exists())

    def test_no_host_v2_credentials_are_persisted_for_worker_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = Store(root / "state")
            config = validate({
                "sandbox": {"backend": "docker", "worker_image": "fixture-worker"},
                "models": {"implementation": {
                    "provider": "opencode", "model": "provider/model", "effort": "high"}},
            })
            credentials = [{"integrationID": "provider", "id": "credential",
                            "value": {"type": "key", "key": "private-key"}}]
            captured = []
            result = {"exit_code": 0, "error": None, "stderr": "",
                      "stdout": '{"type":"text","part":{"text":"{\\"summary\\":\\"ok\\"}"}}\n'}
            def run(argv, *args, **kwargs):
                captured.append(argv)
                return result
            env = {"XDG_DATA_HOME": str(root / "data")}
            with patch.dict(os.environ, env, clear=False), patch(
                    "adaptive_rsi.opencode.platform.system", return_value="Linux"), patch(
                    "adaptive_rsi.opencode.shutil.which", side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None), patch(
                    "adaptive_rsi.opencode.detect_opencode", return_value=OpenCodeCapabilities("2.1.3", (2, 1, 3))), patch(
                    "adaptive_rsi.opencode.run_process", return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.opencode.run_cli", side_effect=run):
                merge_auth_export(credentials)
                value = OpenCodeAgent(store).invoke(
                    "probe", "Return a valid result", workspace, "p", "run", config,
                    config["models"]["implementation"])
                self.assertEqual(auth_providers(None, OpenCodeCapabilities("2.1.3", (2, 1, 3))), ["provider"])
                self.assertTrue(remove_auth_export("provider"))
            self.assertEqual(value, {"summary": "ok"})
            self.assertEqual([call[:3] for call in captured], [
                ["opencode", "auth", "import"], ["opencode", "run", "--standalone"],
                ["opencode", "auth", "export"]])

    def test_v2_credential_files_are_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data")}, clear=False), \
                    patch("os.umask", return_value=0o022):
                merge_auth_export([{"integrationID": "provider", "id": "credential",
                                    "value": {"type": "key", "key": "private-key"}}])
                path = root / "data/opencode/auth-export.json"
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(list(path.parent.glob(".rsi-auth-*")), [])
                self.assertTrue(remove_auth_export("provider"))
                self.assertFalse(path.exists())


@unittest.skipUnless(os.environ.get("RSI_OPENCODE_TESTS") == "1", "Enable RSI_OPENCODE_TESTS=1 with OpenCode installed")
class OpenCodeIntegrationTest(unittest.TestCase):
    def test_real_cli_reads_edits_and_returns_validated_result(self):
        requests = []
        with tempfile.TemporaryDirectory(prefix="rsi-real-opencode-") as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "calculator.py"
            source.write_text("def add(a,b): return a-b\n")
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(payload)
                    step = len(requests)
                    if step == 1:
                        name, args = "read", {"filePath": str(source)}
                    elif step == 2:
                        name, args = "edit", {"filePath": str(source), "oldString": "return a-b", "newString": "return a+b"}
                    if step <= 2:
                        delta = {"tool_calls": [{"index": 0, "id": "call_" + str(step), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                    else:
                        delta = {"content": '{"summary":"Fixed addition"}'}
                    chunks = [{"id": "chat-" + str(step), "object": "chat.completion.chunk", "model": "fixture", "created": 1,
                               "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                              {"id": "chat-" + str(step), "object": "chat.completion.chunk", "model": "fixture", "created": 1,
                               "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if step <= 2 else "stop"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
                    raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            container = os.environ.get("RSI_DOCKER_TESTS") == "1"
            hostname = "host.docker.internal" if container else "127.0.0.1"
            connection = {"provider": {"fixture": {"npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                            "options": {"baseURL": "http://" + hostname + ":" + str(server.server_port) + "/v1", "apiKey": "fixture-key"},
                            "models": {"fixture": {"name": "Fixture", "limit": {"context": 64000, "output": 8192}}}}}}
            config = validate({"sandbox": {"backend": "docker" if container else "auto"},
                               "models": {"implementation": {"provider": "opencode", "model": "fixture/fixture", "effort": "default"}}})
            store = Store(root / "controller")
            began = time.monotonic()
            try:
                with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(connection)}):
                    result = CodexAgent(store).invoke("implementation", "Fix addition in calculator.py. Read before editing.", workspace,
                                                     "fixture", "run", config, cancel=lambda: time.monotonic() - began > 90)
                self.assertEqual(result, {"summary": "Fixed addition"})
                self.assertIn("return a+b", source.read_text())
                self.assertEqual(len(requests), 3)
                self.assertTrue(any("return a-b" in json.dumps(request["messages"]) for request in requests[1:]))
                call = store.one("SELECT state,tokens,unit,usage_known FROM calls")
                self.assertEqual(call, {"state": "completed", "tokens": 45, "unit": "opencode_session", "usage_known": 1})
                self.assertFalse(any(p.name == "auth.json" for p in workspace.rglob("*")))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
