from collections import Counter
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import time
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import test_parallel as parallel


class ParallelReleaseTest(unittest.TestCase):
    def test_log_reader_cannot_rewind_a_running_workers_output(self):
        writes = []
        class RunningWorker:
            returncode = None

            def __init__(self, descriptor):
                self.descriptor = descriptor
                self.iteration = 0

            def poll(self):
                self.iteration += 1
                if self.iteration == 1:
                    os.write(self.descriptor, b'first result\n')
                    return None
                os.write(self.descriptor, b'second result\n')
                self.returncode = 0
                return 0

        def spawn(*args, **kwargs):
            log = kwargs['stdout']
            process = RunningWorker(os.dup(log.fileno()))
            seek = log.seek
            def write_during_read(*args, **kwargs):
                result = seek(*args, **kwargs)
                # A child write can race a parent that just rewound its reader.
                os.write(process.descriptor, b'concurrent result\n')
                return result
            log.seek = write_during_read
            writes.append(process.descriptor)
            return process

        output = io.StringIO()
        try:
            with patch.object(parallel.subprocess, 'Popen', side_effect=spawn), \
                    patch.object(parallel.time, 'sleep'), redirect_stdout(output):
                self.assertEqual(parallel.run_groups([['tests.synthetic']]), 0)
            self.assertIn('first result', output.getvalue())
            self.assertIn('second result', output.getvalue())
        finally:
            for descriptor in writes:
                os.close(descriptor)

    def test_groups_cover_exact_discovery_once_including_platform_tests(self):
        groups = parallel.discover_groups()
        self.assertEqual(len(groups), 4)
        selectors = [selector for group in groups for selector in group]
        self.assertEqual(len(selectors), len(set(selectors)))
        loader = unittest.TestLoader()
        expected = Counter(parallel.test_ids(loader.discover(str(parallel.ROOT), top_level_dir=str(parallel.ROOT))))
        actual = Counter(parallel.test_ids(loader.loadTestsFromNames(selectors)))
        self.assertFalse(loader.errors)
        self.assertEqual(actual, expected)
        self.assertTrue(all(count == 1 for count in actual.values()))
        self.assertIn("tests.test_runtime.RuntimeTest.test_green_tests_cannot_bypass_blocking_maintainability_review", actual)
        self.assertEqual(groups, parallel.discover_groups())
        self.assertEqual(len(parallel.discover_groups(workers=1)), 1)

    def test_discovery_errors_empty_suites_and_duplicate_ids_fail_closed(self):
        for suite, errors in [
            (unittest.TestSuite(), []),
            (unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]), ["broken import"]),
            (unittest.TestSuite([unittest.FunctionTestCase(str), unittest.FunctionTestCase(str)]), []),
        ]:
            loader = Mock(errors=errors)
            loader.discover.return_value = suite
            with self.subTest(errors=errors), patch.object(parallel.unittest, "TestLoader", return_value=loader):
                with self.assertRaises(ValueError):
                    parallel.discover_groups()
        for workers in [0, 5]:
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                parallel.discover_groups(workers=workers)

    def test_groups_balance_module_counts_without_splitting_fixtures(self):
        def suite():
            cases = []
            for index, count in enumerate([8, 7, 6, 5, 4, 3, 2, 1]):
                cls = type("ModuleTest", (unittest.TestCase,), {
                    "__module__": "tests.synthetic_" + str(index),
                    **{"test_" + str(i): lambda self: None for i in range(count)},
                })
                cases.extend(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
            return unittest.TestSuite(cases)
        loader = Mock(errors=[])
        loader.discover.side_effect = lambda *args, **kwargs: suite()
        loader.loadTestsFromNames.side_effect = lambda names: suite()
        with patch.object(parallel.unittest, "TestLoader", return_value=loader):
            groups = parallel.discover_groups()
        sizes = [sum(8 - int(name.rsplit("_", 1)[1]) for name in group) for group in groups]
        self.assertEqual(sizes, [9, 9, 9, 9])
        self.assertEqual(len({name for group in groups for name in group}), 8)

    def test_selector_coverage_mismatch_fails_closed(self):
        loader = Mock(errors=[])
        loader.discover.return_value = unittest.TestSuite([unittest.FunctionTestCase(str)])
        loader.loadTestsFromNames.return_value = unittest.TestSuite()
        with patch.object(parallel.unittest, "TestLoader", return_value=loader):
            with self.assertRaisesRegex(ValueError, "exactly reproduce"):
                parallel.discover_groups()

    def test_real_workers_use_separate_processes_no_shell_and_propagate_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            names = ["test_parallel_good", "test_parallel_bad"]
            for name, succeeds in zip(names, [True, False]):
                (root / (name + ".py")).write_text(
                    "import os, unittest\n"
                    "class WorkerTest(unittest.TestCase):\n"
                    "    def test_result(self):\n"
                    "        with open({!r}, 'a') as log:\n"
                    "            log.write({!r} + ':' + str(os.getpid()) + '\\n')\n"
                    "        self.assertTrue({!r})\n".format(str(marker), name, succeeds))
            output = io.StringIO()
            with patch.object(parallel.subprocess, "Popen", wraps=subprocess.Popen) as popen, redirect_stdout(output):
                self.assertEqual(parallel.run_groups([[name] for name in names], root), 1)
                calls = list(popen.call_args_list)
            records = [line.split(":") for line in marker.read_text().splitlines()]
            self.assertEqual({name for name, pid in records}, set(names))
            self.assertEqual(len({pid for name, pid in records}), 2)
            self.assertNotIn(str(os.getpid()), {pid for name, pid in records})
            for call, name in zip(calls, names):
                self.assertEqual(call.args[0], [sys.executable, "-u", "-m", "unittest", "-v", name])
                self.assertIs(call.kwargs["shell"], False)
                self.assertTrue(call.kwargs["start_new_session"])
                self.assertEqual(call.kwargs["cwd"], root)
            self.assertIn("FAIL: test_result", output.getvalue())
            self.assertIn("[group 1] exit 0", output.getvalue())
            self.assertIn("[group 2] exit 1", output.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(parallel.run_groups([[names[0]]], root), 0)

    def test_cancel_terminates_and_reaps_workers_and_main_returns_failure(self):
        process = Mock(pid=12345, returncode=None)
        process.poll.side_effect = KeyboardInterrupt()
        with patch.object(parallel.subprocess, "Popen", return_value=process), patch.object(parallel.os, "killpg") as kill, redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                parallel.run_groups([["tests.example"]])
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])
        process.wait.assert_called_once_with()
        with patch.object(parallel, "discover_groups", side_effect=KeyboardInterrupt()), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(parallel.main([]), 130)

    def test_start_failure_reaps_already_started_workers(self):
        process = Mock(pid=12345, returncode=None)
        with patch.object(parallel.subprocess, "Popen", side_effect=[process, OSError("cannot spawn")]), patch.object(parallel.os, "killpg") as kill, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, "cannot spawn"):
                parallel.run_groups([["tests.one"], ["tests.two"]])
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])
        process.wait.assert_called_once_with()
        for groups in [[], [[]], [["tests.one"]] * 5]:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                parallel.run_groups(groups)

    def test_cleanup_reserves_owned_group_until_after_escalation(self):
        process = Mock(pid=12345, returncode=None)
        reaped = Mock(pid=54321, returncode=0)
        events = []
        process.wait.side_effect = lambda: events.append("wait")
        with patch.object(parallel.os, "killpg", side_effect=lambda pid, sig: events.append((pid, sig))), patch.object(parallel.time, "sleep", side_effect=lambda seconds: events.append(("grace", seconds))):
            parallel.stop_workers([process, reaped])
        self.assertEqual(events, [(12345, signal.SIGTERM), ("grace", 1),
                                  (12345, signal.SIGKILL), "wait"])
        process.poll.assert_not_called()
        reaped.poll.assert_not_called()
        reaped.wait.assert_not_called()

    def test_cleanup_kills_ignoring_descendant_after_leader_exits(self):
        # stdin is a separate emergency exit: closing it in finally releases the
        # descendant even if the cleanup implementation under test is broken.
        child = ("import os, signal\n"
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                 "print('ready', flush=True)\n"
                 "while os.read(0, 1): pass\n")
        leader = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-u', '-c', " + repr(child) + "])\n"
                  "while True: time.sleep(1)\n")
        process = subprocess.Popen([sys.executable, "-u", "-c", leader],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "descendant did not start")
            self.assertEqual(process.stdout.readline(), b"ready\n")
            started = time.monotonic()
            parallel.stop_workers([process])
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(process.returncode, -signal.SIGTERM)
            self.assertTrue(select.select([process.stdout], [], [], 2)[0],
                            "descendant survived after its leader exited")
            self.assertEqual(os.read(process.stdout.fileno(), 1), b"",
                             "descendant retained the output pipe")
        finally:
            process.stdin.close()
            # Only signal while the unreaped leader still reserves the PGID.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdout.close()

    def test_release_entrypoints_share_one_immutable_asset_writer(self):
        from tests.test_release import WorkflowConfigurationTest
        workflow = WorkflowConfigurationTest().workflow("release.yml")
        # Both a version-merge push and an explicit dispatch can resolve to
        # publish. Event-based groups would let them clobber the same draft.
        self.assertEqual(workflow["concurrency"]["group"], "adaptive-rsi-release")
        self.assertIs(workflow["concurrency"]["cancel-in-progress"], False)

    def test_bot_merge_continues_after_uploading_verified_artifact(self):
        from tests.test_release import WorkflowConfigurationTest
        parser = WorkflowConfigurationTest()
        workflow = parser.workflow('release.yml')
        trigger = workflow.get('on', workflow.get(True, workflow.get('true', {}))).get('workflow_run', {})
        self.assertEqual(trigger.get('workflows'), ['版本 PR 精确提交检查'])
        self.assertEqual(trigger.get('types'), ['completed'])
        prepare = workflow['jobs']['prepare']
        self.assertIn("github.event.workflow_run.conclusion == 'success'", prepare.get('if', ''))
        self.assertIn("github.event.workflow_run.path == '.github/workflows/release-verify.yml'", prepare['if'])
        verified = parser.workflow('release-verify.yml')
        merge_commands = '\n'.join(step.get('run', '') for step in verified['jobs']['merge']['steps'])
        # A run started by GITHUB_TOKEN does not reliably emit workflow_run, so
        # the merge job explicitly continues the canonical publish entry after
        # the verified artifact has been uploaded.
        self.assertIn('gh workflow run release.yml', merge_commands)
        self.assertIn('prepare_release=false', merge_commands)
        self.assertEqual(verified['jobs']['merge']['permissions']['actions'], 'write')

    def test_fallback_gates_run_independently_and_publish_requires_every_result(self):
        from tests.test_release import WorkflowConfigurationTest
        jobs = WorkflowConfigurationTest().workflow("release.yml")["jobs"]
        fallback = {"tests", "node", "browser", "build"}
        self.assertTrue(fallback <= set(jobs), "fallback gates must be separate jobs")
        condition = ("always() && needs.prepare.result == 'success' && "
                     "needs.prepare.outputs.mode == 'publish' && "
                     "(needs.prepare.outputs.artifact_run_id == '' || needs.reuse.result != 'success')")
        for name in sorted(fallback):
            with self.subTest(job=name):
                self.assertEqual(jobs[name]["needs"], ["prepare", "reuse"])
                self.assertEqual(jobs[name]["if"], condition)
                self.assertFalse(jobs[name].get("continue-on-error", False))
                checkout = jobs[name]["steps"][0]
                self.assertEqual(checkout["with"]["ref"], "${{ needs.prepare.outputs.sha }}")
                self.assertIs(checkout["with"]["persist-credentials"], False)
        publish = jobs["publish"]
        self.assertEqual(set(publish["needs"]), {"prepare", "reuse"} | fallback)
        # Evaluate the actual GitHub boolean condition across success, failure,
        # cancellation and skipped results; no subprocess or runner is invoked.
        from itertools import product
        from types import SimpleNamespace
        def evaluate(expression, needs):
            expression = expression.replace("&&", " and ").replace("||", " or ")
            return eval(expression, {"__builtins__": {}}, {"always": lambda: True, "needs": needs})
        for mode in ["publish", "verify", "idle", "blocked"]:
            for prepare_result in ["success", "failure", "cancelled", "skipped"]:
                for results in product(["success", "failure", "cancelled", "skipped"], repeat=5):
                    reuse_result, *gate_results = results
                    needs = SimpleNamespace(
                        prepare=SimpleNamespace(result=prepare_result, outputs=SimpleNamespace(mode=mode)),
                        reuse=SimpleNamespace(result=reuse_result),
                        **{name: SimpleNamespace(result=result)
                           for name, result in zip(sorted(fallback), gate_results)})
                    expected = (prepare_result == "success" and mode == "publish" and
                                (reuse_result == "success" or all(result == "success" for result in gate_results)))
                    self.assertEqual(evaluate(publish["if"], needs), expected,
                                     (mode, prepare_result, results))
        self.assertEqual(publish["if"],
                         "always() && needs.prepare.result == 'success' && "
                         "needs.prepare.outputs.mode == 'publish' && "
                         "(needs.reuse.result == 'success' || "
                         "(needs.tests.result == 'success' && needs.node.result == 'success' && "
                         "needs.browser.result == 'success' && needs.build.result == 'success'))")

    def test_release_parallel_gates_preserve_checks_and_permission_boundaries(self):
        from tests.test_release import WorkflowConfigurationTest
        parser = WorkflowConfigurationTest()
        jobs = parser.workflow("release.yml")["jobs"]
        commands = lambda job: "\n".join(step.get("run", "") for step in jobs[job]["steps"])
        self.assertIn("node --test tests/web_*.cjs", commands("node"))
        for command in ["node scripts/check_i18n_ast.cjs", "scripts/check_i18n.py", "scripts/check_api_contract.py"]:
            self.assertIn(command, commands("node"))
        self.assertIn("npm run test:browser", commands("browser"))
        self.assertNotIn("scripts/test_parallel.py", commands("build"))
        self.assertNotIn("npm run test:browser", commands("build"))
        for name in ["tests", "node", "browser"]:
            self.assertEqual(jobs[name]["permissions"], {"contents": "read"})
        self.assertEqual(jobs["reuse"]["permissions"], {"contents": "read", "actions": "read"})
        self.assertEqual(jobs["build"]["permissions"], {
            "contents": "read", "id-token": "write", "attestations": "write"})
        self.assertEqual(jobs["publish"]["permissions"], {"contents": "write"})
        self.assertEqual(jobs["prepare"]["permissions"]["actions"], "write")
        self.assertNotIn("gh workflow run release-verify.yml", commands("prepare"))
        for command in ["validate-verified", "--signer-workflow", "--source-ref refs/heads/main",
                        "--source-digest", "--deny-self-hosted-runners", "verify-dmg", "scripts/release_smoke.py"]:
            self.assertIn(command, commands("reuse"))
        for command in ["scripts/test_updater_signing.py", "codesign --verify --deep --strict",
                        "verify-dmg", "scripts/release_smoke.py"]:
            self.assertIn(command, commands("build"))
        verified = parser.workflow("release-verify.yml")["jobs"]
        for name in ["tests", "browser", "verify"]:
            self.assertEqual(verified[name]["needs"], "gate")
        self.assertTrue({"tests", "browser", "verify", "report"} <= set(verified["merge"]["needs"]))
        merge = "\n".join(step.get("run", "") for step in verified["merge"]["steps"])
        self.assertIn('--squash --match-head-commit "$PR_SHA"', merge)
        self.assertNotIn("--admin", merge)

    def test_release_report_uses_actual_path_and_artifacts_have_separate_sources(self):
        from tests.test_release import WorkflowConfigurationTest
        jobs = WorkflowConfigurationTest().workflow('release.yml')['jobs']
        report = next(step for step in jobs['timings']['steps']
                      if 'scripts/ci_metrics.py' in step.get('run', ''))
        self.assertIn("needs.build.result == 'success' && 'rebuild'", report['env']['RELEASE_PATH'])
        self.assertIn("needs.reuse.result == 'failure' && 'validation_failed'", report['env']['REUSE_REASON'])
        names = {}
        for source in ('reuse', 'build'):
            upload = next(step for step in jobs[source]['steps']
                          if step.get('uses', '').startswith('actions/upload-artifact@'))
            names[source] = upload['with']['name']
        self.assertNotEqual(names['reuse'], names['build'])
        download = next(step for step in jobs['publish']['steps']
                        if step.get('uses', '').startswith('actions/download-artifact@'))
        self.assertIn('needs.reuse.result', download['with']['name'])

    def test_release_paths_keep_full_parallel_gate_on_macos_python312(self):
        from tests.test_release import WorkflowConfigurationTest
        parser = WorkflowConfigurationTest()
        for name, job in [("release-verify.yml", "tests"), ("release.yml", "tests")]:
            workflow = parser.workflow(name)
            config = workflow["jobs"][job]
            with self.subTest(workflow=name):
                self.assertEqual(config["runs-on"], "macos-latest")
                setup = next(step for step in config["steps"] if step.get("uses", "").startswith("actions/setup-python@"))
                self.assertEqual(setup["with"]["python-version"], "3.12")
                commands = [step.get("run", "") for step in config["steps"]]
                gates = [command for command in commands
                         if command.startswith("python3 scripts/test_parallel.py")]
                self.assertEqual(len(gates), 1)
                self.assertNotIn("python3 -m unittest discover -v", commands)
                gate = next(step for step in config["steps"]
                            if step.get("run", "").startswith("python3 scripts/test_parallel.py"))
                self.assertFalse(gate.get("continue-on-error", False))
                self.assertNotIn("if", gate)
                build = workflow["jobs"]["verify" if name == "release-verify.yml" else "build"]
                build_commands = [step.get("run", "") for step in build["steps"]]
                self.assertTrue(any("scripts/release_smoke.py" in command for command in build_commands))
                self.assertTrue(any("verify-dmg" in command for command in build_commands))
                if name == "release-verify.yml":
                    self.assertTrue({"tests", "browser", "verify"} <= set(workflow["jobs"]["report"]["needs"]))
                    self.assertTrue({"tests", "browser", "verify", "report"} <= set(workflow["jobs"]["merge"]["needs"]))
                    self.assertEqual(build["needs"], "gate")


if __name__ == "__main__":
    unittest.main()
