import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
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
            self.store.close()
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
                self.assertEqual(saved["state"], "superseded" if change in ("supersede", "cancel") else "pr_open")
                if change in ("supersede", "cancel"):
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
                                          automation_receipt_failures={}, automation_receipt_failures_lock=threading.Lock(),
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

    def test_transient_inspection_failure_is_not_cached_and_recovers_on_refresh(self):
        import base64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo = fixture(root / "project")
            git(repo, "remote", "add", "origin", "https://github.com/example-owner/example-repo.git")
            runtime = Runtime(Store(root / "state"), FakeAgent())
            self.addCleanup(runtime.close)
            project = runtime.add_project(repo, "工作流测试项目")
            runtime.configure({"github": {"repository": "example-owner/example-repo"}}, project)
            release._workflow_choices_cache.clear()
            self.addCleanup(release._workflow_choices_cache.clear)

            valid_yaml = (
                "name: Deploy\n"
                "run-name: ${{ inputs.rsi_operation_id }}\n"
                "on:\n"
                "  workflow_dispatch:\n"
                "    inputs:\n"
                "      rsi_operation_id:\n"
                "        type: string\n"
                "        required: true\n"
                "      rsi_sha:\n"
                "        type: string\n"
                "        required: true\n"
            )
            b64_content = base64.b64encode(valid_yaml.encode("utf-8")).decode("ascii")

            transient_error = True

            def mock_api(repo_name, endpoint, method="GET", fields=None):
                if endpoint == "":
                    return {"default_branch": "main"}
                if endpoint.startswith("commits/"):
                    return {"sha": "c" * 40}
                if endpoint.startswith("actions/workflows"):
                    return {"workflows": [{"path": ".github/workflows/deploy.yml", "name": "Deploy", "state": "active"}]}
                if endpoint.startswith("contents/"):
                    if transient_error:
                        raise RuntimeError("HTTP 429 rate limit")
                    return {"encoding": "base64", "type": "file", "content": b64_content}
                raise ValueError(endpoint)

            with patch("adaptive_rsi.release.api", side_effect=mock_api):
                res1 = release.workflow_choices(runtime, project)
                self.assertFalse(res1["workflows"][0]["compatible"])
                self.assertEqual(res1["workflows"][0]["reason"], "Unable to inspect this workflow; refresh before authorizing")
                self.assertNotIn(("example-owner/example-repo", "c" * 40), release._workflow_choices_cache)

                transient_error = False
                res2 = release.workflow_choices(runtime, project)
                self.assertTrue(res2["workflows"][0]["compatible"])
                self.assertEqual(res2["workflows"][0]["reason"], "")
                self.assertIn(("example-owner/example-repo", "c" * 40), release._workflow_choices_cache)

                res3 = release.workflow_choices(runtime, project)
                self.assertIs(res3, res2)

                res4 = release.workflow_choices(runtime, project, refresh=True)
                self.assertTrue(res4["workflows"][0]["compatible"])
                self.assertIsNot(res4, res3)

    def test_authorize_workflow_penetrates_cache_with_fresh_validation(self):
        import base64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo = fixture(root / "project")
            git(repo, "remote", "add", "origin", "https://github.com/example-owner/example-repo.git")
            runtime = Runtime(Store(root / "state"), FakeAgent())
            self.addCleanup(runtime.close)
            project = runtime.add_project(repo, "工作流授权测试项目")
            runtime.configure({"github": {"repository": "example-owner/example-repo"}}, project)
            release._workflow_choices_cache.clear()
            self.addCleanup(release._workflow_choices_cache.clear)

            valid_yaml = (
                "name: Deploy\n"
                "run-name: ${{ inputs.rsi_operation_id }}\n"
                "on:\n"
                "  workflow_dispatch:\n"
                "    inputs:\n"
                "      rsi_operation_id:\n"
                "        type: string\n"
                "        required: true\n"
                "      rsi_sha:\n"
                "        type: string\n"
                "        required: true\n"
            )
            b64_content = base64.b64encode(valid_yaml.encode("utf-8")).decode("ascii")
            commit_sha = "e" * 40

            poisoned_result = {
                "repository": "example-owner/example-repo",
                "sha": commit_sha,
                "workflows": [{"workflow": ".github/workflows/deploy.yml", "name": "Deploy", "compatible": False,
                               "reason": "Unable to inspect this workflow; refresh before authorizing"}]
            }
            release._workflow_choices_cache[("example-owner/example-repo", commit_sha)] = (time.time(), poisoned_result)

            def mock_api(repo_name, endpoint, method="GET", fields=None):
                if endpoint == "":
                    return {"default_branch": "main"}
                if endpoint.startswith("commits/"):
                    return {"sha": commit_sha}
                if endpoint.startswith("actions/workflows"):
                    return {"workflows": [{"path": ".github/workflows/deploy.yml", "name": "Deploy", "state": "active"}]}
                if endpoint.startswith("contents/"):
                    return {"encoding": "base64", "type": "file", "content": b64_content}
                if endpoint.startswith("git/matching-refs/"):
                    return []
                if endpoint == "git/refs":
                    return {}
                raise ValueError(endpoint)

            with patch("adaptive_rsi.release.api", side_effect=mock_api):
                target = release.authorize_workflow(runtime, project, "release", ".github/workflows/deploy.yml")
                self.assertEqual(target["workflow"], ".github/workflows/deploy.yml")
                self.assertEqual(target["sha"], commit_sha)
                self.assertEqual(target["ref"], "rsi-workflow/" + commit_sha)
                self.assertTrue(target["enabled"])


