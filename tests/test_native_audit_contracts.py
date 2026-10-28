"""Offline contracts for native tool indexing and response boundaries."""
import json
import subprocess
import tempfile
import unittest
import urllib.error
import http.client
from pathlib import Path
from unittest.mock import patch
from email.message import Message

from adaptive_rsi.native import Conversation, WorkspaceTools, tools_for, post_json, ProviderUnavailable, execute_tools


class NativeAuditContractsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = WorkspaceTools(self.root, "review", self.root / "state", {"limits": {"command_seconds": 30}}, None, ())

    def test_transport_receipts_distinguish_rejected_and_ambiguous_billing(self):
        errors = [
            (urllib.error.URLError(ConnectionRefusedError()), "not_sent", False),
            (http.client.RemoteDisconnected("response lost"), "unknown", True),
            (urllib.error.HTTPError("http://localhost", 429, "limited", Message(), None), "rejected", False),
        ]
        for error, outcome, possible in errors:
            with self.subTest(outcome=outcome), patch("adaptive_rsi.native.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ProviderUnavailable) as caught:
                    post_json("http://localhost", {}, {}, 1, 1000)
                self.assertEqual(caught.exception.request_outcome, outcome)
                self.assertEqual(caught.exception.possibly_billed, possible)
                self.assertEqual(caught.exception.retry_may_duplicate, possible)

    def test_malformed_protocol_responses_do_not_mutate_history(self):
        fixtures = {
            "responses": {"output": [{"type": "function_call", "call_id": [], "name": "search", "arguments": "{}"}]},
            "chat_completions": {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": [], "function": {"name": "search", "arguments": "{}"}}]}}]},
            "ollama": {"message": {"role": "assistant", "tool_calls": [{"id": [], "function": {"name": "search", "arguments": {}}}]}},
            "anthropic": {"content": [{"type": "tool_use", "id": [], "name": "search", "input": {}}]},
            "gemini": {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"id": [], "name": "search", "args": {}}}]}}]},
        }
        for protocol, response in fixtures.items():
            with self.subTest(protocol=protocol):
                conversation = Conversation(protocol, "model", "task", tools_for("review"), "default")
                before = json.dumps(conversation.history)
                with self.assertRaisesRegex(RuntimeError, "invalid tool response structure"):
                    conversation.receive(response)
                self.assertEqual(json.dumps(conversation.history), before)

    def test_regex_search_and_invalid_patterns(self):
        (self.root / "source.py").write_text("item 12\nitem no\nITEM 34\n")
        result = self.tools.execute("search", {"text": r"item \d+", "regex": True, "case_sensitive": False})
        self.assertEqual([row["line"] for row in result["matches"]], [1, 3])
        with self.assertRaisesRegex(ValueError, "Invalid regular expression"):
            self.tools.execute("search", {"text": "[", "regex": True})

    def test_regex_pathological_pattern_is_bounded(self):
        (self.root / "source.py").write_text("a" * 10000 + "!\n")
        result = self.tools.execute("search", {"text": "(a+)+$", "regex": True})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reason"], "regex time limit")
        self.assertIsNotNone(result["next_cursor"])

    def test_regex_pages_and_invalid_search_inputs(self):
        (self.root / "a.py").write_text("item 1\nitem 2\nitem 3\n")
        first = self.tools.execute("search", {"text": r"item \d", "regex": True, "limit": 1})
        second = self.tools.execute("search", {"text": r"item \d", "regex": True, "limit": 2,
                                                "cursor": first["next_cursor"]})
        self.assertEqual([row["line"] for row in second["matches"]], [2, 3])
        self.assertFalse(second["truncated"])
        for extra in [{"text": "x" * 1001}, {"limit": 0}, {"path": "../*"},
                      {"cursor": "[]"}, {"text": "different", "cursor": first["next_cursor"]}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.tools.execute("search", {"text": "item", **extra})

    def test_regex_cancellation_terminates_worker(self):
        from adaptive_rsi.process import Cancelled
        (self.root / "a.py").write_text("a" * 10000 + "!\n")
        import time
        started = time.monotonic()
        self.tools.cancel = lambda: time.monotonic() - started > 0.2
        with self.assertRaises(Cancelled):
            self.tools.execute("search", {"text": "(a+)+$", "regex": True})

    def test_skipped_files_are_reported(self):
        (self.root / "large.py").write_bytes(b"x" * (1024 * 1024 + 1))
        (self.root / "binary.py").write_bytes(b"\xff")
        result = self.tools.execute("search", {"text": "x"})
        self.assertEqual(result["skipped_files"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reason"], "unreadable or oversized files")

    def test_unicode_search_budget_keeps_results_and_continuation(self):
        (self.root / "a.py").write_text(("中" * 1000 + "\n") * 100)
        result = execute_tools(self.tools, [("one", "search", {"text": "中"})])[0][2]
        self.assertGreater(len(result["matches"]), 0)
        self.assertLess(len(json.dumps(result).encode()), 128000)
        self.assertEqual(result["reason"], "result byte limit")
        self.assertIsNotNone(result["next_cursor"])

    def test_search_scopes_case_and_resumes_matches(self):
        (self.root / "a.py").write_text("HELLO 1\nhello 2\nhello 3\n")
        (self.root / "a.txt").write_text("hello excluded\n")
        first = self.tools.execute("search", {"text": "hello", "path": "*.py", "case_sensitive": False, "limit": 2})
        self.assertEqual([row["line"] for row in first["matches"]], [1, 2])
        self.assertEqual(first["reason"], "match limit")
        second = self.tools.execute("search", {"text": "hello", "path": "*.py", "case_sensitive": False,
                                                "cursor": first["next_cursor"], "limit": 2})
        self.assertEqual([row["line"] for row in second["matches"]], [3])
        self.assertFalse(second["truncated"])

    def test_git_index_honors_nested_ignore_and_tracked_inclusion(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text("*.log\n!keep.log\n")
        (self.root / "a.log").write_text("ignored")
        (self.root / "keep.log").write_text("included")
        (self.root / "tracked.log").write_text("tracked")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "tracked.log"], check=True)
        (self.root / "sub").mkdir()
        (self.root / "sub/.gitignore").write_text("private.py\n")
        (self.root / "sub/private.py").write_text("ignored")
        (self.root / "sub/new.py").write_text("source")
        (self.root / ".rsiignore").write_text("excluded.py\n")
        (self.root / "excluded.py").write_text("user excluded")
        files = self.tools.files()
        self.assertNotIn("a.log", files)
        self.assertNotIn("sub/private.py", files)
        self.assertNotIn("excluded.py", files)
        self.assertTrue({"keep.log", "tracked.log", "sub/new.py"}.issubset(files))

    def test_list_pages_can_be_resumed_without_loss(self):
        for name in ["a.py", "b.py", "c.py"]:
            (self.root / name).write_text("line\n")
        first = self.tools.execute("list_files", {"limit": 2})
        self.assertEqual(first["files"], ["a.py", "b.py"])
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(first["reason"], "page limit")
        second = self.tools.execute("list_files", {"offset": first["next_offset"], "limit": 2})
        self.assertEqual(second["files"], ["c.py"])
        self.assertIsNone(second["next_offset"])


if __name__ == "__main__":
    unittest.main()
