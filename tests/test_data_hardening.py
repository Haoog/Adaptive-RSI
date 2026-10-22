import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.dashboard import records
from adaptive_rsi.discovery import issue_comment_context
from adaptive_rsi.readiness import inspect as readiness
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store, encode
from tests.test_runtime import FakeAgent, fixture


class DataHardeningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Runtime(Store(self.root / "state"), FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / "project"))
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {"Cookie": self.server.cookie + "=" + self.server.token}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(Request(self.server.origin + path, headers=self.headers)) as response:
            return response.read()

    def post(self, path, body):
        headers = {**self.headers, "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
                   "Content-Type": "application/json"}
        request = Request(self.server.origin + path, json.dumps(body).encode(), headers)
        return urlopen(request)

    def test_invalid_project_configs_are_isolated_and_cannot_be_enabled(self):
        malformed = self.runtime.add_project(fixture(self.root / "malformed"))
        timezone = self.runtime.add_project(fixture(self.root / "timezone"))
        self.runtime.store.execute("UPDATE projects SET config='not-json',paused=0 WHERE id=?", (malformed,))
        self.runtime.store.execute("UPDATE projects SET config=?,paused=0 WHERE id=?",
                                   (encode({"schedule": {"timezone": "Mars/Olympus"}}), timezone))
        self.runtime.store.execute("INSERT INTO schedules VALUES (?, 'light', 1, '')", (malformed,))
        self.runtime.store.execute("INSERT INTO schedules VALUES (?, 'light', 1, '')", (timezone,))
        self.runtime.store.execute("INSERT INTO schedules VALUES (?, 'light', 1, '')", (self.project,))

        data = json.loads(self.get("/api/state?project=" + self.project))
        by_id = {project["id"]: project for project in data["projects"]}
        self.assertIn("Expecting value", by_id[malformed]["configuration_error"])
        self.assertEqual(by_id[malformed]["raw_overrides"], "not-json")
        self.assertIsNone(by_id[malformed]["overrides"])
        self.assertIn("Mars/Olympus", by_id[timezone]["configuration_error"])
        self.assertTrue(by_id[malformed]["paused"])
        self.assertTrue(by_id[timezone]["paused"])
        self.assertFalse(by_id[self.project].get("configuration_error"))
        self.assertEqual(self.runtime.store.one("SELECT paused FROM projects WHERE id=?", (malformed,))["paused"], 0)
        self.assertEqual(self.runtime.store.one("SELECT paused FROM projects WHERE id=?", (timezone,))["paused"], 0)
        schedules = {row["project"]: row for row in data["schedules"]}
        self.assertIsNone(schedules[malformed]["next_effective"])
        self.assertIsNone(schedules[timezone]["next_effective"])
        self.assertIsInstance(schedules[self.project]["next_effective"], (int, float))

        with self.assertRaises(HTTPError) as denied:
            self.post("/api/pause", {"project": malformed, "paused": False})
        self.assertEqual(denied.exception.code, 400)
        denied.exception.close()
        self.assertEqual(self.runtime.store.one("SELECT paused FROM projects WHERE id=?", (malformed,))["paused"], 0)

    def test_invalid_global_config_returns_an_explicit_error(self):
        self.runtime.store.put_setting("config", [])
        with self.assertRaises(HTTPError) as invalid:
            self.get("/api/state")
        self.assertEqual(invalid.exception.code, 400)
        self.assertIn("Global configuration is invalid", json.load(invalid.exception)["error"])

    def test_state_sections_keep_legacy_shape_and_reduce_queries_and_payload(self):
        now = time.time()
        with self.runtime.store.connect() as db:
            db.executemany("INSERT INTO candidates(id,project,kind,parent,status,content,evidence,created) "
                            "VALUES (?,?,'strategy',NULL,'proposed',?,?,?)",
                            (("candidate-" + str(i), self.project, encode({"guidance": "g" * 2048}), "{}", now - i)
                             for i in range(100)))
            db.executemany("INSERT INTO calls(id,project,role,model,effort,state,tokens,seconds,created,provider) "
                            "VALUES (?,?,'planning','model','high','success',10,0.1,?,'openai')",
                            (("call-" + str(i), self.project, now - i) for i in range(1000)))

        original_query = self.runtime.store.query
        measurements = []

        def request_state(path):
            sql = []
            def tracked(query, args=()):
                sql.append(query)
                return original_query(query, args)
            self.runtime.store.query = tracked
            started = time.perf_counter()
            try:
                payload = self.get(path)
            finally:
                elapsed = time.perf_counter() - started
                self.runtime.store.query = original_query
            select_count = sum(query.lstrip().upper().startswith("SELECT") for query in sql)
            measurements.append({"bytes": len(payload), "select_queries": select_count, "seconds": elapsed})
            return json.loads(payload)

        legacy = request_state("/api/state?project=" + self.project)
        core = request_state("/api/state?project=" + self.project + "&sections=core")
        self.assertIn("candidates", legacy)
        self.assertIn("quality", legacy)
        self.assertNotIn("candidates", core)
        self.assertNotIn("quality", core)
        self.assertEqual(core["sections"], ["core"])
        self.assertLess(measurements[1]["bytes"], measurements[0]["bytes"])
        self.assertLess(measurements[1]["select_queries"], measurements[0]["select_queries"])
        print("/api/state SQLite evidence: " + json.dumps({"legacy": measurements[0], "core": measurements[1]}))

    def test_task_counts_follow_the_same_system_filter_as_task_rows(self):
        user_task = self.runtime.store.add_task(self.project, "goal", "user-attention", "User attention")
        system_task = self.runtime.store.add_task(self.project, "system_issues", "system-attention", "System attention")
        self.runtime.store.execute("UPDATE tasks SET state='failed' WHERE id IN (?,?)", (user_task, system_task))

        visible = records(self.runtime.store, self.project, group="attention", include_system=False)
        included = records(self.runtime.store, self.project, group="attention", include_system=True)
        self.assertEqual([task["id"] for task in visible["tasks"]], [user_task])
        self.assertEqual(visible["task_counts"], [{"state": "failed", "count": 1}])
        self.assertEqual(next(row["count"] for row in included["task_counts"] if row["state"] == "failed"), 2)

    def test_readiness_names_missing_tools_and_dependencies_without_model_calls(self):
        config = merge(DEFAULTS, {"checks": [{"name": "missing", "cwd": ".", "argv": ["missing-tool"]}],
                                 "environment": {"seed_paths": ["missing-seed"]}})
        real_which = shutil.which
        with patch("adaptive_rsi.readiness.Sandbox.backend", return_value="seatbelt"), \
             patch("adaptive_rsi.readiness.shutil.which",
                   side_effect=lambda name: None if name == "missing-tool" else real_which(name)):
            result = readiness(self.runtime, self.runtime.project(self.project), config)
        items = result["items"]
        tools = next(item for item in items if item["code"] == "tools")
        dependencies = next(item for item in items if item["code"] == "dependencies")
        models = [item for item in items if item["code"] == "model"]
        self.assertEqual((tools["reason"], tools["missing"]), ("tools_missing", ["missing-tool"]))
        self.assertEqual((dependencies["reason"], dependencies["missing"]),
                         ("dependencies_missing", ["missing-seed"]))
        self.assertTrue(all(item["ready"] and item["reason"] == "model_unverified" for item in models))
        self.assertIn("no model call", models[0]["detail"])
        self.assertEqual(self.runtime.store.query("SELECT * FROM calls"), [])


class IssueCommentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.execute("INSERT INTO projects VALUES ('p','p','/tmp/issue-comment-project','{}',1,0)")
        self.runtime = SimpleNamespace(store=self.store)
        self.project = {"id": "p"}
        self.config = merge(DEFAULTS, {"github": {"repository": "owner/project"}})
        self.task = {"source": "issue", "evidence": encode([{"number": 7, "updated": "2026-10-20T00:00:00Z"}])}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def comment(number, updated, body=None, created=None):
        return {"id": number, "user": {"login": "reviewer"}, "body": body or "Comment " + str(number),
                "created_at": created or updated, "updated_at": updated,
                "html_url": "https://github.com/owner/project/issues/7#issuecomment-" + str(number)}

    @staticmethod
    def page_args(args):
        fields = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "-f"]
        return {field.split("=", 1)[0]: field.split("=", 1)[1] for field in fields}

    def test_rest_id_ascending_pages_keep_latest_window_without_sort_parameters(self):
        comments = [self.comment(i, "2026-10-" + str(1 + i // 100).zfill(2) + "T00:00:00Z") for i in range(1, 1002)]
        requests = []
        def checked(args, timeout=60):
            params = self.page_args(args)
            requests.append(params)
            page = int(params["page"])
            start = (page - 1) * 100
            return json.dumps(comments[start:start + 100])

        with patch("adaptive_rsi.discovery.time.time", return_value=1790000000):
            result = issue_comment_context(self.runtime, self.project, self.task, self.config, checked=checked)
        self.assertEqual(result["status"], "loaded")
        self.assertEqual(len(requests), 11)
        self.assertNotIn("sort", requests[0])
        self.assertNotIn("direction", requests[0])
        self.assertEqual([comment["id"] for comment in result["comments"]], list(range(1001, 981, -1)))
        self.assertFalse(result["complete"])
        self.assertEqual(result["omitted_count"], 981)

    def test_incremental_scan_finds_edited_old_comment_after_cache_expires(self):
        pages = [[self.comment(1, "2026-10-01T00:00:00Z"), self.comment(2, "2026-10-02T00:00:00Z")]]
        initial_requests = []
        def first(args, timeout=60):
            initial_requests.append(self.page_args(args))
            return json.dumps(pages[0])
        start = 1790000000
        with patch("adaptive_rsi.discovery.time.time", return_value=start):
            original = issue_comment_context(self.runtime, self.project, self.task, self.config, checked=first)
        with patch("adaptive_rsi.discovery.time.time", return_value=start + 10):
            cached = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                           checked=lambda *_args, **_kwargs: self.fail("fresh cache should avoid another request"))
        self.assertEqual(cached["status"], "cached")
        self.assertEqual(len(initial_requests), 1)
        old_cursor = self.store.setting("issue-comments:p:7")["cursor"]
        edited = self.comment(1, "2026-10-21T00:00:01Z", "Old comment edited", "2026-10-01T00:00:00Z")
        new = self.comment(3, "2026-10-21T00:00:02Z", "New comment", "2026-10-21T00:00:02Z")
        calls = []
        def incremental(args, timeout=60):
            params = self.page_args(args)
            calls.append(params)
            return json.dumps([edited, new] if params["page"] == "1" else [])
        with patch("adaptive_rsi.discovery.time.time", return_value=start + 600):
            updated = issue_comment_context(self.runtime, self.project, self.task, self.config, checked=incremental)
        self.assertEqual(original["comments"][0]["id"], 2)
        self.assertEqual(calls[0]["since"], old_cursor)
        self.assertEqual({comment["id"]: comment["body"] for comment in updated["comments"]}[1], "Old comment edited")
        self.assertEqual({comment["id"] for comment in updated["comments"]}, {1, 2, 3})
        self.assertTrue(updated["complete"])
        self.assertGreater(self.store.setting("issue-comments:p:7")["cursor"], old_cursor)

    def test_issue_revision_refresh_drops_deleted_comment_and_resets_cursor(self):
        start = 1790000000
        initial = [self.comment(i, "2026-10-0" + str(i) + "T00:00:00Z") for i in [1, 2, 3]]
        with patch("adaptive_rsi.discovery.time.time", return_value=start):
            first = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                          checked=lambda args, timeout=60: json.dumps(initial))
        old_cursor = self.store.setting("issue-comments:p:7")["cursor"]
        self.task["evidence"] = encode([{"number": 7, "updated": "2026-10-21T00:00:00Z"}])
        current = [initial[0], initial[2]]
        requests = []
        def full_refresh(args, timeout=60):
            params = self.page_args(args)
            requests.append(params)
            return json.dumps(current if params["page"] == "1" else [])
        with patch("adaptive_rsi.discovery.time.time", return_value=start + 30):
            refreshed = issue_comment_context(self.runtime, self.project, self.task, self.config, checked=full_refresh)
        self.assertEqual(len(first["comments"]), 3)
        self.assertNotIn("since", requests[0])
        self.assertEqual({comment["id"] for comment in refreshed["comments"]}, {1, 3})
        self.assertTrue(refreshed["complete"])
        self.assertNotEqual(self.store.setting("issue-comments:p:7")["cursor"], old_cursor)
        with patch("adaptive_rsi.discovery.time.time", return_value=start + 31):
            cached = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                           checked=lambda *_args, **_kwargs: self.fail("same Issue revision should stay cached"))
        self.assertEqual(cached["status"], "cached")

    def test_mid_pagination_failure_keeps_cursor_and_comments_and_backoffs(self):
        initial = [self.comment(1, "2026-10-01T00:00:00Z")]
        now = 1790000000
        with patch("adaptive_rsi.discovery.time.time", return_value=now):
            original = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                             checked=lambda args, timeout=60: json.dumps(initial))
        entry = self.store.setting("issue-comments:p:7")
        old_cursor = entry["cursor"]
        rows = [self.comment(i + 2, "2026-10-22T00:00:00Z") for i in range(100)]
        calls = []
        def broken(args, timeout=60):
            params = self.page_args(args)
            calls.append(params)
            if params["page"] == "1":
                return json.dumps(rows)
            raise RuntimeError("GitHub comments failed (HTTP 403)")
        with patch("adaptive_rsi.discovery.time.time", return_value=now + 600):
            failed = issue_comment_context(self.runtime, self.project, self.task, self.config, checked=broken)
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["failure_code"], "permission_denied")
        self.assertEqual(self.store.setting("issue-comments:p:7")["cursor"], old_cursor)
        self.assertEqual([comment["id"] for comment in failed["comments"]], [1])
        self.assertEqual(len(calls), 2)
        with patch("adaptive_rsi.discovery.time.time", return_value=now + 601):
            again = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                          checked=lambda *_args, **_kwargs: self.fail("failure backoff should avoid another request"))
        self.assertEqual(again["status"], "unavailable")

    def test_long_comment_is_marked_truncated_and_context_incomplete(self):
        long = self.comment(1, "2026-10-01T00:00:00Z", "x" * 4100)
        with patch("adaptive_rsi.discovery.time.time", return_value=1790000000):
            result = issue_comment_context(self.runtime, self.project, self.task, self.config,
                                           checked=lambda args, timeout=60: json.dumps([long]))
        self.assertEqual(len(result["comments"][0]["body"]), 4000)
        self.assertTrue(result["comments"][0]["truncated"])
        self.assertFalse(result["complete"])
        self.assertIn("truncated", result["warning"])


if __name__ == "__main__":
    unittest.main()