class _ManualReconciliationFixture(unittest.TestCase):
    """Shared real-runtime fixture for manual reconciliation tests."""

    TRUSTED = "a" * 40
    MERGED = "b" * 40
    WORKFLOW = ".github/workflows/publish.yml"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        repo = fixture(root / "project")
        git(repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime = Runtime(Store(root / "state"), FakeAgent())
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.add_project(repo, enabled=True)
        release_policy = {**DEFAULTS["delivery"]["release"], "enabled": True, "workflow": self.WORKFLOW,
                          "ref": "trusted-v1", "sha": self.TRUSTED}
        deploy_policy = {**DEFAULTS["delivery"]["deploy"], "enabled": True, "workflow": self.WORKFLOW,
                         "ref": "trusted-v1", "sha": self.TRUSTED, "automatic_rollback": False}
        self.runtime.configure({"github": {"repository": "example/project"},
                                "delivery": {"mode": "pr", "release": release_policy, "deploy": deploy_policy}},
                               self.project)
        task = self.runtime.goal(self.project, "Manual reconciliation")
        run = self.runtime.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.store.execute(
            "UPDATE runs SET state='merged', result=? WHERE id=?",
            (encode({"merged_sha": self.MERGED, "feedback": {"state": "verified", "head": self.MERGED}}), run["id"]))
        self.record = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        assert self.record is not None
        self.posts = []

    def operation(self, kind="release", state="dispatch_unknown", **extra):
        policy = json.loads(self.record["config"])["delivery"][kind]
        data = {"state": state, "repository": "example/project", "sha": self.MERGED,
                "workflow": policy["workflow"], "ref": policy["ref"], "workflow_sha": policy["sha"],
                "operation_id": "rsi-operation", "created_at": time.time(), "attempt": 0,
                "dispatch_receipt": "unknown", "error": "GitHub did not confirm the dispatch"}
        data.update(extra)
        return release.save(self.runtime, self.record, kind, data)

    def remote(self, run_id=17, **overrides):
        data = {"id": run_id, "head_sha": self.TRUSTED, "head_branch": "trusted-v1",
                "display_title": "rsi-operation", "event": "workflow_dispatch",
                "path": self.WORKFLOW + "@refs/tags/trusted-v1", "status": "queued", "conclusion": None,
                "run_attempt": 1, "created_at": "2026-10-30T00:00:00Z", "updated_at": "2026-10-30T00:00:00Z"}
        data.update(overrides)
        return data

    def api(self, runs=None, single=None):
        def handler(repo, endpoint, method="GET", fields=None):
            self.assertEqual(repo, "example/project")
            if method == "POST":
                self.posts.append((endpoint, fields))
                return {}
            if endpoint.startswith("actions/runs/"):
                run_id = int(endpoint.rsplit("/", 1)[1])
                if single is not None:
                    return single
                match = next((item for item in runs or [] if item.get("id") == run_id), None)
                if match is not None:
                    return match
                raise AssertionError("unexpected single-run read")
            if "/runs?" in endpoint:
                items = list(runs or [])
                if "page=" in endpoint:
                    import urllib.parse
                    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)
                    page = int(qs.get("page", [1])[0])
                    per_page = int(qs.get("per_page", [100])[0])
                    start = (page - 1) * per_page
                    return {"workflow_runs": items[start:start + per_page]}
                return {"workflow_runs": items}
            raise AssertionError(endpoint)
        return handler

    def saved(self, kind="release"):
        return self.runtime.store.setting(release.key(self.record["id"], kind))

    def stored_size(self, kind="release"):
        row = self.runtime.store.one("SELECT value FROM settings WHERE key=?",
                                     (release.key(self.record["id"], kind),))
        return len(row["value"])


