"""Regression checks for isolation, revoked delivery and bounded background work."""
import json
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
        run = {"id": "review-run", "project": self.project,
               "config": encode(self.runtime.config(self.project)), "started": time.time()}

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
                                   [{"number": 91, "updated": "2026-10-20T10:00:00Z"}])
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
                                   [{"number": 73, "updated": "2026-10-20T10:00:00Z"}])
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
