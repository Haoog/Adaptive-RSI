"""Regression tests for whole-project audit hardening (round 4)."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from adaptive_rsi.credentials import Credentials
from adaptive_rsi.delivery import _public_text, pr_body
from adaptive_rsi.evaluator import empty_test_run
from adaptive_rsi.native import post_json
from adaptive_rsi.process import run_process, scrub_tree, Sandbox
from adaptive_rsi.review import enforce_review
from adaptive_rsi.server_validation import SAFE_VALIDATION_MESSAGES
from adaptive_rsi.store import Store, decode_evidence


class AuditRound4Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_process_output_mb_clamped_and_wait_timeout(self):
        # Even with output_mb=0 or negative, it clamps to at least 1MB
        result = run_process(["python3", "-c", "print('hello')"], output_mb=0)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("hello", result["stdout"])

    def test_scrub_tree_handles_recursive_symlink_loops(self):
        project = self.root / "project"
        project.mkdir()
        loop = project / "loop"
        loop.symlink_to(loop)
        # Should complete without crashing on RecursionError / RuntimeError
        scrub_tree(project)

    def test_sandbox_check_handles_empty_or_malformed_argv(self):
        config = {"limits": {"command_seconds": 5, "max_output_mb": 1}}
        box = Sandbox(self.root / "state", config)
        check = {"name": "test-check", "argv": ["python3", "-c", "pass"]}
        # When argv starts with docker and ends with --name without a trailing name, cleanup must not raise IndexError
        with patch.object(box, "command", return_value=(["docker", "run", "--name"], ".")):
            with patch("adaptive_rsi.process.run_process") as mock_run:
                mock_run.return_value = {"exit_code": 1, "error": "Command timed out", "stdout": "", "stderr": ""}
                result = box._check(check, self.root)
                self.assertEqual(result["error"], "Command timed out")

    def test_store_decode_evidence_bounds_merged_at(self):
        valid = json.dumps({"merged_at": 1700000000})
        decoded = decode_evidence(valid)
        self.assertEqual(decoded["merged_at"], 1700000000)

        # Negative timestamp should return evidence_error
        negative = json.dumps({"merged_at": -100})
        self.assertEqual(decode_evidence(negative).get("evidence_error"), "invalid_evidence_shape:merged_at")

        # Beyond 64-bit int bounds should return evidence_error
        overflow = json.dumps({"merged_at": 2**64})
        self.assertEqual(decode_evidence(overflow).get("evidence_error"), "invalid_evidence_shape:merged_at")

    def test_store_schema_indexes_exist(self):
        store = Store(self.root / "state")
        with store.connect() as db:
            indexes = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
            self.assertIn("task_dependencies_dependency", indexes)
            self.assertIn("runs_task_state", indexes)
        store.close()

    def test_store_end_spans_whitelists_tables(self):
        store = Store(self.root / "state")
        with store.connect() as db:
            # Allowed tables do not raise ValueError
            store._end_spans(db, "phase_times", "run-1", 100.0)
            store._end_spans(db, "remote_waits", "run-1", 100.0)
            # Unwhitelisted table raises ValueError
            with self.assertRaisesRegex(ValueError, "Invalid span table"):
                store._end_spans(db, "users; DROP TABLE runs; --", "run-1", 100.0)
        store.close()

    def test_store_transition_task_fields_whitelisted(self):
        store = Store(self.root / "state")
        now = time.time()
        with store.connect() as db:
            db.execute("INSERT INTO projects(id, name, path, config, created) VALUES ('p1', 'p1', '/tmp/p1', '{}', ?)", (now,))
            db.execute("INSERT INTO tasks(id, project, fingerprint, title, body, state, created, updated) VALUES ('t1', 'p1', 'fp1', 'T1', 'B1', 'queued', ?, ?)", (now, now))
            db.execute("INSERT INTO runs(id, task, project, state, config, strategy, base, path, started, result) VALUES ('r1', 't1', 'p1', 'running', '{}', '{}', 'main', '/tmp/p1', ?, '{}')", (now,))
        
        # Disallowed field is ignored while allowed field is updated
        updated = store.transition("r1", "succeeded", result={"stage": "done"}, terminal=True,
                                   task_fields={"state": "merged", "injected_column": "bad"})
        self.assertTrue(updated)
        with store.connect() as db:
            task = db.execute("SELECT state FROM tasks WHERE id='t1'").fetchone()
            self.assertEqual(task["state"], "merged")
        store.close()

    def test_credentials_put_atomic_mode_600(self):
        cred_path = self.root / "creds_dir"
        creds = Credentials(cred_path)
        ref = "a" * 32
        creds.put(ref, "file", "secret-token-123")
        self.assertEqual(creds.get({"credential": "file", "secret_ref": ref}), "secret-token-123")
        file_path = cred_path / "credentials" / ref
        mode = stat.S_IMODE(file_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_server_validation_safe_messages(self):
        expected_strings = [
            "Task dependency cycle",
            "Dependency must be another task in the same project",
            "Urgency must be 0 or 1",
            "Only waiting tasks can be scheduled",
            "Automatic delivery must be explicitly enabled per project",
            "Parallel project limit is a global setting; select global defaults",
            "Software updates are a global setting",
            "Only a verified candidate awaiting approval can be delivered",
            "Independent gates have not passed",
            "No verified code candidate to revert",
            "Explicit confirmation is required to reset invalid project settings",
            "Project configuration is valid; reset is unavailable",
            "Project configuration must be a JSON object",
            "Configuration must be an object",
        ]
        for msg in expected_strings:
            self.assertIn(msg, SAFE_VALIDATION_MESSAGES, f"Missing safe validation string: {msg}")

    def test_native_post_json_validates_envelope_structure(self):
        # Empty or non-dict envelope returns failure response
        with patch("adaptive_rsi.process.run_process") as mock_run:
            mock_run.return_value = {
                "exit_code": 0, "error": None, "error_kind": None,
                "stdout": "[]", "stderr": ""
            }
            with self.assertRaisesRegex(RuntimeError, "Envelope must be an object"):
                post_json("https://api.test/model", {}, {}, 10, 1024, cancel=lambda: False)

    def test_delivery_public_text_strips_control_characters(self):
        raw = "Hello\x00\x08World\x1f!\t\n"
        cleaned = _public_text(raw)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\x08", cleaned)
        self.assertNotIn("\x1f", cleaned)
        self.assertIn("HelloWorld!", cleaned)

    def test_delivery_pr_body_truncates_large_file_lists(self):
        task = {"id": "task-1", "title": "Large Change", "summary": "A large task"}
        run = {"id": "run-1", "base": "main"}
        outcome = {
            "files": [f"file_{i}.py" for i in range(75)],
            "summary": "Completed verified improvement.",
            "evaluation": {"decision": "pass"},
            "review": {"decision": "pass"}
        }
        body = pr_body(task, run, outcome)
        self.assertIn("还有 25 个文件", body)
        self.assertIn("file_0.py", body)

    def test_evaluator_empty_test_run_cargo_patterns(self):
        cargo_ok = "test result: ok. 10 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.13s"
        self.assertFalse(empty_test_run(cargo_ok))

        cargo_fail = "test result: FAILED. 8 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.13s"
        self.assertFalse(empty_test_run(cargo_fail))

    def test_review_enforce_finding_line_and_rank_defensive(self):
        valid_review = {
            "decision": "pass",
            "reason": "All checks passed cleanly.",
            "assessments": [
                {"area": "correctness", "decision": "pass", "reason": "criteria met"},
                {"area": "security", "decision": "pass", "reason": "no secrets"},
                {"area": "maintainability", "decision": "pass", "reason": "clean code"},
                {"area": "architecture", "decision": "pass", "reason": "follows boundaries"},
                {"area": "tests", "decision": "pass", "reason": "adequate tests"},
                {"area": "scope", "decision": "pass", "reason": "focused diff"},
            ],
            "findings": [
                {
                    "area": "correctness",
                    "path": "src/main.py",
                    "line": 42,
                    "title": "Minor typo",
                    "reason": "Docstring typo",
                    "suggestion": "Fix spelling",
                    "severity": "low",
                }
            ],
        }
        # Tolerates unexpected block_on configuration
        result = enforce_review(valid_review, {"review": {"block_on": "nonexistent"}})
        self.assertEqual(result["decision"], "pass")


if __name__ == "__main__":
    unittest.main()
