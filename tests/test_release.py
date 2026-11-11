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
from adaptive_rsi.release import ApiRejected, advance, api as real_api, authorize_workflow, candidates, health_check, key, owner_key, pending, retry, workflow_choices
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
        self.runtime.store.transition(self.run["id"], "merged", {"merged_sha": MERGED, "feedback": {"state": "verified", "head": MERGED}}, True)
        self.run = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (self.run["id"],))
        self.runtime.store.put_setting("delivery:" + self.run["id"], {"repository": "example/project", "branch": "main", "head": "evaluated", "base": "baseline"})
        self.commands = []
        self.remote = None

    def test_missing_remote_run_preserves_identity_without_error_events(self):
        from adaptive_rsi.release import reconcile
        data = {'state': 'rerun_unknown', 'workflow_run_id': 17, 'repository': 'example/project',
                'operation_id': 'fixture-operation', 'previous_attempt': 1, 'rerun_pending': True}
        with patch('adaptive_rsi.release.api', side_effect=ApiRejected('not found', 404)) as api:
            for _ in range(2):
                result = reconcile(self.runtime, self.run, 'release', data, persist=False)
                self.assertEqual(result['workflow_run_id'], 17)
                self.assertEqual(result['state'], 'rerun_unknown')
                self.assertEqual(result['remote_visibility']['state'], 'run_not_visible')
            self.assertEqual(api.call_count, 2)

    def test_missing_workflow_listing_backs_off_and_recovers_without_error_events(self):
        calls = []
        def uncertain(repo, endpoint, method="GET", fields=None):
            result = self.api(repo, endpoint, method, fields)
            if "/dispatches" in endpoint:
                raise RuntimeError("Network disconnected after GitHub accepted request")
            return result
        with patch("adaptive_rsi.release.api", side_effect=uncertain):
            first = self.tick()["release"]
            self.assertEqual(first["state"], "dispatch_unknown")
        events_before = len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'"))
        def missing(repo, endpoint, method="GET", fields=None):
            calls.append(endpoint)
            raise ApiRejected("gh: Not Found (HTTP 404)", 404)
        with patch("adaptive_rsi.release.api", side_effect=missing):
            tick = self.tick()["release"]
            self.assertEqual(tick["state"], "dispatch_unknown")
            self.assertEqual(tick["remote_visibility"]["state"], "workflow_not_visible")
            self.assertEqual(tick["reconciliation"]["state"], "workflow_not_visible")
            calls.clear()
            for _ in range(2):
                again = self.tick()["release"]
                self.assertEqual(again["state"], "dispatch_unknown")
                self.assertEqual(again["reconciliation"]["state"], "workflow_not_visible")
            self.assertEqual(calls, [])
        self.assertEqual(len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")), events_before)
        self.assertTrue(pending(self.runtime, self.run))
        # 退避窗口到期后下一次调度轮询自动恢复；把到期时间归零模拟窗口过去。
        saved = self.runtime.store.setting(key(self.run["id"], "release"))
        saved["remote_visibility"]["next_check_at"] = 0
        self.runtime.store.put_setting(key(self.run["id"], "release"), saved)
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            self.remote = self.remote_run(first)
            result = self.tick()["release"]
            self.assertEqual(result["state"], "workflow_succeeded")
            self.assertNotIn("remote_visibility", result)

    def test_permanent_reconciliation_rejection_blocks_tracking_with_one_error_event(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, state="in_progress")
            self.assertEqual(self.tick()["release"]["state"], "running")
        before = len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'"))
        reads = []
        def forbidden(repo, endpoint, method="GET", fields=None):
            reads.append(endpoint)
            raise ApiRejected("gh: Forbidden (HTTP 403)", 403)
        with patch("adaptive_rsi.release.api", side_effect=forbidden):
            for expected in ["running", "running", "reconciliation_blocked"]:
                self.assertEqual(self.tick()["release"]["state"], expected)
            # 转入终态后不再读取同一个必然失败的远端，也不再写事件。
            self.assertEqual(self.tick()["release"]["state"], "reconciliation_blocked")
        self.assertEqual(reads, ["actions/runs/17"] * 3)
        saved = self.runtime.store.setting(key(self.run["id"], "release"))
        self.assertEqual(saved["state"], "reconciliation_blocked")
        self.assertEqual(saved["advance_failures"], 3)
        self.assertEqual(saved["last_advance_error"], "gh: Forbidden (HTTP 403)")
        self.assertIn("last_advance_error_at", saved)
        self.assertEqual(len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")) - before, 1)
        self.assertFalse(pending(self.runtime, self.run))
        with self.assertRaisesRegex(ValueError, "Reconciliation is blocked"):
            retry(self.runtime, self.project, self.run["id"], "release")

    def test_blocked_reconciliation_recovers_read_only_without_widening_abandon(self):
        from adaptive_rsi.release import ReconciliationRejected, abandon, resolve
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
        data = self.runtime.store.setting(key(self.run["id"], "release"))
        data.update(state="reconciliation_blocked", error="gh: Forbidden (HTTP 403)", advance_failures=3)
        self.runtime.store.put_setting(key(self.run["id"], "release"), data)
        self.remote = self.remote_run(data, state="in_progress")
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            view = candidates(self.runtime, self.project, self.run["id"], "release")
            self.assertEqual(view["state"], "reconciliation_blocked")
            self.assertEqual([item["id"] for item in view["selectable_candidates"]], [17])
            self.assertFalse(view["can_abandon"])
            # 权限恢复后人工选择可见 run 即回到被跟踪的执行态，失败计数从头开始。
            self.assertEqual(resolve(self.runtime, self.project, self.run["id"], "release", 17)["state"], "running")
        saved = self.runtime.store.setting(key(self.run["id"], "release"))
        self.assertEqual(saved["state"], "running")
        for field in ("advance_failures", "last_advance_error", "last_advance_error_at"):
            self.assertNotIn(field, saved)
        # 可见性规则不因新状态放宽：有可见 run 时仍拒绝关闭。
        saved.update(state="reconciliation_blocked", error="gh: Forbidden (HTTP 403)")
        self.runtime.store.put_setting(key(self.run["id"], "release"), saved)
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            with self.assertRaisesRegex(ReconciliationRejected, "still exposes"):
                abandon(self.runtime, self.project, self.run["id"], "release", "operator note")
        self.assertEqual(self.runtime.store.setting(key(self.run["id"], "release"))["state"], "reconciliation_blocked")
        with patch("adaptive_rsi.release.api", side_effect=ApiRejected("gh: Not Found (HTTP 404)", 404)):
            closed = abandon(self.runtime, self.project, self.run["id"], "release", "GitHub access permanently revoked")
        self.assertEqual(closed["state"], "failed")

    def test_corrupt_automation_receipt_is_preserved_and_blocks_dispatch(self):
        from adaptive_rsi.release import status
        for raw in ('{broken', '[]', 'null', '{}', '{"state":3}', '{"state":"submitted"}'):
            with self.subTest(raw=raw):
                self.runtime.store.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                                           (key(self.run['id'], 'release'), raw))
                with patch('adaptive_rsi.release.api') as api:
                    self.assertFalse(pending(self.runtime, self.run))
                    result = self.tick()
                    self.assertEqual(result['release']['receipt_error'], 'invalid_automation_receipt')
                    self.assertEqual(status(self.runtime, self.project, self.run['id'])['release']['state'], 'blocked')
                    api.assert_not_called()
                self.assertEqual(self.runtime.store.one('SELECT value FROM settings WHERE key=?',
                                 (key(self.run['id'], 'release'),))['value'], raw)
        events = self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")
        self.assertEqual(len(events), 1)
        self.runtime.store.put_setting(key(self.run['id'], 'release'), {'state': 'failed'})
        pending(self.runtime, self.run)
        self.runtime.store.execute('UPDATE settings SET value=? WHERE key=?', ('[]', key(self.run['id'], 'release')))
        pending(self.runtime, self.run)
        self.assertEqual(len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")), 2)

    def test_legacy_pin_preserves_cancel_and_backs_off_execution_errors(self):
        from adaptive_rsi.release import _pin_legacy_feedback
        from adaptive_rsi.process import Cancelled
        outcome = {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}
        with patch('adaptive_rsi.gitops.snapshot', side_effect=Cancelled('fixture paused')):
            with self.assertRaises(Cancelled):
                _pin_legacy_feedback(self.runtime, self.run, outcome, MERGED)
        with patch('adaptive_rsi.gitops.snapshot', side_effect=OSError('fixture disk failure')) as snapshot:
            for _ in range(2):
                result = advance(self.runtime, self.runtime.project(self.project), self.run, outcome)
                self.assertEqual(result['release']['state'], 'blocked')
                # 执行异常才归入「本地跑不起来」，并保留类型与摘要。
                self.assertEqual(result['release']['error'],
                                 'Legacy release verification could not run locally: OSError: fixture disk failure')
            self.assertEqual(snapshot.call_count, 1)
        events = self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['message'],
                         'Legacy release verification could not run locally: OSError: fixture disk failure')

    def test_legacy_pin_failed_decision_backs_off_until_retry_at(self):
        # 确定性 decision!="pass" 与执行异常同样走 retry_key 退避：同一 sha 的
        # 固定 checks 靠每分钟重跑翻转只能靠 flaky，不得每 tick 重新完整评估。
        outcome = {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}
        evaluation = {'decision': 'fail'}
        with patch('adaptive_rsi.gitops.snapshot', side_effect=lambda source, folder, sha: folder) as snapshot, \
             patch.object(self.runtime.evaluator, 'evaluate', return_value=evaluation) as evaluate:
            for _ in range(2):
                result = advance(self.runtime, self.runtime.project(self.project), self.run, outcome)
                self.assertEqual(result['release']['state'], 'blocked')
                # 确定性检查失败必须保持检查语义：不能自称本地执行异常，
                # 也不能把内部异常类名写进用户可见文本。
                self.assertEqual(result['release']['error'], 'Legacy release verification did not pass: fail')
                self.assertNotIn('could not run locally', result['release']['error'])
                self.assertNotIn('_PinUnavailable', result['release']['error'])
            # 一次 pin 尝试做 baseline+candidate 两份快照；第二个 tick 被 retry_at
            # 拦截后不得再执行 snapshot/evaluate。
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual(evaluate.call_count, 1)
        events = self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['message'], 'Legacy release verification did not pass: fail')
        deferred = self.runtime.store.setting('legacy-release-pin:' + self.run['id'] + ':' + MERGED)
        self.assertGreater(deferred['retry_at'], time.time())
        self.assertEqual(deferred['error'], 'Legacy release verification did not pass: fail')
        # retry_at 到期后允许再次评估，与 _PinUnavailable deferred 语义一致。
        with patch('adaptive_rsi.release.time.time', return_value=deferred['retry_at'] + 1), \
             patch('adaptive_rsi.gitops.snapshot', side_effect=lambda source, folder, sha: folder) as snapshot, \
             patch.object(self.runtime.evaluator, 'evaluate', return_value=evaluation) as evaluate:
            result = advance(self.runtime, self.runtime.project(self.project), self.run, outcome)
            self.assertEqual(result['release']['state'], 'blocked')
            self.assertIn('did not pass', result['release']['error'])
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual(evaluate.call_count, 1)
        deferred = self.runtime.store.setting('legacy-release-pin:' + self.run['id'] + ':' + MERGED)
        self.assertGreater(deferred['retry_at'], time.time() + 500)

    def test_owner_lookup_migrates_empty_history_only_once(self):
        from adaptive_rsi.release import deployment_owner
        original = self.runtime.store.query
        scans = []
        def query(sql, args=()):
            if "key LIKE 'deployment-owner:%'" in sql:
                scans.append(sql)
            return original(sql, args)
        with patch.object(self.runtime.store, 'query', side_effect=query):
            self.assertIsNone(deployment_owner(self.runtime, self.project, 'example/project', self.policy))
            self.assertIsNone(deployment_owner(self.runtime, self.project, 'example/project', self.policy))
            self.assertEqual(len(scans), 1)
            receipt = {'run': self.run['id'], 'sha': MERGED}
            self.runtime.store.put_setting(owner_key(self.project, 'example/project', self.policy), receipt)
            self.assertEqual(deployment_owner(self.runtime, self.project, 'example/project', self.policy), receipt)

    def test_invalid_deployment_owner_shape_is_treated_as_no_owner(self):
        from adaptive_rsi.release import deployment_owner
        deploy = self.setup_deployment()
        name = owner_key(self.project, "example/project", deploy)
        for raw in ('[1, 2]', '"x"', '{"sha": 1}', '{"run": 123, "sha": "%s"}' % MERGED,
                    '{"run": "run", "sha": 1}'):
            with self.subTest(raw=raw):
                self.runtime.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (name, raw))
                self.assertIsNone(deployment_owner(self.runtime, self.project, "example/project", deploy))
        errors = [json.loads(row["data"])["error_code"] for row in
                  self.runtime.store.query("SELECT data FROM events WHERE kind='automation_error'")]
        self.assertEqual(errors, ["invalid_deployment_owner"])
        # 合法的 run/sha 记录仍原样返回，并清除一次性告警状态。
        self.runtime.store.put_setting(name, {"run": self.run["id"], "sha": MERGED})
        self.assertEqual(deployment_owner(self.runtime, self.project, "example/project", deploy),
                         {"run": self.run["id"], "sha": MERGED})

    def test_advance_and_health_treat_invalid_deployment_owner_as_absent(self):
        from adaptive_rsi.release import deployment_health, deployment_owner
        deploy = self.setup_deployment()
        name = owner_key(self.project, "example/project", deploy)
        self.runtime.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (name, '[1, 2]'))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["deploy"]
        self.assertEqual(first["state"], "submitted")
        # 派发写入修复坏记录，CAS 以“无 owner”为基线而不是静默通过。
        self.assertEqual(deployment_owner(self.runtime, self.project, "example/project", deploy),
                         {"run": self.run["id"], "sha": MERGED})
        self.runtime.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (name, '"x"'))
        data = {"state": "workflow_succeeded", "sha": MERGED, "repository": "example/project",
                "operation_id": first["operation_id"], "workflow": deploy["workflow"],
                "ref": deploy["ref"], "workflow_sha": deploy["sha"]}
        with patch("adaptive_rsi.release.health_check", return_value={"sha": MERGED, "healthy": True}):
            tracked = deployment_health(self.runtime, self.runtime.project(self.project), self.run,
                                        data, deploy, None)
        self.assertEqual(tracked["state"], "healthy")

    def test_retry_with_invalid_deployment_owner_behaves_like_no_owner(self):
        from adaptive_rsi.release import deployment_owner
        deploy = self.setup_deployment()
        def rejected(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("git/ref/tags/"):
                raise ApiRejected("gh: Not Found (HTTP 404)", 404)
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=rejected):
            self.tick()
        name = owner_key(self.project, "example/project", deploy)
        self.runtime.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (name, '{"sha": 1}'))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            retried = retry(self.runtime, self.project, self.run["id"], "deploy")
        self.assertEqual(retried["state"], "submitted")
        self.assertEqual(deployment_owner(self.runtime, self.project, "example/project", deploy),
                         {"run": self.run["id"], "sha": MERGED})

    def test_legacy_deploy_snapshot_keeps_pending_health_tracking(self):
        deploy = {**self.policy, "health_url": "https://service.example/health", "automatic_rollback": False}
        self.runtime.configure({"delivery": {"deploy": deploy}}, self.project)
        saved = self.runtime.config(self.project)
        for field in ("health_attempts", "health_interval_seconds", "health_warmup_seconds"):
            saved["delivery"]["deploy"].pop(field)
        run = {**self.run, "config": encode(saved)}
        self.runtime.store.put_setting(key(run["id"], "deploy"), {"state": "workflow_succeeded"})
        self.assertTrue(pending(self.runtime, run))

    def test_health_retry_clears_stale_rollback_warnings_without_dispatch(self):
        policy = {**self.policy, 'health_url': 'https://service.example/health'}
        self.runtime.configure({'delivery': {'deploy': policy}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.store.execute('UPDATE runs SET config=? WHERE id=?', (encode(config), self.run['id']))
        data = {'state': 'health_failed', 'operation_id': 'fixture-deploy', 'sha': MERGED,
                'repository': 'example/project', 'rollback_unavailable': True,
                'rollback_blocked_reason': 'data_compatibility', 'rollback_recovery_notes': 'stale',
                'rollback_target': {'sha': PREVIOUS}}
        self.runtime.store.put_setting(key(self.run['id'], 'deploy'), data)
        with patch('adaptive_rsi.release.authorized', return_value='example/project'), \
             patch('adaptive_rsi.release.deployment_owner', return_value=None), \
             patch('adaptive_rsi.release._retry_target_allowed', return_value=True), \
             patch('adaptive_rsi.release.api') as remote:
            result = retry(self.runtime, self.project, self.run['id'], 'deploy')
        remote.assert_not_called()
        self.assertEqual(result['state'], 'workflow_succeeded')
        for field in ('rollback_unavailable', 'rollback_blocked_reason', 'rollback_recovery_notes'):
            self.assertNotIn(field, result)
        self.assertEqual(result['rollback_target'], {'sha': PREVIOUS})

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

    def test_local_rate_limit_cooldown_keeps_release_pending_and_dispatches_later(self):
        # 冷却在请求发出前拦下：没有 gh 调用，也不能写成 GitHub 拒绝的终态失败。
        with patch("adaptive_rsi.release._API_BACKOFF", {"github.com": time.monotonic() + 60}), \
                patch("adaptive_rsi.release.checked") as command:
            first = self.tick()["release"]
        self.assertEqual(first["state"], "pending")
        self.assertNotIn("dispatch_receipt", first)
        self.assertNotIn("dispatch_http_status", first)
        self.assertIn("cooldown active", first["error"])
        self.assertGreater(first["retry_at"], time.time())
        self.assertEqual(command.call_count, 0)
        self.assertEqual(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'"), [])
        self.assertTrue(pending(self.runtime, self.run))
        saved = self.runtime.store.setting(key(self.run["id"], "release"))
        self.assertEqual((saved["state"], saved.get("dispatch_receipt")), ("pending", None))
        # 冷却结束后同一个 run 自动完成派发，恰好一次 POST。
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            second = self.tick()["release"]
        self.assertEqual(second["state"], "submitted")
        self.assertEqual(second["operation_id"], saved["operation_id"])
        self.assertEqual(sum(item[0].endswith("/dispatches") for item in self.commands), 1)

    def test_local_rate_limit_cooldown_before_dispatch_post_reverts_to_pending(self):
        # validate_pin 之后才进入冷却：POST 没有发出，必须从 dispatching 退回 pending。
        with patch("adaptive_rsi.release.validate_pin"), \
                patch("adaptive_rsi.release._API_BACKOFF", {"github.com": time.monotonic() + 60}), \
                patch("adaptive_rsi.release.checked") as command:
            first = self.tick()["release"]
        self.assertEqual(first["state"], "pending")
        self.assertNotIn("dispatch_receipt", first)
        self.assertEqual(command.call_count, 0)
        self.assertEqual([item for item in self.commands if "/dispatches" in item[0]], [])
        self.assertTrue(pending(self.runtime, self.run))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            second = self.tick()["release"]
        self.assertEqual(second["state"], "submitted")
        self.assertEqual(sum(item[0].endswith("/dispatches") for item in self.commands), 1)

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
        return advance(self.runtime, self.runtime.project(self.project), self.run, {"merged_sha": MERGED, "feedback": {"state": "verified", "head": MERGED}})

    def remote_run(self, data, state="completed", conclusion="success", attempt=1):
        return {"id": 17, "head_sha": TRUSTED, "display_title": data["operation_id"], "event": "workflow_dispatch",
                "path": data["workflow"], "head_branch": data.get("ref"), "status": state, "conclusion": conclusion,
                "run_attempt": attempt, "html_url": "https://github.com/example/project/actions/runs/17"}

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
            self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
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

    def test_reconciliation_evidence_survives_rerun_polling_that_still_sees_the_old_attempt(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.assertEqual(self.tick()["release"]["state"], "failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            # GitHub 未确认新 attempt：超时后进入 rerun_unknown 的人工对账窗口。
            with patch("adaptive_rsi.release.time.time", return_value=time.time() + 301):
                self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
            # 只读查询读到新 attempt，记录 rerun_confirmed 证据。
            self.remote = self.remote_run(first, attempt=2, state="in_progress")
            view = candidates(self.runtime, self.project, self.run["id"], "release")
            self.assertEqual(view["reconciliation_state"], "rerun_confirmed")
            saved = self.runtime.store.setting(key(self.run["id"], "release"))
            self.assertEqual(saved["reconciliation"]["state"], "rerun_confirmed")
            # 轮询短暂回落到旧 attempt 时，已确认的证据不得被覆写。
            self.remote = self.remote_run(first, conclusion="failure")
            self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
            saved = self.runtime.store.setting(key(self.run["id"], "release"))
            self.assertEqual(saved["reconciliation"]["state"], "rerun_confirmed")
            self.assertEqual([item["id"] for item in saved["reconciliation"]["candidates"]], [17])
            # 新 attempt 再次可见时轮询才推进状态与证据。
            self.remote = self.remote_run(first, attempt=2, state="in_progress")
            self.assertEqual(self.tick()["release"]["state"], "running")
            saved = self.runtime.store.setting(key(self.run["id"], "release"))
            self.assertEqual(saved["reconciliation"]["state"], "matched")

    def test_reconciliation_still_lands_after_evidence_recorded(self):
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()["release"]
            self.remote = self.remote_run(first, conclusion="failure")
            self.assertEqual(self.tick()["release"]["state"], "failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerunning")
            with patch("adaptive_rsi.release.time.time", return_value=time.time() + 301):
                self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
            self.remote = self.remote_run(first, attempt=2, state="in_progress")
            candidates(self.runtime, self.project, self.run["id"], "release")
            saved = self.runtime.store.setting(key(self.run["id"], "release"))
            self.assertEqual(saved["reconciliation"]["state"], "rerun_confirmed")
            self.remote = self.remote_run(first, conclusion="failure")
            with patch("adaptive_rsi.release.time.time", return_value=time.time() + 301):
                self.assertEqual(self.tick()["release"]["state"], "rerun_unknown")
            from adaptive_rsi.dashboard import records
            self.assertFalse(pending(self.runtime, self.run))
            self.assertEqual(records(self.runtime.store, self.project)["tasks"][0]["display_state"], "release_failed")
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "release")["state"], "rerun_unknown")
            self.remote = self.remote_run(first, attempt=2)
            reconciled = retry(self.runtime, self.project, self.run["id"], "release")
            self.assertEqual(reconciled["state"], "workflow_succeeded")
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

    def test_rollback_retry_uses_known_healthy_receipt_instead_of_merge_verification(self):
        from adaptive_rsi.release import save
        self.setup_deployment(rollback=True)
        result = json.loads(self.run["result"])
        result["feedback"] = {"state": "regressed", "head": "d" * 40}
        self.runtime.store.execute("UPDATE runs SET result=? WHERE id=?",
                                   (encode(result), self.run["id"]))
        self.run["result"] = encode(result)
        rollback = {"sha": PREVIOUS, "operation_id": "rsi-operation-rollback",
                    "state": "failed", "workflow_run_id": 17, "attempt": 1,
                    "repository": "example/project", "workflow": self.policy["workflow"],
                    "ref": self.policy["ref"], "workflow_sha": TRUSTED}
        data = {"state": "rollback_failed", "operation_id": "rsi-operation", "sha": MERGED,
                "repository": "example/project", "workflow": self.policy["workflow"],
                "ref": self.policy["ref"], "workflow_sha": TRUSTED,
                "rollback_target": {"sha": PREVIOUS, "run": "previous-run",
                                    "health": {"healthy": True}},
                "rollback": rollback}
        save(self.runtime, self.run, "deploy", data)
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            self.remote = self.remote_run(rollback, conclusion="failure")
            retried = retry(self.runtime, self.project, self.run["id"], "deploy")
        self.assertEqual(retried["rollback"]["state"], "rerunning")
        self.assertTrue(any(item[0].endswith("/rerun") for item in self.commands))

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
        deploy = {**self.policy, "health_url": "https://app.example/health", "automatic_rollback": rollback,
                  "rollback_data_policy": "backward_compatible" if rollback else "manual"}
        self.runtime.configure({"delivery": {"release": {"enabled": False}, "deploy": deploy}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.store.execute("UPDATE runs SET config=? WHERE id=?", (encode(config), self.run["id"]))
        self.run["config"] = encode(config)
        return deploy

    def test_lost_rollback_dispatch_response_preserves_receipt_without_redispatch(self):
        deploy = self.setup_deployment(rollback=True)
        data = {'state': 'health_failed', 'operation_id': 'fixture', 'sha': MERGED,
                'repository': 'example/project', 'workflow': deploy['workflow'], 'ref': deploy['ref'],
                'workflow_sha': deploy['sha'], 'rollback_target': {'sha': PREVIOUS}}
        self.runtime.store.put_setting(key(self.run['id'], 'deploy'), data)
        self.runtime.store.put_setting(owner_key(self.project, 'example/project', deploy),
                                       {'run': self.run['id'], 'sha': MERGED})
        calls = []
        def network(repo, endpoint, method='GET', fields=None):
            if method == 'POST':
                calls.append(endpoint)
                raise OSError('lost response')
            return self.api(repo, endpoint, method, fields)
        with patch('adaptive_rsi.release.validate_pin'), patch('adaptive_rsi.release.api', side_effect=network):
            self.tick()
            saved = self.runtime.store.setting(key(self.run['id'], 'deploy'))
            self.assertEqual(saved['state'], 'rolling_back')
            self.assertEqual(saved['rollback']['state'], 'dispatch_unknown')
            self.tick()
        self.assertEqual(len(calls), 1)

    def test_permanent_rollback_read_failure_blocks_tracking_and_keeps_manual_exits(self):
        from adaptive_rsi.release import abandon, candidates, deployment_terminal, resolve
        deploy = self.setup_deployment(rollback=True)
        rollback = {"state": "running", "operation_id": "fixture-rollback", "sha": PREVIOUS,
                    "repository": "example/project", "workflow": deploy["workflow"], "ref": deploy["ref"],
                    "workflow_sha": deploy["sha"], "workflow_run_id": 18}
        self.runtime.store.put_setting(key(self.run["id"], "deploy"), {
            "state": "rolling_back", "operation_id": "fixture", "sha": MERGED,
            "repository": "example/project", "workflow": deploy["workflow"], "ref": deploy["ref"],
            "workflow_sha": deploy["sha"], "workflow_run_id": 17, "rollback": rollback})
        before = len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'"))
        reads = []
        def forbidden(repo, endpoint, method="GET", fields=None):
            reads.append(endpoint)
            raise ApiRejected("gh: Forbidden (HTTP 403)", 403)
        with patch("adaptive_rsi.release.api", side_effect=forbidden):
            for expected in ("rolling_back", "rolling_back", "reconciliation_blocked"):
                self.assertEqual(self.tick()["deploy"]["state"], expected)
            self.assertFalse(pending(self.runtime, self.run))
            # 转入终态后不再读取同一个必然失败的远端，也不再写事件。
            self.assertEqual(self.tick()["deploy"]["state"], "reconciliation_blocked")
        self.assertEqual(reads, ["actions/runs/18"] * 3)
        saved = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertEqual(saved["state"], "reconciliation_blocked")
        self.assertEqual(saved["advance_failures"], 3)
        self.assertEqual(saved["last_advance_error"], "gh: Forbidden (HTTP 403)")
        self.assertEqual(saved["rollback"]["state"], "reconciliation_blocked")
        self.assertTrue(deployment_terminal(saved, deploy))
        self.assertEqual(len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")) - before, 1)
        # 恢复读取权限后，回滚 run 的只读对账入口可用，而不是只剩 ValueError。
        def readable(repo, endpoint, method="GET", fields=None):
            self.assertEqual(endpoint, "actions/runs/18")
            return {"id": 18, "head_sha": TRUSTED, "display_title": "fixture-rollback",
                    "event": "workflow_dispatch", "path": deploy["workflow"], "head_branch": deploy["ref"],
                    "status": "in_progress", "conclusion": None, "run_attempt": 1}
        with patch("adaptive_rsi.release.api", side_effect=readable):
            view = candidates(self.runtime, self.project, self.run["id"], "deploy", target="rollback")
            self.assertEqual(view["state"], "reconciliation_blocked")
            self.assertEqual([item["id"] for item in view["selectable_candidates"]], [18])
            resolve(self.runtime, self.project, self.run["id"], "deploy", 18, target="rollback")
        resumed = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertEqual(resumed["state"], "rolling_back")
        for field in ("advance_failures", "last_advance_error", "last_advance_error_at"):
            self.assertNotIn(field, resumed)
        # 阻塞状态没有放宽关闭条件：确认远端不可见时人工仍可关闭。
        resumed.update(state="reconciliation_blocked", error="gh: Forbidden (HTTP 403)")
        self.runtime.store.put_setting(key(self.run["id"], "deploy"), resumed)
        with patch("adaptive_rsi.release.api", side_effect=ApiRejected("gh: Not Found (HTTP 404)", 404)):
            closed = abandon(self.runtime, self.project, self.run["id"], "deploy", "GitHub access permanently revoked")
        self.assertEqual(closed["state"], "failed")

    def test_permanent_preflight_failure_stops_polling_and_retry_dispatches_once(self):
        deploy = self.setup_deployment()
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": "previous-run", "sha": PREVIOUS})
        self.runtime.store.put_setting(key("previous-run", "deploy"), {"state": "healthy", "sha": PREVIOUS})
        before = len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'"))
        reads = []
        def forbidden(repo, endpoint, method="GET", fields=None):
            reads.append(endpoint)
            if endpoint.startswith("compare/"):
                raise ApiRejected("gh: Forbidden (HTTP 403)", 403)
            return self.api(repo, endpoint, method, fields)
        # 派发前的永久 403 与前两轮的对账失败共用同一预算：3 轮后停止轮询。
        with patch("adaptive_rsi.release.api", side_effect=forbidden):
            for expected in ("blocked", "blocked", "failed"):
                self.assertEqual(self.tick()["deploy"]["state"], expected)
            self.assertEqual(reads, ["compare/" + PREVIOUS + "..." + MERGED] * 3)
            self.assertFalse(pending(self.runtime, self.run))
            # 达到上限后不再调用 GitHub，也不再写事件。
            reads.clear()
            for _ in range(2):
                self.assertEqual(self.tick()["deploy"]["state"], "failed")
        self.assertEqual(reads, [])
        saved = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["advance_failures"], 3)
        self.assertEqual(saved["last_advance_error"], "gh: Forbidden (HTTP 403)")
        self.assertEqual(saved["error"], "gh: Forbidden (HTTP 403)")
        self.assertEqual(saved["dispatch_receipt"], "preflight_error")
        self.assertEqual(len(self.runtime.store.query("SELECT * FROM events WHERE kind='automation_error'")) - before, 1)
        # 权限恢复后 retry 清零预算并放回待发，只发起一次新的派发。
        self.commands.clear()
        def restored(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                return {"status": "ahead", "ahead_by": 1}
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=restored):
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "deploy")["state"], "pending")
            self.assertEqual(self.tick()["deploy"]["state"], "submitted")
            self.assertEqual(self.tick()["deploy"]["state"], "submitted")
        self.assertEqual(sum(method == "POST" and "/dispatches" in endpoint
                             for endpoint, method, _ in self.commands), 1)
        resumed = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertNotIn("advance_failures", resumed)

    def test_preflight_cooldown_does_not_consume_failure_budget(self):
        deploy = self.setup_deployment()
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": "previous-run", "sha": PREVIOUS})
        self.runtime.store.put_setting(key("previous-run", "deploy"), {"state": "healthy", "sha": PREVIOUS})
        def cooldown(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                raise ApiRejected("GitHub rate limit cooldown active; request was not sent", 429, local=True)
            return self.api(repo, endpoint, method, fields)
        # 本机冷却没有发出请求：连续多轮也不能把回执推成永久失败。
        with patch("adaptive_rsi.release.api", side_effect=cooldown):
            for _ in range(4):
                self.assertIn(self.tick()["deploy"]["state"], {"pending", "blocked"})
        saved = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertNotIn("advance_failures", saved)
        self.assertTrue(pending(self.runtime, self.run))
        def restored(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                return {"status": "ahead"}
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=restored):
            self.assertEqual(self.tick()["deploy"]["state"], "submitted")

    def test_health_exception_does_not_modify_concurrent_receipt(self):
        deploy = self.setup_deployment(rollback=True)
        name = key(self.run['id'], 'deploy')
        original = {'state': 'health_failed', 'operation_id': 'old', 'sha': MERGED,
                    'repository': 'example/project', 'workflow': deploy['workflow'], 'ref': deploy['ref'],
                    'workflow_sha': deploy['sha'], 'rollback_target': {'sha': PREVIOUS}}
        replacement = {'state': 'healthy', 'operation_id': 'new', 'error': 'new-error'}
        self.runtime.store.put_setting(name, original)
        def concurrent(runtime, project, run, data, policy, cancel, persist):
            runtime.store.put_setting(name, replacement)
            raise OSError('old-operation-error')
        with patch('adaptive_rsi.release.deployment_health', side_effect=concurrent):
            self.tick()
        self.assertEqual(self.runtime.store.setting(name), replacement)

    def test_rejected_preflight_leaves_waiting_release_with_failed_state_and_manual_entry(self):
        self.runtime.configure({"delivery": {"deploy": self.policy}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.store.execute("UPDATE runs SET config=? WHERE id=?", (encode(config), self.run["id"]))
        self.run["config"] = encode(config)
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()
            self.assertEqual(first["deploy"]["state"], "waiting_release")
            self.remote = self.remote_run(first["release"])
            owner = owner_key(self.project, "example/project", self.policy)
            self.runtime.store.put_setting(owner, {"run": self.run["id"], "sha": MERGED})
            # Trusted tag lookup 被显式拒绝（404）：dispatch 前的 validate_pin 失败。
            def rejected(repo, endpoint, method="GET", fields=None):
                if endpoint.startswith("git/ref/tags/"):
                    raise ApiRejected("gh: Not Found (HTTP 404)", 404)
                return self.api(repo, endpoint, method, fields)
            with patch("adaptive_rsi.release.api", side_effect=rejected):
                self.tick()
        saved = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["dispatch_receipt"], "rejected")
        self.assertEqual(saved["dispatch_http_status"], 404)
        self.assertTrue(saved.get("operation_id"))
        from adaptive_rsi.dashboard import records
        self.assertFalse(pending(self.runtime, self.run))
        self.assertEqual(records(self.runtime.store, self.project)["tasks"][0]["display_state"], "deploy_failed")
        # 人工介入入口：retry 按被拒回执重新派发，且只重发一次。
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            self.assertEqual(retry(self.runtime, self.project, self.run["id"], "deploy")["state"], "submitted")
        self.assertEqual(sum("/dispatches" in item[0] for item in self.commands), 2)

    def test_rejected_preflight_release_failure_is_terminal_for_its_own_run(self):
        self.runtime.configure({"delivery": {"deploy": self.policy}}, self.project)
        config = self.runtime.config(self.project)
        self.runtime.store.execute("UPDATE runs SET config=? WHERE id=?", (encode(config), self.run["id"]))
        self.run["config"] = encode(config)
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            first = self.tick()
            self.assertEqual(first["deploy"]["state"], "waiting_release")
            self.remote = self.remote_run(first["release"])
            owner = owner_key(self.project, "example/project", self.policy)
            self.runtime.store.put_setting(owner, {"run": self.run["id"], "sha": MERGED})
            def rejected(repo, endpoint, method="GET", fields=None):
                if endpoint.startswith("git/ref/tags/"):
                    raise ApiRejected("gh: Not Found (HTTP 404)", 404)
                return self.api(repo, endpoint, method, fields)
            with patch("adaptive_rsi.release.api", side_effect=rejected):
                self.tick()
        saved = self.runtime.store.setting(key(self.run["id"], "deploy"))
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["dispatch_receipt"], "rejected")
        self.assertTrue(saved.get("operation_id"))
        from adaptive_rsi.dashboard import records
        self.assertFalse(pending(self.runtime, self.run))
        self.assertEqual(records(self.runtime.store, self.project)["tasks"][0]["display_state"], "deploy_failed")
        # 终态失败只属于本 run：release 仍保持成功状态，不被覆盖。
        self.assertEqual(self.runtime.store.setting(key(self.run["id"], "release"))["state"], "workflow_succeeded")

    def test_retry_rejected_preflight_without_owner_acquires_deployment_ownership(self):
        from adaptive_rsi.release import deployment_owner
        deploy = self.setup_deployment()
        def rejected(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("git/ref/tags/"):
                raise ApiRejected("gh: Not Found (HTTP 404)", 404)
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=rejected):
            self.tick()
        self.assertIsNone(deployment_owner(self.runtime, self.project, "example/project", deploy))
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            retried = retry(self.runtime, self.project, self.run["id"], "deploy")
        self.assertEqual(retried["state"], "submitted")
        self.assertEqual(deployment_owner(self.runtime, self.project, "example/project", deploy),
                         {"run": self.run["id"], "sha": MERGED})

    def test_retry_rejected_preflight_with_terminal_owner_and_ahead_transfers_ownership(self):
        from adaptive_rsi.release import deployment_owner
        deploy = self.setup_deployment()
        def rejected(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("git/ref/tags/"):
                raise ApiRejected("gh: Not Found (HTTP 404)", 404)
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=rejected):
            self.tick()
        previous_run = "previous-run"
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": previous_run, "sha": PREVIOUS})
        self.runtime.store.put_setting(key(previous_run, "deploy"), {"state": "healthy"})
        def api_ahead(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                return {"status": "ahead"}
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=api_ahead):
            retried = retry(self.runtime, self.project, self.run["id"], "deploy")
        self.assertEqual(retried["state"], "submitted")
        self.assertEqual(deployment_owner(self.runtime, self.project, "example/project", deploy),
                         {"run": self.run["id"], "sha": MERGED})

    def test_health_unconfirmed_owner_allows_compare_and_takeover_by_newer_deployment(self):
        from adaptive_rsi.release import deployment_terminal
        deploy = self.setup_deployment()
        self.assertTrue(deployment_terminal({"state": "health_unconfirmed"}, deploy))
        previous = "previous-run"
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": previous, "sha": PREVIOUS})
        self.runtime.store.put_setting(key(previous, "deploy"),
                                       {"state": "health_unconfirmed", "sha": PREVIOUS, "repository": "example/project",
                                        "created_at": time.time() - 60})
        compares = []
        def api_ahead(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                compares.append(endpoint)
                return {"status": "ahead"}
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=api_ahead):
            result = self.tick()["deploy"]
        self.assertEqual(result["state"], "submitted")
        self.assertEqual(compares, ["compare/" + PREVIOUS + "..." + MERGED])

    def test_rollback_health_unconfirmed_target_wait_is_bounded(self):
        from adaptive_rsi.release import TARGET_WAIT_SECONDS
        deploy = self.setup_deployment()
        previous = "previous-run"
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": previous, "sha": PREVIOUS})
        stale = {"state": "rollback_health_unconfirmed", "sha": PREVIOUS, "repository": "example/project",
                 "rollback": {"state": "workflow_succeeded", "sha": PREVIOUS, "health_started_at": time.time() - 60}}
        self.runtime.store.put_setting(key(previous, "deploy"), stale)
        with patch("adaptive_rsi.release.api", side_effect=AssertionError("target wait must not read GitHub")):
            waiting = self.tick()["deploy"]
        self.assertEqual(waiting["state"], "waiting_target")
        self.assertEqual(waiting["blocked_by_target"], {"run": previous, "state": "rollback_health_unconfirmed"})
        stale["rollback"]["health_started_at"] = time.time() - TARGET_WAIT_SECONDS - 60
        self.runtime.store.put_setting(key(previous, "deploy"), stale)
        with patch("adaptive_rsi.release.api", side_effect=AssertionError("target wait must not read GitHub")):
            blocked = self.tick()["deploy"]
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn(previous, blocked["error"])
        self.assertIn("automatic tracking of that receipt has stopped", blocked["error"])

    def test_receipt_state_table_drives_pending_and_deployment_ownership(self):
        from adaptive_rsi.release import AUTOMATION_STATES, automation_state_flag, deployment_terminal
        deploy = self.setup_deployment()
        identity = {"repository": "example/project", "workflow": deploy["workflow"], "ref": deploy["ref"],
                    "workflow_sha": deploy["sha"], "sha": MERGED, "operation_id": "fixture"}
        for state in AUTOMATION_STATES:
            receipt = {"state": state, **identity}
            if state == "rolling_back":
                receipt["rollback"] = {"state": "running", **identity}
            self.runtime.store.put_setting(key(self.run["id"], "deploy"), receipt)
            with self.subTest(state=state):
                self.assertEqual(pending(self.runtime, self.run),
                                 not automation_state_flag(receipt, "terminal", deploy))
                self.assertEqual(deployment_terminal(receipt, deploy),
                                 automation_state_flag(receipt, "ownership_terminal", deploy))
        self.assertTrue(deployment_terminal({"state": "health_unconfirmed"}, deploy))
        self.assertFalse(deployment_terminal({"state": "rollback_health_unconfirmed"}, deploy))

    def test_retry_rejected_preflight_refuses_when_another_deployment_active(self):
        deploy = self.setup_deployment()
        def rejected(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("git/ref/tags/"):
                raise ApiRejected("gh: Not Found (HTTP 404)", 404)
            return self.api(repo, endpoint, method, fields)
        with patch("adaptive_rsi.release.api", side_effect=rejected):
            self.tick()
        active_run = "active-run"
        self.runtime.store.put_setting(owner_key(self.project, "example/project", deploy),
                                       {"run": active_run, "sha": PREVIOUS})
        self.runtime.store.put_setting(key(active_run, "deploy"), {"state": "dispatching", "repository": "example/project",
            "workflow": deploy["workflow"], "ref": deploy["ref"], "workflow_sha": deploy["sha"],
            "sha": PREVIOUS, "operation_id": "active-fixture"})
        self.commands.clear()
        with patch("adaptive_rsi.release.api", side_effect=self.api):
            with self.assertRaisesRegex(ValueError, "Another deployment is still active on this target"):
                retry(self.runtime, self.project, self.run["id"], "deploy")
        self.assertFalse(any("/dispatches" in item[0] for item in self.commands))

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
        from adaptive_rsi.release import owner_key, stable_key
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
        owner = self.runtime.store.setting(owner_key(self.project, "example/project", deploy))
        self.assertEqual(owner["sha"], PREVIOUS)
        self.assertEqual(owner["run"], self.run["id"])
        stable = self.runtime.store.setting(stable_key(self.project, "example/project", deploy["health_url"]))
        self.assertEqual(stable["sha"], PREVIOUS)
        self.assertEqual(stable["run"], self.run["id"])
        self.assertTrue(stable["health"]["healthy"])

    def test_rollback_updates_deployment_owner_and_allows_subsequent_advance_without_superseded(self):
        from adaptive_rsi.release import owner_key, stable_key
        deploy = self.setup_deployment(rollback=True)
        self.runtime.store.put_setting(stable_key(self.project, "example/project", deploy["health_url"]), {"sha": PREVIOUS})
        with patch("adaptive_rsi.release.api", side_effect=self.api), patch("adaptive_rsi.release.health_check", side_effect=lambda url, sha: {"healthy": sha == PREVIOUS, "sha": sha}):
            first = self.tick()["deploy"]
            self.remote = self.remote_run(first)
            failed = self.tick()["deploy"]
            self.assertEqual(failed["state"], "rolling_back")
            rollback = failed["rollback"]
            self.remote = self.remote_run(rollback)
            rolled = self.tick()["deploy"]
            self.assertEqual(rolled["state"], "rolled_back")

        # 1. 验证 deployment_owner 更新为回滚目标提交
        owner = self.runtime.store.setting(owner_key(self.project, "example/project", deploy))
        self.assertEqual(owner["sha"], PREVIOUS)
        self.assertEqual(owner["run"], self.run["id"])

        # 2. 验证 stable_key 同步更新为回滚提交与健康凭证
        stable = self.runtime.store.setting(stable_key(self.project, "example/project", deploy["health_url"]))
        self.assertEqual(stable["sha"], PREVIOUS)
        self.assertEqual(stable["run"], self.run["id"])
        self.assertTrue(stable["health"]["healthy"])

        # 3. 后续演进运行 Run 2（基于回滚基准提交 PREVIOUS 分支）
        NEXT = "d" * 40
        self.runtime.goal(self.project, "Subsequent evolution commit")
        run2 = self.runtime.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.store.transition(run2["id"], "merged", {"merged_sha": NEXT, "feedback": {"state": "verified", "head": NEXT}}, True)
        run2 = self.runtime.store.one("SELECT * FROM runs WHERE id=?", (run2["id"],))
        self.runtime.store.put_setting("delivery:" + run2["id"], {"repository": "example/project", "branch": "main", "head": "evaluated", "base": "baseline"})

        compare_endpoints = []
        def api_with_compare(repo, endpoint, method="GET", fields=None):
            if endpoint.startswith("compare/"):
                compare_endpoints.append(endpoint)
                base, head = endpoint[len("compare/"):].split("...")
                self.assertEqual(base, PREVIOUS)
                self.assertEqual(head, NEXT)
                return {"status": "ahead"}
            return self.api(repo, endpoint, method, fields)

        with patch("adaptive_rsi.release.api", side_effect=api_with_compare):
            run2_result = advance(self.runtime, self.runtime.project(self.project), run2,
                                  {"merged_sha": NEXT, "feedback": {"state": "verified", "head": NEXT}})

        self.assertEqual(compare_endpoints, [f"compare/{PREVIOUS}...{NEXT}"])
        self.assertEqual(run2_result["deploy"]["state"], "submitted")
        new_owner = self.runtime.store.setting(owner_key(self.project, "example/project", deploy))
        self.assertEqual(new_owner["sha"], NEXT)
        self.assertEqual(new_owner["run"], run2["id"])

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

    def test_failed_release_blocks_deployment_without_pending_forever(self):
        self.runtime.configure({"delivery": {"deploy": self.policy}}, self.project)
        self.run['config'] = encode(self.runtime.config(self.project))
        self.runtime.store.put_setting(key(self.run['id'], 'release'), {'state': 'failed', 'error': 'fixture failure'})
        with patch('adaptive_rsi.release.api') as api:
            result = self.tick()
        self.assertEqual(result['deploy']['state'], 'blocked')
        self.assertEqual(result['deploy']['error'], 'Release workflow failed; retry release before deployment')
        self.assertFalse(pending(self.runtime, self.run))
        api.assert_not_called()
        self.runtime.store.put_setting(key(self.run['id'], 'release'), {'state': 'rerun_unknown',
            'repository': 'example/project', 'workflow': self.policy['workflow'], 'ref': self.policy['ref'],
            'workflow_sha': self.policy['sha'], 'sha': MERGED, 'operation_id': 'release-fixture'})
        with patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data):
            uncertain = self.tick()
        self.assertEqual(uncertain['deploy']['error'], 'Release rerun result is unconfirmed; reconcile release before deployment')
        self.runtime.store.put_setting(key(self.run['id'], 'release'), {'state': 'submitted',
            'repository': 'example/project', 'workflow': self.policy['workflow'], 'ref': self.policy['ref'],
            'workflow_sha': self.policy['sha'], 'sha': MERGED, 'operation_id': 'release-fixture'})
        with patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data):
            result = self.tick()
        self.assertEqual(result['deploy']['state'], 'waiting_release')
        self.assertEqual(result['deploy']['error'], '')
        self.assertTrue(pending(self.runtime, self.run))

    def test_revoked_release_blocks_deployment_and_resumes_after_restoring_authorization(self):
        self.runtime.configure({'delivery': {'deploy': self.policy}}, self.project)
        self.run['config'] = encode(self.runtime.config(self.project))
        self.runtime.configure({'delivery': {'release': {'enabled': False}}}, self.project)
        with patch('adaptive_rsi.release.api') as api:
            result = self.tick()
        self.assertEqual(result['release']['state'], 'blocked')
        self.assertEqual(result['deploy']['state'], 'blocked')
        self.assertTrue(result['deploy']['blocked_by_release'])
        self.assertFalse(pending(self.runtime, self.run))
        api.assert_not_called()
        self.runtime.configure({'delivery': {'release': self.policy}}, self.project)
        self.assertTrue(pending(self.runtime, self.run))
        with patch('adaptive_rsi.release.api', side_effect=self.api):
            result = self.tick()
        self.assertEqual(result['release']['state'], 'submitted')
        self.assertEqual(result['deploy']['state'], 'waiting_release')

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
            branch = 'chore/release-v1.3.0-' + base[:12]
            self.assertEqual(module.desktop_mode(base, head, branch, 'pull_request'), 'skip')
            self.assertEqual(module.desktop_mode(base, head, 'feature/version', 'pull_request'), 'full')
            self.assertEqual(module.desktop_mode(base, head, branch, 'workflow_dispatch'), 'full')
            self.assertIn("## Unreleased\n\n## 1.3.0", result["CHANGELOG.md"])
            self.assertIn("中文功能", result["CHANGELOG.md"])
            (root / "manual.py").write_text("print('unreviewed')")
            git(root, "add", ".")
            git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "manual")
            with self.assertRaisesRegex(ValueError, "independently reconstructed"):
                module.verify_generated(base, git(root, "rev-parse", "HEAD"))

class ReleaseVerificationDispatchTest(unittest.TestCase):
    def test_local_cli_delegates_prepare_to_canonical_workflow(self):
        from scripts import release as module
        with patch.dict(os.environ, {'PREPARE_RELEASE': 'true', 'GITHUB_ACTIONS': 'false',
                'RELEASE_LEVEL': 'minor', 'NEW_PRODUCT_STAGE': 'true',
                'GITHUB_REPOSITORY': 'example/project'}, clear=True), \
                patch.object(sys, 'argv', ['release.py', 'prepare']), \
                patch.object(module, 'run', return_value='') as command, \
                patch.object(module, 'prepare') as prepare, \
                patch.object(module, 'ensure_release_verification') as ensure, \
                patch.object(module, 'gh') as api, patch.object(module, 'output') as output:
            module.main()
        command.assert_called_once_with('gh', 'workflow', 'run', 'release.yml', '--repo',
                'example/project', '--ref', 'main', '-f', 'prepare_release=true',
                '-f', 'release_level=minor', '-f', 'new_product_stage=true')
        prepare.assert_not_called()
        ensure.assert_not_called()
        api.assert_not_called()
        self.assertEqual(output.call_args.kwargs['mode'], 'requested')

    def test_local_cli_dispatch_failure_is_unknown_without_retry(self):
        from scripts import release as module
        for error in (subprocess.CalledProcessError(1, ['gh']), OSError('connection lost')):
            with self.subTest(error=type(error).__name__), \
                    patch.dict(os.environ, {'PREPARE_RELEASE': 'true',
                        'GITHUB_REPOSITORY': 'example/project'}, clear=True), \
                    patch.object(sys, 'argv', ['release.py', 'prepare']), \
                    patch.object(module, 'run', side_effect=error) as command, \
                    patch.object(module, 'prepare') as prepare, \
                    patch.object(module, 'output') as output:
                module.main()
            command.assert_called_once_with('gh', 'workflow', 'run', 'release.yml', '--repo',
                    'example/project', '--ref', 'main', '-f', 'prepare_release=true',
                    '-f', 'release_level=patch', '-f', 'new_product_stage=false')
            prepare.assert_not_called()
            self.assertEqual(output.call_args.kwargs['mode'], 'dispatch_unknown')

    def test_local_cli_validates_release_selection_before_dispatch(self):
        from scripts import release as module
        for level, stage in [('surprise', 'false'), ('minor', 'false'),
                             ('major', 'false'), ('patch', 'invalid')]:
            with self.subTest(level=level, stage=stage), \
                    patch.dict(os.environ, {'PREPARE_RELEASE': 'true',
                        'RELEASE_LEVEL': level, 'NEW_PRODUCT_STAGE': stage}, clear=True), \
                    patch.object(sys, 'argv', ['release.py', 'prepare']), \
                    patch.object(module, 'run') as command, \
                    patch.object(module, 'prepare') as prepare:
                with self.assertRaises(ValueError):
                    module.main()
            command.assert_not_called()
            prepare.assert_not_called()

    def test_remote_and_unrequested_cli_keep_prepare_path(self):
        from scripts import release as module
        for env in ({'GITHUB_ACTIONS': 'true', 'PREPARE_RELEASE': 'true'}, {}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, 'argv', ['release.py', 'prepare']), \
                    patch.object(module, 'run') as command, \
                    patch.object(module, 'prepare') as prepare:
                module.main()
            prepare.assert_called_once_with()
            command.assert_not_called()

    def test_dispatch_ignores_other_sha_and_verifies_saved_receipt(self):
        from scripts import release as module
        sha = 'c' * 40
        receipt, posts = {}, []
        def api(endpoint, method='GET', **fields):
            if endpoint.startswith('actions/workflows/release-verify.yml/runs?'):
                return {'workflow_runs': [{'id': 99, 'path': '.github/workflows/release-verify.yml',
                        'event': 'workflow_dispatch', 'head_branch': 'main', 'status': 'completed',
                        'conclusion': 'success', 'display_title': module.release_verification_title(9, 'd' * 40)}]}
            if endpoint.startswith('commits/'):
                return {'check_runs': [receipt] if receipt else []}
            if endpoint == 'check-runs':
                receipt.update(fields, id=17)
                return receipt
            if endpoint == 'check-runs/17':
                return receipt
            posts.append((endpoint, method, fields))
            return {}
        with patch.object(module, 'gh', side_effect=api) as calls:
            result = module.ensure_release_verification(9, sha)
        self.assertEqual(result['verification_state'], 'dispatched')
        self.assertTrue(any(call.args == ('check-runs/17',) for call in calls.call_args_list))
        self.assertEqual(len(posts), 1)

    def test_receipt_identity_readback_failure_blocks_dispatch(self):
        from scripts import release as module
        with patch.object(module, 'gh', side_effect=[{'workflow_runs': []},
                {'check_runs': []}, {'id': 17}, {'name': 'wrong'}]) as api:
            with self.assertRaisesRegex(ValueError, 'receipt identity'):
                module.ensure_release_verification(9, 'c' * 40)
        self.assertEqual(api.call_count, 4)
        self.assertFalse(any(call.args[0].endswith('/dispatches') for call in api.call_args_list))

    def test_unknown_or_incomplete_run_listing_cannot_authorize_dispatch(self):
        from scripts import release as module
        with patch.object(module, 'gh', return_value={}) as api:
            with self.assertRaisesRegex(ValueError, 'listing'):
                module.ensure_release_verification(9, 'c' * 40)
        self.assertEqual(api.call_count, 1)

    def test_exact_sha_active_success_and_failed_runs_never_redispatch(self):
        from scripts import release as module
        sha = 'c' * 40
        for status, conclusion, expected in [('queued', None, 'active'),
                ('in_progress', None, 'active'), ('completed', 'success', 'success'),
                ('completed', 'failure', 'failed'), ('completed', 'cancelled', 'failed')]:
            with self.subTest(status=status, conclusion=conclusion):
                item = {'id': 42, 'path': '.github/workflows/release-verify.yml',
                        'event': 'workflow_dispatch', 'head_branch': 'main', 'status': status,
                        'conclusion': conclusion, 'display_title': module.release_verification_title(9, sha)}
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/project'}), \
                        patch.object(module, 'gh', return_value={'workflow_runs': [item]}) as api:
                    result = module.ensure_release_verification(9, sha)
                self.assertEqual(result['verification_state'], expected)
                self.assertEqual(api.call_count, 1)
                if expected == 'failed':
                    self.assertEqual(result['verification_retry'], 'gh run rerun 42 --repo example/project')

    def test_prepare_dispatches_once_and_keeps_unknown_receipt_across_retries(self):
        from scripts import release as module
        sha = 'c' * 40
        receipts, dispatches = [], []
        def api(endpoint, method='GET', **fields):
            if endpoint.startswith('actions/workflows/release-verify.yml/runs?'):
                return {'workflow_runs': []}
            if endpoint.startswith('commits/' + sha + '/check-runs?'):
                return {'check_runs': receipts}
            if endpoint == 'check-runs' and method == 'POST':
                receipts.append(dict(fields, id=17))
                return receipts[-1]
            if endpoint == 'check-runs/17':
                return receipts[-1]
            if endpoint.endswith('/dispatches'):
                dispatches.append(fields)
                raise subprocess.CalledProcessError(1, ['gh'])
            raise AssertionError((endpoint, method, fields))
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example/project'}), \
                patch.object(module, 'gh', side_effect=api):
            first = module.ensure_release_verification(9, sha)
            second = module.ensure_release_verification(9, sha)
        self.assertEqual(first['verification_state'], 'dispatch_unknown')
        self.assertEqual(second['verification_state'], 'dispatch_unknown')
        self.assertEqual(dispatches, [{'ref': 'main', 'inputs[pr]': '9', 'inputs[sha]': sha}])
        self.assertEqual(receipts[0]['head_sha'], sha)
        self.assertEqual(receipts[0]['status'], 'completed')
        self.assertEqual(receipts[0]['conclusion'], 'neutral')
        self.assertEqual(receipts[0]['external_id'], module.release_verification_title(9, sha))


class ReleaseBatchCadenceTest(unittest.TestCase):
    def prepare(self, requested='false', notes='## Unreleased\n', level='patch', generated=False, new_stage='false', artifact_error=None):
        from scripts import release as module
        head='b'*40
        def command(*args, **kwargs):
            if args[:2]==('git','rev-parse'):
                return head
            if args[:3]==('gh','pr','list'):
                return '[]' if 'open' in args else json.dumps([{'number':9,'state':'OPEN','headRefOid':'c'*40}])
            raise AssertionError('Unexpected side effect: '+repr(args))
        def api(endpoint, method='GET', **fields):
            if endpoint.startswith('actions/workflows/release-verify.yml/runs?'):
                return {'workflow_runs': [{'id': 42, 'path': '.github/workflows/release-verify.yml',
                        'event': 'workflow_dispatch', 'head_branch': 'main', 'status': 'queued',
                        'display_title': module.release_verification_title(9, 'c' * 40)}]}
            return {'default_branch':'main'} if not endpoint else {'sha':head}
        def read(sha,name):
            return '__version__ = "1.2.3"\n' if name.endswith('__init__.py') else notes
        with patch.dict(os.environ,{'PREPARE_RELEASE':requested,'RELEASE_LEVEL':level,
                                    'NEW_PRODUCT_STAGE':new_stage,'GITHUB_REPOSITORY':'example/project'}),patch.object(module,'run',side_effect=command) as calls,patch.object(module,'gh',side_effect=api),patch.object(module,'read_at',side_effect=read),patch.object(module,'verify_generated',side_effect=None if generated else ValueError('not mechanical')),patch.object(module,'tree_at',return_value='d'*40),patch.object(module,'find_verified_artifact',side_effect=artifact_error,return_value={'run_id':17,'name':'release-verified-'+('d'*40)}),patch.object(module,'output') as output:
            module.prepare()
            return output.call_args.kwargs,calls.call_args_list

    def test_ordinary_merges_never_bump_or_create_pr(self):
        result,calls=self.prepare()
        self.assertEqual(result['mode'],'idle')
        self.assertEqual(len(calls),1)

    def test_prepare_owns_verification_dispatch(self):
        from scripts import release as module
        with patch.object(module, 'ensure_release_verification', return_value={
                'verification_state': 'active', 'verification_run_id': 42}) as dispatch:
            result, _ = self.prepare(requested='true')
        dispatch.assert_called_once_with(9, 'c' * 40)
        self.assertEqual(result['verification_state'], 'active')
        self.assertEqual(result['verification_run_id'], 42)

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

    def test_prepare_reports_artifact_path_in_summary(self):
        from scripts import release as module
        notes = '<!-- adaptive-rsi-release:1.2.3;base:' + ('a' * 40) + ' -->'
        with tempfile.TemporaryDirectory() as temp:
            summary = Path(temp) / 'summary.md'
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}):
                result, _ = self.prepare(notes=notes, generated=True)
            self.assertEqual(result['release_path'], 'reuse_candidate')
            self.assertEqual(result['artifact_reuse_reason'], 'available')
            self.assertIn('reuse_candidate', summary.read_text())
            self.assertIn('available', summary.read_text())
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}):
                result, _ = self.prepare(notes=notes, generated=True,
                                         artifact_error=subprocess.CalledProcessError(1, ['gh']))
            self.assertEqual(result['release_path'], 'rebuild')
            self.assertEqual(result['artifact_reuse_reason'], 'lookup_error')
            self.assertIn('lookup_error', summary.read_text())

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
    def test_malformed_receipt_cannot_unlock_retry(self):
        from adaptive_rsi import release
        self.assertEqual(release.verified_feedback({"result": "{"}), {})
        self.assertEqual(release.verified_feedback({"result": '{"feedback":"bad"}'}), {})
        self.assertEqual(release.verified_feedback({"result": '{"feedback":{"state":"verified"}}'})["state"], "verified")

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

    def test_artifact_lookup_reports_missing_expired_untrusted_and_available(self):
        from scripts import release as module
        tree = 'a' * 40
        name = 'release-verified-' + tree
        for artifacts, run_info, expected in [
                ([], {}, 'missing'),
                ([{'id': 1, 'name': name, 'expired': True}], {}, 'expired'),
                ([{'id': 1, 'name': name, 'workflow_run': {'id': 17}}],
                 {'path': '.github/workflows/checks.yml'}, 'untrusted'),
                ([{'id': 1, 'name': name, 'workflow_run': {'id': 17}}],
                 {'path': '.github/workflows/release-verify.yml', 'event': 'workflow_dispatch',
                  'status': 'completed', 'conclusion': 'success', 'head_branch': 'main'}, 'available'),
                # The merge job may continue publication while the producer run
                # is still finishing, as long as its artifact was uploaded.
                ([{'id': 1, 'name': name, 'workflow_run': {'id': 17}}],
                 {'path': '.github/workflows/release-verify.yml', 'event': 'workflow_dispatch',
                  'status': 'in_progress', 'conclusion': None, 'head_branch': 'main'}, 'available')]:
            with self.subTest(expected=expected), patch.object(module, 'gh', side_effect=[
                    {'artifacts': artifacts}, run_info]):
                details = {}
                module.find_verified_artifact(tree, 'main', details)
                self.assertEqual(details['artifact_reuse_reason'], expected)

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
        self.assertEqual(jobs["browser"]["runs-on"], "macos-latest")
        self.assertEqual(jobs["macos_sandbox_core"]["runs-on"], "macos-latest")
        self.assertEqual(jobs["macos_sandbox_flows"]["runs-on"], "macos-latest")
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

    def test_version_metadata_cannot_supply_required_release_verify_check(self):
        checks = self.workflow('checks.yml')['jobs']['verify']
        self.assertEqual(checks.get('name'), "${{ needs.changes.outputs.verification_mode == 'version' && 'version_metadata_summary' || 'verify' }}")
        workflow = self.workflow('release-verify.yml')
        report = workflow['jobs']['report']
        self.assertEqual(set(report['needs']), {'gate', 'tests', 'browser', 'verify'})
        script = report['steps'][0]['run']
        self.assertIn('-f name=verify', script)
        self.assertIn('-f head_sha="$PR_SHA"', script)
        self.assertEqual(report['steps'][0]['env']['PR_SHA'], '${{ needs.gate.outputs.sha }}')
        self.assertEqual(set(workflow['jobs']['merge']['needs']), {'gate', 'tests', 'browser', 'verify', 'report'})

    def test_release_verification_attests_and_uploads_exact_tree_artifacts(self):
        workflow = self.workflow("release-verify.yml")
        permissions = workflow["jobs"]["verify"]["permissions"]
        self.assertEqual(permissions["id-token"], "write")
        self.assertEqual(permissions["attestations"], "write")
        steps = workflow["jobs"]["verify"]["steps"]
        attest = next(step for step in steps if step.get("id") == "attest")
        self.assertTrue(attest["continue-on-error"])
        upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact") and step.get("with", {}).get("name", "").startswith("release-verified-"))
        self.assertIn("steps.attest.outcome", upload["if"])
        text = (self.ROOT / ".github/workflows/release-verify.yml").read_text()
        self.assertIn("actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8", text)
        self.assertIn("release-verified-${{ steps.source.outputs.tree }}", text)
        self.assertIn("RSI_SOURCE_TREE", text)
        self.assertIn("RSI_WORKFLOW_SOURCE_SHA", text)
        self.assertIn("hashFiles('package-lock.json')", text)
        self.assertIn("hashFiles('desktop/src-tauri/Cargo.lock')", text)

    def test_canonical_prepare_serializes_requests_with_check_write_permission(self):
        workflow = self.workflow('release.yml')
        self.assertEqual(workflow['concurrency'], {
            'group': 'adaptive-rsi-release', 'cancel-in-progress': False})
        prepare = workflow['jobs']['prepare']
        for permission in ('contents', 'pull-requests', 'actions', 'checks'):
            self.assertEqual(prepare['permissions'][permission], 'write')
        step = next(item for item in prepare['steps'] if item.get('id') == 'prepare')
        self.assertEqual(step['run'], 'python3 scripts/release.py prepare')
        self.assertEqual(step['env']['GH_TOKEN'], '${{ github.token }}')
        self.assertEqual(step['env']['PREPARE_RELEASE'], '${{ inputs.prepare_release }}')

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
            ".ci/test-durations.json",
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
