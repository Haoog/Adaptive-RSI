import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.package import package_plugin
from scripts.package_desktop import desktop_build_mode, package_desktop, smoke_bundled_engine, verify_macos_dmg
from scripts.release import release_assets


class DesktopReleasePackageTest(unittest.TestCase):
    def test_generated_version_desktop_mode_requires_matching_branch_and_exact_patch(self):
        from scripts import release
        base, head = 'a' * 40, 'b' * 40
        branch = 'chore/release-v1.2.4-' + base[:12]
        with patch.object(release, 'verify_generated', return_value='1.2.4') as verify:
            self.assertEqual(release.desktop_mode(base, head, branch, 'pull_request'), 'skip')
            verify.assert_called_once_with(base, head)
            self.assertEqual(release.desktop_mode(base, head, branch, 'workflow_dispatch'), 'full')
            self.assertEqual(release.desktop_mode(base, head, branch.replace('1.2.4', '1.2.5'), 'pull_request'), 'full')
            self.assertEqual(release.desktop_mode(base, head, branch[:-1] + 'f', 'pull_request'), 'full')
        with patch.object(release, 'verify_generated', side_effect=ValueError('extra source or mode change')):
            self.assertEqual(release.desktop_mode(base, head, branch, 'pull_request'), 'full')

    def test_workflow_classifies_generated_head_before_desktop_paths(self):
        from tests.test_release import WorkflowConfigurationTest
        workflow = WorkflowConfigurationTest().workflow('desktop.yml')
        step = next(step for step in workflow['jobs']['changes']['steps'] if step.get('id') == 'classify')
        self.assertIn('HEAD_SHA', step['env'])
        self.assertIn('HEAD_REF', step['env'])
        self.assertIn('scripts.classify_changes', step['run'])
        self.assertIn('desktop_mode', step['run'])
        self.assertIn('"$HEAD_SHA"', step['run'])
        self.assertIn('"$base" "$HEAD_SHA"', step['run'])

    def test_workflow_shell_uses_base_helpers_for_untrusted_head(self):
        from scripts import release
        from tests.test_release import WorkflowConfigurationTest, git
        workflow = WorkflowConfigurationTest().workflow('desktop.yml')
        script = next(step['run'] for step in workflow['jobs']['changes']['steps'] if step.get('id') == 'classify')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['scripts/release.py', 'scripts/classify_changes.py', 'scripts/package_desktop.py',
                         'scripts/sbom.py', *release.RELEASE_FILES]:
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(release.ROOT / name, destination)
            git(root, 'init', '-q')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'base')
            base = git(root, 'rev-parse', 'HEAD')
            with patch.object(release, 'ROOT', root):
                version = release.get_version(release.read_at(base, 'adaptive_rsi/__init__.py'))
                parts = version.split('.')
                target = '.'.join(parts[:2] + [str(int(parts[2]) + 1)])
                for name, content in release.bump_files(base, target).items():
                    (root / name).write_text(content)
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'release')
            head = git(root, 'rev-parse', 'HEAD')
            git(root, 'remote', 'add', 'origin', str(root))
            git(root, 'checkout', '--detach', base)
            env = dict(os.environ, EVENT_NAME='pull_request', BASE_SHA=base, BASE_REF='main',
                       HEAD_SHA=head, HEAD_REF=f'chore/release-v{target}-{base[:12]}', GITHUB_OUTPUT=str(root / 'outputs'))
            for expected in ['skip', 'full']:
                if expected == 'full':
                    git(root, 'checkout', '--detach', head)
                    (root / 'scripts/classify_changes.py').write_text("raise RuntimeError('PR code must not run')\n")
                    git(root, 'add', '.')
                    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'tamper')
                    env['HEAD_SHA'] = git(root, 'rev-parse', 'HEAD')
                    git(root, 'checkout', '--detach', base)
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Desktop build mode: ' + expected, result.stdout)
            sbom = root / 'scripts/sbom.py'
            with sbom.open('a') as stream:
                stream.write('\n# Inventory-only change\n')
            git(root, 'add', 'scripts/sbom.py')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'sbom')
            env['HEAD_SHA'] = git(root, 'rev-parse', 'HEAD')
            env['HEAD_REF'] = 'sbom-only'
            git(root, 'checkout', '--detach', base)
            outputs = root / 'outputs'
            outputs.unlink()
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['Desktop build mode: full', 'scripts/sbom.py'])
            self.assertEqual(outputs.read_text(), 'mode=full\n')

            env['EVENT_NAME'] = 'workflow_dispatch'
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root,
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Desktop build mode: full', result.stdout)

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


class DesktopBuildScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required to exercise the desktop build entry point")

    def run_build(self, platform, extra_env, mode="build"):
        library = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "desktop/scripts/build.mjs"
            script.parent.mkdir(parents=True)
            shutil.copyfile(library / "desktop/scripts/build.mjs", script)
            shutil.copyfile(library / "desktop/scripts/build-config.mjs", script.with_name("build-config.mjs"))
            (root / "adaptive_rsi").mkdir()
            (root / "adaptive_rsi/__init__.py").write_text('__version__ = "1.2.3"\n')
            runner = root / "record.mjs"
            runner.write_text('''
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import {appendFileSync} from 'node:fs';
Object.defineProperty(process, 'platform', {value: process.env.RSI_TEST_PLATFORM});
childProcess.spawnSync = (command, args, {argv0, cwd, env}) => {
  appendFileSync('children.jsonl', JSON.stringify({command, args, argv0, cwd, env}) + '\\n');
  return {status: 0};
};
syncBuiltinESMExports();
await import('./desktop/scripts/build.mjs');
''', encoding="utf-8")
            env = {"RSI_TEST_PLATFORM": platform, "PATH": os.defpath, "TEST_RETAINED": "yes", **extra_env}
            result = subprocess.run([self.node, str(runner), mode], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=10)
            trace = root / "children.jsonl"
            children = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            config = root / "desktop/.generated/version.json"
            return result, children, json.loads(config.read_text()) if config.exists() else None

    def test_windows_uses_uv_and_node_without_shell_shims(self):
        result, children, config = self.run_build("win32", {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config, {"version": "1.2.3"})
        engine, icons, bundle = children
        self.assertEqual(engine["command"], "uv")
        self.assertEqual(len(engine["args"]), 7)
        self.assertEqual(engine["args"][:-1], ["run", "--no-project", "--managed-python", "--python",
                                               "3.12.13", "python"])
        self.assertTrue(engine["args"][-1].endswith(str(Path("desktop/scripts/engine.py"))))
        for call in (icons, bundle):
            self.assertEqual(Path(call["command"]).resolve(), Path(self.node).resolve())
            self.assertEqual(call["argv0"], "node")
            self.assertEqual(call["args"][0], "-e")
            self.assertTrue(call["args"][3].endswith(str(Path("node_modules/@tauri-apps/cli"))))
        self.assertEqual(icons["args"][4:], ["icon", "app-icon.svg"])
        self.assertEqual(bundle["args"][4:], ["build", "--config", ".generated/version.json"])

    def test_signed_config_and_notarization_credentials_only_reach_bundle(self):
        shared = {"RSI_UPDATER_PUBLIC_KEY": "public", "TAURI_SIGNING_PRIVATE_KEY": "private-test",
                  "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "updater-pass-test",
                  "APPLE_SIGNING_IDENTITY": "Developer ID Application: Fixture"}
        for credentials in (
            {"APPLE_ID": "test-user", "APPLE_PASSWORD": "test-pass", "APPLE_TEAM_ID": "test-team"},
            {"APPLE_API_KEY": "test-key", "APPLE_API_ISSUER": "test-issuer", "APPLE_API_KEY_PATH": "test-path"},
        ):
            with self.subTest(credentials=sorted(credentials)):
                env = {**shared, **credentials}
                result, children, config = self.run_build("darwin", env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config, {"version": "1.2.3", "bundle": {"createUpdaterArtifacts": True,
                    "macOS": {"signingIdentity": env["APPLE_SIGNING_IDENTITY"], "hardenedRuntime": True}},
                    "plugins": {"updater": {"pubkey": "public"}}})
                self.assertEqual(len(children), 3)
                for child in children[:2]:
                    self.assertFalse(set(env).intersection(child["env"]))
                    self.assertEqual(child["env"]["TEST_RETAINED"], "yes")
                self.assertEqual({key: children[2]["env"][key] for key in env}, env)

    def test_dev_and_unsigned_builds_do_not_receive_unused_signing_secrets(self):
        secrets = {"TAURI_SIGNING_PRIVATE_KEY": "unused-private-test", "APPLE_PASSWORD": "unused-pass-test"}
        for mode in ("dev", "build"):
            with self.subTest(mode=mode):
                result, children, config = self.run_build("darwin", secrets, mode=mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config, {"version": "1.2.3"})
                self.assertEqual(children[-1]["args"][4], mode)
                for child in children:
                    self.assertFalse(set(secrets).intersection(child["env"]))

    def test_invalid_signing_configuration_stops_before_spawning_children(self):
        for platform, env, detail in (
            ("darwin", {"RSI_UPDATER_PUBLIC_KEY": "test"}, "TAURI_SIGNING_PRIVATE_KEY"),
            ("darwin", {"APPLE_SIGNING_IDENTITY": "Developer ID", "APPLE_ID": "test"}, "notarization"),
            ("win32", {"APPLE_SIGNING_IDENTITY": "Developer ID"}, "requires macOS"),
        ):
            with self.subTest(platform=platform, detail=detail):
                result, children, config = self.run_build(platform, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(detail, result.stderr)
                self.assertEqual(children, [])
                self.assertIsNone(config)


class DesktopSourceArchiveTest(unittest.TestCase):
    def test_plugin_zip_includes_desktop_rust_modules(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            bundle = package_plugin(source, Path(directory))
            digest, name = bundle.with_suffix(".zip.sha256").read_text().split()
            self.assertEqual((digest, name), (hashlib.sha256(bundle.read_bytes()).hexdigest(), bundle.name))
            with zipfile.ZipFile(bundle) as archive:
                for name in ("main.rs", "update_receipt.rs", "update_check_race.rs", "update_boundary_tests.rs",
                             "bin/verify_updater.rs"):
                    path = "desktop/src-tauri/src/" + name
                    self.assertEqual(archive.read("adaptive-rsi/" + path), (source / path).read_bytes())
                desktop_parts = {part for name in archive.namelist() if name.startswith("adaptive-rsi/desktop/")
                                 for part in Path(name).parts}
                self.assertTrue(desktop_parts.isdisjoint({".generated", "node_modules", "target", "binaries"}))


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