class ManualReconciliationTest(_ManualReconciliationFixture):
    """Manual association may only bind an exact run returned by a read-only query."""

    def test_rollback_rerun_resolution_resumes_parent_tracking(self):
        self.operation(state="workflow_succeeded")
        for status, conclusion in [("queued", None), ("in_progress", None), ("completed", "success"),
                                   ("completed", "failure"), ("completed", "cancelled")]:
            with self.subTest(status=status):
                rollback = {**self.operation(kind="deploy"), "state": "rerun_unknown",
                            "workflow_run_id": 17, "attempt": 1, "previous_attempt": 1,
                            "rerun_pending": True, "rerun_requested_at": time.time() - 400}
                self.operation(kind="deploy", state="rollback_failed", rollback=rollback, error="Previous attempt was unconfirmed")
                self.assertFalse(release.pending(self.runtime, self.record))
                with patch.object(release, "api", side_effect=self.api(single=self.remote(
                        17, run_attempt=2, status=status, conclusion=conclusion))):
                    release.resolve(self.runtime, self.project, self.record["id"], "deploy", 17, target="rollback")
                saved = self.saved("deploy")
                resumed = conclusion not in {"failure", "cancelled"}
                self.assertEqual(saved["state"], "rolling_back" if resumed else "rollback_failed")
                self.assertEqual(release.pending(self.runtime, self.record), resumed)
                if resumed:
                    self.assertEqual(saved["error"], "")
                self.assertEqual(self.posts, [])

    def test_multiple_exact_candidates_bind_only_the_explicit_selection(self):
        remotes = [self.remote(101), self.remote(102, status="in_progress")]
        for selected, expected in [(101, "queued"), (102, "running")]:
            with self.subTest(selected=selected):
                self.operation()
                with patch.object(release, "api", side_effect=self.api(runs=remotes)):
                    view = release.candidates(self.runtime, self.project, self.record["id"], "release")
                    self.assertIsNone(self.saved().get("workflow_run_id"))
                    self.assertEqual(view.get("reconciliation_state"), "manual_inspection_required")
                    result = release.resolve(self.runtime, self.project, self.record["id"], "release", selected)
                self.assertEqual(result["state"], expected)
                saved = self.saved()
                self.assertEqual(saved["workflow_run_id"], selected)
                self.assertEqual([item["id"] for item in saved["manual_reconciliation"]["candidates"]], [101, 102])
                self.assertEqual(saved["manual_reconciliation"]["previous"]["candidate_run_ids"], [101, 102])
                self.assertEqual(self.posts, [])

    def test_candidates_list_fixed_links_and_explain_absence_without_free_text(self):
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(101), self.remote(102)])):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
        self.assertEqual([item["id"] for item in view["candidates"]], [101, 102])
        self.assertEqual(view["candidates"][0]["url"], "https://github.com/example/project/actions/runs/101")
        self.assertEqual(view["state"], "dispatch_unknown")
        saved = self.saved()
        self.assertEqual(saved["reconciliation"]["state"], "manual_inspection_required")
        self.assertFalse(saved["reconciliation"]["redispatch_allowed"])
        self.assertEqual(saved["state"], "dispatch_unknown")
        self.assertEqual(self.posts, [])

        with patch.object(release, "api", side_effect=self.api(runs=[])):
            empty = release.candidates(self.runtime, self.project, self.record["id"], "release")
        self.assertEqual(empty["candidates"], [])
        self.assertEqual(empty["state"], "dispatch_unknown")
        self.assertIn("manual run IDs are not accepted", empty["reason"])
        self.assertIn("awaiting_remote_visibility", self.saved()["reconciliation"]["state"])
        self.assertEqual(self.posts, [])

    def test_selection_rejects_ids_outside_the_server_query(self):
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17)])):
            with self.assertRaisesRegex(release.ReconciliationRejected, "does not match this operation"):
                release.resolve(self.runtime, self.project, self.record["id"], "release", 99)
        saved = self.saved()
        self.assertEqual(saved["state"], "dispatch_unknown")
        self.assertIsNone(saved.get("workflow_run_id"))
        self.assertEqual(self.posts, [])

    def test_selection_rejects_every_identity_mismatch(self):
        self.operation()
        mismatches = [
            {"head_sha": "c" * 40},
            {"head_branch": "other-ref"},
            {"display_title": "other-operation"},
            {"event": "push"},
            {"path": ".github/workflows/other.yml@refs/tags/trusted-v1"},
        ]
        for overrides in mismatches:
            with self.subTest(overrides=overrides), \
                    patch.object(release, "api", side_effect=self.api(runs=[self.remote(17, **overrides)])):
                with self.assertRaisesRegex(release.ReconciliationRejected, "does not match this operation"):
                    release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
                self.assertEqual(self.saved()["state"], "dispatch_unknown")
        self.assertEqual(self.posts, [])

    def test_exact_selection_binds_read_only_and_persists_raw_audit(self):
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17, status="in_progress")])):
            release.candidates(self.runtime, self.project, self.record["id"], "release")
            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 17, source="test")
        saved = self.saved()
        self.assertEqual(result["state"], "running")
        self.assertEqual(saved["state"], "running")
        self.assertEqual(saved["workflow_run_id"], 17)
        self.assertEqual(saved["reconciliation"]["state"], "matched")
        self.assertFalse(saved["reconciliation"]["redispatch_allowed"])
        audit = saved["manual_reconciliation"]
        self.assertEqual(audit["source"], "test")
        self.assertEqual(audit["selected_run_id"], 17)
        self.assertEqual(audit["previous"]["state"], "dispatch_unknown")
        self.assertEqual(audit["previous"]["candidate_run_ids"], [17])
        self.assertEqual(audit["candidates"][0]["id"], 17)
        self.assertEqual(audit["candidates"][0]["url"], "https://github.com/example/project/actions/runs/17")
        self.assertGreater(audit["at"], 0)
        self.assertEqual(saved["manual_reconciliations"][-1]["selected_run_id"], 17)
        events = self.runtime.store.query("SELECT kind FROM events WHERE run=?", (self.record["id"],))
        self.assertIn("automation_reconciled", [row["kind"] for row in events])
        self.assertEqual(self.posts, [])

    def test_selection_never_turns_queued_evidence_into_success(self):
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17, status="queued", conclusion=None)])):
            release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        saved = self.saved()
        self.assertEqual(saved["state"], "queued")
        self.assertNotEqual(saved["state"], "workflow_succeeded")
        self.assertEqual(self.posts, [])

    def test_resolve_missing_run_persists_diagnostic_without_success_audit(self):
        for late in (False, True):
            with self.subTest(late=late):
                self.operation(state='rerun_unknown', workflow_run_id=17, attempt=1, previous_attempt=1,
                               rerun_pending=True, rerun_requested_at=time.time() - 400)
                lookup = self.api(single=self.remote(17, run_attempt=2, status='in_progress'))
                calls = []
                def unavailable(repo, endpoint, method='GET', fields=None):
                    if endpoint == 'actions/runs/17':
                        calls.append(endpoint)
                        if not late or len(calls) == 2:
                            raise release.ApiRejected('fixture missing', 404)
                    return lookup(repo, endpoint, method, fields)
                with patch.object(release, 'api', side_effect=unavailable):
                    with self.assertRaisesRegex(release.ReconciliationRejected, 'not visible'):
                        release.resolve(self.runtime, self.project, self.record['id'], 'release', 17)
                saved = self.saved()
                self.assertEqual(saved['remote_visibility']['state'], 'run_not_visible')
                self.assertEqual(saved['workflow_run_id'], 17)
                self.assertNotIn('manual_reconciliation', saved)

    def test_missing_workflow_listing_reports_visibility_without_server_error(self):
        self.operation()
        def unavailable(repo, endpoint, method='GET', fields=None):
            raise release.ApiRejected('fixture missing', 404)
        with patch.object(release, 'api', side_effect=unavailable):
            view = release.candidates(self.runtime, self.project, self.record['id'], 'release')
            self.assertEqual(view['reconciliation_state'], 'workflow_not_visible')
            self.assertEqual(view['candidates'], [])
            self.assertIn('not visible', view['reason'])
            saved = self.saved()
            self.assertEqual(saved['state'], 'dispatch_unknown')
            self.assertEqual(saved['remote_visibility']['state'], 'workflow_not_visible')
            self.assertEqual(saved['remote_visibility']['http_status'], 404)
            self.assertEqual(saved['reconciliation']['state'], 'workflow_not_visible')
            with self.assertRaisesRegex(release.ReconciliationRejected, 'not visible'):
                release.resolve(self.runtime, self.project, self.record['id'], 'release', 17)
            self.assertEqual(self.saved()['state'], 'dispatch_unknown')
            self.assertNotIn('manual_reconciliation', self.saved())
        # 工作流文件恢复可见后，同一记录可正常完成人工对账。
        with patch.object(release, 'api', side_effect=self.api(runs=[self.remote(17, status='in_progress')])):
            view = release.candidates(self.runtime, self.project, self.record['id'], 'release')
            self.assertEqual(view['reconciliation_state'], 'matched')
            self.assertNotIn('remote_visibility', self.saved())
            result = release.resolve(self.runtime, self.project, self.record['id'], 'release', 17)
            self.assertEqual(result['state'], 'running')
        self.assertEqual(self.posts, [])

    def test_candidates_refuse_stale_choices_after_receipt_changes(self):
        self.operation()
        lookup = self.api(runs=[self.remote(17)])
        def changed(repo, endpoint, method='GET', fields=None):
            result = lookup(repo, endpoint, method, fields)
            self.runtime.store.put_setting(release.key(self.record['id'], 'release'), {'state': 'failed'})
            return result
        with patch.object(release, 'api', side_effect=changed):
            with self.assertRaisesRegex(release.ReconciliationRejected, 'changed'):
                release.candidates(self.runtime, self.project, self.record['id'], 'release')

    def test_reconciliation_preserves_noncanonical_receipt_cas(self):
        self.operation(state='rerun_unknown', workflow_run_id=17, attempt=1, previous_attempt=1,
                       rerun_pending=True, rerun_requested_at=time.time() - 400)
        name = release.key(self.record['id'], 'release')
        self.runtime.store.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(self.saved(), indent=2), name))
        with patch.object(release, 'api', side_effect=self.api(single=self.remote(17, run_attempt=2, status='in_progress'))):
            release.candidates(self.runtime, self.project, self.record['id'], 'release')
        self.assertEqual(self.saved()['reconciliation']['state'], 'rerun_confirmed')
        self.runtime.store.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(self.saved(), indent=2), name))
        with patch.object(release, 'api', side_effect=self.api(single=self.remote(17, run_attempt=2, status='in_progress'))):
            release.resolve(self.runtime, self.project, self.record['id'], 'release', 17)
        self.assertEqual(self.saved()['state'], 'running')

    def test_rerun_unknown_requires_a_confirmed_new_attempt(self):
        self.operation(state="rerun_unknown", workflow_run_id=17, attempt=1, previous_attempt=1,
                       rerun_pending=True, rerun_requested_at=time.time() - 400)
        with patch.object(release, "api", side_effect=self.api(single=self.remote(17, run_attempt=1))):
            with self.assertRaisesRegex(release.ReconciliationRejected, "has not confirmed a new attempt"):
                release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        self.assertEqual(self.saved()["state"], "rerun_unknown")

        with patch.object(release, "api", side_effect=self.api(single=self.remote(17, run_attempt=2, status="in_progress"))):
            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        saved = self.saved()
        self.assertEqual(result["state"], "running")
        self.assertEqual(saved["state"], "running")
        self.assertFalse(saved["rerun_pending"])
        self.assertEqual(saved["rerun_receipt"], "confirmed")
        self.assertEqual(saved["manual_reconciliation"]["previous"]["state"], "rerun_unknown")
        self.assertEqual(self.posts, [])

    def test_manual_selection_keeps_authorization_and_deployment_ownership_checks(self):
        self.operation()
        self.runtime.configure({"delivery": {"release": {"enabled": False}}}, self.project)
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17)])):
            with self.assertRaisesRegex(release.ReconciliationRejected, "authorization"):
                release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        self.assertEqual(self.saved()["state"], "dispatch_unknown")

        self.runtime.configure({"delivery": {"release": {"enabled": True}}}, self.project)
        self.operation(kind="deploy")
        policy = self.runtime.config(self.project)["delivery"]["deploy"]
        self.runtime.store.put_setting(release.owner_key(self.project, "example/project", policy),
                                       {"run": "another-run", "sha": self.MERGED})
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17)])):
            with self.assertRaisesRegex(release.ReconciliationRejected, "newer deployment owns this target"):
                release.resolve(self.runtime, self.project, self.record["id"], "deploy", 17)
        self.assertEqual(self.saved("deploy")["state"], "dispatch_unknown")
        self.assertEqual(self.posts, [])

    def test_delayed_remote_visibility_transition_and_resolution_flow(self):
        self.operation()
        # 1. Dispatch just submitted/unknown: remote run has not appeared yet (empty runs).
        with patch.object(release, "api", side_effect=self.api(runs=[])):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
            self.assertEqual(view["reconciliation_state"], "awaiting_remote_visibility")
            self.assertEqual(view["candidates"], [])
            saved = self.saved()
            self.assertEqual(saved["state"], "dispatch_unknown")
            self.assertEqual(saved["reconciliation"]["state"], "awaiting_remote_visibility")
            self.assertEqual(saved["reconciliation"]["candidate_run_ids"], [])
            self.assertFalse(saved["reconciliation"]["redispatch_allowed"])
            # Zero redispatch sent.
            self.assertEqual(self.posts, [])

            # Selecting before visibility is rejected; manual IDs are rejected.
            with self.assertRaisesRegex(release.ReconciliationRejected, "does not match this operation"):
                release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
            self.assertEqual(self.posts, [])

        # 2. Delayed appearance: workflow run becomes visible on GitHub.
        remote_run = self.remote(17, status="in_progress")
        with patch.object(release, "api", side_effect=self.api(runs=[remote_run])):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
            self.assertEqual(view["reconciliation_state"], "matched")
            self.assertEqual([c["id"] for c in view["candidates"]], [17])
            saved = self.saved()
            self.assertEqual(saved["reconciliation"]["state"], "matched")
            self.assertEqual(saved["reconciliation"]["candidate_run_ids"], [17])
            self.assertFalse(saved["reconciliation"]["redispatch_allowed"])
            self.assertEqual(self.posts, [])

            # 3. Operator resolves the delayed matching run.
            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 17, source="test_delayed")
            self.assertEqual(result["state"], "running")
            saved = self.saved()
            self.assertEqual(saved["state"], "running")
            self.assertEqual(saved["workflow_run_id"], 17)
            self.assertEqual(saved["reconciliation"]["state"], "matched")
            self.assertFalse(saved["reconciliation"]["redispatch_allowed"])

            audit = saved["manual_reconciliation"]
            self.assertEqual(audit["selected_run_id"], 17)
            self.assertEqual(audit["source"], "test_delayed")
            self.assertEqual(audit["previous"]["state"], "dispatch_unknown")
            self.assertEqual(audit["previous"]["reconciliation_state"], "matched")
            self.assertEqual(audit["previous"]["candidate_run_ids"], [17])
            self.assertEqual(audit["candidates"][0]["id"], 17)
            self.assertGreater(audit["at"], 0)
            self.assertEqual(saved["manual_reconciliations"][-1]["selected_run_id"], 17)

            events = self.runtime.store.query("SELECT kind, data FROM events WHERE run=?", (self.record["id"],))
            reconciled_events = [e for e in events if e["kind"] == "automation_reconciled"]
            self.assertEqual(len(reconciled_events), 1)

            # Never redispatches.
            self.assertEqual(self.posts, [])

    def test_delayed_remote_visibility_direct_resolution_captures_visibility_audit(self):
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=[])):
            release.candidates(self.runtime, self.project, self.record["id"], "release")
        self.assertEqual(self.saved()["reconciliation"]["state"], "awaiting_remote_visibility")

        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17, status="in_progress")])):
            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 17, source="test_direct")
        self.assertEqual(result["state"], "running")
        saved = self.saved()
        self.assertEqual(saved["manual_reconciliation"]["previous"]["reconciliation_state"], "awaiting_remote_visibility")
        self.assertEqual(saved["manual_reconciliation"]["previous"]["candidate_run_ids"], [])
        self.assertEqual(saved["manual_reconciliation"]["selected_run_id"], 17)
        self.assertEqual(self.posts, [])

    def test_reconcile_persists_delayed_remote_visibility_sequence(self):
        self.operation()
        # Programmatic reconcile when remote has not appeared yet.
        with patch.object(release, "api", side_effect=self.api(runs=[])):
            result = release.reconcile(self.runtime, self.record, "release", self.saved(), persist=True)
            self.assertEqual(result["state"], "dispatch_unknown")
            saved = self.saved()
            self.assertEqual(saved["reconciliation"]["state"], "awaiting_remote_visibility")
            self.assertEqual(saved["reconciliation"]["candidate_run_ids"], [])
            self.assertFalse(saved["reconciliation"]["redispatch_allowed"])
            self.assertEqual(self.posts, [])

        # Delayed appearance: remote now visible.
        remote_run = self.remote(17, status="in_progress")
        with patch.object(release, "api", side_effect=self.api(runs=[remote_run])):
            result = release.reconcile(self.runtime, self.record, "release", self.saved(), persist=True)
            self.assertEqual(result["state"], "running")
            saved = self.saved()
            self.assertEqual(saved["workflow_run_id"], 17)
            self.assertEqual(saved["state"], "running")
            self.assertEqual(saved["reconciliation"]["state"], "matched")
            self.assertEqual(saved["reconciliation"]["candidate_run_ids"], [17])
            self.assertEqual(self.posts, [])

    def test_retry_network_timeout_sets_rerun_unknown_and_enables_immediate_reconciliation(self):
        self.operation(kind="release", state="failed", workflow_run_id=17, attempt=1,
                       dispatch_receipt="accepted", error="Previous run failed")
        failed_remote = self.remote(17, run_attempt=1, status="completed", conclusion="failure")
        base_api = self.api(single=failed_remote)

        def retry_api(repo, endpoint, method="GET", fields=None):
            if method == "POST" and endpoint == "actions/runs/17/rerun":
                raise RuntimeError("ReadTimeout: HTTPSConnectionPool(host='api.github.com', port=443): Read timed out.")
            return base_api(repo, endpoint, method, fields)

        with patch.object(release, "api", side_effect=retry_api), \
                patch.object(release, "validate_pin"):
            with self.assertRaises(RuntimeError) as ctx:
                release.retry(self.runtime, self.project, self.record["id"], "release")
            self.assertIn("Read timed out", str(ctx.exception))

        saved = self.saved("release")
        self.assertEqual(saved["state"], "rerun_unknown")
        self.assertEqual(saved["rerun_receipt"], "unknown")
        self.assertTrue(saved.get("rerun_pending"))
        self.assertNotIn("rollback", saved)
        self.assertNotEqual(saved["state"], "rolling_back")

        with patch.object(release, "api", side_effect=self.api(single=failed_remote)):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
        self.assertEqual(view["state"], "rerun_unknown")
        self.assertEqual(view["reconciliation_state"], "rerun_unconfirmed")
        self.assertEqual([c["id"] for c in view["candidates"]], [17])

        confirmed_run = self.remote(17, run_attempt=2, status="in_progress")
        with patch.object(release, "api", side_effect=self.api(single=confirmed_run)):
            resolved = release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        self.assertEqual(resolved["state"], "running")
        saved_after = self.saved("release")
        self.assertEqual(saved_after["state"], "running")
        self.assertEqual(saved_after["attempt"], 2)

    def test_release_retry_never_introduces_rolling_back_even_with_new_dict_reference(self):
        self.operation(kind="release", state="failed", workflow_run_id=17, attempt=1,
                       dispatch_receipt="accepted", error="Previous run failed")
        failed_remote = self.remote(17, run_attempt=1, status="completed", conclusion="failure")
        base_api = self.api(single=failed_remote)

        def retry_api(repo, endpoint, method="GET", fields=None):
            if method == "POST" and endpoint == "actions/runs/17/rerun":
                raise RuntimeError("Connection reset by peer")
            return base_api(repo, endpoint, method, fields)

        orig_reconcile = release.reconcile
        def copy_reconcile(runtime, run, kind, data, persist=True):
            return dict(orig_reconcile(runtime, run, kind, data, persist=persist))

        with patch.object(release, "reconcile", side_effect=copy_reconcile), \
                patch.object(release, "api", side_effect=retry_api), \
                patch.object(release, "validate_pin"):
            with self.assertRaises(RuntimeError):
                release.retry(self.runtime, self.project, self.record["id"], "release")

        saved = self.saved("release")
        self.assertEqual(saved["state"], "rerun_unknown")
        self.assertEqual(saved["rerun_receipt"], "unknown")
        self.assertNotIn("rollback", saved)
        self.assertNotEqual(saved["state"], "rolling_back")

    def test_deploy_rollback_retry_network_timeout_sets_rerun_unknown_and_enables_reconciliation(self):
        rollback_data = {
            **self.operation(kind="deploy"),
            "sha": self.TRUSTED,
            "operation_id": "rsi-operation-rollback",
            "state": "failed",
            "workflow_run_id": 17,
            "attempt": 1,
            "dispatch_receipt": "accepted",
            "error": "Rollback failed",
        }
        self.operation(kind="deploy", state="rollback_failed", rollback=rollback_data,
                       error="Rollback failed")
        failed_remote = self.remote(17, run_attempt=1, status="completed", conclusion="failure",
                                    display_title="rsi-operation-rollback")
        base_api = self.api(single=failed_remote)

        def retry_api(repo, endpoint, method="GET", fields=None):
            if method == "POST" and endpoint == "actions/runs/17/rerun":
                raise RuntimeError("ReadTimeout: Read timed out.")
            return base_api(repo, endpoint, method, fields)

        with patch.object(release, "api", side_effect=retry_api), \
                patch.object(release, "validate_pin"):
            with self.assertRaises(RuntimeError):
                release.retry(self.runtime, self.project, self.record["id"], "deploy")

        saved = self.saved("deploy")
        self.assertEqual(saved["state"], "rollback_failed")
        self.assertEqual(saved["rollback"]["state"], "rerun_unknown")
        self.assertEqual(saved["rollback"]["rerun_receipt"], "unknown")
        self.assertTrue(saved["rollback"].get("rerun_pending"))

        with patch.object(release, "api", side_effect=self.api(single=failed_remote)):
            view = release.candidates(self.runtime, self.project, self.record["id"], "deploy", target="rollback")
        self.assertEqual(view["state"], "rerun_unknown")
        self.assertEqual(view["target"], "rollback")
        self.assertEqual(view["reconciliation_state"], "rerun_unconfirmed")

        confirmed_run = self.remote(17, run_attempt=2, status="in_progress",
                                    display_title="rsi-operation-rollback")
        with patch.object(release, "api", side_effect=self.api(single=confirmed_run)):
            resolved = release.resolve(self.runtime, self.project, self.record["id"], "deploy", 17, target="rollback")
        self.assertEqual(resolved["state"], "running")
        saved_after = self.saved("deploy")
        self.assertEqual(saved_after["state"], "rolling_back")
        self.assertEqual(saved_after["rollback"]["state"], "running")
        self.assertEqual(saved_after["rollback"]["attempt"], 2)


