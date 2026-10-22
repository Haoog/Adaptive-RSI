import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.package_desktop import package_desktop
from scripts.release import release_assets


class DesktopReleasePackageTest(unittest.TestCase):
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
