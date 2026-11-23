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
        index = command.index(self.config["sandbox"]["image"])
        self.assertNotIn("--entrypoint", command[:index])
        self.assertEqual(command[index:], [self.config["sandbox"]["image"], *args])
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

    def test_missing_writable_target_without_approved_parent_fails_closed(self):
        missing = self.workspace / "new-output"
        with self.assertRaisesRegex(ValueError, "no approved writable directory: new-output"):
            self.box.command(["true"], self.workspace, writable=[missing])
        self.assertFalse(missing.exists())

    def test_approved_file_does_not_grant_writes_to_a_missing_sibling(self):
        approved = self.workspace / "approved.txt"
        approved.write_text("existing", encoding="utf-8")
        missing = self.workspace / "new-output.txt"
        with self.assertRaisesRegex(ValueError, "no approved writable directory: new-output.txt"):
            self.box.command(["true"], self.workspace, writable=[approved, missing])
        self.assertEqual(list(self.workspace.iterdir()), [approved])

    def test_missing_writable_target_under_approved_directory_needs_no_bind_mount(self):
        parent = self.workspace / "output"
        parent.mkdir()
        missing = parent / "new-output"
        command, _ = self.box.command(["true"], self.workspace, writable=[missing, parent])
        mounts = [command[index + 1] for index, arg in enumerate(command) if arg == "--mount"]
        self.assertEqual(mounts, [
            f"type=bind,src={self.workspace},dst=/workspace,readonly",
            f"type=bind,src={parent},dst=/workspace/output",
        ])
        self.assertFalse(missing.exists())

    def test_missing_writable_target_under_protected_directory_fails_closed(self):
        parent = self.workspace / "output"
        protected = parent / "protected"
        protected.mkdir(parents=True)
        missing = protected / "new-output"
        with self.assertRaisesRegex(ValueError, "no approved writable directory: output/protected/new-output"):
            self.box.command(["true"], self.workspace, protected=[protected], writable=[parent, missing])
        self.assertFalse(missing.exists())

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
