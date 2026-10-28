"""为当前平台构建含 Python 解释器的运行器，不读取用户凭据。"""
import os
import json
import hashlib
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[2]
desktop = root / "desktop"
cargo = Path.home() / ".cargo/bin/rustc"
rustc = shutil.which("rustc") or str(cargo)
triple = next(line.split(": ", 1)[1] for line in subprocess.check_output([rustc, "-vV"], text=True).splitlines() if line.startswith("host: "))
environment = desktop / ".generated/python"
if not (environment / "bin/python").exists():
    subprocess.run(["uv", "venv", "--python", "3.12.13", str(environment)], check=True)
python = environment / "bin/python"
subprocess.run(["uv", "pip", "install", "--python", str(python), "--requirement",
                str(desktop / "requirements-engine.txt"), str(root)], check=True)
subprocess.run([str(python), str(root / "scripts/sbom.py"), str(desktop / ".generated/engine-inventory.json")], check=True)
cargo_command = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
metadata = json.loads(subprocess.check_output([cargo_command, "metadata", "--locked", "--format-version", "1",
                    "--manifest-path", str(desktop / "src-tauri/Cargo.toml")], cwd=root, timeout=300))
receipt = {"lock_sha256": hashlib.sha256((desktop / "src-tauri/Cargo.lock").read_bytes()).hexdigest(),
           "packages": [{key: package.get(key) for key in ("name", "version", "license", "source")}
                        for package in metadata["packages"]]}
(desktop / ".generated/cargo-inventory.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "adaptive-rsi-engine",
                "--collect-all", "adaptive_rsi", "--paths", str(root), "--distpath", str(desktop / ".generated/dist"),
                "--workpath", str(desktop / ".generated/build"), "--specpath", str(desktop / ".generated"),
                str(desktop / "engine_entry.py")], cwd=root, check=True)
binaries = desktop / "src-tauri/binaries"
binaries.mkdir(parents=True, exist_ok=True)
shutil.copy2(desktop / ".generated/dist/adaptive-rsi-engine", binaries / ("adaptive-rsi-engine-" + triple))
