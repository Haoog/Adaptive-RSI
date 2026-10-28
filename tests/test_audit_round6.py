"""Regressions for the third audit: exercise failures at their production seams."""
import copy
import errno
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from adaptive_rsi.batching import execute
from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.credentials import Credentials
from adaptive_rsi.evaluator import junit_test_summary
from adaptive_rsi.evaluator import structured_test_summary
from adaptive_rsi.evolution import paired_summary, Evolution
from adaptive_rsi.native import WorkspaceTools, execute_tools
from adaptive_rsi.provider_health import ModelFailure, ProviderHealth, ProviderPaused, connection_key
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.storage_tools import backup, retain_backups, verify_backup
from adaptive_rsi.store import Store
from adaptive_rsi.usage import native_usage
from adaptive_rsi.workflow import change_kind
from scripts.check_api_contract import check_keys


class AuditRound6Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','audit',?,'{}',1,?)",
                           (str(self.root / "project"), time.time()))

    def server(self):
        server = Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(close)
        return server

    def get(self, server, path):
        request = urllib.request.Request(server.local_origin + "/api/" + path,
                                         headers={"Cookie": server.cookie + "=" + server.token})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_failed_connection_open_returns_capacity(self):
        self.store.close()
        available = self.store._connection_capacity._value
        for _ in range(3):
            with patch.object(self.store, "_new_connection", side_effect=sqlite3.OperationalError("injected")):
                with self.assertRaises(sqlite3.OperationalError):
                    with self.store.connect():
                        pass
        self.assertEqual(self.store._connection_capacity._value, available)
        self.assertEqual(self.store._metrics["borrowed"], 0)
        self.assertEqual(self.store.one("SELECT 1 AS ok")["ok"], 1)

    def test_failed_connection_prepare_returns_capacity(self):
        available = self.store._connection_capacity._value
        with patch.object(self.store, "_prepare", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.connect():
                    pass
        self.assertEqual(self.store._connection_capacity._value, available)
        self.assertEqual(self.store._metrics["borrowed"], 0)

    def test_connection_journal_initialization_failure_closes_open_handle(self):
        from unittest.mock import Mock
        self.store.close()
        opened = Mock()
        opened.execute.side_effect = sqlite3.OperationalError("journal unavailable")
        available = self.store._connection_capacity._value
        with patch("adaptive_rsi.store.sqlite3.connect", return_value=opened):
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.connect():
                    pass
        opened.close.assert_called_once()
        self.assertEqual(self.store._connection_capacity._value, available)

    def test_retention_keeps_last_expired_valid_backup(self):
        item = backup(self.store)
        path = self.store.root / "backups" / (item["id"] + ".sqlite3")
        expired = time.time() - 100 * 86400
        os.utime(path, (expired, expired))
        self.assertEqual(retain_backups(self.store)["kept"], 1)
        self.assertTrue(verify_backup(path)["valid"])

    def test_corrupt_new_backup_cannot_displace_valid_backup(self):
        item = backup(self.store)
        valid = self.store.root / "backups" / (item["id"] + ".sqlite3")
        recent = time.time() - 60
        os.utime(valid, (recent, recent))
        (valid.parent / "0000000000000000.sqlite3").write_bytes(b"corrupt")
        retain_backups(self.store, max_count=1)
        self.assertTrue(valid.is_file())
        self.assertTrue(verify_backup(valid)["valid"])

    def test_backup_validation_rejects_input_symlink(self):
        item = backup(self.store)
        link = self.root / "linked.sqlite3"
        link.symlink_to(self.store.root / "backups" / (item["id"] + ".sqlite3"))
        with self.assertRaises(ValueError):
            verify_backup(link)

    def test_half_open_lease_admits_one_attempt(self):
        config = copy.deepcopy(DEFAULTS)
        config["recovery"]["provider_retry_minutes"] = 1
        self.store.put_setting(connection_key(config, "openai", "p"), {
            "paused": True, "failures": 3, "code": "timeout", "at": time.time() - 120})
        health = ProviderHealth(self.store)
        health.require(config, "openai", "p", "first")
        with self.assertRaises(ProviderPaused):
            health.require(config, "openai", "p", "second")

    def test_late_success_cannot_clear_newer_failure(self):
        config = copy.deepcopy(DEFAULTS)
        health = ProviderHealth(self.store)
        old = health.require(config, "openai", "p")
        newer = health.require(config, "openai", "p")
        error = ModelFailure("429", "rate_limit")
        error.retry_after = 120
        health.record(config, "openai", "p", error, attempt=newer)
        health.record(config, "openai", "p", attempt=old)
        self.assertGreater(health.status(config, "openai", "p")["retry_at"], time.time())

    def test_diagnostics_never_calls_a_model(self):
        report = self.runtime.model_center.diagnose("openai", "audit")
        self.assertFalse(report["paid_probe"])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM calls")["n"], 0)

    def test_archived_project_rejects_resume_and_new_goals(self):
        self.runtime.archive_project("p", "archive-project")
        with self.assertRaises(ValueError):
            self.runtime.pause("p", False)
        with self.assertRaises(ValueError):
            self.runtime.goal("p", "must not run")
        self.assertEqual(self.runtime.project("p")["paused"], 1)

    def test_effective_revision_changes_with_inherited_globals(self):
        before = self.store.config_revision("p")
        self.runtime.configure({"schedule": {"light_minutes": 61}})
        self.assertNotEqual(self.store.config_revision("p"), before)
        with self.assertRaises(ValueError):
            self.runtime.configure({"schedule": {"light_minutes": 62}}, "p", expected_revision=before)

    def test_group_preserves_scheduling_constraints(self):
        task = self.store.add_task("p", "goal", "one", "audit", priority=70)
        available = time.time() + 3600
        self.store.execute("UPDATE tasks SET available=?,urgency=1 WHERE id=?", (available, task))
        group = execute(self.runtime, "p", [task], "group", request="audit", preview=False)
        row = self.store.one("SELECT available,urgency,priority FROM tasks WHERE id=?", (group["id"],))
        self.assertEqual(row, {"available": available, "urgency": 1, "priority": 70})

    def test_explicit_ui_kind_does_not_require_performance_proof(self):
        self.assertEqual(change_kind({"title": "优化按钮文字", "body": ""}, {"change_kind": "ui"}), "ui")
        self.assertEqual(change_kind({"title": "降低延迟", "body": ""}, {"change_kind": "ui"}), "performance")

    def test_contract_validates_tail_and_numeric_bounds(self):
        schema = {"type": "array", "items": {"type": "integer", "minimum": 0}}
        self.assertTrue(check_keys([1, "bad"], schema, schema))
        self.assertTrue(check_keys([1, -1], schema, schema))
        self.assertEqual(check_keys([0, 1], schema, schema), [])

    def test_multiline_multisuite_junit_summary(self):
        text = '<testsuites>\n<testsuite tests="2" failures="1"/>\n<testsuite tests="3" skipped="1"/>\n</testsuites>'
        report = junit_test_summary(text)
        self.assertIsNotNone(report)
        self.assertEqual(report["executed"], 5)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["skipped"], 1)

    def test_malformed_usage_does_not_discard_valid_tool_response(self):
        for protocol, field in (("responses", "usage"), ("anthropic", "usage"), ("gemini", "usageMetadata")):
            for value in ([1], "invalid", True):
                with self.subTest(protocol=protocol, value=value):
                    self.assertIsNone(native_usage(protocol, {field: value}))

    def test_task_duration_model_uses_actual_runs(self):
        task = self.store.add_task("p", "goal", "timing", "timing")
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended) VALUES ('r',?,'p','completed',100,108)", (task,))
        self.assertEqual(self.store.task_duration_model()["median_seconds"], 8)

    def test_atomic_write_preserves_original_after_disk_failure(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        path = workspace / "source.py"
        path.write_text("original\r\n")
        path.chmod(0o755)
        tools = WorkspaceTools(workspace, "implementation", self.store.root, DEFAULTS, None, [])
        with patch("adaptive_rsi.native.os.fsync", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                tools.execute("write_file", {"path": "source.py", "content": "changed"})
        self.assertEqual(path.read_bytes(), b"original\r\n")
        self.assertFalse(list(workspace.glob(".rsi-write-*")))
        tools.execute("write_file", {"path": "source.py", "content": "changed"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_large_source_ranges_are_streamed(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "large.py").write_bytes((b"x" * 1000 + b"\n") * 3000)
        tools = WorkspaceTools(workspace, "analysis", self.store.root, DEFAULTS, None, [])
        result = tools.execute("read_file", {"path": "large.py", "start_line": 2990, "end_line": 2992})
        self.assertEqual(len(result.splitlines()), 3)
        self.assertTrue(result.startswith("2990: "))

    def test_readonly_tools_overlap_but_return_in_call_order(self):
        barrier = threading.Barrier(2, timeout=2)
        tools = WorkspaceTools(self.root, "analysis", self.store.root, DEFAULTS, None, [])
        def read(worker, name, args):
            barrier.wait()
            return args["path"]
        calls = [("a", "read_file", {"path": "a"}), ("b", "read_file", {"path": "b"})]
        with patch.object(WorkspaceTools, "execute", read):
            self.assertEqual(execute_tools(tools, calls), [("a", "read_file", "a"), ("b", "read_file", "b")])

    def test_account_retry_after_is_shared_without_inflating_failures(self):
        health = ProviderHealth(self.store)
        problem = ModelFailure("limited", "rate_limit")
        problem.retry_after = 120
        health.record(DEFAULTS, "openai", "first", problem)
        with self.assertRaises(ModelFailure) as rejected:
            health.require(DEFAULTS, "openai", "second")
        self.assertTrue(rejected.exception.local_admission)
        health.record(DEFAULTS, "openai", "second", rejected.exception)
        self.assertEqual(health.status(DEFAULTS, "openai", "second")["failures"], 0)
        self.assertEqual(health.status(DEFAULTS, "openai", "second")["cooldown_scope"], "account")

    def test_rotating_same_credential_reference_invalidates_probe_identity(self):
        credentials = Credentials(self.store.root)
        provider = {"id": "test", "credential": "session", "secret_ref": "a" * 32}
        credentials.put(provider["secret_ref"], "session", "first-test-key")
        before = self.runtime.model_center.signature(provider)
        credentials.put(provider["secret_ref"], "session", "second-test-key")
        self.assertNotEqual(self.runtime.model_center.signature(provider), before)
        credentials.delete(provider["secret_ref"], "session")

    def test_storage_manifest_is_streamed_and_cached_with_explicit_refresh(self):
        from adaptive_rsi.storage_tools import overview
        backup(self.store)
        with patch("adaptive_rsi.storage_tools.file_digest", wraps=__import__(
                "adaptive_rsi.storage_tools", fromlist=["file_digest"]).file_digest) as digest:
            first = overview(self.store)
            second = overview(self.store)
            self.assertEqual(first["backup_manifest"], second["backup_manifest"])
            self.assertEqual(digest.call_count, 1)
            overview(self.store, refresh=True)
            self.assertEqual(digest.call_count, 2)

    def test_malformed_global_configuration_still_has_a_repair_response(self):
        server = self.server()
        self.store.execute("INSERT OR REPLACE INTO settings VALUES ('config','{broken')")
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.get(server, "state?sections=core")
        with failed.exception as response:
            self.assertEqual(response.code, 400)
            self.assertEqual(json.load(response)["error_code"], "invalid_global_configuration")
        result = self.runtime.repair_global_config("reset-invalid-global-config")
        self.assertTrue(result["saved"])
        self.assertEqual(self.store.setting(result["backup_key"])["raw"], "{broken")
        self.assertEqual(self.runtime.project("p")["paused"], 1)

    def test_state_configuration_and_revision_share_one_read_snapshot(self):
        server = self.server()
        original = self.runtime.config
        changed = threading.Event()
        def read(project=None):
            value = original(project)
            if project is None and not changed.is_set():
                changed.set()
                worker = threading.Thread(target=lambda: self.runtime.configure({"schedule": {"light_minutes": 61}}))
                worker.start()
                worker.join(3)
                self.assertFalse(worker.is_alive())
            return value
        with patch.object(self.runtime, "config", read):
            response = self.get(server, "state?project=p&sections=core")
        self.assertTrue(changed.is_set())
        self.assertEqual(response["config"]["schedule"]["light_minutes"], 60)
        self.assertEqual(response["global_config_revision"], 0)
        self.assertEqual(response["config_revision"], 0)
        self.assertEqual(original()["schedule"]["light_minutes"], 61)

    def test_post_failure_log_preserves_safe_error_code(self):
        server = self.server()
        request = urllib.request.Request(server.local_origin + "/api/config", data=b'{"values":{"unknown":1}}',
            headers={"Cookie": server.cookie + "=" + server.token, "Origin": server.origin,
                     "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as failed:
            urllib.request.urlopen(request, timeout=5)
        with failed.exception as response:
            json.load(response)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = self.store.one("SELECT error_code FROM request_log WHERE method='POST' ORDER BY started DESC LIMIT 1")
            if row:
                break
            time.sleep(.01)
        self.assertIsNotNone(row)
        self.assertNotEqual(row["error_code"], "")

    def test_paired_statistics_do_not_change_with_correlated_repeats(self):
        trials = [{"case": "win", "stable": {"pass": False}, "candidate": {"pass": True}},
                  {"case": "loss", "stable": {"pass": True}, "candidate": {"pass": False}}]
        first, repeated = paired_summary(trials), paired_summary(trials * 5)
        self.assertEqual(first["wilson_95"], repeated["wilson_95"])
        self.assertEqual((repeated["wins"], repeated["losses"]), (1, 1))

    def test_go_summary_counts_top_level_tests_separately_from_subtests(self):
        events = [{"Package": "p", "Test": name, "Action": "pass"} for name in ("TestA/one", "TestA/two", "TestA")]
        result = structured_test_summary("\n".join(json.dumps(event) for event in events))
        self.assertEqual(result["executed"], 1)
        self.assertEqual(result["subtests"], 2)

    def test_case_exposure_reservation_is_atomic_and_idempotent(self):
        self.store.execute("INSERT INTO cases VALUES ('case','p','family','holdout','{}',0,0)")
        key = self.store.reserve_case_exposure("p", "case", "experiment", 1)
        self.assertEqual(self.store.reserve_case_exposure("p", "case", "experiment", 1), key)
        with self.assertRaises(ValueError):
            self.store.reserve_case_exposure("p", "case", "different", 1)
        self.assertEqual(self.store.one("SELECT uses FROM cases WHERE id='case'")["uses"], 1)

    def test_concurrent_imports_cannot_put_one_case_in_two_splits(self):
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        import concurrent.futures
        source = fixture(self.root / "source")
        base = git(source, "rev-parse", "HEAD")
        evolution = Evolution(self.runtime)
        import adaptive_rsi.evolution as module
        original = module.snapshot
        barrier = threading.Barrier(2, timeout=5)
        def snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            barrier.wait()
            return result
        def insert(split):
            try:
                return evolution.import_case("p", {"family": split, "split": split, "repository": str(source),
                    "base": base, "task": "same held-out task", "checks": CHECKS, "source": "real"})
            except ValueError:
                return None
        with patch.object(module, "snapshot", snapshot), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(insert, ["validation", "holdout"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM cases")["n"], 1)
        self.assertEqual(len(list((self.store.root / "suites").iterdir())), 1)

    def test_readding_archived_project_is_explicit_and_restore_stays_paused(self):
        from tests.test_runtime import fixture
        source = fixture(self.root / "restorable")
        project = self.runtime.add_project(source)
        task = self.store.add_task(project, "goal", "source", "keep history")
        self.runtime.archive_project(project, "archive-project")
        repeated = self.runtime.add_project(source, enabled=True, details=True)
        self.assertTrue(repeated["archived"])
        self.assertTrue(repeated["paused"])
        self.assertTrue(self.store.setting("project-archived:" + project))
        restored = self.runtime.restore_project(project, "restore-project")
        self.assertTrue(restored["paused"])
        self.assertFalse(self.store.setting("project-archived:" + project))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "cancelled")

    def test_update_drain_renews_only_with_owner_lease(self):
        from adaptive_rsi.lifecycle import readiness, barrier
        self.runtime.updates.desktop_managed = True
        with patch("adaptive_rsi.lifecycle.time.time", return_value=100):
            first = readiness(self.runtime, "prepare", "0.5.999")["barrier"]
        with patch("adaptive_rsi.lifecycle.time.time", return_value=3600):
            with self.assertRaises(ValueError):
                readiness(self.runtime, "prepare", "0.5.999", "wrong")
            renewed = readiness(self.runtime, "prepare", "0.5.999", first["lease"])["barrier"]
        self.assertEqual(renewed["expires"], 7200)
        with patch("adaptive_rsi.lifecycle.time.time", return_value=3701):
            self.assertIsNotNone(barrier(self.runtime))

    def test_junit_accepts_standalone_empty_element_reports(self):
        self.assertEqual(junit_test_summary('log\n<testsuite tests="4" failures="1"/>\n') ["executed"], 4)

    def test_target_ownership_is_shared_and_migrates_legacy_receipts(self):
        from adaptive_rsi.release import owner_key, deployment_owner, key
        from adaptive_rsi.config import fingerprint
        from adaptive_rsi.store import encode
        config = self.runtime.config("p")
        config["github"]["repository"] = "example/a"
        policy = config["delivery"]["deploy"]
        policy.update(workflow=".github/workflows/deploy.yml", health_url="https://service.example/health")
        self.store.execute("INSERT INTO runs(id,project,state,config,result,started,ended) VALUES ('old','p','merged',?,'{}',1,2)", (encode(config),))
        receipt = {"run": "old", "sha": "a" * 40}
        legacy = "deployment-owner:" + fingerprint({"project": "p", "repository": "example/a", "target": policy["health_url"]})
        self.store.put_setting(legacy, receipt)
        self.store.put_setting(key("old", "deploy"), {"state": "running"})
        self.assertEqual(owner_key("p", "example/a", policy), owner_key("another", "example/b", policy))
        self.assertEqual(deployment_owner(self.runtime, "another", "example/b", policy), receipt)
        self.assertEqual(self.store.setting(owner_key("p", "example/a", policy)), receipt)

    def test_dispatch_intent_and_owner_rollback_together(self):
        from adaptive_rsi.release import save, key
        self.store.execute("INSERT INTO runs(id,project,state,config,result,started,ended) VALUES ('r','p','merged','{}','{}',1,2)")
        self.store.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            save(self.runtime, {"id": "r"}, "deploy", {"state": "dispatching"}, expected=None,
                 ownership=("owner", None, {"run": "r"}))
        self.assertIsNone(self.store.setting("owner"))
        self.assertIsNone(self.store.setting(key("r", "deploy")))

    def test_service_ownership_is_acquired_before_database_initialization(self):
        path = self.root / "owned-service"
        owner = Runtime.open(path, agent=object())
        self.addCleanup(owner.close)
        with patch("adaptive_rsi.store.Store", side_effect=AssertionError("must not initialize")):
            with self.assertRaisesRegex(RuntimeError, "owns this state"):
                Runtime.open(path)
        owner.close()
        recovered = Runtime.open(path, agent=object())
        self.addCleanup(recovered.close)

    def test_stratified_selection_covers_source_and_task_size(self):
        from adaptive_rsi.evolution import select_cases, case_stratum
        cases = [{"id": str(index), "uses": 0, "spec": json.dumps({"source": source, "task": "x" * size})}
                 for index, (source, size) in enumerate((("real", 100), ("real", 2000), ("synthetic", 100), ("synthetic", 20000)))]
        cases += [{**cases[0], "id": "duplicate-stratum"}]
        selected = select_cases(cases, 4, "seed")
        self.assertEqual(len({case_stratum(case) for case in selected}), 4)
        self.assertEqual([case["id"] for case in selected], [case["id"] for case in select_cases(cases, 4, "seed")])

    def test_quality_evidence_does_not_fabricate_a_cost_comparison(self):
        trials = [{"case": str(case), "split": "validation" if case < 3 else "holdout", "repetition": repeat,
                   "stable": {"pass": False, "tokens": 100, "seconds": 1, "usage_known": False},
                   "candidate": {"pass": True, "tokens": 1, "seconds": 1, "usage_known": False}}
                  for case in range(6) for repeat in range(2)]
        report = Evolution.decide(trials, DEFAULTS)
        self.assertFalse(report["cost_comparable"])
        self.assertEqual(report["cost_state"], "unknown_usage")
        self.assertIsNone(report["cost_ratio"])
        self.assertTrue(report["cost_warning"])

    def test_quoted_git_diff_paths_preserve_utf8_and_control_characters(self):
        from adaptive_rsi.gitops import diff_path
        self.assertEqual(diff_path('"b/\\346\\226\\207\\344\\273\\266\\tname.py"'), "文件\tname.py")
        self.assertEqual(diff_path("a/deleted.py"), "deleted.py")

    def test_failure_history_keeps_original_attempts_and_review_deadlines(self):
        import io
        import contextlib
        from scripts.test_worker import run
        class Failure(unittest.TestCase):
            def runTest(self):
                self.fail("intentional fault fixture")
        destination = self.root / "timing.json"
        with tempfile.TemporaryFile(mode="w+") as errors, patch.object(unittest.defaultTestLoader, "loadTestsFromName", side_effect=lambda _: unittest.TestSuite([Failure()])), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            self.assertEqual(run(["fixture"], destination), 1)
            self.assertEqual(run(["fixture"], destination), 1)
        receipts = json.loads(destination.with_suffix(".failures.json").read_text())
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len({receipt["attempt"] for receipt in receipts}), 2)
        self.assertTrue(all(not receipt["quarantined"] and receipt["review_by"] > receipt["created"] for receipt in receipts))

    def test_health_transport_bounds_stalled_body_and_cancels_real_connection(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from adaptive_rsi.release import health_check
        from adaptive_rsi.process import Cancelled, operation_cancellation
        import concurrent.futures
        arrived, closed = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.flush()
                arrived.set()
                self.connection.settimeout(4)
                if self.connection.recv(1) == b"":
                    closed.set()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        target = "http://127.0.0.1:" + str(server.server_port)
        began = time.monotonic()
        report = health_check(target, "a" * 40, timeout=.8)
        self.assertFalse(report["healthy"])
        self.assertTrue(arrived.is_set())
        self.assertTrue(closed.wait(2))
        self.assertLess(time.monotonic() - began, 3)
        arrived.clear()
        closed.clear()
        stop = threading.Event()
        def probe():
            with operation_cancellation(stop.is_set):
                return health_check(target, "a" * 40, timeout=10)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(probe)
            self.assertTrue(arrived.wait(3))
            stop.set()
            with self.assertRaises(Cancelled):
                future.result(timeout=3)
            self.assertTrue(closed.wait(2))
