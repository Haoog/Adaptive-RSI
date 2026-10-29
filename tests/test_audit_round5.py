"""Regression coverage for the second whole-project audit."""
import contextlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.evolution import Evolution
from adaptive_rsi.maintenance import Maintenance
from adaptive_rsi.native import Conversation, NativeAgent, ProviderInterrupted, ProviderUnavailable, WorkspaceTools
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.storage_tools import _archive, diagnostics
from adaptive_rsi.store import Store, uid
from adaptive_rsi.lifecycle import readiness
from adaptive_rsi.batching import execute as batch, preserve_acceptance
from adaptive_rsi.storage_tools import verify_backup, retain_backups, restore
from scripts.package_desktop import package_updater
from scripts.release import release_assets


class AuditRound5Test(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)

    def test_protocol_history_trims_outputs_preserving_call_ids(self):
        for protocol in ("responses", "gemini", "anthropic", "chat_completions"):
            with self.subTest(protocol=protocol):
                conversation = Conversation(protocol, "test", "instruction", [], "default")
                for index in range(10):
                    conversation.results([(str(index), "read_file", "x" * 20000)])
                before = len(json.dumps(conversation.history))
                last = json.dumps(conversation.history[-4:])
                conversation.trim_history(30000)
                self.assertLess(len(json.dumps(conversation.history)), before // 2)
                self.assertEqual(json.dumps(conversation.history[-4:]), last)
                self.assertIn("truncated" if protocol in ("responses", "gemini") else "trimmed", json.dumps(conversation.history))

    def test_deadline_blocks_second_tool_after_first_consumes_budget(self):
        clock, starts = [0.0], []
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "source.txt").write_text("hello")
        original = WorkspaceTools.execute

        def execute(tools, name, args):
            starts.append(clock[0])
            value = original(tools, name, args)
            clock[0] += 10
            return value

        def response(*args, **kwargs):
            clock[0] = 59
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": str(index), "function": {"name": "read_file", "arguments": json.dumps(
                    {"path": "source.txt", "start_line": 1, "end_line": 1})}} for index in range(2)]}}]}

        fake = SimpleNamespace(root=self.root, reserve_call=lambda *a: "call", record_usage=lambda *a: None,
                               event=lambda *a: None, execute=lambda *a: None)
        with patch("adaptive_rsi.native.Credentials") as credentials, \
                patch("adaptive_rsi.native.post_json", side_effect=response), \
                patch("adaptive_rsi.native.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(WorkspaceTools, "execute", execute):
            credentials.return_value.get.return_value = None
            with self.assertRaisesRegex(ProviderUnavailable, "timed out"):
                NativeAgent(fake).invoke("implementation", "test", workspace, "p", "r",
                    {"limits": {"max_output_mb": 1, "command_seconds": 60}},
                    {"model": "test", "effort": "default"},
                    {"id": "test", "protocol": "chat_completions", "base_url": "https://invalid.example"})
        self.assertEqual(starts, [59])

    def test_interrupted_provider_keeps_recovery_metadata(self):
        cause = ProviderUnavailable("limited", "rate_limit")
        cause.retry_after, cause.provider_id = 120, "connection"
        wrapped = ProviderInterrupted(cause)
        self.assertEqual((wrapped.retry_after, wrapped.provider_id), (120, "connection"))

    def test_duplicate_tool_ids_are_rejected_before_any_execution(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "duplicate", "function": {"name": "write_file", "arguments":
             json.dumps({"path": "out.txt", "content": "unexpected"})}} for _ in range(2)]}}]}
        fake = SimpleNamespace(root=self.root, reserve_call=lambda *a: "call", record_usage=lambda *a: None,
                               event=lambda *a: None, execute=lambda *a: None)
        with patch("adaptive_rsi.native.Credentials") as credentials, \
                patch("adaptive_rsi.native.post_json", return_value=response), \
                patch.object(WorkspaceTools, "execute") as execute:
            credentials.return_value.get.return_value = None
            with self.assertRaisesRegex(RuntimeError, "duplicate tool call IDs"):
                NativeAgent(fake).invoke("implementation", "test", workspace, "p", "r",
                    {"limits": {"max_output_mb": 1, "command_seconds": 60}},
                    {"model": "test", "effort": "default"},
                    {"id": "test", "protocol": "chat_completions", "base_url": "https://invalid.example"})
        execute.assert_not_called()

    def test_reused_tool_id_in_later_turn_does_not_repeat_execution(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "same", "function": {"name": "read_file", "arguments":
             json.dumps({"path": "source.txt", "start_line": 1, "end_line": 1})}}]}}]}
        fake = SimpleNamespace(root=self.root, reserve_call=lambda *a: "call", record_usage=lambda *a: None,
                               event=lambda *a: None, execute=lambda *a: None)
        with patch("adaptive_rsi.native.Credentials") as credentials, \
                patch("adaptive_rsi.native.post_json", return_value=response), \
                patch.object(WorkspaceTools, "execute", return_value={"content": "ok"}) as execute:
            credentials.return_value.get.return_value = None
            with self.assertRaisesRegex(RuntimeError, "reused an earlier tool call ID"):
                NativeAgent(fake).invoke("implementation", "test", workspace, "p", "r",
                    {"limits": {"max_output_mb": 1, "command_seconds": 60}},
                    {"model": "test", "effort": "default"},
                    {"id": "test", "protocol": "chat_completions", "base_url": "https://invalid.example"})
        self.assertEqual(execute.call_count, 1)

    def test_database_failure_does_not_escape_scheduler_error_handler(self):
        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("readonly database")
        fake = SimpleNamespace(_scheduler_failures_lock=threading.Lock(), _scheduler_failures={},
                               store=SimpleNamespace(event=unavailable))
        with self.assertLogs("adaptive_rsi.runtime", "ERROR"):
            with Runtime.scheduler_scope(fake, "loop"):
                raise sqlite3.OperationalError("readonly database")

    def test_cleanup_advances_past_already_cleaned_tasks(self):
        for index in range(201):
            self.store.execute("INSERT INTO tasks(id,state,updated) VALUES (?,'completed',0)", (str(index),))
        self.store.put_setting("triage-state:200", {"legacy": True})
        fake = SimpleNamespace(store=self.store, stop=threading.Event(), dispatch_lock=threading.RLock(),
            scheduler_scope=lambda *a, **k: contextlib.nullcontext(), config=lambda p: {"providers": []},
            model_center=SimpleNamespace(cleanup_credentials=lambda: None, key=lambda *a: "unused"))
        Maintenance.maintenance(fake)
        self.assertIsNone(self.store.setting("triage-state:200"))

    def test_cleanup_preserves_invalid_evidence_and_continues(self):
        self.store.execute("INSERT INTO runs(id,state,ended,result) VALUES ('bad','superseded',0,'{')")
        for key in ("run-baseline:bad", "task-proof:bad", "provider-health:bad", "issue-comments:bad"):
            self.store.execute("INSERT INTO settings(key,value) VALUES (?,?)", (key, "{"))
        self.store.execute("INSERT INTO settings(key,value) VALUES ('task-proof:old','{\"at\":0}')")
        fake = SimpleNamespace(store=self.store, stop=threading.Event(), dispatch_lock=threading.RLock(),
            scheduler_scope=lambda *a, **k: contextlib.nullcontext(), config=lambda p: {"providers": []},
            model_center=SimpleNamespace(cleanup_credentials=lambda: None, key=lambda *a: "unused"))
        Maintenance.maintenance(fake)
        self.assertIsNone(self.store.one("SELECT value FROM settings WHERE key='task-proof:old'"))
        self.assertIsNotNone(self.store.one("SELECT value FROM settings WHERE key='run-baseline:bad'"))
        self.assertIsNotNone(self.store.one("SELECT value FROM settings WHERE key='task-proof:bad'"))

    def test_diagnostics_survives_bad_history(self):
        self.store.execute("INSERT INTO runs(id,result) VALUES ('bad','{')")
        report = diagnostics(self.store)
        self.assertEqual(report["invalid_evidence"], 1)

    def test_archive_preserves_original_tempfile_error_and_removes_snapshot(self):
        (self.store.root / "backups").mkdir()
        snapshot = self.store.root / "backups/snapshot.sqlite3"
        snapshot.write_bytes(b"snapshot")
        with patch("adaptive_rsi.storage_tools.backup", return_value={"id": "snapshot", "bytes": 8}), \
                patch("adaptive_rsi.storage_tools.tempfile.mkstemp", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                _archive(self.store, 10)
        self.assertFalse(snapshot.exists())

    def test_monitor_does_not_resurrect_concurrently_rolled_back_candidate(self):
        self.store.execute("INSERT INTO candidates(id,project,status,content,evidence) VALUES ('c','p','stable','{}','{}')")
        self.store.execute("INSERT INTO runs(id,project,strategy,config) VALUES ('r','p','[{\"id\":\"c\"}]','{}')")
        evolution = Evolution(SimpleNamespace(store=self.store, config=lambda p: {"evolution": {"monitor_tasks": 10}}))
        original = self.store.query
        def query(sql, args=()):
            rows = original(sql, args)
            if sql == "SELECT * FROM candidates WHERE project=? AND status='stable'":
                evolution.rollback("c")
            return rows
        with patch.object(self.store, "query", side_effect=query):
            evolution.monitor("p", True, {"id": "r"})
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='c'")["status"], "rolled_back")

    def test_search_reports_match_limit(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "source.txt").write_text("needle\n" * 101)
        tools = WorkspaceTools(workspace, "planning", self.root, {"limits": {"command_seconds": 60}}, None, ())
        result = tools.execute("search", {"text": "needle"})
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["matches"]), 100)

    def test_desktop_update_barrier_waits_for_active_work_and_recovers(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        runtime.updates.desktop_managed = True
        self.store.execute("INSERT INTO runs(id,state,started) VALUES ('run','executing',1)")
        result = readiness(runtime, "prepare", "9.0.0")
        lease = result["barrier"]["lease"]
        self.assertFalse(result["ready"])
        self.assertTrue(runtime.updates.is_busy())
        with self.assertRaisesRegex(ValueError, "active"):
            readiness(runtime, "commit", lease=lease)
        self.store.execute("UPDATE runs SET ended=2 WHERE id='run'")
        self.assertEqual(readiness(runtime, "commit", lease=lease)["barrier"]["phase"], "installing")
        with self.assertRaisesRegex(ValueError, "lease"):
            readiness(runtime, "cancel", lease="incorrect")
        self.assertIsNone(readiness(runtime, "cancel", lease=lease)["barrier"])
        self.assertFalse(runtime.updates.is_busy())

    def test_batch_group_preserves_acceptance_and_overrides(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('p','p','/tmp/p','{}',1,0)")
        one = self.store.add_task("p", "goal", "one", "One", acceptance=["One passes"], overrides={"review": {"block_on": "high"}})
        two = self.store.add_task("p", "goal", "two", "Two", acceptance=["Two passes"], overrides={"review": {"block_on": "high"}})
        first = batch(runtime, "p", [one, two], "group", "same", preview=False)
        second = batch(runtime, "p", [one, two], "group", "same", preview=False)
        self.assertEqual(first, second)
        combined = self.store.one("SELECT * FROM tasks WHERE id=?", (first["id"],))
        self.assertEqual(json.loads(combined["acceptance"]), ["One passes", "Two passes"])
        self.assertEqual(json.loads(combined["overrides"]), {"review": {"block_on": "high"}})
        plan = {"decision": "ready", "acceptance": ["One passes", "Planner check"]}
        preserve_acceptance(combined, plan)
        self.assertEqual(plan["acceptance"], ["One passes", "Planner check", "Two passes"])

        restored = batch(runtime, "p", [first["id"]], "ungroup", "restore", preview=False)
        self.assertEqual([item["state"] for item in restored["receipts"]], ["restored", "restored"])
        self.assertEqual(batch(runtime, "p", [first["id"]], "ungroup", "restore", preview=False), restored)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (one,))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (two,))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (first["id"],))["state"], "cancelled")

    def test_background_operation_receipts_persist(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        submitted = runtime.operations.start("backup", {})
        for _ in range(100):
            result = runtime.operations.get(submitted["id"])
            if result["ended"] is not None:
                break
            threading.Event().wait(0.01)
        self.assertEqual(result["state"], "completed")
        self.assertTrue((self.store.root / "backups" / (result["result"]["id"] + ".sqlite3")).is_file())

    def test_batch_approval_rejects_changed_preview(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('p','p','/tmp/p','{}',1,0)")
        task = self.store.add_task("p", "goal", "one", "One")
        self.store.execute("INSERT INTO runs(id,task,project,state,started,result) VALUES (?,?,?,'awaiting_approval',1,?)",
                           ("run", task, "p", json.dumps({"files": ["first.py"], "risk": "medium", "patch_hash": "old"})))
        preview = batch(runtime, "p", [task], "approve")
        self.assertEqual(preview["candidates"][0]["files"], ["first.py"])
        self.store.execute("UPDATE runs SET result=? WHERE id='run'", (json.dumps({"files": ["second.py"], "risk": "high", "patch_hash": "new"}),))
        with patch.object(runtime, "deliver") as deliver, self.assertRaisesRegex(ValueError, "preview changed"):
            batch(runtime, "p", [task], "approve", "request", preview=False, expected=preview["revision"])
        deliver.assert_not_called()

    def test_backup_progress_does_not_write_source_database(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        reported, resume = threading.Event(), threading.Event()

        def copy(store, cancel, progress):
            progress(10, 20)
            reported.set()
            if not resume.wait(5):
                raise RuntimeError("test timed out")
            return {"id": "fixture"}

        try:
            with patch("adaptive_rsi.storage_tools.backup", side_effect=copy):
                operation = runtime.operations.start("backup", {})["id"]
                self.assertTrue(reported.wait(5))
                self.assertEqual(runtime.operations.get(operation)["result"], {"pages_done": 10, "pages_total": 20})
                self.assertEqual(json.loads(self.store.one("SELECT result FROM operations WHERE id=?", (operation,))["result"]), {})
                resume.set()
                for _ in range(100):
                    result = runtime.operations.get(operation)
                    if result["ended"] is not None:
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(result["result"], {"id": "fixture"})
        finally:
            resume.set()

    def test_backup_verification_and_bounded_retention(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        saved = runtime.operations.start("backup", {})
        for _ in range(100):
            current = runtime.operations.get(saved["id"])
            if current["ended"] is not None:
                break
            threading.Event().wait(0.01)
        path = self.store.root / "backups" / (current["result"]["id"] + ".sqlite3")
        self.assertTrue(verify_backup(path)["valid"])
        for _ in range(3):
            backup_path = self.store.root / "backups" / (uid() + ".sqlite3")
            backup_path.write_bytes(path.read_bytes())
        result = retain_backups(self.store, max_count=2, max_age_days=90, now=time.time() + 86400)
        self.assertEqual(result["kept"], 2)
        self.assertEqual(len(result["removed"]), 2)

    def test_restore_accepts_legacy_v1_backup_without_migration_ledger(self):
        source = self.root / "legacy-v1.sqlite3"
        with sqlite3.connect(source) as db:
            db.executescript("""
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,path TEXT,config TEXT,paused INTEGER,created REAL);
                CREATE TABLE tasks(id TEXT PRIMARY KEY,project TEXT,source TEXT,fingerprint TEXT,title TEXT,body TEXT,
                    evidence TEXT,acceptance TEXT,state TEXT,priority INTEGER,attempts INTEGER,overrides TEXT,
                    available REAL,created REAL,updated REAL,urgency INTEGER,infra_retries INTEGER,budget_waits INTEGER);
                CREATE TABLE runs(id TEXT PRIMARY KEY,task TEXT,project TEXT,state TEXT,config TEXT,strategy TEXT,
                    base TEXT,path TEXT,calls INTEGER,tokens INTEGER,started REAL,ended REAL,result TEXT);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                PRAGMA user_version=1;
            """)
        restored = restore(source, self.root / "restored")
        self.assertTrue(restored["restored"])
        checked = Store(self.root / "restored")
        self.addCleanup(checked.close)
        self.assertEqual([row["version"] for row in checked.schema_migrations()], [1, 2, 3, 4, 5, 6, 7])

    def test_snapshot_and_config_revision_prevent_cross_tab_overwrite(self):
        with self.store.snapshot():
            first = self.store.one("SELECT count(*) AS n FROM schema_migrations")["n"]
            second = self.store.one("SELECT count(*) AS n FROM schema_migrations")["n"]
        self.assertEqual(first, second)
        self.assertEqual(self.store.config_revision(), 0)
        self.assertEqual(self.store.bump_config_revision(expected=0), 1)
        with self.assertRaisesRegex(ValueError, "changed in another window"):
            self.store.bump_config_revision(expected=0)

    def test_agent_contract_reports_bootstrap_csrf_before_state(self):
        from adaptive_rsi.server import Server
        fake = SimpleNamespace(store=self.store, assets={}, model_center=SimpleNamespace(busy=0),
                               auth_sessions=SimpleNamespace(busy=0), import_in_progress=False)
        server = Server(fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(server.origin + "/api/contract",
                                         headers={"Cookie": server.cookie + "=" + server.token})
        with urllib.request.urlopen(request, timeout=3) as response:
            body = json.load(response)
        self.assertEqual(body["csrf"], server.csrf)
        self.assertEqual(body["descriptor"]["api_version"], 2)

    def test_project_archive_retains_rows_and_rejects_invalid_confirmation(self):
        runtime = Runtime(self.store)
        self.addCleanup(runtime.close)
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) VALUES ('p','P','/tmp/p','{}',0,0)")
        task = self.store.add_task("p", "goal", "kept", "Keep history")
        with self.assertRaisesRegex(ValueError, "confirmation"):
            runtime.archive_project("p", "wrong")
        self.assertTrue(runtime.archive_project("p", "archive-project")["archived"])
        self.assertEqual(self.store.one("SELECT name FROM projects WHERE id='p'")["name"], "P")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "cancelled")

    def test_updater_release_manifest_and_incomplete_assets(self):
        bundle, output = self.root / "bundle", self.root / "output"
        bundle.mkdir()
        update = bundle / "Adaptive RSI.app.tar.gz"
        update.write_bytes(b"example bundle")
        Path(str(update) + ".sig").write_text("example-signature\n")
        with patch("scripts.package_desktop.subprocess.run") as verify:
            feed = package_updater(bundle, output, "1.2.3")
        verify.assert_called_once()
        self.assertEqual(feed["version"], "1.2.3")
        for name in ["adaptive-rsi-1.2.3-plugin.zip", "adaptive-rsi-1.2.3-plugin.zip.sha256",
                     "adaptive_rsi-1.2.3-py3-none-any.whl", "adaptive-rsi-1.2.3-macos-arm64-adhoc.dmg",
                     "adaptive-rsi-1.2.3-macos-arm64-adhoc.dmg.sha256"]:
            (output / name).write_bytes(b"fixture")
        self.assertIn("desktop-latest.json", [path.name for path in release_assets(output, "1.2.3")])
        (output / "adaptive-rsi-1.2.3-macos-arm64.app.tar.gz.sig").unlink()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            release_assets(output, "1.2.3")


if __name__ == "__main__":
    unittest.main()
