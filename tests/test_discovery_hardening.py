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
from adaptive_rsi.context import terms
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
                    task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(number),))
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
                self.store.execute("UPDATE tasks SET state=? WHERE fingerprint='issue:owner/project:1'", (state,))
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], state)
        self.synchronize([{**issue, "updated_at": "2026-10-21T00:00:00Z"}])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], "cancelled")

    def test_delivered_issue_tasks_ignore_edits_and_return_on_reopen(self):
        issue = {"number": 1, "title": "Requirements", "body": "body", "labels": [], "state": "open",
                 "updated_at": "2026-10-20T00:00:00Z", "comments": 2,
                 "html_url": "https://github.com/owner/project/issues/1"}
        self.synchronize([issue])
        self.store.execute("UPDATE tasks SET state='merged' WHERE fingerprint='issue:owner/project:1'")
        # 已交付任务的 Issue 普通编辑：只刷新证据，不重排。
        self.synchronize([{**issue, "body": "edited", "updated_at": "2026-10-21T00:00:00Z"}])
        row = self.store.one("SELECT * FROM tasks WHERE fingerprint='issue:owner/project:1'")
        self.assertEqual(row["state"], "merged")
        self.assertEqual(row["body"], "body")
        self.assertEqual(json.loads(row["evidence"])[0]["state"], "open")
        # 关闭再重新打开：值得重跑，任务带最新正文重新入队。
        self.synchronize([{**issue, "state": "closed", "updated_at": "2026-10-22T00:00:00Z"}])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE fingerprint='issue:owner/project:1'")["state"], "merged")
        self.synchronize([{**issue, "state": "open", "body": "reopened body", "updated_at": "2026-10-23T00:00:00Z"}])
        row = self.store.one("SELECT * FROM tasks WHERE fingerprint='issue:owner/project:1'")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["body"], "reopened body")

    @staticmethod
    def issue(repository="owner/project", number=7):
        return {"number": number, "title": repository + " requirement", "body": repository + " body",
                "labels": ["bug"], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                "html_url": "https://github.com/" + repository + "/issues/" + str(number)}

    def test_same_number_in_different_repositories_preserves_tasks_and_history(self):
        for state in ["queued", "merged"]:
            for legacy in [False, True]:
                with self.subTest(state=state, legacy=legacy):
                    number = len(self.store.query("SELECT id FROM tasks")) + 1
                    alpha, beta = self.issue("owner/alpha", number), self.issue("owner/beta", number)
                    self.config["github"]["repository"] = "owner/alpha"
                    self.synchronize([alpha])
                    old = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/alpha:" + str(number),))
                    self.store.execute("UPDATE tasks SET state=?,fingerprint=? WHERE id=?",
                                       (state, "issue:" + str(number) if legacy else old["fingerprint"], old["id"]))
                    self.store.execute("INSERT INTO runs(id,task,project,state,result,started,ended) VALUES (?,?,'p','merged','{}',1,2)",
                                       ("run-" + old["id"], old["id"]))
                    self.store.defer(old["id"], {"reason": "original decision"})
                    self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, old["id"]))
                    before = self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],))
                    self.config["github"]["repository"] = "owner/beta"
                    self.synchronize([beta])
                    self.synchronize([beta])
                    new = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/beta:" + str(number),))
                    self.assertNotEqual(new["id"], old["id"])
                    self.assertEqual((new["state"], new["body"]), ("queued", beta["body"]))
                    self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],)), before)
                    # Returning to the old repository adopts legacy identity without changing its task/history.
                    self.config["github"]["repository"] = "OWNER/ALPHA"
                    self.synchronize([alpha])
                    self.synchronize([alpha])
                    after = self.store.one("SELECT * FROM tasks WHERE id=?", (old["id"],))
                    self.assertEqual(after, {**before, "fingerprint": "issue:owner/alpha:" + str(number)})
                    self.assertEqual(self.store.one("SELECT task FROM runs WHERE id=?", ("run-" + old["id"],))["task"], old["id"])
                    self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (old["id"],))["data"]),
                                     {"reason": "original decision"})
                    self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE fingerprint IN (?,?)",
                                                        ("issue:owner/alpha:" + str(number), "issue:owner/beta:" + str(number)))), 2)

    def test_unverifiable_legacy_issue_is_never_adopted_from_current_configuration(self):
        for evidence in [[{"number": 7}], [{"number": 7, "url": "https://github.com/other/repo/issues/7"}],
                         [{"number": 8, "url": "https://github.com/owner/project/issues/7"}], {}, [None]]:
            with self.subTest(evidence=evidence):
                self.store.execute("DELETE FROM tasks")
                legacy = self.store.add_task("p", "issue", "issue:7", "Legacy", evidence=evidence)
                before = self.store.one("SELECT * FROM tasks WHERE id=?", (legacy,))
                self.synchronize([self.issue()])
                self.synchronize([self.issue()])
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (legacy,)), before)
                self.assertEqual(len(self.store.query("SELECT id FROM tasks")), 2)

    def test_label_policy_relaxation_restores_unchanged_task_once_after_restart(self):
        for policy in ["required", "excluded"]:
            with self.subTest(policy=policy):
                issue = self.issue(number=1 if policy == "required" else 2)
                self.synchronize([issue])
                task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(issue["number"]),))
                if policy == "required":
                    self.config["github"]["labels"] = ["security"]
                else:
                    self.config["github"]["exclude_labels"] = ["bug"]
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], "obsolete")
                self.store.close()
                self.store = self.runtime.store = Store(self.root)
                self.config["github"]["labels"] = []
                self.config["github"]["exclude_labels"] = []
                self.synchronize([issue])
                restored = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
                self.assertEqual(restored["state"], "queued")
                self.assertEqual(restored["evidence"], task["evidence"])
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],)), restored)

    def test_policy_relaxation_preserves_planning_decisions_without_repeated_queueing(self):
        for state in ["obsolete", "deferred", "rejected", "cancelled"]:
            with self.subTest(state=state):
                issue = self.issue(number=len(self.store.query("SELECT id FROM tasks")) + 1)
                self.synchronize([issue])
                task = self.store.one("SELECT * FROM tasks WHERE fingerprint=?", ("issue:owner/project:" + str(issue["number"]),))
                self.store.defer(task["id"], {"reason": "Planning found no actionable change"})
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
                self.config["github"]["labels"] = ["security"]
                self.synchronize([issue])
                self.config["github"]["labels"] = []
                self.synchronize([issue])
                self.synchronize([issue])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], state)
                self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (task["id"],))["data"]),
                                 {"reason": "Planning found no actionable change"})

    def test_old_exclusion_marker_does_not_override_a_newer_obsolete_decision(self):
        issue = self.issue()
        self.synchronize([issue])
        self.config["github"]["labels"] = ["security"]
        self.synchronize([issue])
        self.store.execute("UPDATE tasks SET updated=updated+1 WHERE source='issue'")
        self.config["github"]["labels"] = []
        self.synchronize([issue])
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE source='issue'")["state"], "obsolete")

    def test_issue_sync_page_budget_persists_and_resumes_without_losing_issues(self):
        pages = []
        def checked(args, **kwargs):
            page = int(next(value.split("=", 1)[1] for value in args if value.startswith("page=")))
            pages.append(page)
            start = (page - 1) * 100 + 1
            if start > 205:
                return "[]"
            return encode([{"number": number, "title": "Issue " + str(number), "body": "body",
                            "labels": [], "state": "open", "updated_at": "2026-10-20T00:00:00Z",
                            "html_url": "https://github.com/owner/project/issues/" + str(number)}
                           for number in range(start, min(start + 100, 206))])
        with patch("adaptive_rsi.discovery.ISSUE_SYNC_MAX_PAGES", 2):
            sync(self.runtime, self.project, self.config, checked)
            progress = self.store.setting("issue-sync:p")
            self.assertEqual(progress["page"], 3)
            self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 200)
            sync(self.runtime, self.project, self.config, checked)
        self.assertEqual(pages, [1, 2, 3])
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 205)
        self.assertEqual(self.store.setting("issue-sync:p")["page"], 1)

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

    def test_related_work_reuses_bounded_title_tokenization(self):
        evidence = [{"path": "client.py"}]
        for index in range(40):
            self.store.add_task("p", "inspection", "related-" + str(index),
                                "Handle client response " + str(index), evidence=evidence)
        task = {"id": "subject", "title": "Handle client response", "body": "", "evidence": encode(evidence)}
        before = terms.cache_info()
        first = related_work(self.store, "p", task)
        second = related_work(self.store, "p", task)
        after = terms.cache_info()
        self.assertEqual(first, second)
        self.assertGreater(after.hits, before.hits)

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


