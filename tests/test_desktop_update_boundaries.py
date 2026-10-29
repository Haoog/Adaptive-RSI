"""Isolated release inventory and publication boundary regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.sbom import components
from scripts.package_desktop import desktop_build_mode, package_updater


class DesktopInventoryBoundaryTest(unittest.TestCase):
    def fixture(self, root):
        lock = root / "desktop/src-tauri/Cargo.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text('version = 3\n[[package]]\nname = "fixture"\nversion = "1.0.0"\n'
                        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                        'checksum = "' + "a" * 64 + '"\n')
        generated = root / "desktop/.generated"
        generated.mkdir()
        receipt = {"lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(), "packages": [
            {"name": "fixture", "version": "1.0.0", "license": "MIT",
             "source": "registry+https://github.com/rust-lang/crates.io-index"}]}
        (generated / "cargo-inventory.json").write_text(json.dumps(receipt))
        return generated, receipt

    def test_formal_inventory_requires_every_locked_crate_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated, receipt = self.fixture(root)
            receipt["packages"] = []
            (generated / "cargo-inventory.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "metadata.*fixture"):
                components(root, require_inventory=True)

    def test_dependency_inventory_changes_require_full_desktop_build(self):
        self.assertEqual(desktop_build_mode(["scripts/sbom.py"]), "full")

    def test_failed_inventory_validation_does_not_publish_partial_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            (bundle / "app.app.tar.gz").write_bytes(b"test archive")
            (bundle / "app.app.tar.gz.sig").write_text("test signature")
            with patch("scripts.package_desktop.subprocess.run"), \
                    patch("scripts.sbom.components", side_effect=ValueError("inventory invalid")):
                with self.assertRaisesRegex(ValueError, "inventory invalid"):
                    package_updater(bundle, output, "1.2.3", require_inventory=True)
            self.assertFalse(output.exists() and any(output.iterdir()))

    def test_existing_release_asset_is_preserved_and_new_assets_are_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            output.mkdir()
            (bundle / "app.app.tar.gz").write_bytes(b"test archive")
            (bundle / "app.app.tar.gz.sig").write_text("test signature")
            existing = output / "desktop-latest.json"
            existing.write_bytes(b"existing release")
            with patch("scripts.package_desktop.subprocess.run"), patch("scripts.sbom.components", return_value=[]):
                with self.assertRaises(FileExistsError):
                    package_updater(bundle, output, "1.2.3")
            self.assertEqual(existing.read_bytes(), b"existing release")
            self.assertEqual([path.name for path in output.iterdir()], [existing.name])

    def test_release_inventory_timestamp_uses_reproducible_build_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            (bundle / "app.app.tar.gz").write_bytes(b"test archive")
            (bundle / "app.app.tar.gz.sig").write_text("test signature")
            with patch("scripts.package_desktop.subprocess.run"), patch("scripts.sbom.components", return_value=[]), \
                    patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "0"}):
                package_updater(bundle, output, "1.2.3")
            self.assertEqual(json.loads((output / "sbom.cdx.json").read_text())["metadata"]["timestamp"],
                             "1970-01-01T00:00:00Z")

    def test_update_feed_contains_only_this_versions_bilingual_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            (bundle / "app.app.tar.gz").write_bytes(b"test archive")
            (bundle / "app.app.tar.gz.sig").write_text("test signature")
            for filename, note in [("CHANGELOG.md", "- 保留运行保护。"),
                                   ("CHANGELOG.en.md", "- Preserve run holds.")]:
                (root / filename).write_text(
                    "# Changelog\n\n## Unreleased\n\n- future changes\n\n## 1.2.3\n\n"
                    "<!-- adaptive-rsi-release:1.2.3;base:" + "a" * 40 + " -->\n\n" + note +
                    "\n\n## 1.2.2\n\n- previous changes\n", encoding="utf-8")
            with patch("scripts.package_desktop.ROOT", root), \
                    patch("scripts.package_desktop.subprocess.run"), patch("scripts.sbom.components", return_value=[]):
                package_updater(bundle, output, "1.2.3")
            notes = json.loads((output / "desktop-latest.json").read_text())["notes"]
            self.assertIn("保留运行保护", notes)
            self.assertIn("Preserve run holds", notes)
            self.assertNotIn("adaptive-rsi-release:", notes)
            self.assertNotIn("future changes", notes)
            self.assertNotIn("previous changes", notes)


if __name__ == "__main__":
    unittest.main()