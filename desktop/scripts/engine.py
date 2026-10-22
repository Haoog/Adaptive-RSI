"""为当前平台构建含 Python 解释器的运行器，不读取用户凭据。"""
import os
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
    subprocess.run(["uv", "venv", "--python", "3.12", str(environment)], check=True)
python = environment / "bin/python"
subprocess.run(["uv", "pip", "install", "--python", str(python), "pyinstaller>=6,<7", str(root)], check=True)
subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "adaptive-rsi-engine",
                "--collect-all", "adaptive_rsi", "--paths", str(root), "--distpath", str(desktop / ".generated/dist"),
                "--workpath", str(desktop / ".generated/build"), "--specpath", str(desktop / ".generated"),
                str(desktop / "engine_entry.py")], cwd=root, check=True)
binaries = desktop / "src-tauri/binaries"
binaries.mkdir(parents=True, exist_ok=True)
shutil.copy2(desktop / ".generated/dist/adaptive-rsi-engine", binaries / ("adaptive-rsi-engine-" + triple))
