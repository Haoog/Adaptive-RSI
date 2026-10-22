#!/usr/bin/env python3
"""Build a local plugin ZIP without sessions, credentials, caches or test output."""
import hashlib
import json
import re
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = root / "dist"
destination.mkdir(exist_ok=True)
version = json.loads((root / "plugin.json").read_text())["version"]
if json.loads((root / "update.json").read_text())["version"] != version:
    raise SystemExit("Plugin and update versions must match")
for filename, pattern in [("pyproject.toml", r'^version = "([^"]+)"'),
                          ("adaptive_rsi/__init__.py", r'^__version__ = "([^"]+)"')]:
    found = re.search(pattern, (root / filename).read_text(), re.M)
    if not found or found.group(1) != version:
        raise SystemExit("Release versions must match: " + filename)
bundle = destination / ("adaptive-rsi-" + version + "-plugin.zip")
files = [root / name for name in ["plugin.json", "update.json", "pyproject.toml", "package.json", "package-lock.json", "CHANGELOG.md", "CHANGELOG.en.md", "RELIABILITY.md", "RELIABILITY.en.md", "README.md", "README.en.md", "CONTRIBUTING.md", "CONTRIBUTING.en.md", "VALIDATION.md", "VALIDATION.en.md", "LIVE_VALIDATION.md", "LIVE_VALIDATION.en.md", "RELEASING.md", "RELEASING.en.md", "AGENTS.md", "LICENSE", ".gitignore", "启动 Adaptive RSI.command", ".agents/plugins/marketplace.json"]]
files += [root / name for name in ["SERVER.md", "SERVER.en.md"]]
for folder in ["adaptive_rsi", "scripts", "skills", "tests", "docs", "deploy"]:
    files += [path for path in (root / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"]
with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        info = zipfile.ZipInfo("adaptive-rsi/" + path.relative_to(root).as_posix(), (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
        archive.writestr(info, path.read_bytes())
checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
bundle.with_suffix(".zip.sha256").write_text(checksum + "  " + bundle.name + "\n")
print(bundle)
