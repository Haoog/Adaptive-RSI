import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from adaptive_rsi.config import fingerprint
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent, fixture


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
