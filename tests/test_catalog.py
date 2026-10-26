import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.catalog import CatalogError, list_models
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store


def opencode_fixture(directory, version="2.1.3", fail=False):
    script = Path(directory) / "opencode"
    trace = Path(directory) / "opencode-trace.jsonl"
    script.write_text("#!" + sys.executable + "\n" + '''import json, sys
from pathlib import Path
version = %r
fail = %r
args = sys.argv[1:]
with Path(__file__).with_name("opencode-trace.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("opencode v" + version)
    raise SystemExit(0)
if args[:2] == ["auth", "export"]:
    print("[]")
    raise SystemExit(0)
if fail:
    print("private-output-secret")
    print("private-stderr-secret", file=sys.stderr)
    raise SystemExit(2)
if version.startswith("2."):
    if args == ["api", "GET", "/api/model", "--standalone"]:
        print(json.dumps({"location": {}, "data": [
            {"id": "coder", "modelID": "coder", "providerID": "opencode-go", "name": "Coder",
             "capabilities": {"tools": True}, "variants": [{"id": "high"}, {"id": "low"}]},
            {"id": "embed", "modelID": "embed", "providerID": "opencode-go", "name": "Embed",
             "capabilities": {"tools": False}, "variants": []},
            {"id": "other", "modelID": "other", "providerID": "fixture", "name": "Other",
             "capabilities": {"tools": True}, "variants": []}]}))
        raise SystemExit(0)
    if args == ["models", "--standalone"]:
        print("opencode-go/coder\\nfixture/other")
        raise SystemExit(0)
else:
    if args == ["--pure", "models", "--verbose"]:
        print("provider/legacy")
        print(json.dumps({"providerID": "provider", "name": "Legacy",
                          "capabilities": {"toolcall": True},
                          "variants": {"low": {}, "high": {}}}))
        raise SystemExit(0)
raise SystemExit(3)
''' % (version, fail))
    script.chmod(0o700)
    return script, trace