class IssueRequirementsTest(unittest.TestCase):
    """Real SQLite/task/run fixtures; no credentials, network or model calls."""

    def setUp(self):
        from adaptive_rsi.runtime import Runtime
        from tests.test_runtime import fixture
        self.temp = tempfile.TemporaryDirectory(prefix="issue-requirements-")
        self.root = Path(self.temp.name)
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, SimpleNamespace())
        self.project_id = self.runtime.add_project(self.repo)
        self.runtime.configure({"github": {"repository": "owner/project"},
                                "sources": {"inspection": False, "issues": False},
                                "evolution": {"enabled": False}}, self.project_id)
        self.project = self.runtime.project(self.project_id)
        self.config = self.runtime.config(self.project_id)
        self.issue = DiscoveryHardeningTest.issue()
        self.synchronize()
        self.task_id = self.store.one("SELECT id FROM tasks WHERE source='issue'")["id"]

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def synchronize(self, rows=None):
        sync(self.runtime, self.project, self.config,
             lambda *args, **kwargs: encode([self.issue] if rows is None else rows))

    def task(self):
        return self.store.one("SELECT * FROM tasks WHERE id=?", (self.task_id,))

    def update(self, body):
        self.issue.update(body=body, updated_at=self.issue["updated_at"] + "1")
        self.synchronize()

    def test_remote_updates_and_revisions_reach_actual_planning_input(self):
        from adaptive_rsi.gitops import git
        self.runtime.revise_task(self.task_id, "Mandatory: reject negative inputs")
        self.update("upstream v2")
        self.runtime.revise_task(self.task_id, "Keep zero valid")
        self.update("upstream v3")
        expected = "upstream v3\n\nMandatory: reject negative inputs\n\nKeep zero valid"
        self.assertEqual(self.task()["body"], expected)
        before = self.task()
        self.synchronize()
        self.assertEqual(self.task(), before)
        self.assertEqual(len(self.store.query("SELECT * FROM task_revisions")), 2)
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.assertEqual(run["task"], self.task_id)
        prompts = []

        def planning(role, prompt, *args, **kwargs):
            self.assertEqual(role, "planning")
            prompts.append(json.loads(prompt.split("\n")[-1]))
            return {"decision": "deferred", "reason": "Fixture stops after planning", "scope": [],
                    "acceptance": [], "risk": "low", "value": {
                        "impact": "medium", "effort": "low", "confidence": "high", "benefit": "Fixture",
                        "cost": "Fixture", "inaction": "Fixture", "evidence": ["User requirements"]}}

        # Execute the production process/pipeline through planning; replace only
        # external/environment probes and the model boundary.
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), git(self.repo, "rev-parse", "HEAD"), "main")), \
             patch.object(self.runtime, "issue_still_open", return_value=True), \
             patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
             patch("adaptive_rsi.discovery.issue_comment_context", return_value={"status": "loaded", "comments": []}), \
             patch.object(self.runtime, "invoke", side_effect=planning):
            self.runtime.process(run)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["task"]["body"], expected)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "deferred")

    def test_legacy_intact_and_lost_additions_migrate_once_on_unchanged_source(self):
        # Legacy sync may have lost the first addition before another revise.
        for lost in [False, True]:
            with self.subTest(lost=lost):
                self.store.execute("DELETE FROM task_revisions")
                first_body = self.issue["body"]
                second_body = "legacy newer upstream" if lost else first_body + "\n\nfirst"
                current = second_body + "\n\nsecond"
                self.store.execute("UPDATE tasks SET body=?,evidence=?,state='deferred',updated=123,available=456 WHERE id=?",
                                   (current, encode([{"number": 7, "url": self.issue["html_url"],
                                                      "updated": self.issue["updated_at"], "state": "open"}]), self.task_id))
                with self.store.connect() as db:
                    db.executemany("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                                   [(self.task_id, first_body, "first"), (self.task_id, second_body, "second")])
                self.synchronize()
                expected_upstream = second_body if lost else first_body
                self.assertEqual(self.task()["body"], expected_upstream + "\n\nfirst\n\nsecond")
                self.assertEqual((self.task()["state"], self.task()["updated"], self.task()["available"]), ("deferred", 123, 456))
                self.store.execute("DELETE FROM task_revisions")
                self.synchronize()
                self.update("fresh upstream")
                self.assertEqual(self.task()["body"], "fresh upstream\n\nfirst\n\nsecond")

    def test_legacy_already_overwritten_body_recovers_and_revise_keeps_order(self):
        original = self.task()["body"]
        with self.store.connect() as db:
            db.execute("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                       (self.task_id, original, "lost local"))
        self.store.execute("UPDATE tasks SET body='remote after loss' WHERE id=?", (self.task_id,))
        self.runtime.revise_task(self.task_id, "new local")
        self.assertEqual(self.task()["body"], "remote after loss\n\nlost local\n\nnew local")
        self.update("latest")
        self.assertEqual(self.task()["body"], "latest\n\nlost local\n\nnew local")

    def test_legacy_sync_lost_latest_revision_but_kept_earlier_addition(self):
        original = self.task()["body"]
        with self.store.connect() as db:
            db.executemany("INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,0)",
                           [(self.task_id, original, "first"), (self.task_id, original + "\n\nfirst", "second")])
        self.store.execute("UPDATE tasks SET body=? WHERE id=?", (original + "\n\nfirst", self.task_id))
        self.synchronize()
        self.assertEqual(self.task()["body"], original + "\n\nfirst\n\nsecond")
        self.synchronize()
        self.assertEqual(self.task()["body"], original + "\n\nfirst\n\nsecond")

    def test_sync_and_local_revision_serialize_snapshot_updates(self):
        import concurrent.futures
        import threading
        from adaptive_rsi.discovery import _issue_task
        self.runtime.revise_task(self.task_id, "first")
        started = threading.Event()
        self.issue.update(body="concurrent upstream", updated_at="new revision")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            futures = []

            def revise():
                started.set()
                self.runtime.revise_task(self.task_id, "second")

            def lookup(db, *args):
                result = _issue_task(db, *args)
                futures.append(pool.submit(revise))
                self.assertTrue(started.wait(5))
                self.assertFalse(futures[0].done())
                return result

            with patch("adaptive_rsi.discovery._issue_task", side_effect=lookup):
                self.synchronize()
            futures[0].result(timeout=10)
        self.assertEqual(self.task()["body"], "concurrent upstream\n\nfirst\n\nsecond")
        self.update("following upstream")
        self.assertEqual(self.task()["body"], "following upstream\n\nfirst\n\nsecond")

    def test_upstream_matching_local_text_is_not_removed_or_deduplicated(self):
        self.runtime.revise_task(self.task_id, "same text")
        self.update("same text")
        self.assertEqual(self.task()["body"], "same text\n\nsame text")
        self.synchronize()
        self.assertEqual(self.task()["body"], "same text\n\nsame text")
        self.runtime.revise_task(self.task_id, "same text")
        self.update("replacement")
        self.assertEqual(self.task()["body"], "replacement\n\nsame text\n\nsame text")

    def test_active_and_stopping_runs_keep_sealed_input_until_finished(self):
        self.runtime.revise_task(self.task_id, "local")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        before = self.task()
        self.update("new upstream")
        self.assertEqual(self.task(), before)
        with self.assertRaisesRegex(ValueError, "active run"):
            self.runtime.revise_task(self.task_id, "forbidden while active")
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (self.task_id,))
        failed = self.task()
        self.synchronize([])
        self.assertEqual(self.task(), failed)
        self.store.execute("UPDATE runs SET state='failed',ended=1 WHERE id=?", (run["id"],))
        self.synchronize([])
        self.assertEqual((self.task()["state"], self.task()["body"]), ("queued", "new upstream\n\nlocal"))
        self.assertEqual(self.store.setting("issue-sync:" + self.project_id)["pending"], {})

    def test_cancelled_run_waits_for_end_and_requires_explicit_revision(self):
        self.runtime.revise_task(self.task_id, "local")
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.config)
        self.runtime.cancel_task(self.task_id)
        before = self.task()
        self.update("upstream while stopping")
        self.assertEqual(self.task(), before)
        with self.assertRaisesRegex(ValueError, "active run"):
            self.runtime.revise_task(self.task_id, "new local")
        self.store.execute("UPDATE runs SET state='cancelled',ended=1 WHERE id=?", (run["id"],))
        self.synchronize([])
        self.assertEqual(self.task(), before)
        self.runtime.revise_task(self.task_id, "new local")
        self.synchronize([])
        self.assertEqual((self.task()["state"], self.task()["body"]),
                         ("queued", "upstream while stopping\n\nlocal\n\nnew local"))

    def test_cancelled_and_delivered_policy_preserves_local_additions(self):
        self.runtime.revise_task(self.task_id, "local")
        original = self.task()["body"]
        for state in ["cancelled", "completed", "merged", "pr_closed", "pr_open", "reverted", "superseded", "expired"]:
            with self.subTest(state=state):
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.task_id))
                self.update("edit for " + state)
                self.assertEqual((self.task()["state"], self.task()["body"]), (state, original))
        for state in ["completed", "merged", "pr_closed"]:
            with self.subTest(reopen=state):
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, self.task_id))
                self.issue["state"] = "closed"
                self.update("closed")
                self.issue["state"] = "open"
                self.update("reopened")
                self.assertEqual((self.task()["state"], self.task()["body"]), ("queued", "reopened\n\nlocal"))

    def test_label_policy_restores_decision_and_supplement(self):
        self.runtime.revise_task(self.task_id, "local")
        self.store.defer(self.task_id, {"reason": "held"})
        self.config["github"]["labels"] = ["security"]
        self.synchronize()
        self.assertEqual(self.task()["state"], "obsolete")
        self.config["github"]["labels"] = []
        self.synchronize()
        self.assertEqual((self.task()["state"], self.task()["body"]), ("deferred", self.issue["body"] + "\n\nlocal"))
        before = self.task()
        self.synchronize()
        self.assertEqual(self.task(), before)
        self.assertEqual(json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (self.task_id,))["data"]), {"reason": "held"})

    def test_oversized_remote_composition_rolls_back_without_losing_local(self):
        self.runtime.revise_task(self.task_id, "local")
        before = self.task()
        progress = self.store.setting("issue-sync:" + self.project_id)
        with self.assertRaisesRegex(ValueError, "30000"):
            self.update("x" * 30000)
        self.assertEqual(self.task(), before)
        self.assertEqual(self.store.setting("issue-sync:" + self.project_id), progress)
        self.update("shortened")
        self.assertEqual(self.task()["body"], "shortened\n\nlocal")


if __name__ == "__main__":
    unittest.main()
