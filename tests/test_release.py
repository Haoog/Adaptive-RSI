import ast
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, validate
from adaptive_rsi.gitops import git
from adaptive_rsi.release import ApiRejected, advance, api as real_api, authorize_workflow, health_check, key, pending, retry, workflow_choices
from adaptive_rsi.delivery import pr_body
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from adaptive_rsi.updates import automatic_allowed
from tests.test_runtime import fixture


TRUSTED = "a" * 40
MERGED = "b" * 40
PREVIOUS = "c" * 40
WORKFLOW = """name: Trusted delivery
run-name: ${{ inputs.rsi_operation_id }}
on:
  workflow_dispatch:
    inputs:
      rsi_operation_id:
        required: true
        type: string
      rsi_sha:
        required: true
        type: string
jobs:
  deliver:
    runs-on: ubuntu-latest
    steps:
      - run: echo configured-by-maintainer
"""


class ReleaseAutomationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = fixture(self.root / "project")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime = Runtime(Store(self.root / "state"))
        self.project = self.runtime.add_project(self.repo, enabled=True)
        self.policy = {"enabled": True, "workflow": ".github/workflows/publish.yml", "ref": "trusted-v1", "sha": TRUSTED}
        self.runtime.configure({"github": {"repository": "example/project"}, "delivery": {"mode": "pr", "release": self.policy}}, self.project)
        task = self.runtime.goal(self.project, "Verified change")
        self.run = self.runtime.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.store.transition(self.run["id"], "merged", {"merged_sha": MERGED, "feedback": {"state": "verified"}}, True)
        self.run = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (self.run["id"],))
        self.runtime.store.put_setting("delivery:" + self.run["id"], {"repository": "example/project", "branch": "main", "head": "evaluated", "base": "baseline"})
        self.commands = []
        self.remote = None

    def test_legacy_deploy_snapshot_keeps_pending_health_tracking(self):
        deploy = {**self.policy, "health_url": "https://service.example/health", "automatic_rollback": False}
        self.runtime.configure({"delivery": {"deploy": deploy}}, self.project)
        saved = self.runtime.config(self.project)
        for field in ("health_attempts", "health_interval_seconds", "health_warmup_seconds"):
            saved["delivery"]["deploy"].pop(field)
        run = {**self.run, "config": encode(saved)}
        self.runtime.store.put_setting(key(run["id"], "deploy"), {"state": "workflow_succeeded"})
        self.assertTrue(pending(self.runtime, run))

    def test_rejected_retry_receipt_survives_concurrent_authorization_revocation(self):
        from adaptive_rsi.release import save
        data = {"state": "failed", "operation_id": "rsi-release-test", "sha": MERGED,
                "dispatch_receipt": "rejected"}
        save(self.runtime, self.run, "release", data)
        def remote(repo, endpoint, method="GET", fields=None):
            if endpoint.endswith("/dispatches"):
                self.runtime.configure({"delivery": {"release": {"enabled": False}}}, self.project)
                raise ApiRejected("explicit rejection", 422)
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=remote), self.assertRaises(ApiRejected):
            retry(self.runtime, self.project, self.run["id"], "release")
        receipt = self.runtime.store.setting(key(self.run["id"], "release"))
        self.assertEqual((receipt["state"], receipt["dispatch_receipt"], receipt["dispatch_http_status"]),
                         ("failed", "rejected", 422))

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def api(self, repo, endpoint, method="GET", fields=None):
        self.assertEqual(repo, "example/project")
        self.commands.append((endpoint, method, fields))
        if endpoint.startswith("git/ref/tags/"):
            return {"object": {"type": "commit", "sha": TRUSTED}}
        if endpoint.startswith("contents/"):
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(WORKFLOW.encode()).decode()}
        if "/dispatches" in endpoint or endpoint.endswith("/rerun"):
            return {}
        if endpoint.startswith("actions/runs/"):
            return self.remote
        if "/runs?" in endpoint:
            return {"workflow_runs": [self.remote] if self.remote else []}
        raise AssertionError(endpoint)

    def tick(self):
        return advance(self.runtime, self.runtime.project(self.project), self.run, {"merged_sha": MERGED, "feedback": {"state": "verified"}})

    def remote_run(self, data, state="completed", conclusion="success", attempt=1):
        return {"id": 17, "head_sha": TRUSTED, "display_title": data["operation_id"], "event": "workflow_dispatch",
                "path": data["workflow"], "status": state, "conclusion": conclusion, "run_attempt": attempt,
                "html_url": "https://github.com/example/project/actions/runs/17"}

    def test_dispatch_pins_target_and_reconciles_after_restart_without_duplicates(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.assertEqual(first["state"], "submitted")
            dispatch = next(item for item in self.commands if "/dispatches" in item[0])
            self.assertEqual(dispatch[2]["inputs[rsi_sha]"], MERGED)
            self.assertEqual(dispatch[2]["ref"], "trusted-v1")
            self.remote = self.remote_run(first)
            self.runtime.close()
            self.runtime = Runtime(Store(self.root / "state"))
            self.assertEqual(self.tick()["release"]["state"], "workflow_succeeded")
            self.tick()
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 1)

    def test_ambiguous_dispatch_is_reconciled_and_never_automatically_repeated(self):
        def uncertain(repo, endpoint, method="GET", fields=None):
            result = self.api(repo, endpoint, method, fields)
            if "/dispatches" in endpoint:
                raise RuntimeError("Network disconnected after GitHub accepted request")
            return result
        with patch("adaptive_rsi.release.api", side_effect=uncertain):
            first = self.tick()["release"]
            self.assertEqual(first["state"], "dispatch_unknown")
            self.tick()
            with self.assertRaisesRegex(ValueError, "confirmed failed"):
                retry(self.runtime, self.project, self.run["id"], "release")
            self.remote = self.remote_run(first)
            self.assertEqual(self.tick()["release"]["state"], "workflow_succeeded")
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 1)

    def test_pause_or_revoke_during_validation_blocks_unissued_action(self):
        for revoked in [False, True]:
            with self.subTest(revoked=revoked):
                self.runtime.pause(self.project, False)
                self.runtime.configure({"delivery": {"release": {"enabled": True}}}, self.project)
                self.runtime.store.execute("DELETE FROM settings WHERE key=?", (key(self.run["id"], "release"),))
                self.commands.clear()
                def change_policy(repo, endpoint, method="GET", fields=None):
                    result = self.api(repo, endpoint, method, fields)
                    if endpoint.startswith("contents/"):
                        if revoked:
                            self.runtime.configure({"delivery": {"release": {"enabled": False}}}, self.project)
                        else:
                            self.runtime.pause(self.project, True)
                    return result
                with patch("adaptive_rsi.release.api", side_effect=change_policy):
                    self.tick()
                self.assertFalse(any("/dispatches" in item[0] for item in self.commands))

    def test_pin_drift_and_cross_project_retry_are_rejected(self):
        with patch("adaptive_rsi.release.api", return_value={"object": {"type": "commit", "sha": PREVIOUS}}):
            result = self.tick()["release"]
        self.assertEqual(result["state"], "blocked")
        other = self.runtime.add_project(fixture(self.root / "other"))
        with self.assertRaisesRegex(ValueError, "this project"):
            retry(self.runtime, other, self.run["id"], "release")

    def test_pr_body_describes_behavior_evidence_and_omits_task_body(self):
        task = {"title": "修复登录重试", "body": "PRIVATE-USER-GOAL"}
        run = {"id": "run-id", "base": "a" * 40}
        outcome = {"summary": "限制登录重试次数", "files": ["login.py"],
                   "evaluation": {"decision": "pass", "improvement": {"decision": "pass"}},
                   "review": {"decision": "pass"},
                   "value": {"benefit": "避免重复跳转", "inaction": "用户会陷入循环"}}
        body = pr_body(task, run, outcome)
        self.assertIn("## 解决的问题", body)
        self.assertIn("限制登录重试次数", body)
        self.assertIn("独立代码评审：通过", body)
        self.assertIn("避免重复跳转", body)
        self.assertNotIn("PRIVATE-USER-GOAL", body)
        self.assertNotIn("@team", pr_body({**task, "title": "@team <b>login</b>"}, run, outcome))

    def test_confirmed_failure_retries_same_workflow_run_and_requires_current_permission(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.assertEqual(self.tick()["release"]["state"], "failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            reruns = [item for item in self.commands if item[0].endswith("/rerun")]
            self.assertEqual(len(reruns), 1)
            self.assertEqual(reruns[0][0], "actions/runs/17/rerun")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            self.assertEqual(sum(item[0].endswith("/rerun") for item in self.commands), 1)
            self.runtime.configure({"delivery": {"release": {"enabled": False}}}, self.project)
            with self.assertRaisesRegex(ValueError, "revoked"):
                retry(self.runtime, self.project, self.run["id"], "release")

    def rerun_process_fixture(self):
        executable = self.root / "gh"
        executable.write_text("#!" + sys.executable + "\nimport os,sys\n"
                              "status=os.environ['RSI_RERUN_RESPONSE']\n"
                              "if status != '204':\n"
                              "    print('gh: rejected (HTTP '+status+')' if status.isdigit() else 'connection reset after request', file=sys.stderr)\n"
                              "    sys.exit(1)\n")
        executable.chmod(0o700)
        def boundary(repo, endpoint, method="GET", fields=None):
            result = self.api(repo, endpoint, method, fields)
            if endpoint.endswith("/rerun"):
                return real_api(repo, endpoint, method, fields)
            return result
        return boundary

    def test_explicit_rerun_rejection_via_real_cli_boundary_restores_failed_and_can_retry(self):
        boundary = self.rerun_process_fixture()
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}), patch("adaptive_rsi.release.api", side_effect=boundary):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.tick()
            for code in [403, 422]:
                with self.subTest(code=code), patch.dict(os.environ, {"RSI_RERUN_RESPONSE": str(code)}), self.assertRaises(ApiRejected):
                    retry(self.runtime, self.project, self.run["id"], "release")
                saved = self.runtime.store.setting(key(self.run["id"], "release"))
                self.assertEqual(saved["state"], "failed")
                self.assertFalse(saved["rerun_pending"])
                self.assertEqual(saved["rerun_http_status"], code)
                self.assertEqual(saved["rerun_receipt"], "rejected")
            with patch.dict(os.environ, {"RSI_RERUN_RESPONSE": "204"}):
                self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            self.assertEqual(sum(item[0].endswith("/rerun") for item in self.commands), 3)

    def test_timeout_or_server_error_is_not_classified_as_confirmed_rejection(self):
        self.rerun_process_fixture()
        for response in ["408", "500", "network"]:
            with self.subTest(response=response), patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"], "RSI_RERUN_RESPONSE": response}):
                with self.assertRaises(RuntimeError) as error:
                    real_api("example/project", "actions/runs/17/rerun", "POST")
                self.assertNotIsInstance(error.exception, ApiRejected)

    def test_unknown_rerun_is_durable_becomes_attention_and_manual_reconcile_never_reposts(self):
        from adaptive_rsi.dashboard import records
        boundary = self.rerun_process_fixture()
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"], "RSI_RERUN_RESPONSE": "network"}), patch("adaptive_rsi.release.api", side_effect=boundary):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.tick()
            with self.assertRaises(RuntimeError):
                retry(self.runtime, self.project, self.run["id"], "release")
            self.runtime.close()
            self.runtime = Runtime(Store(self.root / "state"))
            self.assertEqual(self.tick()["release"]["state"], "rerunning")
            with patch("adaptive_rsi.release.time.time", return_value=time.time() + 301):
                self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
            self.assertFalse(pending(self.runtime, self.run))
            self.assertEqual(records(self.runtime.store, self.project)["tasks"][0]["display_state"], "release_failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerun_unknown")
            self.remote = self.remote_run(first, attempt=2)
            reconciled = retry(self.runtime, self.project, self.run["id"], "release")
            self.assertEqual(reconciled["state"], "workflow_succeeded")
            self.assertFalse(reconciled["rerun_pending"])
            self.assertEqual(sum(item[0].endswith("/rerun") for item in self.commands), 1)

    def test_rollback_rerun_rejection_and_unknown_are_not_stuck_rolling_back(self):
        from adaptive_rsi.release import save
        self.setup_deployment(rollback=True)
        boundary = self.rerun_process_fixture()
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}), patch("adaptive_rsi.release.api", side_effect=boundary):
            data = self.tick()["deploy"]
            rollback = {**data, "sha": PREVIOUS, "operation_id": data["operation_id"] + "-rollback", "state": "failed", "workflow_run_id": 17, "attempt": 1}
            data.update(state="rollback_failed", rollback=rollback)
            save(self.runtime, self.run, "deploy", data)
            self.remote = self.remote_run(rollback, conclusion="failure")
            with patch.dict(os.environ, {"RSI_RERUN_RESPONSE": "403"}), self.assertRaises(ApiRejected):
                retry(self.runtime, self.project, self.run["id"], "deploy")
            self.assertEqual(self.runtime.store.setting(key(self.run["id"], "deploy"))["state"], "rollback_failed")
            with patch.dict(os.environ, {"RSI_RERUN_RESPONSE": "network"}), self.assertRaises(RuntimeError):
                retry(self.runtime, self.project, self.run["id"], "deploy")
            with patch("adaptive_rsi.release.time.time", return_value=time.time() + 301):
                result = self.tick()["deploy"]
            self.assertEqual(result["state"], "rollback_failed")
            self.assertEqual(result["rollback"]["state"], "rerun_unknown")
            self.assertFalse(pending(self.runtime, self.run))
            retry(self.runtime, self.project, self.run["id"], "deploy")
            self.assertEqual(sum(item[0].endswith("/rerun") for item in self.commands), 2)

    def test_authorization_is_not_inherited_or_granted_by_task_overrides(self):
        with self.assertRaisesRegex(ValueError, "per project"):
            self.runtime.configure({"delivery": {"release": self.policy}})
        with self.assertRaisesRegex(ValueError, "cannot grant"):
            self.runtime.goal(self.project, "grant", {"delivery": {"release": self.policy}})
        other = self.runtime.add_project(fixture(self.root / "other"))
        self.assertFalse(self.runtime.config(other)["delivery"]["release"]["enabled"])

    def test_existing_workflow_authorization_preserves_pr_and_merge_permissions(self):
        self.runtime.configure({"delivery": {"release": {"enabled": False}, "mode": "local", "auto_merge": False}}, self.project)
        def choices(repo, endpoint, method="GET", fields=None):
            if endpoint == "":
                return {"default_branch": "main"}
            if endpoint.startswith("commits/"):
                return {"sha": TRUSTED}
            if endpoint.startswith("actions/workflows?"):
                return {"workflows": [{"name": "Publish", "path": self.policy["workflow"], "state": "active"}]}
            if endpoint.startswith("git/matching-refs/"):
                return []
            if endpoint == "git/refs":
                self.assertEqual(fields["sha"], TRUSTED)
                return {}
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=choices):
            self.assertTrue(workflow_choices(self.runtime, self.project)["workflows"][0]["compatible"])
            target = authorize_workflow(self.runtime, self.project, "release", self.policy["workflow"])
        self.assertEqual(target["sha"], TRUSTED)
        self.assertEqual(self.runtime.config(self.project)["delivery"]["mode"], "local")
        self.assertFalse(self.runtime.config(self.project)["delivery"]["auto_merge"])

    def setup_deployment(self, rollback=False):
        deploy = {**self.policy, "health_url": "https://app.example/health", "automatic_rollback": rollback}
        self.runtime.configure({"delivery": {"release": {"enabled": False}, "deploy": deploy}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.store.execute("UPDATE runs SET config=? WHERE id=?", (encode(config), self.run["id"]))
        self.run["config"] = encode(config)
        return deploy

    def test_deploy_success_without_health_target_is_explicitly_unverified(self):
        self.setup_deployment()
        self.runtime.configure({"delivery": {"deploy": {"health_url": ""}}}, self.project)
        self.run["config"] = encode(self.runtime.config(self.project))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["deploy"]
            self.remote = self.remote_run(first)
            result = self.tick()["deploy"]
        self.assertEqual(result["state"], "workflow_succeeded")
        self.assertEqual(result["health"], "unverified")

    def test_deployment_waits_durably_for_authorized_release_then_dispatches(self):
        self.runtime.configure({"delivery": {"deploy": self.policy}}, self.project)
        self.run["config"] = encode(self.runtime.config(self.project))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()
            self.assertEqual(first["deploy"]["state"], "waiting_release")
            self.assertEqual(self.runtime.release_status(self.project, self.run["id"])["deploy"]["state"], "waiting_release")
            self.remote = self.remote_run(first["release"])
            second = self.tick()
            self.assertEqual(second["release"]["state"], "workflow_succeeded")
            self.assertEqual(second["deploy"]["state"], "submitted")
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 2)

    def test_health_failure_rolls_back_only_to_previously_verified_same_target(self):
        from adaptive_rsi.release import stable_key
        deploy = self.setup_deployment(rollback=True)
        self.runtime.store.put_setting(stable_key(self.project, "example/project", deploy["health_url"]), {"sha": PREVIOUS})
        with patch("adaptive_rsi.release.api", side_effect=self.api), patch("adaptive_rsi.release.health_check", side_effect=lambda url, sha: {"healthy": sha == PREVIOUS, "sha": sha}):
            first = self.tick()["deploy"]
            self.remote = self.remote_run(first)
            failed = self.tick()["deploy"]
            self.assertEqual(failed["state"], "rolling_back")
            rollback = failed["rollback"]
            self.assertEqual(rollback["sha"], PREVIOUS)
            self.remote = self.remote_run(rollback)
            rolled = self.tick()["deploy"]
            self.assertEqual(rolled["state"], "rolled_back")
            self.assertTrue(rolled["rollback"]["health"]["healthy"])
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 2)

    def test_no_previous_stable_sha_never_claims_successful_rollback(self):
        self.setup_deployment(rollback=True)
        with patch("adaptive_rsi.release.api", side_effect=self.api), patch("adaptive_rsi.release.health_check", return_value={"healthy": False}):
            first = self.tick()["deploy"]
            self.remote = self.remote_run(first)
            result = self.tick()["deploy"]
        self.assertEqual(result["state"], "health_failed")
        self.assertTrue(result["rollback_unavailable"])
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 1)

    def test_regressed_or_missing_postmerge_verification_cannot_dispatch(self):
        for feedback, expected in [({}, "waiting_verification"), ({"state": "regressed"}, "blocked_verification"),
                                   ({"state": "insufficient_evidence"}, "blocked_verification")]:
            with self.subTest(feedback=feedback), patch("adaptive_rsi.release.api") as api:
                result = advance(self.runtime, self.runtime.project(self.project), self.run, {"merged_sha": MERGED, "feedback": feedback})
                self.assertEqual(result["release"]["state"], expected)
                api.assert_not_called()

    def test_waiting_release_does_not_inherit_stale_verification_error(self):
        # 检查通过后部署转入等待发布；此前等待检查时的提示已过期，不得继续展示。
        self.runtime.configure({"delivery": {"deploy": self.policy}}, self.project)
        self.run["config"] = encode(self.runtime.config(self.project))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            stale = advance(self.runtime, self.runtime.project(self.project), self.run, {"merged_sha": MERGED, "feedback": {}})
            self.assertEqual(stale["deploy"]["state"], "waiting_verification")
            self.assertEqual(stale["deploy"]["error"], "Post-merge verification must pass before release/deploy")
            waiting = self.tick()
            self.assertEqual(waiting["deploy"]["state"], "waiting_release")
            self.assertEqual(waiting["deploy"]["error"], "")

    def test_owner_change_during_health_check_prevents_stale_rollback(self):
        from adaptive_rsi.release import owner_key, stable_key
        policy = self.setup_deployment(rollback=True)
        self.runtime.store.put_setting(stable_key(self.project, "example/project", policy["health_url"]), {"sha": PREVIOUS})
        def changed_owner(url, sha):
            self.runtime.store.put_setting(owner_key(self.project, "example/project", policy), {"run": "newer-run", "sha": "d" * 40})
            return {"healthy": False, "sha": sha}
        with patch("adaptive_rsi.release.api", side_effect=self.api), patch("adaptive_rsi.release.health_check", side_effect=changed_owner):
            first = self.tick()["deploy"]
            self.remote = self.remote_run(first)
            self.assertEqual(self.tick()["deploy"]["state"], "superseded")
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 1)

    def test_late_old_or_diverged_deployment_cannot_overwrite_newer_healthy_target(self):
        from adaptive_rsi.release import owner_key
        policy = self.setup_deployment()
        self.runtime.store.put_setting(owner_key(self.project, "example/project", policy), {"run": "newer-run", "sha": "d" * 40})
        self.runtime.store.put_setting(key("newer-run", "deploy"), {"state": "healthy"})
        for status in ["behind", "diverged", "identical"]:
            self.runtime.store.execute("DELETE FROM settings WHERE key=?", (key(self.run["id"], "deploy"),))
            def compared(repo, endpoint, method="GET", fields=None):
                if endpoint.startswith("compare/"):
                    return {"status": status}
                return self.api(repo, endpoint, method, fields)
            with self.subTest(status=status), patch("adaptive_rsi.release.api", side_effect=compared):
                self.assertEqual(self.tick()["deploy"]["state"], "superseded")
        self.assertFalse(any("/dispatches" in item[0] for item in self.commands))


