"""服务会话、认证、异常及备份资源边界的真实 HTTP 回归。"""
import base64
import concurrent.futures
import hashlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from adaptive_rsi.catalog import CatalogError
from adaptive_rsi.operations import Operations
from adaptive_rsi.server import Handler, Server
from adaptive_rsi.store import Store


class ServerHardeningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.runtime = SimpleNamespace(store=self.store, dispatch_lock=threading.RLock(),
                                       updates=SimpleNamespace(require_ready=lambda: None))
        self.runtime.operations = Operations(self.runtime)
        self.addCleanup(self.runtime.operations.close)
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.backups = self.store.root / "backups"
        self.backups.mkdir()
        self.identifier = "a" * 16
        self.backup = self.backups / (self.identifier + ".sqlite3")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.store.close()
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

    def test_release_retry_preserves_fixed_rejections_and_hides_unknown_errors(self):
        messages = [
            'Post-merge verification must pass for the exact target commit before retrying delivery',
            'A newer deployment owns this target; stale delivery cannot be retried',
            'Only a confirmed failed workflow run can be retried; uncertain dispatches require reconciliation',
        ]
        for message in messages + ['PRIVATE-INTERNAL-ERROR']:
            with self.subTest(message=message):
                def reject(*args):
                    raise ValueError(message)
                self.runtime.retry_release = reject
                status, _, body = self.request('/api/release-retry', 'POST',
                                               {'project': 'p', 'run': 'r', 'kind': 'deploy'})
                payload = json.loads(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload['error'], message if message in messages else 'Invalid request')
                self.assertEqual(payload['error_code'], 'validation_error' if message in messages else 'invalid_request')

    def test_diagnostic_export_requires_consent_and_recomputes_allowlisted_preview(self):
        secret = "private-key-and-source-text"
        self.store.execute("INSERT INTO runs(id,project,state,result) VALUES ('r','p',?,?)",
                           (secret, json.dumps({"failure": {"code": secret}})))
        self.store.put_setting("secret", secret)
        status, _, payload = self.request("/api/diagnostics/preview")
        self.assertEqual(status, 200)
        preview = json.loads(payload)
        self.assertEqual(set(preview), {"format", "version", "created", "included",
                                        "row_counts", "invalid_evidence_count", "sqlite_version"})
        self.assertNotIn(secret, payload.decode())
        status, _, payload = self.request("/api/diagnostics/export", "POST", {"consent": False})
        self.assertEqual(status, 400)
        self.assertNotIn(secret, payload.decode())
        status, _, payload = self.request("/api/diagnostics/export", "POST",
                                          {"consent": True, "preview": {"secret": secret}})
        self.assertEqual(status, 400)
        status, _, payload = self.request("/api/diagnostics/export", "POST", {"consent": True})
        self.assertEqual(status, 200)
        exported = json.loads(payload)
        self.assertEqual(exported["filename"], "adaptive-rsi-diagnostics.json")
        body = json.loads(exported["body"])
        self.assertEqual(set(body), set(preview))
        self.assertNotIn(secret, exported["body"])

    def test_shutdown_status_exposes_phase_and_remaining_work(self):
        self.runtime.shutdown_status = lambda: {"phase": "workers", "started": 1.0, "ended": None,
                                                "remaining_workers": 2, "remaining_operations": 1,
                                                "maintenance_running": False}
        status, _, payload = self.request("/api/shutdown-status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"phase": "workers", "started": 1.0, "ended": None,
                                               "remaining_workers": 2, "remaining_operations": 1,
                                               "maintenance_running": False})
        # The probe must remain readable after the runtime closes its store,
        # because the overlay keeps polling until the process exits.
        self.store.close()
        status, _, payload = self.request("/api/shutdown-status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["phase"], "workers")

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

    def patch_run(self, size):
        identifier = "b" * 16
        directory = self.store.root / "runs" / identifier
        directory.mkdir(parents=True)
        self.store.execute("INSERT INTO runs(id,project,state,path) VALUES (?,?,?,?)",
                           (identifier, "project", "merged", str(directory)))
        (directory / "candidate.patch").write_bytes(b"x" * size)
        return identifier, directory

    def test_oversized_patch_preview_is_not_reported_as_missing_artifacts(self):
        identifier, directory = self.patch_run(2 * 1024 * 1024 + 10)
        status, _, body = self.request("/api/patch?id=" + identifier)
        payload = json.loads(body)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "patch_too_large")
        self.assertIn("2 MiB", payload["error"])
        self.assertIn("run directory", payload["error"])
        self.assertNotIn(str(directory), body.decode())
        status, _, body = self.request("/api/diff-files?id=" + identifier)
        self.assertEqual((status, json.loads(body)), (400, payload))
        (directory / "candidate.patch").unlink()
        for route in ("/api/patch?id=", "/api/diff-files?id="):
            status, _, body = self.request(route + identifier)
            self.assertEqual((status, json.loads(body)),
                             (400, {"error": "Run has no artifacts in this state directory"}))

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

    def test_backup_download_records_completed_and_missing_responses(self):
        self.backup.write_bytes(b'backup payload')
        self.assertEqual(self.request('/api/backup?id=' + self.identifier)[0], 200)
        rows = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = self.store.query("SELECT status,error_code FROM request_log WHERE path='/api/backup'")
            if rows:
                break
            time.sleep(.01)
        self.assertEqual([(r['status'], r['error_code']) for r in rows], [(200, '')])
        self.backup.unlink()
        self.assertEqual(self.request('/api/backup?id=' + self.identifier)[0], 400)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = self.store.query("SELECT status,error_code FROM request_log WHERE path='/api/backup' ORDER BY rowid")
            if len(rows) == 2:
                break
            time.sleep(.01)
        self.assertEqual([(r['status'], r['error_code']) for r in rows], [(200, ''), (400, 'http_400')])

    def test_request_log_write_does_not_wait_for_the_database_write_lock(self):
        holder = sqlite3.connect(str(self.store.path), timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            status, _, _ = self.request("/api/health")
            elapsed = time.monotonic() - started
            # 响应体先于 finally 写出，等到 handler 真正尝试写日志再断言释放时机。
            deadline = time.monotonic() + 1
            while (self.store.metrics()["request_logging"]["observed"] < 1
                   and time.monotonic() < deadline):
                time.sleep(.005)
            self.assertEqual(self.store.metrics()["request_logging"]["observed"], 1)
            # 写锁仍被占用时 handler 线程就必须归还连接，而不是等满默认 15s。
            deadline = time.monotonic() + .2
            while self.store.metrics()["borrowed"] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(self.store.metrics()["borrowed"], 0)
            self.assertEqual(self.store.metrics()["request_logging"]["failed"], 1)
        finally:
            holder.rollback()
        self.assertEqual(status, 200)
        self.assertLess(elapsed, .2)
        # 锁释放后日志照常写入，采样与计数语义不变。
        self.assertEqual(self.request("/api/health")[0], 200)
        rows = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = self.store.query("SELECT status FROM request_log WHERE path='/api/health'")
            if rows:
                break
            time.sleep(.01)
        self.assertEqual([row["status"] for row in rows], [200])

    def test_interrupted_backup_stream_is_not_logged_as_success(self):
        self.backup.write_bytes(b'payload')
        original = Handler.end_headers
        def truncate_after_headers(handler):
            original(handler)
            if handler.path.startswith('/api/backup'):
                self.backup.write_bytes(b'')
        with patch.object(Handler, 'end_headers', truncate_after_headers):
            with self.assertRaises(http.client.IncompleteRead):
                self.request('/api/backup?id=' + self.identifier)
        rows = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = self.store.query("SELECT status,error_code FROM request_log WHERE path='/api/backup'")
            if rows:
                break
            time.sleep(.01)
        self.assertEqual([(r['status'], r['error_code']) for r in rows], [(500, 'download_interrupted')])
        self.assertTrue(self.server._backup_gate.acquire(blocking=False))
        self.server._backup_gate.release()

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

    def test_backup_copy_outside_scheduler_lock_stays_visible_to_update_admission(self):
        from adaptive_rsi import storage_tools
        from adaptive_rsi.lifecycle import readiness

        started, release = threading.Event(), threading.Event()
        pauses, answers = [], []

        def slow_backup(store, **kwargs):
            started.set()
            release.wait(5)
            return {"id": "b" * 16, "bytes": 1, "created": 0.0}

        self.runtime.pause = lambda project, paused: pauses.append((project, paused))
        self.runtime.model_center = SimpleNamespace(busy=0, catalog_inflight=set())
        self.runtime.auth_sessions = SimpleNamespace(busy=0)
        self.runtime.import_in_progress = False
        self.runtime.maintenance_lock = threading.Lock()
        self.runtime.updates.desktop_managed = True
        poster = threading.Thread(target=lambda: answers.append(self.request("/api/backup", "POST")), daemon=True)
        with patch.object(storage_tools, "backup", slow_backup):
            poster.start()
            self.assertTrue(started.wait(5), "备份桩未进入拷贝阶段")
            try:
                # 拷贝在 dispatch_lock 之外进行，调度与暂停请求不再被整库拷贝冻结。
                clock = time.monotonic()
                status, _, _ = self.request("/api/pause", "POST", {"project": "p", "paused": True})
                self.assertEqual(status, 200)
                self.assertLess(time.monotonic() - clock, 1.0, "备份期间 /api/pause 被 dispatch_lock 阻塞")
                self.assertEqual(pauses, [("p", True)])
                # 锁外拷贝必须在同一临界区内登记为在办工作，桌面更新不得在它结束前 commit。
                self.assertTrue(self.runtime.operations.busy())
                prepared = readiness(self.runtime, "prepare", "9.0.0")
                self.assertFalse(prepared["ready"])
                self.assertIn("background_operations", prepared["reasons"])
                with self.assertRaisesRegex(ValueError, "active operations"):
                    readiness(self.runtime, "commit", lease=prepared["barrier"]["lease"])
            finally:
                release.set()
            poster.join(5)
        self.assertEqual(answers[0][0], 200)
        self.assertFalse(self.runtime.operations.busy())
        self.runtime.updates.desktop_owner_pid = os.getpid()
        committed = readiness(self.runtime, "commit", lease=prepared["barrier"]["lease"])
        self.assertEqual(committed["barrier"]["phase"], "installing")

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
        handler.wfile = SimpleNamespace(write=lambda chunk: writes.append(len(chunk)), flush=lambda: None)
        handler.backup(self.backup, {})
        self.assertEqual(writes, [65536] * 3)
        self.assertEqual((handler._response_status, handler._response_error_code), (200, ''))
        handler.wfile = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(BrokenPipeError()))
        handler.backup(self.backup, {})
        self.assertTrue(handler.close_connection)
        self.assertTrue(self.server._backup_gate.acquire(blocking=False))
        self.assertTrue(self.server._backup_gate.acquire(blocking=False))
        self.server._backup_gate.release()
        self.server._backup_gate.release()

    def test_backup_manifest_hashes_bounded_chunks_from_the_open_descriptor(self):
        content = b"manifest fixture" * 18000
        self.backup.write_bytes(content)
        handler = object.__new__(Handler)
        handler.server = self.server
        responses, reads = [], []
        handler.respond = lambda data, code=200, content_type="application/json; charset=utf-8", headers=None: responses.append(data)
        original = os.fdopen

        class BoundedReader:
            def __init__(self, descriptor, mode):
                self.stream = original(descriptor, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                self_test.assertGreater(size, 0)
                self_test.assertLessEqual(size, 64 * 1024)
                reads.append(size)
                return self.stream.read(size)

        def verify(path):
            # Reopening the path after verification would hash different bytes.
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(b"replacement content")
            replacement.replace(path)
            return {"bytes": len(content), "schema_version": 1}

        self_test = self
        with patch("adaptive_rsi.server.os.fdopen", side_effect=BoundedReader), \
                patch("adaptive_rsi.storage_tools.verify_backup", side_effect=verify):
            handler.backup(self.backup, {"format": ["manifest"]})
        self.assertTrue(reads)
        self.assertEqual(responses[0]["backup"]["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(responses[0]["backup"]["bytes"], len(content))

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

    def test_respond_treats_socket_write_timeout_as_client_disconnect(self):
        # 慢读客户端会让 wfile.write 抛 TimeoutError（OSError 子类）；它与断管
        # 一样是死连接：关闭连接、request_log 记中断而不是从未送达的 200，
        # 异常不再上抛给 except Exception → api_error 对同一 socket 二次响应。
        def disconnected(handler):
            handler.server = self.server
            handler.send_response = lambda _: None
            handler.headers_common = lambda: None
            handler.send_header = lambda *_: None
            handler.end_headers = lambda: None
            handler.close_connection = False
            handler._request_id = "fixture-request"
            return handler

        handler = disconnected(object.__new__(Handler))
        writes = []
        handler.wfile = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(TimeoutError()))
        handler.api_error = lambda exc: writes.append("api_error")
        try:
            # Mirror the _do_GET/_do_POST boundary: respond() must swallow the
            # write failure, so api_error is never reached for this request.
            try:
                handler.respond({"ok": True})
            except Exception as exc:
                handler.api_error(exc)
        finally:
            del handler.api_error
        self.assertEqual(writes, [])
        self.assertTrue(handler.close_connection)
        self.assertEqual((handler._response_status, handler._response_error_code),
                         (500, "response_interrupted"))

        # api_error itself must also survive a dead socket instead of raising
        # into handle_error's stderr traceback for a second futile respond().
        Handler.api_error(handler, RuntimeError("boom"))
        self.assertTrue(handler.close_connection)
        self.assertEqual((handler._response_status, handler._response_error_code),
                         (500, "response_interrupted"))

        # Other OSError write failures keep the same closed-socket semantics.
        handler = disconnected(object.__new__(Handler))
        handler.wfile = SimpleNamespace(
            write=lambda _: (_ for _ in ()).throw(ConnectionResetError()))
        handler.respond({"error": "x"}, 404)
        self.assertTrue(handler.close_connection)
        self.assertEqual((handler._response_status, handler._response_error_code),
                         (500, "response_interrupted"))

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
        def stopping(*args, **kwargs):
            raise ValueError(message)
        self.runtime.retry = stopping
        status, _, body = self.request("/api/retry", "POST", {"task": "stopping-task"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": message, "error_code": "validation_error"})

    def test_configuration_validation_retains_fixed_guidance_without_echoing_inputs(self):
        from adaptive_rsi.config import validate
        self.runtime.configure = lambda values, *_, **__: validate(values)
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

    def test_revision_conflict_reports_the_same_structured_reason_for_config_and_routes(self):
        # 同一条 revision 冲突必须让两个写端点返回相同的可操作原因码，而不是一个带原因、一个只剩通用兜底。
        from adaptive_rsi.runtime import Runtime
        runtime = Runtime(Store(self.root / "revision-state"))
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        headers = {"Cookie": server.cookie + "=" + server.token, "Origin": server.local_origin,
                   "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"}

        def post(path, body):
            payload = json.dumps(body).encode()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", path, body=payload, headers=headers)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        try:
            stale = runtime.store.config_revision()
            runtime.configure({"schedule": {"weight": 3}})
            self.assertNotEqual(runtime.store.config_revision(), stale)
            secret = "private-revision-secret"
            status, config_body = post("/api/config", {"values": {"schedule": {"weight": 4}}, "revision": stale})
            self.assertEqual(status, 400)
            status, routes_body = post("/api/model-center/routes",
                                       {"models": {"planning": {"provider": secret}}, "revision": stale})
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(config_body), json.loads(routes_body))
            self.assertEqual(json.loads(routes_body)["reason_code"], "config_revision_conflict")
            self.assertIsNone(json.loads(routes_body)["field_path"])
            self.assertNotIn(secret.encode(), config_body + routes_body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            runtime.close()

    def test_feedback_overview_is_read_only_and_project_scoped(self):
        for candidate, project in [("feedback-p", "p"), ("feedback-q", "q")]:
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (candidate, project, "strategy", "stable", "{}", "{}", 1))
        self.store.event("monitor_feedback", "fixture", "p", None,
                         {"candidate": "feedback-p", "label": "false_positive", "source": "human:alice"})
        self.store.event("monitor_feedback", "fixture", "q", None,
                         {"candidate": "feedback-q", "label": "rejected", "source": "human:bob"})
        before = self.store.one("SELECT evidence,status FROM candidates WHERE id=?", ("feedback-p",))
        status, _, payload = self.request("/api/monitoring-feedback?project=p")
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertEqual(body["samples"], 1)
        self.assertEqual(body["sources"], {"human:alice": 1})
        self.assertEqual(body["labeled_candidates"], 1)
        self.assertFalse(body["automatic_threshold_changes"])
        self.assertTrue(body["read_only"])
        after = self.store.one("SELECT evidence,status FROM candidates WHERE id=?", ("feedback-p",))
        self.assertEqual(after["evidence"], before["evidence"])
        self.assertEqual(after["status"], before["status"])

    def test_task_run_history_pages_without_loss_or_duplicates(self):
        self.store.execute("INSERT INTO projects(id,name,path,created) VALUES ('p','P','/tmp/p',0)")
        self.store.execute("INSERT INTO tasks(id,project,title,created) VALUES ('t','p','Task',0)")
        # 二十次运行共享同一 started，用于验证游标必须同时比较 id 才能继续翻页。
        for index in range(60):
            started = 1000.0 if index < 20 else 1000.0 + index
            self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) "
                               "VALUES (?,?,?,?,?,?,?)",
                               ("run-%02d" % index, "t", "p", "failed", started, started + 1, "{}"))
        status, _, payload = self.request("/api/task-runs?task=t&project=p&limit=50")
        self.assertEqual(status, 200)
        first = json.loads(payload)
        self.assertEqual(set(first), {"runs", "has_more", "next_before"})
        self.assertEqual(len(first["runs"]), 50)
        self.assertTrue(first["has_more"])
        last = first["runs"][-1]
        self.assertEqual(last["id"], "run-10")
        self.assertEqual(first["next_before"], "%s:%s" % (last["started"], last["id"]))

        status, _, payload = self.request("/api/task-runs?task=t&project=p&limit=50&before=" +
                                          quote(first["next_before"], safe=""))
        self.assertEqual(status, 200)
        second = json.loads(payload)
        self.assertEqual(len(second["runs"]), 10)
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_before"])
        self.assertEqual([row["id"] for row in second["runs"]], ["run-%02d" % index for index in range(9, -1, -1)])

        identifiers = [row["id"] for row in first["runs"] + second["runs"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(set(identifiers), {"run-%02d" % index for index in range(60)})

    def test_latest_run_tie_is_consistent_across_read_models(self):
        self.store.execute("INSERT INTO projects(id,name,path,created) VALUES ('p','P',?,0)", (str(self.root),))
        # 两个任务分别覆盖 id 字典序大于/小于插入序：started 相同时，所有读模型都必须选中最后写入的 run。
        fixtures = {"tie-min": [("b1", {"feedback": {"state": "regressed"}}), ("a1", {"summary": "latest"})],
                    "tie-max": [("a2", {"feedback": {"state": "regressed"}}), ("b2", {"summary": "latest"})]}
        for task, runs in fixtures.items():
            self.store.execute("INSERT INTO tasks(id,project,source,title,state,created,updated) "
                               "VALUES (?,?,'user',?,'merged',0,0)", (task, "p", task))
            for identifier, result in runs:
                self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) "
                                   "VALUES (?,?,?,?,?,?,?)",
                                   (identifier, task, "p", "merged", 1000.0, 1001.0, json.dumps(result)))
        expected = {"tie-min": ("a1", ["a1", "b1"]), "tie-max": ("b2", ["b2", "a2"])}

        status, _, payload = self.request("/api/attention")
        self.assertEqual(status, 200)
        notices = {row["id"]: row for row in json.loads(payload)["notifications"]}

        from adaptive_rsi.dashboard import records
        state = records(self.store)
        tasks = {row["id"]: row for row in state["tasks"]}
        runs = {}
        for row in state["runs"]:
            runs.setdefault(row["task"], []).append(row["id"])

        for task, (latest, order) in expected.items():
            with self.subTest(task=task):
                self.assertEqual(notices[task]["run"], latest)
                # display_state 由同一次 latest run 派生；旧排序会取到 regressed 反馈。
                self.assertEqual(tasks[task]["display_state"], "merged")
                self.assertEqual(runs[task], order)
                status, _, payload = self.request("/api/task-runs?task=%s&project=p&limit=2" % task)
                self.assertEqual(status, 200)
                self.assertEqual([row["id"] for row in json.loads(payload)["runs"]], order)

    def test_backup_copy_runs_outside_dispatch_lock(self):
        from adaptive_rsi import storage_tools

        started, release = threading.Event(), threading.Event()
        responses, claims, pauses = [], [], []

        def slow_backup(store, **kwargs):
            started.set()
            release.wait(5)
            return {"id": "b" * 16, "bytes": 1, "created": 0.0}

        def claimed(self, project, config):
            claims.append(project)
            return None

        self.runtime.pause = lambda project, paused: pauses.append((project, paused))
        poster = threading.Thread(target=lambda: responses.append(self.request("/api/backup", "POST")), daemon=True)
        with patch.object(storage_tools, "backup", slow_backup), patch.object(Store, "claim", claimed):
            poster.start()
            self.assertTrue(started.wait(5), "备份桩未进入拷贝阶段")
            try:
                clock = time.monotonic()
                status, _, _ = self.request("/api/pause", "POST", {"project": "p", "paused": True})
                self.assertEqual(status, 200)
                self.assertLess(time.monotonic() - clock, 0.2, "备份期间 /api/pause 被 dispatch_lock 阻塞")
                self.assertEqual(pauses, [("p", True)])

                # 调度循环领取任务同样只需 dispatch_lock：备份期间必须能立即进入。
                def dispatch_once():
                    with self.runtime.dispatch_lock:
                        self.store.claim("p", {})

                dispatcher = threading.Thread(target=dispatch_once, daemon=True)
                clock = time.monotonic()
                dispatcher.start()
                dispatcher.join(0.2)
                self.assertFalse(dispatcher.is_alive(), "备份期间 dispatch_lock 仍被占用")
                self.assertLess(time.monotonic() - clock, 0.2)
                self.assertEqual(claims, ["p"])

                # 只读接口不与拷贝争锁，中位数不应因锁竞争变差。
                latencies = []
                for _ in range(3):
                    clock = time.monotonic()
                    self.assertEqual(self.request("/api/health")[0], 200)
                    latencies.append(time.monotonic() - clock)
                self.assertLess(sorted(latencies)[1], 0.2)
            finally:
                release.set()
            poster.join(5)
        self.assertFalse(poster.is_alive(), "备份请求未结束")
        status, _, payload = responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"id": "b" * 16, "bytes": 1, "created": 0.0})

    def test_backup_and_rehearsal_require_updates_ready_before_copying(self):
        from adaptive_rsi import storage_tools
        from adaptive_rsi.updates import UpdateInProgress

        checked = []

        def refuse():
            checked.append(True)
            raise UpdateInProgress("Software update in progress")

        def forbidden(*args, **kwargs):
            raise AssertionError("更新期间不得开始拷贝")

        self.runtime.updates.require_ready = refuse
        with patch.object(storage_tools, "backup", forbidden), \
                patch.object(storage_tools, "rehearse_restore", forbidden):
            status, _, payload = self.request("/api/backup", "POST")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(payload)["error_code"], "update_in_progress")
            status, _, payload = self.request("/api/rehearse-restore", "POST", {"id": self.identifier})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(payload)["error_code"], "update_in_progress")
        self.assertEqual(checked, [True, True])
        self.assertEqual(list(self.backups.glob("*.sqlite3")), [])

    def test_storage_copies_block_update_commit_until_the_request_finishes(self):
        from adaptive_rsi import storage_tools
        from adaptive_rsi.lifecycle import readiness
        from adaptive_rsi.updates import UpdateManager
        updates = object.__new__(UpdateManager)
        updates.runtime, updates.store = self.runtime, self.store
        self.runtime.updates.desktop_managed = True
        self.runtime.model_center = SimpleNamespace(busy=0, catalog_inflight=())
        self.runtime.auth_sessions = SimpleNamespace(busy=0)
        self.runtime.import_in_progress = False
        self.runtime.maintenance_lock = threading.Lock()
        self.runtime.wake = threading.Event()
        for endpoint, function, fails in [('/api/backup', 'backup', False),
                                           ('/api/rehearse-restore', 'rehearse_restore', True)]:
            with self.subTest(endpoint=endpoint):
                entered, release = threading.Event(), threading.Event()
                responses = []
                def copy(*args, **kwargs):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    if fails:
                        raise ValueError('fixture copy failure')
                    return {'id': self.identifier}
                poster = threading.Thread(target=lambda: responses.append(
                    self.request(endpoint, 'POST', {'id': self.identifier})), daemon=True)
                with patch.object(storage_tools, function, side_effect=copy):
                    poster.start()
                    self.assertTrue(entered.wait(2))
                    try:
                        prepared = readiness(self.runtime, 'prepare', '0.5.999')
                        self.assertFalse(prepared['ready'])
                        self.assertIn('background_operations', prepared['reasons'])
                        self.assertTrue(updates.work_busy())
                        with self.assertRaisesRegex(ValueError, 'active operations'):
                            readiness(self.runtime, 'commit', lease=prepared['barrier']['lease'])
                    finally:
                        release.set()
                        poster.join(3)
                self.assertFalse(poster.is_alive())
                self.assertTrue(readiness(self.runtime)['ready'])
                self.assertFalse(updates.work_busy())
                readiness(self.runtime, 'cancel', lease=prepared['barrier']['lease'])
                self.assertEqual(responses[0][0], 400 if fails else 200)

    def test_backup_endpoint_still_publishes_backup_file(self):
        status, _, payload = self.request("/api/backup", "POST")
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertEqual(set(result), {"id", "bytes", "created"})
        self.assertTrue((self.backups / (result["id"] + ".sqlite3")).is_file())

    def test_rehearsal_report_path_is_confined_to_state_directory(self):
        from adaptive_rsi import storage_tools

        snapshot = storage_tools.backup(self.store)
        victim = self.root / "victim.txt"
        victim.write_text("ORIGINAL CONTENT")
        cases = [
            # 状态目录之外的绝对路径与相对逃逸都不得写文件。
            str(victim),
            str(self.store.root / "reports" / ".." / ".." / "victim.txt"),
        ]
        for destination in cases:
            status, _, body = self.request("/api/rehearse-restore", "POST",
                                           {"id": snapshot["id"], "report": destination})
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"], "report_outside_state_directory")
        self.assertEqual(victim.read_text(), "ORIGINAL CONTENT")

        # 状态目录内的合法目标（reports/ 与 backups/ 下）仍然允许。
        for destination in (str(self.store.root / "reports" / "api-rehearsal.json"),
                            str(self.store.root / "backups" / "api-rehearsal.json")):
            status, _, body = self.request("/api/rehearse-restore", "POST",
                                           {"id": snapshot["id"], "report": destination})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["valid"])
            self.assertTrue(Path(destination).is_file())

    def test_rehearsal_default_report_path_rejects_symlinked_reports_dir(self):
        from adaptive_rsi import storage_tools

        snapshot = storage_tools.backup(self.store)
        # 默认路径分支（report 省略或为 true）经 reports/ 目录；它是指向状态目录
        # 外部的符号链接时同样不得写出界外文件。
        outside = self.root / "escaped-reports"
        outside.mkdir()
        reports_link = self.store.root / "reports"
        reports_link.symlink_to(outside, target_is_directory=True)
        escaped = outside / f"rehearsal-{snapshot['id']}.json"

        for body in ({"id": snapshot["id"]}, {"id": snapshot["id"], "report": True}):
            status, _, payload = self.request("/api/rehearse-restore", "POST", body)
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(payload)["error"], "report_outside_state_directory")
            self.assertFalse(escaped.exists())

    def test_rehearsal_report_read_rejects_symlinked_file(self):
        reports = self.store.root / "reports"
        reports.mkdir()
        # 合法报告仍按原样返回，字段结构不变。
        report = {"id": self.identifier, "valid": True, "source": "fixture"}
        report_file = reports / f"rehearsal-{self.identifier}.json"
        report_file.write_text(json.dumps(report), encoding="utf-8")
        status, _, body = self.request(f"/api/rehearsal?id={self.identifier}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), report)

        # 同一路径替换为指向状态目录外文件的符号链接后不得跟随读取。
        secret = self.root / "secret.txt"
        secret.write_text('{"leaked":"SECRET-FILE-CONTENT"}', encoding="utf-8")
        report_file.unlink()
        report_file.symlink_to(secret)
        for endpoint in ("/api/rehearsal", "/api/rehearse-restore"):
            with self.subTest(endpoint=endpoint):
                status, _, body = self.request(f"{endpoint}?id={self.identifier}")
                self.assertEqual(status, 400)
                self.assertNotIn(b"SECRET-FILE-CONTENT", body)
                self.assertEqual(json.loads(body)["error"], "Rehearsal report not found")

    def test_rehearsal_report_read_rejects_symlinked_reports_directory(self):
        outside = self.root / "outside-reports"
        outside.mkdir()
        (outside / f"rehearsal-{self.identifier}.json").write_text(
            '{"leaked":"SECRET-FILE-CONTENT"}', encoding="utf-8")
        (self.store.root / "reports").symlink_to(outside, target_is_directory=True)
        status, _, body = self.request(f"/api/rehearsal?id={self.identifier}")
        self.assertEqual(status, 400)
        self.assertNotIn(b"SECRET-FILE-CONTENT", body)

    def test_connection_gate_releases_permit_when_thread_spawn_fails(self):
        # Thread.start() 抛错时 process_request 必须退还 gate 名额，
        # 否则反复失败会耗尽信号量并让服务器永久拒接新连接。
        gate = self.server._connection_gate
        before = gate._value
        with patch("socketserver.ThreadingMixIn.process_request", side_effect=RuntimeError("spawn failed")):
            fake_request = MagicMock()
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    self.server.process_request(fake_request, ("127.0.0.1", 0))
        self.assertEqual(gate._value, before)


if __name__ == "__main__":
    unittest.main()
