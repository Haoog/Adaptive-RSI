import concurrent.futures
import json
import os
import platform
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.adapters import detect
from adaptive_rsi.agent import CodexAgent
from adaptive_rsi.config import DEFAULTS, fingerprint, merge, validate
from adaptive_rsi.evaluator import Evaluator, empty_test_run
from adaptive_rsi.evolution import Evolution
from adaptive_rsi.gitops import git, snapshot
from adaptive_rsi.process import Sandbox, checked, run_process
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store, encode, uid


CHECKS = [{"name": "real unittest regression", "cwd": ".", "argv": ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]}]


def fixture(root):
    root.mkdir()
    checked(["git", "init", "--quiet", str(root)])
    (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests").mkdir()
    (root / "tests/test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass Regression(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.0.1"\n')
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "fixture")
    return root


class FakeAgent:
    """Only the model is substituted; Git isolation and evaluation remain real."""
    def __init__(self, malicious=False):
        self.roles = []
        self.malicious = malicious

    def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        self.roles.append((role, config["models"][role]))
        if role == "planning":
            return {"decision": "ready", "reason": "Verified arithmetic defect", "scope": ["calculator.py", "tests"],
                    "acceptance": ["2 + 3 equals 5"], "risk": "low"}
        if role == "implementation":
            if self.malicious:
                (workspace / "tests/test_calculator.py").write_text("import unittest\nclass Fake(unittest.TestCase):\n    def test_fake(self): pass\n")
            else:
                (workspace / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
            return {"summary": "Corrected addition"}
        if role == "review":
            return {"decision": "pass", "reason": "Narrow change meets acceptance"}
        if role == "analysis":
            return {"findings": [{"title": "Addition subtracts", "body": "add(2, 3) returns -1",
                                  "path": "calculator.py", "quote": "return a - b", "acceptance": ["2+3=5"], "priority": 60}]}
        if role == "evolution":
            return {"kind": "strategy", "guidance": "Reproduce arithmetic boundary cases before editing", "triggers": ["add"], "rationale": "Observed subtraction error"}
        raise AssertionError(role)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="adaptive-rsi-test-")
        self.root = Path(self.tmp.name).resolve()
        self.repo = fixture(self.root / "project")
        self.store = Store(self.root / "controller")
        self.agent = FakeAgent()
        self.runtime = Runtime(self.store, self.agent)
        self.project = self.runtime.add_project(self.repo)
        self.runtime.configure({"checks": CHECKS, "sources": {"inspection": False, "issues": False},
                                "limits": {"retries": 0}, "evolution": {"enabled": False}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def claim(self, text="Fix addition"):
        task = self.runtime.goal(self.project, text)
        self.runtime.pause(self.project, False)
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    def test_settings_precedence_and_frozen_run(self):
        self.runtime.configure({"schedule": {"light_minutes": 240}})
        self.runtime.configure({"schedule": {"light_minutes": 360}}, self.project)
        task = self.runtime.goal(self.project, "Fix addition", {"schedule": {"light_minutes": 480}})
        self.runtime.pause(self.project, False)
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.configure({"schedule": {"light_minutes": 600}}, self.project)
        self.assertEqual(json.loads(run["config"])["schedule"]["light_minutes"], 480)
        self.assertEqual(self.runtime.config(self.project)["schedule"]["light_minutes"], 600)

    def test_claim_is_atomic_and_project_locked(self):
        self.runtime.goal(self.project, "one")
        self.runtime.goal(self.project, "two")
        self.runtime.pause(self.project, False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: self.store.claim(self.project, self.runtime.config(self.project)), range(4)))
        self.assertEqual(sum(r is not None for r in runs), 1)

    def test_resume_coalesces_missed_intervals(self):
        config = merge(self.runtime.config(self.project), {"sources": {"inspection": True}})
        self.runtime.schedule(self.project, config, now=100)
        self.runtime.schedule(self.project, config, now=1000000)
        self.runtime.schedule(self.project, config, now=1000000)
        rows = self.store.query("SELECT * FROM tasks WHERE source LIKE 'system_%'")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.store.query("SELECT * FROM schedules")), 2)

    def test_recovery_retains_artifacts_and_requires_fresh_retry(self):
        task, run = self.claim()
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE id=?", (task,))["state"], "interrupted")
        self.runtime.retry(task)
        new = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertNotEqual(new["id"], run["id"])

    def test_daily_budget_is_atomic(self):
        config = merge(self.runtime.config(self.project), {"limits": {"daily_calls": 1}})
        route = config["models"]["planning"]
        self.store.reserve_call(self.project, "run1", "planning", route, config)
        with self.assertRaisesRegex(RuntimeError, "budget"):
            self.store.reserve_call(self.project, "run2", "planning", route, config)

    def test_read_only_registration_does_not_enable_project(self):
        self.assertEqual(self.runtime.project(self.project)["paused"], 1)
        self.runtime.goal(self.project, "a goal")
        self.assertIsNone(self.store.claim(self.project, self.runtime.config(self.project)))

    def test_duplicate_inspection_and_unchanged_scan_no_model_call(self):
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "light")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        self.runtime.request(self.project, "light")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        self.assertEqual(sum(r[0] == "analysis" for r in self.agent.roles), 1)
        self.assertEqual(len(self.store.query("SELECT id FROM tasks WHERE source='inspection'")), 1)

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_real_git_and_sandbox_end_to_end_without_user_tree_changes(self):
        (self.repo / "notes.txt").write_text("uncommitted user work")
        task, run = self.claim()
        self.runtime.process(run)
        result = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(result["state"], "awaiting_approval", result["result"])
        evaluation = json.loads(result["result"])["evaluation"]
        self.assertFalse(evaluation["checks"]["baseline"][0]["passed"])
        self.assertTrue(evaluation["checks"]["candidate"][0]["passed"])
        self.assertEqual((self.repo / "notes.txt").read_text(), "uncommitted user work")
        self.assertIn("a - b", (self.repo / "calculator.py").read_text())
        self.assertEqual([r[0] for r in self.agent.roles], ["planning", "implementation", "review"])
        self.assertEqual(self.agent.roles[1][1]["model"], "gpt-6-luna")
        self.assertEqual(self.agent.roles[1][1]["effort"], "max")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_fake_agent_success_cannot_replace_original_tests(self):
        self.runtime.agent = FakeAgent(malicious=True)
        task, run = self.claim()
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "failed", row["result"])
        self.assertFalse(json.loads(row["result"])["checks"]["candidate"][0]["passed"])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_dependency_seed_is_frozen_for_both_checks_and_preserves_original(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor/value.txt").write_text("sealed dependency")
        with (self.repo / "tests/test_calculator.py").open("a") as stream:
            stream.write("    def test_dependency(self):\n        from pathlib import Path\n        self.assertEqual(Path('vendor/value.txt').read_text(), 'sealed dependency')\n")
        git(self.repo, "add", "tests")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "dependency check")
        class SeedEditingAgent(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == "implementation":
                    (Path(args[1]) / "vendor/value.txt").write_text("candidate attempted dependency change")
                return result
        self.runtime.agent = SeedEditingAgent()
        self.runtime.configure({"environment": {"seed_paths": ["vendor"]}}, self.project)
        task, run = self.claim()
        self.runtime.process(run)
        row = self.store.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        self.assertEqual(row["state"], "awaiting_approval", row["result"])
        self.assertEqual((self.repo / "vendor/value.txt").read_text(), "sealed dependency")
        self.assertEqual((Path(row["path"]) / "evaluation/candidate/vendor/value.txt").read_text(), "sealed dependency")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_sandbox_hides_controller_prevents_network_and_harness_writes(self):
        workspace = self.store.root / "runs" / "probe"
        workspace.mkdir(parents=True)
        secret = self.store.root / "holdout.txt"
        secret.write_text("protected")
        testfile = workspace / "fixed-test.txt"
        testfile.write_text("original")
        script = """import pathlib,socket
def denied(fn):
 try: fn()
 except OSError: return
 raise RuntimeError('isolation bypassed')
denied(lambda: pathlib.Path(%r).read_text())
denied(lambda: pathlib.Path(%r).write_text('tampered'))
denied(lambda: socket.create_connection(('127.0.0.1', 9), timeout=1))
print('isolated')
""" % (str(secret), str(testfile))
        check = {"name": "isolation", "argv": ["python3", "-c", script]}
        result = Sandbox(self.store.root, self.runtime.config(self.project)).check(check, workspace, protected=[testfile])
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(testfile.read_text(), "original")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_read_only_model_cannot_mutate_sealed_candidate(self):
        workspace = self.store.root / "runs" / "sealed-probe"
        output = workspace / ".rsi-agent"
        output.mkdir(parents=True)
        codefile = workspace / "code.py"
        codefile.write_text("original")
        script = """from pathlib import Path
try: Path(%r).write_text('tamper')
except PermissionError: pass
else: raise RuntimeError('readonly bypassed')
Path(%r).write_text('allowed output')
""" % (str(codefile), str(output / "result.txt"))
        args = Sandbox(self.store.root, self.runtime.config(self.project)).worker(
            ["python3", "-c", script], workspace, read_only=True, output_dir=output)
        result = run_process(args, cwd=workspace)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(codefile.read_text(), "original")

    def test_missing_checks_never_passes(self):
        evaluation = self.runtime.evaluator.evaluate(self.repo, self.repo, [], self.root / "eval", DEFAULTS)
        self.assertEqual(evaluation["decision"], "insufficient_evidence")

    def test_snapshot_excludes_future_fixes_and_git_objects(self):
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "hidden future solution")
        future = git(self.repo, "rev-parse", "HEAD")
        copy = snapshot(self.repo, self.root / "shallow-snapshot", base)
        self.assertEqual(git(copy, "rev-list", "--count", "HEAD"), "1")
        self.assertIn("a - b", (copy / "calculator.py").read_text())
        with self.assertRaises(RuntimeError):
            git(copy, "cat-file", "-e", future)

    def test_empty_suite_keeps_proposal_out_of_stable_strategy(self):
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "evolve")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        candidates = self.store.query("SELECT * FROM candidates")
        self.assertEqual(candidates[0]["status"], "insufficient_evidence")
        self.assertEqual(self.runtime.active_strategies(self.project), [])

    def test_case_families_cannot_leak_between_splits(self):
        manifest = {"family": "addition", "split": "validation", "repository": str(self.repo), "base": "HEAD",
                    "task": "Correct add", "checks": CHECKS, "source": "synthetic"}
        Evolution(self.runtime).import_case(self.project, manifest)
        with self.assertRaisesRegex(ValueError, "family"):
            Evolution(self.runtime).import_case(self.project, {**manifest, "split": "holdout"})
        with self.assertRaisesRegex(ValueError, "family"):
            Evolution(self.runtime).import_case(self.project, {**manifest, "family": "renamed", "split": "holdout"})
        self.assertEqual(len(self.store.query("SELECT * FROM tasks")), 0)

    def test_rollback_restores_parent_and_invalidates_skill(self):
        for candidate, parent, status in [("a", None, "superseded"), ("b", "a", "stable")]:
            self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,created) VALUES (?,?,?,?,?,?,?)",
                               (candidate, self.project, "strategy", parent, status, "{}", time.time()))
        self.store.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                           (uid(), self.project, None, "verified", '["add"]', "lesson", '{"candidate":"b"}', time.time()+100, time.time()))
        self.runtime.rollback_strategy("b")
        self.assertEqual(self.runtime.active_strategies(self.project)[0]["id"], "a")
        self.assertEqual(self.runtime.memories(self.project, "add"), [])

    def test_github_sync_uses_incremental_cursor_dedupes_and_closes(self):
        config = merge(self.runtime.config(self.project), {"github": {"repository": "owner/repo"}})
        issue = {"number": 1, "title": "Fix addition", "body": "task data", "labels": [], "state": "OPEN", "updatedAt": "2026-10-19", "url": "https://github.com/owner/repo/issues/1"}
        calls = []
        def gh(args, **kwargs):
            calls.append(args)
            return json.dumps([issue]) if args[1] == "issue" else "[]"
        with patch("adaptive_rsi.runtime.checked", side_effect=gh):
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.runtime.sync_issues(self.runtime.project(self.project), config)
            self.assertEqual(len(self.store.query("SELECT * FROM tasks WHERE source='issue'")), 1)
            self.assertIn("--search", [args for args in calls if args[1] == "issue"][1])
            issue["state"] = "CLOSED"
            self.runtime.sync_issues(self.runtime.project(self.project), config)
        self.assertEqual(self.store.one("SELECT state FROM tasks WHERE source='issue'")["state"], "obsolete")

    @unittest.skipUnless(platform.system() == "Darwin", "macOS sandbox execution")
    def test_paired_evolution_promotes_retrieves_and_rolls_back_verified_guidance(self):
        second = fixture(self.root / "multiply")
        (second / "calculator.py").write_text("def multiply(a, b):\n    return a / b\n")
        (second / "tests/test_calculator.py").write_text("import unittest\nfrom calculator import multiply\nclass Regression(unittest.TestCase):\n    def test_multiply(self):\n        self.assertEqual(multiply(2, 3), 6)\n")
        git(second, "add", ".")
        git(second, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "independent multiply defect")
        for family, split, repo, text in [("addition", "validation", self.repo, "Correct addition"),
                                         ("multiply", "holdout", second, "Protected multiply acceptance task")]:
            Evolution(self.runtime).import_case(self.project, {"family": family, "split": split,
                "repository": str(repo), "base": "HEAD", "task": text, "checks": CHECKS, "source": "synthetic"})
        store = self.store
        proposal_prompts = []
        class GuidedProvider(FakeAgent):
            def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
                call = store.reserve_call(project, run, role, config["models"][role], config)
                store.execute("UPDATE calls SET state='completed',tokens=100 WHERE id=?", (call,))
                if role == "evolution":
                    proposal_prompts.append(prompt)
                    return super().invoke(role, prompt, workspace, project, run, config, cancel, protected)
                if role == "implementation":
                    if "Reproduce arithmetic boundary cases before editing" in prompt:
                        path = workspace / "calculator.py"
                        path.write_text(path.read_text().replace("a - b", "a + b").replace("a / b", "a * b"))
                    return {"summary": "Applied supplied strategy"}
                return super().invoke(role, prompt, workspace, project, run, config, cancel, protected)
        self.runtime.agent = GuidedProvider()
        self.runtime.configure({"evolution": {"enabled": True, "min_cases_per_split": 1, "max_calls": 10}}, self.project)
        self.runtime.pause(self.project, False)
        self.runtime.request(self.project, "evolve")
        run = self.store.claim(self.project, self.runtime.config(self.project))
        self.runtime.process(run)
        candidates = self.store.query("SELECT * FROM candidates")
        self.assertEqual(candidates[0]["status"], "stable", candidates)
        self.assertNotIn("Protected multiply acceptance task", proposal_prompts[0])
        self.assertEqual(len(json.loads(candidates[0]["evidence"])["trials"]), 2)
        self.assertEqual(len(self.runtime.memories(self.project, "add")), 1)
        self.runtime.configure({"models": {"implementation": {"model": "another-model"}}}, self.project)
        self.assertEqual(self.runtime.active_strategies(self.project), [])
        self.assertEqual(self.runtime.memories(self.project, "add"), [])
        self.runtime.configure({"models": {"implementation": {"model": "gpt-6-luna"}}}, self.project)
        self.assertEqual(len(self.runtime.active_strategies(self.project)), 1)
        self.runtime.monitor_strategy(self.project, False)
        self.assertEqual(self.runtime.active_strategies(self.project), [])
        self.assertEqual(self.runtime.memories(self.project, "add"), [])


