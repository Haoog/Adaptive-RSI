"""Behavioral regressions from the October whole-project audit."""
import json
import os
import sys
import tempfile
import threading
import time
import random
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.adapters import detect
from adaptive_rsi.config import validate
from adaptive_rsi.context import code_map
from adaptive_rsi.credentials import CredentialError, Credentials
from adaptive_rsi.environment import capture
from adaptive_rsi.gitops import git
from adaptive_rsi.native import ArgumentsError, WorkspaceTools
from adaptive_rsi.process import run_process
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


class AuditRegressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def runtime(self):
        runtime = Runtime(Store(self.root / "state"), FakeAgent())
        self.addCleanup(runtime.close)
        return runtime

    def test_private_archive_preserves_history_and_restorable_database(self):
        import gzip
        import sqlite3
        from adaptive_rsi.storage_tools import archive
        store = Store(self.root / "archive-state")
        self.addCleanup(store.close)
        store.event("retained", "evidence")
        report = archive(store, 1)
        self.assertFalse(report["history_deleted"])
        self.assertEqual(Path(report["path"]).stat().st_mode & 0o777, 0o600)
        target = self.root / "archived.sqlite3"
        target.write_bytes(gzip.decompress(Path(report["path"]).read_bytes()))
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT message FROM events").fetchone()[0], "evidence")
        self.assertEqual(store.one("SELECT count(*) AS n FROM events")["n"], 1)
        (store.root / "archives" / "full").write_bytes(b"x" * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "capacity reached"):
            archive(store, 1)

    def test_fast_output_cannot_bypass_limit(self):
        result = run_process([sys.executable, "-c", "print('x' * 2000000)"], output_mb=1)
        self.assertEqual(result["error_kind"], "output_limit")

    def test_long_usage_history_preserves_totals_and_fills_months(self):
        from adaptive_rsi.usage_report import report
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        store.execute("INSERT INTO calls(id,project,run,state,tokens,created) VALUES ('old','p',NULL,'completed',123,1)")
        data = report(store, {"start": ["0"], "end": [str(200 * 86400)]})
        self.assertEqual(data["summary"]["tokens"], 123)
        self.assertEqual(sum(row["tokens"] for row in data["daily"]), 123)
        self.assertTrue(any(row["calls"] == 0 for row in data["daily"]))

    def test_cancellation_terminates_a_real_blocked_http_request(self):
        from adaptive_rsi.native import post_json
        from adaptive_rsi.process import Cancelled
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                entered.set()
                release.wait(5)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(post_json, "http://127.0.0.1:" + str(server.server_port), {}, {}, 30, 4096, cancelled.is_set)
                self.assertTrue(entered.wait(3))
                began = time.monotonic()
                cancelled.set()
                with self.assertRaises(Cancelled):
                    future.result(timeout=2)
                self.assertLess(time.monotonic() - began, 1)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_packaged_transport_uses_the_internal_engine_entrypoint(self):
        from adaptive_rsi.native import post_json
        result = {"error_kind": None, "error": None, "exit_code": 0,
                  "stdout": json.dumps({"ok": True, "response": {"content": "test"}})}
        with patch.object(sys, "frozen", True, create=True), patch("adaptive_rsi.process.run_process", return_value=result) as process:
            self.assertEqual(post_json("http://127.0.0.1", {}, {}, 30, 4096, lambda: False), {"content": "test"})
        self.assertEqual(process.call_args.args[0], [sys.executable, "internal-native-transport"])

    def test_native_write_scope_rejects_before_mutation(self):
        tools = WorkspaceTools(self.root, "implementation", self.root / "state", validate({}), None, [])
        tools.scope = ["allowed"]
        with self.assertRaises(ValueError):
            tools.execute("write_file", {"path": "other.py", "content": "no"})
        self.assertFalse((self.root / "other.py").exists())
        tools.execute("write_file", {"path": "allowed/yes.py", "content": "yes"})
        self.assertTrue((self.root / "allowed/yes.py").is_file())

    @unittest.skipUnless(sys.platform == "darwin", "real Seatbelt write boundary")
    def test_command_cannot_write_outside_host_plan_scope(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "allowed").mkdir()
        (workspace / "outside").write_text("original")
        config = validate({"sandbox": {"backend": "seatbelt"}})
        tools = WorkspaceTools(workspace, "implementation", self.root / "state", config, None, [])
        tools.scope = ["allowed"]
        for path, expected in [("allowed/new", 0), ("outside", 1), ("new-outside", 1)]:
            result = tools.execute("run_command", {"cwd": ".", "argv": [sys.executable, "-c",
                "from pathlib import Path;Path(" + repr(path) + ").write_text('changed')"]})
            self.assertEqual(bool(result["exit_code"]), bool(expected))
        self.assertEqual((workspace / "outside").read_text(), "original")
        self.assertFalse((workspace / "new-outside").exists())

    def test_invalid_nested_historical_evidence_is_isolated(self):
        from adaptive_rsi.dashboard import records
        from adaptive_rsi.telemetry import quality_summary, workflow_metrics
        from adaptive_rsi.store import decode_evidence
        runtime = self.runtime()
        project = runtime.add_project(fixture(self.root / "project"), enabled=True)
        runtime.goal(project, "historical evidence")
        run = runtime.store.claim(project, runtime.config(project))
        runtime.store.transition(run["id"], "failed", {}, True)
        for evidence in [{"review": None}, {"evaluation": {"checks": []}}, {"review": {"findings": [None]}}, {"trials": [None]}, {"trials": [{"case": []}]}]:
            raw = json.dumps(evidence)
            runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (raw, run["id"]))
            self.assertIn("evidence_error", decode_evidence(raw))
            data = records(runtime.store, project)
            self.assertTrue(data["runs"][0]["evidence_error"])
            self.assertEqual(quality_summary(runtime.store, project)[0]["passed"], 0)
            self.assertEqual(workflow_metrics(runtime.store, project)["accepted"], 0)
            self.assertEqual(runtime.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"], raw)

    def test_referenced_retired_credentials_do_not_starve_later_cleanup(self):
        runtime = self.runtime()
        center = runtime.model_center
        pending = {format(index, "032x"): ["session"] for index in range(40)}
        runtime.store.put_setting("credential-cleanup-pending", pending)
        protected = {(ref, "session") for ref in list(pending)[:32]}
        with patch.object(center, "_credential_references", return_value=protected), patch.object(center.credentials, "delete") as deleted:
            center.cleanup_credentials()
        self.assertEqual(deleted.call_count, 8)
        self.assertEqual(len(runtime.store.setting("credential-cleanup-pending")), 32)

    def test_randomized_stop_transitions_never_resume_cancelled_work(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        store.execute("INSERT INTO projects VALUES ('p','test','/fixture','{}',0,0)")
        rng = random.Random(20261027)
        for index in range(100):
            task = store.add_task("p", "goal", str(index), "test")
            run = store.claim("p", validate({}))
            stopped = rng.choice([True, False])
            cancelled = rng.choice([True, False])
            if stopped:
                store.put_setting("run-stop:" + run["id"], True)
            if cancelled:
                store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task,))
            state = rng.choice(["completed", "failed", "evaluating", "awaiting_approval"])
            store.transition(run["id"], state, {}, state != "evaluating")
            actual = store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"]
            self.assertEqual(actual, "cancelled" if cancelled else "interrupted" if stopped else state)
            if actual == "evaluating":
                store.transition(run["id"], "completed", {}, True)

    def test_resource_capacity_and_benchmark_exclusion(self):
        from adaptive_rsi.resources import ExecutionResources
        resources = ExecutionResources(capacity=2)
        entered, release = threading.Event(), threading.Event()
        def reader():
            with resources.acquire():
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(entered.wait(1))
        exclusive = threading.Event()
        def benchmark():
            with resources.acquire(exclusive=True):
                with resources.acquire():
                    exclusive.set()
        other = threading.Thread(target=benchmark)
        other.start()
        self.assertFalse(exclusive.wait(0.1))
        release.set()
        thread.join(2)
        other.join(2)
        self.assertTrue(exclusive.is_set())

    def test_repeated_store_and_tool_use_retains_bounded_resources(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        tools = WorkspaceTools(self.root, "planning", self.root / "state", validate({}), None, [])
        folder = self.root / "sources"
        folder.mkdir()
        for index in range(20):
            (folder / (str(index) + ".py")).write_text("x" * 300000)
        for _ in range(20):
            store.query("SELECT 1")
            tools.execute("search", {"text": "missing-audit-text"})
        self.assertLessEqual(store._pool.qsize(), 8)
        self.assertLessEqual(tools._text_cache_bytes, 4 * 1024 * 1024)

    def test_dependency_fingerprint_frames_paths_and_records_modes(self):
        repo = fixture(self.root / "project")
        (repo / "seed").mkdir()
        file = repo / "seed/a"
        file.write_text("Xseed/bY")
        config = validate({"environment": {"seed_paths": ["seed"]}})
        first = capture(repo, self.root / "one", config)
        file.write_text("X")
        (repo / "seed/b").write_text("Y")
        second = capture(repo, self.root / "two", config)
        self.assertNotEqual(first["sha256"], second["sha256"])
        file.chmod(0o755)
        third = capture(repo, self.root / "three", config)
        self.assertNotEqual(second["sha256"], third["sha256"])

    def test_malformed_node_manifests_do_not_abort_detection(self):
        for value in [[], {"scripts": []}, {"scripts": {"test": 123}}]:
            with self.subTest(value=value):
                (self.root / "package.json").write_text(json.dumps(value))
                self.assertEqual(detect(self.root)["checks"], [])

    def test_schema_mistakes_are_repairable_tool_errors(self):
        tool = WorkspaceTools(self.root, "planning", self.root / "state", validate({}), None, [])
        with self.assertRaises(ArgumentsError):
            tool.execute("read_file", {"path": "source.py", "start_line": "1", "end_line": 1})

    def test_close_does_not_repool_borrowed_connections(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        with store.connect():
            store.close()
        self.assertEqual(store._pool.qsize(), 0)
        self.assertEqual(store.query("SELECT 1 AS n"), [{"n": 1}])

    def test_recovery_settles_reserved_calls(self):
        runtime = self.runtime()
        project = runtime.add_project(fixture(self.root / "project"), enabled=True)
        runtime.goal(project, "audit goal")
        config = runtime.config(project)
        run = runtime.store.claim(project, config)
        call = runtime.store.reserve_call(project, run["id"], "planning", config["models"]["planning"], config)
        runtime.store.recover()
        self.assertEqual(runtime.store.one("SELECT state FROM calls WHERE id=?", (call,))["state"], "interrupted")

    def test_unicode_and_untracked_files_enter_context_index(self):
        repo = fixture(self.root / "project")
        path = repo / "中文.py"
        path.write_text("def unique_audit_identifier(): pass\n")
        git(repo, "add", path.name)
        (repo / "new.py").write_text("def another_audit_identifier(): pass\n")
        result = json.dumps(code_map(repo, "unique_audit_identifier another_audit_identifier"), ensure_ascii=False)
        self.assertIn("中文.py", result)
        self.assertIn("new.py", result)
        self.assertIn("unique_audit_identifier", result)

    def test_global_settings_isolate_preexisting_invalid_project(self):
        runtime = self.runtime()
        project = runtime.add_project(fixture(self.root / "project"))
        runtime.store.execute("UPDATE projects SET config=? WHERE id=?",
                              ('{"schedule":{"timezone":"Invalid/Audit"}}', project))
        runtime.configure({"triage": {"policy": "Audit policy"}})
        self.assertEqual(runtime.config()["triage"]["policy"], "Audit policy")

    def test_check_names_and_pause_require_correct_types(self):
        with self.assertRaises(ValueError):
            validate({"checks": [{"name": 123, "argv": ["true"]}]})
        with self.assertRaises(ValueError):
            self.runtime().pause("unknown", "false")

    def test_update_startup_failure_releases_admission(self):
        manager = self.runtime().updates
        manager.shutdown = lambda: None
        with patch.object(manager, "status", side_effect=OSError("audit")):
            with self.assertRaises(OSError):
                manager.apply_async()
        self.assertFalse(manager.lock.locked())
        with patch.object(manager, "status", return_value={"state": "available", "version": "9.0.0"}), patch(
                "adaptive_rsi.updates.threading.Thread.start", side_effect=RuntimeError("audit")):
            with self.assertRaises(RuntimeError):
                manager.apply_async()
        self.assertFalse(manager.lock.locked())
        self.assertFalse(manager.installing)

    def test_credential_file_symlink_fifo_and_oversize_are_rejected(self):
        credentials = Credentials(self.root)
        reference = "a" * 32
        provider = {"credential": "file", "secret_ref": reference}
        credentials.directory.mkdir()
        path = credentials.directory / reference
        (self.root / "key").write_text("test-only")
        path.symlink_to(self.root / "key")
        with self.assertRaises(CredentialError):
            credentials.get(provider)
        self.assertEqual(credentials.status(provider), "missing")
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(CredentialError):
            credentials.get(provider)
        path.unlink()
        path.write_text("x" * 8193)
        path.chmod(0o600)
        with self.assertRaises(CredentialError):
            credentials.get(provider)
