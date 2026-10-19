#!/usr/bin/env python3
"""Build a local plugin ZIP without sessions, credentials, caches or test output."""
import hashlib
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / "dist"
destination.mkdir(exist_ok=True)
bundle = destination / "adaptive-rsi-0.1.0-plugin.zip"
files = [root / name for name in ["plugin.json", "pyproject.toml", "README.md", "README.en.md", "CONTRIBUTING.md", "CONTRIBUTING.en.md", "VALIDATION.md", "VALIDATION.en.md", "AGENTS.md", "LICENSE", ".gitignore", "启动 Adaptive RSI.command", ".agents/plugins/marketplace.json"]]
for folder in ["adaptive_rsi", "scripts", "skills", "tests"]:
    files += [path for path in (root / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, "adaptive-rsi/" + path.relative_to(root).as_posix())
checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
bundle.with_suffix(".zip.sha256").write_text(checksum + "  " + bundle.name + "\n")
print(bundle)
