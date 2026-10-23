import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.package_desktop import desktop_build_mode, package_desktop, smoke_bundled_engine, verify_macos_dmg
from scripts.release import release_assets


class DesktopReleasePackageTest(unittest.TestCase):
    def test_path_classification_runs_full_build_only_for_desktop_packaging_inputs(self):
        self.assertEqual(desktop_build_mode(["adaptive_rsi/web/app.js"]), "smoke")
        for path in ["desktop/src-tauri/tauri.conf.json", "desktop/package-lock.json",
                     "scripts/package.py", "scripts/package_desktop.py", "pyproject.toml",
                     ".github/workflows/release.yml"]:
            with self.subTest(path=path):
                self.assertEqual(desktop_build_mode(["adaptive_rsi/web/app.js", path]), "full")
        self.assertEqual(desktop_build_mode(["README.md"]), "skip")
        self.assertEqual(desktop_build_mode([], "workflow_dispatch"), "full")

    def test_copies_one_verified_arm64_disk_image_with_the_package_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, output = root / "bundle", root / "dist"
            bundle.mkdir()
            (bundle / "Adaptive RSI_0.5.0_aarch64.dmg").write_bytes(b"verified image")
            asset = package_desktop(bundle, output, "0.5.0", "Darwin", "arm64")
            self.assertEqual(asset.name, "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg")
            self.assertEqual(asset.read_bytes(), b"verified image")
            self.assertEqual((output / (asset.name + ".sha256")).read_text(),
                             hashlib.sha256(b"verified image").hexdigest() + "  " + asset.name + "\n")

    def test_rejects_other_platforms_invalid_versions_and_ambiguous_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle, output = root / "bundle", root / "dist"
            bundle.mkdir()
            image = bundle / "Adaptive RSI.dmg"
            image.write_bytes(b"image")
            for system, machine, version in [
                ("Linux", "arm64", "0.5.0"),
                ("Darwin", "x86_64", "0.5.0"),
                ("Darwin", "arm64", "../../bad"),
            ]:
                with self.subTest(system=system, machine=machine, version=version), self.assertRaises(ValueError):
                    package_desktop(bundle, output, version, system, machine)
            (bundle / "Second.dmg").write_bytes(b"second image")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                package_desktop(bundle, output, "0.5.0", "Darwin", "arm64")

    def test_disk_image_verification_checks_signature_engine_and_detaches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "adaptive-rsi.dmg"
            image.write_bytes(b"image")
            mountpoint = root / "mount"
            app = mountpoint / "Adaptive RSI.app"
            engine = app / "Contents/MacOS/adaptive-rsi-engine"
            engine.parent.mkdir(parents=True)
            engine.write_bytes(b"engine")
            with patch("scripts.package_desktop.subprocess.run") as run, \
                    patch("scripts.package_desktop.subprocess.check_output", return_value="Adaptive RSI 0.5.0\n") as output:
                result = verify_macos_dmg(image, mountpoint)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["hdiutil", "verify", str(image)])
            self.assertEqual(commands[1][:5], ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint"])
            self.assertEqual(commands[2], ["codesign", "--verify", "--deep", "--strict", str(app)])
            self.assertEqual(commands[3], ["hdiutil", "detach", str(mountpoint)])
            output.assert_called_once_with([str(engine), "--version"], text=True, timeout=30)
            self.assertEqual(result, {"app": "Adaptive RSI.app", "engine_version": "Adaptive RSI 0.5.0"})

    def test_bundled_engine_smoke_requires_a_real_desktop_handshake(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "adaptive-rsi-engine"
            binary.write_text(
                "#!" + sys.executable + "\n"
                "import json, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('Adaptive RSI 0.5.0')\n"
                "    raise SystemExit(0)\n"
                "print(json.dumps({'origin':'http://127.0.0.1:1','cookie':'rsi','token':'t','csrf':'c','owned':True}), flush=True)\n"
                "sys.stdin.read()\n"
            )
            binary.chmod(0o700)
            self.assertEqual(smoke_bundled_engine(binary, timeout=5),
                             {"version": "Adaptive RSI 0.5.0", "startup": True, "owned": True})


class DesktopReleaseAssetsTest(unittest.TestCase):
    def test_formal_release_requires_the_exact_desktop_bundle_and_one_wheel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["adaptive-rsi-0.5.0-plugin.zip", "adaptive-rsi-0.5.0-plugin.zip.sha256",
                     "adaptive_rsi-0.5.0-py3-none-any.whl", "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg",
                     "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg.sha256"]
            for name in names:
                (root / name).write_bytes(b"verified")
            (root / "unrelated.log").write_text("ignored")
            self.assertEqual({path.name for path in release_assets(root, "0.5.0")}, set(names))
            (root / "adaptive_rsi-0.5.0-another.whl").write_bytes(b"ambiguous")
            with self.assertRaisesRegex(ValueError, "one wheel"):
                release_assets(root, "0.5.0")

    def test_missing_or_linked_desktop_package_blocks_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ["adaptive-rsi-0.5.0-plugin.zip", "adaptive-rsi-0.5.0-plugin.zip.sha256",
                         "adaptive_rsi-0.5.0-py3-none-any.whl"]:
                (root / name).write_bytes(b"verified")
            with self.assertRaisesRegex(ValueError, "desktop DMG"):
                release_assets(root, "0.5.0")
            (root / "source.dmg").write_bytes(b"image")
            (root / "adaptive-rsi-0.5.0-macos-arm64-adhoc.dmg").symlink_to(root / "source.dmg")
            with self.assertRaisesRegex(ValueError, "desktop DMG"):
                release_assets(root, "0.5.0")


if __name__ == "__main__":
    unittest.main()
