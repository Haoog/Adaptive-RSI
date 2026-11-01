"""Guards for the replayable upgrade harness itself, without spawning anything.

The real stages are exercised by ``python3 -B scripts/upgrade_harness.py``; these
tests only pin the safety rules that keep the harness from touching the user's
installed application or claiming an acceptance it never ran.
"""
import json
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
    (bundle / "Contents/Info.plist").write_text(
        "<plist><dict><key>CFBundleShortVersionString</key><string>1.2.3</string></dict></plist>"
    )
    binary = bundle / "Contents/MacOS/adaptive-rsi"
    binary.write_text("fixture")
    binary.chmod(0o755)
    return bundle


class StageSelectionTest(unittest.TestCase):
    def test_unknown_and_empty_selections_are_rejected(self):
        with self.assertRaises(ValueError):
            harness.select_stages(["replacement-now"])
        with self.assertRaises(ValueError):
            harness.select_stages([])

    def test_all_runs_the_executable_stages_in_a_stable_order(self):
        self.assertEqual(harness.select_stages(["all"]),
                         ["signing", "receipt", "authorization", "isolation", "bad_signature", "startup_failure"])

    def test_failure_matrix_stage_aliases_are_accepted(self):
        self.assertEqual(harness.select_stages(["bad_signature"]), ["bad_signature"])
        self.assertEqual(harness.select_stages(["bad-signature"]), ["bad_signature"])
        self.assertEqual(harness.select_stages(["startup_failure"]), ["startup_failure"])
        self.assertEqual(harness.select_stages(["startup-failure"]), ["startup_failure"])

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

    def test_failure_matrix_without_a_fixture_is_blocked_not_crashed(self):
        self.assertEqual(harness.stage_bad_signature(None, None)["status"], "blocked")
        self.assertEqual(harness.stage_startup_failure(None, None)["status"], "blocked")
        with self.assertRaises(ValueError):
            harness.validate_app_source(None, "bad_signature")
        with self.assertRaises(ValueError):
            harness.validate_app_source(None, "startup_failure")

    def test_build_artifact_bundle_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = fake_bundle(directory)
            self.assertEqual(harness.validate_app_source(bundle), bundle.resolve())

    def test_escaping_symlink_in_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = fake_bundle(directory)
            outside = Path(directory) / "outside-target"
            outside.write_text("OUTSIDE")

            # Escaping symlink in Contents/MacOS
            escaping_bin = bundle / "Contents/MacOS/escape_bin"
            escaping_bin.symlink_to(outside)
            with self.assertRaises(ValueError) as ctx:
                harness.validate_app_source(bundle)
            self.assertIn("escaping symlink", str(ctx.exception))

            # Remove the escaping binary link and test escaping Info.plist link
            escaping_bin.unlink()
            (bundle / "Contents/Info.plist").unlink()
            (bundle / "Contents/Info.plist").symlink_to(outside)
            with self.assertRaises(ValueError) as ctx:
                harness.validate_app_source(bundle)
            self.assertIn("escaping symlink", str(ctx.exception))

            # Restore valid Info.plist and test internal symlink is accepted
            (bundle / "Contents/Info.plist").unlink()
            (bundle / "Contents/Info.plist").write_text(
                "<plist><dict><key>CFBundleShortVersionString</key><string>1.2.3</string></dict></plist>"
            )
            internal_link = bundle / "Contents/MacOS/internal_link"
            internal_link.symlink_to("adaptive-rsi")
            self.assertEqual(harness.validate_app_source(bundle), bundle.resolve())


