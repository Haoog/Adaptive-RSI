import copy
import json
import os
import platform
import tempfile
import threading
import time
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
from adaptive_rsi.store import Store, encode
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


class ActiveRunCredentialTest(unittest.TestCase):
    """仅使用临时数据库和内存钥匙串替身，不访问宿主凭据或网络。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.keys = {}
        backend_patch = patch("adaptive_rsi.credentials.MacKeychain")
        self.backend = backend_patch.start()
        self.addCleanup(backend_patch.stop)
        self.backend.return_value.request.side_effect = self.keychain_request
        network_patch = patch("adaptive_rsi.native.post_json", return_value={
            "choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "finish", "type": "function", "function": {
                    "name": "finish", "arguments": '{"findings": []}'}}]}}],
            "usage": {"total_tokens": 1}})
        self.network = network_patch.start()
        self.addCleanup(network_patch.stop)
        self.runtime = Runtime(Store(self.root / "state"))
        self.addCleanup(self.runtime.close)
        self.center = self.runtime.model_center
        self.data = {"id": "gateway", "name": "Fixture", "protocol": "chat_completions",
                     "base_url": "https://fixture.invalid/v1", "env_key": ""}
        self.center.save_provider(self.data, {"mode": "keychain", "value": "fixture-old"})
        self.old = self.center.provider("gateway")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime.store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                                   ("p", "Fixture", str(self.workspace), "{}", 0, time.time()))

    def keychain_request(self, service, reference, operation, value=None):
        if operation == "put":
            self.keys[(service, reference)] = value
        elif operation == "delete":
            self.keys.pop((service, reference), None)
        else:
            return self.keys.get((service, reference), "")
        return ""

    def run_snapshot(self, run="r", state="planning", ended=None):
        config = self.runtime.config("p")
        config["models"]["analysis"] = {"provider": "gateway", "model": "fixture",
                                          "effort": "default", "fallback": None}
        task = self.runtime.store.add_task("p", "manual", run, "Fixture task")
        self.runtime.store.execute("INSERT INTO runs(id,task,project,state,config,started,ended) VALUES (?,?,?,?,?,?,?)",
                                   (run, task, "p", state, encode(config), time.time(), ended))
        return self.runtime.store.one("SELECT * FROM runs WHERE id=?", (run,))

    def rotate(self, mode="keychain"):
        self.center.save_provider(self.data, {"mode": mode, "value": "fixture-new"})

    def assert_missing(self, provider):
        with self.assertRaises(CredentialError) as error:
            self.center.credentials.get(provider)
        self.assertEqual(error.exception.code, "missing_key")

    def assert_retired(self, provider):
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"),
                         {provider["secret_ref"]: [provider["credential"]]})

    def test_rotation_keeps_sealed_native_run_working_until_last_run_ends(self):
        first = self.run_snapshot()
        # ended 而非状态名称决定运行是否仍可能调用模型。
        self.run_snapshot("second", "waiting_provider")
        self.rotate()
        current = self.center.provider("gateway")
        self.assertNotEqual(current["secret_ref"], self.old["secret_ref"])
        self.assert_retired(self.old)
        self.assertEqual(self.runtime.invoke("analysis", "Fixture only", self.workspace, first), {"findings": []})
        self.assertEqual(self.network.call_args.args[2]["Authorization"], "Bearer fixture-old")
        self.assertEqual(self.center.credentials.get(current), "fixture-new")
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        self.center.cleanup_credentials()
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='second'", (time.time(),))
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})
        self.center.cleanup_credentials()
        self.assertEqual(self.center.credentials.get(current), "fixture-new")

    def test_removal_keeps_sealed_native_run_working_then_cleanup_survives_restart(self):
        run = self.run_snapshot()
        self.center.remove_provider("gateway")
        self.assertEqual(self.runtime.config()["providers"], [])
        self.assert_retired(self.old)
        self.assertEqual(self.runtime.invoke("analysis", "Fixture only", self.workspace, run), {"findings": []})
        self.assertEqual(self.network.call_args.args[2]["Authorization"], "Bearer fixture-old")
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        restarted = Runtime(Store(self.runtime.store.root))
        try:
            restarted.model_center.cleanup_credentials()
            self.assert_missing(self.old)
            self.assertEqual(restarted.store.setting("credential-cleanup-pending"), {})
        finally:
            restarted.close()

    def test_malformed_unfinished_snapshot_defers_cleanup_conservatively(self):
        self.run_snapshot()
        for raw in ["{", "null", "{}", '{"providers": null}', '{"providers": [null]}']:
            with self.subTest(config=raw):
                self.runtime.store.execute("UPDATE runs SET config=? WHERE id='r'", (raw,))
                self.rotate()
                self.center.cleanup_credentials()
                self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_file_backend_delete_failure_is_retryable(self):
        self.rotate("file")
        provider = self.center.provider("gateway")
        with patch.object(self.center.credentials, "delete", side_effect=PermissionError("Fixture permissions")):
            self.center.remove_provider("gateway")
        self.assert_retired(provider)
        self.assertEqual(self.center.credentials.get(provider), "fixture-new")
        self.center.cleanup_credentials()
        self.assert_missing(provider)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_maintenance_reclaims_retired_key_after_run_finishes(self):
        self.run_snapshot()
        self.rotate()
        self.runtime.maintenance()
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.assert_retired(self.old)
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        self.runtime.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_maintenance_cleanup_failure_does_not_block_other_retention(self):
        self.run_snapshot()
        self.rotate()
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        with patch.object(self.center, "cleanup_credentials", side_effect=RuntimeError("Fixture cleanup failure")):
            self.runtime.maintenance()
        self.assertGreater(self.runtime.store.setting("maintenance-last", 0), 0)
        self.assertIn("credential_cleanup", self.runtime.store.one("SELECT data FROM events WHERE kind='scheduler_error'")["data"])
        self.assert_retired(self.old)
        self.runtime.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assert_missing(self.old)

    def test_maintenance_skips_credentials_during_update_and_retries_afterward(self):
        self.run_snapshot()
        self.rotate()
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='r'", (time.time(),))
        pending = self.runtime.store.setting("credential-cleanup-pending")
        with patch.object(self.runtime.updates, "is_busy", return_value=True), patch.object(
                self.center.credentials, "delete", wraps=self.center.credentials.delete) as delete:
            self.runtime.maintenance()
            delete.assert_not_called()
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), pending)
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.runtime.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_malformed_registry_preserved_and_rejected_before_provider_mutations(self):
        reference = self.old["secret_ref"]
        invalid = [None, [], 42, "bad", {reference: "env"}, {reference: []},
                   {reference: ["file", "invalid"]}, {reference: [None]}, {"bad": "keychain"},
                   {reference: ["file"] * 4}]
        for pending in invalid:
            with self.subTest(pending=pending):
                self.runtime.store.put_setting("credential-cleanup-pending", pending)
                self.runtime.store.put_setting("issue-comments:p:fixture", {"checked_at": 0})
                with patch.object(self.center.credentials, "put") as put, patch.object(
                        self.center.credentials, "delete") as delete:
                    with self.assertRaises(CenterError):
                        self.rotate()
                    with self.assertRaises(CenterError):
                        self.center.remove_provider("gateway")
                    self.runtime.store.put_setting("maintenance-last", 0)
                    self.runtime.maintenance()
                    put.assert_not_called()
                    delete.assert_not_called()
                self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), pending)
                self.assertEqual(self.center.provider("gateway"), self.old)
                self.assertGreater(self.runtime.store.setting("maintenance-last"), 0)
                self.assertIsNone(self.runtime.store.setting("issue-comments:p:fixture"))
        # 损坏的原始 JSON 同样不能被当作空队列覆盖。
        self.runtime.store.execute("UPDATE settings SET value='{' WHERE key='credential-cleanup-pending'")
        self.runtime.store.put_setting("maintenance-last", 0)
        self.runtime.maintenance()
        self.assertEqual(self.runtime.store.one("SELECT value FROM settings WHERE key='credential-cleanup-pending'")["value"], "{")
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")

    def test_registry_limit_rejects_new_intents_without_dropping_existing_entries(self):
        pending = {format(index, "032x"): "session" for index in range(4096)}
        self.runtime.store.put_setting("credential-cleanup-pending", pending)
        with patch.object(self.center.credentials, "put") as put:
            with self.assertRaises(CenterError) as error:
                self.rotate()
            put.assert_not_called()
        self.assertEqual(error.exception.code, "credential_registry_full")
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), pending)
        pending["f" * 32] = "session"
        self.runtime.store.put_setting("credential-cleanup-pending", pending)
        with self.assertRaises(CenterError):
            self.center.cleanup_credentials()
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), pending)

    def test_same_reference_in_two_backends_does_not_overwrite_retirement(self):
        file_provider = {**self.old, "credential": "file"}
        self.center.credentials.put(file_provider["secret_ref"], "file", "fixture-file")
        self.run_snapshot("keychain-run")
        self.runtime.configure({"providers": [file_provider]}, "p")
        self.run_snapshot("file-run")
        self.rotate()
        # 模拟旧版注册表：项目 file 引用不能把已退役的 keychain 身份抹去。
        self.runtime.store.put_setting("credential-cleanup-pending", {self.old["secret_ref"]: "keychain"})
        self.center.remove_provider("gateway", "p")
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"),
                         {self.old["secret_ref"]: ["file", "keychain"]})
        self.runtime.store.execute("UPDATE runs SET ended=? WHERE id='keychain-run'", (time.time(),))
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.center.credentials.get(file_provider), "fixture-file")
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"),
                         {self.old["secret_ref"]: ["file"]})
        self.runtime.store.execute("UPDATE runs SET ended=?", (time.time(),))
        self.runtime.maintenance()
        self.assert_missing(file_provider)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_post_commit_event_failure_never_deletes_new_live_key(self):
        event = self.runtime.store.event
        def fail_config_event(kind, *args, **kwargs):
            if kind == "config":
                raise OSError("Fixture event failure after commit")
            return event(kind, *args, **kwargs)
        with patch.object(self.runtime.store, "event", side_effect=fail_config_event):
            with self.assertRaises(OSError):
                self.rotate()
        current = self.center.provider("gateway")
        self.assertNotEqual(current["secret_ref"], self.old["secret_ref"])
        self.assertEqual(self.center.credentials.get(current), "fixture-new")
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.center.credentials.get(current), "fixture-new")

    def test_fresh_intent_precedes_put_and_survives_interruption_with_failed_rollback(self):
        real_put = self.center.credentials.put
        def interrupted_put(reference, mode, value):
            self.assertIn(mode, self.runtime.store.setting("credential-cleanup-pending")[reference])
            real_put(reference, mode, value)
            raise SystemExit("Fixture interrupted after put")
        with patch.object(self.center.credentials, "put", side_effect=interrupted_put), patch.object(
                self.center.credentials, "delete", side_effect=CredentialError("keychain_locked")):
            with self.assertRaises(SystemExit):
                self.rotate()
        self.assertEqual(len(self.keys), 2)
        restarted = Runtime(Store(self.runtime.store.root))
        try:
            restarted.model_center.cleanup_credentials()
            self.assertEqual(len(self.keys), 1)
            self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        finally:
            restarted.close()

    def test_failed_configure_and_rollback_delete_keep_fresh_intent_for_retry(self):
        with patch.object(self.runtime, "configure", side_effect=ValueError("Fixture configure failure")), patch.object(
                self.center.credentials, "delete", side_effect=PermissionError("Fixture rollback failure")):
            with self.assertRaisesRegex(ValueError, "Fixture configure failure"):
                self.rotate()
        self.assertEqual(len(self.keys), 2)
        self.center.cleanup_credentials()
        self.assertEqual(len(self.keys), 1)
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")

    def test_completed_snapshot_does_not_pin_old_credential(self):
        self.run_snapshot(state="planning", ended=time.time())
        self.rotate()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_shared_project_reference_stays_until_final_current_reference_removed(self):
        self.runtime.configure({"providers": [self.old]}, "p")
        self.rotate()
        self.center.cleanup_credentials()
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.assert_retired(self.old)
        self.center.remove_provider("gateway", "p")
        self.assert_missing(self.old)
        self.assertEqual(self.center.credentials.get(self.center.provider("gateway")), "fixture-new")

    def test_cleanup_failure_is_retryable_and_registry_contains_no_secret_material(self):
        with patch.object(self.center.credentials, "delete", side_effect=CredentialError("keychain_locked")):
            self.rotate()
        self.assert_retired(self.old)
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.assertEqual(self.runtime.store.one("SELECT kind FROM events WHERE kind='credential_cleanup_failed'")["kind"],
                         "credential_cleanup_failed")
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})
        for file in self.runtime.store.root.iterdir():
            if file.is_file():
                self.assertNotIn(b"fixture-old", file.read_bytes())
                self.assertNotIn(b"fixture-new", file.read_bytes())

    def test_failed_configuration_keeps_old_key_and_durable_retirement(self):
        def fail_configure(*args):
            pending = self.runtime.store.setting("credential-cleanup-pending")
            self.assertEqual(pending[self.old["secret_ref"]], ["keychain"])
            self.assertEqual(len(pending), 2)
            raise ValueError("Fixture configuration failure")
        with patch.object(self.runtime, "configure", side_effect=fail_configure):
            with self.assertRaises(ValueError):
                self.rotate()
        self.assertEqual(len(self.keys), 1)
        self.center.cleanup_credentials()
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.assert_retired(self.old)
        self.center.remove_provider("gateway")
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})

    def test_failed_removal_keeps_current_key_and_durable_retirement(self):
        with patch.object(self.runtime, "configure", side_effect=ValueError("Fixture failure")):
            with self.assertRaises(CenterError):
                self.center.remove_provider("gateway")
        self.assert_retired(self.old)
        self.center.cleanup_credentials()
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")

    def test_retirement_write_failure_rolls_back_new_key_without_changing_config(self):
        with patch.object(self.center, "_retire_credential", side_effect=OSError("Fixture storage failure")):
            with self.assertRaises(OSError):
                self.rotate()
        self.assertEqual(len(self.keys), 1)
        self.assertEqual(self.center.provider("gateway"), self.old)
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")

    def test_switching_storage_mode_retires_previous_backend(self):
        self.run_snapshot()
        self.rotate("session")
        self.assert_retired(self.old)
        self.assertEqual(self.center.credentials.get(self.old), "fixture-old")
        self.runtime.store.execute("UPDATE runs SET ended=?", (time.time(),))
        self.center.cleanup_credentials()
        self.assert_missing(self.old)
        self.assertEqual(self.center.credentials.get(self.center.provider("gateway")), "fixture-new")

    def test_switching_to_no_auth_retires_previous_backend(self):
        self.center.save_provider(self.data, {"mode": "none"})
        self.assert_missing(self.old)
        self.assertEqual(self.runtime.store.setting("credential-cleanup-pending"), {})


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

    def test_custom_readd_revives_missing_model_and_survives_catalog_refresh(self):
        with DirectoryServer() as fixture:
            self.save(fixture, "none", value="")
            self.center.fetch("gateway")
            self.center.edit_model("gateway", {"id": "coder-a", "favorite": True, "unit_price": 2})
            fixture.models = [{"id": "coder-b"}]
            self.center.fetch("gateway")
            missing = next(m for m in self.center.directory(self.center.provider("gateway"))["models"] if m["id"] == "coder-a")
            self.assertTrue(missing["missing"])
            self.assertFalse(missing["manual"])

            result = self.center.edit_model("gateway", {"id": "coder-a", "name": "Custom coder", "manual": True})
            self.assertEqual(result, {"saved": True})
            for directory in [self.center.directory(self.center.provider("gateway")), self.center.fetch("gateway")]:
                model = next(m for m in directory["models"] if m["id"] == "coder-a")
                self.assertTrue(model["manual"])
                self.assertFalse(model["missing"])
                self.assertTrue(model["enabled"])
                self.assertEqual(model["name"], "Custom coder")
                self.assertTrue(model["favorite"])
                self.assertEqual(model["unit_price"], 2)
            snapshot = self.center.snapshot()
            models = next(p for p in snapshot["providers"] if p["id"] == "gateway")["catalog"]["models"]
            self.assertIn("coder-a", [m["id"] for m in models if m["enabled"] and not m.get("missing") and not m.get("kind")])

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
            saved = self.center.save_routes(routes, project)
            self.assertEqual(saved["models"], self.runtime.config(project)["models"])
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

    def test_credential_rotation_never_deletes_a_reference_still_in_use(self):
        # 评审实测回归：项目覆盖继承连接的密钥时，不能删除全局仍在引用的凭据。
        from adaptive_rsi.credentials import Credentials
        with DirectoryServer() as fixture:
            self.save(fixture, "file", value="global-key")
            provider = self.center.provider("gateway")
            shared = provider["secret_ref"]
            credentials = Credentials(self.runtime.store.root)
            self.assertEqual(credentials.get(provider), "global-key")
            self.runtime.store.execute(
                "INSERT INTO projects VALUES (?,?,?,?,?,?)",
                ("proj01", "fixture", str(self.root / "unused"), "{}", 1, time.time()))
            project = "proj01"
            self.center.save_provider(fixture.provider, {"mode": "file", "value": "project-key"}, project)
            replaced = self.center.provider("gateway", project)
            self.assertNotEqual(replaced["secret_ref"], shared)
            # 全局配置仍引用共享凭据，文件必须保留且可读取。
            self.assertEqual(credentials.get(self.center.provider("gateway")), "global-key")
            self.assertTrue((self.runtime.store.root / "credentials" / shared).is_file())
            self.assertEqual(credentials.get(replaced), "project-key")
            # 全局重新保存且无其他引用时，旧凭据才被删除。
            self.center.save_provider(fixture.provider, {"mode": "file", "value": "rotated-key"})
            self.assertFalse((self.runtime.store.root / "credentials" / shared).exists())
            self.assertEqual(credentials.get(self.center.provider("gateway")), "rotated-key")
