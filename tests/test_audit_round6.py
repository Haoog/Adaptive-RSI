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

    def test_rotated_credentials_do_not_receive_old_inflight_failures(self):
        credentials = Credentials(self.store.root)
        provider = {"id": "gateway", "credential": "session", "secret_ref": "c" * 32}
        config = copy.deepcopy(DEFAULTS)
        config["providers"] = [provider]
        health = ProviderHealth(self.store)
        for code in ("auth", "rate_limit"):
            credentials.put(provider["secret_ref"], "session", "old-fixture-key")
            attempt = health.require(config, "gateway", "p", "model")
            credentials.put(provider["secret_ref"], "session", "fresh-fixture-key")
            error = ModelFailure("fixture failure", code)
            error.retry_after = 100 if code == "rate_limit" else 0
            health.record(config, "gateway", "p", error, "model", attempt=attempt)
            self.assertEqual(health.status(config, "gateway", "p")["failures"], 0)
            self.assertTrue(health.admission(config, "gateway", "p")["ready"])
            self.assertEqual(self.store.setting(attempt["key"])["code"], code)

    def test_recovery_lease_is_released_after_nonprovider_failures_and_cancellation(self):
        from adaptive_rsi.agent import CodexAgent
        from adaptive_rsi.process import Cancelled
        config = copy.deepcopy(DEFAULTS)
        config["recovery"]["provider_retry_minutes"] = 1
        key = ProviderHealth(self.store).key(config, "openai", "p")
        for error in (ModelFailure("missing", "model_missing"), ModelFailure("format", "tool_format"), Cancelled("fixture cancelled")):
            self.store.put_setting(key, {"paused": True, "failures": 3, "code": "timeout", "at": time.time() - 120})
            with patch.object(CodexAgent, "_invoke", side_effect=error):
                with self.assertRaises(type(error)):
                    CodexAgent(self.store).invoke("analysis", "fixture", self.root, "p", "unused", config)
            health = ProviderHealth(self.store)
            self.assertTrue(health.admission(config, "openai", "p")["ready"])
            state = health.status(config, "openai", "p")
            self.assertNotIn("half_open_owner", state)
            self.assertEqual(state["failures"], 3)

    def test_model_preferences_survive_credential_rotation_without_old_verification(self):
        center = self.runtime.model_center
        provider = {"id": "gateway", "credential": "session", "secret_ref": "d" * 32}
        center.credentials.put(provider["secret_ref"], "session", "first-fixture-key")
        with patch.object(center, "provider", return_value=provider):
            center.edit_model("gateway", {"id": "manual-model", "name": "Personal model", "manual": True,
                                         "favorite": True, "unit_price": 2.5}, "p")
        old_key = center.key(provider, "p")
        directory = center.directory(provider, "p")
        directory["models"][0]["verified"] = {"at": time.time()}
        center._save_directory(old_key, provider, "p", directory)
        center.credentials.put(provider["secret_ref"], "session", "second-fixture-key")
        models = center.directory(provider, "p")["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual((models[0]["name"], models[0]["favorite"], models[0]["unit_price"]), ("Personal model", True, 2.5))
        self.assertNotIn("verified", models[0])
        self.assertTrue(self.store.setting(old_key)["models"][0]["verified"])

    def test_global_repair_invalidates_old_global_and_inherited_project_revisions(self):
        self.store.put_setting("config", {"unsupported": True})
        global_before, project_before = self.store.config_revision(), self.store.config_revision("p")
        self.runtime.repair_global_config("reset-invalid-global-config")
        self.assertGreater(self.store.config_revision(), global_before)
        self.assertGreater(self.store.config_revision("p"), project_before)
        for project, revision in ((None, global_before), ("p", project_before)):
            with self.assertRaisesRegex(ValueError, "changed in another window"):
                self.runtime.configure({"schedule": {"weight": 2}}, project, expected_revision=revision)

    def test_health_cleanup_faults_do_not_replace_user_cancellation(self):
        from adaptive_rsi.agent import CodexAgent
        from adaptive_rsi.process import Cancelled
        for operation in ("record", "release"):
            with patch.object(CodexAgent, "_invoke", side_effect=Cancelled("user cancelled")), patch.object(
                    ProviderHealth, operation, side_effect=sqlite3.OperationalError("injected unavailable database")):
                with self.assertLogs(level="WARNING"), self.assertRaises(Cancelled):
                    CodexAgent(self.store).invoke("analysis", "fixture", self.root, "p", "unused", DEFAULTS)


class AuditStorageBudgetsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.addCleanup(self.store.close)

    def test_archive_manifest_verifies_and_detects_corruption(self):
        from adaptive_rsi.storage_tools import archive, verify_archive
        self.store.event('audit', 'preserved evidence')
        report = archive(self.store, compression=1)
        path = Path(report['path'])
        self.assertTrue(verify_archive(path)['valid'])
        self.assertEqual(report['compression'], 1)
        self.assertGreaterEqual(report['seconds'], 0)
        self.assertEqual(Path(report['manifest']).stat().st_mode & 0o777, 0o600)
        with path.open('r+b') as stream:
            stream.seek(20)
            original = stream.read(1)
            stream.seek(20)
            stream.write(bytes([original[0] ^ 1]))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            verify_archive(path)
        self.assertEqual(self.store.one('SELECT message FROM events')['message'], 'preserved evidence')

    def test_cancelled_archive_does_not_publish_or_delete_history(self):
        from adaptive_rsi.process import Cancelled
        from adaptive_rsi.storage_tools import archive
        calls = []
        def cancelled():
            calls.append(1)
            return len(calls) > 2
        with self.assertRaises(Cancelled):
            archive(self.store, cancel=cancelled)
        self.assertEqual(list((self.store.root / 'archives').glob('*.gz')), [])
        self.assertEqual(list((self.store.root / 'archives').glob('*.manifest.json')), [])
        self.assertEqual(list((self.store.root / 'archives').glob('.archive-*')), [])

    def test_manifest_size_and_expansion_budget_are_enforced(self):
        from adaptive_rsi.storage_tools import archive, verify_archive
        report = archive(self.store)
        manifest = Path(report['manifest'])
        data = json.loads(manifest.read_text())
        data['source_bytes'] = 1
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'expanded size'):
            verify_archive(report['path'])
        manifest.write_text(' ' * 16385)
        with self.assertRaisesRegex(ValueError, 'too large'):
            verify_archive(report['path'])

    def test_archive_publication_failure_preserves_existing_manifest(self):
        from adaptive_rsi.storage_tools import archive, backup
        snapshot = backup(self.store)
        manifest = self.store.root / 'archives' / (snapshot['id'] + '.sqlite3.gz.manifest.json')
        manifest.parent.mkdir()
        manifest.write_text('existing evidence')
        with patch('adaptive_rsi.storage_tools.backup', return_value=snapshot):
            with self.assertRaises(FileExistsError):
                archive(self.store)
        self.assertEqual(manifest.read_text(), 'existing evidence')

    def test_cancellation_during_manifest_sync_prevents_publication(self):
        from adaptive_rsi.storage_tools import archive
        from adaptive_rsi.process import Cancelled
        cancelled = threading.Event()
        fsync = os.fsync
        calls = []
        def sync(fd):
            fsync(fd)
            calls.append(fd)
            if len(calls) == 2:
                cancelled.set()
        with patch('adaptive_rsi.storage_tools.os.fsync', side_effect=sync):
            with self.assertRaises(Cancelled):
                archive(self.store, cancel=cancelled.is_set)
        self.assertEqual(list((self.store.root / 'archives').iterdir()), [])

    def test_passive_checkpoint_reports_reader_pin_without_waiting(self):
        self.store.event('before', 'first')
        self.store.wal_diagnostics(checkpoint=True)
        with self.store.snapshot():
            self.store.query('SELECT * FROM events')
            worker = threading.Thread(target=lambda: self.store.event('after', 'second'))
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            report = self.store.wal_diagnostics(checkpoint=True)
            self.assertEqual(report['active_snapshots'], 1)
            self.assertGreater(report['pending_pages'], 0)
            self.assertEqual(report['mode'], 'passive')
        report = self.store.wal_diagnostics(checkpoint=True)
        self.assertEqual(report['active_snapshots'], 0)
        self.assertEqual(report['pending_pages'], 0)


