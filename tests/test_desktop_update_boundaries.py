"""Isolated release inventory and publication boundary regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.scripts import engine
from scripts.sbom import components, engine_inventory
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
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/desktop.yml"
        pull_request = workflow.read_text().split("  pull_request:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
        self.assertIn("      - 'scripts/sbom.py'", pull_request.splitlines())

    def test_formal_inventory_rejects_missing_and_legacy_engine_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated, _ = self.fixture(root)
            inventory = generated / "engine-inventory.json"
            with self.assertRaisesRegex(ValueError, "actual engine dependency inventory"):
                components(root, require_inventory=True)
            inventory.write_text(json.dumps({"components": [{"name": "PyInstaller"}]}))
            with self.assertRaisesRegex(ValueError, "runtime-only"):
                components(root, require_inventory=True)

    def test_engine_records_runtime_dependencies_before_installing_build_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated, _ = self.fixture(root)
            installed = []
            commands = []
            windows = engine.os.name == "nt"
            target = "x86_64-pc-windows-msvc" if windows else "aarch64-apple-darwin"
            frozen_name = "adaptive-rsi-engine.exe" if windows else "adaptive-rsi-engine"
            sidecar = f"adaptive-rsi-engine-{target}" + (".exe" if windows else "")

            def run(args, **kwargs):
                commands.append(args)
                if args[:3] == ["uv", "pip", "install"]:
                    installed.append("pyinstaller" if "--requirement" in args else "adaptive-rsi")
                elif any(Path(arg).name == "sbom.py" for arg in args):
                    from importlib.metadata import PathDistribution
                    for name in installed:
                        info = root / (name + ".dist-info")
                        info.mkdir()
                        (info / "METADATA").write_text(f"Name: {name}\nVersion: 1\n")
                    with patch("importlib.metadata.distributions", return_value=[
                        PathDistribution(root / (name + ".dist-info")) for name in installed
                    ]):
                        engine_inventory(args[-1])
                elif "PyInstaller" in args:
                    dist = Path(args[args.index("--distpath") + 1])
                    dist.mkdir()
                    (dist / frozen_name).write_bytes(b"frozen engine")

            def check_output(args, **kwargs):
                if "-vV" in args:
                    return f"host: {target}\n"
                return json.dumps({"packages": [{"name": "fixture", "version": "1.0.0", "license": "MIT",
                    "source": "registry+https://github.com/rust-lang/crates.io-index"}]})

            with patch.object(engine, "ROOT", root), patch.object(engine, "DESKTOP", root / "desktop"), \
                    patch.object(engine, "rust_tool", side_effect=lambda name: name), \
                    patch.object(engine.subprocess, "run", side_effect=run), \
                    patch.object(engine.subprocess, "check_output", side_effect=check_output):
                engine.build()
            self.assertEqual(installed, ["adaptive-rsi", "pyinstaller"])
            self.assertTrue(any("--constraint" in call for call in commands))
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            (bundle / "app.app.tar.gz").write_bytes(b"test archive")
            (bundle / "app.app.tar.gz.sig").write_text("test signature")
            with patch("scripts.package_desktop.ROOT", root), patch("scripts.package_desktop.subprocess.run"):
                package_updater(bundle, output, "1.2.3", require_inventory=True)
            sbom = json.loads((output / "sbom.cdx.json").read_text(encoding="utf-8"))
            self.assertEqual({item["name"] for item in sbom["components"]},
                             {"adaptive-rsi-desktop", "adaptive-rsi-engine", "CPython", "adaptive-rsi", "fixture"})
            self.assertEqual((root / "desktop/src-tauri/binaries" / sidecar).read_bytes(), b"frozen engine")
            self.assertEqual({path.name for path in generated.iterdir()},
                             {"cargo-inventory.json", "engine-inventory.json"})

    def test_engine_paths_match_tauri_sidecar_conventions(self):
        venv = Path("fixture") / "python"
        self.assertEqual(engine.build_paths(venv, "x86_64-pc-windows-msvc", windows=True),
                         (venv / "Scripts/python.exe", "adaptive-rsi-engine.exe",
                          "adaptive-rsi-engine-x86_64-pc-windows-msvc.exe"))
        self.assertEqual(engine.build_paths(venv, "aarch64-apple-darwin", windows=False),
                         (venv / "bin/python", "adaptive-rsi-engine", "adaptive-rsi-engine-aarch64-apple-darwin"))

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