class StableUpdatePolicyTest(unittest.TestCase):
    def test_policy_is_explicit_and_does_not_allow_downgrades(self):
        for target, patch_ok, minor_ok in [("1.2.4", True, True), ("1.3.0", False, True), ("2.0.0", False, False)]:
            status = {"current": "1.2.3", "version": target}
            self.assertEqual(automatic_allowed(status, "patch"), patch_ok)
            self.assertEqual(automatic_allowed(status, "minor"), minor_ok)
            self.assertTrue(automatic_allowed(status, "stable"))
        self.assertFalse(automatic_allowed({"current": "1.2.3", "version": "1.2.2"}, "stable"))
        self.assertEqual(validate({})["updates"]["auto_policy"], "patch")

    def test_health_endpoint_must_be_explicit_https_without_credentials(self):
        for url in ["http://example/health", "https://user:secret@example/health", "https://example/health?token=x", "https://example/health#x"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate({"delivery": {"deploy": {"health_url": url}}})


class ReleaseVersionPatchTest(unittest.TestCase):
    def test_consecutive_releases_preserve_authored_notes_then_reconstruct_empty_unreleased(self):
        from scripts import release as module
        with tempfile.TemporaryDirectory() as temp, patch.object(module, "ROOT", Path(temp)):
            root = Path(temp)
            contents = {"plugin.json": '{"version":"1.2.3"}\n', "update.json": '{"version":"1.2.3"}\n',
                        "pyproject.toml": 'version = "1.2.3"\n', "adaptive_rsi/__init__.py": '__version__ = "1.2.3"\n',
                        "CHANGELOG.md": '# 版本\n\n## Unreleased\n\n- 作者记录的具体改进。\n\n## 1.2.3\n\n- 原版。\n',
                        "CHANGELOG.en.md": '# Changelog\n\n## Unreleased\n\n- Specific improvement recorded by the author.\n\n## 1.2.3\n\n- Old.\n'}
            for name, text in contents.items():
                (root / name).parent.mkdir(exist_ok=True)
                (root / name).write_text(text)
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
            for cycle, target in enumerate(["1.3.0", "1.3.1", "1.3.2"]):
                if cycle:
                    (root / "normal-change.py").write_text("value = " + str(cycle) + "\n")
                    git(root, "add", "normal-change.py")
                    git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "正常改进")
                base = git(root, "rev-parse", "HEAD")
                result = module.bump_files(base, target)
                self.assertEqual(result, module.bump_files(base, target))
                for name, text in result.items():
                    (root / name).write_text(text)
                git(root, "add", "--", *module.RELEASE_FILES)
                git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "准备版本 " + target)
                head = git(root, "rev-parse", "HEAD")
                self.assertEqual(set(git(root, "diff", "--name-only", base, head).splitlines()), set(module.RELEASE_FILES))
                self.assertEqual(module.verify_generated(base, head), target)
                for name, authored in [("CHANGELOG.md", "- 作者记录的具体改进。"), ("CHANGELOG.en.md", "- Specific improvement recorded by the author.")]:
                    section = result[name].split("## " + target + "\n", 1)[1].split("\n## ", 1)[0]
                    self.assertIn("## Unreleased\n\n## " + target, result[name])
                    self.assertIn(authored, result[name])
                    if cycle == 0:
                        self.assertIn(authored, section)
                    else:
                        fallback = "- 发布经过检查的主线改进；本版本对应提交 `" + base + "`。" if name == "CHANGELOG.md" else "- Release verified mainline improvements from commit `" + base + "`."
                        self.assertIn(fallback, section)
                        self.assertNotIn(authored, section)

    def test_version_patch_is_reconstructed_and_manual_additions_block(self):
        spec = importlib.util.spec_from_file_location("release_script", Path(__file__).resolve().parents[1] / "scripts/release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module.ROOT = root
            for name, text in {
                "plugin.json": '{"version":"1.2.3"}\n', "update.json": '{"version":"1.2.3"}\n',
                "pyproject.toml": 'version = "1.2.3"\n', "adaptive_rsi/__init__.py": '__version__ = "1.2.3"\n',
                "CHANGELOG.md": '# 版本\n\n## Unreleased\n\n- 中文功能。\n\n## 1.2.3\n\n- 原版。\n',
                "CHANGELOG.en.md": '# Changelog\n\n## Unreleased\n\n- English feature.\n\n## 1.2.3\n\n- Old.\n',
            }.items():
                (root / name).parent.mkdir(exist_ok=True)
                (root / name).write_text(text)
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
            base = git(root, "rev-parse", "HEAD")
            result = module.bump_files(base, "1.3.0")
            for name, text in result.items():
                (root / name).write_text(text)
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "版本")
            head = git(root, "rev-parse", "HEAD")
            self.assertEqual(module.verify_generated(base, head), "1.3.0")
            self.assertIn("## Unreleased\n\n## 1.3.0", result["CHANGELOG.md"])
            self.assertIn("中文功能", result["CHANGELOG.md"])
            (root / "manual.py").write_text("print('unreviewed')")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "manual")
            with self.assertRaisesRegex(ValueError, "independently reconstructed"):
                module.verify_generated(base, git(root, "rev-parse", "HEAD"))

