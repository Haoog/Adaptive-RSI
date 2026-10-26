import concurrent.futures
import copy
import json
import platform
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.acceptance import seal
from adaptive_rsi.adapters import test_path
from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.delivery import baseline, merge_gate, tick
from adaptive_rsi.evaluator import Evaluator, freeze_harness
from adaptive_rsi.gitops import changes, git, snapshot
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.process import Sandbox, run_process
from adaptive_rsi.store import Store, encode
from tests.test_runtime import CHECKS, FakeAgent, fixture


SPEC = {"files": [{"path": ".rsi-acceptance/check.py", "content": "from calculator import add\nassert add(2,3)==5\n"}],
        "checks": [{"name": "independent behavior", "argv": ["python3", "-c", "exec(open('.rsi-acceptance/check.py').read())"], "criteria": [0]}]}


class DeliveryLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "state")
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "evolution": {"enabled": False}, "limits": {"retries": 0}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_frozen_harness_restores_modern_test_names_and_configured_check_files(self):
        baseline_root = self.root / "harness-baseline"
        target = self.root / "harness-target"
        for relative, content in {
                "src/math.test.cjs": "assert.equal(add(2, 3), 5)\n",
                "src/math.spec.tsx": "expect(add(2, 3)).toBe(5)\n",
                "qa/verify_behavior.py": "assert add(2, 3) == 5\n"}.items():
            path = baseline_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        shutil.copytree(baseline_root, target)
        (target / "src/math.test.cjs").write_text("assert.equal(add(2, 3), 0)\n")
        (target / "qa/verify_behavior.py").write_text("assert add(2, 3) == 0\n")
        (target / "src/new-feature.test.tsx").write_text("expect(newFeature()).toBe(true)\n")

        protected = freeze_harness(baseline_root, target, [
            {"cwd": ".", "argv": ["python3", "qa/verify_behavior.py"]}])

        self.assertEqual((target / "src/math.test.cjs").read_text(), "assert.equal(add(2, 3), 5)\n")
        self.assertEqual((target / "qa/verify_behavior.py").read_text(), "assert add(2, 3) == 5\n")
        self.assertTrue((target / "src/new-feature.test.tsx").is_file())
        self.assertTrue(any(str(path).endswith("qa/verify_behavior.py") for path in protected))
        for name in ["math.test.cjs", "math.spec.tsx", "math.test.jsx", "math.spec.mjs", "foo_test.py",
                     "FooTest.java", "TestWidget.cs", "math_test.exs", "math_test.dart", "test.py"]:
            self.assertTrue(test_path(name), name)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_candidate_cannot_weaken_a_modern_test_file_to_hide_a_regression(self):
        baseline_root = self.root / "javascript-baseline"
        test_pathname = baseline_root / "src/math.test.cjs"
        test_pathname.parent.mkdir(parents=True)
        (baseline_root / "src/math.cjs").write_text(
            "exports.add = (a, b) => a - b;\nexports.multiply = (a, b) => a * b;\n")
        original_test = ('const test = require("node:test");\n'
                         'const assert = require("node:assert/strict");\n'
                         'const {multiply} = require("./math.cjs");\n'
                         'test("multiplication remains correct", () => assert.equal(multiply(2, 3), 6));\n')
        test_pathname.write_text(original_test)
        candidate = self.root / "javascript-candidate"
        shutil.copytree(baseline_root, candidate)
        (candidate / "src/math.cjs").write_text(
            "exports.add = (a, b) => a + b;\nexports.multiply = () => 0;\n")
        (candidate / "src/math.test.cjs").write_text(original_test.replace("), 6));", "), 0));"))
        checks = [{"name": "JavaScript regression", "argv": ["node", "--test", "src/math.test.cjs"]}]

        result = Evaluator(self.store).evaluate(baseline_root, candidate, checks,
            self.root / "javascript-evaluation", self.runtime.config(self.project))

        self.assertTrue(all(check["passed"] for check in result["checks"]["baseline"]))
        self.assertFalse(all(check["passed"] for check in result["checks"]["candidate"]))
        self.assertTrue(all(check["passed"] for check in result["checks"]["candidate_full"]))
        self.assertEqual(result["decision"], "fail")

    def test_independent_acceptance_requires_complete_coverage_and_safe_paths(self):
        for edit in [lambda s: s["checks"][0].update(criteria=[]),
                     lambda s: s["checks"][0].update(criteria=[1]),
                     lambda s: s["files"][0].update(path="calculator.py"),
                     lambda s: s["files"][0].update(path=".rsi-acceptance/../../escape")]:
            spec = copy.deepcopy(SPEC)
            edit(spec)
            with self.assertRaises((RuntimeError, ValueError)):
                seal(spec, ["correct sum"], self.root / str(time.time_ns()))

    def test_generated_python_alias_is_normalized_before_sealing_without_mutating_input(self):
        spec = copy.deepcopy(SPEC)
        spec["checks"][0]["argv"][0] = "python"
        result = seal(spec, ["correct sum"], self.root / "acceptance")
        self.assertEqual(spec["checks"][0]["argv"][0], "python")
        self.assertEqual(result["checks"][0]["argv"][0], "python3")
        self.assertEqual(result["spec"]["checks"][0]["argv"][0], "python3")

    def test_annotated_scope_is_rejected_before_spending_implementation_calls(self):
        class AnnotatedPlanner(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "planning":
                    result["scope"] = ["calculator.py：修改加法"]
                return result
        self.runtime.agent = AnnotatedPlanner()
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        result = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        self.assertEqual([r[0] for r in self.runtime.agent.roles], ["planning"], result["result"])
        self.assertEqual(result["state"], "blocked")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_high_risk_acceptance_artifacts_never_enter_implementer_workspace(self):
        workspaces = {}
        class ObservingAgent(FakeAgent):
            def invoke(self, role, prompt, workspace, *args, **kwargs):
                workspaces[role] = workspace
                result = super().invoke(role, prompt, workspace, *args, **kwargs)
                if role == "planning":
                    result["risk"] = "high"
                if role == "acceptance":
                    (workspace / "sealed-result.json").write_text("hidden acceptance result")
                if role == "implementation":
                    self_test.assertFalse((workspace / "sealed-result.json").exists())
                    self_test.assertNotEqual(workspace, workspaces["acceptance"])
                return result
        self_test = self
        self.runtime.agent = ObservingAgent()
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "awaiting_approval")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_candidate_new_failing_test_is_not_erased_by_frozen_regression(self):
        candidate = snapshot(self.repo, self.root / "candidate")
        (candidate / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        test = candidate / "tests/test_calculator.py"
        test.write_text(test.read_text() + "    def test_new_failure(self):\n        self.fail('candidate regression')\n")
        result = Evaluator(self.store).evaluate(self.repo, candidate, CHECKS, self.root / "evaluation", self.runtime.config(self.project))
        self.assertTrue(all(c["passed"] for c in result["checks"]["candidate"]))
        self.assertFalse(all(c["passed"] for c in result["checks"]["candidate_full"]))
        self.assertEqual(result["decision"], "fail")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_independent_acceptance_blocks_weak_original_and_candidate_tests(self):
        candidate = snapshot(self.repo, self.root / "candidate")
        acceptance = seal(SPEC, ["2+3=5"], self.root / "acceptance")
        checks = [{"name": "weak project check", "argv": ["python3", "-c", "print('existing build succeeds')"]}]
        result = Evaluator(self.store).evaluate(self.repo, candidate, checks, self.root / "evaluation", self.runtime.config(self.project), acceptance=acceptance)
        self.assertTrue(result["checks"]["candidate"][0]["passed"])
        self.assertFalse(result["checks"]["acceptance_candidate"][0]["passed"])
        self.assertEqual(result["decision"], "fail")

    def test_weighted_dispatch_persists_and_does_not_starve_second_project(self):
        other_repo = fixture(self.root / "other")
        other = self.runtime.add_project(other_repo)
        self.runtime.configure({"schedule": {"weight": 3}}, other)
        for p in [self.project, other]:
            self.runtime.pause(p, False)
            for n in range(12):
                self.runtime.goal(p, "task " + str(n))
        counts = {self.project: 0, other: 0}
        class Pool:
            def submit(_, fn, run):
                counts[run["project"]] += 1
                self.store.transition(run["id"], "completed", {}, True)
                future = concurrent.futures.Future()
                future.set_result(None)
                return future
        eligible = [(self.runtime.project(p), self.runtime.config(p)) for p in counts]
        with patch.object(self.runtime, "pool", Pool()):
            for _ in range(8):
                self.runtime.futures.clear()
                self.runtime.dispatch(eligible)
        self.assertEqual(counts[self.project], 2)
        self.assertEqual(counts[other], 6)
        self.assertEqual(Store(self.store.root).setting("fair-service"), self.store.setting("fair-service"))

    def test_remote_baseline_fetch_never_changes_user_head_or_dirty_files(self):
        remote = fixture(self.root / "upstream")
        (remote / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        git(remote, "add", ".")
        git(remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "remote fix")
        git(remote, "branch", "-M", "main")
        expected = git(remote, "rev-parse", "HEAD")
        original = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "notes.txt").write_text("user draft")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        config = merge(self.runtime.config(self.project), {"github": {"repository": "example/project"}, "delivery": {"base_branch": "main"}})
        def fetch(root, *args, timeout=None):
            argv = [str(remote) if a == "https://github.com/example/project.git" else a for a in args]
            return git(root, *argv)
        with patch("adaptive_rsi.delivery.github_git", side_effect=fetch):
            source, actual, _ = baseline(self.runtime, self.runtime.project(self.project), config)
        self.assertEqual(actual, expected)
        self.assertEqual(git(source, "rev-parse", "rsi-baseline"), expected)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), original)
        self.assertEqual((self.repo / "notes.txt").read_text(), "user draft")

    def overlapping_baseline_fetches(self, names):
        """对给定项目并发调用 baseline，返回同时处于 fetch 内的最大并发数。"""
        repos = {}
        for name in names:
            repo = fixture(self.root / ("baseline-" + name + "-" + str(time.time_ns())))
            git(repo, "remote", "add", "origin", "https://github.com/example/" + name + ".git")
            repos[name] = repo
        projects = {}
        for name, repo in repos.items():
            project = self.runtime.add_project(repo)
            self.runtime.configure({"github": {"repository": "example/" + name},
                                    "delivery": {"base_branch": "main"}}, project)
            projects[name] = project
        active, peak, guard = [], 0, threading.Lock()

        def fetch(root, *args, timeout=None):
            nonlocal peak
            with guard:
                active.append(root)
                peak = max(peak, len(active))
            time.sleep(0.3)
            with guard:
                active.pop()
            return ""

        def run(name):
            baseline(self.runtime, self.runtime.project(projects[name]),
                     self.runtime.config(projects[name]))

        with patch("adaptive_rsi.delivery.github_git", side_effect=fetch), \
                patch("adaptive_rsi.delivery.git", return_value="f" * 40):
            threads = [threading.Thread(target=run, args=(name,)) for name in names]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        return peak

    def test_baseline_locks_are_per_project_and_do_not_serialize_projects(self):
        # 不同项目的基线 fetch 并发进行，不再被全局锁串行阻塞。
        self.assertGreaterEqual(self.overlapping_baseline_fetches(["one", "two"]), 2)
        # 同一项目的并发基线刷新仍然互斥。
        self.assertLessEqual(self.overlapping_baseline_fetches(["solo", "solo"]), 1)

    def test_exact_head_ci_conflict_review_and_empty_check_gates(self):
        good = {"headRefOid": "head", "baseRefOid": "base", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "reviewDecision": "", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        self.assertEqual(merge_gate(good, "head", "base"), "ready")
        for values, state in [({"headRefOid": "other"}, "stale"), ({"baseRefOid": "other"}, "stale"),
                              ({"mergeable": "CONFLICTING"}, "waiting"), ({"reviewDecision": "REVIEW_REQUIRED"}, "waiting_review"),
                              ({"statusCheckRollup": []}, "waiting_ci"),
                              ({"statusCheckRollup": [{"conclusion": "FAILURE"}]}, "ci_failed"),
                              ({"statusCheckRollup": [{"status": "IN_PROGRESS"}]}, "waiting_ci")]:
            with self.subTest(values=values):
                self.assertEqual(merge_gate({**good, **values}, "head", "base"), state)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_auto_pr_is_queued_only_after_all_local_gates(self):
        self.runtime.configure({"delivery": {"mode": "pr", "auto_merge": True}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        row = self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "awaiting_approval")
        self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='system_publish'")), 1)

    def test_restart_retries_publish_but_never_restarts_implementation(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.recover()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "interrupted")
        self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))

    @unittest.skipUnless(platform.system() == "Darwin" and shutil.which("codex"), "installed Codex permissions support")
    def test_actual_codex_single_layer_permissions_hide_secrets_and_disable_network(self):
        workspace = snapshot(self.repo, self.store.root / "worker")
        secret = self.store.root / "secret.txt"
        secret.write_text("controller secret")
        unrelated = self.root / "unregistered-private.txt"
        unrelated.write_text("unregistered private marker")
        code = """import errno, os, socket
from pathlib import Path
blocked = %r
for path in blocked:
    try:
        Path(path).read_text()
    except PermissionError:
        pass
    else:
        raise AssertionError('protected read succeeded')
try:
    Path('.git/config').write_text('bad')
except PermissionError:
    pass
else:
    raise AssertionError('git write succeeded')
try:
    socket.create_connection(('127.0.0.1', 9), timeout=1)
except OSError as exc:
    assert exc.errno in (errno.EPERM, errno.EACCES), exc
else:
    raise AssertionError('network permitted')
assert 'RSI_TEST_SECRET' not in os.environ
Path('allowed.txt').write_text('isolated write succeeds')
print('BOUNDARIES_OK')
""" % [str(secret), str(self.repo / "calculator.py"), str(unrelated)]
        permissions = Sandbox(self.store.root, DEFAULTS).codex_permissions(workspace, [self.repo])
        import os
        result = run_process(["codex"] + permissions + ["sandbox", "-P", "rsi", "-C", str(workspace), "python3", "-c", code],
                             workspace, env={**os.environ, "RSI_TEST_SECRET": "must-not-leak"})
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("BOUNDARIES_OK", result["stdout"])
        readonly = Sandbox(self.store.root, DEFAULTS).codex_permissions(workspace, [self.repo], read_only=True)
        code = "from pathlib import Path\nassert Path('allowed.txt').read_text()\ntry:\n Path('denied.txt').write_text('no')\nexcept PermissionError:\n print('READ_ONLY_OK')\nelse:\n raise AssertionError('read-only write succeeded')"
        result = run_process(["codex"] + readonly + ["sandbox", "-P", "rsi", "-C", str(workspace), "python3", "-c", code], workspace)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("READ_ONLY_OK", result["stdout"])
        output = workspace / ".rsi-agent" / "review-result.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        readonly = Sandbox(self.store.root, DEFAULTS).codex_permissions(
            workspace, [self.repo], read_only=True, output_file=output)
        code = "from pathlib import Path\nPath(%r).write_text('{}')\ntry:\n Path('review-edit.txt').write_text('no')\nexcept PermissionError:\n print('OUTPUT_ONLY_OK')\nelse:\n raise AssertionError('review edited project')" % str(output)
        result = run_process(["codex"] + readonly + ["sandbox", "-P", "rsi", "-C", str(workspace), "python3", "-c", code], workspace)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("OUTPUT_ONLY_OK", result["stdout"])

    def test_project_policy_revocation_blocks_remote_merge(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"}, "delivery": {"mode": "pr", "auto_merge": True}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(run["id"], "pr_open", {"pr": "https://github.com/example/project/pull/1"}, True)
        self.store.put_setting("delivery:" + run["id"], {"head": "h", "base": "b", "branch": "main"})
        self.runtime.configure({"delivery": {"auto_merge": False}}, self.project)
        pr = {"state": "OPEN", "headRefOid": "h", "baseRefOid": "b", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.checked") as command:
            tick(self.runtime)
        command.assert_not_called()

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_native_command_cannot_install_host_git_execution_hooks(self):
        from adaptive_rsi.native import WorkspaceTools
        workspace = snapshot(self.repo, self.store.root / "native-worker")
        tools = WorkspaceTools(workspace, "implementation", self.store.root, DEFAULTS, None, [])
        result = tools.execute("run_command", {"cwd": ".", "argv": ["git", "config", "core.fsmonitor", "./malicious-monitor"]})
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn("malicious-monitor", (workspace / ".git/config").read_text())

    def test_host_git_does_not_execute_filters_from_global_configuration(self):
        marker = self.root / "host-execution-marker"
        config = self.root / "global-git-config"
        config.write_text('[filter "evil"]\n    clean = touch ' + str(marker) + '; cat\n')
        (self.repo / ".gitattributes").write_text("*.txt filter=evil\n")
        (self.repo / "payload.txt").write_text("candidate data")
        with patch.dict("os.environ", {"GIT_CONFIG_GLOBAL": str(config)}):
            changes(self.repo)
        self.assertFalse(marker.exists())

    def test_deferred_reassessment_uses_remote_execution_baseline_without_extra_model_calls(self):
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, "Deferred work")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        from adaptive_rsi.config import fingerprint
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        signature = fingerprint({"body": row["body"], "evidence": row["evidence"], "policy": self.runtime.config(self.project)["triage"]["policy"]})
        self.store.transition(run["id"], "deferred", {"base": "remote-sha", "triage_input": signature}, True)
        for head, expected in [("remote-sha", "deferred"), ("new-remote-sha", "queued")]:
            self.runtime.request(self.project, "reassess")
            check = self.store.claim(self.project, self.runtime.config(self.project))
            with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), head, "main")):
                self.runtime.process(check)
            self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], expected)
        self.assertEqual(self.runtime.agent.roles, [])

    def test_legacy_deferred_without_triage_input_is_retained_until_inputs_change(self):
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, "Deferred legacy work")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(run["id"], "deferred", {"base": "old-sha", "reason": "Waiting for dependency"}, True)
        # 模拟升级前数据：没有迁移 decision，只有缺少 triage_input 的历史 run 结果。
        self.store.execute("DELETE FROM task_decisions")
        from adaptive_rsi.config import fingerprint

        def reassess(head):
            self.runtime.request(self.project, "reassess")
            check = self.store.claim(self.project, self.runtime.config(self.project))
            with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), head, "main")):
                self.runtime.process(check)

        reassess("new-sha")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "deferred")
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        decision = json.loads(self.store.one("SELECT data FROM task_decisions WHERE task=?", (task,))["data"])
        self.assertEqual(decision["decision_source"], "legacy_triage")
        self.assertEqual(decision["base"], "new-sha")
        self.assertEqual(decision["triage_input"],
                         fingerprint({"body": row["body"], "evidence": row["evidence"],
                                      "policy": self.runtime.config(self.project)["triage"]["policy"]}))
        # 输入与基线均未变化：保持暂缓，不重新入队。
        reassess("new-sha")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "deferred")
        # 输入真正变化后才唤醒。
        self.store.execute("UPDATE tasks SET body=? WHERE id=?", (row["body"] + " with updated dependency", task))
        reassess("new-sha")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_tick_accepts_run_mapping_as_active_run(self):
        run = self.tracked_pr()
        pr = {"state": "OPEN", "headRefOid": "head", "baseRefOid": "base", "mergeable": "MERGEABLE",
              "mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED",
              "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr):
            tick(self.runtime, active_run={"id": run["id"]})
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "pr_open")
        self.assertEqual(json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])["delivery_status"],
                         "waiting_review")

    def test_merged_tasks_still_trigger_strategy_evolution_and_reverts_reach_planning(self):
        self.runtime.pause(self.project, False)
        self.runtime.configure({"evolution": {"enabled": True, "every_completed": 2}}, self.project)
        runs = []
        for _ in range(2):
            self.runtime.goal(self.project, "Completed improvement")
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.store.transition(run["id"], "merged", {"feedback": {"state": "verified"}}, True)
            runs.append(run)
        self.runtime.maybe_evolve(self.project, self.runtime.config(self.project))
        self.assertTrue(self.store.one("SELECT 1 FROM tasks WHERE source='system_evolve' AND state='queued'"))
        self.store.execute("UPDATE runs SET state='reverted',result=? WHERE id=?", (encode({"feedback": {"state": "reverted"}}), runs[0]["id"]))
        self.assertIn("reverted", [item["feedback"]["state"] for item in self.runtime.outcomes(self.project)])

    def tracked_pr(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"}, "delivery": {"mode": "pr", "auto_merge": True}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(run["id"], "pr_open", {"pr": "https://github.com/example/project/pull/1"}, True)
        self.store.put_setting("delivery:" + run["id"], {"head": "head", "base": "base", "branch": "main"})
        self.store.put_setting("run-baseline:" + run["id"], {"repository": "example/project", "branch": "main", "sha": "base"})
        return run

    def test_feedback_failure_preserves_merge_metadata_for_retry(self):
        run = self.tracked_pr()
        pr = {"state": "MERGED", "headRefOid": "head", "baseRefOid": "base", "mergeCommit": {"oid": "merged-sha"}}
        now = time.time()
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.feedback", side_effect=RuntimeError("temporary fetch failure")):
            tick(self.runtime, now)
        row = self.store.one("SELECT state,result FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "merged")
        self.assertEqual(json.loads(row["result"])["merged_sha"], "merged-sha")
        with patch("adaptive_rsi.delivery.feedback") as feedback:
            tick(self.runtime, now + 61)
        feedback.assert_called_once()

    def test_retargeted_pr_cannot_merge_into_another_branch_at_the_same_sha(self):
        self.tracked_pr()
        pr = {"state": "OPEN", "headRefOid": "head", "baseRefOid": "base", "baseRefName": "other",
              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.checked", return_value="") as command:
            tick(self.runtime)
        self.assertEqual(command.call_count, 1)
        self.assertIn("close", command.call_args.args[0])

    def test_merged_retargeted_pr_is_unverified_and_does_not_confirm_feedback(self):
        run = self.tracked_pr()
        pr = {"state": "MERGED", "headRefOid": "head", "baseRefOid": "base", "baseRefName": "other", "mergeCommit": {"oid": "external"}}
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.feedback") as feedback:
            tick(self.runtime)
        feedback.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "unverified_merge")

    def test_pause_during_remote_base_check_prevents_merge(self):
        self.tracked_pr()
        pr = {"state": "OPEN", "headRefOid": "head", "baseRefOid": "base", "baseRefName": "main",
              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        def pause_during_fetch(*args):
            self.runtime.pause(self.project, True)
            return str(self.repo), "base", "main"
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.baseline", side_effect=pause_during_fetch), patch("adaptive_rsi.delivery.checked") as command:
            tick(self.runtime)
        command.assert_not_called()

    def test_merge_rechecks_target_and_sealed_authorization_after_baseline_fetch(self):
        run = self.tracked_pr()
        self.runtime.configure({"delivery": {"base_branch": "main"}}, self.project)
        original_config = self.runtime.config(self.project)
        original_policy = self.store.setting("delivery:" + run["id"])
        original_identity = self.store.setting("run-baseline:" + run["id"])
        pr = {"state": "OPEN", "headRefOid": "head", "baseRefOid": "base", "baseRefName": "main",
              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}
        for change in ["repository", "branch", "origin", "policy", "identity", "run", "permission", "unrelated", "unchanged"]:
            with self.subTest(change=change):
                self.runtime.configure({"github": original_config["github"], "delivery": original_config["delivery"]}, self.project)
                self.store.put_setting("delivery:" + run["id"], original_policy)
                self.store.put_setting("run-baseline:" + run["id"], original_identity)
                self.store.execute("UPDATE runs SET state='pr_open' WHERE id=?", (run["id"],))
                git(self.repo, "remote", "set-url", "origin", "https://github.com/example/project.git")
                self.store.put_setting("delivery-tick", 0)

                def change_during_fetch(*args):
                    if change == "repository":
                        self.runtime.configure({"github": {"repository": "example/replacement"}}, self.project)
                    elif change == "branch":
                        self.runtime.configure({"delivery": {"base_branch": "release"}}, self.project)
                    elif change == "origin":
                        git(self.repo, "remote", "set-url", "origin", "https://github.com/example/replacement.git")
                    elif change == "policy":
                        self.store.put_setting("delivery:" + run["id"], {**original_policy, "head": "replacement"})
                    elif change == "identity":
                        self.store.put_setting("run-baseline:" + run["id"], {**original_identity, "branch": "release"})
                    elif change == "run":
                        self.store.execute("UPDATE runs SET state='superseded' WHERE id=?", (run["id"],))
                    elif change == "permission":
                        self.runtime.configure({"delivery": {"auto_merge": False}}, self.project)
                    elif change == "unrelated":
                        self.runtime.configure({"schedule": {"weight": 2}}, self.project)
                    return str(self.repo), "base", "main"

                with patch("adaptive_rsi.delivery.pr_view", return_value=pr), \
                        patch("adaptive_rsi.delivery.baseline", side_effect=change_during_fetch) as fetch, \
                        patch("adaptive_rsi.delivery.checked", return_value="") as command:
                    tick(self.runtime)
                fetch.assert_called_once()
                if change in {"unchanged", "unrelated"}:
                    command.assert_called_once_with(["gh", "pr", "merge", "https://github.com/example/project/pull/1",
                                                     "--repo", "example/project", "--squash", "--match-head-commit", "head"])
                else:
                    command.assert_not_called()

    def test_publishing_cannot_change_original_target_even_with_identical_commit(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"}, "delivery": {"mode": "pr", "auto_merge": True, "base_branch": "main"}}, self.project)
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.execute("UPDATE runs SET base='base' WHERE id=?", (run["id"],))
        self.store.put_setting("run-baseline:" + run["id"], {"repository": "example/project", "branch": "main", "sha": "base"})
        self.store.transition(run["id"], "awaiting_approval", {"evaluation": {"decision": "pass"}, "review": {"decision": "pass"}}, True)
        self.runtime.deliver(run["id"], automatic=True)
        self.runtime.configure({"delivery": {"base_branch": "release"}}, self.project)
        publishing = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertIsNotNone(publishing)
        original = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), "base", "release")), patch("adaptive_rsi.delivery.checked") as command:
            with self.assertRaisesRegex(RuntimeError, "baseline changed"):
                self.runtime.publish(self.runtime.project(self.project), self.store.one("SELECT * FROM tasks WHERE id=?", (task,)),
                                     original, self.repo, self.runtime.config(self.project))
        command.assert_not_called()

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_stale_candidate_before_pr_is_invalidated_and_can_be_replanned(self):
        self.runtime.configure({"delivery": {"mode": "pr"}}, self.project)
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, "Fix addition")
        original = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(original)
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"github": {"repository": "example/project"}}, self.project)
        publishing = self.store.claim(self.project, self.runtime.config(self.project))
        base = git(self.repo, "rev-parse", "HEAD")
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")):
            self.runtime.process(publishing)
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (original["id"],))["state"], "superseded")
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "blocked")
        self.runtime.retry(task)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "queued")

    def test_historical_service_differences_do_not_starve_rejoining_projects(self):
        other = self.runtime.add_project(fixture(self.root / "other"))
        counts = {self.project: 0, other: 0}
        for project in counts:
            self.runtime.pause(project, False)
            for index in range(20):
                self.runtime.goal(project, "task " + str(index))
        self.store.put_setting("fair-service", {self.project: 10000, other: 0})
        class Pool:
            def submit(_, fn, run):
                counts[run["project"]] += 1
                self.store.transition(run["id"], "completed", {}, True)
                future = concurrent.futures.Future()
                future.set_result(None)
                return future
        with patch.object(self.runtime, "pool", Pool()):
            for _ in range(20):
                self.runtime.futures.clear()
                self.runtime.dispatch([(self.runtime.project(p), self.runtime.config(p)) for p in counts])
        self.assertGreaterEqual(min(counts.values()), 9)

    def test_dispatch_reads_concurrency_limit_once_per_pass(self):
        self.runtime.pause(self.project, False)
        for index in range(6):
            self.runtime.goal(self.project, "task " + str(index))

        class Pool:
            def submit(_, fn, run):
                self.store.transition(run["id"], "completed", {}, True)
                future = concurrent.futures.Future()
                future.set_result(None)
                return future

        eligible = [(self.runtime.project(self.project), self.runtime.config(self.project))]
        original = self.runtime.config
        calls = []

        def counting(project=None):
            calls.append(project)
            return original(project)

        with patch.object(self.runtime, "pool", Pool()), patch.object(self.runtime, "config", side_effect=counting):
            self.runtime.futures.clear()
            self.runtime.dispatch(eligible)
        # 并发上限只在本次 dispatch 读取一次；此前每轮 while 迭代都会重读配置。
        self.assertEqual(calls, [None])

    def test_long_regression_output_survives_investigation_failure_and_retries_once(self):
        from adaptive_rsi.delivery import feedback
        run = self.tracked_pr()
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True)
        run["path"] = str(directory)
        head = git(self.repo, "rev-parse", "HEAD")
        outcome = {"merged_sha": head, "merged_at": time.time()}
        report = {"decision": "fail", "checks": {"candidate": [{"name": "long failure", "passed": False, "stderr": "x" * 40000}]}}
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), head, "main")), patch.object(self.runtime.evaluator, "evaluate", return_value=report) as evaluate:
            with patch.object(self.store, "add_task", side_effect=RuntimeError("temporary database failure")):
                with self.assertRaisesRegex(RuntimeError, "database failure"):
                    feedback(self.runtime, self.runtime.project(self.project), run, outcome, self.runtime.config(self.project), time.time())
            saved = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
            self.assertTrue(saved["feedback"]["investigation_pending"])
            self.assertEqual(len(saved["feedback"]["evaluation"]["checks"]["candidate"][0]["stderr"]), 40000)
            feedback(self.runtime, self.runtime.project(self.project), run, saved, self.runtime.config(self.project), time.time())
            self.assertEqual(evaluate.call_count, 1)
        tasks = self.store.query("SELECT body FROM tasks WHERE fingerprint LIKE 'postmerge:%'")
        self.assertEqual(len(tasks), 1)
        self.assertLess(len(tasks[0]["body"]), 30000)

    def test_postmerge_monitoring_stays_on_original_target_after_settings_change(self):
        from adaptive_rsi.delivery import feedback
        run = self.tracked_pr()
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True)
        run["path"] = str(directory)
        head = git(self.repo, "rev-parse", "HEAD")
        self.runtime.configure({"delivery": {"base_branch": "release"}}, self.project)
        def pinned_baseline(runtime, project, config):
            self.assertEqual(config["github"]["repository"], "example/project")
            self.assertEqual(config["delivery"]["base_branch"], "main")
            return str(self.repo), head, "main"
        outcome = {"merged_sha": head, "merged_at": time.time()}
        with patch("adaptive_rsi.delivery.baseline", side_effect=pinned_baseline), patch.object(self.runtime.evaluator, "evaluate", return_value={"decision": "pass", "checks": {}}):
            feedback(self.runtime, self.runtime.project(self.project), run, outcome, self.runtime.config(self.project), time.time())
        self.assertEqual(outcome["feedback"]["head"], head)
        self.assertEqual(outcome["feedback"]["state"], "verified")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_merged_feedback_rechecks_changed_mainline_and_reports_real_regression(self):
        (self.repo / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "fixed")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        self.runtime.configure({"delivery": {"feedback_minutes": 5}, "github": {"repository": "example/project"}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        directory = self.store.root / "runs" / run["id"]
        directory.mkdir(parents=True)
        (directory / "dependencies").mkdir()
        acceptance = seal(SPEC, ["2+3=5"], directory / "acceptance")
        (directory / "acceptance.json").write_text(encode(acceptance))
        self.store.execute("UPDATE runs SET path=?,base=? WHERE id=?", (str(directory), base, run["id"]))
        self.store.transition(run["id"], "pr_open", {"pr": "https://github.com/example/project/pull/1"}, True)
        self.store.put_setting("delivery:" + run["id"], {"head": "candidate", "base": base, "branch": "main"})
        self.store.put_setting("run-baseline:" + run["id"], {"repository": "example/project", "branch": "main", "sha": base})
        pr = {"state": "MERGED", "headRefOid": "candidate", "baseRefOid": base, "mergeCommit": {"oid": base}}
        now = time.time()
        with patch("adaptive_rsi.delivery.pr_view", return_value=pr), patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), base, "main")):
            tick(self.runtime, now)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual(result["feedback"]["state"], "verified")
        (self.repo / "calculator.py").write_text("def add(a,b):\n    return a-b\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "later regression")
        head = git(self.repo, "rev-parse", "HEAD")
        with patch("adaptive_rsi.delivery.baseline", return_value=(str(self.repo), head, "main")):
            tick(self.runtime, now + 301)
        result = json.loads(self.store.one("SELECT result FROM runs WHERE id=?", (run["id"],))["result"])
        self.assertEqual(result["feedback"]["state"], "regressed")
        self.assertTrue(result["feedback"]["evaluation"]["checks"]["baseline"][0]["passed"])
        self.assertFalse(result["feedback"]["evaluation"]["checks"]["acceptance_candidate"][0]["passed"])
        self.assertTrue(self.store.one("SELECT 1 FROM tasks WHERE state='queued' AND title LIKE 'Investigate verified%'"))
        self.assertNotIn("evaluation", self.runtime.outcomes(self.project)[0]["feedback"])

    def test_recovered_publish_recognizes_existing_delivery_without_pushing_again(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, "Fix addition")
        original = self.store.claim(self.project, self.runtime.config(self.project))
        self.store.transition(original["id"], "pr_open", {"pr": "https://github.com/example/project/pull/1"}, True)
        self.store.add_task(self.project, "system_publish", "publish:" + original["id"], "publish", original["id"])
        run = self.store.claim(self.project, self.runtime.config(self.project))
        with patch.object(self.runtime, "publish") as publish:
            self.runtime.process(run)
        publish.assert_not_called()
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"], "completed")
