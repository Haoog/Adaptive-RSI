import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, fingerprint
from adaptive_rsi.delivery import StaleCandidate, publish, tick
from adaptive_rsi.gitops import changes, git, snapshot
from adaptive_rsi.process import Cancelled
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_runtime import FakeAgent, fixture

from adaptive_rsi import release
from scripts import release as release_script


class ReleaseEndpointTest(unittest.TestCase):
    def test_repository_root_and_nonempty_paths_are_passed_exactly_to_gh(self):
        for endpoint in ["", "commits/main", "releases?per_page=100", "contents/.github/workflows/release.yml?ref=main"]:
            expected = "repos/example/project" + ("/" + endpoint if endpoint else "")
            with self.subTest(helper="script", endpoint=endpoint), patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/project"}), patch.object(release_script, "run", return_value='{"default_branch":"main"}') as command:
                self.assertEqual(release_script.gh(endpoint), {"default_branch": "main"})
                command.assert_called_once_with("gh", "api", expected, "--method", "GET")
            with self.subTest(helper="product", endpoint=endpoint), patch.object(release, "checked", return_value='{"default_branch":"main"}') as command:
                self.assertEqual(release.api("example/project", endpoint), {"default_branch": "main"})
                command.assert_called_once_with(["gh", "api", expected, "--method", "GET"])

    def test_nonempty_write_endpoint_retains_method_and_fields_without_executing(self):
        expected = ["gh", "api", "repos/example/project/git/refs", "--method", "POST", "-f", "ref=refs/tags/v1.0.0", "-f", "sha=" + "a" * 40]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "example/project"}), patch.object(release_script, "run", return_value="") as command:
            self.assertEqual(release_script.gh("git/refs", "POST", ref="refs/tags/v1.0.0", sha="a" * 40), {})
            command.assert_called_once_with(*expected)
        with patch.object(release, "checked", return_value="") as command:
            self.assertEqual(release.api("example/project", "git/refs", "POST", {"ref": "refs/tags/v1.0.0", "sha": "a" * 40}), {})
            command.assert_called_once_with(expected)


class DeliveryRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = fixture(self.root / "project")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.add_project(self.repo, enabled=True)
        self.runtime.configure({"github": {"repository": "example/project"},
                                "delivery": {"base_branch": "main", "mode": "pr"}}, self.project)
        task = self.runtime.goal(self.project, "Verified change")
        self.run = self.store.claim(self.project, self.runtime.config(self.project))
        base = git(self.repo, "rev-parse", "HEAD")
        self.store.execute("UPDATE runs SET base=? WHERE id=?", (base, self.run["id"]))
        self.store.transition(self.run["id"], "awaiting_approval",
                              {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        self.run = self.store.one("SELECT * FROM runs WHERE id=?", (self.run["id"],))
        self.task = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        self.identity = {"repository": "example/project", "branch": "main", "sha": base}
        self.store.put_setting("run-baseline:" + self.run["id"], self.identity)
        approval = self.runtime.deliver(self.run["id"], automatic=True)
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (approval,))
        self.approval = approval
        self.workspace = snapshot(self.repo, self.root / "candidate")
        (self.workspace / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        changes(self.workspace)
        git(self.workspace, "checkout", "-b", "feat/rsi-" + self.run["id"])
        git(self.workspace, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "candidate")
        self.head = git(self.workspace, "rev-parse", "HEAD")
        self.url = "https://github.com/example/project/pull/1"

    def publish(self):
        return publish(self.runtime, self.runtime.project(self.project), self.task, self.run,
                       self.workspace, json.loads(self.run["config"]))

    def test_lost_receipt_recovers_open_or_merged_pr_after_baseline_advances(self):
        for state in ["OPEN", "MERGED"]:
            self.store.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (self.run["id"],))
            self.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (self.task["id"],))
            with self.subTest(state=state), \
                    patch("adaptive_rsi.delivery.baseline", return_value=("unused", "new-base", "main")), \
                    patch("adaptive_rsi.delivery.checked", return_value=encode([{"url": self.url}])) as command, \
                    patch("adaptive_rsi.delivery.pr_view", return_value={"state": state, "headRefOid": self.head, "baseRefName": "main"}), \
                    patch("adaptive_rsi.delivery.github_git") as push:
                self.assertEqual(self.publish(), self.url)
                push.assert_not_called()
                self.assertEqual(len(command.call_args_list), 1)
                self.assertEqual(command.call_args.args[0][1:3], ["pr", "list"])
                self.assertEqual(self.store.setting("delivery:" + self.run["id"]),
                                 {"repository": "example/project", "branch": "main", "base": self.run["base"], "head": self.head})
                self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task["id"],))["state"], "pr_open")
                self.assertEqual(json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (self.run["id"],))["result"])["pr"], self.url)

    def recover_existing_pr_at_view_barrier(self, retry):
        outcome = json.loads(self.run["result"])
        outcome["patch_hash"] = fingerprint(git(self.workspace, "diff", self.run["base"], "--binary", "--no-ext-diff", "--no-textconv"))
        self.store.execute("UPDATE runs SET path=?,result=? WHERE id=?",
                           (str(self.workspace.parent), encode(outcome), self.run["id"]))
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.approval,))
        job = self.store.claim(self.project, self.runtime.config(self.project))
        arrived, released = threading.Event(), threading.Event()
        pr = {"state": "OPEN", "headRefOid": self.head, "baseRefName": "main"}
        def view(*args):
            arrived.set()
            self.assertTrue(released.wait(10), "view barrier was not released")
            return pr
        def recover(*args):
            url = publish(self.runtime, *args)
            # Assert durability before runtime.process receives the successful return.
            saved = self.store.one("SELECT state,result FROM runs WHERE id=?", (self.run["id"],))
            self.assertEqual(json.loads(saved["result"])["pr"], self.url)
            self.assertEqual(saved["state"], "superseded" if retry else "pr_open")
            self.assertEqual(self.store.setting("delivery:" + self.run["id"]),
                             {"repository": "example/project", "branch": "main", "base": self.run["base"], "head": self.head})
            return url
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), "advanced-base", "main")), \
                patch("adaptive_rsi.delivery.checked", return_value=encode([{"url": self.url}])) as command, \
                patch("adaptive_rsi.delivery.pr_view", side_effect=view), \
                patch("adaptive_rsi.delivery.github_git") as push, \
                patch.object(self.runtime, "publish", side_effect=recover), ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(self.runtime.process, job)
            try:
                self.assertTrue(arrived.wait(10), "worker did not reach view barrier: " + str(
                    self.store.one("SELECT state,result FROM runs WHERE id=?", (job["id"],))))
                if retry:
                    self.runtime.cancel_task(self.task["id"])
                    self.runtime.retry(self.task["id"])
            finally:
                released.set()
            worker.result(timeout=10)
        push.assert_not_called()
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0][1:3], ["pr", "list"])
        saved = self.store.one("SELECT state,result FROM runs WHERE id=?", (self.run["id"],))
        self.assertEqual(saved["state"], "superseded" if retry else "pr_open")
        self.assertEqual(json.loads(saved["result"])["pr"], self.url)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (job["id"],))["state"], "completed")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task["id"],))["state"],
                         "queued" if retry else "pr_open")
        if retry:
            self.assertTrue(json.loads(saved["result"])["close_pending"])
            self.store = self.runtime.store = Store(self.store.root)
            with patch("adaptive_rsi.delivery.pr_view", return_value=pr), \
                    patch("adaptive_rsi.delivery.checked", return_value="") as close:
                tick(self.runtime, project_id=self.project)
            close.assert_called_once_with(["gh", "pr", "close", self.url, "--repo", "example/project"])
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task["id"],))["state"], "queued")
            saved = self.store.one("SELECT state,result FROM runs WHERE id=?", (self.run["id"],))
            self.assertEqual(saved["state"], "superseded")
            self.assertFalse(json.loads(saved["result"])["close_pending"])

    def test_existing_pr_receipt_survives_retry_at_view_return_barrier_and_recovers_close(self):
        self.recover_existing_pr_at_view_barrier(retry=True)

    def test_existing_pr_normal_recovery_persists_receipt_before_return_without_remote_writes(self):
        self.recover_existing_pr_at_view_barrier(retry=False)

    def test_advanced_baseline_without_existing_pr_never_publishes(self):
        with patch("adaptive_rsi.delivery.baseline", return_value=("unused", "new-base", "main")), \
                patch("adaptive_rsi.delivery.checked", return_value="[]") as command, \
                patch("adaptive_rsi.delivery.github_git") as push:
            with self.assertRaises(StaleCandidate):
                self.publish()
            push.assert_not_called()
            self.assertEqual(command.call_count, 1)

    def test_existing_pr_requires_exact_head_and_sealed_repository_and_branch(self):
        for mismatch in ["head", "pr_branch", "missing_pr_branch", "sealed_repo", "sealed_branch", "sealed_base"]:
            identity = dict(self.identity)
            pr = {"state": "OPEN", "headRefOid": self.head, "baseRefName": "main"}
            if mismatch == "head":
                pr["headRefOid"] = "other-head"
            elif mismatch == "pr_branch":
                pr["baseRefName"] = "other-branch"
            elif mismatch == "missing_pr_branch":
                del pr["baseRefName"]
            else:
                identity[{"sealed_repo": "repository", "sealed_branch": "branch", "sealed_base": "sha"}[mismatch]] = "other"
            self.store.put_setting("run-baseline:" + self.run["id"], identity)
            with self.subTest(mismatch=mismatch), \
                    patch("adaptive_rsi.delivery.baseline", return_value=("unused", "new-base", "main")), \
                    patch("adaptive_rsi.delivery.checked", return_value=encode([{"url": self.url}])), \
                    patch("adaptive_rsi.delivery.pr_view", return_value=pr), \
                    patch("adaptive_rsi.delivery.github_git") as push:
                with self.assertRaises(RuntimeError):
                    self.publish()
                push.assert_not_called()
                self.assertIsNone(self.store.setting("delivery:" + self.run["id"]))

    def test_recovery_retains_verified_receipt_after_pause_cancel_revocation_or_supersession(self):
        for change in ["pause", "cancel", "revoke", "supersede"]:
            self.runtime.pause(self.project, False)
            self.runtime.configure({"delivery": {"mode": "pr"}}, self.project)
            self.store.execute("UPDATE runs SET state='awaiting_approval' WHERE id=?", (self.run["id"],))
            self.store.execute("UPDATE tasks SET state='awaiting_approval' WHERE id=?", (self.task["id"],))
            def view(*args):
                if change == "pause":
                    self.runtime.pause(self.project, True)
                elif change == "cancel":
                    self.runtime.cancel_task(self.task["id"])
                elif change == "revoke":
                    self.runtime.configure({"delivery": {"mode": "local"}}, self.project)
                else:
                    self.store.execute("UPDATE runs SET state='superseded' WHERE id=?", (self.run["id"],))
                return {"state": "OPEN", "headRefOid": self.head, "baseRefName": "main"}
            with self.subTest(change=change), \
                    patch("adaptive_rsi.delivery.baseline", return_value=("unused", "new-base", "main")), \
                    patch("adaptive_rsi.delivery.checked", return_value=encode([{"url": self.url}])), \
                    patch("adaptive_rsi.delivery.pr_view", side_effect=view), \
                    patch("adaptive_rsi.delivery.github_git") as push:
                self.assertEqual(self.publish(), self.url)
                push.assert_not_called()
                self.assertIsNotNone(self.store.setting("delivery:" + self.run["id"]))
                saved = self.store.one("SELECT state,result FROM runs WHERE id=?", (self.run["id"],))
                self.assertEqual(json.loads(saved["result"])["pr"], self.url)
                self.assertEqual(saved["state"], "superseded" if change == "supersede" else "pr_open")
                if change == "supersede":
                    self.assertTrue(json.loads(saved["result"])["close_pending"])
                if change == "cancel":
                    self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (self.task["id"],))["state"], "cancelled")