class ReleaseBatchCadenceTest(unittest.TestCase):
    def prepare(self, requested='false', notes='## Unreleased\n', level='patch', generated=False, new_stage='false'):
        from scripts import release as module
        head='b'*40
        def command(*args, **kwargs):
            if args[:2]==('git','rev-parse'):
                return head
            if args[:3]==('gh','pr','list'):
                return '[]' if 'open' in args else json.dumps([{'number':9,'state':'OPEN','headRefOid':'c'*40}])
            raise AssertionError('Unexpected side effect: '+repr(args))
        def api(endpoint):
            return {'default_branch':'main'} if not endpoint else {'sha':head}
        def read(sha,name):
            return '__version__ = "1.2.3"\n' if name.endswith('__init__.py') else notes
        with patch.dict(os.environ,{'PREPARE_RELEASE':requested,'RELEASE_LEVEL':level,
                                    'NEW_PRODUCT_STAGE':new_stage,'GITHUB_REPOSITORY':'example/project'}),patch.object(module,'run',side_effect=command) as calls,patch.object(module,'gh',side_effect=api),patch.object(module,'read_at',side_effect=read),patch.object(module,'verify_generated',side_effect=None if generated else ValueError('not mechanical')),patch.object(module,'tree_at',return_value='d'*40),patch.object(module,'find_verified_artifact',return_value={'run_id':17,'name':'release-verified-'+('d'*40)}),patch.object(module,'output') as output:
            module.prepare()
            return output.call_args.kwargs,calls.call_args_list

    def test_ordinary_merges_never_bump_or_create_pr(self):
        result,calls=self.prepare()
        self.assertEqual(result['mode'],'idle')
        self.assertEqual(len(calls),1)

    def test_explicit_stable_batch_reuses_existing_version_pr(self):
        result,calls=self.prepare(requested='true')
        self.assertEqual(result['mode'],'verify')
        self.assertEqual(result['pr'],9)
        self.assertTrue(any('chore/release-v1.2.4-' in str(call) for call in calls))

    def test_minor_increment_requires_explicit_selection(self):
        with self.assertRaisesRegex(ValueError,'new product stage'):
            self.prepare(requested='true',level='minor')
        result,calls=self.prepare(requested='true',level='minor',new_stage='true')
        self.assertEqual(result['mode'],'verify')
        self.assertTrue(any('chore/release-v1.3.0-' in str(call) for call in calls))

    def test_mechanical_version_commit_still_publishes_without_new_batch(self):
        result,_=self.prepare(notes='<!-- adaptive-rsi-release:1.2.3;base:'+('a'*40)+' -->',generated=True)
        self.assertEqual(result['mode'],'publish')
        self.assertEqual(result['version'],'1.2.3')
        self.assertEqual(result['artifact_run_id'],17)
        self.assertEqual(result['artifact_name'],'release-verified-'+('d'*40))

    def test_invalid_explicit_increment_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'Release level'):
            self.prepare(requested='true',level='surprise')


