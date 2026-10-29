"""Explicit desktop platform support matrix regressions."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import package_desktop
from scripts.package_desktop import (SUPPORTED_TARGETS, UNKNOWN_TARGET_REASON, package_desktop as package,
                                     support_matrix, support_status)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/package_desktop.py"


class DesktopPlatformSupportStatusTest(unittest.TestCase):
    def test_macos_arm64_is_the_only_supported_production_target(self):
        supported = sorted(name for name, entry in SUPPORTED_TARGETS.items() if entry["supported"])
        self.assertEqual(supported, ["macos-arm64"])
        for system, machine in [("Darwin", "arm64"), ("Darwin", "aarch64"), ("darwin", "ARM64"),
                                ("macOS", " arm64 ")]:
            with self.subTest(system=system, machine=machine):
                status = support_status(system, machine)
                self.assertTrue(status["supported"])
                self.assertEqual(status["target"], "macos-arm64")
                self.assertIn("ad-hoc", status["reason"])

    def test_supported_target_declares_adhoc_signing_and_evidence(self):
        entry = SUPPORTED_TARGETS["macos-arm64"]
        self.assertEqual(entry["signing"], "adhoc")
        self.assertEqual(entry["evidence"]["build"], "verified")
        for key in ("install", "update"):
            self.assertIn(entry["evidence"][key], {"verified", "partial"})
        self.assertNotIn("not_available", entry["evidence"].values())

    def test_every_unsupported_target_reports_a_concrete_reason(self):
        cases = [("Darwin", "x86_64", "macos-x86_64"),
                 ("Windows", "AMD64", "windows-x86_64"),
                 ("Windows", "arm64", "windows-arm64"),
                 ("Linux", "x86_64", "linux-x86_64"),
                 ("Linux", "aarch64", "linux-arm64")]
        for system, machine, target in cases:
            with self.subTest(system=system, machine=machine):
                status = support_status(system, machine)
                self.assertFalse(status["supported"])
                self.assertEqual(status["target"], target)
                self.assertTrue(status["reason"].startswith("Unsupported:"))
                self.assertIn("evidence", status["reason"])
                entry = SUPPORTED_TARGETS[target]
                self.assertFalse(entry["supported"])
                self.assertEqual(set(entry["evidence"]), {"build", "install", "update"})
                self.assertEqual(set(entry["evidence"].values()), {"not_available"})

    def test_unknown_platforms_stay_unsupported(self):
        for system, machine in [("Plan9", "mips"), ("Darwin", "riscv64"), ("", ""),
                                (None, None), ("Linux", "sparc")]:
            with self.subTest(system=system, machine=machine):
                status = support_status(system, machine)
                self.assertEqual(status["supported"], False)
                self.assertEqual(status["target"], "unknown")
                self.assertEqual(status["reason"], UNKNOWN_TARGET_REASON)
                self.assertIn("evidence", status["reason"])

    def test_matrix_serializes_every_declared_target_with_evidence(self):
        matrix = support_matrix()
        self.assertEqual(matrix["format"], 1)
        self.assertEqual({entry["target"] for entry in matrix["targets"]}, set(SUPPORTED_TARGETS))
        for entry in matrix["targets"]:
            with self.subTest(target=entry["target"]):
                self.assertEqual(entry["supported"], SUPPORTED_TARGETS[entry["target"]]["supported"])
                self.assertEqual(set(entry["evidence"]), {"build", "install", "update"})
                self.assertTrue(entry["reason"])
        self.assertEqual(matrix["unknown"], {"supported": False, "target": "unknown",
                                            "reason": UNKNOWN_TARGET_REASON})


class DesktopPlatformPackagingGateTest(unittest.TestCase):
    def test_packaging_refuses_every_unsupported_and_unknown_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, output = root / "bundle", root / "dist"
            bundle.mkdir()
            (bundle / "Adaptive RSI_0.5.0_aarch64.dmg").write_bytes(b"image")
            for system, machine, target in [("Darwin", "x86_64", "macos-x86_64"),
                                            ("Windows", "AMD64", "windows-x86_64"),
                                            ("Linux", "arm64", "linux-arm64"),
                                            ("Plan9", "mips", "unknown")]:
                with self.subTest(system=system, machine=machine), \
                        self.assertRaisesRegex(ValueError, "Unsupported desktop target " + target):
                    package(bundle, output, "0.5.0", system, machine)
            self.assertFalse(output.exists() and any(output.iterdir()))
            asset = package(bundle, output, "0.5.0", "Darwin", "arm64")
            self.assertEqual(asset.name, "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg")
            self.assertTrue((output / (asset.name + ".sha256")).is_file())

    def test_packaging_entry_exits_clearly_instead_of_building_an_unsupported_target(self):
        with patch("scripts.package_desktop.platform.system", return_value="Linux"), \
                patch("scripts.package_desktop.platform.machine", return_value="x86_64"), \
                patch.object(sys, "argv", ["package_desktop.py"]), \
                patch("scripts.package_desktop.package_desktop") as build, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                self.assertRaises(SystemExit) as raised:
            package_desktop.main()
        self.assertEqual(raised.exception.code, 2)
        build.assert_not_called()
        self.assertIn("Unsupported desktop target linux-x86_64", stderr.getvalue())
        self.assertIn("evidence", stderr.getvalue())

    def test_packaging_entry_reaches_the_builder_on_the_supported_target(self):
        with patch("scripts.package_desktop.platform.system", return_value="Darwin"), \
                patch("scripts.package_desktop.platform.machine", return_value="arm64"), \
                patch.object(sys, "argv", ["package_desktop.py"]), \
                patch("scripts.package_desktop.package_desktop", side_effect=RuntimeError("BUILD")):
            with self.assertRaisesRegex(RuntimeError, "BUILD"):
                package_desktop.main()


class DesktopPlatformMatrixCliTest(unittest.TestCase):
    def test_cli_prints_the_json_matrix_without_building(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--print-support-matrix"],
                                cwd=ROOT, text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        matrix = json.loads(result.stdout)
        self.assertEqual(matrix["format"], 1)
        targets = {entry["target"]: entry for entry in matrix["targets"]}
        self.assertEqual(set(targets), set(SUPPORTED_TARGETS))
        self.assertTrue(targets["macos-arm64"]["supported"])
        self.assertEqual(targets["macos-arm64"]["signing"], "adhoc")
        for name, entry in targets.items():
            if name == "macos-arm64":
                continue
            self.assertFalse(entry["supported"], name)
            self.assertEqual(set(entry["evidence"].values()), {"not_available"}, name)
        self.assertNotIn("asset", matrix)


if __name__ == "__main__":
    unittest.main()
