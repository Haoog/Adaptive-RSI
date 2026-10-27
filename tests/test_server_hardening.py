"""服务会话、认证、异常及备份资源边界的真实 HTTP 回归。"""
import base64
import concurrent.futures
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.catalog import CatalogError
from adaptive_rsi.server import Handler, Server
from adaptive_rsi.store import Store


class ServerHardeningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.runtime = SimpleNamespace(store=self.store, dispatch_lock=threading.RLock(),
                                       updates=SimpleNamespace(require_ready=lambda: None))
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backups = self.store.root / "backups"
        self.backups.mkdir()
        self.identifier = "a" * 16
        self.backup = self.backups / (self.identifier + ".sqlite3")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, path="/api/health", method="GET", body=None, authenticated=True, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {}
        if authenticated:
            values["Cookie"] = self.server.cookie + "=" + self.server.token
        if method == "POST":
            values.update({"Origin": self.server.local_origin, "X-RSI-CSRF": self.server.csrf,
                           "Content-Type": "application/json"})
            body = json.dumps(body or {})
        values.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_update_check_acknowledges_current_pending_state(self):
        pending = {"state": "checking", "busy": False, "current": "fixture"}
        starts = []
        self.runtime.updates.check_async = lambda: starts.append(True) or True
        self.runtime.updates.snapshot = lambda: pending
        status, _, payload = self.request("/api/updates/check", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"started": True, "update": pending})
        self.assertEqual(starts, [True])
        self.runtime.updates.check_async = lambda: False
        status, _, payload = self.request("/api/updates/check", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"started": False, "update": pending})

    def test_rotation_is_private_atomic_and_preserves_cookie_contract(self):
        old_token = self.server.token
        status, headers, payload = self.request("/api/session/rotate", "POST")
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertEqual(result, {"url": self.server.url, "csrf": self.server.csrf})
        self.assertIn(self.server.cookie + "=" + self.server.token, headers["Set-Cookie"])
        saved = self.store.root / "server.json"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(saved.read_text())["token"], self.server.token)
        self.assertNotEqual(old_token, self.server.token)
        self.assertEqual(self.request(headers={"Cookie": self.server.cookie + "=" + old_token})[0], 401)
        self.assertEqual(self.request()[0], 200)

    def test_rotation_failure_preserves_existing_session_and_hides_path(self):
        before = self.server.token, self.server.csrf, self.server.url
        with patch("adaptive_rsi.server.atomic_json", side_effect=OSError("/private/path secret-key")):
            status, _, payload = self.request("/api/session/rotate", "POST")
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret-key", payload)
        self.assertNotIn(b"/private/path", payload)
        self.assertEqual(before, (self.server.token, self.server.csrf, self.server.url))
        self.assertEqual(self.request()[0], 200)

    def test_concurrent_rotation_has_complete_private_final_file(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.server.rotate_session(), range(12)))
        saved = self.store.root / "server.json"
        data = json.loads(saved.read_text())
        self.assertEqual(data["token"], self.server.token)
        self.assertEqual(data["csrf"], self.server.csrf)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len({item["url"] for item in results}), 12)

    def test_rotation_replaces_session_symlink_without_reading_target(self):
        victim = self.root / "victim"
        victim.write_text("private victim content")
        (self.store.root / "server.json").symlink_to(victim)
        self.server.rotate_session()
        self.assertFalse((self.store.root / "server.json").is_symlink())
        self.assertEqual(victim.read_text(), "private victim content")

    def test_backup_binary_stream_and_json_chunk_compatibility(self):
        content = bytes(range(256)) * 5000
        self.backup.write_bytes(content)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded file read")):
            status, headers, body = self.request("/api/backup?id=" + self.identifier)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(content)))
        self.assertEqual(body, content)
        status, _, body = self.request("/api/backup?id=" + self.identifier + "&format=json&offset=7")
        self.assertEqual(status, 200)
        chunk = json.loads(body)
        self.assertEqual(chunk["size"], len(content))
        self.assertEqual(chunk["length"], 512 * 1024)
        self.assertEqual(base64.b64decode(chunk["data"]), content[7:7 + 512 * 1024])
        status, _, body = self.request("/api/backup?id=" + self.identifier + "&format=json&offset=" + str(len(content)))
        self.assertEqual(json.loads(body)["length"], 0)
        self.assertEqual(status, 200)

    def test_backup_rejects_large_files_symlinks_fifo_and_bad_offsets(self):
        with self.backup.open("wb") as stream:
            stream.truncate(1024 * 1024 * 1024 + 1)
        self.assertEqual(self.request("/api/backup?id=" + self.identifier)[0], 400)
        self.backup.unlink()
        self.backup.symlink_to(self.root / "missing-private-target")
        status, _, body = self.request("/api/backup?id=" + self.identifier)
        self.assertEqual(status, 500)
        self.assertNotIn(str(self.root).encode(), body)
        self.backup.unlink()
        os.mkfifo(self.backup)
        self.assertEqual(self.request("/api/backup?id=" + self.identifier)[0], 400)
        self.backup.unlink()
        self.backup.write_bytes(b"abc")
        for offset in ["-1", "4", "bad"]:
            self.assertEqual(self.request("/api/backup?id=" + self.identifier + "&format=json&offset=" + offset)[0], 400)
        self.assertEqual(self.request("/api/backup?id=../../private")[0], 400)
        self.assertEqual(self.request("/api/backup?id=" + self.identifier, authenticated=False)[0], 401)

    def test_backup_concurrency_gate_never_blocks_health(self):
        self.backup.write_bytes(b"abc")
        gate = self.server._backup_gate
        gate.acquire()
        gate.acquire()
        try:
            status, headers, _ = self.request("/api/backup?id=" + self.identifier)
            self.assertEqual(status, 429)
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(self.request()[0], 200)
        finally:
            gate.release()
            gate.release()
        self.assertEqual(self.request("/api/backup?id=" + self.identifier)[0], 200)

    def test_backup_read_and_write_sizes_are_bounded_and_disconnect_releases_gate(self):
        self.backup.write_bytes(b"a" * (192 * 1024))
        handler = object.__new__(Handler)
        handler.server = self.server
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.send_response = lambda _: None
        handler.headers_common = lambda: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        writes = []
        handler.wfile = SimpleNamespace(write=lambda chunk: writes.append(len(chunk)))
        handler.backup(self.backup, {})
        self.assertEqual(writes, [65536] * 3)
        handler.wfile = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(BrokenPipeError()))
        handler.backup(self.backup, {})
        self.assertTrue(handler.close_connection)
        self.assertTrue(self.server._backup_gate.acquire(blocking=False))
        self.assertTrue(self.server._backup_gate.acquire(blocking=False))
        self.server._backup_gate.release()
        self.server._backup_gate.release()

    def test_backup_total_deadline_closes_connection(self):
        self.backup.write_bytes(b"a" * 128000)
        handler = object.__new__(Handler)
        handler.server = self.server
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.send_response = lambda _: None
        handler.headers_common = lambda: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        with patch("adaptive_rsi.server.time.monotonic", side_effect=[10, 131]):
            handler.backup(self.backup, {})
        self.assertEqual(handler.wfile.getvalue(), b"")
        self.assertTrue(handler.close_connection)

    def test_auth_failure_limit_is_thread_safe_and_valid_clients_bypass_it(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.request(authenticated=False), range(42)))
        self.assertEqual(sum(status == 401 for status, _, _ in responses), 30)
        self.assertEqual(sum(status == 429 for status, _, _ in responses), 12)
        self.assertTrue(all("Retry-After" in headers for status, headers, _ in responses if status == 429))
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request("/?token=" + self.server.token, authenticated=False)[0], 303)
        self.assertEqual(self.request("/login.css", authenticated=False)[0], 200)
        self.assertEqual(self.request("/install", authenticated=False)[0], 200)
        self.server._failure_window = 0
        self.assertEqual(self.request(authenticated=False)[0], 401)

    def test_audit_is_bounded_and_never_records_request_secrets(self):
        for _ in range(6):
            self.request("/?token=private-query-token", authenticated=False,
                         headers={"Cookie": "private-cookie", "X-Forwarded-For": "private-forwarded"})
        events = self.store.query("SELECT data,message FROM events WHERE kind='security_failure'")
        self.assertEqual(len(events), 1)
        self.server._audit_after = 0
        self.request(authenticated=False)
        events = self.store.query("SELECT data,message FROM events WHERE kind='security_failure'")
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[-1]["data"])["suppressed"], 5)
        self.assertNotIn("private-", json.dumps(events))
        self.assertEqual(self.request(headers={"Host": "private-invalid-host"})[0], 403)
        self.assertEqual(self.request("/api/session/rotate", "POST", headers={"Origin": "https://private-origin"})[0], 403)
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='security_failure'")), 2)

    def test_exception_boundary_hides_details_and_retains_machine_codes(self):
        secret = "/private/state secret-key https://user:pass@provider.test"
        for exception, expected in [(OSError(secret), 500), (sqlite3.OperationalError(secret), 500),
                                    (RuntimeError(secret), 500), (ValueError(secret), 400),
                                    (AttributeError(secret), 500)]:
            with self.subTest(exception=type(exception)), patch.object(self.store, "one", side_effect=exception):
                status, _, body = self.request("/api/task")
                self.assertEqual(status, expected)
                self.assertNotIn(secret.encode(), body)
        for code in ["missing_key", "connection", "unknown_provider", "provider_paused", "model_missing",
                     "tool_format", "unsupported", "http_error", "budget_exhausted",
                     "opencode_incompatible_2.1.3", secret]:
            with patch("adaptive_rsi.server.list_models", side_effect=CatalogError(code)):
                status, _, body = self.request("/api/models", "POST", {"provider": "x"})
            self.assertNotIn(secret.encode(), body)
            if code != secret:
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["catalog_error"], code)

    def test_configuration_diagnostics_do_not_echo_exception_paths_or_credentials(self):
        from adaptive_rsi.server import configuration_error
        from zoneinfo import ZoneInfoNotFoundError
        secret = "/private/state https://user:pass@host.test secret-key"
        self.assertNotIn(secret, configuration_error(ValueError(secret), "{}"))
        self.assertNotIn(secret, configuration_error(ZoneInfoNotFoundError(secret),
                          json.dumps({"schedule": {"timezone": secret}})))
        self.assertIn("Mars/Olympus", configuration_error(ZoneInfoNotFoundError(secret),
                      json.dumps({"schedule": {"timezone": "Mars/Olympus"}})))
        self.assertIn("Expecting value", configuration_error(json.JSONDecodeError(secret, "secret", 0), "secret"))

    def test_stopping_task_errors_retain_actionable_guidance(self):
        message = "Wait for the active run to finish stopping before retrying or supplementing this task"
        def stopping(*args):
            raise ValueError(message)
        self.runtime.retry = stopping
        status, _, body = self.request("/api/retry", "POST", {"task": "stopping-task"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": message, "error_code": "validation_error"})

    def test_configuration_validation_retains_fixed_guidance_without_echoing_inputs(self):
        from adaptive_rsi.config import validate
        self.runtime.configure = lambda values, *_: validate(values)
        invalid = [
            ({"schedule": {"working_hours": [20, 20]}}, "working_hours requires different start/end hours in 0..24; start must be below 24"),
            ({"github": {"repository": "invalid"}}, "Repository must be owner/name"),
            ({"limits": {"concurrency": 5}}, "Maximum concurrency is 4"),
        ]
        for values, expected in invalid:
            with self.subTest(values=values):
                status, _, body = self.request("/api/config", "POST", {"values": values})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], expected)
                self.assertEqual(json.loads(body)["error_code"], "validation_error")
        status, _, body = self.request("/api/config", "POST", {"values": {"private-secret-field": "secret"}})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error_code"], "invalid_configuration")
        self.assertNotIn(b"private-secret-field", body)
        status, _, body = self.request("/api/state?sections=private-secret-field")
        self.assertEqual(status, 400)
        self.assertNotIn(b"private-secret-field", body)
        self.assertIn(b"core, candidates or analytics", body)


if __name__ == "__main__":
    unittest.main()
