"""Guards for the replayable upgrade harness itself, without spawning anything.

The real stages are exercised by ``python3 -B scripts/upgrade_harness.py``; these
tests only pin the safety rules that keep the harness from touching the user's
installed application or claiming an acceptance it never ran.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import upgrade_harness as harness


def fake_bundle(parent, name="Adaptive RSI.app"):
    bundle = Path(parent) / name
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_text("<plist/>")
    (bundle / "Contents/MacOS/adaptive-rsi").write_text("fixture")
    return bundle


class StageSelectionTest(unittest.TestCase):
    def test_unknown_and_empty_selections_are_rejected(self):
        with self.assertRaises(ValueError):
            harness.select_stages(["replacement-now"])
        with self.assertRaises(ValueError):
            harness.select_stages([])

    def test_all_runs_the_executable_stages_in_a_stable_order(self):
        self.assertEqual(harness.select_stages(["all"]),
                         ["signing", "receipt", "authorization", "isolation"])

    def test_replacement_is_never_claimed_without_running_it(self):
        stage = harness.stage_replacement()
        self.assertEqual(stage["status"], "blocked")
        self.assertEqual(stage["reason"], "requires_real_signed_tauri_installer")


class AppSourceGuardTest(unittest.TestCase):
    def test_installed_application_is_refused(self):
        for path in ("/Applications/Adaptive RSI.app", str(Path.home() / "Applications/Adaptive RSI.app")):
            with self.assertRaises(ValueError, msg=path):
                harness.validate_app_source(path)

    def test_symlinked_bundle_and_non_bundle_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = fake_bundle(directory)
            link = Path(directory) / "linked.app"
            os.symlink(bundle, link)
            with self.assertRaises(ValueError):
                harness.validate_app_source(link)
            with self.assertRaises(ValueError):
                harness.validate_app_source(Path(directory))
            incomplete = Path(directory) / "Broken.app"
            (incomplete / "Contents").mkdir(parents=True)
            with self.assertRaises(ValueError):
                harness.validate_app_source(incomplete)

    def test_isolation_without_a_fixture_is_blocked_not_crashed(self):
        self.assertEqual(harness.stage_isolation(None, None)["status"], "blocked")
        with self.assertRaises(ValueError):
            harness.validate_app_source(None)

    def test_build_artifact_bundle_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = fake_bundle(directory)
            self.assertEqual(harness.validate_app_source(bundle), bundle.resolve())


class SummaryTest(unittest.TestCase):
    def test_completion_requires_a_real_replacement_stage(self):
        staging = [{"stage": name, "status": "passed"} for name in ("signing", "receipt", "isolation")]
        report, status = harness.summarize(staging)
        self.assertEqual(report["actual_acceptance"], "incomplete")
        self.assertEqual(report["acceptance_scope"], "staging_only")
        self.assertEqual(status, 0)
        self.assertTrue(report["note"])
        # Requiring completeness without the real replacement must not pass.
        self.assertEqual(harness.summarize(staging, require_complete=True)[1], 3)

        with_replacement = staging + [{"stage": "replacement", "status": "passed"}]
        report, status = harness.summarize(with_replacement)
        self.assertEqual(report["actual_acceptance"], "complete")
        self.assertEqual(report["acceptance_scope"], "real_installer_replacement")
        self.assertEqual(status, 0)

    def test_failures_and_required_blocked_stages_change_the_exit_code(self):
        failed = [{"stage": "signing", "status": "failed"}]
        self.assertEqual(harness.summarize(failed)[1], 1)
        blocked = [{"stage": "replacement", "status": "blocked"}]
        self.assertEqual(harness.summarize(blocked)[1], 0)
        self.assertEqual(harness.summarize(blocked, require_complete=True)[1], 3)
        self.assertEqual(harness.summarize(blocked, require_complete=True)[0]["actual_acceptance"], "incomplete")

    def test_release_signing_credentials_never_reach_a_stage(self):
        env = {"PATH": "/usr/bin", "TAURI_SIGNING_PRIVATE_KEY": "secret",
               "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "secret", "HOME": "/tmp"}
        cleaned = harness.strip_signing_env(env)
        self.assertEqual(sorted(cleaned), ["HOME", "PATH"])


if __name__ == "__main__":
    unittest.main()
