"""Regression checks for isolation, revoked delivery and bounded background work."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.config import BUILTIN_PROVIDERS, fingerprint, merge
from adaptive_rsi.dashboard import records
from adaptive_rsi.delivery import StaleCandidate, feedback, merge_gate, publish, schedule, tick
from adaptive_rsi.discovery import sync
from adaptive_rsi.evolution import Evolution
from adaptive_rsi.gitops import changes, git, snapshot
from adaptive_rsi.process import Cancelled
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_runtime import CHECKS, FakeAgent, fixture


class RuntimeSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "limits": {"retries": 0}}, self.project)
        self.runtime.pause(self.project, False)

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def candidate(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"}, "delivery": {"mode": "pr", "base_branch": "main"}}, self.project)
        task = self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        base = git(self.repo, "rev-parse", "HEAD")
        self.store.execute("UPDATE runs SET base=? WHERE id=?", (base, run["id"]))
        self.store.put_setting("run-baseline:" + run["id"], {"repository": "example/project", "branch": "main", "sha": base})
        self.store.transition(run["id"], "awaiting_approval", {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        self.runtime.deliver(run["id"], automatic=True)
        job = self.store.claim(self.project, self.runtime.config(self.project))
        workspace = snapshot(self.repo, self.root / "candidate")
        (workspace / "calculator.py").write_text("def add(a,b):\n return a+b\n")
        git(workspace, "add", ".")
        return (self.runtime.project(self.project), self.store.one("SELECT * FROM tasks WHERE id=?", (task,)),
                self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],)), workspace, job)

    def test_pause_resume_interrupts_inflight_invoke_and_preserves_cancel_retry(self):
        provider = {"id": "fixture_api", "name": "Fixture", "base_url": "https://example.invalid/v1",
                    "protocol": "chat_completions", "env_key": "", "credential": "none"}
        self.runtime.agent = CodexAgent(self.store)
        self.runtime.configure({"providers": [provider], "models": {"implementation": {
            "provider": "fixture_api", "model": "fixture-model", "effort": "default"}}}, self.project)
        response = {"choices": [{"message": {"tool_calls": [{"id": "finish-1", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"summary": "fixture"})}}]}}]}
        for explicit_cancel in [False, True]:
            with self.subTest(explicit_cancel=explicit_cancel):
                task = self.runtime.goal(self.project, "Pause active worker " + str(explicit_cancel))
                run = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertEqual(run["task"], task)

                def pause_before_response(*args, **kwargs):
                    self.runtime.pause(self.project, True)
                    self.runtime.pause(self.project, False)
                    self.assertTrue(self.runtime.is_cancelled(run))
                    self.assertIsNone(self.store.one("SELECT ended FROM runs WHERE id=?", (run["id"],))["ended"])
                    self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "planning")
                    if explicit_cancel:
                        self.runtime.cancel_task(task)
                    for operation in [lambda: self.runtime.retry(task),
                                      lambda: self.runtime.revise_task(task, "New criterion")]:
                        with self.assertRaisesRegex(ValueError, "Wait for the active run to finish stopping"):
                            operation()
                    self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))
                    return response

                def invoke_candidate(run, project, task, source, base, directory, workspace, strategies):
                    self.runtime.invoke("implementation", "fixture", workspace, run)
                    self.fail("Paused worker accepted the in-flight response")

                base = git(self.repo, "rev-parse", "HEAD")
                with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")), \
                        patch.object(self.runtime, "execute_candidate", side_effect=invoke_candidate), \
                        patch("adaptive_rsi.native.post_json", side_effect=pause_before_response) as post:
                    self.runtime.process(run)
                self.assertEqual(post.call_count, 1)
                expected = "cancelled" if explicit_cancel else "interrupted"
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], expected)
                self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], expected)
                self.assertIsNotNone(self.store.one("SELECT ended FROM runs WHERE id=?", (run["id"],))["ended"])
                self.assertEqual(self.store.one("SELECT state FROM calls WHERE run=?", (run["id"],))["state"], "failed")
                self.assertIsNone(self.store.setting("run-stop:" + run["id"]))
                self.runtime.retry(task)
                fresh = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertNotEqual(fresh["id"], run["id"])
                self.assertFalse(self.runtime.is_cancelled(fresh))
                with patch("adaptive_rsi.native.post_json", return_value=response):
                    self.assertEqual(self.runtime.invoke("implementation", "fixture", self.repo, fresh), {"summary": "fixture"})
                self.store.transition(fresh["id"], "completed", {}, True)

    def test_late_pause_before_approval_interrupts_process_without_auto_publish(self):
        self.runtime.configure({"delivery": {"mode": "pr"}}, self.project)
        for explicit_cancel in [False, True]:
            with self.subTest(explicit_cancel=explicit_cancel):
                task = self.runtime.goal(self.project, "Late pause " + str(explicit_cancel))
                run = self.store.claim(self.project, self.runtime.config(self.project))
                transition = self.store.transition
                attempted = []

                def pause_before_transition(run_id, state, *args, **kwargs):
                    if state == "awaiting_approval":
                        attempted.append(state)
                        self.runtime.pause(self.project, True)
                        self.runtime.pause(self.project, False)
                        if explicit_cancel:
                            self.runtime.cancel_task(task)
                    return transition(run_id, state, *args, **kwargs)

                outcome = {"decision": "pass", "summary": "fixture", "evaluation": {"decision": "pass"},
                           "review": {"decision": "pass"}}
                base = git(self.repo, "rev-parse", "HEAD")
                with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")), \
                        patch.object(self.runtime, "execute_candidate", return_value=outcome), \
                        patch.object(self.store, "transition", side_effect=pause_before_transition), \
                        patch.object(self.runtime, "deliver", wraps=self.runtime.deliver) as deliver, \
                        patch.object(self.runtime, "monitor_strategy") as monitor:
                    self.runtime.process(run)
                self.assertEqual(attempted, ["awaiting_approval"])
                expected = "cancelled" if explicit_cancel else "interrupted"
                saved = self.store.one("SELECT state,ended,result FROM runs WHERE id=?", (run["id"],))
                self.assertEqual(saved["state"], expected)
                self.assertIsNotNone(saved["ended"])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], expected)
                self.assertFalse(self.store.query("SELECT 1 FROM tasks WHERE source='system_publish'"))
                self.assertFalse(self.store.query("SELECT 1 FROM skills WHERE task=?", (task,)))
                self.assertFalse(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='awaiting_approval'", (run["id"],)))
                self.assertIsNone(self.store.setting("run-stop:" + run["id"]))
                deliver.assert_not_called()
                monitor.assert_not_called()
                self.runtime.retry(task)
                fresh = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertFalse(self.runtime.is_cancelled(fresh))
                self.store.transition(fresh["id"], "completed", {}, True)

    def test_late_pause_before_failure_retry_interrupts_process_without_requeue(self):
        self.runtime.configure({"limits": {"retries": 1}}, self.project)
        task = self.runtime.goal(self.project, "Late pause before retry")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        transition = self.store.transition
        attempted = []

        def pause_before_transition(run_id, state, *args, **kwargs):
            if state == "failed":
                attempted.append(kwargs["task_fields"])
                self.runtime.pause(self.project, True)
                self.runtime.pause(self.project, False)
            return transition(run_id, state, *args, **kwargs)

        base = git(self.repo, "rev-parse", "HEAD")
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")), \
                patch.object(self.runtime, "execute_candidate", return_value={"decision": "fail", "reason": "fixture"}), \
                patch.object(self.store, "transition", side_effect=pause_before_transition), \
                patch.object(self.runtime, "monitor_strategy") as monitor:
            self.runtime.process(run)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0]["state"], "queued")
        self.assertEqual(self.store.one("SELECT state,available FROM tasks WHERE id=?", (task,)),
                         {"state": "interrupted", "available": 0})
        saved = self.store.one("SELECT state,ended FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(saved["state"], "interrupted")
        self.assertIsNotNone(saved["ended"])
        self.assertFalse(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='retry'", (run["id"],)))
        self.assertFalse(self.store.query("SELECT 1 FROM tasks WHERE source='system_publish'"))
        self.assertIsNone(self.store.setting("run-stop:" + run["id"]))
        self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))
        monitor.assert_not_called()

    def test_late_pause_before_infrastructure_retry_does_not_requeue(self):
        task = self.runtime.goal(self.project, "Late infrastructure pause")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        transition = self.store.transition
        attempted = []

        def pause_before_transition(run_id, state, *args, **kwargs):
            if state == "blocked":
                attempted.append(state)
                self.runtime.pause(self.project, True)
                self.runtime.pause(self.project, False)
            return transition(run_id, state, *args, **kwargs)

        base = git(self.repo, "rev-parse", "HEAD")
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")), \
                patch.object(self.runtime, "execute_candidate", side_effect=RuntimeError("HTTP 503 connection failed")), \
                patch.object(self.store, "transition", side_effect=pause_before_transition):
            self.runtime.process(run)
        self.assertEqual(attempted, ["blocked"])
        self.assertEqual(self.store.one("SELECT state,available,infra_retries FROM tasks WHERE id=?", (task,)),
                         {"state": "interrupted", "available": 0, "infra_retries": 0})
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "interrupted")
        self.assertFalse(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='infrastructure_retry'", (run["id"],)))

    def test_planning_yield_requeue_respects_pause_cancel_and_normal_yield(self):
        base = git(self.repo, "rev-parse", "HEAD")
        for stop in ["pause", "cancel", None]:
            with self.subTest(stop=stop):
                task = self.runtime.goal(self.project, "Assess before yielding " + str(stop))
                run = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertEqual(run["task"], task)
                higher = self.runtime.goal(self.project, "Higher priority work " + str(stop))
                self.store.execute("UPDATE tasks SET urgency=1 WHERE id=?", (higher,))
                transition = self.store.transition
                yielded = []

                def stop_before_yield(run_id, state, *args, **kwargs):
                    if state == "assessed":
                        yielded.append(run_id)
                        if stop == "pause":
                            self.runtime.pause(self.project, True)
                            self.runtime.pause(self.project, False)
                        elif stop == "cancel":
                            self.runtime.cancel_task(task)
                    return transition(run_id, state, *args, **kwargs)

                with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")), \
                        patch("adaptive_rsi.readiness.inspect", return_value={"ready": True}), \
                        patch.object(self.store, "transition", side_effect=stop_before_yield):
                    self.runtime.process(run)
                self.assertEqual(yielded, [run["id"]])
                expected = "interrupted" if stop == "pause" else "cancelled" if stop == "cancel" else "assessed"
                saved = self.store.one("SELECT state,ended FROM runs WHERE id=?", (run["id"],))
                self.assertEqual(saved["state"], expected)
                self.assertIsNotNone(saved["ended"])
                self.assertEqual(self.store.one("SELECT state,attempts,available FROM tasks WHERE id=?", (task,)),
                                 {"state": "queued" if stop is None else expected,
                                  "attempts": 0 if stop is None else 1, "available": 0})
                self.assertIsNone(self.store.setting("run-stop:" + run["id"]))
                next_run = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertEqual(next_run["task"], higher)
                self.store.transition(next_run["id"], "completed", {}, True)
                fresh = self.store.claim(self.project, self.runtime.config(self.project))
                if stop is None:
                    self.assertEqual(fresh["task"], task)
                    self.assertNotEqual(fresh["id"], run["id"])
                    self.store.transition(fresh["id"], "completed", {}, True)
                else:
                    self.assertIsNone(fresh)

    def test_pause_stop_request_survives_runtime_reopen_and_recovery(self):
        task = self.runtime.goal(self.project, "Durable pause")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.pause(self.project, True)
        reopened = Runtime(Store(self.store.root), object())
        try:
            reopened.pause(self.project, False)
            self.assertTrue(reopened.is_cancelled(run))
            self.assertTrue(self.runtime.is_cancelled(run))
            with patch.object(reopened.updates, "restore_startup"), \
                    patch.object(reopened, "loop", side_effect=reopened.stop.set):
                reopened.start()
                reopened.thread.join(5)
            self.assertFalse(reopened.thread.is_alive())
            self.assertEqual(reopened.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "interrupted")
            self.assertIsNone(reopened.store.setting("run-stop:" + run["id"]))
            reopened.stop.clear()
            reopened.retry(task)
            fresh = reopened.store.claim(self.project, reopened.config(self.project))
            self.assertFalse(reopened.is_cancelled(fresh))
            self.assertNotEqual(fresh["id"], run["id"])
        finally:
            reopened.close()

    def test_pause_marks_only_unfinished_runs_in_its_project(self):
        task = self.runtime.goal(self.project, "Completed history")
        ended = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(ended["id"], "completed", {}, True)
        active_task = self.runtime.goal(self.project, "Active work")
        active = self.store.claim(self.project, self.runtime.config(self.project))
        other = "other-project"
        self.store.execute("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                           (other, other, str(self.root / "other"), "{}", 0, time.time()))
        self.runtime.goal(other, "Other work")
        other_run = self.store.claim(other, self.runtime.config(other))
        self.runtime.pause(self.project, True)
        self.runtime.pause(self.project, False)
        self.assertTrue(self.runtime.is_cancelled(active))
        self.assertFalse(self.runtime.is_cancelled(other_run))
        self.assertIsNone(self.store.setting("run-stop:" + ended["id"]))
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "completed")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (active_task,))["state"], "planning")

    def test_cancelled_unfinished_run_blocks_retry_and_revision_until_it_stops(self):
        for operation in ["retry", "revise"]:
            with self.subTest(operation=operation):
                task = self.runtime.goal(self.project, "Original requirements " + operation)
                run = self.store.claim(self.project, self.runtime.config(self.project))
                self.runtime.cancel_task(task)
                before = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
                with self.assertRaisesRegex(ValueError, "Wait for the active run to finish stopping"):
                    if operation == "retry":
                        self.runtime.retry(task)
                    else:
                        self.runtime.revise_task(task, "Mandatory new criterion")
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (task,)), before)
                self.assertTrue(self.runtime.is_cancelled(run))
                self.assertFalse(self.store.query("SELECT * FROM task_revisions WHERE task=?", (task,)))
                self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))
                self.store.transition(run["id"], "interrupted", {"reason": "Worker stopped"}, True)
                if operation == "retry":
                    self.runtime.retry(task)
                else:
                    self.runtime.revise_task(task, "Mandatory new criterion")
                fresh = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertNotEqual(fresh["id"], run["id"])
                body = self.store.one("SELECT body FROM tasks WHERE id=?", (task,))["body"]
                self.assertEqual("Mandatory new criterion" in body, operation == "revise")
                self.store.transition(fresh["id"], "completed", {}, True)

    def test_system_requests_preserve_cancellation_until_worker_stops(self):
        self.runtime.configure({"sources": {"inspection": True, "issues": False},
                                "github": {"repository": "owner/project"}}, self.project)
        config = self.runtime.config(self.project)
        for kind in ["light", "issues", "evolve", "reassess"]:
            with self.subTest(kind=kind):
                task = self.runtime.request(self.project, kind)
                run = self.store.claim(self.project, config)
                self.assertEqual(run["task"], task)
                self.runtime.cancel_task(task)
                before = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
                self.assertEqual(self.runtime.request(self.project, kind), task)
                if kind == "light":
                    self.runtime.schedule(self.project, config)
                self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (task,)), before)
                self.assertTrue(self.runtime.is_cancelled(run))
                self.assertIsNone(self.store.claim(self.project, config))
                self.store.transition(run["id"], "interrupted", {}, True)
                self.assertEqual(self.runtime.request(self.project, kind), task)
                fresh = self.store.claim(self.project, config)
                self.assertEqual(fresh["task"], task)
                self.assertNotEqual(fresh["id"], run["id"])
                self.store.transition(fresh["id"], "completed", {}, True)

    def test_failure_retry_is_atomic_with_dependency_cleanup(self):
        config = self.runtime.config(self.project)
        config["limits"]["retries"] = 1
        parent = self.runtime.goal(self.project, "Parent repair")
        child = self.runtime.goal(self.project, "Wait for parent merge")
        self.runtime.set_dependency(child, parent)
        run = self.store.claim(self.project, config)
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (parent,))
        transition = self.store.transition
        observed = []

        def cleanup_after_transition(*args, **kwargs):
            changed = transition(*args, **kwargs)
            self.runtime.release_dead_dependencies()
            observed.append(self.store.one("SELECT state,available FROM tasks WHERE id=?", (parent,)))
            return changed

        with patch.object(self.store, "transition", side_effect=cleanup_after_transition):
            self.runtime.finish_failure(run, task, {"decision": "fail", "reason": "Check failed"}, config)
        self.assertEqual(observed[0]["state"], "queued")
        self.assertGreater(observed[0]["available"], time.time())
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "failed")
        self.assertEqual(self.store.one("SELECT dependency FROM task_dependencies WHERE task=?", (child,))["dependency"], parent)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (child,))["state"], "queued")
        self.assertIsNone(self.store.claim(self.project, config))
        retry = self.store.one("SELECT project,run,message,data FROM events WHERE kind='retry'")
        self.assertEqual(retry, {"project": self.project, "run": run["id"],
                                 "message": "Fresh run scheduled with bounded backoff", "data": "{}"})
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (parent,))
        self.assertEqual(self.store.claim(self.project, config)["task"], child)

    def test_dependency_cleanup_rechecks_parent_retry_and_child_state(self):
        for change in ["retry_parent", "cancel_child"]:
            with self.subTest(change=change):
                parent = self.runtime.goal(self.project, "Parent " + change)
                child = self.runtime.goal(self.project, "Child " + change)
                self.runtime.set_dependency(child, parent)
                self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (parent,))
                query = self.store.query

                def change_after_query(sql, args=()):
                    rows = query(sql, args)
                    if "td.task AS task" in sql:
                        if change == "retry_parent":
                            self.runtime.retry(parent)
                        else:
                            self.runtime.cancel_task(child)
                    return rows

                with patch.object(self.store, "query", side_effect=change_after_query):
                    self.runtime.release_dead_dependencies()
                self.assertEqual(self.store.one("SELECT dependency FROM task_dependencies WHERE task=?", (child,))["dependency"], parent)
                expected = "queued" if change == "retry_parent" else "cancelled"
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (child,))["state"], expected)
                self.assertFalse(self.store.query("SELECT * FROM events WHERE kind='dependency_released'"))

    def test_cancel_rechecks_terminal_state_after_preview(self):
        from adaptive_rsi.task_lifecycle import cancellation_preview
        for state in ["pr_open", "merged", "completed"]:
            with self.subTest(state=state):
                task = self.runtime.goal(self.project, "Finishing " + state)
                run = self.store.claim(self.project, self.runtime.config(self.project))
                self.assertEqual(run["task"], task)
                if state in ["pr_open", "merged"]:
                    self.store.transition(run["id"], "awaiting_approval", {}, True)
                with self.store.connect() as db:
                    preview = cancellation_preview(db, task)
                if state in ["pr_open", "merged"]:
                    self.store.execute("UPDATE runs SET state=? WHERE id=?", (state, run["id"]))
                    self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task))
                else:
                    self.store.transition(run["id"], state, {}, True)
                with self.assertRaisesRegex(ValueError, "finished"):
                    self.runtime.cancel_task(task, expected=preview['revision'])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], state)
                self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], state)
                self.assertFalse(self.store.query("SELECT * FROM events WHERE kind='cancelled'"))
                with self.assertRaises(ValueError):
                    self.runtime.retry(task)

    def test_cancel_before_failure_retry_preserves_cancellation(self):
        config = self.runtime.config(self.project)
        config["limits"]["retries"] = 1
        task = self.runtime.goal(self.project, "Cancelled repair")
        run = self.store.claim(self.project, config)
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.runtime.cancel_task(task)
        self.runtime.finish_failure(run, row, {"decision": "fail", "reason": "Check failed"}, config)
        self.assertEqual(self.store.one("SELECT state,available FROM tasks WHERE id=?", (task,)),
                         {"state": "cancelled", "available": 0})
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "cancelled")
        self.assertFalse(self.store.query("SELECT * FROM events WHERE kind='retry'"))

    def test_retry_revokes_queued_approval_even_when_fresh_verification_fails(self):
        task = self.runtime.goal(self.project, "Original requirements")
        old = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(old["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        queued = self.runtime.deliver(old["id"])
        self.runtime.cancel_task(task)
        self.runtime.retry(task)
        fresh = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(fresh["task"], task)
        self.store.transition(fresh["id"], "failed", {"reason": "New verification fails"}, True)
        self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (old["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (queued,))["state"], "cancelled")
        with self.assertRaisesRegex(ValueError, "awaiting approval"):
            self.runtime.deliver(old["id"])

    def test_retry_invalidation_is_atomic_and_preserves_delivered_history(self):
        task = self.runtime.goal(self.project, "Original requirements")
        old = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(old["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        queued = self.runtime.deliver(old["id"])
        for state in ["pr_open", "merged"]:
            self.store.execute("INSERT INTO runs(id,task,project,state,config,started,ended,result) "
                               "VALUES (?,?,?,?,?,1,2,?)",
                               (state, task, self.project, state, old["config"], encode({"pr": state})))
        # Legacy cancellations may have left pending approvals; retry must revoke
        # those atomically even though new cancellations already revoke them.
        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task,))
        self.store.execute("CREATE TRIGGER reject_retry BEFORE UPDATE OF state ON tasks "
                           "WHEN NEW.id='" + task + "' AND NEW.state='queued' "
                           "BEGIN SELECT RAISE(ABORT, 'Injected retry write failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Injected retry write failure"):
                self.runtime.retry(task)
        finally:
            self.store.execute("DROP TRIGGER reject_retry")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "cancelled")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (queued,))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (old["id"],))["state"], "awaiting_approval")
        self.runtime.retry(task)
        for state in ["pr_open", "merged"]:
            self.assertEqual(self.store.one("SELECT state,result FROM runs WHERE id=?", (state,)),
                             {"state": state, "result": encode({"pr": state})})

    def test_claimed_publish_exits_after_retry_before_loading_candidate(self):
        _, task, original, _, job = self.candidate()
        self.runtime.cancel_task(task["id"])
        self.runtime.retry(task["id"])
        with patch("adaptive_rsi.runtime.changes") as load, patch.object(self.runtime, "publish") as remote:
            self.runtime.process(job)
        load.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (job["id"],))["state"], "interrupted")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], "queued")

    def test_retry_during_publish_rechecks_each_remote_write_and_keeps_pr_receipt(self):
        _, task, original, workspace, job = self.candidate()
        outcome = json.loads(original["result"])
        outcome["patch_hash"] = fingerprint(git(workspace, "diff", original["base"], "--binary", "--no-ext-diff", "--no-textconv"))
        for boundary in ["fetch", "push", "create", "unchanged"]:
            with self.subTest(boundary=boundary):
                # Each pass uses the same sealed candidate and a fresh publish worker directory.
                self.store.execute("UPDATE runs SET state='awaiting_approval',path=?,result=? WHERE id=?",
                                   (str(workspace.parent), encode(outcome), original["id"]))
                self.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (task["id"],))
                self.store.execute("UPDATE tasks SET state=? WHERE id=?", ("planning" if boundary == "fetch" else "queued", job["task"]))
                worker = self.store.claim(self.project, self.runtime.config(self.project)) if boundary != "fetch" else job
                def retry():
                    self.runtime.cancel_task(task["id"])
                    self.runtime.retry(task["id"])
                def fetch(*args):
                    if boundary == "fetch":
                        retry()
                    return str(self.repo), original["base"], "main"
                def push(*args):
                    if boundary == "push":
                        retry()
                def gh(argv, **kwargs):
                    if argv[:3] == ["gh", "pr", "list"]:
                        return "[]"
                    self.assertEqual(argv[:3], ["gh", "pr", "create"])
                    if boundary == "create":
                        retry()
                    return "https://github.com/example/project/pull/1"
                with patch("adaptive_rsi.delivery.baseline", side_effect=fetch), \
                        patch("adaptive_rsi.delivery.github_git", side_effect=push) as pushed, \
                        patch("adaptive_rsi.delivery.checked", side_effect=gh) as remote:
                    self.runtime.process(worker)
                saved = self.store.one("SELECT state,result FROM runs WHERE id=?", (original["id"],))
                self.assertEqual(saved["state"], "pr_open" if boundary == "unchanged" else "superseded")
                self.assertEqual(pushed.call_count, 0 if boundary == "fetch" else 1)
                creates = [c for c in remote.call_args_list if c.args[0][:3] == ["gh", "pr", "create"]]
                self.assertEqual(len(creates), int(boundary in ["create", "unchanged"]))
                if boundary in ["create", "unchanged"]:
                    self.assertEqual(json.loads(saved["result"])["pr"], "https://github.com/example/project/pull/1")
                if boundary == "create":
                    self.assertTrue(json.loads(saved["result"])["close_pending"])
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"],
                                 "pr_open" if boundary == "unchanged" else "queued")
                self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=? AND state='queued'", (task["id"],))

    def test_stale_publish_failure_after_retry_cannot_block_new_requirements(self):
        _, task, original, workspace, job = self.candidate()
        outcome = json.loads(original["result"])
        outcome["patch_hash"] = fingerprint(git(workspace, "diff", original["base"], "--binary", "--no-ext-diff", "--no-textconv"))
        self.store.execute("UPDATE runs SET path=?,result=? WHERE id=?",
                           (str(workspace.parent), encode(outcome), original["id"]))
        def stale_publish(*args):
            self.runtime.cancel_task(task["id"])
            self.runtime.retry(task["id"])
            raise StaleCandidate("Target changed during publishing")
        with patch.object(self.runtime, "publish", side_effect=stale_publish):
            self.runtime.process(job)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")

    def test_revision_supersedes_pending_candidates_and_only_cancels_queued_publish_jobs(self):
        task = self.runtime.goal(self.project, "Original requirements")
        pending = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(pending["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        queued = self.runtime.deliver(pending["id"])
        for state in ["pr_open", "merged"]:
            self.store.execute("INSERT INTO runs(id,task,project,state,config,started,ended,result) "
                               "VALUES (?,?,?,?,?,1,2,?)",
                               (state, task, self.project, state, pending["config"], encode({"pr": state})))
            self.store.add_task(self.project, "system_publish", "publish:" + state,
                                "Previously delivered", body=state)
        self.runtime.cancel_task(task)
        self.runtime.revise_task(task, "New acceptance criterion")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (pending["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (queued,))["state"], "cancelled")
        for state in ["pr_open", "merged"]:
            self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (state,))["state"], state)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE body=? AND source='system_publish'", (state,))["state"], "queued")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")
        with self.assertRaisesRegex(ValueError, "awaiting approval"):
            self.runtime.deliver(pending["id"])

    def test_revision_invalidation_rolls_back_with_requirement_update(self):
        task = self.runtime.goal(self.project, "Original requirements")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(run["id"], "awaiting_approval", {
            "evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        queued = self.runtime.deliver(run["id"])
        # Keep an old pending candidate to exercise the revision transaction's
        # rollback independently from the cancellation transaction.
        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task,))
        self.store.execute("CREATE TRIGGER reject_revision BEFORE UPDATE OF body ON tasks "
                           "BEGIN SELECT RAISE(ABORT, 'Injected requirement write failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "Injected requirement write failure"):
                self.runtime.revise_task(task, "New acceptance criterion")
        finally:
            self.store.execute("DROP TRIGGER reject_revision")
        self.assertEqual(self.store.one("SELECT state,body FROM tasks WHERE id=?", (task,)),
                         {"state": "cancelled", "body": "Original requirements"})
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "awaiting_approval")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (queued,))["state"], "queued")
        self.assertFalse(self.store.query("SELECT * FROM task_revisions WHERE task=?", (task,)))

    def test_claimed_publish_job_rejects_superseded_candidate_before_loading_files(self):
        _, task, original, _, job = self.candidate()
        self.runtime.cancel_task(task["id"])
        self.runtime.revise_task(task["id"], "New acceptance criterion")
        with patch("adaptive_rsi.runtime.changes") as load, patch.object(self.runtime, "publish") as remote:
            self.runtime.process(job)
        load.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (job["id"],))["state"], "interrupted")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"], "queued")

    def test_revision_during_publish_fetch_blocks_old_candidate_without_rewriting_source(self):
        _, task, original, workspace, job = self.candidate()
        outcome = json.loads(original["result"])
        outcome["patch_hash"] = fingerprint(git(workspace, "diff", original["base"], "--binary", "--no-ext-diff", "--no-textconv"))
        self.store.execute("UPDATE runs SET path=?,result=? WHERE id=?",
                           (str(workspace.parent), encode(outcome), original["id"]))

        def revise_during_fetch(*args):
            self.runtime.cancel_task(task["id"])
            self.runtime.revise_task(task["id"], "Mandatory: reject negative inputs")
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (job["task"],))["state"], "planning")
            return str(self.repo), original["base"], "main"

        with patch("adaptive_rsi.delivery.baseline", side_effect=revise_during_fetch) as fetch, \
                patch("adaptive_rsi.delivery.checked", return_value="[]") as command, \
                patch("adaptive_rsi.delivery.github_git") as push:
            self.runtime.process(job)
        fetch.assert_called_once()
        push.assert_not_called()
        self.assertFalse(any("create" in call.args[0] for call in command.call_args_list))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (job["id"],))["state"], "interrupted")
        source = self.store.one("SELECT state,body FROM tasks WHERE id=?", (task["id"],))
        self.assertEqual(source["state"], "queued")
        self.assertIn("Mandatory: reject negative inputs", source["body"])

    def test_periodic_scan_preserves_quota_backoff_until_resume_budget(self):
        self.runtime.configure({"sources": {"inspection": True, "issues": False},
                                "schedule": {"light_minutes": 5}}, self.project)
        config = self.runtime.config(self.project)
        now = 1800000000
        with patch("adaptive_rsi.runtime.time.time", return_value=now), \
                patch("adaptive_rsi.store.time.time", return_value=now):
            self.runtime.schedule(self.project, config, now=now)
            run = self.store.claim(self.project, config)
            self.store.transition(run["id"], "waiting_budget", {"failure": {"code": "budget"}}, True,
                                  task_fields={"available": now + 900, "budget_waits": 1})
        with patch("adaptive_rsi.runtime.time.time", return_value=now + 301), \
                patch("adaptive_rsi.store.time.time", return_value=now + 301):
            self.runtime.schedule(self.project, config, now=now + 301)
            self.runtime.resume_budget(self.project, config)
            self.assertEqual(self.store.one("SELECT state,available,budget_waits FROM tasks WHERE id=?", (run["task"],)),
                             {"state": "waiting_budget", "available": now + 900, "budget_waits": 1})
            self.assertIsNone(self.store.claim(self.project, config))
        with patch("adaptive_rsi.runtime.time.time", return_value=now + 900), \
                patch("adaptive_rsi.store.time.time", return_value=now + 900):
            self.runtime.resume_budget(self.project, config)
            fresh = self.store.claim(self.project, config)
        self.assertEqual(fresh["task"], run["task"])
        self.assertNotEqual(fresh["id"], run["id"])

    def test_publish_rechecks_pause_revocation_stop_and_cancellation_after_fetch(self):
        project, task, run, workspace, job = self.candidate()
        for reason in ["pause", "revoke", "stop", "cancel"]:
            self.runtime.pause(self.project, False)
            self.runtime.stop.clear()
            self.runtime.configure({"delivery": {"mode": "pr"}}, self.project)
            self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (job["task"],))
            def fetch(*args):
                if reason == "pause":
                    self.runtime.pause(self.project, True)
                elif reason == "revoke":
                    self.runtime.configure({"delivery": {"mode": "local"}}, self.project)
                elif reason == "stop":
                    self.runtime.stop.set()
                else:
                    self.runtime.cancel_task(job["task"])
                return str(self.repo), run["base"], "main"
            with self.subTest(reason=reason), patch("adaptive_rsi.delivery.baseline", side_effect=fetch), \
                    patch("adaptive_rsi.delivery.checked", return_value="[]") as command, \
                    patch("adaptive_rsi.delivery.github_git") as push:
                with self.assertRaises((RuntimeError, Cancelled)):
                    publish(self.runtime, project, task, run, workspace, self.runtime.config(self.project))
                push.assert_not_called()
                self.assertFalse(any("create" in call.args[0] for call in command.call_args_list))

    def test_pause_during_push_prevents_pr_creation(self):
        project, task, run, workspace, _ = self.candidate()
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), run["base"], "main")), \
                patch("adaptive_rsi.delivery.checked", return_value="[]") as command, \
                patch("adaptive_rsi.delivery.github_git", side_effect=lambda *args: self.runtime.pause(self.project, True)) as push:
            with self.assertRaises(Cancelled):
                publish(self.runtime, project, task, run, workspace, self.runtime.config(self.project))
            push.assert_called_once()
            self.assertFalse(any("create" in call.args[0] for call in command.call_args_list))

    def test_review_receives_the_exact_sealed_patch_and_fails_closed_when_missing_or_oversized(self):
        class CaptureAgent(FakeAgent):
            prompt = None
            def invoke(self, role, prompt, *args, **kwargs):
                self.prompt = prompt
                return {"summary": "review evidence captured"}

        agent = CaptureAgent()
        self.runtime.agent = agent
        workspace = self.root / "review-run" / "candidate"
        workspace.mkdir(parents=True)
        patch_path = workspace.parent / "candidate.patch"
        patch_text = "diff --git a/calculator.py b/calculator.py\n@@ -1 +1 @@\n-return a-b\n+return a+b\n"
        patch_path.write_text(patch_text)
        self.runtime.goal(self.project, "Review sealed candidate")
        run = self.store.claim(self.project, self.runtime.config(self.project))

        self.runtime.invoke("review", "Review this candidate", workspace, run)
        self.assertIn(patch_text, agent.prompt)
        self.assertIn("untrusted evidence", agent.prompt)

        patch_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "diff is unavailable"):
            self.runtime.invoke("review", "Review this candidate", workspace, run)
        patch_path.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(RuntimeError, "256 KiB"):
            self.runtime.invoke("review", "Review this candidate", workspace, run)
        self.assertEqual(agent.prompt.count(patch_text), 1)

    def test_cancelled_delivery_tracker_never_submits_a_merge_after_baseline_fetch(self):
        project, task, run, _, job = self.candidate()
        config = self.runtime.config(self.project)
        config["delivery"]["auto_merge"] = True
        self.runtime.configure({"delivery": {"auto_merge": True}}, self.project)
        self.store.execute("UPDATE runs SET state='pr_open',ended=?,config=?,result=? WHERE id=?",
                           (time.time(), encode(config), encode({"pr": "https://github.com/example/project/pull/1"}), run["id"]))
        self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (task["id"],))
        self.store.put_setting("delivery:" + run["id"], {"head": "candidate-head", "base": run["base"], "branch": "main"})
        self.store.put_setting("run-baseline:" + run["id"],
                               {"repository": "example/project", "branch": "main", "sha": run["base"]})
        pr = {"state": "OPEN", "headRefOid": "candidate-head", "baseRefOid": run["base"],
              "baseRefName": "main", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
              "statusCheckRollup": [{"conclusion": "SUCCESS"}]}

        def cancel_during_fetch(*args):
            self.runtime.cancel_task(job["task"])
            return str(self.repo), run["base"], "main"

        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), \
                patch("adaptive_rsi.delivery.baseline", side_effect=cancel_during_fetch) as fetch, \
                patch("adaptive_rsi.delivery.checked") as remote:
            tick(self.runtime, now=time.time() + 61, project_id=self.project, active_run=job["id"])

        fetch.assert_called_once()
        self.assertFalse(any("merge" in call.args[0] for call in remote.call_args_list))

    def test_issue_revision_change_blocks_and_closes_the_old_auto_merge_candidate(self):
        project, task, run, _, job = self.candidate()
        config = self.runtime.config(self.project)
        config["delivery"]["auto_merge"] = True
        self.store.execute("UPDATE runs SET state='pr_open',ended=?,config=?,result=? WHERE id=?",
                           (time.time(), encode(config), encode({"pr": "https://github.com/example/project/pull/2"}), run["id"]))
        self.store.execute("UPDATE tasks SET state='pr_open',source='issue',evidence=? WHERE id=?",
                           (encode([{"number": 42, "updated": "2026-10-21T10:00:00Z"}]), task["id"]))
        self.runtime.configure({"delivery": {"auto_merge": True}}, self.project)
        self.store.transition(job["id"], "completed", {}, True)
        self.store.put_setting("delivery:" + run["id"], {"head": "candidate-head", "base": run["base"], "branch": "main"})
        self.store.put_setting("run-baseline:" + run["id"],
                               {"repository": "example/project", "branch": "main", "sha": run["base"]})
        pr = {"state": "OPEN", "headRefOid": "candidate-head", "baseRefOid": run["base"],
              "baseRefName": "main", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
              "statusCheckRollup": [{"conclusion": "SUCCESS"}]}

        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), \
                patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), run["base"], "main")), \
                patch.object(self.runtime, "issue_still_open", return_value=False) as issue, \
                patch("adaptive_rsi.delivery.checked", return_value="") as remote:
            tick(self.runtime, now=time.time() + 61, project_id=self.project)

        issue.assert_called_once()
        self.assertFalse(any("merge" in call.args[0] for call in remote.call_args_list))
        self.assertTrue(any(call.args[0][1:3] == ["pr", "close"] for call in remote.call_args_list))
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "superseded")

    def test_issue_delivery_gate_checks_revision_and_label_policy(self):
        self.runtime.configure({"github": {"repository": "example/project", "labels": ["bug"],
                                            "exclude_labels": ["wontfix"]}}, self.project)
        task_id = self.store.add_task(self.project, "issue", "issue:42", "Fix a bug", "body",
                                      [{"number": 42, "updated": "2026-10-21T10:00:00Z"}])
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        responses = [
            {"state": "OPEN", "updatedAt": "2026-10-21T10:00:00Z", "labels": [{"name": "bug"}]},
            {"state": "OPEN", "updatedAt": "2026-10-21T10:00:00Z", "labels": [{"name": "bug"}, {"name": "wontfix"}]},
            {"state": "OPEN", "updatedAt": "2026-10-21T11:00:00Z", "labels": [{"name": "bug"}]},
        ]
        with patch("adaptive_rsi.runtime.checked", side_effect=[encode(item) for item in responses]):
            self.assertTrue(self.runtime.issue_still_open(self.runtime.project(self.project), task,
                                                          self.runtime.config(self.project)))
            self.assertFalse(self.runtime.issue_still_open(self.runtime.project(self.project), task,
                                                           self.runtime.config(self.project)))
            with self.assertRaises(StaleCandidate):
                self.runtime.issue_still_open(self.runtime.project(self.project), task,
                                              self.runtime.config(self.project))

    def test_publish_resumes_commit_after_branch_was_created_before_interruption(self):
        project, task, run, workspace, _ = self.candidate()
        git(workspace, "checkout", "-b", "feat/rsi-" + run["id"])
        changes(workspace)
        def command(args, **kwargs):
            return "[]" if "list" in args else "https://github.com/example/project/pull/1"
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), run["base"], "main")), \
                patch("adaptive_rsi.delivery.checked", side_effect=command), patch("adaptive_rsi.delivery.github_git"):
            publish(self.runtime, project, task, run, workspace, self.runtime.config(self.project))
        self.assertNotEqual(git(workspace, "rev-parse", "HEAD"), run["base"])
        self.assertEqual(git(workspace, "diff", "--cached", "--name-only"), "")

    def test_manual_reapproval_requeues_terminal_job_with_fresh_authorization(self):
        _, _, run, _, job = self.candidate()
        for state in ["failed", "blocked", "cancelled", "interrupted"]:
            self.store.execute("UPDATE runs SET state=?,ended=? WHERE id=?", (state, time.time(), job["id"]))
            self.store.execute("UPDATE tasks SET state=? WHERE id=?", (state, job["task"]))
            self.runtime.configure({"delivery": {"mode": "local"}}, self.project)
            task = self.runtime.deliver(run["id"])
            row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
            self.assertEqual(row["state"], "queued")
            self.assertEqual(json.loads(row["evidence"]), [{"authorization": "per_candidate"}])
            self.assertEqual(self.runtime.deliver(run["id"]), task)
            self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='system_publish'")), 1)

    def test_local_builtin_native_execution_probe_and_accounting_agree_without_codex(self):
        for provider in [p for p in BUILTIN_PROVIDERS if p["id"] in ["ollama", "lmstudio"]]:
            name = provider["id"]
            response = {"tool_calls": [{"id": "finish", "function": {"name": "finish", "arguments": '{"summary":"RSI_OK"}'}}]}
            reply = {"message": response} if name == "ollama" else {"choices": [{"message": response}]}
            with self.subTest(provider=name), patch("adaptive_rsi.agent.shutil.which", return_value=None), \
                    patch("adaptive_rsi.native.post_json", return_value=reply) as api:
                self.assertTrue(CodexAgent.available(provider))
                self.runtime.model_center.probe(name, "test-local")
                self.assertEqual(api.call_args.args[0], provider["base_url"] + ("/api/chat" if name == "ollama" else "/chat/completions"))
            call = self.store.one("SELECT unit,state FROM calls WHERE provider=?", (name,))
            self.assertEqual(call, {"unit": "http_request", "state": "completed"})

    def test_unstable_failed_ci_enters_attention_and_recovers_without_stopping_tracking(self):
        project, task, run, _, _ = self.candidate()
        self.store.execute("UPDATE runs SET state='pr_open',result=? WHERE id=?", (encode({"pr": "https://github.com/example/project/pull/1"}), run["id"]))
        self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (task["id"],))
        self.store.execute("UPDATE runs SET ended=? WHERE ended IS NULL", (time.time(),))
        self.store.put_setting("delivery:" + run["id"], {"head": "head", "base": run["base"], "branch": "main"})
        pr = {"state": "OPEN", "headRefOid": "head", "baseRefOid": run["base"], "mergeable": "MERGEABLE",
              "mergeStateStatus": "UNSTABLE", "statusCheckRollup": [{"conclusion": "FAILURE"}]}
        other = self.runtime.add_project(fixture(self.root / "other"))
        now = time.time()
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr):
            tick(self.runtime, now)
            self.assertEqual(merge_gate(pr, "head", run["base"]), "ci_failed")
            self.assertEqual(merge_gate({**pr, "statusCheckRollup": [{"status": "IN_PROGRESS"}, {"conclusion": "FAILURE"}]},
                                        "head", run["base"]), "ci_failed")
            rows = records(self.store, self.project, group="attention", limit=1)
            self.assertEqual(rows["tasks"][0]["display_state"], "ci_failed")
            self.assertEqual(next(r for r in rows["runs"] if r["id"] == run["id"])["delivery_status"], "ci_failed")
            self.assertEqual(records(self.store, other, group="attention")["tasks"], [])
            self.assertEqual(records(self.store, self.project, group="active", include_system=False)["tasks"], [])
            pr.update(mergeStateStatus="CLEAN", statusCheckRollup=[{"conclusion": "SUCCESS"}])
            tick(self.runtime, now + 61)
        self.assertEqual(records(self.store, self.project, group="attention")["tasks"], [])
        self.assertEqual(records(self.store, self.project, group="active", include_system=False)["tasks"][0]["id"], task["id"])
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "pr_open")

    def test_issue_server_since_skips_history_and_preserves_held_revision_after_restart(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "example/project", "max_issues": 1}})
        issue = {"number": 9000, "title": "New", "body": "new", "labels": [], "state": "open",
                 "updated_at": "2026-10-21T10:00:00Z", "html_url": "https://github.com/example/project/issues/9000"}
        requests = []
        def gh(args, **kwargs):
            if args[1] != "api":
                return "[]"
            requests.append(args)
            page = next(a for a in args if a.startswith("page="))
            return encode([issue]) if page == "page=1" else "[]"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertFalse(any(a.startswith("since=") for a in requests[0]))
        self.store = self.runtime.store = Store(self.store.root)
        task = self.store.one("SELECT * FROM tasks WHERE source='issue'")
        self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (task["id"],))
        issue["updated_at"] = "2026-10-21T11:00:00Z"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertTrue(any(a.startswith("since=") for a in requests[-1]))
        self.assertIn("9000", self.store.setting("issue-sync:" + self.project)["pending"])
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task["id"],))
        sync(self.runtime, self.runtime.project(self.project), config, lambda *a, **kw: "[]")
        self.assertEqual(json.loads(self.store.one("SELECT evidence FROM tasks WHERE id=?", (task["id"],))["evidence"])[0]["updated"], issue["updated_at"])

    def test_changed_issue_invalidates_waiting_candidate_and_applies_held_revision(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"},
                                "delivery": {"mode": "pr"}, "limits": {"retries": 3}}, self.project)
        task = self.store.add_task(self.project, "issue", "issue:91", "Fix addition", "old requirements",
                                   [{"number": 91, "updated": "2026-10-20T10:00:00Z", "url": "https://github.com/example/project/issues/91"}])
        original = self.store.claim(self.project, self.runtime.config(self.project))
        base = git(self.repo, "rev-parse", "HEAD")
        run_dir = self.root / "awaiting-approval"
        candidate = snapshot(self.repo, run_dir / "candidate")
        (candidate / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        git(candidate, "add", ".")
        candidate_patch = git(candidate, "diff", base, "--binary", "--no-ext-diff", "--no-textconv")
        outcome = {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"},
                   "summary": "fixed", "patch_hash": fingerprint(candidate_patch)}
        self.store.execute("UPDATE runs SET path=?,base=? WHERE id=?", (str(run_dir), base, original["id"]))
        self.store.transition(original["id"], "awaiting_approval", outcome, True)
        self.runtime.deliver(original["id"])
        publishing = self.store.claim(self.project, self.runtime.config(self.project))
        newer = "2026-10-21T11:00:00Z"

        with patch("adaptive_rsi.runtime.checked", return_value=encode({
                "state": "OPEN", "updatedAt": newer, "labels": []})):
            self.runtime.process(publishing)

        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "blocked")
        issue = {"number": 91, "title": "Updated issue", "body": "new requirements", "labels": [],
                 "state": "open", "updated_at": newer,
                 "html_url": "https://github.com/example/project/issues/91"}
        sync(self.runtime, self.runtime.project(self.project), self.runtime.config(self.project),
             lambda *args, **kwargs: encode([issue]))
        refreshed = self.store.one("SELECT state,title,body,evidence FROM tasks WHERE id=?", (task,))
        self.assertEqual((refreshed["state"], refreshed["title"], refreshed["body"]),
                         ("queued", "Updated issue", "new requirements"))
        self.assertEqual(json.loads(refreshed["evidence"])[0]["updated"], newer)

    def test_closed_issue_revision_stays_durable_until_active_task_can_be_obsoleted(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "example/project", "max_issues": 1}})
        task = self.store.add_task(self.project, "issue", "issue:73", "Active issue", "old body",
                                   [{"number": 73, "updated": "2026-10-20T10:00:00Z", "url": "https://github.com/example/project/issues/73"}])
        self.store.execute("UPDATE tasks SET state='executing' WHERE id=?", (task,))
        issue = {"number": 73, "title": "Closed issue", "body": "closed while running", "labels": [], "state": "closed",
                 "updated_at": "2026-10-21T10:00:00Z", "html_url": "https://github.com/example/project/issues/73"}
        def gh(args, **kwargs):
            if args[1] != "api":
                return "[]"
            page = next(a for a in args if a.startswith("page="))
            return encode([issue]) if page == "page=1" else "[]"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertIn("73", self.store.setting("issue-sync:" + self.project)["pending"])
        self.assertEqual(self.store.one("SELECT body FROM tasks WHERE id=?", (task,))["body"], "old body")
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (task,))
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "obsolete")
        self.assertNotIn("73", self.store.setting("issue-sync:" + self.project)["pending"])

    def test_production_evolution_orders_are_balanced_and_recorded(self):
        self.runtime.configure({"limits": {"daily_calls": 200}, "evolution": {"min_cases_per_split": 1, "max_calls": 200}}, self.project)
        evolution = Evolution(self.runtime)
        for split in ["validation", "holdout"]:
            evolution.import_case(self.project, {"family": split, "split": split, "repository": str(self.repo),
                                                "base": "HEAD", "task": split, "checks": CHECKS, "source": "synthetic"})
        self.runtime.goal(self.project, "Experiment")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        directory = self.root / "experiment"
        directory.mkdir()
        observed = []
        def execute(*args, **kwargs):
            observed.append(args[5].name)
            return {"decision": "pass", "review": {"decision": "pass"}}
        with patch.object(self.runtime, "execute_candidate", side_effect=execute), \
                patch.object(Evolution, "split_decision", return_value={"decision": "pass"}), \
                patch.object(Evolution, "decide", return_value={"decision": "insufficient_evidence", "reason": "test only"}):
            result = evolution.propose_and_evaluate(self.runtime.project(self.project), self.repo, directory, run)
        trials = json.loads(self.store.one("SELECT evidence FROM candidates WHERE id=?", (result["candidate"],))["evidence"])["trials"]
        self.assertEqual([t["order"] for t in trials], [["stable", "candidate"], ["candidate", "stable"]] * 2)
        self.assertEqual(observed, [v for t in trials for v in t["order"]])

    def test_monitoring_schedules_only_when_due_and_stops_after_retention(self):
        _, _, run, _, _ = self.candidate()
        now = time.time()
        outcome = {"merged_sha": run["base"], "merged_at": now, "monitoring_completed": True}
        self.store.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (encode(outcome), run["id"]))
        schedule(self.runtime, now)
        self.assertFalse(self.store.query("SELECT * FROM tasks WHERE source='system_delivery'"))
        outcome.pop("monitoring_completed")
        outcome["feedback_polled_at"] = now
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        schedule(self.runtime, now + 61)
        self.assertFalse(self.store.query("SELECT * FROM tasks WHERE source='system_delivery'"))
        interval = self.runtime.config(self.project)["delivery"]["feedback_minutes"] * 60
        schedule(self.runtime, now + interval)
        job = self.store.one("SELECT * FROM tasks WHERE source='system_delivery'")
        self.assertEqual(job["state"], "queued")
        outcome["monitoring_completed"] = True
        outcome["feedback"] = {"investigation_pending": True}
        self.store.execute("UPDATE tasks SET state='completed' WHERE id=?", (job["id"],))
        self.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        schedule(self.runtime, now + interval + 61)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (job["id"],))["state"], "queued")
        outcome["feedback"] = {}
        outcome.pop("monitoring_completed")
        later = now + 31 * 86400
        with patch("adaptive_rsi.delivery.baseline") as fetch:
            feedback(self.runtime, self.runtime.project(self.project), run, outcome, self.runtime.config(self.project), later)
            fetch.assert_not_called()
        self.assertTrue(outcome["monitoring_completed"])
        self.store.execute("UPDATE tasks SET state='completed' WHERE id=?", (job["id"],))
        schedule(self.runtime, later + 61)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (job["id"],))["state"], "completed")
