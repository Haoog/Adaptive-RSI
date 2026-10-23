import copy
import json
import os
import platform
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.catalog import CatalogError, normalize
from adaptive_rsi.config import DEFAULTS, validate
from adaptive_rsi.credentials import CredentialError, Credentials
from adaptive_rsi.model_center import CenterError
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
from tests.test_runtime import fixture


class DirectoryServer:
    def __init__(self):
        self.models = [{"id": "coder-a"}, {"id": "coder-b"}, {"id": "embed-only", "type": "embedding"}]
        self.requests, self.fail, self.tool = [], False, True
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                fixture.requests.append((self.path, dict(self.headers), None))
                if fixture.fail:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.reply({"data": fixture.models})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append((self.path, dict(self.headers), body))
                message = {"role": "assistant", "content": "RSI_OK"}
                if fixture.tool:
                    message["tool_calls"] = [{"id": "test", "type": "function", "function": {
                        "name": "finish", "arguments": json.dumps({"summary": "RSI_OK"})}}]
                self.reply({"choices": [{"message": message}], "usage": {"total_tokens": 12}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def provider(self):
        return {"id": "gateway", "name": "Local gateway", "protocol": "chat_completions",
                "base_url": "http://127.0.0.1:" + str(self.server.server_port), "env_key": ""}


class ModelCenterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.runtime = Runtime(Store(self.root / "state"))
        self.center = self.runtime.model_center

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def save(self, fixture, mode="session", project=None, value="private-test-key"):
        self.center.save_provider(fixture.provider, {"mode": mode, "value": value}, project)

    def test_session_credentials_reach_catalog_and_probe_but_never_persist(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            self.center.fetch("gateway")
            result = self.center.probe("gateway", "coder-a")
            self.assertEqual(len(result["calls"]), 1)
            self.assertEqual(result["calls"][0]["tokens"], 12)
            self.assertEqual(result["calls"][0]["state"], "completed")
            for path, headers, body in fixture.requests:
                self.assertEqual(headers["Authorization"], "Bearer private-test-key")
                self.assertNotIn("private-test-key", path)
                if body:
                    self.assertEqual([t["function"]["name"] for t in body["tools"]], ["finish"])
                    self.assertNotIn("private-test-key", json.dumps(body))
            self.assertNotIn("private-test-key", json.dumps(self.center.snapshot()))
            self.assertNotIn("private-test-key", json.dumps(self.runtime.store.query("SELECT * FROM events")))
            for file in self.runtime.store.root.iterdir():
                if file.is_file():
                    self.assertNotIn(b"private-test-key", file.read_bytes())

    def test_directory_survives_restart_and_preserves_disabled_custom_missing_models(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            self.center.fetch("gateway")
            self.center.edit_model("gateway", {"id": "coder-a", "enabled": False})
            self.center.edit_model("gateway", {"id": "private/coder", "name": "Custom", "manual": True})
            fixture.models = [{"id": "coder-a"}, {"id": "coder-c"}]
            self.center.fetch("gateway")
            directory = self.center.directory(self.center.provider("gateway"))
            models = {m["id"]: m for m in directory["models"]}
            self.assertFalse(models["coder-a"]["enabled"])
            self.assertTrue(models["coder-b"]["missing"])
            self.assertFalse(models["private/coder"]["missing"])
            self.assertEqual(models["private/coder"]["name"], "Custom")
            restarted = Runtime(Store(self.runtime.store.root))
            try:
                self.assertEqual(restarted.model_center.directory(restarted.model_center.provider("gateway")), directory)
            finally:
                restarted.close()

    def test_failed_refresh_keeps_previous_directory(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            first = self.center.fetch("gateway")
            fixture.fail = True
            with self.assertRaises(CatalogError):
                self.center.fetch("gateway")
            directory = self.center.directory(self.center.provider("gateway"))
            self.assertEqual(first["models"], directory["models"])
            self.assertEqual(first["fetched"], directory["fetched"])
            self.assertEqual(directory["error"], "http_error")

    def test_project_directory_and_routes_do_not_change_other_projects_or_defaults(self):
        project = self.runtime.add_project(fixture(self.root / "project-a"))
        other = self.runtime.add_project(fixture(self.root / "project-b"))
        with DirectoryServer() as service:
            self.save(service, "none", value="")
            self.center.fetch("gateway")
            self.center.edit_model("gateway", {"id": "coder-a", "enabled": False}, project)
            self.assertFalse(next(p for p in self.center.snapshot(project)["providers"] if p["id"] == "gateway")["catalog"]["models"][0]["enabled"])
            self.assertTrue(next(p for p in self.center.snapshot(other)["providers"] if p["id"] == "gateway")["catalog"]["models"][0]["enabled"])
            routes = copy.deepcopy(DEFAULTS["models"])
            routes["implementation"] = {"provider": "gateway", "model": "coder-b", "effort": "default", "fallback": None}
            self.center.save_routes(routes, project)
            self.assertEqual(self.runtime.config(project)["models"], routes)
            self.assertEqual(self.runtime.config(other)["models"], DEFAULTS["models"])
            self.runtime.configure({"models": {"planning": {"effort": "medium"}}})
            self.assertEqual(self.runtime.config(project)["models"]["planning"]["effort"], "medium")
            self.assertEqual(self.runtime.config(project)["models"]["implementation"]["model"], "coder-b")
            self.assertEqual(set(self.center.snapshot(project)["overrides"]), {"implementation"})
            self.runtime.configure({"models": {"planning": {"effort": "high"}}})
            self.center.save_routes(DEFAULTS["models"], project)
            self.assertEqual(self.center.snapshot(project)["overrides"], {})
            changed = copy.deepcopy(DEFAULTS["models"])
            changed["planning"]["effort"] = "medium"
            self.center.save_routes(changed)
            self.assertEqual(self.runtime.config(project)["models"], changed)

    def test_credential_rotation_invalidates_directory_and_proof(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            self.center.fetch("gateway")
            self.center.probe("gateway", "coder-a")
            self.center.edit_model("gateway", {"id": "coder-b", "enabled": False})
            self.center.edit_model("gateway", {"id": "custom/coder", "manual": True})
            old = self.center.provider("gateway")
            self.save(fixture, value="new-test-key")
            current = self.center.provider("gateway")
            self.assertNotEqual(old["secret_ref"], current["secret_ref"])
            self.assertIsNone(self.center.directory(current)["fetched"])
            self.center.fetch("gateway")
            self.assertEqual(fixture.requests[-1][1]["Authorization"], "Bearer new-test-key")
            self.assertFalse(any(m.get("verified") for m in self.center.directory(current)["models"]))
            models = {m["id"]: m for m in self.center.directory(current)["models"]}
            self.assertFalse(models["coder-b"]["enabled"])
            self.assertIn("custom/coder", models)

    def test_used_connections_cannot_be_removed_and_invalid_save_does_not_store_key(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            routes = copy.deepcopy(DEFAULTS["models"])
            routes["review"]["fallback"] = {"provider": "gateway", "model": "coder-a", "effort": "default"}
            self.center.save_routes(routes)
            with self.assertRaises(CenterError) as error:
                self.center.remove_provider("gateway")
            self.assertEqual(error.exception.code, "provider_in_use")
            with patch.object(self.center.credentials, "put") as write:
                with self.assertRaises(ValueError):
                    self.center.save_provider({**fixture.provider, "base_url": "http://external.example"}, {"mode": "session", "value": "secret"})
                write.assert_not_called()
            self.center.save_routes(DEFAULTS["models"])
            self.center.remove_provider("gateway")
            self.assertEqual(self.runtime.config()["providers"], [])

    def test_changing_endpoint_requires_a_new_key_and_does_not_send_old_key(self):
        with DirectoryServer() as fixture:
            self.save(fixture)
            with self.assertRaises(CredentialError) as error:
                self.center.save_provider({**fixture.provider, "base_url": "https://other.example/v1"}, {"mode": "session", "value": ""})
            self.assertEqual(error.exception.code, "missing_key")
            self.assertEqual(fixture.requests, [])

    def test_old_async_result_cannot_replace_changed_connection(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            def change_connection(*args):
                self.center.save_provider({**fixture.provider, "base_url": fixture.provider["base_url"] + "/other"}, {"mode": "none"})
                return {"models": [{"id": "old-result", "name": "Old"}], "truncated": False}
            with patch("adaptive_rsi.model_center.list_models", side_effect=change_connection), self.assertRaises(CenterError) as error:
                self.center.fetch("gateway")
            self.assertEqual(error.exception.code, "connection_changed")
            self.assertEqual(self.center.directory(self.center.provider("gateway"))["models"], [])

    def test_probe_records_usage_without_quota_and_text_only_response_cannot_pass(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            fixture.tool = False
            with self.assertRaises(CenterError):
                self.center.probe("gateway", "coder-a")
            self.assertFalse(any(m.get("verified") for m in self.center.directory(self.center.provider("gateway"))["models"]))
            self.assertEqual(self.runtime.store.one("SELECT state FROM calls")["state"], "failed")
            self.runtime.configure({"limits": {"daily_calls": 1}})
            fixture.tool = True
            self.center.probe("gateway", "coder-a")
            self.assertEqual(len(fixture.requests), 2)

    def test_explicit_non_agent_types_are_not_enabled_and_efforts_are_preserved(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            self.center.fetch("gateway")
            models = self.center.directory(self.center.provider("gateway"))["models"]
            self.assertFalse(next(m for m in models if m["id"] == "embed-only")["enabled"])
            with self.assertRaises(CenterError):
                self.center.edit_model("gateway", {"id": "embed-only", "enabled": True})
        models = normalize([{"model": "gpt-6.1-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "bogus"}]}], "codex_login")
        self.assertEqual(models[0]["efforts"], ["high"])

    def test_session_keys_are_scoped_to_state_directory(self):
        reference = uuid.uuid4().hex
        self.center.credentials.put(reference, "session", "test-key")
        provider = {"credential": "session", "secret_ref": reference}
        with self.assertRaises(CredentialError):
            Credentials(self.root / "different-state").get(provider)
        self.assertEqual(self.center.credentials.get(provider), "test-key")
        self.center.credentials.delete(reference, "session")
        with self.assertRaises(CredentialError):
            self.center.credentials.get(provider)

    @patch("adaptive_rsi.cli_worker.platform.system", new=lambda: "Darwin")
    def test_codex_route_receives_key_only_in_environment_and_probe_reads_nonce(self):
        with DirectoryServer() as fixture:
            provider = {**fixture.provider, "protocol": "codex"}
            self.center.save_provider(provider, {"mode": "session", "value": "codex-private-test-key"})
            def fake_process(argv, cwd, timeout, cancel, output_mb, env):
                self.assertEqual(env["RSI_PROVIDER_API_KEY"], "codex-private-test-key")
                self.assertNotIn("codex-private-test-key", json.dumps(argv))
                self.assertIn('shell_environment_policy.exclude=["RSI_PROVIDER_API_KEY"]', argv)
                nonce = (Path(cwd) / "probe.txt").read_text()
                self.assertNotEqual(nonce, "RSI_OK")
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text(json.dumps({"summary": nonce}))
                return {"exit_code": 0, "error": None, "stdout": "codex-private-test-key", "stderr": ""}
            with patch("adaptive_rsi.agent.shutil.which", return_value="/mock/codex"), patch("adaptive_rsi.agent.Sandbox.codex_permissions", return_value=["-c", 'default_permissions="rsi"']), patch("adaptive_rsi.agent.run_process", side_effect=fake_process):
                result = self.center.probe("gateway", "coder-a")
            self.assertEqual(result["calls"][0]["state"], "completed")

    def test_automatic_updates_wait_for_model_probe(self):
        self.runtime.configure({"updates": {"mode": "manual"}})
        self.runtime.store.put_setting("update-status", {"state": "available", "requested": True})
        self.center.busy = 1
        try:
            with patch.object(self.runtime.updates, "apply_async") as install:
                self.runtime.updates.tick()
                install.assert_not_called()
        finally:
            self.center.busy = 0

    def test_directory_refresh_does_not_write_while_software_update_installs(self):
        self.runtime.updates.installing = True
        with patch("adaptive_rsi.model_center.list_models") as fetch, self.assertRaises(CenterError):
            self.center.fetch("openai")
        fetch.assert_not_called()
        self.runtime.updates.installing = False
        def install_started(*args):
            self.runtime.updates.installing = True
            return {"models": [{"id": "test-model", "name": "Test"}], "truncated": False}
        try:
            with patch("adaptive_rsi.model_center.list_models", side_effect=install_started), self.assertRaises(CenterError):
                self.center.fetch("openai")
            self.assertIsNone(self.center.directory(self.center.provider("openai"))["fetched"])
        finally:
            self.runtime.updates.installing = False

    @unittest.skipUnless(platform.system() == "Darwin", "macOS Keychain")
    def test_real_keychain_round_trip_and_no_plaintext_database_key(self):
        reference = uuid.uuid4().hex
        try:
            self.center.credentials.put(reference, "keychain", "temporary-validation-key")
            self.assertEqual(self.center.credentials.get({"credential": "keychain", "secret_ref": reference}), "temporary-validation-key")
        finally:
            self.center.credentials.delete(reference, "keychain")

    def test_center_api_keeps_credentials_out_of_snapshots_and_requires_csrf(self):
        server = Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        headers = {"Cookie": server.cookie + "=" + server.token, "Origin": server.origin, "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"}
        try:
            with DirectoryServer() as fixture:
                body = json.dumps({"provider": fixture.provider, "authentication": {"mode": "session", "value": "api-private-key"}}).encode()
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(server.origin + "/api/model-center/provider", body, {k: v for k, v in headers.items() if k != "X-RSI-CSRF"}))
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                with urllib.request.urlopen(urllib.request.Request(server.origin + "/api/model-center/provider", body, headers)) as response:
                    self.assertTrue(json.load(response)["saved"])
                with urllib.request.urlopen(urllib.request.Request(server.origin + "/api/model-center", headers=headers)) as response:
                    snapshot = response.read().decode()
                self.assertNotIn("api-private-key", snapshot)
                self.assertIn('"credential_status": "configured"', snapshot)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