class CatalogFixture:
    def __init__(self, protocol="responses", behavior="normal"):
        self.requests, self.protocol, self.behavior = [], protocol, behavior
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.requests.append((self.path, dict(self.headers)))
                if behavior in ["auth", "redirect", "unsupported"]:
                    self.send_response({"auth": 401, "redirect": 302, "unsupported": 404}[behavior])
                    self.send_header("Location", fixture.url + "/key-must-not-follow")
                    self.end_headers()
                    self.wfile.write(b"private-provider-error-secret")
                    return
                second = "after=" in self.path or "after_id=" in self.path or "pageToken=" in self.path
                if protocol == "ollama":
                    data = {"models": [{"name": "local:latest", "model": "local:latest"}]}
                elif protocol == "gemini":
                    data = {"models": [{"name": "models/gemini-code", "displayName": "Gemini Code", "supportedGenerationMethods": ["generateContent"]},
                                       {"name": "models/embed-only", "supportedGenerationMethods": ["embedContent"]}]}
                    if not second:
                        data["nextPageToken"] = "page/private+cursor"
                else:
                    data = {"data": [{"id": "coder-b", "display_name": "Coder B"}] if second else [
                        {"id": "coder-a"}, {"id": "coder-a"}, {"id": "bad model"}, {"id": "bad\nmodel"}, None],
                        "has_more": not second, "last_id": "page/private+cursor"}
                if behavior == "empty":
                    data = {"models" if protocol in ["gemini", "ollama"] else "data": []}
                if behavior == "broken":
                    data = {"data": {"private-provider-error-secret": "not a list"}}
                if behavior == "repeated_cursor":
                    data.update(has_more=True, last_id="same-cursor")
                if behavior == "large":
                    data = {"data": [{"id": "m" + str(i)} for i in range(1005)]}
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def provider(self):
        return {"id": "unsaved", "name": "Fixture", "protocol": self.protocol,
                "base_url": self.url, "env_key": "RSI_CATALOG_TEST_KEY"}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CatalogTest(unittest.TestCase):
    def test_official_go_protocol_bases_use_public_independent_directory(self):
        cases = [("chat_completions", "openai/v1"), ("responses", "openai/v1"),
                 ("codex", "openai/v1"), ("anthropic", "anthropic/v1"), ("gemini", "google/v1beta")]
        for protocol, path in cases:
            for suffix in ("", "/"):
                with self.subTest(protocol=protocol, suffix=suffix):
                    provider = {"id": "go", "name": "Go", "protocol": protocol,
                                "base_url": "https://opencode.ai/inference/go/" + path + suffix,
                                "env_key": "RSI_GO_CATALOG_TEST_KEY"}
                    with patch.dict(os.environ, {}, clear=True), \
                            patch("adaptive_rsi.catalog.get_json", return_value={"object": "list", "data": [
                                {"id": "glm-5.1", "object": "model", "owned_by": "opencode"},
                                {"id": "gemini-4-argon", "object": "model", "owned_by": "opencode"}]}) as get:
                        data = list_models(provider, key="private-go-test-key")
                    self.assertEqual([model["id"] for model in data["models"]], ["gemini-4-argon", "glm-5.1"])
                    self.assertFalse(data["truncated"])
                    self.assertEqual(get.call_args.args[0], "https://opencode.ai/inference/go/v1/models")
                    headers = {key.lower(): value for key, value in get.call_args.args[1].items()}
                    self.assertFalse(set(headers) & {"authorization", "x-api-key", "x-goog-api-key"})
                    self.assertEqual(headers["user-agent"], "Adaptive-RSI")
                    self.assertNotIn("private-go-test-key", str(get.call_args))
        # The documented directory is public even when the inference key is not set yet.
        with patch.dict(os.environ, {}, clear=True), \
                patch("adaptive_rsi.catalog.get_json", return_value={"data": [{"id": "glm-5.1"}]}):
            self.assertEqual(list_models(provider)["models"][0]["id"], "glm-5.1")

    def test_official_go_directory_keeps_response_validation_and_size_bound(self):
        provider = {"id": "go", "name": "Go", "protocol": "anthropic", "env_key": "",
                    "base_url": "https://opencode.ai/inference/go/anthropic/v1"}
        with patch("adaptive_rsi.catalog.get_json", return_value={"data": [{"id": "m" + str(i)} for i in range(1001)]}):
            result = list_models(provider)
            self.assertEqual(len(result["models"]), 1000)
            self.assertTrue(result["truncated"])
        with patch("adaptive_rsi.catalog.get_json", return_value={"models": []}), \
                self.assertRaises(CatalogError) as error:
            list_models(provider)
        self.assertEqual(error.exception.code, "invalid_response")

    def test_go_directory_rewrite_never_matches_custom_hosts_ports_or_paths(self):
        bases = ["https://gateway.example/inference/go/openai/v1", "https://opencode.ai.example/inference/go/openai/v1",
                 "https://sub.opencode.ai/inference/go/openai/v1", "https://opencode.ai:8443/inference/go/openai/v1",
                 "https://opencode.ai/inference/go/openai/v1/custom", "https://opencode.ai/inference/zen/openai/v1",
                 "https://opencode.ai/inference/go/unknown/v1", "https://opencode.ai/inference/go/openai/v10"]
        for base in bases:
            with self.subTest(base=base), patch("adaptive_rsi.catalog.get_json", return_value={"data": [{"id": "custom"}]}) as get:
                list_models({"id": "custom", "name": "Custom Go", "protocol": "chat_completions", "base_url": base, "env_key": ""}, key="private-custom-key")
                self.assertEqual(get.call_args.args[0], base + "/models")
                self.assertEqual(get.call_args.args[1]["Authorization"], "Bearer private-custom-key")

    def test_official_go_redirect_cannot_forward_inference_key(self):
        provider = {"id": "go", "name": "Go", "protocol": "gemini", "env_key": "",
                    "base_url": "https://opencode.ai/inference/go/google/v1beta"}
        opener = urllib.request.build_opener
        with CatalogFixture(behavior="redirect") as fixture:
            def transport(*handlers):
                real = opener(*handlers)
                original = real.open
                def open_local(request, **kwargs):
                    self.assertEqual(request.full_url, "https://opencode.ai/inference/go/v1/models")
                    local = urllib.request.Request(fixture.url + "/go-models", headers=dict(request.header_items()))
                    return original(local, **kwargs)
                real.open = open_local
                return real
            with patch("adaptive_rsi.catalog.urllib.request.build_opener", side_effect=transport), \
                    self.assertRaises(CatalogError) as error:
                list_models(provider, key="private-go-test-key")
            self.assertEqual(error.exception.code, "http_error")
            self.assertEqual(len(fixture.requests), 1)
            self.assertNotIn("private-go-test-key", str(fixture.requests))

    def test_protocols_auth_pagination_and_model_ids(self):
        for protocol in ["responses", "chat_completions", "codex", "anthropic", "gemini", "ollama"]:
            with self.subTest(protocol=protocol), CatalogFixture(protocol) as fixture, patch.dict(os.environ, {"RSI_CATALOG_TEST_KEY": "test-only-secret"}):
                data = list_models(fixture.provider())
                expected = ["gemini-code"] if protocol == "gemini" else ["local:latest"] if protocol == "ollama" else ["coder-a", "coder-b"]
                self.assertEqual([m["id"] for m in data["models"]], expected)
                self.assertFalse(data["truncated"])
                self.assertEqual(len(fixture.requests), 1 if protocol == "ollama" else 2)
                for path, headers in fixture.requests:
                    self.assertNotIn("test-only-secret", path)
                    headers = {k.lower(): v for k, v in headers.items()}
                    key = "x-api-key" if protocol == "anthropic" else "x-goog-api-key" if protocol == "gemini" else "authorization"
                    self.assertEqual(headers[key], "test-only-secret" if protocol in ["anthropic", "gemini"] else "Bearer test-only-secret")
                    self.assertEqual(urllib.parse.urlsplit(path).path, "/api/tags" if protocol == "ollama" else "/models")
                if protocol != "ollama":
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fixture.requests[1][0]).query)
                    self.assertEqual(query["after_id" if protocol == "anthropic" else "pageToken" if protocol == "gemini" else "after"], ["page/private+cursor"])

    def test_empty_truncated_and_malformed_directories(self):
        for behavior in ["empty", "large", "broken", "repeated_cursor"]:
            with self.subTest(behavior=behavior), CatalogFixture(behavior=behavior) as fixture:
                provider = {**fixture.provider(), "env_key": ""}
                if behavior in ["broken", "repeated_cursor"]:
                    with self.assertRaises(CatalogError) as error:
                        list_models(provider)
                    self.assertEqual(error.exception.code, "invalid_response")
                else:
                    data = list_models(provider)
                    self.assertEqual(len(data["models"]), 1000 if behavior == "large" else 0)
                    self.assertEqual(data["truncated"], behavior == "large")

    def test_errors_are_redacted_and_redirects_never_forward_keys(self):
        for behavior, code in [("auth", "auth"), ("redirect", "http_error"), ("unsupported", "unsupported")]:
            with self.subTest(behavior=behavior), CatalogFixture(behavior=behavior) as fixture, patch.dict(os.environ, {"RSI_CATALOG_TEST_KEY": "test-only-secret"}):
                with self.assertRaises(CatalogError) as error:
                    list_models(fixture.provider())
                self.assertEqual(error.exception.code, code)
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(len(fixture.requests), 1)

    def test_invalid_unsaved_config_and_missing_environment_never_connect(self):
        with CatalogFixture() as fixture, patch.dict(os.environ, {}, clear=True):
            for provider, code in [(fixture.provider(), "missing_key"),
                                   ({**fixture.provider(), "base_url": "http://external.example/v1"}, "invalid_provider"),
                                   ({**fixture.provider(), "base_url": fixture.url + "?key=secret"}, "invalid_provider"),
                                   ({**fixture.provider(), "env_key": "an-actual-secret"}, "invalid_provider"),
                                   (None, "invalid_provider"), ("unconfigured", "invalid_provider")]:
                with self.subTest(code=code), self.assertRaises(CatalogError) as error:
                    list_models(provider)
                self.assertEqual(error.exception.code, code)
            self.assertEqual(fixture.requests, [])

    def test_builtin_local_endpoints(self):
        with patch("adaptive_rsi.catalog.get_json", return_value={"models": [{"name": "local-code"}]}) as get:
            self.assertEqual(list_models("ollama")["models"][0]["id"], "local-code")
            self.assertEqual(get.call_args[0][0], "http://127.0.0.1:11434/api/tags")
        with patch("adaptive_rsi.catalog.get_json", return_value={"data": [{"id": "local-code"}]}) as get:
            list_models("lmstudio")
            self.assertEqual(get.call_args[0][0], "http://127.0.0.1:1234/v1/models")

    def test_codex_handshake_pagination_and_no_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "codex"
            trace = Path(directory) / "requests.jsonl"
            script.write_text("#!" + sys.executable + "\n" + '''import json, sys
from pathlib import Path
trace = Path(__file__).with_name("requests.jsonl")
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    with trace.open("a") as stream: stream.write(line)
    if request["method"] == "initialize":
        print(json.dumps({"id": 0, "result": {}}), flush=True)
    elif request["method"] == "initialized": initialized = True
    elif request["method"] == "model/list" and initialized:
        second = request["params"].get("cursor") == "second"
        print(json.dumps({"id": request["id"], "result": {"data": [
            {"id": "opaque-id", "model": "real-model-b" if second else "real-model-a", "displayName": "A model"},
            {"model": "hidden-model", "hidden": True}], "nextCursor": None if second else "second"}}), flush=True)
    else: raise RuntimeError("invalid handshake or inference")
''')
            script.chmod(0o700)
            with patch("adaptive_rsi.catalog.shutil.which", return_value=str(script)):
                data = list_models("openai")
            self.assertEqual([m["id"] for m in data["models"]], ["real-model-a", "real-model-b"])
            self.assertEqual([json.loads(line)["method"] for line in trace.read_text().splitlines()], ["initialize", "initialized", "model/list", "model/list"])

    def test_codex_timeout_terminates_process_and_hides_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "codex"
            pid_file = Path(directory) / "pid"
            script.write_text("#!" + sys.executable + "\nimport os, time, sys\nfrom pathlib import Path\nPath(__file__).with_name('pid').write_text(str(os.getpid()))\nprint('private-stderr-secret', file=sys.stderr, flush=True)\ntime.sleep(60)\n")
            script.chmod(0o700)
            processes, spawn = [], subprocess.Popen
            def capture(*args, **kwargs):
                process = spawn(*args, **kwargs)
                processes.append(process)
                return process
            with patch("adaptive_rsi.catalog.shutil.which", return_value=str(script)), patch("adaptive_rsi.catalog.TIMEOUT", 0.5), patch("adaptive_rsi.catalog.subprocess.Popen", side_effect=capture):
                with self.assertRaises(CatalogError) as error:
                    list_models("openai")
            self.assertEqual(error.exception.code, "timeout")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(processes[0].returncode, -9)

    def test_opencode_v1_and_v2_commands_and_parsers_do_not_cross_flags(self):
        cases = {
            "1.19.3": (["provider/legacy"], ["--pure", "models", "--verbose"]),
            "2.1.3": (["fixture/other", "opencode-go/coder"], ["api", "GET", "/api/model", "--standalone"]),
        }
        for version, (expected, required) in cases.items():
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                script, trace = opencode_fixture(directory, version)
                env = {"XDG_CONFIG_HOME": str(Path(directory) / "config"),
                       "XDG_DATA_HOME": str(Path(directory) / "data"),
                       "XDG_CACHE_HOME": str(Path(directory) / "cache")}
                with patch.dict(os.environ, env), patch(
                        "adaptive_rsi.catalog.shutil.which",
                        side_effect=lambda name: str(script) if name == "opencode" else None):
                    data = list_models("opencode")
                self.assertEqual([model["id"] for model in data["models"]], expected)
                calls = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertIn(required, calls)
                model_calls = [call for call in calls if call[:1] not in (["--version"], ["auth"])]
                if version.startswith("1."):
                    self.assertFalse(any("--standalone" in call for call in model_calls))
                    self.assertEqual(data["models"][0]["efforts"], ["high", "low"])
                else:
                    self.assertFalse(any("--pure" in call or "--verbose" in call for call in model_calls))
                    coder = next(model for model in data["models"] if model["id"] == "opencode-go/coder")
                    self.assertEqual(coder["efforts"], ["high", "low"])
                    self.assertFalse(any(model["id"].endswith("/embed") for model in data["models"]))

    def test_opencode_command_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            script, _ = opencode_fixture(directory, "2.1.3", fail=True)
            env = {"XDG_CONFIG_HOME": str(Path(directory) / "config"),
                   "XDG_DATA_HOME": str(Path(directory) / "data"),
                   "XDG_CACHE_HOME": str(Path(directory) / "cache")}
            with patch.dict(os.environ, env), patch(
                    "adaptive_rsi.catalog.shutil.which",
                    side_effect=lambda name: str(script) if name == "opencode" else None):
                with self.assertRaises(CatalogError) as error:
                    list_models("opencode")
            self.assertEqual(error.exception.code, "opencode_error")
            self.assertNotIn("private", str(error.exception))

    def test_api_fetches_unsaved_provider_without_mutation_or_dispatch_lock(self):
        with tempfile.TemporaryDirectory() as directory, CatalogFixture() as fixture:
            runtime = Runtime(Store(directory))
            server = Server(runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            headers = {"Cookie": server.cookie + "=" + server.token, "Content-Type": "application/json", "Origin": server.origin, "X-RSI-CSRF": server.csrf}
            body = json.dumps({"provider": {**fixture.provider(), "env_key": ""}}).encode()
            try:
                for missing in ["Cookie", "Origin", "X-RSI-CSRF"]:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(server.origin + "/api/models", body, {k: v for k, v in headers.items() if k != missing}))
                    self.assertIn(error.exception.code, [401, 403])
                    error.exception.close()
                with runtime.dispatch_lock:
                    with urllib.request.urlopen(urllib.request.Request(server.origin + "/api/models", body, headers), timeout=3) as response:
                        self.assertEqual(len(json.load(response)["models"]), 2)
                self.assertEqual(runtime.config()["providers"], [])
                self.assertEqual(runtime.store.query("SELECT * FROM calls"), [])
                bad = json.dumps({"provider": None}).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(server.origin + "/api/models", bad, headers))
                self.assertEqual(json.load(error.exception)["catalog_error"], "invalid_provider")
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                runtime.close()