class ConfigurationAndAdapterTest(unittest.TestCase):
    def test_rust_integration_tests_are_not_rejected_for_empty_doc_tests(self):
        self.assertFalse(empty_test_run("running 0 tests\nrunning 3 tests\n3 passed; 0 failed\nrunning 0 tests"))
        self.assertTrue(empty_test_run("running 0 tests\n0 passed; 0 failed"))
        self.assertTrue(empty_test_run("No examples found.\n0 examples, 0 failures"))
        self.assertTrue(empty_test_run("# tests 0\n# pass 0"))
    def test_configuration_rejects_typos_escapes_and_invalid_model_effort(self):
        for override in [{"scheduel": {}}, {"models": {"implementation": {"effort": "ultra"}}},
                         {"checks": [{"name": "escape", "cwd": "../", "argv": ["true"]}]},
                         {"schedule": {"light_minutes": 0}}, {"delivery": {"auto_merge": True}},
                         {"limits": {"daily_calls": True}}]:
            with self.assertRaises(ValueError, msg=str(override)):
                validate(override)

    def test_polyglot_manifest_detection_and_honest_environment_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {"java/pom.xml": "", "kotlin/build.gradle.kts": "", "js/package.json": '{"scripts":{"test":"node --test"}}',
                     "py/pyproject.toml": "", "go/go.mod": "module example", "rs/Cargo.toml": "", "cpp/CMakeLists.txt": "",
                     "cs/sample.csproj": "", "php/composer.json": "{}", "ruby/Gemfile": "", "swift/Package.swift": "",
                     "flutter/pubspec.yaml": "flutter:\n", "sql/example.sql": "", "shell/example.sh": "", "html/index.html": ""}
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with patch("adaptive_rsi.adapters.shutil.which", return_value=None):
                data = detect(root)
            self.assertEqual(len(data["modules"]), 12)
            self.assertTrue(all(m["status"] == "environment_blocked" for m in data["modules"]))
            self.assertIn("SQL", data["auxiliary"])

    def test_promotion_requires_quality_and_known_cost_on_both_splits(self):
        def trial(split, before, after, tokens=100):
            return {"split": split, "stable": {"pass": before, "tokens": 100, "seconds": 10},
                    "candidate": {"pass": after, "tokens": tokens, "seconds": 10}}
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", False, True)], DEFAULTS)["decision"], "pass")
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", True, False)], DEFAULTS)["decision"], "fail")
        self.assertEqual(Evolution.decide([trial("validation", False, True), trial("holdout", True, True)], DEFAULTS)["decision"], "insufficient_evidence")
        self.assertEqual(Evolution.decide([trial("validation", False, True, 0), trial("holdout", False, True, 0)], DEFAULTS)["decision"], "insufficient_evidence")

    def test_subprocess_timeout_and_output_cap(self):
        result = run_process(["python3", "-c", "import time; time.sleep(5)"], timeout=0.1)
        self.assertEqual(result["error"], "Command timed out")

    def test_codex_command_really_routes_model_and_effort_and_records_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "controller")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            commands = []
            def provider(argv, *args, **kwargs):
                commands.append(argv)
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text('{"summary":"done"}')
                return {"exit_code": 0, "error": None, "stderr": "", "stdout": '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":10}}\n'}
            with patch("adaptive_rsi.agent.shutil.which", return_value="/usr/bin/codex"), \
                 patch("adaptive_rsi.agent.Sandbox.worker", side_effect=lambda argv, *a, **kw: argv), \
                 patch("adaptive_rsi.agent.run_process", side_effect=provider):
                result = CodexAgent(store).invoke("implementation", "task", workspace, "project", "run", DEFAULTS)
            self.assertEqual(result, {"summary": "done"})
            args = commands[0]
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-luna")
            self.assertIn('model_reasoning_effort="max"', args)
            self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
            self.assertIn("--ignore-user-config", args)
            self.assertEqual(store.one("SELECT tokens FROM calls")["tokens"], 60)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(self.tmp.name), FakeAgent())
        self.server = Server(self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.runtime.close()
        self.tmp.cleanup()

    def headers(self):
        return {"Cookie": self.server.cookie + "=" + self.server.token}

    def test_local_api_requires_authentication_origin_and_csrf(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.server.origin + "/api/state")
        self.assertEqual(denied.exception.code, 401)
        headers = self.headers()
        with urllib.request.urlopen(urllib.request.Request(self.server.origin + "/api/state", headers=headers)) as response:
            data = json.load(response)
            self.assertEqual(data["csrf"], self.server.csrf)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.server.origin + "/api/config", b'{"values":{}}', headers)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        self.assertEqual(denied.exception.code, 403)
        headers.update({"Origin": self.server.origin, "X-RSI-CSRF": self.server.csrf})
        request = urllib.request.Request(self.server.origin + "/api/config", b'{"values":{"schedule":{"light_minutes":240}}}', headers)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.runtime.config()["schedule"]["light_minutes"], 240)


if __name__ == "__main__":
    unittest.main()
