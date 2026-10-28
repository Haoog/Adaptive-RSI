#!/usr/bin/env python3
"""Build checks and collection for the verified macOS ARM64 desktop bundle."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "desktop/src-tauri/target/release/bundle/dmg"
OUTPUT_DIR = ROOT / "dist"
FULL_BUILD_FILES = {
    ".github/workflows/desktop.yml",
    ".github/workflows/release-verify.yml",
    ".github/workflows/release.yml",
    "pyproject.toml",
    "rust-toolchain.toml",
    "requirements-build.txt",
    "scripts/package.py",
    "scripts/package_desktop.py",
    "scripts/sbom.py",
    "scripts/test_updater_signing.py",
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
    for name in (f"adaptive-rsi-{version}-macos-arm64-adhoc.dmg",
                 f"adaptive-rsi-{version}-macos-arm64-adhoc.dmg.sha256"):
        if (output / name).exists() or (output / name).is_symlink():
            raise ValueError("Desktop release asset already exists: " + name)
    destination = output / f"adaptive-rsi-{version}-macos-arm64-adhoc.dmg"
    staging = output / (".desktop-" + next(tempfile._get_candidate_names()))
    try:
        staging.mkdir(mode=0o700)
        staged_image = staging / destination.name
        staged_checksum = staging / (destination.name + ".sha256")
        shutil.copy2(images[0], staged_image)
        digest = hashlib.sha256(staged_image.read_bytes()).hexdigest()
        staged_checksum.write_text(digest + "  " + destination.name + "\n")
        os.link(staged_image, destination)
        try:
            os.link(staged_checksum, output / staged_checksum.name)
        except FileExistsError:
            destination.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


def package_updater(bundle_dir, output_dir, version, require_inventory=False):
    """Validate the entire release set before publishing any of its assets."""
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("A stable package version is required")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".desktop-updater-", dir=output) as directory:
        staged = Path(directory)
        feed = _package_updater(bundle_dir, staged, version, require_inventory)
        try:
            for asset in sorted(staged.iterdir()):
                destination = output / asset.name
                os.link(asset, destination)
                published.append(destination)
        except BaseException:
            for asset in published:
                asset.unlink()
            raise
    return feed


def _package_updater(bundle_dir, output_dir, version, require_inventory=False):
    """Publish a manifest only when the complete signed updater artifact exists."""
    bundle_dir, output = Path(bundle_dir), Path(output_dir)
    bundles = [p for p in bundle_dir.glob("*.app.tar.gz") if p.is_file() and not p.is_symlink()]
    if len(bundles) != 1:
        raise ValueError("Expected exactly one signed desktop updater bundle")
    signature = Path(str(bundles[0]) + ".sig")
    if not signature.is_file() or signature.is_symlink() or not 1 <= signature.stat().st_size <= 8192:
        raise ValueError("Missing desktop updater signature")
    signed = signature.read_text(encoding="ascii").strip()
    name = "adaptive-rsi-" + version + "-macos-arm64.app.tar.gz"
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundles[0], output / name)
    (output / (name + ".sig")).write_text(signed + "\n", encoding="ascii")
    subprocess.run(["cargo", "run", "--quiet", "--locked", "--manifest-path",
                    str(ROOT / "desktop/src-tauri/Cargo.toml"), "--bin", "verify_updater", "--",
                    str(output / name), str(output / (name + ".sig")), version], check=True, timeout=120)
    feed = {"version": version, "notes": "", "size": (output / name).stat().st_size, "platforms": {"darwin-aarch64": {
        "signature": signed,
        "url": "https://github.com/Haoog/Adaptive-RSI/releases/download/v" + version + "/" + name}}}
    (output / "desktop-latest.json").write_text(json.dumps(feed, indent=2) + "\n", encoding="utf-8")
    # Build a minimal, machine-readable release inventory. It contains only
    # public build inputs and proof hashes, never user state or credentials.
    sys.path.insert(0, str(ROOT))
    from scripts.sbom import components
    inventory = components(ROOT, require_inventory=require_inventory)
    if require_inventory and any(item["name"] == "engine-inventory-unavailable" for item in inventory):
        raise ValueError("Formal releases require the actual engine dependency inventory")
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(os.environ["SOURCE_DATE_EPOCH"]) if "SOURCE_DATE_EPOCH" in os.environ else None)),
                     "component": {"type": "application", "name": "adaptive-rsi", "version": version}},
        "components": [
            {"type": "application", "name": "adaptive-rsi-desktop", "version": version},
            {"type": "application", "name": "adaptive-rsi-engine", "version": version},
        ] + inventory,
    }
    (output / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    return feed


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
        if os.environ.get("RSI_UPDATER_PUBLIC_KEY"):
            source = (ROOT / "adaptive_rsi/__init__.py").read_text()
            version = re.search(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"$', source).group(1)
            package_updater(BUNDLE_DIR.parent / "macos", OUTPUT_DIR, version, require_inventory=True)
        print(json.dumps({"asset": image.name, "sha256": image.with_name(image.name + ".sha256").name,
                          "bytes": image.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