class ReleaseReconciliationRaceTest(unittest.TestCase):
    def test_tracking_cannot_erase_rerun_intent_for_release_deploy_or_rollback(self):
        for kind, rollback in [("release", False), ("deploy", False), ("deploy", True)]:
            with self.subTest(kind=kind, rollback=rollback), tempfile.TemporaryDirectory() as temp:
                store = Store(Path(temp) / "state")
                self.addCleanup(store.close)
                config = copy.deepcopy(DEFAULTS)
                policy = {**config["delivery"][kind], "enabled": True, "workflow": ".github/workflows/publish.yml",
                          "ref": "trusted", "sha": "a" * 40}
                if rollback:
                    policy["health_url"] = "https://example.test/health"
                config["delivery"].update(mode="pr", **{kind: policy})
                config["github"]["repository"] = "example/project"
                run = {"id": "run", "project": "project", "config": encode(config), "state": "merged",
                       "result": encode({"merged_sha": "b" * 40, "feedback": {"state": "verified", "head": "b" * 40}})}
                operation = {"state": "running", "repository": "example/project", "workflow": policy["workflow"],
                             "ref": "trusted", "workflow_sha": "a" * 40, "sha": "b" * 40,
                             "operation_id": "operation", "workflow_run_id": 17, "attempt": 1}
                if rollback:
                    operation.update(state="rolling_back", rollback={**operation, "operation_id": "operation-rollback", "workflow_run_id": 23})
                store.execute("INSERT INTO runs(id,project,state,config,result) VALUES (?,?,?,?,?)",
                              (run["id"], run["project"], run["state"], run["config"], run["result"]))
                store.put_setting(release.key("run", kind), operation)
                store.put_setting("delivery:run", {"head": "b" * 40, "base": "c" * 40,
                                                   "branch": "main", "repository": "example/project"})
                from types import SimpleNamespace
                operation_lock = threading.RLock()
                runtime = SimpleNamespace(store=store, dispatch_lock=threading.RLock(), stop=threading.Event(),
                                          automation_lock_for=lambda project, kind: operation_lock,
                                          wake=threading.Event(), config=lambda p: config,
                                          project=lambda p: {"id": "project", "path": "unused", "paused": False})
                target = operation["rollback"] if rollback else operation
                remote = {"id": target["workflow_run_id"], "head_sha": "a" * 40, "display_title": target["operation_id"],
                          "event": "workflow_dispatch", "path": policy["workflow"], "status": "completed",
                          "conclusion": "failure", "run_attempt": 1}
                snapshot_taken, respond = threading.Event(), threading.Event()
                posts = []
                tracker_thread = []
                def api(repo, endpoint, method="GET", fields=None):
                    if method == "POST":
                        posts.append(endpoint)
                        return {}
                    self.assertEqual(endpoint, "actions/runs/" + str(target["workflow_run_id"]))
                    if threading.get_ident() == tracker_thread[0]:
                        snapshot_taken.set()
                        if not respond.wait(5):
                            raise AssertionError("Tracking response barrier timed out")
                    return dict(remote)
                def track():
                    tracker_thread.append(threading.get_ident())
                    return release.advance(runtime, runtime.project("project"), run, json.loads(run["result"]))
                with patch.object(release, "api", side_effect=api), \
                        patch.object(release, "validate_pin"), \
                        patch.object(release, "origin_repository", return_value="example/project"), \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(track)
                    try:
                        self.assertTrue(snapshot_taken.wait(5))
                        if rollback:
                            failed = copy.deepcopy(operation)
                            failed.update(state="rollback_failed")
                            failed["rollback"]["state"] = "failed"
                            store.put_setting(release.key("run", kind), failed)
                        first = release.retry(runtime, "project", "run", kind)
                        pending = first["rollback"] if rollback else first
                        self.assertTrue(pending["rerun_pending"])
                    finally:
                        respond.set()
                    tracked = future.result(timeout=5)[kind]
                    saved = store.setting(release.key("run", kind))
                    pending = saved["rollback"] if rollback else saved
                    self.assertEqual(pending["state"], "rerunning")
                    self.assertTrue(pending["rerun_pending"])
                    self.assertEqual(tracked, saved)
                    persisted_run = store.one("SELECT result FROM runs WHERE id=?", (run["id"],))
                    self.assertEqual(json.loads(persisted_run["result"])["automation"][kind], saved)
                    repeated = release.retry(runtime, "project", "run", kind)
                    if rollback:
                        self.assertTrue(repeated["rollback"]["rerun_pending"])
                    self.assertEqual(posts, ["actions/runs/" + str(target["workflow_run_id"]) + "/rerun"])


class WorkflowChoicesFeedbackTest(unittest.TestCase):
    def test_origin_mismatch_is_a_safe_actionable_validation_error(self):
        import http.client
        from adaptive_rsi import server as server_module
        for message in ["Workflow repository must match the selected project's origin",
                        "Workflow must be an existing repository file"]:
            self.assertIn(message, server_module.SAFE_VALIDATION_MESSAGES)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime = Runtime(Store(root / "state"), FakeAgent())
            project = runtime.add_project(fixture(root / "project"), "工作流列表项目")
            runtime.configure({"github": {"repository": "example-owner/example-repo"}}, project)
            with self.assertRaises(ValueError) as caught:
                release.workflow_choices(runtime, project)
            self.assertEqual(str(caught.exception),
                             "Workflow repository must match the selected project's origin")
            server = server_module.Server(runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                try:
                    connection.request("GET", "/api/release-workflows?project=" + project,
                                       headers={"Cookie": server.cookie + "=" + server.token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                runtime.close()
            self.assertEqual(response.status, 400)
            self.assertEqual(payload, {"error": "Workflow repository must match the selected project's origin",
                                       "error_code": "validation_error"})


if __name__ == "__main__":
    unittest.main()