class VerifiedArtifactReuseTest(unittest.TestCase):
    def test_only_successful_release_verify_artifacts_from_main_are_reused(self):
        from scripts import release as module
        tree = "a" * 40
        name = "release-verified-" + tree
        artifacts = [
            {"id": 1, "name": name, "expired": True, "workflow_run": {"id": 10}},
            {"id": 2, "name": name, "expired": False, "workflow_run": {"id": 20}},
            {"id": 3, "name": name, "expired": False, "workflow_run": {"id": 30}},
        ]
        runs = {
            "20": {"path": ".github/workflows/checks.yml", "event": "workflow_dispatch",
                   "status": "completed", "conclusion": "success", "head_branch": "main"},
            "30": {"path": ".github/workflows/release-verify.yml", "event": "workflow_dispatch",
                   "status": "completed", "conclusion": "success", "head_branch": "main"},
        }
        def api(endpoint):
            if endpoint.startswith("actions/artifacts?"):
                return {"artifacts": artifacts}
            return runs[endpoint.rsplit("/", 1)[-1]]
        with patch.object(module, "gh", side_effect=api):
            self.assertEqual(module.find_verified_artifact(tree, "main"), {"run_id": 30, "name": name})

    def test_failed_or_untrusted_artifact_lookup_returns_no_reuse(self):
        from scripts import release as module
        tree = "b" * 40
        name = "release-verified-" + tree
        with patch.object(module, "gh", side_effect=[
                    {"artifacts": [{"id": 1, "name": name, "expired": False, "workflow_run": {"id": 10}}]},
                    {"path": ".github/workflows/release-verify.yml", "event": "workflow_dispatch",
                     "status": "completed", "conclusion": "failure", "head_branch": "main"},
                ]):
            self.assertIsNone(module.find_verified_artifact(tree, "main"))


