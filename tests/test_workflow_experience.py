import base64
import errno
import io
import json
import os
import platform
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from adaptive_rsi.config import fingerprint
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import CHECKS, FakeAgent, fixture


class BackupPublicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.payload = "private backup payload" * 50000
        self.store.put_setting("backup-probe", self.payload)
        self.folder = self.store.root / "backups"

    @contextmanager
    def source_progress(self, callback):
        connect = self.store.connect

        @contextmanager
        def instrumented():
            with connect() as source:
                class Source:
                    def __getattr__(self, name):
                        return getattr(source, name)

                    def backup(self, target):
                        source.backup(target, pages=1, progress=callback)

                yield Source()

        with patch.object(self.store, "connect", instrumented):
            yield

    def handler(self):
        from adaptive_rsi.server import Handler
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(_backup_gate=threading.BoundedSemaphore(2))
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda _: None
        handler.headers_common = lambda: None
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        return handler

    def test_in_progress_snapshot_is_private_then_published_closed_and_portable(self):
        from adaptive_rsi.server import RequestError
        from adaptive_rsi.storage_tools import backup, overview
        identifier = "a" * 16
        final = self.folder / (identifier + ".sqlite3")
        paused, resume = threading.Event(), threading.Event()
        targets = []
        connect, link = sqlite3.connect, os.link

        def progress(status, remaining, total):
            if remaining and not paused.is_set():
                paused.set()
                if not resume.wait(10):
                    raise RuntimeError("Backup probe timed out")

        def track_target(path, *args, **kwargs):
            db = connect(path, *args, **kwargs)
            if str(path).endswith(".tmp"):
                targets.append(db)
            return db

        def publish(source, destination):
            self.assertFalse(final.exists())
            self.assertEqual(len(targets), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                targets[0].execute("SELECT 1")
            link(source, destination)

        self.assertGreater(Path(str(self.store.path) + "-wal").stat().st_size, 0)
        with self.source_progress(progress), \
                patch("adaptive_rsi.storage_tools.uid", return_value=identifier), \
                patch("adaptive_rsi.storage_tools.sqlite3.connect", side_effect=track_target), \
                patch("adaptive_rsi.storage_tools.os.link", side_effect=publish), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(backup, self.store)
            try:
                self.assertTrue(paused.wait(10))
                self.assertFalse(final.exists())
                staging = list(self.folder.glob("*.tmp"))
                self.assertEqual(len(staging), 1)
                self.assertEqual(staging[0].stat().st_mode & 0o777, 0o600)
                self.assertTrue(all(path.name.startswith(staging[0].name)
                                    for path in self.folder.iterdir()))
                snapshot = overview(self.store)
                self.assertEqual(snapshot["backups"], [])
                self.assertEqual(snapshot["bytes"]["backups"], 0)
                for params in ({}, {"format": ["json"], "offset": ["0"]}):
                    with self.assertRaisesRegex(RequestError, "Backup not found"):
                        self.handler().backup(final, params)
            finally:
                resume.set()
            saved = future.result(timeout=10)

        self.assertEqual(saved["id"], identifier)
        self.assertEqual(saved["bytes"], final.stat().st_size)
        self.assertEqual(list(self.folder.iterdir()), [final])
        self.assertEqual(final.stat().st_mode & 0o777, 0o600)
        self.assertEqual([item["id"] for item in overview(self.store)["backups"]], [identifier])
        handler = self.handler()
        handler.backup(final, {})
        self.assertEqual(handler.wfile.getvalue(), final.read_bytes())
        chunks = []
        handler.respond = chunks.append
        handler.backup(final, {"format": ["json"], "offset": ["0"]})
        self.assertEqual(base64.b64decode(chunks[0]["data"]), final.read_bytes()[:512 * 1024])
        # Copy only the database bytes: a WAL source must produce a standalone snapshot.
        portable = self.root / "portable.sqlite3"
        portable.write_bytes(final.read_bytes())
        self.assertEqual(portable.read_bytes()[18:20], b"\x01\x01")
        with closing(sqlite3.connect(portable.as_uri() + "?mode=ro", uri=True)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(json.loads(db.execute(
                "SELECT value FROM settings WHERE key='backup-probe'").fetchone()[0]), self.payload)
        self.assertFalse(Path(str(portable) + "-wal").exists())
        self.assertFalse(Path(str(portable) + "-shm").exists())

    def test_overview_skips_files_disappearing_during_stat(self):
        from adaptive_rsi.storage_tools import backup, overview
        for kind in ("runs", "backups", "database", "backup_listing"):
            with self.subTest(kind=kind):
                if kind == "database":
                    transient = self.store.root / "state.sqlite3-journal"
                elif kind == "runs":
                    transient = self.store.root / "runs" / "run" / "temporary-journal"
                else:
                    saved = backup(self.store)
                    transient = self.folder / (saved["id"] + ".sqlite3")
                transient.parent.mkdir(parents=True, exist_ok=True)
                if kind in ("runs", "database"):
                    transient.write_bytes(b"temporary")
                method = "stat" if kind in ("database", "backup_listing") else "lstat"
                original = getattr(Path, method)
                vanished = []

                def disappear(path, *args, **kwargs):
                    if path == transient and not vanished:
                        vanished.append(path)
                        path.unlink()
                    return original(path, *args, **kwargs)

                with patch.object(Path, method, disappear):
                    snapshot = overview(self.store)
                self.assertEqual(vanished, [transient])
                self.assertEqual(snapshot["backups"], [])
                self.assertEqual(snapshot["bytes"]["runs"], 0)
                expected_database = sum(p.stat().st_size for p in self.store.root.glob("state.sqlite3*"))
                self.assertEqual(snapshot["bytes"]["database"], expected_database)

    def test_overview_propagates_permission_and_io_errors(self):
        from adaptive_rsi.storage_tools import backup, overview
        saved = backup(self.store)
        published = self.folder / (saved["id"] + ".sqlite3")
        run_file = self.store.root / "runs" / "evidence"
        run_file.parent.mkdir()
        run_file.write_bytes(b"evidence")
        for path, method in ((run_file, "lstat"), (published, "lstat"),
                             (published, "stat"), (self.store.path, "stat")):
            for error in (PermissionError(errno.EACCES, "Denied"), OSError(errno.EIO, "I/O failure")):
                with self.subTest(path=path.name, method=method, error=error.errno):
                    original = getattr(Path, method)

                    def fail(candidate, *args, **kwargs):
                        if candidate == path:
                            raise error
                        return original(candidate, *args, **kwargs)

                    with patch.object(Path, method, fail):
                        with self.assertRaises(type(error)) as raised:
                            overview(self.store)
                    self.assertIs(raised.exception, error)

    def test_overview_during_concurrent_backup_publication(self):
        from adaptive_rsi.storage_tools import backup, overview
        ready, done = threading.Event(), threading.Event()

        def publish():
            if not ready.wait(10):
                raise RuntimeError("Overview probe timed out")
            try:
                return [backup(self.store) for _ in range(12)]
            finally:
                done.set()

        observations = 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(publish)
            ready.set()
            deadline = time.monotonic() + 10
            while not done.is_set() or observations < 30:
                self.assertLess(time.monotonic(), deadline)
                snapshot = overview(self.store)
                for saved in snapshot["backups"]:
                    self.assertGreater(saved["bytes"], 0)
                    self.assertTrue((self.folder / (saved["id"] + ".sqlite3")).is_file())
                observations += 1
            published = future.result(timeout=10)
        snapshot = overview(self.store)
        self.assertEqual(len(snapshot["backups"]), len(published))
        self.assertEqual(snapshot["bytes"]["backups"], sum(saved["bytes"] for saved in published))
        self.assertTrue(all(path.suffix == ".sqlite3" for path in self.folder.iterdir()))

    def test_copy_failure_cleans_staging_and_preserves_existing_backup(self):
        from adaptive_rsi.storage_tools import backup
        saved = backup(self.store)
        existing = self.folder / (saved["id"] + ".sqlite3")
        original = existing.read_bytes()

        def fail(status, remaining, total):
            raise sqlite3.OperationalError("Injected copy failure")

        with self.source_progress(fail):
            with self.assertRaisesRegex(sqlite3.OperationalError, "Injected copy failure"):
                backup(self.store)
        self.assertEqual(list(self.folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), original)

    def test_journal_conversion_failure_closes_target_and_cleans_staging(self):
        from adaptive_rsi.storage_tools import backup
        targets = []
        connect = sqlite3.connect

        class FailingJournal(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA journal_mode=DELETE":
                    raise sqlite3.OperationalError("Injected journal failure")
                return super().execute(sql, *args, **kwargs)

        def failing_target(path):
            target = connect(path, factory=FailingJournal)
            targets.append(target)
            return target

        with patch("adaptive_rsi.storage_tools.sqlite3.connect", side_effect=failing_target):
            with self.assertRaisesRegex(sqlite3.OperationalError, "Injected journal failure"):
                backup(self.store)
        self.assertEqual(list(self.folder.iterdir()), [])
        with self.assertRaises(sqlite3.ProgrammingError):
            targets[0].execute("SELECT 1")

    def test_publication_failure_cleans_staging_and_preserves_existing_backup(self):
        from adaptive_rsi.storage_tools import backup
        saved = backup(self.store)
        existing = self.folder / (saved["id"] + ".sqlite3")
        original = existing.read_bytes()
        with patch("adaptive_rsi.storage_tools.os.link", side_effect=OSError("Injected publication failure")):
            with self.assertRaisesRegex(OSError, "Injected publication failure"):
                backup(self.store)
        self.assertEqual(list(self.folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), original)

    def test_name_collision_never_overwrites_or_removes_valid_backup(self):
        from adaptive_rsi.storage_tools import backup
        saved = backup(self.store)
        existing = self.folder / (saved["id"] + ".sqlite3")
        original = existing.read_bytes()
        self.store.put_setting("backup-probe", "new value")
        with patch("adaptive_rsi.storage_tools.uid", return_value=saved["id"]):
            with self.assertRaises(FileExistsError):
                backup(self.store)
        self.assertEqual(list(self.folder.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), original)
        with closing(sqlite3.connect(existing)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(json.loads(db.execute(
                "SELECT value FROM settings WHERE key='backup-probe'").fetchone()[0]), self.payload)


class ExperienceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / "repo"))

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_submission_retry_returns_same_task_but_intentional_repeat_is_new(self):
        first = self.runtime.goal(self.project, "Fix addition", request_id="request-1")
        self.assertEqual(first, self.runtime.goal(self.project, "Fix addition", request_id="request-1"))
        self.assertNotEqual(first, self.runtime.goal(self.project, "Fix addition", request_id="request-2"))
        with self.assertRaises(ValueError):
            self.runtime.goal(self.project, "Different content", request_id="request-1")

    def test_expired_candidate_can_retry_from_fresh_run(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.store.execute("UPDATE tasks SET state='expired' WHERE id=?", (task,))
        self.runtime.retry(task)
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_new_deferral_without_run_survives_reassessment(self):
        task = self.runtime.goal(self.project, "Need evidence")
        config = self.runtime.config(self.project)
        signature = fingerprint({"body": "Need evidence", "evidence": '[{"source": "user"}]',
                                 "policy": config["triage"]["policy"]})
        self.store.defer(task, {"base": "same-sha", "triage_input": signature, "reason": "Need evidence"})
        self.runtime.reassess_deferred(self.project, config, "same-sha")
        self.assertEqual("deferred", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.runtime.reassess_deferred(self.project, config, "changed-sha")
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_requirement_revision_keeps_task_and_invalidates_inputs(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.revise_task(task, "Also cover negative numbers")
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertIn("Also cover negative numbers", row["body"])
        self.assertEqual(1, len(self.store.query("SELECT * FROM task_revisions WHERE task=?", (task,))))

    def test_dependency_wait_does_not_block_independent_work(self):
        first = self.runtime.goal(self.project, "First")
        dependent = self.runtime.goal(self.project, "Dependent")
        independent = self.runtime.goal(self.project, "Independent")
        self.runtime.set_dependency(dependent, first)
        self.runtime.pause(self.project, False)
        self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (first,))
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(independent, run["task"])

    def test_retry_feedback_includes_findings_without_acceptance_source(self):
        from adaptive_rsi.experience import retry_feedback
        result = {"review": {"decision": "fail", "findings": [{"path": "calc.py", "line": 2,
                   "reason": "Negative input fails", "suggestion": "Handle negative input"}]},
                  "acceptance": {"source": "SEALED_SECRET"}}
        feedback = retry_feedback({"id": "run-1", "base": "sha", "state": "failed", "result": json.dumps(result)})
        self.assertIn("Handle negative input", json.dumps(feedback))
        self.assertNotIn("SEALED_SECRET", json.dumps(feedback))

    def test_transient_failure_schedules_fresh_run_without_using_code_retry(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        with patch.object(self.runtime, "execute_candidate", side_effect=RuntimeError("HTTP 503 connection failed")):
            self.runtime.process(run)
        current = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual("queued", current["state"])
        self.assertEqual(1, current["infra_retries"])
        self.assertEqual(0, current["attempts"])
        self.assertGreater(current["available"], time.time())

    def test_dependency_cycles_are_rejected(self):
        first = self.runtime.goal(self.project, "First")
        second = self.runtime.goal(self.project, "Second")
        self.runtime.set_dependency(first, second)
        with self.assertRaises(ValueError):
            self.runtime.set_dependency(second, first)

    def test_backup_is_consistent_restore_is_paused_and_never_overwrites(self):
        from adaptive_rsi.storage_tools import backup, restore, diagnostics
        task = self.runtime.goal(self.project, "PRIVATE task secret=DO_NOT_EXPORT")
        saved = backup(self.store)
        destination = self.root / "restored"
        restore(self.store.root / "backups" / (saved["id"] + ".sqlite3"), destination)
        recovered = Store(destination)
        self.assertEqual(task, recovered.one("SELECT id FROM tasks")["id"])
        self.assertEqual(1, recovered.one("SELECT paused FROM projects")["paused"])
        with self.assertRaises(ValueError):
            restore(self.store.path, destination)
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(diagnostics(self.store)))
        self.assertNotIn(str(self.root), json.dumps(diagnostics(self.store)))

    def artifact_run(self):
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Keep private verification evidence")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True)
        (directory / "candidate.patch").write_bytes(b"PRIVATE original patch")
        (directory / "acceptance.json").write_text(encode({"hash": "sealed-hash", "criteria": ["preserves behavior"],
            "checks": [{"name": "preservation", "argv": ["python3", "-V"], "cwd": "."}],
            "spec": {"files": [], "checks": []}}))
        (directory / "dependencies").mkdir()
        outcome = {"pr": "https://github.com/example/project/pull/7", "merged_sha": "a" * 40,
                   "merged_at": time.time(), "evaluation": {"acceptance_hash": "sealed-hash"},
                   "feedback": {"state": "verified", "head": "old-head"}}
        self.store.execute("UPDATE runs SET path=? WHERE id=?", (str(directory), run["id"]))
        self.store.transition(run["id"], "merged", outcome, True)
        self.store.put_setting("run-baseline:" + run["id"], {"repository": "example/project", "branch": "main", "sha": "base"})
        return self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],)), directory, outcome

    def patch_response(self, store, run):
        from adaptive_rsi.server import Handler
        handler = object.__new__(Handler)
        handler.path = "/api/patch?id=" + run["id"]
        handler.server = SimpleNamespace(runtime=SimpleNamespace(store=store))
        handler.authenticated = lambda: True
        responses = []
        handler.respond = lambda data, status=200, **kwargs: responses.append((status, data))
        handler.do_GET()
        return responses[0]

    def test_restore_detaches_private_artifacts_and_local_proofs_but_keeps_remote_receipts(self):
        from adaptive_rsi.storage_tools import backup, restore, MISSING_ARTIFACTS
        self.runtime.configure({"github": {"repository": "example/project"}}, self.project)
        run, directory, outcome = self.artifact_run()
        self.assertEqual(self.patch_response(self.store, run), (200, b"PRIVATE original patch"))
        remote = {"delivery:" + run["id"]: {"head": "head", "base": "base", "branch": "main"},
                  "automation:" + run["id"] + ":deploy": {"state": "submitted", "operation_id": "receipt"}}
        for key, value in remote.items():
            self.store.put_setting(key, value)
        local_keys = [prefix + self.project for prefix in
                      ("evaluation-baseline:", "verified-checks:", "task-proof:", "task-proof-current:")]
        for key in local_keys:
            self.store.put_setting(key, {"path": str(directory), "state": "pass"})
        before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        saved = backup(self.store)
        destination = self.root / "restored-private"
        restore(self.store.root / "backups" / (saved["id"] + ".sqlite3"), destination)
        restored = Runtime(Store(destination))
        try:
            imported = restored.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
            self.assertIsNone(imported["path"])
            self.assertEqual(imported["state"], "merged")
            result = json.loads(imported["result"])
            self.assertEqual(result["pr"], outcome["pr"])
            self.assertEqual(result["feedback"]["previous"], outcome["feedback"])
            self.assertEqual(result["feedback"]["reason"], MISSING_ARTIFACTS)
            for key, value in remote.items():
                self.assertEqual(restored.store.setting(key), value)
            self.assertEqual(restored.store.setting("run-baseline:" + run["id"])["branch"], "main")
            for key in local_keys:
                self.assertIsNone(restored.store.setting(key))
            with patch.object(Path, "read_bytes", side_effect=AssertionError("private artifact read")):
                self.assertEqual(self.patch_response(restored.store, imported)[0], 400)
            restored.pause(self.project, False)
            from adaptive_rsi.delivery import tick
            receipt = remote["automation:" + run["id"] + ":deploy"]
            with patch("adaptive_rsi.delivery.origin_repository", return_value="example/project"), patch("adaptive_rsi.delivery.baseline") as baseline, patch("adaptive_rsi.delivery.snapshot") as snapshot, patch.object(restored.evaluator, "evaluate") as evaluate, patch("adaptive_rsi.release.reconcile", return_value=receipt) as reconcile:
                tick(restored, time.time(), project_id=self.project)
            reconcile.assert_called_once()
            baseline.assert_not_called()
            snapshot.assert_not_called()
            evaluate.assert_not_called()
            self.assertEqual(result["feedback"]["state"], "insufficient_evidence")
            self.assertEqual(before, {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()})
            self.assertFalse((destination / "runs").exists())
        finally:
            restored.close()

    def test_patch_and_feedback_reject_foreign_paths_and_symlinked_evidence(self):
        from adaptive_rsi.delivery import feedback
        run, directory, outcome = self.artifact_run()
        foreign = self.root / "foreign-run"
        foreign.mkdir()
        (foreign / "candidate.patch").write_bytes(b"FOREIGN private patch")
        (foreign / "acceptance.json").write_text('{"hash":"sealed-hash"}')
        for location in (foreign, directory):
            with self.subTest(location=location):
                if location == directory:
                    for name in ("candidate.patch", "acceptance.json"):
                        (directory / name).unlink()
                        (directory / name).symlink_to(foreign / name)
                self.store.execute("UPDATE runs SET path=? WHERE id=?", (str(location), run["id"]))
                current = {**run, "path": str(location)}
                with patch.object(Path, "read_bytes", side_effect=AssertionError("foreign patch read")), patch.object(Path, "read_text", side_effect=AssertionError("foreign acceptance read")):
                    self.assertEqual(self.patch_response(self.store, current)[0], 400)
                    with patch("adaptive_rsi.delivery.baseline") as baseline, patch.object(self.runtime.evaluator, "evaluate") as evaluate:
                        feedback(self.runtime, self.runtime.project(self.project), current, dict(outcome), self.runtime.config(self.project), time.time())
                    baseline.assert_not_called()
                    evaluate.assert_not_called()
        self.assertEqual(sorted(p.name for p in foreign.iterdir()), ["acceptance.json", "candidate.patch"])

    def test_feedback_missing_required_acceptance_or_dependencies_never_evaluates(self):
        from adaptive_rsi.delivery import feedback
        run, directory, outcome = self.artifact_run()
        config = self.runtime.config(self.project)
        acceptance = (directory / "acceptance.json").read_text()
        for missing in ("acceptance", "dependencies"):
            with self.subTest(missing=missing):
                saved = json.loads(run["config"])
                if missing == "acceptance":
                    (directory / "acceptance.json").unlink()
                else:
                    (directory / "acceptance.json").write_text(acceptance)
                    saved["environment"]["seed_paths"] = ["vendor"]
                current = {**run, "config": json.dumps(saved)}
                result = dict(outcome)
                with patch("adaptive_rsi.delivery.baseline") as baseline, patch.object(self.runtime.evaluator, "evaluate") as evaluate:
                    feedback(self.runtime, self.runtime.project(self.project), current, result, config, time.time())
                baseline.assert_not_called()
                evaluate.assert_not_called()
                self.assertEqual(result["feedback"]["state"], "insufficient_evidence")
                self.assertIn("fresh verification", result["feedback"]["reason"])
                self.assertFalse((directory / "feedback").exists())

    def test_feedback_rejects_preexisting_nested_output_symlinks_before_fetch(self):
        from adaptive_rsi.delivery import feedback
        run, directory, outcome = self.artifact_run()
        outputs = ("baseline", "baseline/.git", "baseline/.git/config", "current", "current/.git",
                   "current/.git/config", "evaluation", "evaluation/report.json",
                   "evaluation/baseline", "evaluation/candidate", "evaluation/acceptance_baseline",
                   "evaluation/acceptance_candidate")
        for index, name in enumerate(outputs):
            with self.subTest(output=name):
                nonce = "nested-" + str(index)
                output = directory / "feedback" / ("123-" + nonce)
                target = output / name
                target.parent.mkdir(parents=True)
                outside = self.root / ("outside-" + str(index))
                outside.mkdir()
                (outside / "marker").write_bytes(b"PRIVATE untouched")
                (outside / ".git").mkdir()
                (outside / ".git" / "config").write_bytes(b"PRIVATE git config")
                if name.endswith(("config", "report.json")):
                    target.symlink_to(outside / "marker")
                else:
                    target.symlink_to(outside, target_is_directory=True)
                before = {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()}
                result = dict(outcome)
                with patch("adaptive_rsi.delivery.time.time_ns", return_value=123), patch("adaptive_rsi.delivery.uid", return_value=nonce), patch("adaptive_rsi.delivery.baseline") as fetch, patch("adaptive_rsi.delivery.snapshot") as snapshot, patch.object(self.runtime.evaluator, "evaluate") as evaluate:
                    feedback(self.runtime, self.runtime.project(self.project), run, result, self.runtime.config(self.project), time.time())
                fetch.assert_not_called()
                snapshot.assert_not_called()
                evaluate.assert_not_called()
                self.assertEqual(result["feedback"]["state"], "insufficient_evidence")
                self.assertEqual(before, {p.relative_to(outside): p.read_bytes() for p in outside.rglob("*") if p.is_file()})

    def test_feedback_same_timestamp_uses_fresh_exclusive_output_directories(self):
        from adaptive_rsi.delivery import feedback
        run, directory, outcome = self.artifact_run()
        destinations = []
        def snapshot(source, target, sha):
            target.mkdir()
            destinations.append(target)
            return target
        with patch("adaptive_rsi.delivery.time.time_ns", return_value=123), patch("adaptive_rsi.delivery.uid", side_effect=["first", "second"]), patch("adaptive_rsi.delivery.baseline", return_value=("fixture", "new-head", "main")), patch("adaptive_rsi.delivery.snapshot", side_effect=snapshot), patch.object(self.runtime.evaluator, "evaluate", return_value={"decision": "pass", "checks": {}}):
            for _ in range(2):
                feedback(self.runtime, self.runtime.project(self.project), run, dict(outcome), self.runtime.config(self.project), time.time())
        self.assertEqual({p.parent.name for p in destinations}, {"123-first", "123-second"})
        self.assertTrue(all(p.is_relative_to(directory) for p in destinations))
        output = directory / "feedback" / "123-first"
        (output / "marker").write_bytes(b"existing evidence")
        with patch("adaptive_rsi.delivery.time.time_ns", return_value=123), patch("adaptive_rsi.delivery.uid", return_value="first"), patch("adaptive_rsi.delivery.baseline") as fetch, patch.object(self.runtime.evaluator, "evaluate") as evaluate:
            feedback(self.runtime, self.runtime.project(self.project), run, dict(outcome), self.runtime.config(self.project), time.time())
        fetch.assert_not_called()
        evaluate.assert_not_called()
        self.assertEqual((output / "marker").read_bytes(), b"existing evidence")

    def test_feedback_malformed_acceptance_and_symlink_loops_still_reconcile_receipts(self):
        from adaptive_rsi.delivery import tick
        from adaptive_rsi.store import encode
        self.runtime.configure({"github": {"repository": "example/project"}}, self.project)
        run, directory, outcome = self.artifact_run()
        receipt = {"state": "submitted", "operation_id": "receipt"}
        self.store.put_setting("delivery:" + run["id"], {"head": "head", "base": "base", "branch": "main"})
        self.store.put_setting("automation:" + run["id"] + ":deploy", receipt)
        acceptance = directory / "acceptance.json"
        original = acceptance.read_text()
        for index, malformed in enumerate(("[1]", '{"hash":"sealed-hash","spec":[]}', "not JSON", "loop")):
            with self.subTest(evidence=malformed):
                if malformed == "loop":
                    acceptance.unlink()
                    acceptance.symlink_to(acceptance.name)
                else:
                    acceptance.write_text(malformed)
                self.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
                with patch("adaptive_rsi.delivery.origin_repository", return_value="example/project"), patch("adaptive_rsi.delivery.baseline") as fetch, patch("adaptive_rsi.delivery.snapshot") as snapshot, patch.object(self.runtime.evaluator, "evaluate") as evaluate, patch("adaptive_rsi.release.reconcile", return_value=receipt) as reconcile:
                    tick(self.runtime, time.time() + index * 61, project_id=self.project)
                fetch.assert_not_called()
                snapshot.assert_not_called()
                evaluate.assert_not_called()
                reconcile.assert_called_once()
                result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
                self.assertEqual(result["feedback"]["state"], "insufficient_evidence")
                self.assertEqual(result["feedback"]["previous"]["state"], "verified")
                self.assertEqual(self.store.query("SELECT * FROM events WHERE kind='delivery_error'"), [])
        acceptance.unlink()
        acceptance.write_text(original)
        self.assertFalse((directory / "feedback").exists())

    def test_feedback_with_local_evidence_still_verifies_inside_own_run(self):
        from adaptive_rsi.delivery import feedback
        run, directory, outcome = self.artifact_run()
        run["path"] = None  # Explicitly copied artifacts can recover a restored run's evidence.
        def snapshot(source, target, sha):
            target.mkdir(parents=True)
            self.assertTrue(target.is_relative_to(directory))
            return target
        with patch("adaptive_rsi.delivery.baseline", return_value=("fixture", "new-head", "main")), patch("adaptive_rsi.delivery.snapshot", side_effect=snapshot), patch.object(self.runtime.evaluator, "evaluate", return_value={"decision": "pass", "checks": {}}) as evaluate:
            feedback(self.runtime, self.runtime.project(self.project), run, outcome, self.runtime.config(self.project), time.time())
        self.assertEqual(outcome["feedback"]["state"], "verified")
        self.assertEqual(evaluate.call_args.kwargs["acceptance"]["change_kind"], "preservation")
        self.assertEqual(evaluate.call_args.kwargs["seeds"], directory / "dependencies")
        self.assertTrue(evaluate.call_args.args[3].is_relative_to(directory))

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_review_rejection_reaches_next_implementation_without_sealed_source(self):
        class Reviewer(FakeAgent):
            rejected = False
            prompts = []

            def invoke(self, role, prompt, *args, **kwargs):
                if role == "implementation":
                    self.prompts.append(prompt)
                result = super().invoke(role, prompt, *args, **kwargs)
                if role == "review" and not self.rejected:
                    self.rejected = True
                    result.update(decision="fail", reason="Boundary issue", findings=[{
                        "severity": "high", "area": "correctness", "path": "calculator.py", "line": 2,
                        "title": "Handle boundary", "reason": "Boundary input is wrong", "suggestion": "RETRY_REVIEW_MARKER"}])
                return result

        agent = Reviewer()
        self.runtime.agent = agent
        self.runtime.configure({"checks": CHECKS, "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual("failed", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.runtime.retry(task)
        self.runtime.process(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual(2, len(agent.prompts))
        self.assertIn("RETRY_REVIEW_MARKER", agent.prompts[1])
        self.assertNotIn("assert add(2, 3) == 5", agent.prompts[1])
        self.assertEqual("awaiting_approval", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_provider_resume_is_scoped_to_revision_and_failure(self):
        from adaptive_rsi.provider_health import connection_key
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Connection failure")
        cancelled = self.runtime.goal(self.project, "Cancelled task")
        self.runtime.pause(self.project, False)
        connection = connection_key(self.runtime.config(self.project), "openai", self.project)
        for index, identifier in enumerate([task, cancelled]):
            self.store.execute("UPDATE tasks SET state=? WHERE id=?", ("blocked" if identifier == task else "cancelled", identifier))
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'blocked',?,1,2)",
                               ("resume-" + str(index), identifier, self.project, encode({"failure": {"code": "provider", "action": "retry_later", "connection": connection, "recoverable": True}})))
        self.assertEqual(0, self.runtime.resume_provider_tasks("opencode", self.project))
        self.assertEqual(1, self.runtime.resume_provider_tasks("openai", self.project))
        self.assertEqual("cancelled", self.store.one("SELECT state FROM tasks WHERE id=?", (cancelled,))["state"])
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_auth_failure_does_not_auto_retry(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        with patch.object(self.runtime, "execute_candidate", side_effect=RuntimeError("HTTP 401 unauthorized")):
            self.runtime.process(run)
        current = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.assertEqual("blocked", current["state"])
        self.assertEqual(0, current["infra_retries"])

    def test_visible_queue_matches_claim_urgency_value_and_dependency(self):
        from adaptive_rsi.dashboard import records
        low = self.runtime.goal(self.project, "Low value")
        high = self.runtime.goal(self.project, "High value")
        urgent = self.runtime.goal(self.project, "Urgent")
        self.store.execute("UPDATE tasks SET priority=1 WHERE id=?", (low,))
        self.runtime.set_dependency(urgent, urgency=1)
        displayed = records(self.store, self.project, group="active")["tasks"]
        self.assertEqual([urgent, high, low], [task["id"] for task in displayed])
        self.runtime.pause(self.project, False)
        self.assertEqual(urgent, self.store.claim(self.project, self.runtime.config(self.project))["task"])

    def test_model_cannot_grant_behavior_preservation_exception(self):
        from adaptive_rsi.workflow import change_kind
        self.assertEqual("behavior", change_kind({"title": "整理接口", "body": "Handle the reported defect"}, {"change_kind": "refactor"}))
        self.assertEqual("behavior", change_kind({"title": "Refactor module", "body": "Also fix incorrect output"}, {"change_kind": "refactor"}))
        self.assertEqual("refactor", change_kind({"title": "整理模块并保持行为不变", "body": ""}, {"change_kind": "refactor"}))

    def test_dependency_only_project_does_not_starve_another_project(self):
        from concurrent.futures import Future
        parent = self.runtime.goal(self.project, "Parent")
        dependent = self.runtime.goal(self.project, "Dependent")
        self.runtime.set_dependency(dependent, parent)
        self.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (parent,))
        other = self.runtime.add_project(fixture(self.root / "other"))
        independent = self.runtime.goal(other, "Independent work")
        self.runtime.pause(self.project, False)
        self.runtime.pause(other, False)
        with patch.object(self.runtime.pool, "submit", return_value=Future()) as submit:
            self.runtime.dispatch([(self.runtime.project(p), self.runtime.config(p)) for p in [self.project, other]])
        self.assertEqual(independent, submit.call_args[0][1]["task"])

    def test_feedback_excludes_normalized_sealed_paths_and_keeps_new_test_failure(self):
        from adaptive_rsi.experience import retry_feedback
        result = {"reason": "SEALED_SAMPLE", "failure": {"code": "review", "detail": "SEALED_SAMPLE"},
                  "review": {"findings": [{"path": "./.rsi-acceptance/check.py", "suggestion": "SEALED_SAMPLE"},
                                           {"path": "calculator.py", "reason": "Handle sample 12345"}]},
                  "evaluation": {"checks": {"candidate_full": [{"name": "new boundary", "passed": False, "exit_code": 1, "stderr": "AssertionError"}]}}}
        feedback = retry_feedback({"result": json.dumps(result)}, protected=["12345"])
        self.assertNotIn("SEALED_SAMPLE", json.dumps(feedback))
        self.assertNotIn("12345", json.dumps(feedback))
        self.assertEqual("candidate_full", feedback["failed_regression_checks"][0]["stage"])

    def test_global_resume_resets_only_matching_project_circuit(self):
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Temporary connection failure")
        self.runtime.pause(self.project, False)
        config = self.runtime.config(self.project)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "openai", self.project, RuntimeError("HTTP 503 connection failed"))
        self.assertTrue(health.status(config, "openai", self.project)["paused"])
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task,))
        self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES ('global-resume',?,?,'blocked',?,1,2)",
                           (task, self.project, encode({"failure": {"code": "provider", "action": "retry_later", "connection": connection_key(config, "openai", self.project), "recoverable": True}})))
        self.assertEqual(1, self.runtime.model_center.resume_provider("openai")["resumed_tasks"])
        self.assertFalse(health.status(config, "openai", self.project)["paused"])

    def test_performance_contract_uses_real_measurements_and_tolerance(self):
        from adaptive_rsi.workflow import evidence_contract, improvement
        task = {"source": "goal", "title": "降低接口延迟"}
        with self.assertRaises(ValueError):
            evidence_contract(task, {"change_kind": "performance"})
        plan = {"change_kind": "performance", "performance_threshold": {"metric": "latency", "unit": "ms", "target": 80, "tolerance": 2, "direction": "lower"}}
        contract = evidence_contract(task, plan)
        def sample(value):
            return {"passed": True, "stdout": json.dumps({"metric": "latency", "unit": "ms", "value": value})}
        acceptance = {"change_kind": "performance", "contract": contract}
        checks = {"acceptance_baseline": [sample(100)], "acceptance_candidate": [sample(79)]}
        self.assertEqual("pass", improvement(checks, acceptance)["decision"])
        checks["acceptance_candidate"] = [sample(99)]
        self.assertEqual("insufficient_evidence", improvement(checks, acceptance)["decision"])

    def test_metrics_count_logical_interventions_and_same_failure_only(self):
        from adaptive_rsi.telemetry import workflow_metrics
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Fix addition")
        for index, name in enumerate(["a", "b"]):
            evidence = {"failure": {"code": "checks"}, "evaluation": {"checks": {"candidate": [{"name": name, "passed": False, "exit_code": 1}]}}}
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,'failed',?,?,?)",
                               ("metric-" + name, task, self.project, encode(evidence), time.time() + index, time.time() + index + 1))
        self.assertEqual(0, workflow_metrics(self.store, self.project)["repeated_failures"])
        self.store.execute("UPDATE runs SET result=(SELECT result FROM runs WHERE id='metric-a') WHERE id='metric-b'")
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        self.runtime.retry(task)
        report = workflow_metrics(self.store, self.project)
        self.assertEqual(1, report["repeated_failures"])
        self.assertEqual(1, report["interventions"])

    def test_long_partial_and_escaped_sealed_samples_are_removed_before_truncation(self):
        from adaptive_rsi.experience import retry_feedback
        secret = "PRIVATE_SEALED_" + "ABC0123456789" * 70
        for quoted in [secret, secret[15:200], json.dumps("私密样本" * 20)[1:-1]]:
            row = {"result": json.dumps({"review": {"findings": [{"path": "app.py", "reason": "Boundary fails on " + quoted}]}})}
            feedback = retry_feedback(row, protected=[secret, "私密样本" * 20])
            self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_javascript_acceptance_numeric_samples_are_not_retry_feedback(self):
        from adaptive_rsi.experience import protected_feedback_values, retry_feedback
        from adaptive_rsi.recovery import save
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        spec = {"files": [{"path": "check.js", "content": 'const input = 31415926;\nif (add(input,1)!==31415927) throw Error("bad");'}]}
        save(self.runtime, run, "acceptance", spec, "signature")
        feedback = retry_feedback({"result": json.dumps({"review": {"findings": [{"path": "app.js", "reason": "For input 31415926 expected 31415927"}]}})}, protected_feedback_values(self.store, task))
        self.assertNotIn("31415926", json.dumps(feedback))
        self.assertNotIn("31415927", json.dumps(feedback))

    def test_shrinking_redactions_cannot_expose_truncated_sealed_samples(self):
        from adaptive_rsi.experience import retry_feedback
        secret = "TOPSECRETVALUE1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        prefixes = ["https://x/" + "a" * 700, "token=" + "a" * 700,
                    "Bearer " + "a" * 700, "sk-" + "a" * 700]
        for prefix in prefixes:
            with self.subTest(prefix=prefix[:12]):
                row = {"result": json.dumps({"review": {"findings": [
                    {"path": "app.py", "reason": prefix + " " + secret}]}})}
                feedback = retry_feedback(row, protected=[secret])
                self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_sealed_literals_and_embedded_urls_cannot_shrink_past_the_scan(self):
        from adaptive_rsi.experience import retry_feedback
        short = "0123456789ABCDEFGH"
        secret = "UNIQUE_SEALED_PRIVATE_SAMPLE_0123456789ABCDEFGHIJ"
        embedded_url = "INPUT https://example.com/long/private/path END_SECRET_79"
        contextual = "A" * 40 + " PRIVATE_TAIL"
        for reason, samples in [((short + " ") * 37 + secret, [short, secret]),
                                (embedded_url, [embedded_url]),
                                ("Bearer " + contextual, [contextual]),
                                ("https://x/" + "b" * 750 + " Bearer " + contextual,
                                 ["PREFIX_" + contextual + "_SUFFIX"])]:
            with self.subTest(reason=reason[:30]):
                row = {"result": json.dumps({"review": {"findings": [
                    {"path": "app.py", "reason": reason}]}})}
                feedback = retry_feedback(row, protected=samples)
                self.assertEqual("<sealed>", feedback["review_findings"][0]["reason"])

    def test_large_sealed_fixtures_do_not_build_an_unbounded_fragment_index(self):
        import hashlib
        import tracemalloc
        from adaptive_rsi.experience import retry_feedback
        # Eight valid-sized minified fixtures have mostly unique overlapping windows.
        samples = ["".join(hashlib.sha256(str(index * 1000 + n).encode()).hexdigest() for n in range(1000)) for index in range(8)]
        row = {"result": json.dumps({"review": {"findings": [{"path": "app.py", "reason": "Handle the empty boundary case"}]}})}
        tracemalloc.start()
        try:
            feedback = retry_feedback(row, samples)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertIn("empty boundary", feedback["review_findings"][0]["reason"])
        self.assertLess(peak, 12 * 1024 * 1024)

    def test_legacy_triage_decision_is_migrated_and_scope_is_not_complete_dependencies(self):
        from adaptive_rsi.gitops import git
        task = self.runtime.goal(self.project, "Wait for backend behavior")
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        config = self.runtime.config(self.project)
        repo = Path(self.runtime.project(self.project)["path"])
        base = git(repo, "rev-parse", "HEAD")
        self.store.put_setting("triage-state:" + task, {"base": base, "scope": ["calc.py"],
            "triage_input": fingerprint({"body": row["body"], "evidence": row["evidence"], "policy": config["triage"]["policy"]})})
        self.store.execute("UPDATE tasks SET state='deferred' WHERE id=?", (task,))
        self.runtime.reassess_deferred(self.project, config, base)
        self.assertEqual("deferred", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
        self.assertIsNotNone(self.store.one("SELECT * FROM task_decisions WHERE task=?", (task,)))
        (repo / "backend.py").write_text("API_VERSION = 2\n")
        git(repo, "add", "backend.py")
        git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-m", "Change backend dependency")
        self.runtime.reassess_deferred(self.project, config, git(repo, "rev-parse", "HEAD"))
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_planning_yield_is_not_a_code_attempt(self):
        from adaptive_rsi.telemetry import workflow_metrics
        from adaptive_rsi.store import encode
        task = self.runtime.goal(self.project, "Fix addition")
        passed = {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}
        for index, (state, evidence) in enumerate([("assessed", {}), ("awaiting_approval", passed)]):
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,?,?,?,?,?)",
                ("first-pass-" + str(index), task, self.project, state, encode(evidence), time.time() + index, time.time() + index + 1))
        self.assertEqual(1, workflow_metrics(self.store, self.project)["first_pass"])

    def test_cancel_during_infrastructure_failure_cannot_be_requeued(self):
        task = self.runtime.goal(self.project, "Fix addition")
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        transition = self.store.transition
        def concurrent_cancel(run_id, state, result=None, terminal=False, **kwargs):
            if state == "blocked":
                self.runtime.cancel_task(task)
            return transition(run_id, state, result, terminal, **kwargs)
        with patch.object(self.store, "transition", side_effect=concurrent_cancel), \
                patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
                patch.object(self.runtime, "invoke", side_effect=RuntimeError("HTTP 503 connection failed")):
            self.runtime.process(run)
        self.assertEqual("cancelled", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])

    def test_paused_implementation_provider_is_not_attributed_to_planner(self):
        from adaptive_rsi.agent import CodexAgent
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        task = self.runtime.goal(self.project, "Fix addition")
        config = self.runtime.config(self.project)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        config["models"]["implementation"]["provider"] = "opencode"
        run["config"] = json.dumps(config)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "opencode", self.project, RuntimeError("HTTP 503"))
        self.store.reserve_call(self.project, run["id"], "planning", config["models"]["planning"], config)
        agent = CodexAgent(self.store)
        def invoke(*args, **kwargs):
            return agent.invoke("implementation", "Implement", self.root, self.project, run["id"], config)
        with patch.object(self.runtime, "invoke", side_effect=invoke), patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}):
            self.runtime.process(run)
        evidence = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertIn("connection", evidence.get("failure", {}), evidence)
        self.assertEqual(connection_key(config, "opencode", self.project), evidence["failure"]["connection"])
        self.assertTrue(evidence["failure"]["recoverable"])

    def test_readiness_circuit_failure_resumes_with_connection_without_model_call(self):
        from adaptive_rsi.provider_health import ProviderHealth, connection_key
        task = self.runtime.goal(self.project, "Fix addition")
        config = self.runtime.config(self.project)
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, config)
        config["models"]["implementation"]["provider"] = "opencode"
        run["config"] = json.dumps(config)
        health = ProviderHealth(self.store)
        for _ in range(3):
            health.record(config, "opencode", self.project, RuntimeError("HTTP 503"))
        with patch.object(self.runtime, "invoke", side_effect=AssertionError("Readiness must stop before a model call")):
            self.runtime.process(run)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        evidence = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual("blocked", row["state"])
        self.assertTrue(evidence["failure"]["recoverable"])
        self.assertEqual(connection_key(config, "opencode", self.project), evidence["failure"]["connection"])
        self.assertEqual(1, self.runtime.resume_provider_tasks("opencode", self.project))
        self.assertEqual("queued", self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"])
