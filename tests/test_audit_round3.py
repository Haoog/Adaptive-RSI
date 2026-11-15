"""Regression tests for the whole-project audit hardening (round 3)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.evaluator import Evaluator, _changed_harness, empty_test_run, freeze_harness
from adaptive_rsi.native import Conversation, tools_for
from adaptive_rsi.process import scrub_tree
from adaptive_rsi.release import compatible_workflow
from adaptive_rsi.server import Handler
from adaptive_rsi.storage_tools import backup, archive
from adaptive_rsi.store import Store


class AuditRound3Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_scrub_tree_allows_benign_external_symlinks(self):
        project = self.root / "project"
        project.mkdir()
        external = self.root / "external_node_modules"
        external.mkdir()
        (external / "package.json").write_text('{"name": "external"}')
        (project / "node_modules").symlink_to(external)
        # Benign external symlinks (e.g. pnpm / venv) should not crash scrub_tree
        scrub_tree(project)

        # But symlinks targeting controller state must be rejected
        state = self.root / "state"
        state.mkdir()
        (project / "leak_state").symlink_to(state)
        with self.assertRaisesRegex(RuntimeError, "External symlink cannot enter protected host state"):
            scrub_tree(project, state=state)


    def test_empty_test_run_recognizes_unicode_and_vitest_output(self):
        vitest_output = """
        ✓ src/index.test.ts (3 tests)
        ✓ 12 passed in 1.45s
        """
        self.assertFalse(empty_test_run(vitest_output))

        bun_output = "✔ 8 tests passed"
        self.assertFalse(empty_test_run(bun_output))

        empty_output = "No tests found, exiting with code 0"
        self.assertTrue(empty_test_run(empty_output))

    def test_freeze_harness_skips_broken_symlinks(self):
        baseline = self.root / "baseline"
        target = self.root / "target"
        baseline.mkdir()
        target.mkdir()
        test_file = baseline / "test_example.py"
        test_file.write_text("def test_ok(): pass")
        # Broken symlink
        (baseline / "test_broken.py").symlink_to(self.root / "nonexistent.py")
        freeze_harness(baseline, target)
        self.assertTrue((target / "test_example.py").is_file())
        self.assertFalse((target / "test_broken.py").exists())

    def test_freeze_harness_restores_baseline_test_data_without_dropping_new_fixtures(self):
        baseline = self.root / "baseline-data"
        candidate = self.root / "candidate-data"
        target = self.root / "target-data"
        for root in (baseline, candidate):
            (root / "pkg" / "testdata").mkdir(parents=True)
            (root / "pkg" / "testdata" / "golden.json").write_text('{"v": 1}')
            (root / "pkg" / "x_test.go").write_text("func TestX(t *testing.T) {}\n")
            (root / "src" / "__snapshots__").mkdir(parents=True)
            (root / "src" / "__snapshots__" / "ui.snap").write_text("expected\n")
        (candidate / "pkg" / "testdata" / "golden.json").write_text('{"v": 2}')
        (candidate / "pkg" / "testdata" / "new.json").write_text('{"new": true}')
        (candidate / "src" / "__snapshots__" / "ui.snap").write_text("approximate\n")
        shutil.copytree(candidate, target)

        freeze_harness(baseline, target)

        self.assertEqual((target / "pkg" / "testdata" / "golden.json").read_text(), '{"v": 1}')
        self.assertEqual((target / "src" / "__snapshots__" / "ui.snap").read_text(), "expected\n")
        self.assertEqual((target / "pkg" / "testdata" / "new.json").read_text(), '{"new": true}')
        self.assertTrue(_changed_harness(["pkg/testdata/golden.json"], [], baseline))
        self.assertTrue(_changed_harness(["src/__snapshots__/ui.snap"], [], baseline))
        # A fixture added for a new candidate test is not drift in the baseline.
        self.assertFalse(_changed_harness(["pkg/testdata/new.json"], [], baseline))
        # Application data under src/fixtures stays candidate-owned.
        (baseline / "src" / "fixtures").mkdir(parents=True)
        (baseline / "src" / "fixtures" / "data.json").write_text("{}")
        self.assertFalse(_changed_harness(["src/fixtures/data.json"], [], baseline))

    def test_freeze_baseline_fixture_symlink_and_reject_substitution(self):
        baseline = self.root / 'baseline-link'
        target = self.root / 'target-link'
        (baseline / 'pkg' / 'testdata').mkdir(parents=True)
        (baseline / 'shared').mkdir()
        (baseline / 'shared' / 'expected.json').write_text('baseline')
        link = baseline / 'pkg' / 'testdata' / 'expected.json'
        link.symlink_to('../../shared/expected.json')
        shutil.copytree(baseline, target, symlinks=True)
        (target / 'shared' / 'expected.json').write_text('candidate')
        freeze_harness(baseline, target)
        self.assertEqual((target / 'pkg' / 'testdata' / 'expected.json').read_text(), 'baseline')
        self.assertFalse((target / 'pkg' / 'testdata' / 'expected.json').is_symlink())
        dst = target / 'pkg' / 'testdata' / 'expected.json'
        dst.unlink()
        dst.symlink_to('../../shared/other.json')
        with self.assertRaisesRegex(RuntimeError, 'Candidate symlink'):
            freeze_harness(baseline, target)
        self.assertEqual((target / 'shared' / 'expected.json').read_text(), 'candidate')

    def test_candidate_stage_freezes_fixture_directory_symlink_target(self):
        baseline = self.root / "baseline-dir-link"
        candidate = self.root / "candidate-dir-link"
        fixture = Path("shared/fixtures/expected.json")
        (baseline / fixture).parent.mkdir(parents=True)
        (baseline / fixture).write_text("baseline")
        (baseline / "pkg").mkdir()
        (baseline / "pkg" / "testdata").symlink_to("../shared/fixtures", target_is_directory=True)
        shutil.copytree(baseline, candidate, symlinks=True)
        (candidate / fixture).write_text("candidate")
        store = Store(self.root / "state-dir-link")
        self.addCleanup(store.close)
        evaluator = Evaluator(store)
        observed = []

        def check(_spec, target, *_args):
            observed.append({
                "fixture": (Path(target) / "pkg" / "testdata" / "expected.json").read_text(),
                "protected": [Path(path) for path in _args[1]],
            })
            return {"exit_code": 0, "error": None, "stdout": "1 passed in 0.01s", "stderr": ""}

        config = {"environment": {"seed_paths": [], "setup": []}, "limits": {"concurrency": 1}}
        with patch("adaptive_rsi.evaluator.Sandbox.check", side_effect=check):
            evaluator._standard_stage(
                "candidate", candidate, baseline, candidate,
                [{"name": "unit", "argv": ["python3", "-c", "pass"]}],
                self.root / "dir-link-stage", config, None, None, None, [])

        self.assertEqual(observed[0]["fixture"], "baseline")
        self.assertFalse((self.root / "dir-link-stage" / "candidate" / "pkg" / "testdata").is_symlink())
        self.assertIn(self.root / "dir-link-stage" / "candidate" / "pkg" / "testdata",
                      observed[0]["protected"])
        self.assertTrue(_changed_harness([fixture.as_posix()], [], baseline))

    def test_candidate_stage_rejects_fixture_symlink_into_protected_state(self):
        project = self.root / "state-link-project"
        state = self.root / "protected-state"
        (project / "pkg" / "testdata").mkdir(parents=True)
        state.mkdir()
        (state / "state.json").write_text("protected")
        (project / "pkg" / "testdata" / "expected.json").symlink_to(state / "state.json")
        evaluator = Evaluator(Store(state))
        self.addCleanup(evaluator.store.close)
        config = {"environment": {"seed_paths": [], "setup": []}, "limits": {"concurrency": 1}}

        with patch("adaptive_rsi.evaluator.Sandbox.check") as check:
            with self.assertRaisesRegex(RuntimeError, "External symlink cannot enter protected host state"):
                evaluator._standard_stage(
                    "candidate", project, project, project,
                    [{"name": "unit", "argv": ["python3", "-c", "pass"]}],
                    self.root / "state-link-stage", config, None, None, None, [])

        check.assert_not_called()
        self.assertFalse((self.root / "state-link-stage" / "candidate").exists())

    def test_candidate_stage_rejects_nested_state_link_in_fixture_directory_symlink(self):
        project = self.root / "nested-state-link-project"
        fixture_root = project / "shared" / "fixtures"
        (fixture_root / "nested").mkdir(parents=True)
        (project / "pkg").mkdir()
        state = self.root / "nested-protected-state"
        state.mkdir()
        (state / "secret.json").write_text("protected")
        (fixture_root / "nested" / "expected.json").symlink_to(state / "secret.json")
        (project / "pkg" / "testdata").symlink_to("../shared/fixtures", target_is_directory=True)
        evaluator = Evaluator(Store(state))
        self.addCleanup(evaluator.store.close)
        config = {"environment": {"seed_paths": [], "setup": []}, "limits": {"concurrency": 1}}

        with patch("adaptive_rsi.evaluator.Sandbox.check") as check:
            with self.assertRaisesRegex(RuntimeError, "External symlink cannot enter protected host state"):
                evaluator._standard_stage(
                    "candidate", project, project, project,
                    [{"name": "unit", "argv": ["python3", "-c", "pass"]}],
                    self.root / "nested-state-link-stage", config, None, None, None, [])

        check.assert_not_called()
        self.assertFalse((self.root / "nested-state-link-stage" / "candidate").exists())

    def test_candidate_stage_materializes_safe_fixture_links_inside_fixture_directory_link(self):
        baseline = self.root / "nested-fixture-link-baseline"
        candidate = self.root / "nested-fixture-link-candidate"
        fixture_root = baseline / "shared" / "fixtures"
        (fixture_root / "nested").mkdir(parents=True)
        (fixture_root / "value.json").write_text("baseline")
        (fixture_root / "nested" / "alias.json").symlink_to("../value.json")
        (baseline / "pkg").mkdir()
        (baseline / "pkg" / "testdata").symlink_to("../shared/fixtures", target_is_directory=True)
        shutil.copytree(baseline, candidate, symlinks=True)
        (candidate / "shared" / "fixtures" / "value.json").write_text("candidate")
        evaluator = Evaluator(Store(self.root / "nested-fixture-link-state"))
        self.addCleanup(evaluator.store.close)
        observed = []

        def check(_spec, target, *_args):
            expected = Path(target) / "pkg" / "testdata" / "nested" / "alias.json"
            observed.append((expected.read_text(), expected.is_symlink(),
                             (Path(target) / "pkg" / "testdata").is_symlink()))
            return {"exit_code": 0, "error": None, "stdout": "1 passed in 0.01s", "stderr": ""}

        config = {"environment": {"seed_paths": [], "setup": []}, "limits": {"concurrency": 1}}
        with patch("adaptive_rsi.evaluator.Sandbox.check", side_effect=check):
            evaluator._standard_stage(
                "candidate", candidate, baseline, candidate,
                [{"name": "unit", "argv": ["python3", "-c", "pass"]}],
                self.root / "nested-fixture-link-stage", config, None, None, None, [])

        self.assertEqual(observed[0], ("baseline", False, False))

    def test_conversation_trim_history_bounds_memory(self):
        conv = Conversation("chat_completions", "test-model", "initial instructions", tools_for("planning"), "default")
        # Append many large turns
        for i in range(12):
            conv.history.append({"role": "assistant", "content": "thinking " + str(i)})
            conv.history.append({"role": "tool", "tool_call_id": str(i), "content": "x" * 5000})

        # Trim with a tight byte budget
        conv.trim_history(10000)
        # Earlier items should have their content trimmed
        trimmed = any("...[output trimmed for context capacity]..." in str(item.get("content")) for item in conv.history[1:-4])
        self.assertTrue(trimmed)

    def test_compatible_workflow_allows_comments_and_quotes(self):
        workflow_yaml = """
name: Release
run-name: "${{ inputs.rsi_operation_id }}" # release dispatch
on:
  workflow_dispatch:
    inputs:
      rsi_operation_id:
        required: 'true'
        type: "string"
      rsi_sha:
        required: true
        type: string
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: echo OK
"""
        self.assertTrue(compatible_workflow(workflow_yaml))

    def test_server_csp_includes_object_src_none(self):
        handler = object.__new__(Handler)
        headers = {}
        handler.send_header = lambda k, v: headers.update({k: v})
        handler.headers_common()
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("object-src 'none'", csp)


if __name__ == "__main__":
    unittest.main()
