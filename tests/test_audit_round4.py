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
from adaptive_rsi.evaluator import clarity_test_summary, empty_test_run
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

    def test_evaluator_empty_test_run_unittest_zero_and_tap(self):
        # unittest 零发现时结尾恒有 OK 行，裸 OK 不得否决确定性的零计数信号。
        self.assertTrue(empty_test_run("Ran 0 tests in 0.001s\n\nOK\n"))
        self.assertFalse(empty_test_run("test_x (m.T) ... ok\n\nRan 1 test in 0.0s\n\nOK\n"))
        # TAP 计划行 1..0 是零用例；版本串 "1.0 " 不得误判为空跑。
        self.assertTrue(empty_test_run("TAP version 13\n1..0"))
        self.assertFalse(empty_test_run("gcc 1.0 \nBuild ok"))
        # 子测试里的 1..0 # SKIP 不能否决外层编号 TAP 结果：ok 2 - actual 是真实执行证据。
        nested_skip = ("TAP version 13\n# Subtest: empty\n    1..0 # SKIP empty\n"
                       "ok 1 - empty # SKIP\nok 2 - actual\n1..2\n")
        self.assertFalse(empty_test_run(nested_skip))
        # 全部跳过的 TAP 运行仍视为有执行证据（TAP 把 skip 计为已执行测试点）。
        self.assertFalse(empty_test_run("TAP version 13\nok 1 - x # SKIP\n1..1\n"))
        # 纯 1..0 外层计划且无任何编号结果时仍是空跑。
        self.assertTrue(empty_test_run("TAP version 13\n1..0 # SKIP nothing\n"))

    def test_evaluator_empty_test_run_go_single_package_zero(self):
        # go test -run 过滤后零执行时仍打印 PASS 与 `ok pkg [no tests to run]`；
        # 这两类行只表示过滤生效，不是执行证据。
        zero = "testing: warning: no tests to run\nPASS\nok  \tpkg\t0.004s [no tests to run]\n"
        self.assertTrue(empty_test_run(zero))
        # 只有 no test files 的包同样不算执行证据。
        self.assertTrue(empty_test_run("?   \tpkg\t[no test files]\n"))

    def test_evaluator_empty_test_run_go_mixed_packages(self):
        # 多包混合：另一个包的裸 ok 行仍是真实执行证据，不能因零执行包被判为空跑。
        mixed = ("testing: warning: no tests to run\nPASS\n"
                 "ok  \tpkg1\t0.004s [no tests to run]\nok  \tpkg2\t0.5s\n")
        self.assertFalse(empty_test_run(mixed))
        self.assertFalse(empty_test_run("ok  \tpkg1\t0.5s\nok  \tpkg2\t0.004s [no tests to run]\n"))
        self.assertFalse(empty_test_run("?   \tpkg1\t[no test files]\nok  \tpkg2\t0.5s\n"))
        # PASS 行不能吃掉紧随其后 ok 行的前导换行：否则 finditer 非重叠扫描漏掉
        # 真正执行过测试的包，混包输出被误判为空跑。
        self.assertFalse(empty_test_run("?   \tpkg1\t[no test files]\nPASS\nok  \tpkg2\t0.5s\n"))
        # 两个包的 ok 行都带零执行标记时，PASS 行仍是唯一命中，必须判为空跑。
        self.assertTrue(empty_test_run("?   \tpkg1\t[no test files]\nPASS\nok  \tpkg2\t[no tests to run]\n"))

    def test_evaluator_empty_test_run_large_output_is_fast(self):
        # 12 MB 输出上限下不应逐字符跑四个正则：旧实现 8 MB 约需 7 s。
        text = "".join("=== RUN TestCase%d\n--- PASS: TestCase%d (0.00s)\n" % (i, i)
                       for i in range(150000))
        began = time.monotonic()
        self.assertFalse(empty_test_run(text))
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 2.5, "empty_test_run took %.2fs on %.1f MB" % (elapsed, len(text) / 1048576))

    def test_clarity_test_summary_field_order_agnostic(self):
        # pytest 7+ prints failures first; field order must not matter.
        failure_first = clarity_test_summary("===== 2 failed, 3 passed, 1 skipped in 4.21s =====")
        passed_first = clarity_test_summary("===== 3 passed, 2 failed, 1 skipped in 4.21s =====")
        self.assertIsNotNone(failure_first)
        self.assertIsNotNone(passed_first)
        for key in ("executed", "passed", "failed", "skipped"):
            self.assertEqual(failure_first[key], passed_first[key])
        # Skipped tests never ran their body, so they do not inflate executed.
        self.assertEqual(failure_first["executed"], 5)
        self.assertEqual(failure_first["failed"], 2)
        self.assertEqual(failure_first["skipped"], 1)

    def test_clarity_test_summary_counts_errors_and_deselected(self):
        summary = clarity_test_summary("==== 1 error, 4 passed, 2 deselected in 0.50s ====")
        self.assertIsNotNone(summary)
        # Errors are reported separately instead of inflating ``failed``: the
        # error may share a test with ``passed`` (fixture teardown), so merging
        # it would double-count that test in ``executed``.
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["errors"], 1)
        # Deselected tests never ran: they stay out of executed/skipped and are
        # reported separately so evidence does not overstate coverage.
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["executed"], 4)
        self.assertEqual(summary["deselected"], 2)

    def test_clarity_test_summary_teardown_error_not_double_counted(self):
        # A test whose body passed but whose fixture teardown failed reports
        # ``1 passed, 1 error`` for a single collected test.
        summary = clarity_test_summary("===== 1 passed, 1 error in 0.30s =====")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["errors"], 1)

    def test_clarity_test_summary_accepts_humanized_duration(self):
        # pytest prints ``in 65.00s (0:01:05)`` for runs of 60+ seconds.
        summary = clarity_test_summary("===== 3 passed in 65.00s (0:01:05) =====")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["executed"], 3)

    def test_clarity_test_summary_rejects_zero_passed(self):
        self.assertIsNone(clarity_test_summary("===== 0 passed, 1 failed in 0.10s ====="))
        self.assertIsNone(clarity_test_summary("===== no tests ran in 0.01s ====="))
        self.assertIsNone(clarity_test_summary("===== 2 errors in 0.05s ====="))

    def test_clarity_test_summary_reads_failure_only_run(self):
        # pytest omits zero-count fields, so an all-failing run ends with
        # ``1 failed in 0.03s`` and must still yield structured stats.
        summary = clarity_test_summary("===== 1 failed in 0.03s =====")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["passed"], 0)
        self.assertEqual(summary["failed"], 1)

    def test_clarity_test_summary_counts_skipped_only_run(self):
        summary = clarity_test_summary("===== 2 skipped in 0.05s =====")
        self.assertIsNotNone(summary)
        # Skipped tests are collected but never executed.
        self.assertEqual(summary["executed"], 0)
        self.assertEqual(summary["skipped"], 2)

    def test_evaluator_empty_test_run_all_skipped(self):
        # unittest: two collected tests, both skipped, exit code still 0.
        self.assertTrue(empty_test_run("Ran 2 tests in 0.001s\n\nOK (skipped=2)\n"))
        # node --test TAP and spec reporter summaries; zero pass/fail.
        self.assertTrue(empty_test_run(
            "# tests 2\n# suites 0\n# pass 0\n# fail 0\n# cancelled 0\n# skipped 2\n# todo 0\n"))
        self.assertTrue(empty_test_run(
            "\u2139 tests 2\n\u2139 suites 0\n\u2139 pass 0\n\u2139 fail 0\n\u2139 skipped 2\n"))
        # pytest CLI summary for a run where every test was skipped.
        self.assertTrue(empty_test_run("===== 2 skipped in 0.01s ====="))
        # JUnit XML reporting only skipped cases.
        self.assertTrue(empty_test_run('<testsuite tests="2" failures="0" errors="0" skipped="2"/>'))
        # Verbose skip identities without a surviving summary line.
        self.assertTrue(empty_test_run("test_a (m.T) ... skipped 'platform'\n"
                                       "test_b (m.T) ... skipped 'platform'\n"))
        # One executed test keeps the run non-empty even when others skip.
        self.assertFalse(empty_test_run("Ran 3 tests in 0.002s\n\nOK (skipped=2)\n"))
        self.assertFalse(empty_test_run("# tests 3\n# pass 1\n# fail 0\n# skipped 2\n"))
        self.assertFalse(empty_test_run("===== 1 passed, 2 skipped in 0.02s ====="))
        self.assertFalse(empty_test_run("test_a (m.T) ... ok\n"
                                        "test_b (m.T) ... skipped 'platform'\n"))
        # TAP skipped points still count as executed evidence for this gate.
        self.assertFalse(empty_test_run("TAP version 13\nok 1 - x # SKIP\n1..1\n"))

    def test_evaluator_empty_test_run_quiet_pytest_all_skipped(self):
        # ``pytest -q`` prints no ``=`` rule; the bare summary line is the only
        # evidence that every collected test body was skipped.
        self.assertTrue(empty_test_run("ss                                                                       [100%]\n"
                                       "2 skipped in 0.01s\n"))
        self.assertTrue(empty_test_run("2 skipped, 1 warning in 0.01s\n"))
        self.assertTrue(empty_test_run("2 skipped in 65.00s (0:01:05)\n"))
        # One executed test keeps the quiet run non-empty.
        self.assertFalse(empty_test_run("1 passed, 1 skipped in 0.01s\n"))
        # A timing line without a test outcome is not a quiet summary.
        self.assertFalse(empty_test_run("3 warnings in 2.1s\n"))
        # The decorated form stays the only ``clarity_test_summary`` source.
        self.assertIsNone(clarity_test_summary("2 skipped in 0.01s\n"))

    def test_evaluator_empty_test_run_quiet_pytest_multiple_suites(self):
        # A wrapper script runs several suites and prints one quiet summary
        # per suite. A skip-only first suite must not hide a later suite that
        # executed tests, and the verdict cannot depend on the print order.
        self.assertFalse(empty_test_run("2 skipped in 0.01s\n3 passed in 0.02s\n"))
        self.assertFalse(empty_test_run("3 passed in 0.02s\n2 skipped in 0.01s\n"))
        self.assertFalse(empty_test_run("1 failed, 1 skipped in 0.01s\n2 skipped in 0.02s\n"))
        # Only when every recognizable suite reports skips does it stay empty.
        self.assertTrue(empty_test_run("2 skipped in 0.01s\n5 skipped, 1 warning in 0.02s\n"))
        # xfail/xpass test bodies really ran, so a skip-only suite must not
        # hide them when a wrapper concatenates the two summaries.
        self.assertFalse(empty_test_run("1 skipped in 0.01s\n1 xfailed in 0.02s\n"))
        self.assertFalse(empty_test_run("1 xfailed in 0.02s\n1 skipped in 0.01s\n"))
        self.assertFalse(empty_test_run("1 skipped in 0.01s\n1 xpassed in 0.02s\n"))
        self.assertFalse(empty_test_run("2 skipped, 1 xfailed in 0.02s\n"))

    def test_evaluator_empty_test_run_cargo_all_ignored(self):
        ignored = ("test result: ok. 0 passed; 0 failed; 5 ignored; 0 measured; "
                   "0 filtered out; finished in 0.00s")
        self.assertTrue(empty_test_run(ignored))
        self.assertFalse(empty_test_run(
            "test result: ok. 1 passed; 0 failed; 4 ignored; 0 measured; 0 filtered out; "
            "finished in 0.01s"))
        # A separate binary that really ran tests keeps the whole run non-empty.
        self.assertFalse(empty_test_run(
            ignored + "\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; "
                      "0 filtered out; finished in 0.01s"))
        # An executed count anywhere still means the run was not skip-only.
        self.assertFalse(empty_test_run("test result: ok. 0 passed; 4 ignored; 1 passed"))

    def test_standard_stage_flags_quiet_pytest_all_skipped(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.evaluator import Evaluator
        checks = [{"name": "unit", "argv": ["python3", "-m", "pytest", "-q"]}]

        def execute(_sandbox, check, workspace, *args, **kwargs):
            return {"name": check["name"], "argv": check["argv"], "exit_code": 0,
                    "stdout": "ss                                                                       [100%]\n"
                              "2 skipped in 0.01s\n",
                    "stderr": "", "error": None}

        source = self.root / "source"
        source.mkdir()
        (source / "app.py").write_text("VALUE = 1\n")
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        runner = Evaluator(store)
        config = merge(DEFAULTS, {})
        with patch("adaptive_rsi.evaluator.Sandbox.check", autospec=True, side_effect=execute):
            rows = runner._standard_stage("candidate", source, source, source, checks,
                                          self.root / "evaluation", config, None, None, None,
                                          [])["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["empty_tests"])
        self.assertFalse(rows[0]["passed"])

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
