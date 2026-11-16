"""Docker check command and lifecycle regressions without starting containers."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.process import Cancelled, Sandbox


class DockerCheckCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.config = copy.deepcopy(DEFAULTS)
        self.box = Sandbox(self.workspace / "state", self.config)
        for target, value in (("adaptive_rsi.process.Sandbox.backend", "docker"),
                              ("adaptive_rsi.process.shutil.which", "/fake/docker"),
                              ("adaptive_rsi.process._cached_probe", {"exit_code": 0})):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_command_preserves_arguments_and_isolation(self):
        args = ["cargo", "test", "$(touch /tmp/injected); *", "", "--name"]
        command, cwd = self.box.command(args, self.workspace)
        self.assertIsNone(cwd)
        index = command.index("--entrypoint")
        self.assertEqual(command[index:], ["--entrypoint", "cargo", self.config["sandbox"]["image"], *args[1:]])
        for flag in ("--init", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges"):
            self.assertIn(flag, command[:index])
        self.assertTrue(any(arg.startswith("--user=") for arg in command[:index]))
        self.assertTrue(any(arg.startswith("--memory=") for arg in command[:index]))
        self.assertTrue(any(arg.startswith("--pids-limit=") for arg in command[:index]))

    def test_empty_executable_is_rejected(self):
        for args in ([], [""], ["", "argument"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.box.command(args, self.workspace)

    def test_exit_and_missing_init_failure_are_reported(self):
        check = {"name": "check", "argv": ["cargo", "test"]}
        for code, stderr in ((0, ""), (23, "check failed"), (125, "docker-init not found")):
            with self.subTest(code=code):
                outcome = {"exit_code": code, "error": None, "stderr": stderr, "stdout": ""}
                with patch("adaptive_rsi.process.run_process", return_value=outcome) as run:
                    result = self.box._check(check, self.workspace)
                self.assertEqual(result["exit_code"], code)
                self.assertEqual(result["stderr"], stderr)
                self.assertEqual(result["argv"], check["argv"])
                self.assertEqual(run.call_count, 1)

    def test_timeout_and_cancellation_remove_named_container(self):
        check = {"name": "check", "argv": ["true"]}
        timeout_result = {"exit_code": -9, "error": "Command timed out", "stdout": "", "stderr": ""}
        for outcome in (timeout_result, Cancelled("Cancelled", {"exit_code": -9, "error": "Cancelled"})):
            with self.subTest(outcome=outcome):
                with patch("adaptive_rsi.process.run_process", side_effect=[outcome, {"exit_code": 0}]) as run:
                    if isinstance(outcome, Cancelled):
                        with self.assertRaises(Cancelled):
                            self.box._check(check, self.workspace)
                    else:
                        result = self.box._check(check, self.workspace)
                        self.assertEqual(result["error"], "Command timed out")
                    command = run.call_args_list[0].args[0]
                    name = command[command.index("--name") + 1]
                    self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", name])
                    self.assertEqual(run.call_args_list[0].args[2], self.config["limits"]["command_seconds"])


if __name__ == "__main__":
    unittest.main()