class FailureMatrixStageTest(unittest.TestCase):
    def setUp(self):
        self._orig_verify_codesign = harness.verify_codesign
        harness.verify_codesign = lambda path: (0, "")

    def tearDown(self):
        harness.verify_codesign = self._orig_verify_codesign

    def test_bad_signature_stage_reports_blocked_requiring_real_updater_integration(self):
        with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as state_dir:
            bundle = fake_bundle(app_dir)
            result = harness.stage_bad_signature(bundle, state=state_dir)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "requires_real_updater_integration")
            self.assertIn("native_update.rs", result["detail"])

    def test_startup_failure_stage_reports_blocked_requiring_real_updater_integration(self):
        with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as state_dir:
            bundle = fake_bundle(app_dir)
            result = harness.stage_startup_failure(bundle, state=state_dir)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "requires_real_updater_integration")
            self.assertIn("native_update.rs", result["detail"])

    def test_expected_bad_signature_receipt_shape(self):
        receipt = harness.make_bad_signature_receipt("1.2.3", now=1700000000)
        self.assertEqual(receipt["receipt_version"], 1)
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["error_code"], "update_download_or_signature_failed")
        self.assertFalse(receipt["signature_verified"])
        self.assertEqual(receipt["from_version"], "1.2.3")
        self.assertEqual(receipt["to_version"], "9.9.9")

    def test_expected_startup_failure_receipt_shape(self):
        receipt = harness.make_startup_failure_receipt("1.2.3", now=1700000000)
        self.assertEqual(receipt["receipt_version"], 1)
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["error_code"], "update_health_failed")
        self.assertTrue(receipt["signature_verified"])
        self.assertEqual(receipt["from_version"], "1.2.3")
        self.assertEqual(receipt["to_version"], "broken-candidate")

    def test_run_executes_failure_matrix_stages_with_app(self):
        with tempfile.TemporaryDirectory() as app_dir, tempfile.TemporaryDirectory() as state_dir:
            bundle = fake_bundle(app_dir)
            report, status = harness.run(["bad_signature", "startup_failure"], app=bundle, state=state_dir)
            self.assertEqual(status, 0)
            self.assertEqual(report["passed"], [])
            self.assertEqual(report["failed"], [])
            self.assertEqual(report["blocked"], ["bad_signature", "startup_failure"])
            self.assertEqual(report["actual_acceptance"], "incomplete")

    def test_escaping_symlink_leaves_external_target_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            purelib = Path(d) / "purelib.app"
            (purelib / "Contents").mkdir(parents=True)
            (purelib / "Contents/MacOS").mkdir(parents=True)
            (purelib / "Contents/Info.plist").write_text(
                "<plist><dict><key>CFBundleShortVersionString</key><string>1.2.3</string></dict></plist>"
            )
            outside = Path(d) / "outside-bin"
            outside.write_text("ORIGINAL")
            binpath = purelib / "Contents/MacOS/purelib"
            binpath.symlink_to(outside)

            res_startup = harness.stage_startup_failure(purelib)
            self.assertEqual(res_startup["status"], "blocked")
            self.assertIn("escaping symlink", res_startup["reason"])

            res_bad_sig = harness.stage_bad_signature(purelib)
            self.assertEqual(res_bad_sig["status"], "blocked")
            self.assertIn("escaping symlink", res_bad_sig["reason"])

            self.assertEqual(outside.read_text(), "ORIGINAL")

    def test_codesign_boundary_mocking(self):
        with tempfile.TemporaryDirectory() as app_dir:
            bundle = fake_bundle(app_dir)
            harness.verify_codesign = lambda path: (0, "")
            res_pass = harness.stage_isolation(bundle, None)
            self.assertEqual(res_pass["status"], "passed")

            harness.verify_codesign = lambda path: (1, "code object is not signed at all")
            res_fail = harness.stage_isolation(bundle, None)
            self.assertEqual(res_fail["status"], "failed")
            self.assertEqual(res_fail["exit_code"], 1)
            self.assertEqual(res_fail["reason"], "the isolated copy does not keep a valid signature")


class MacOSCodesignTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "requires macOS codesign and app bundle environment")
    def test_macos_real_codesign_requires_signed_fixture(self):
        real_app = os.environ.get("REAL_SIGNED_APP")
        if not real_app or not Path(real_app).exists():
            self.skipTest("requires a genuinely signed macOS .app build artifact via REAL_SIGNED_APP")
        rc, err = harness.verify_codesign(real_app)
        self.assertEqual(rc, 0, f"real codesign verification failed: {err}")


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

    def test_blocked_failure_stages_cause_require_complete_to_exit_3(self):
        blocked_stages = [{"stage": "bad_signature", "status": "blocked", "reason": "requires_real_updater_integration"}]
        report, status = harness.summarize(blocked_stages, require_complete=True)
        self.assertEqual(status, 3)
        self.assertEqual(report["actual_acceptance"], "incomplete")
        self.assertIn("bad_signature", report["blocked"])


if __name__ == "__main__":
    unittest.main()
