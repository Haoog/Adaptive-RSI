import contextlib
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.discovery import related_work, sync
from adaptive_rsi.store import Store, encode


class DiscoveryHardeningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.store.execute("INSERT INTO projects VALUES ('p','p','/tmp/discovery-project','{}',1,0)")
        self.runtime = SimpleNamespace(store=self.store)
        self.project = {"id": "p"}
        self.config = merge(DEFAULTS, {"github": {"repository": "owner/project"}})

    def tearDown(self):
        self.temp.cleanup()

    def synchronize(self, rows):
        sync(self.runtime, self.project, self.config, lambda *args, **kwargs: encode(rows))

    def test_held_issue_revision_resumes_after_failed_or_interrupted_run(self):
        for state in ["failed", "interrupted"]:
            for change in ["open", "closed", "excluded"]:
                with self.subTest(state=state, change=change):
                    number = len(self.store.query("SELECT id FROM tasks")) + 1
                    issue = {"number": number, "title": "Old requirements", "body": "old body",
                             "labels": [], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                             "html_url": "https://github.com/owner/project/issues/" + str(number)}
                    self.synchronize([issue])
                    task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:" + str(number),))
                    self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (task["id"],))
                    revised = {**issue, "title": "New requirements", "body": "new body",
                               "updated_at": "2026-10-21T00:00:00Z"}
                    if change == "closed":
                        revised["state"] = "closed"
                    elif change == "excluded":
                        revised["labels"] = [self.config["github"]["exclude_labels"][0]]
                    self.synchronize([revised])
                    self.assertEqual(self.store.one("SELECT body FROM tasks WHERE id=?", (task["id"],))["body"], "old body")
                    self.assertIn(str(number), self.store.setting("issue-sync:p")["pending"])
                    self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
                    self.store = self.runtime.store = Store(self.root)
                    self.synchronize([])
                    refreshed = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                    self.assertNotIn(str(number), self.store.setting("issue-sync:p")["pending"])
                    if change == "open":
                        self.assertEqual((refreshed["state"], refreshed["title"], refreshed["body"]),
                                         ("queued", "New requirements", "new body"))
                        self.assertEqual(json.loads(refreshed["evidence"])[0]["updated"], revised["updated_at"])
                    else:
                        self.assertEqual(refreshed["state"], "obsolete")

    def test_unchanged_revision_and_cancelled_task_do_not_resume(self):
        issue = {"number": 1, "title": "Requirements", "body": "body", "labels": [], "state": "open",
                 "updated_at": "2026-10-20T00:00:00Z", "html_url": "https://github.com/owner/project/issues/1"}
        self.synchronize([issue])
        for state in ["failed", "interrupted", "cancelled"]:
            with self.subTest(state=state):
                self.store.execute("UPDATE tasks SET state=? WHERE fingerprint='issue:1'", (state,))
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:1'")["state"], state)
        self.synchronize([{**issue, "updated_at": "2026-10-21T00:00:00Z"}])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:1'")["state"], "cancelled")

    def add_related(self, task_id, title, paths, result=None, updated=1, state="merged"):
        self.store.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,updated) "
                           "VALUES (?,'p','inspection',?,?,'',?,?,?)",
                           (task_id, task_id, title, encode([{"path": path} for path in paths]), state, updated))
        if result is not None:
            self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) "
                               "VALUES (?,?,'p','merged',?,1,2)", ("run-" + task_id, task_id, encode(result)))

    def test_related_work_ranking_preserves_ties_and_latest_results(self):
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": encode([{"path": "client.py"}])}
        self.add_related("first", "Handle client response", ["client.py"], updated=2)
        self.add_related("second", "Handle client response", ["client.py"], updated=2)
        self.add_related("newer", "Handle client response", ["client.py"], updated=3)
        self.add_related("result-path", "Timeout", [], {"files": ["client.py"], "summary": "old result"}, updated=4)
        self.add_related("word-match", "Handle client response", [], updated=9)
        self.add_related("unrelated", "Independent failure", [], updated=10)
        self.add_related("rejected", "Handle client response", ["client.py"], state="rejected")
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task)],
                         ["newer", "first", "second", "result-path", "word-match"])
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task, limit=2)], ["newer", "first"])
        self.assertEqual(related_work(self.store, "p", task, limit=0), [])
        self.assertEqual([row["id"] for row in related_work(self.store, "p", task, limit=-1)],
                         ["newer", "first", "second", "result-path"])
        self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) "
                           "VALUES ('new-result','result-path','p','merged',?,3,4)",
                           (encode({"files": ["other.py"], "summary": "new result"}),))
        self.assertNotIn("result-path", [row["id"] for row in related_work(self.store, "p", task)])

    def test_related_work_keeps_memory_bounded_with_large_run_evidence(self):
        count = 512
        evidence = encode([{"path": "client.py"}])
        result = encode({"files": ["client.py"], "summary": "Fixed timeout", "evaluation": {"stdout": "x" * 16384}})
        with self.store.connect() as db:
            db.executemany("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,state,updated) "
                           "VALUES (?,'p','inspection',?,'Handle client response','',?,'merged',?)",
                           (("task-" + str(i), "key-" + str(i), evidence, i) for i in range(count)))
            db.executemany("INSERT INTO runs(id,task,project,state,result,started,ended) "
                           "VALUES (?,?,'p','merged',?,1,2)",
                           (("run-" + str(i), "task-" + str(i), result) for i in range(count)))
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": evidence}
        tracemalloc.start()
        try:
            rows = related_work(self.store, "p", task)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual([row["id"] for row in rows], ["task-" + str(i) for i in range(count - 1, count - 13, -1)])
        self.assertLess(peak, 1024 * 1024, "Related-work selection retained the whole run history")

    def test_related_work_closes_history_connection_after_invalid_evidence(self):
        self.add_related("broken", "Handle client response", ["client.py"])
        self.store.execute("UPDATE tasks SET evidence='invalid-json' WHERE id='broken'")
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": "[]"}
        opened = []
        connect = self.store.connect

        @contextlib.contextmanager
        def tracked_connection():
            with connect() as db:
                opened.append(db)
                yield db

        with patch.object(self.store, "connect", tracked_connection):
            with self.assertRaises(json.JSONDecodeError):
                related_work(self.store, "p", task)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
