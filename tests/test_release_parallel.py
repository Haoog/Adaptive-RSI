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

    def test_release_paths_keep_full_parallel_gate_on_macos_python312(self):
        from tests.test_release import WorkflowConfigurationTest
        parser = WorkflowConfigurationTest()
        for name, job in [("release-verify.yml", "tests"), ("release.yml", "build")]:
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
