import base64
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS, validate
from adaptive_rsi.gitops import git
from adaptive_rsi.release import advance, authorize_workflow, health_check, key, pending, retry, workflow_choices
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

    def test_confirmed_failure_retries_same_workflow_run_and_requires_current_permission(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.assertEqual(self.tick()["release"]["state"], "failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            reruns = [item for item in self.commands if item[0].endswith("/rerun")]
            self.assertEqual(len(reruns), 1)
            self.assertEqual(reruns[0][0], "actions/runs/17/rerun")
            with self.assertRaisesRegex(ValueError, "confirmed failed"):
                retry(self.runtime, self.project, self.run["id"], "release")
            self.assertEqual(sum(item[0].endswith("/rerun") for item in self.commands), 1)
            self.runtime.configure({"delivery": {"release": {"enabled": False}}}, self.project)
            with self.assertRaisesRegex(ValueError, "revoked"):
                retry(self.runtime, self.project, self.run["id"], "release")

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


if __name__ == "__main__":
    unittest.main()