class ReleaseRetryEndpointTest(_ManualReconciliationFixture):
    """Every fixed retry rejection must reach the console verbatim, not Invalid request."""

    def setUp(self):
        super().setUp()
        from adaptive_rsi import server as server_module
        self.server = server_module.Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)

    def post_retry(self, kind="release", run=None):
        import http.client
        body = json.dumps({"project": self.project, "run": run or self.record["id"],
                           "kind": kind}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/release-retry", body, {
                "Cookie": self.server.cookie + "=" + self.server.token,
                "Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf,
                "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_rejected(self, message, kind="release", run=None, error_code="validation_error"):
        status, payload = self.post_retry(kind, run)
        self.assertEqual(status, 400)
        expected = {"error": message}
        if error_code is not None:
            expected["error_code"] = error_code
        self.assertEqual(payload, expected)

    def test_every_reachable_rejection_surfaces_the_release_message(self):
        # kind 白名单外：固定 kind 校验文案，不是 Invalid request。
        self.assert_rejected("Unknown automation kind", kind="bogus")
        # 目标 run 未合并：跨项目或缺失 run 走同一固定文案。
        self.assert_rejected("Only a verified merged run in this project can be retried", run="missing-run")

        # blocked 记录：要求恢复原授权让跟踪继续，不是模糊失败。
        self.operation(state="blocked")
        self.assert_rejected("Restore the original authorization and let tracking retry the pending operation")
        # 无 operation_id：没有可重试的 GitHub workflow。
        self.operation(state="failed", operation_id=None)
        self.assert_rejected("This operation has no failed GitHub workflow to retry")

        # 回滚没有已知健康目标证据：merge 证据不能替代 known-healthy 回滚契约。
        deploy_policy = json.loads(self.record["config"])["delivery"]["deploy"]
        rollback = {"state": "workflow_succeeded", "sha": "c" * 40,
                    "repository": "example/project", "workflow": deploy_policy["workflow"],
                    "ref": deploy_policy["ref"], "workflow_sha": deploy_policy["sha"],
                    "operation_id": "rsi-rollback", "attempt": 1}
        self.operation(kind="deploy", state="rollback_failed", rollback=rollback, sha="b" * 40)
        self.assert_rejected("Rollback retry requires known healthy target evidence", kind="deploy")

        # 部署目标被其他 run 持有：stale delivery 拒绝。
        policy = self.runtime.config(self.project)["delivery"]["deploy"]
        self.runtime.store.put_setting(release.owner_key(self.project, "example/project", policy),
                                       {"run": "other-run", "sha": self.MERGED})
        self.assert_rejected("A newer deployment owns this target; stale delivery cannot be retried",
                             kind="deploy")
        self.runtime.store.execute("DELETE FROM settings WHERE key=?",
                                   (release.owner_key(self.project, "example/project", policy),))

        # 损坏的自动化回执：ReconciliationRejected 也必须带出原文。
        self.runtime.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                                   (release.key(self.record["id"], "release"), "{broken"))
        self.assert_rejected("Automation record is unreadable; repair the stored record before continuing",
                             error_code=None)

    def test_release_status_reports_missing_run_with_fixed_message(self):
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/release-status?project=" + self.project + "&run=missing",
                               headers={"Cookie": self.server.cookie + "=" + self.server.token})
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(payload, {"error": "Run not found in this project",
                                   "error_code": "validation_error"})


