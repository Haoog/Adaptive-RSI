#!/usr/bin/env python3
"""Build a local plugin ZIP without sessions, credentials, caches or test output."""
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path


def package_plugin(root, destination):
    version = json.loads((root / "plugin.json").read_text())["version"]
    if json.loads((root / "update.json").read_text())["version"] != version:
        raise SystemExit("Plugin and update versions must match")
    for filename, pattern in [("pyproject.toml", r'^version = "([^"]+)"'),
                              ("adaptive_rsi/__init__.py", r'^__version__ = "([^"]+)"')]:
        found = re.search(pattern, (root / filename).read_text(), re.M)
        if not found or found.group(1) != version:
            raise SystemExit("Release versions must match: " + filename)

    files = [root / name for name in [
        "plugin.json", "update.json", "pyproject.toml", "requirements-build.txt", "rust-toolchain.toml",
        "package.json", "package-lock.json", "CHANGELOG.md", "CHANGELOG.en.md", "RELIABILITY.md",
        "RELIABILITY.en.md", "README.md", "README.en.md", "CONTRIBUTING.md", "CONTRIBUTING.en.md",
        "VALIDATION.md", "VALIDATION.en.md", "LIVE_VALIDATION.md", "LIVE_VALIDATION.en.md", "RELEASING.md",
        "RELEASING.en.md", "AGENTS.md", "LICENSE", ".gitignore", "启动 Adaptive RSI.command",
        ".agents/plugins/marketplace.json", "SERVER.md", "SERVER.en.md", "DESKTOP.md", "DESKTOP.en.md",
        "EXPERIENCE.md", "EXPERIENCE.en.md",
    ]]
    files += [root / "desktop" / name for name in [
        "package.json", "package-lock.json", "app-icon.svg", "engine_entry.py", "scripts/build.mjs",
        "scripts/engine.py", "requirements-engine.txt", "src-tauri/Cargo.toml", "src-tauri/Cargo.lock",
        "src-tauri/build.rs", "src-tauri/tauri.conf.json", "src-tauri/capabilities/default.json",
    ]]
    # Treat the Rust source tree as a unit, including nested binaries and test modules.
    rust_source = Path("desktop/src-tauri/src")
    if (root / ".git").exists():
        tracked = set(subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0"))
        files += [root / name for name in tracked
                  if name.startswith(rust_source.as_posix() + "/") and name.endswith(".rs")]
    else:
        tracked = None
        files += [path for path in (root / rust_source).rglob("*.rs") if path.is_file() or path.is_symlink()]
    for folder in ["adaptive_rsi", "scripts", "skills", "tests", "docs", "deploy"]:
        files += [path for path in (root / folder).rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
    if tracked is not None:
        files = [path for path in files if path.relative_to(root).as_posix() in tracked]

    destination.mkdir(exist_ok=True)
    bundle = destination / ("adaptive-rsi-" + version + "-plugin.zip")
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            relative = path.relative_to(root)
            if path.is_symlink() or any((root / parent).is_symlink() for parent in relative.parents):
                raise SystemExit("Package input must not be a symbolic link: " + str(relative))
            if not path.is_file():
                raise SystemExit("Package input must be a regular file: " + str(relative))
            info = zipfile.ZipInfo("adaptive-rsi/" + relative.as_posix(), (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            archive.writestr(info, path.read_bytes())
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.with_suffix(".zip.sha256").write_text(checksum + "  " + bundle.name + "\n")
    return bundle


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    print(package_plugin(root, root / "dist"))
