#!/usr/bin/env python3
"""Collect the verified macOS ARM64 desktop bundle for a formal release."""
import hashlib
import json
import platform
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "desktop/src-tauri/target/release/bundle/dmg"
OUTPUT_DIR = ROOT / "dist"


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
    image = package_desktop()
    print(json.dumps({"asset": image.name, "sha256": image.with_name(image.name + ".sha256").name,
                      "bytes": image.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