class AuditModelDiagnosticsTest(unittest.TestCase):
    def test_stop_reason_matrix_survives_recovery_and_is_not_provider_prose(self):
        from adaptive_rsi.native import response_stop_reason
        from adaptive_rsi.recovery import failure
        samples = [
            ('anthropic', {'stop_reason': 'max_tokens'}, 'output_limit'),
            ('chat_completions', {'choices': [{'finish_reason': 'length'}]}, 'output_limit'),
            ('responses', {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}, 'output_limit'),
            ('responses', {'output': [{'content': [{'type': 'refusal', 'refusal': 'private text'}]}]}, 'refusal'),
            ('gemini', {'promptFeedback': {'blockReason': 'SAFETY'}}, 'safety'),
            ('gemini', {'candidates': [{'finishReason': 'MAX_TOKENS'}]}, 'output_limit'),
            ('chat_completions', {'choices': [{'message': {'refusal': 'private text'}}]}, 'refusal'),
            ('ollama', {'message': {'content': 'plain'}}, 'text_only'),
            ('ollama', {'done_reason': 'length', 'message': {'content': 'partial'}}, 'output_limit'),
            ('anthropic', {'stop_reason': 'private unexpected provider text'}, 'unknown'),
            ('gemini', {'candidates': [None]}, 'text_only'),
        ]
        for protocol, response, expected in samples:
            with self.subTest(protocol=protocol, response=response):
                error = RuntimeError('No valid tool calls')
                error.stop_reason = response_stop_reason(protocol, response)
                result = failure(error)
                self.assertEqual(result['stop_reason'], expected)
                self.assertNotIn('private', json.dumps(result))

    def test_native_failure_keeps_stop_reason_through_actual_adapter(self):
        from types import SimpleNamespace
        from adaptive_rsi.native import NativeAgent
        from adaptive_rsi.recovery import failure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = SimpleNamespace(root=root, reserve_call=lambda *a: 'c', record_usage=lambda *a: None,
                                   event=lambda *a: None, execute=lambda *a: None)
            with patch('adaptive_rsi.native.Credentials') as credentials, patch('adaptive_rsi.native.post_json',
                    return_value={'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]}):
                credentials.return_value.get.return_value = None
                with self.assertRaises(RuntimeError) as raised:
                    NativeAgent(fake).invoke('planning', 'test', root, 'p', 'r',
                        {'limits': {'max_output_mb': 1, 'command_seconds': 60}},
                        {'model': 'test', 'effort': 'default'},
                        {'id': 'test', 'protocol': 'chat_completions', 'base_url': 'https://invalid.example'})
                self.assertEqual(failure(raised.exception)['action'], 'adjust_output_limit')

    def test_request_id_survives_real_http_and_worker_envelope(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from adaptive_rsi.native import post_json, ProviderUnavailable
        from adaptive_rsi.recovery import failure
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(429 if self.path == '/limited' else 200)
                self.send_header('x-request-id', 'request-123')
                self.send_header('Set-Cookie', 'private-cookie')
                if self.path == '/truncated':
                    self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                if self.path == '/truncated':
                    self.wfile.write(b'4\r\nab')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write({'/list': b'[]', '/null': b'null', '/bad': b'{',
                                  '/large': b' ' * 1025}.get(self.path, b'{}'))
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port)
            for cancel in (None, lambda: False):
                response = post_json(url, {}, {}, 5, 1024, cancel=cancel)
                self.assertEqual(response.provider_request_id, 'request-123')
                self.assertEqual(dict(response), {})
                with self.assertRaises(ProviderUnavailable) as raised:
                    post_json(url + '/limited', {}, {}, 5, 1024, cancel=cancel)
                evidence = failure(raised.exception)
                self.assertEqual(evidence['provider_request_id'], 'request-123')
                self.assertNotIn('private-cookie', json.dumps(evidence))
                with self.assertRaises(ProviderUnavailable) as interrupted:
                    post_json(url + '/truncated', {}, {}, 5, 1024, cancel=cancel)
                self.assertEqual(interrupted.exception.code, 'connection')
                self.assertEqual(interrupted.exception.provider_request_id, 'request-123')
                for path in ('/list', '/null', '/bad', '/large'):
                    with self.assertRaises(RuntimeError) as invalid:
                        post_json(url + path, {}, {}, 5, 1024, cancel=cancel)
                    evidence = failure(invalid.exception)
                    self.assertEqual(evidence['provider_request_id'], 'request-123')
                    self.assertNotIn('private-cookie', json.dumps(evidence))
                    if path == '/large':
                        self.assertEqual(evidence['stop_reason'], 'output_limit')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


class AuditRequestSamplingTest(unittest.TestCase):
    def test_operation_polling_uses_the_real_get_route(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = Runtime(Store(temporary.name), agent=object())
        self.addCleanup(runtime.close)
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)
        with patch.object(runtime.operations, 'get', return_value={'state': 'running'}):
            for _ in range(10):
                request = urllib.request.Request(server.local_origin + '/api/operation?id=private-id',
                    headers={'Cookie': server.cookie + '=' + server.token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    json.load(response)
        deadline = time.monotonic() + 2
        while runtime.store.metrics()['request_logging']['observed'] < 10 and time.monotonic() < deadline:
            time.sleep(.01)
        rows = runtime.store.query('SELECT path FROM request_log')
        self.assertEqual(rows, [{'path': '/api/operation'}])

    def test_poll_sampling_reduces_writes_and_keeps_all_errors_and_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch('adaptive_rsi.store.time.monotonic', return_value=60):
                    for index in range(100):
                        store.record_request(str(index), 'GET', '/api/state', 200, 1, 2)
                    for index, method, status in ((100, 'GET', 500), (101, 'POST', 200), (102, 'GET', 429)):
                        store.record_request(str(index), method, '/api/state', status, 1, 2, 'test_error')
                self.assertEqual(store.one('SELECT count(*) AS n FROM request_log')['n'], 4)
                with patch('adaptive_rsi.store.time.monotonic', return_value=121):
                    self.assertTrue(store.record_request('next', 'GET', '/api/state', 200, 1, 2))
                counters = store.metrics()['request_logging']
                self.assertEqual(counters['observed'], 104)
                self.assertEqual(counters['sampled_out'], 99)
                self.assertEqual(counters['persisted'], 5)
            finally:
                store.close()

    def test_failed_log_write_does_not_suppress_next_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch.object(store, 'execute', side_effect=sqlite3.OperationalError('disk full')):
                    with self.assertRaises(sqlite3.OperationalError):
                        store.record_request('failed', 'GET', '/api/state', 200, 1, 2)
                self.assertTrue(store.record_request('retry', 'GET', '/api/state', 200, 1, 2))
                self.assertEqual(store.metrics()['request_logging']['failed'], 1)
            finally:
                store.close()


class AuditMaintenanceRetentionTest(unittest.TestCase):
    def test_old_finished_receipts_expire_but_unknown_and_active_survive(self):
        import contextlib
        from types import SimpleNamespace
        from adaptive_rsi.maintenance import Maintenance
        from adaptive_rsi.storage_tools import archive
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                old = time.time() - 100 * 86400
                store.execute('CREATE TABLE operations(id TEXT,state TEXT,ended REAL)')
                for name, state in [('finished', 'completed'), ('unknown', 'unknown'), ('active', 'running')]:
                    store.execute('INSERT INTO operations VALUES (?,?,?)', (name, state, old))
                    store.put_setting('batch:p:' + name, {'created': old, 'complete': state == 'completed'})
                    store.put_setting('batch:p:' + name + ':item', {'state': state})
                report = archive(store)
                fake = SimpleNamespace(store=store, dispatch_lock=threading.RLock(),
                    scheduler_scope=lambda *a, **k: contextlib.nullcontext(), config=lambda p: {'providers': []},
                    model_center=SimpleNamespace(cleanup_credentials=lambda: None, key=lambda *a: 'unused'))
                Maintenance.maintenance(fake)
                self.assertIsNone(store.setting('batch:p:finished'))
                self.assertIsNone(store.setting('batch:p:finished:item'))
                for name in ('unknown', 'active'):
                    self.assertIsNotNone(store.setting('batch:p:' + name))
                    self.assertIsNotNone(store.one('SELECT id FROM operations WHERE id=?', (name,)))
                self.assertIsNone(store.one("SELECT id FROM operations WHERE id='finished'"))
                check = store.setting('archive-verification:' + Path(report['path']).name)
                self.assertTrue(check['valid'])
                self.assertEqual(store.wal_diagnostics()['mode'], 'passive')
            finally:
                store.close()


class AuditResourceFairnessTest(unittest.TestCase):
    setUp = AuditRound6Test.setUp

    def test_dispatch_accounts_for_recent_duration_not_attempt_count(self):
        import concurrent.futures
        now = time.time()
        self.store.execute("INSERT INTO projects VALUES ('q','other',?,'{}',0,?)",
                           (str(self.root / 'other'), now))
        for project in ('p', 'q'):
            self.runtime.pause(project, False)
            self.runtime.goal(project, 'work')
        self.runtime.configure({'limits': {'concurrency': 1}})
        for project, seconds in (('p', 600), ('q', 10)):
            task = self.store.add_task(project, 'goal', 'historic', 'finished')
            self.store.execute('INSERT INTO runs(id,project,task,state,started,ended) VALUES (?,?,?,\'completed\',?,?)',
                               ('old-' + project, project, task, now - seconds, now))
        selected = []
        class Pool:
            def submit(_, fn, run):
                selected.append(run['project'])
                return concurrent.futures.Future()
        eligible = [(self.runtime.project(p), self.runtime.config(p)) for p in ('p', 'q')]
        with patch.object(self.runtime, 'pool', Pool()):
            self.runtime.dispatch(eligible)
        self.assertEqual(selected, ['q'])
        accounting = self.store.setting('fair-accounting')
        self.assertEqual(accounting['window_seconds'], 86400)
        self.assertEqual({row['project']: row['seconds'] for row in accounting['projects']}, {'p': 600, 'q': 10})

    def test_continuously_waiting_project_is_not_hidden_by_resource_debt(self):
        import concurrent.futures
        now = time.time()
        self.store.execute("INSERT INTO projects VALUES ('q','other',?,'{}',0,?)",
                           (str(self.root / 'other'), now))
        for project in ('p', 'q'):
            self.runtime.pause(project, False)
            self.runtime.goal(project, 'work')
        self.runtime.configure({'limits': {'concurrency': 1}})
        self.store.put_setting('fair-waiting', {'p': now - 4000, 'q': now})
        old = self.store.add_task('p', 'goal', 'historic', 'finished')
        self.store.execute("INSERT INTO runs(id,project,task,state,started,ended) VALUES ('cost','p',?,'completed',?,?)",
                           (old, now - 20000, now))
        selected = []
        class Pool:
            def submit(_, fn, run):
                selected.append(run['project'])
                return concurrent.futures.Future()
        with patch.object(self.runtime, 'pool', Pool()):
            self.runtime.dispatch([(self.runtime.project(p), self.runtime.config(p)) for p in ('p', 'q')])
        self.assertEqual(selected, ['p'])


class AuditResourceWorkloadTest(unittest.TestCase):
    setUp = AuditRound6Test.setUp

    def test_long_and_short_jobs_receive_weighted_worker_time(self):
        import concurrent.futures
        now = time.time()
        self.store.execute("INSERT INTO projects VALUES ('q','short',?,'{}',0,?)",
                           (str(self.root / 'other'), now))
        self.runtime.configure({'limits': {'concurrency': 1}})
        for project, weight in (('p', 1), ('q', 3)):
            self.runtime.pause(project, False)
            self.runtime.configure({'schedule': {'weight': weight}}, project)
            for index in range(80):
                self.runtime.goal(project, 'job ' + str(index))
        used = {'p': 0, 'q': 0}
        class Pool:
            def submit(_, fn, run):
                seconds = 30 if run['project'] == 'p' else 2
                used[run['project']] += seconds
                self.store.transition(run['id'], 'completed', {}, True)
                self.store.execute('UPDATE runs SET started=?,ended=? WHERE id=?',
                                   (now - seconds, now, run['id']))
                future = concurrent.futures.Future()
                future.set_result(None)
                return future
        eligible = [(self.runtime.project(p), self.runtime.config(p)) for p in ('p', 'q')]
        with patch.object(self.runtime, 'pool', Pool()):
            for _ in range(80):
                self.runtime.futures.clear()
                self.runtime.dispatch(eligible)
        self.assertGreater(used['p'], 0)
        self.assertGreater(used['q'], used['p'])
        self.assertLessEqual(abs(used['p'] - used['q'] / 3), 30)


class AuditOfflineRecoveryTest(unittest.TestCase):
    def test_readonly_and_damaged_sources_are_not_modified(self):
        from adaptive_rsi.recovery_diagnostics import inspect_database
        from adaptive_rsi.storage_tools import file_digest
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            store.put_setting('private-key', 'SECRET-CONTENT-DO-NOT-EXPORT')
            path = store.path
            store.close()
            os.chmod(path, 0o400)
            before = (file_digest(path), path.stat().st_mtime_ns)
            report = inspect_database(path)
            self.assertEqual(report['integrity'], 'ok')
            self.assertEqual(before, (file_digest(path), path.stat().st_mtime_ns))
            self.assertFalse(report['source_modified'])
            self.assertNotIn('SECRET-CONTENT', json.dumps(report))
            self.assertTrue(any(table.get('table') == 'settings' and table.get('rows') for table in report['tables']))
            damaged = Path(directory) / 'damaged.sqlite3'
            damaged.write_bytes(b'not a database at all')
            before = file_digest(damaged)
            report = inspect_database(damaged)
            self.assertTrue(report['errors'])
            self.assertEqual(file_digest(damaged), before)

    def test_recovery_copy_includes_wal_without_creating_source_sidecars(self):
        from adaptive_rsi.recovery_diagnostics import inspect_database
        from adaptive_rsi.storage_tools import file_digest
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'state')
            try:
                store.event('wal-evidence', 'private payload')
                paths = list(store.root.glob('state.sqlite3*'))
                before = {path.name: (file_digest(path), path.stat().st_mtime_ns) for path in paths}
                report = inspect_database(store.path)
                self.assertTrue(report['consistent_source'])
                self.assertEqual(report['integrity'], 'ok')
                self.assertEqual(next(row['rows'] for row in report['tables'] if row['table'] == 'events'), 1)
                after = {path.name: (file_digest(path), path.stat().st_mtime_ns)
                         for path in store.root.glob('state.sqlite3*')}
                self.assertEqual(before, after)
                self.assertNotIn('private payload', json.dumps(report))
            finally:
                store.close()

    def test_recovery_inspection_rejects_links_and_oversized_sources(self):
        from adaptive_rsi.recovery_diagnostics import inspect_database
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.write_bytes(b'x' * (1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'capacity'):
                inspect_database(source, max_mb=1)
            link = Path(directory) / 'link'
            link.symlink_to(source)
            with self.assertRaises(OSError):
                inspect_database(link)


class AuditOperationPersistenceTest(unittest.TestCase):
    setUp = AuditRound6Test.setUp

    def test_file_receipt_failure_does_not_prevent_database_completion(self):
        operation = 'a' * 16
        self.store.execute("INSERT INTO operations VALUES (?,'backup','running','{}',?,NULL)",
                           (operation, time.time()))
        with patch('adaptive_rsi.updates.atomic_json', side_effect=OSError('private payload not logged')):
            with self.assertLogs('adaptive_rsi.operations', level='WARNING') as messages:
                self.assertTrue(self.runtime.operations._save_terminal(operation, 'completed', {'id': 'backup'}))
        row = self.store.one('SELECT state,result FROM operations WHERE id=?', (operation,))
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(json.loads(row['result']), {'id': 'backup'})
        self.assertNotIn('private payload', '\n'.join(messages.output))

    def test_failed_receipt_replay_does_not_replace_known_result_with_interrupted(self):
        operation = 'b' * 16
        self.store.execute("INSERT INTO operations VALUES (?,'backup','running','{}',?,NULL)",
                           (operation, time.time()))
        execute = self.store.execute
        def fail_update(sql, args=()):
            if sql.startswith('UPDATE operations SET state=?'):
                raise sqlite3.OperationalError('injected disk failure')
            return execute(sql, args)
        with patch.object(self.store, 'execute', side_effect=fail_update):
            with self.assertLogs('adaptive_rsi.operations', level='ERROR'):
                self.assertFalse(self.runtime.operations._save_terminal(operation, 'completed', {'id': 'backup'}))
                self.runtime.operations.recover()
        self.assertIsNone(self.store.one('SELECT ended FROM operations WHERE id=?', (operation,))['ended'])
        self.runtime.operations.recover()
        row = self.store.one('SELECT state,result FROM operations WHERE id=?', (operation,))
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(json.loads(row['result']), {'id': 'backup'})
        self.assertFalse((self.store.root / 'operation-receipts' / (operation + '.json')).exists())

    def test_both_persistence_failures_have_independent_diagnostics(self):
        with patch('adaptive_rsi.updates.atomic_json', side_effect=OSError('secret')), \
                patch.object(self.store, 'execute', side_effect=sqlite3.OperationalError('secret')):
            with self.assertLogs('adaptive_rsi.operations', level='WARNING') as messages:
                with self.assertRaisesRegex(RuntimeError, 'both database and receipt file'):
                    self.runtime.operations._save_terminal('c' * 16, 'completed', {'id': 'backup'})
        self.assertNotIn('secret', '\n'.join(messages.output))

    def test_success_result_survives_transient_dual_persistence_failure(self):
        operation = 'd' * 16
        self.store.execute("INSERT INTO operations VALUES (?,'diagnostics','running','{}',?,NULL)",
                           (operation, time.time()))
        from adaptive_rsi.updates import atomic_json
        writes = {'file': 0, 'database': 0}
        execute = self.store.execute
        def fail_first_file(*args, **kwargs):
            writes['file'] += 1
            if writes['file'] == 1:
                raise OSError('transient')
            return atomic_json(*args, **kwargs)
        def fail_first_database(sql, args=()):
            if sql.startswith('UPDATE operations SET state=?'):
                writes['database'] += 1
                if writes['database'] == 1:
                    raise sqlite3.OperationalError('transient')
            return execute(sql, args)
        with patch('adaptive_rsi.storage_tools.diagnostics', return_value={'artifact': 'actual-success'}) as business, \
                patch('adaptive_rsi.updates.atomic_json', side_effect=fail_first_file), \
                patch.object(self.store, 'execute', side_effect=fail_first_database):
            with self.assertLogs('adaptive_rsi.operations', level='WARNING'):
                self.runtime.operations._run(operation, 'diagnostics', {}, threading.Event())
        business.assert_called_once()
        row = self.runtime.operations.get(operation)
        self.assertEqual(row['state'], 'completed')
        self.assertEqual(row['result'], {'artifact': 'actual-success'})

    def test_existing_receipt_recovery_does_not_need_file_rewrite(self):
        operation = 'e' * 16
        self.store.execute("INSERT INTO operations VALUES (?,'diagnostics','running','{}',?,NULL)",
                           (operation, time.time()))
        with patch.object(self.store, 'execute', side_effect=sqlite3.OperationalError('offline')):
            with self.assertLogs('adaptive_rsi.operations', level='ERROR'):
                self.runtime.operations._save_terminal(operation, 'completed', {'artifact': 'existing'})
        execute = self.store.execute
        def offline_terminal(sql, args=()):
            if sql.startswith('UPDATE operations SET state=?'):
                raise sqlite3.OperationalError('offline')
            return execute(sql, args)
        with patch('adaptive_rsi.updates.atomic_json', side_effect=OSError('readonly')) as rewriting, \
                patch.object(self.store, 'execute', side_effect=offline_terminal):
            with self.assertLogs('adaptive_rsi.operations', level='ERROR'):
                self.runtime.operations.recover()
            rewriting.assert_not_called()
        self.runtime.operations.recover()
        self.assertEqual(self.runtime.operations.get(operation)['result'], {'artifact': 'existing'})


class AuditMigrationHistoryTest(unittest.TestCase):
    def test_request_history_has_one_time_index_and_query_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                indexes = store.query('PRAGMA index_list(request_log)')
                starts = [row['name'] for row in indexes if row['name'] == 'request_log_started']
                self.assertEqual(starts, ['request_log_started'])
                self.assertNotIn('request_log_created', {row['name'] for row in indexes})
                plan = store.query('EXPLAIN QUERY PLAN SELECT id FROM request_log ORDER BY started DESC LIMIT 10')
                self.assertIn('request_log_started', ' '.join(row['detail'] for row in plan))
            finally:
                store.close()

    def test_sql_bytes_are_hashed_and_bootstrap_does_not_repeat(self):
        import hashlib
        from adaptive_rsi.schema_migrations import scripts
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            record = next(row for row in store.schema_migrations() if row['version'] == 3)
            self.assertEqual(record['checksum'], hashlib.sha256(scripts()[0][1]).hexdigest())
            self.assertEqual(store.one('PRAGMA user_version')['user_version'], 2)
            store.close()
            with patch.object(Store, '_bootstrap', side_effect=AssertionError('bootstrap repeated')):
                for _ in range(2):
                    reopened = Store(directory)
                    reopened.close()

    def test_changed_migration_content_fails_before_ddl(self):
        from adaptive_rsi.schema_migrations import scripts
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.execute('CREATE INDEX sentinel_index ON events(created)')
            store.close()
            changed = [(version, content + b'\nDROP INDEX sentinel_index;\n') for version, content in scripts()]
            with patch('adaptive_rsi.schema_migrations.scripts', return_value=changed):
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    Store(directory)
            with sqlite3.connect(str(Path(directory) / 'state.sqlite3')) as db:
                self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='sentinel_index'").fetchone())

    def test_failed_new_migration_rolls_back_ddl_and_history_together(self):
        from adaptive_rsi.schema_migrations import scripts
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.close()
            additions = scripts() + [(4, b'CREATE TABLE should_rollback(id TEXT);\nINVALID SQL;\n')]
            with patch('adaptive_rsi.schema_migrations.scripts', return_value=additions):
                with self.assertRaises(sqlite3.OperationalError):
                    Store(directory)
            with sqlite3.connect(str(Path(directory) / 'state.sqlite3')) as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone())
                self.assertIsNone(db.execute('SELECT 1 FROM schema_migrations WHERE version=4').fetchone())
