"""为当前平台构建含 Python 解释器的运行器，不读取用户凭据。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
ENGINE_NAME = "adaptive-rsi-engine"
PYTHON_VERSION = "3.12.13"


def build_paths(environment, target, windows):
    if windows:
        return environment / "Scripts/python.exe", ENGINE_NAME + ".exe", f"{ENGINE_NAME}-{target}.exe"
    return environment / "bin/python", ENGINE_NAME, f"{ENGINE_NAME}-{target}"


def rust_tool(name):
    executable = name + ".exe" if os.name == "nt" else name
    return shutil.which(executable) or str(Path.home() / ".cargo/bin" / executable)


def build():
    generated = DESKTOP / ".generated"
    generated.mkdir(exist_ok=True)
    triple = next(line.split(": ", 1)[1] for line in subprocess.check_output(
        [rust_tool("rustc"), "-vV"], text=True).splitlines() if line.startswith("host: "))
    with tempfile.TemporaryDirectory(prefix="engine-", dir=generated) as directory:
        work = Path(directory)
        environment = work / "python"
        python, frozen_name, sidecar_name = build_paths(environment, triple, os.name == "nt")
        subprocess.run(["uv", "venv", "--managed-python", "--python", PYTHON_VERSION, str(environment)], check=True)
        install = ["uv", "pip", "install", "--python", str(python)]
        subprocess.run([*install, str(ROOT)], check=True)
        inventory = work / "engine-inventory.json"
        subprocess.run([str(python), "-I", str(ROOT / "scripts/sbom.py"), str(inventory)], check=True)
        # Keep the inventoried runtime versions when adding the freezer's toolchain.
        constraints = work / "runtime-constraints.txt"
        with constraints.open("w", encoding="utf-8") as output:
            subprocess.run(["uv", "pip", "freeze", "--python", str(python), "--exclude", "adaptive-rsi"],
                           stdout=output, check=True)
        subprocess.run([*install, "--constraint", str(constraints), "--requirement",
                        str(DESKTOP / "requirements-engine.txt")], check=True)

        manifest = DESKTOP / "src-tauri/Cargo.toml"
        metadata = json.loads(subprocess.check_output([rust_tool("cargo"), "metadata", "--locked",
            "--format-version", "1", "--manifest-path", str(manifest)], cwd=ROOT, timeout=300))
        receipt = {"lock_sha256": hashlib.sha256(manifest.with_name("Cargo.lock").read_bytes()).hexdigest(),
                   "packages": [{key: package.get(key) for key in ("name", "version", "license", "source")}
                                for package in metadata["packages"]]}
        subprocess.run([str(python), "-I", "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                        "--name", ENGINE_NAME, "--collect-all", "adaptive_rsi", "--paths", str(ROOT),
                        "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
                        "--specpath", str(work), str(DESKTOP / "engine_entry.py")], cwd=ROOT, check=True)
        binaries = DESKTOP / "src-tauri/binaries"
        binaries.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / "dist" / frozen_name, binaries / sidecar_name)
        inventory.replace(generated / inventory.name)
        (generated / "cargo-inventory.json").write_text(
            json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    build()
