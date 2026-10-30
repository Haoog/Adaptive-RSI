"""Contracts for run-owned external storage of large native tool output.

The artifact store is deliberately outside the workspace.  These tests cover
round-tripping real long output, pagination/search, ownership boundaries,
corruption/expiry/budget handling and the five native protocol envelopes.
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from adaptive_rsi.config import validate
from adaptive_rsi.native import Conversation, WorkspaceTools, execute_tools, tools_for
from adaptive_rsi.tool_artifacts import (
    ArtifactBudgetError,
    ArtifactError,
    ArtifactStore,
    ArtifactUnavailable,
    DEFAULT_PAGE_BYTES,
    INLINE_BODY_BYTES,
    MAX_PAGE_BYTES,
    artifact_reference,
    compact_result,
)


class ArtifactStoreContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.now = [1000.0]
        self.store = ArtifactStore(
            self.state / "runs" / "run-1" / "artifacts",
            run="run-1", invocation="inv-1", role="implementation",
            workspace=self.workspace, ttl_seconds=60, clock=lambda: self.now[0])

    def read_all(self, store, artifact):
        pages, offset = [], 0
        while True:
            page = store.read(artifact, offset=offset, limit=MAX_PAGE_BYTES)
            pages.append(page["content"])
            if page["eof"]:
                return "".join(pages)
            offset = page["next_offset"]

    def test_long_text_round_trips_through_unicode_pages_and_literal_search(self):
        text = "".join("第%d行 汉字🙂 payload-%04d\n" % (i, i) for i in range(2000))
        self.assertGreater(len(text.encode()), 50000)
        reference = self.store.put("stdout", text)
        self.assertRegex(reference["artifact"], r"^art_[0-9a-f]{32}$")
        self.assertEqual(reference["bytes"], len(text.encode()))
        self.assertEqual(reference["lines"], 2000)
        self.assertNotIn(text, json.dumps(reference))

        pages, offset, guard = [], 0, 0
        while True:
            page = self.store.read(reference["artifact"], offset=offset, limit=37)
            self.assertLessEqual(len(page["content"].encode()), 37 + 3)
            pages.append(page["content"])
            if page["eof"]:
                self.assertIsNone(page["next_offset"])
                break
            offset = page["next_offset"]
            guard += 1
            self.assertLess(guard, 10000)
        self.assertEqual("".join(pages), text)

        found = self.store.search(reference["artifact"], "payload-0042")
        self.assertEqual([row["line"] for row in found["matches"]], [43])
        self.assertIn("payload-0042", found["matches"][0]["text"])
        self.assertFalse(found["truncated"])
        self.assertIsNone(found["next_cursor"])

        folded = self.store.search(reference["artifact"], "第7行", case_sensitive=False)
        self.assertEqual([row["line"] for row in folded["matches"]], [8])
        missing = self.store.search(reference["artifact"], "not-present")
        self.assertEqual(missing["matches"], [])
        self.assertFalse(missing["truncated"])

    def test_search_pages_with_a_cursor_and_bounds_inputs(self):
        text = "\n".join("needle %02d" % i for i in range(25)) + "\n"
        artifact = self.store.put("stdout", text)["artifact"]
        first = self.store.search(artifact, "needle", limit=10)
        self.assertEqual([row["line"] for row in first["matches"]], list(range(1, 11)))
        self.assertTrue(first["truncated"])
        self.assertEqual(first["reason"], "match limit")
        second = self.store.search(artifact, "needle", limit=10, cursor=first["next_cursor"])
        self.assertEqual([row["line"] for row in second["matches"]], list(range(11, 21)))
        third = self.store.search(artifact, "needle", limit=10, cursor=second["next_cursor"])
        self.assertEqual([row["line"] for row in third["matches"]], list(range(21, 26)))
        self.assertFalse(third["truncated"])
        for bad in ({"text": "needle", "limit": 0}, {"text": "needle", "limit": 101},
                    {"text": ""}, {"text": "x" * 1001},
                    {"text": "needle", "cursor": "[]"},
                    {"text": "different", "cursor": first["next_cursor"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.search(artifact, **bad)
        for bad in ({"offset": -1}, {"limit": 0}, {"limit": MAX_PAGE_BYTES + 1},
                    {"offset": 10 ** 9}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.read(artifact, **bad)

    def test_ownership_is_run_invocation_and_role_scoped(self):
        artifact = self.store.put("stdout", "private implementation output")["artifact"]
        same_run_other_role = ArtifactStore(
            self.state / "runs" / "run-1" / "artifacts",
            run="run-1", invocation="inv-1", role="review",
            workspace=self.workspace, clock=lambda: self.now[0])
        with self.assertRaisesRegex(ArtifactUnavailable, "different role"):
            same_run_other_role.read(artifact)
        same_invocation_other_run = ArtifactStore(
            self.state / "runs" / "run-1" / "artifacts",
            run="run-2", invocation="inv-1", role="implementation",
            workspace=self.workspace, clock=lambda: self.now[0])
        with self.assertRaisesRegex(ArtifactUnavailable, "different run"):
            same_invocation_other_run.read(artifact)
        other_invocation = ArtifactStore(
            self.state / "runs" / "run-1" / "artifacts",
            run="run-1", invocation="inv-2", role="implementation",
            workspace=self.workspace, clock=lambda: self.now[0])
        with self.assertRaises(ArtifactUnavailable):
            other_invocation.read(artifact)

    def test_rejects_traversal_symlink_and_paths_outside_the_store(self):
        artifact = self.store.put("stdout", "safe")["artifact"]
        for bad in ("../secret", "../../etc/passwd", "/etc/passwd", artifact + "/x",
                    "art_" + "0" * 31, "art_" + "z" * 32):
            with self.subTest(bad=bad), self.assertRaises(ArtifactError):
                self.store.read(bad)
        secret = self.root / "secret.txt"
        secret.write_text("host secret")
        binary = next((self.state / "runs" / "run-1" / "artifacts").glob("art_*.bin"))
        binary.unlink()
        binary.symlink_to(secret)
        with self.assertRaises(ArtifactUnavailable) as caught:
            self.store.read(artifact)
        self.assertNotIn("host secret", str(caught.exception))

    def test_rejects_hardlinked_artifact_and_metadata_files(self):
        artifact = self.store.put("stdout", "hardlink target")["artifact"]
        root = self.state / "runs" / "run-1" / "artifacts"
        binary = root / (artifact + ".bin")
        os.link(binary, root / "binary-alias.bin")
        with self.assertRaisesRegex(ArtifactUnavailable, "unavailable"):
            self.store.read(artifact)

        second = self.store.put("stdout", "metadata hardlink target")["artifact"]
        metadata = root / (second + ".json")
        os.link(metadata, root / "metadata-alias.json")
        with self.assertRaisesRegex(ArtifactUnavailable, "unavailable"):
            self.store.read(second)

    def test_corrupt_missing_and_expired_artifacts_fail_loudly(self):
        artifact = self.store.put("stdout", "complete body")["artifact"]
        binary = next((self.state / "runs" / "run-1" / "artifacts").glob("art_*.bin"))
        binary.write_bytes(b"tampered")
        with self.assertRaisesRegex(ArtifactUnavailable, "corrupt"):
            self.store.read(artifact)

        second = self.store.put("stdout", "another body")["artifact"]
        metadata = next((self.state / "runs" / "run-1" / "artifacts").glob(second + ".json"))
        metadata.unlink()
        with self.assertRaisesRegex(ArtifactUnavailable, "unavailable"):
            self.store.read(second)

        third = self.store.put("stdout", "expiring body")["artifact"]
        self.now[0] += 61
        with self.assertRaisesRegex(ArtifactUnavailable, "expired"):
            self.store.read(third)

    def test_artifact_size_total_and_count_budgets_are_explicit(self):
        limited = ArtifactStore(
            self.state / "runs" / "run-budget" / "artifacts",
            run="run-budget", invocation="inv-budget", role="implementation",
            workspace=self.workspace, max_artifact_bytes=100,
            max_total_bytes=150, max_artifacts=2, clock=lambda: self.now[0])
        with self.assertRaisesRegex(ArtifactBudgetError, "byte"):
            limited.put("stdout", "x" * 101)
        limited.put("stdout", "x" * 100)
        with self.assertRaisesRegex(ArtifactBudgetError, "budget"):
            limited.put("stderr", "y" * 60)
        other = ArtifactStore(
            self.state / "runs" / "run-count" / "artifacts",
            run="run-count", invocation="inv-count", role="implementation",
            workspace=self.workspace, max_artifacts=1, clock=lambda: self.now[0])
        other.put("stdout", "one")
        with self.assertRaisesRegex(ArtifactBudgetError, "count"):
            other.put("stderr", "two")

    def test_store_must_live_outside_the_workspace_and_is_private(self):
        before = stat.S_IMODE(self.root.stat().st_mode)
        with self.assertRaisesRegex(ArtifactError, "outside the workspace"):
            ArtifactStore(self.root, run="run-1", invocation="inv-1",
                          role="implementation", workspace=self.workspace)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), before)
        with self.assertRaisesRegex(ArtifactError, "outside the workspace"):
            ArtifactStore(self.workspace / "artifacts", run="run-1", invocation="inv-1",
                          role="implementation", workspace=self.workspace)
        root = self.state / "runs" / "run-1" / "artifacts"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        artifact = self.store.put("stdout", "private")["artifact"]
        binary = next(root.glob("art_*.bin"))
        metadata = root / (artifact + ".json")
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
        self.assertRegex(artifact, r"^art_[0-9a-f]{32}$")


class CompactResultContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = ArtifactStore(self.root / "state" / "artifacts", run="r", invocation="i",
                                   role="implementation", workspace=self.workspace)

    def test_run_command_structure_is_preserved_and_bodies_round_trip(self):
        stdout = "".join("out-%05d\n" % i for i in range(10000))
        stderr = "".join("err-%05d\n" % i for i in range(3000))
        value = {"exit_code": 1, "stdout": stdout, "stderr": stderr,
                 "error": "Command timed out", "error_kind": "timeout",
                 "timed_out": True, "seconds": 12.5}
        compacted = compact_result(self.store, "run_command", value)
        self.assertEqual(compacted["exit_code"], 1)
        self.assertEqual(compacted["error"], "Command timed out")
        self.assertEqual(compacted["error_kind"], "timeout")
        self.assertTrue(compacted["timed_out"])
        self.assertEqual(compacted["seconds"], 12.5)
        self.assertTrue(compacted["truncated"])
        self.assertIsInstance(compacted["stdout"], dict)
        self.assertIsInstance(compacted["stderr"], dict)
        self.assertEqual(compacted["artifacts"]["stdout"], compacted["stdout"]["artifact"])
        self.assertLess(len(json.dumps(compacted).encode()), 5000)
        self.assertNotIn(stdout[:2000], json.dumps(compacted))
        self.assertEqual(self._read_all(compacted["stdout"]["artifact"]), stdout)
        self.assertEqual(self._read_all(compacted["stderr"]["artifact"]), stderr)
        self.assertEqual(artifact_reference(compacted), compacted["stdout"]["artifact"])

    def _read_all(self, artifact):
        pages, offset = [], 0
        while True:
            page = self.store.read(artifact, offset=offset, limit=MAX_PAGE_BYTES)
            pages.append(page["content"])
            if page["eof"]:
                return "".join(pages)
            offset = page["next_offset"]

    def test_small_results_are_unchanged(self):
        small = {"exit_code": 0, "stdout": "ok\n", "stderr": "", "error": None}
        self.assertIs(compact_result(self.store, "run_command", small), small)
        text = "short text"
        self.assertEqual(compact_result(self.store, "read_file", text), text)

    def test_large_read_file_and_nested_structures_keep_their_shape(self):
        body = "line\n" * 5000
        envelope = compact_result(self.store, "read_file", body)
        self.assertEqual(envelope["artifact"], self.store.search(envelope["artifact"], "line")["artifact"])
        self.assertEqual(self._read_all(envelope["artifact"]), body)
        value = {"files": ["a.py", "b.py"], "nested": {"text": body, "count": 2},
                 "truncated": False}
        compacted = compact_result(self.store, "list_files", value)
        self.assertEqual(compacted["files"], ["a.py", "b.py"])
        self.assertEqual(compacted["nested"]["count"], 2)
        self.assertIsInstance(compacted["nested"]["text"], dict)
        self.assertEqual(self._read_all(compacted["nested"]["text"]["artifact"]), body)

    def test_budget_failure_is_not_silently_hidden(self):
        limited = ArtifactStore(self.root / "state" / "limited", run="r", invocation="i",
                                role="implementation", workspace=self.workspace,
                                max_artifact_bytes=10)
        with self.assertRaises(ArtifactBudgetError):
            compact_result(limited, "read_file", "x" * (INLINE_BODY_BYTES + 1),
                           inline_bytes=1)


class NativeToolArtifactIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = validate({})

    def tools(self, role="implementation", invocation="inv-1", run="run-1"):
        return WorkspaceTools(
            self.workspace, role, self.state, self.config, None, (),
            run=run, invocation=invocation,
            artifact_root=self.state / "runs" / run / "artifacts")

    def test_artifact_readers_are_available_to_read_only_roles(self):
        for role in ("planning", "review", "acceptance", "analysis", "triage", "evolution"):
            with self.subTest(role=role):
                names = [name for name, _, _ in tools_for(role)]
                self.assertIn("read_artifact", names)
                self.assertIn("search_artifact", names)
        self.assertNotIn("read_artifact", [name for name, _, _ in tools_for("probe")])

    def test_read_only_roles_can_read_their_own_but_not_other_invocations(self):
        implementation = self.tools("implementation", "impl-inv")
        artifact = implementation.artifact_store().put("stdout", "implementation-only")["artifact"]
        review = self.tools("review", "review-inv")
        own = review.artifact_store().put("stdout", "review-only")["artifact"]
        self.assertEqual(review.execute("read_artifact", {"artifact": own})["content"], "review-only")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            review.execute("read_artifact", {"artifact": artifact})
        self.assertEqual(review.execute("search_artifact", {"artifact": own, "text": "review"})["matches"][0]["line"], 1)

    def test_execute_tools_compacts_real_long_output_and_reads_it_back(self):
        # A real process under the same sandbox used by implementation roles.
        if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
            self.skipTest("real long-output sandbox check requires macOS Seatbelt")
        tools = self.tools()
        script = ("import sys;sys.stdout.write(''.join('line-%06d ' % i + 'x'*80 + '\\n' "
                  "for i in range(4000)))")
        # -S keeps the child from importing site/venv configuration that the
        # Seatbelt profile may not expose; the script itself only needs sys.
        result = execute_tools(tools, [("call-1", "run_command",
                                        {"cwd": ".", "argv": [sys.executable, "-S", "-c", script]})])[0][2]
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["error"], None)
        self.assertIsInstance(result["stdout"], dict)
        self.assertTrue(result["truncated"])
        artifact = result["stdout"]["artifact"]
        pages, offset = [], 0
        while True:
            page = tools.execute("read_artifact", {"artifact": artifact, "offset": offset,
                                                   "limit": DEFAULT_PAGE_BYTES})
            pages.append(page["content"])
            if page["eof"]:
                break
            offset = page["next_offset"]
        recovered = "".join(pages)
        self.assertEqual(len(recovered.splitlines()), 4000)
        self.assertTrue(recovered.startswith("line-000000 "))
        self.assertTrue(recovered.endswith("x" * 80 + "\n"))
        found = tools.execute("search_artifact", {"artifact": artifact, "text": "line-003999"})
        self.assertEqual(found["matches"][0]["line"], 4000)
        # Artifacts must not be reachable through the workspace tools.
        self.assertFalse((self.workspace / "runs").exists())
        with self.assertRaises(ValueError):
            tools.execute("read_file", {"path": "../state/runs/run-1/artifacts", "start_line": 1,
                                        "end_line": 1})

    def test_execute_tools_reports_budget_failure_without_losing_exit_metadata(self):
        tools = self.tools()
        limited = ArtifactStore(self.state / "runs" / "run-1" / "artifacts", run="run-1",
                                invocation="inv-1", role="implementation",
                                workspace=self.workspace, max_artifact_bytes=10)
        tools._artifact_store = limited
        original = tools.execute
        tools.execute = lambda name, args: {"exit_code": 7, "stdout": "x" * (INLINE_BODY_BYTES + 100),
                                            "stderr": "", "error": "failed",
                                            "error_kind": "exit", "timed_out": False}
        try:
            result = execute_tools(tools, [("call", "run_command", {"cwd": ".", "argv": ["x"]})])[0][2]
        finally:
            tools.execute = original
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["error_kind"], "exit")
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["truncated"])
        self.assertIn("artifact_error", result)
        self.assertLess(len(result["stdout"]), 30000)

    def test_history_trim_keeps_artifact_ids_readable(self):
        tools = self.tools()
        envelope = compact_result(tools.artifact_store(), "read_file", "line\n" * 5000)
        artifact = envelope["artifact"]
        conversation = Conversation("anthropic", "model", "prompt", [], "default")
        conversation.results([("call-1", "read_file", envelope)])
        for index in range(8):
            conversation.history.append({"role": "assistant", "content": "step-%d" % index})
        conversation.trim_history(1000)
        serialized = json.dumps(conversation.history)
        self.assertIn(artifact, serialized)
        pages, offset = [], 0
        while True:
            page = tools.execute("read_artifact", {"artifact": artifact, "offset": offset,
                                                   "limit": MAX_PAGE_BYTES})
            pages.append(page["content"])
            if page["eof"]:
                break
            offset = page["next_offset"]
        self.assertEqual("".join(pages), "line\n" * 5000)

    def test_five_protocol_results_carry_the_artifact_envelope(self):
        tools = self.tools()
        body = "".join("row-%05d\n" % i for i in range(8000))
        envelope = compact_result(tools.artifact_store(), "run_command",
                                  {"exit_code": 0, "stdout": body, "stderr": "", "error": None})
        artifact = envelope["stdout"]["artifact"]
        for protocol in ("responses", "chat_completions", "ollama", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                conversation = Conversation(protocol, "model", "prompt", [], "default")
                conversation.results([("call-1", "run_command", envelope)])
                history = json.dumps(conversation.history)
                self.assertIn(artifact, history)
                self.assertNotIn(body[:5000], history)
                item = conversation.history[-1]
                if protocol == "responses":
                    self.assertEqual(item["type"], "function_call_output")
                    self.assertEqual(item["call_id"], "call-1")
                    payload = json.loads(item["output"])
                elif protocol in ("chat_completions", "ollama"):
                    self.assertEqual(item["role"], "tool")
                    self.assertEqual(item.get("tool_call_id", item.get("tool_name")), "call-1" if protocol == "chat_completions" else "run_command")
                    payload = json.loads(item["content"])
                elif protocol == "anthropic":
                    self.assertEqual(item["role"], "user")
                    self.assertEqual(item["content"][0]["type"], "tool_result")
                    self.assertEqual(item["content"][0]["tool_use_id"], "call-1")
                    payload = json.loads(item["content"][0]["content"])
                else:
                    self.assertEqual(item["role"], "user")
                    payload = item["parts"][0]["functionResponse"]["response"]["result"]
                self.assertEqual(payload["stdout"]["artifact"], artifact)
                self.assertEqual(payload["exit_code"], 0)
                _, request = conversation.request()
                self.assertIn(artifact, json.dumps(request))
        # Measure serialized request bytes on the same logical workload; these
        # are bytes, not provider tokens or a billing claim.
        raw = {"exit_code": 0, "stdout": body, "stderr": "", "error": None}
        for protocol in ("responses", "chat_completions", "ollama", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                before = Conversation(protocol, "model", "prompt", [], "default")
                before.results([("call-1", "run_command", raw)])
                _, before_request = before.request()
                after = Conversation(protocol, "model", "prompt", [], "default")
                after.results([("call-1", "run_command", envelope)])
                _, after_request = after.request()
                before_bytes = len(json.dumps(before_request).encode())
                after_bytes = len(json.dumps(after_request).encode())
                self.assertLess(after_bytes, before_bytes)
                self.assertGreater(after_bytes, 0)

    def test_invalid_artifact_id_never_escapes_the_store(self):
        tools = self.tools()
        for bad in ("../state", "/etc/passwd", "art_" + "z" * 32, "art_" + "0" * 31):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tools.execute("read_artifact", {"artifact": bad})


if __name__ == "__main__":
    unittest.main()