class ReleaseProofTest(unittest.TestCase):
    def test_proof_binds_exact_tree_workflow_and_asset_bytes(self):
        from scripts import release as module
        from scripts.release_smoke import write_proof
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "adaptive_rsi").mkdir()
            (root / "adaptive_rsi/__init__.py").write_text('__version__ = "0.5.0"\n')
            git(root, "init", "-q")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
            sha = git(root, "rev-parse", "HEAD")
            tree = git(root, "rev-parse", "HEAD^{tree}")
            dist = root / "dist"
            dist.mkdir()
            for name in ["adaptive-rsi-0.5.0-plugin.zip", "adaptive_rsi-0.5.0-py3-none-any.whl",
                         "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg"]:
                (dist / name).write_bytes(("fixture:" + name).encode())
            for name in ["adaptive-rsi-0.5.0-plugin.zip", "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg"]:
                digest = hashlib.sha256((dist / name).read_bytes()).hexdigest()
                (dist / (name + ".sha256")).write_text(digest + "  " + name + "\n")
            write_proof(dist, "0.5.0", sha, tree,
                        "Haoog/Adaptive-RSI/.github/workflows/release-verify.yml@refs/heads/main",
                        "c" * 40)
            with patch.object(module, "ROOT", root):
                proof = module.validate_proof(
                    dist, sha, {".github/workflows/release-verify.yml"})
                self.assertEqual(proof["source_tree"], tree)
                self.assertEqual(proof["workflow_source_sha"], "c" * 40)
                self.assertEqual(set(proof["artifacts"]), {path.name for path in dist.iterdir()
                                                           if path.name != "release-proof.json"})
                (dist / "adaptive-rsi-0.5.0-plugin.zip").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "asset bytes"):
                    module.validate_proof(dist, sha, {".github/workflows/release-verify.yml"})
                with self.assertRaisesRegex(ValueError, "trusted release workflow"):
                    module.validate_proof(dist, sha, {".github/workflows/release.yml"})

    def test_proof_rejects_incomplete_identity_metadata(self):
        from scripts.release_smoke import write_proof
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "asset.whl").write_bytes(b"asset")
            for sha, tree, workflow in [
                ("not-a-sha", "a" * 40, "example/repo/.github/workflows/release.yml@refs/heads/main"),
                ("a" * 40, "not-a-tree", "example/repo/.github/workflows/release.yml@refs/heads/main"),
                ("a" * 40, "a" * 40, "release.yml"),
            ]:
                with self.subTest(sha=sha, tree=tree, workflow=workflow), self.assertRaises(ValueError):
                    write_proof(root, "0.5.0", sha, tree, workflow)


class WorkflowConfigurationTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def workflow(self, name):
        path = self.ROOT / ".github/workflows" / name
        try:
            import yaml
            return yaml.safe_load(path.read_text())
        except ImportError:
            if not shutil.which("ruby"):
                self.skipTest("A local YAML parser is unavailable")
            output = subprocess.check_output(
                ["ruby", "-ryaml", "-rjson", "-e",
                 "puts JSON.generate(YAML.load_file(ARGV[0]))", str(path)],
                text=True,
            )
            return json.loads(output)

    def test_python_matrix_uses_ubuntu_and_runs_macos_only_cases_separately(self):
        workflow = self.workflow("checks.yml")
        jobs = workflow["jobs"]
        self.assertEqual(jobs["python"]["runs-on"], "${{ matrix.os }}")
        self.assertTrue(all(item["os"] == "ubuntu-latest" for item in jobs["python"]["strategy"]["matrix"]["include"]))
        self.assertEqual(jobs["browser"]["runs-on"], "macos-14")
        self.assertEqual(jobs["macos_sandbox_core"]["runs-on"], "macos-14")
        self.assertEqual(jobs["macos_sandbox_flows"]["runs-on"], "macos-14")
        required = set(jobs["verify"]["needs"])
        self.assertTrue({"python", "macos_sandbox_core", "macos_sandbox_flows", "browser", "linux"} <= required)
        text = (self.ROOT / ".github/workflows/checks.yml").read_text()
        self.assertIn("hashFiles('package-lock.json')", text)
        self.assertIn("hashFiles('deploy/Dockerfile.worker')", text)
        self.assertIn("hashFiles('pyproject.toml')", text)
        for path in sorted((self.ROOT / "tests").glob("test_*.py")):
            self.assertIn("tests." + path.stem, text)

    def test_python_matrix_covers_every_non_darwin_test_method(self):
        workflow = self.workflow("checks.yml")
        modules = {
            module
            for item in workflow["jobs"]["python"]["strategy"]["matrix"]["include"]
            for module in item["modules"].split()
        }
        for path in sorted((self.ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            module = "tests." + path.stem
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                class_decorators = "\n".join(ast.unparse(item) for item in cls.decorator_list)
                for method in cls.body:
                    if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test"):
                        continue
                    decorators = class_decorators + "\n" + "\n".join(
                        ast.unparse(item) for item in method.decorator_list)
                    if ("skipUnless" in decorators or "skipIf" in decorators) and ("Darwin" in decorators or "Seatbelt" in decorators):
                        continue
                    test_id = module + "." + cls.name + "." + method.name
                    with self.subTest(test=test_id):
                        self.assertTrue(module in modules or module + "." + cls.name in modules,
                                        test_id + " is not covered by the Ubuntu Python matrix")

    def test_every_darwin_decorated_test_is_kept_in_a_macos_job(self):
        text = (self.ROOT / ".github/workflows/checks.yml").read_text()
        expected = set()
        for path in sorted((self.ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for method in cls.body:
                    if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test"):
                        continue
                    decorators = "\n".join(ast.unparse(item) for item in method.decorator_list)
                    if ("skipUnless" in decorators or "skipIf" in decorators) and ("Darwin" in decorators or "Seatbelt" in decorators):
                        expected.add("tests." + path.stem + "." + cls.name + "." + method.name)
        self.assertTrue(expected)
        for test_id in sorted(expected):
            self.assertIn(test_id, text)

    def test_desktop_paths_select_smoke_or_full_and_cache_lockfiles(self):
        workflow = self.workflow("desktop.yml")
        self.assertEqual(set(workflow["jobs"]), {"changes", "desktop-macos"})
        paths = workflow["jobs"]["changes"]["steps"][1]["env"]
        self.assertIn("BASE_SHA", paths)
        text = (self.ROOT / ".github/workflows/desktop.yml").read_text()
        for command in ["scripts/package_desktop.py classify", "scripts/package_desktop.py smoke-engine",
                        "scripts/package_desktop.py verify-dmg", "npm run --prefix desktop build"]:
            self.assertIn(command, text)
        self.assertIn("desktop/package-lock.json", text)
        self.assertIn("hashFiles('desktop/src-tauri/Cargo.lock')", text)
        self.assertIn("cache-dependency-glob: pyproject.toml", text)

    def test_release_verification_attests_and_uploads_exact_tree_artifacts(self):
        workflow = self.workflow("release-verify.yml")
        permissions = workflow["jobs"]["verify"]["permissions"]
        self.assertEqual(permissions["id-token"], "write")
        self.assertEqual(permissions["attestations"], "write")
        steps = workflow["jobs"]["verify"]["steps"]
        attest = next(step for step in steps if step.get("id") == "attest")
        self.assertTrue(attest["continue-on-error"])
        upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertIn("steps.attest.outcome", upload["if"])
        text = (self.ROOT / ".github/workflows/release-verify.yml").read_text()
        self.assertIn("actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8", text)
        self.assertIn("release-verified-${{ steps.source.outputs.tree }}", text)
        self.assertIn("RSI_SOURCE_TREE", text)
        self.assertIn("RSI_WORKFLOW_SOURCE_SHA", text)
        self.assertIn("hashFiles('package-lock.json')", text)
        self.assertIn("hashFiles('desktop/src-tauri/Cargo.lock')", text)

    def test_formal_release_prefers_attested_reuse_and_falls_back_to_rebuild(self):
        workflow = self.workflow("release.yml")
        jobs = workflow["jobs"]
        self.assertTrue({"reuse", "build", "publish"} <= set(jobs))
        self.assertEqual(jobs["build"]["needs"], ["prepare", "reuse"])
        self.assertIn("needs.reuse.result != 'success'", jobs["build"]["if"])
        text = (self.ROOT / ".github/workflows/release.yml").read_text()
        for command in ["actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", "scripts/release.py validate-verified",
                        "gh attestation verify", "--signer-workflow", "--source-ref refs/heads/main", "--source-digest",
                        "scripts/release_smoke.py", "scripts/package_desktop.py verify-dmg"]:
            self.assertIn(command, text)
        self.assertIn("hashFiles('package-lock.json')", text)
        self.assertIn("hashFiles('desktop/src-tauri/Cargo.lock')", text)
        self.assertIn("hashFiles('pyproject.toml')", text)
        if shutil.which("actionlint"):
            subprocess.run(["actionlint", *map(str, (self.ROOT / ".github/workflows").glob("*.yml"))], check=True)

    def test_manual_caches_only_store_public_build_inputs(self):
        allowed_paths = {
            "~/.cache/ms-playwright",
            "~/.cache/pip",
            "~/Library/Caches/ms-playwright",
            "~/Library/Caches/pip",
        }
        for name in ["checks.yml", "release-verify.yml", "release.yml"]:
            workflow = self.workflow(name)
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    if not str(step.get("uses", "")).startswith("actions/cache@"):
                        continue
                    with self.subTest(workflow=name, path=step["with"]["path"]):
                        self.assertIn(step["with"]["path"], allowed_paths)
                        self.assertIn("hashFiles(", step["with"]["key"])


if __name__ == "__main__":
    unittest.main()
