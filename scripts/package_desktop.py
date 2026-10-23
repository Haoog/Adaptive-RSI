#!/usr/bin/env python3
"""Build checks and collection for the verified macOS ARM64 desktop bundle."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "desktop/src-tauri/target/release/bundle/dmg"
OUTPUT_DIR = ROOT / "dist"
FULL_BUILD_FILES = {
    ".github/workflows/desktop.yml",
    ".github/workflows/release-verify.yml",
    ".github/workflows/release.yml",
    "pyproject.toml",
    "scripts/package.py",
    "scripts/package_desktop.py",
}


def desktop_build_mode(paths, event="pull_request"):
    """Require a full desktop build only when desktop packaging can change."""
    if event != "pull_request":
        return "full"
    changed = {path.strip() for path in paths if path.strip()}
    if any(path in FULL_BUILD_FILES or path.startswith("desktop/") for path in changed):
        return "full"
    if any(path.startswith("adaptive_rsi/") for path in changed):
        return "smoke"
    return "skip"


def smoke_bundled_engine(binary, timeout=30):
    """Start the packaged engine and require its desktop handshake."""
    binary = Path(binary)
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("Expected one regular bundled engine executable")
    version = subprocess.check_output([str(binary), "--version"], text=True, timeout=timeout).strip()
    with tempfile.TemporaryDirectory(prefix="rsi-engine-smoke-") as directory:
        process = subprocess.Popen(
            [str(binary), "--state-dir", directory, "desktop-runtime"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], timeout)
            if not ready:
                process.kill()
                _, error = process.communicate(timeout=5)
                raise RuntimeError("Bundled engine did not return a startup handshake: " + error[-4000:])
            line = process.stdout.readline()
            if not line:
                _, error = process.communicate(timeout=5)
                raise RuntimeError("Bundled engine exited before its startup handshake: " + error[-4000:])
            session = json.loads(line)
            required = {"origin", "cookie", "token", "csrf", "owned"}
            if not required.issubset(session):
                raise ValueError("Bundled engine returned an incomplete startup handshake")
            process.stdin.close()
            if process.wait(timeout=timeout) != 0:
                raise RuntimeError("Bundled engine did not stop cleanly after stdin closed")
            return {"version": version, "startup": True, "owned": bool(session["owned"])}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in [process.stdin, process.stdout, process.stderr]:
                if stream and not stream.closed:
                    stream.close()


def verify_macos_dmg(image, mountpoint):
    """Verify the disk image, app signature and bundled engine inside it."""
    image = Path(image)
    mountpoint = Path(mountpoint)
    if not image.is_file() or image.is_symlink():
        raise ValueError("Expected a regular macOS disk image")
    mountpoint.mkdir(parents=True, exist_ok=True)
    subprocess.run(["hdiutil", "verify", str(image)], check=True)
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mountpoint), str(image)],
        check=True,
    )
    try:
        apps = sorted(path for path in mountpoint.glob("*.app") if path.is_dir() and not path.is_symlink())
        if len(apps) != 1:
            raise ValueError("Expected exactly one app in the macOS disk image")
        app = apps[0]
        engine = app / "Contents/MacOS/adaptive-rsi-engine"
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        version = subprocess.check_output([str(engine), "--version"], text=True, timeout=30).strip()
        return {"app": app.name, "engine_version": version}
    finally:
        subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True)


def package_desktop(bundle_dir=BUNDLE_DIR, output_dir=OUTPUT_DIR, version=None,
                    system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system != "Darwin" or machine not in {"arm64", "aarch64"}:
        raise ValueError("The formal desktop asset currently requires a macOS ARM64 build")
    if version is None:
        source = (ROOT / "adaptive_rsi/__init__.py").read_text()
        match = re.search(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"$', source)
        if not match:
            raise ValueError("A stable package version is required")
        version = match[1]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("A stable package version is required")
    images = sorted(Path(bundle_dir).glob("*.dmg"))
    if len(images) != 1 or not images[0].is_file() or images[0].is_symlink():
        raise ValueError("Expected exactly one regular macOS desktop disk image")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"adaptive-rsi-{version}-macos-arm64-adhoc.dmg"
    shutil.copy2(images[0], destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    (output / (destination.name + ".sha256")).write_text(digest + "  " + destination.name + "\n")
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="package",
                        choices=["package", "classify", "smoke-engine", "verify-dmg"])
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("binary", nargs="?")
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.action == "classify":
        print(desktop_build_mode(sys.stdin.readlines(), args.event))
    elif args.action == "smoke-engine":
        if not args.binary:
            parser.error("smoke-engine requires a binary")
        print(json.dumps(smoke_bundled_engine(args.binary), ensure_ascii=False))
    elif args.action == "verify-dmg":
        if not args.image:
            parser.error("verify-dmg requires --image")
        with tempfile.TemporaryDirectory(prefix="rsi-dmg-verify-") as directory:
            print(json.dumps(verify_macos_dmg(args.image, Path(directory) / "mount"), ensure_ascii=False))
    else:
        image = package_desktop()
        print(json.dumps({"asset": image.name, "sha256": image.with_name(image.name + ".sha256").name,
                          "bytes": image.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