class ReconciliationAuditPayloadTest(_ManualReconciliationFixture):
    """Reconciliation keeps bounded evidence instead of the raw GitHub page."""

    FIELDS = {"id", "url", "status", "conclusion", "attempt", "created_at", "updated_at",
              "head_sha", "head_branch", "display_title", "workflow", "path", "event"}
    BUDGET = 64 * 1024

    def page(self, count=99):
        # A real page carries actor, repository and commit objects. Nothing in
        # them is needed to re-check one selection, so they must not persist.
        # Pages stay below 100 runs because the stub repeats one page for every
        # page number, which would otherwise extend the read-only query tenfold.
        padding = "x" * 4096
        return [self.remote(1000 + index,
                            actor={"login": "operator", "avatar_url": "https://avatars.example/" + padding},
                            repository={"full_name": "example/project", "description": padding},
                            head_commit={"id": "c" * 40, "message": padding})
                for index in range(count)]

    def test_large_page_stores_bounded_evidence_and_keeps_the_audit(self):
        remotes = self.page(99)
        self.assertGreater(len(encode(remotes)), 1_000_000)
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=remotes)):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 1042)
        self.assertEqual(view["state"], "dispatch_unknown")
        self.assertEqual(view["candidate_count"], 99)
        self.assertEqual(len(view["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
        self.assertEqual(len(view["selectable_candidates"]), 99)
        self.assertEqual(view["selectable_candidates"][42]["id"], 1042)
        self.assertEqual(result["state"], "queued")
        saved = self.saved()
        self.assertEqual(saved["state"], "queued")
        audit = saved["manual_reconciliation"]
        self.assertEqual(set(audit), set(release.RECONCILIATION_AUDIT_FIELDS))
        self.assertEqual(audit["candidate_count"], 99)
        self.assertEqual(audit["selected_run_id"], 1042)
        self.assertEqual([item["id"] for item in audit["candidates"]],
                         [1000 + index for index in range(release.RECONCILIATION_CANDIDATE_LIMIT)])
        first = audit["candidates"][0]
        self.assertEqual(set(first), self.FIELDS)
        self.assertEqual((first["id"], first["status"]), (1000, "queued"))
        self.assertEqual((first["created_at"], first["updated_at"]),
                         ("2026-10-30T00:00:00Z", "2026-10-30T00:00:00Z"))
        self.assertEqual(first["url"], "https://github.com/example/project/actions/runs/1000")
        self.assertEqual(first["head_sha"], self.TRUSTED)
        self.assertEqual(saved["reconciliation"]["candidate_count"], 1)
        self.assertLess(self.stored_size(), self.BUDGET)
        self.assertEqual(self.posts, [])

    def test_stored_size_does_not_follow_the_remote_page_size(self):
        sizes = []
        for count in (20, 99):
            self.operation()
            with patch.object(release, "api", side_effect=self.api(runs=self.page(count))):
                release.candidates(self.runtime, self.project, self.record["id"], "release")
                release.resolve(self.runtime, self.project, self.record["id"], "release", 1001)
            sizes.append(self.stored_size())
            self.assertLess(sizes[-1], self.BUDGET)
        self.assertLessEqual(abs(sizes[1] - sizes[0]), 32)
        self.assertEqual(self.posts, [])

    def test_history_drops_legacy_raw_payloads_and_stays_capped(self):
        legacy = {"at": 1.0, "source": "console", "selected_run_id": 7,
                  "candidates": [{"id": 7, "status": "queued"}],
                  "raw_candidates": self.page(3),
                  "previous": {"state": "dispatch_unknown"}}
        self.operation(manual_reconciliations=[dict(legacy) for _ in range(12)])
        self.assertGreater(self.stored_size(), 4 * self.BUDGET)
        with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17)])):
            release.resolve(self.runtime, self.project, self.record["id"], "release", 17)
        history = self.saved()["manual_reconciliations"]
        self.assertEqual(len(history), release.RECONCILIATION_AUDIT_LIMIT)
        self.assertEqual([entry["selected_run_id"] for entry in history][-1], 17)
        for entry in history:
            self.assertLessEqual(set(entry), set(release.RECONCILIATION_AUDIT_FIELDS))
            self.assertLessEqual(len(entry["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
        self.assertLess(self.stored_size(), self.BUDGET)
        self.assertEqual(self.posts, [])

    def test_selectable_candidates_exposes_full_range_without_bloating_settings(self):
        remotes = [self.remote(100 + index) for index in range(25)]
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=remotes)):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
            saved_before = self.saved()
            self.assertEqual(saved_before["reconciliation"]["candidate_count"], 25)
            self.assertEqual(len(saved_before["reconciliation"]["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
            self.assertNotIn("selectable_candidates", saved_before["reconciliation"])

            self.assertEqual(view["candidate_count"], 25)
            self.assertEqual(len(view["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
            self.assertIn("selectable_candidates", view)
            self.assertEqual(len(view["selectable_candidates"]), 25)
            self.assertEqual(view["selectable_candidates"][20]["id"], 120)

            result = release.resolve(self.runtime, self.project, self.record["id"], "release", 120)
        self.assertEqual(result["state"], "queued")
        saved_after = self.saved()
        self.assertEqual(saved_after["workflow_run_id"], 120)
        audit = saved_after["manual_reconciliation"]
        self.assertEqual(audit["selected_run_id"], 120)
        self.assertEqual(audit["candidate_count"], 25)
        self.assertEqual(len(audit["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
        self.assertNotIn("selectable_candidates", audit)
        self.assertLess(self.stored_size(), self.BUDGET)
        self.assertEqual(self.posts, [])

    def test_selectable_candidates_capped_at_selectable_limit(self):
        remotes = [self.remote(1000 + index) for index in range(210)]
        self.operation()
        with patch.object(release, "api", side_effect=self.api(runs=remotes)):
            view = release.candidates(self.runtime, self.project, self.record["id"], "release")
        self.assertEqual(view["candidate_count"], 210)
        self.assertEqual(len(view["candidates"]), release.RECONCILIATION_CANDIDATE_LIMIT)
        self.assertEqual(len(view["selectable_candidates"]), release.RECONCILIATION_SELECTABLE_LIMIT)


class ReleaseReconciliationEndpointTest(_ManualReconciliationFixture):
    def test_endpoints_expose_candidates_and_reject_unsafe_ids(self):
        import http.client
        from adaptive_rsi import server as server_module
        self.operation()
        server = server_module.Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            with patch.object(release, "api", side_effect=self.api(runs=[self.remote(17)])):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", "/api/release-candidates?project=" + self.project +
                                       "&run=" + self.record["id"] + "&kind=release",
                                       headers={"Cookie": server.cookie + "=" + server.token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual([item["id"] for item in payload["candidates"]], [17])
                self.assertEqual([item["id"] for item in payload.get("selectable_candidates", [])], [17])
                self.assertEqual(payload["candidates"][0]["url"],
                                 "https://github.com/example/project/actions/runs/17")

                body = json.dumps({"project": self.project, "run": self.record["id"], "kind": "release",
                                   "workflow_run_id": 99}).encode()
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("POST", "/api/release-resolve", body, {
                        "Cookie": server.cookie + "=" + server.token, "Origin": server.origin,
                        "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 400)
                self.assertIn("does not match this operation", payload["error"])
                self.assertEqual(self.posts, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.assertEqual(self.saved()["state"], "dispatch_unknown")

    def test_missing_workflow_listing_endpoint_returns_readable_view(self):
        import http.client
        from adaptive_rsi import server as server_module
        self.operation()
        server = server_module.Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            def unavailable(repo, endpoint, method='GET', fields=None):
                raise release.ApiRejected('fixture missing', 404)
            with patch.object(release, 'api', side_effect=unavailable):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", "/api/release-candidates?project=" + self.project +
                                       "&run=" + self.record["id"] + "&kind=release",
                                       headers={"Cookie": server.cookie + "=" + server.token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["reconciliation_state"], "workflow_not_visible")
                self.assertEqual(payload["candidates"], [])
                self.assertIn("not visible", payload["reason"])
                self.assertEqual(self.posts, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
        saved = self.saved()
        self.assertEqual(saved["state"], "dispatch_unknown")
        self.assertEqual(saved["remote_visibility"]["state"], "workflow_not_visible")

    def test_delayed_appearance_endpoint_sequence_preserves_audit_and_never_redispatches(self):
        import http.client
        from adaptive_rsi import server as server_module
        self.operation()
        server = server_module.Server(self.runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            # 1. Phase 1: Status not visible yet on GitHub Actions.
            with patch.object(release, "api", side_effect=self.api(runs=[])):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", "/api/release-candidates?project=" + self.project +
                                       "&run=" + self.record["id"] + "&kind=release",
                                       headers={"Cookie": server.cookie + "=" + server.token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["reconciliation_state"], "awaiting_remote_visibility")
                self.assertEqual(payload["candidates"], [])
                self.assertEqual(self.saved()["reconciliation"]["state"], "awaiting_remote_visibility")
                self.assertEqual(self.posts, [])

                # Attempting to resolve run 17 before it appears is rejected.
                body = json.dumps({"project": self.project, "run": self.record["id"], "kind": "release",
                                   "workflow_run_id": 17}).encode()
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("POST", "/api/release-resolve", body, {
                        "Cookie": server.cookie + "=" + server.token, "Origin": server.origin,
                        "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 400)
                self.assertIn("does not match this operation", payload["error"])
                self.assertEqual(self.posts, [])

            # 2. Phase 2: Delayed appearance on GitHub Actions.
            remote_run = self.remote(17, status="in_progress")
            with patch.object(release, "api", side_effect=self.api(runs=[remote_run])):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", "/api/release-candidates?project=" + self.project +
                                       "&run=" + self.record["id"] + "&kind=release",
                                       headers={"Cookie": server.cookie + "=" + server.token})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["reconciliation_state"], "matched")
                self.assertEqual([item["id"] for item in payload["candidates"]], [17])
                self.assertEqual(self.saved()["reconciliation"]["state"], "matched")
                self.assertEqual(self.posts, [])

                # 3. Phase 3: Operator resolves through the endpoint.
                body = json.dumps({"project": self.project, "run": self.record["id"], "kind": "release",
                                   "workflow_run_id": 17}).encode()
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("POST", "/api/release-resolve", body, {
                        "Cookie": server.cookie + "=" + server.token, "Origin": server.origin,
                        "X-RSI-CSRF": server.csrf, "Content-Type": "application/json"})
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["state"], "running")
                self.assertEqual(payload["operation"]["workflow_run_id"], 17)

                saved = self.saved()
                self.assertEqual(saved["state"], "running")
                self.assertEqual(saved["workflow_run_id"], 17)
                self.assertEqual(saved["manual_reconciliation"]["selected_run_id"], 17)
                self.assertEqual(saved["manual_reconciliation"]["previous"]["reconciliation_state"], "matched")
                events = self.runtime.store.query("SELECT kind FROM events WHERE run=?", (self.record["id"],))
                self.assertIn("automation_reconciled", [row["kind"] for row in events])
                self.assertEqual(self.posts, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
