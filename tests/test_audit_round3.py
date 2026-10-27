"""Regression tests for the whole-project audit hardening (round 3)."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.evaluator import empty_test_run, freeze_harness
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
