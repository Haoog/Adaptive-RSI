#!/usr/bin/env python3
"""Verify updater signatures with disposable local test keys, never release credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
import email.parser

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "desktop/node_modules/.bin/tauri"
MANIFEST = ROOT / "desktop/src-tauri/Cargo.toml"


def run(argv, env, expect=0):
    result = subprocess.run([str(arg) for arg in argv], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    if (result.returncode == 0) != (expect == 0):
        raise RuntimeError("Updater signature test failed: " + result.stderr[-1000:])


def cargo():
    """Use an explicit cargo when the shell PATH does not include rustup."""
    found = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    if not Path(found).exists():
        raise RuntimeError("cargo is required for updater signature verification")
    return found


def check_signed_cross_version_upgrade(folder, key, current, command, env):
    """Sign both sides of a real N -> N+1 upgrade with one disposable test key.

    The check is about the signature/version binding only: it proves a newer
    artifact cannot be accepted as the old version, that the old signature does
    not cover the new bytes, and that swapping payloads is rejected. It does not
    install anything.
    """
    next_archive = folder / "app-next.tar.gz"
    next_archive.write_bytes(b"test-only update artifact for the next version")
    run([CLI, "signer", "sign", "-f", key, "-p", "", "--app-version", "1.2.4", next_archive], env)
    next_command = [*command[:-2], next_archive, str(next_archive) + ".sig"]
    run([*next_command, "1.2.4"], env)
    for version in ("1.2.3", "1.2.5"):
        run([*next_command, version], env, expect=1)
    run([*command, "1.2.4"], env, expect=1)
    # A payload swap keeps a valid signature file next to the wrong bytes.
    swapped = folder / "app-swapped.tar.gz"
    swapped.write_bytes(next_archive.read_bytes())
    shutil.copyfile(str(current) + ".sig", str(swapped) + ".sig")
    run([*command[:-2], swapped, str(swapped) + ".sig", "1.2.3"], env, expect=1)
    # The two versions must not share an identical artifact, otherwise the test
    # above would not prove anything about a real cross-version upgrade.
    if current.read_bytes() == next_archive.read_bytes():
        raise RuntimeError("cross-version fixtures are identical")


def main():
    env = {key: value for key, value in os.environ.items() if not key.startswith("TAURI_SIGNING_")}
    with tempfile.TemporaryDirectory(prefix="rsi-signature-test-") as directory:
        folder = Path(directory)
        key, archive = folder / "key", folder / "app.tar.gz"
        archive.write_bytes(b"test-only update artifact")
        run([CLI, "signer", "generate", "--ci", "-p", "", "-w", key], env)
        run([CLI, "signer", "sign", "-f", key, "-p", "", "--app-version", "1.2.3", archive], env)
        env["RSI_UPDATER_PUBLIC_KEY"] = (folder / "key.pub").read_text()
        command = [cargo(), "run", "--quiet", "--locked", "--manifest-path", MANIFEST,
                   "--bin", "verify_updater", "--", archive, str(archive) + ".sig"]
        run([*command, "1.2.3"], env)
        run([*command, "1.2.4"], env, expect=1)
        check_signed_cross_version_upgrade(folder, key, archive, command, env)
        archive.write_bytes(b"altered artifact")
        run([*command, "1.2.3"], env, expect=1)
    with tempfile.TemporaryDirectory(prefix="rsi-sbom-") as directory:
        wheel = Path(directory) / "adaptive_rsi-0.0.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as package:
            package.writestr("adaptive_rsi-0.0.0.dist-info/METADATA",
                             "Name: adaptive-rsi\nVersion: 0.0.0\nLicense: MIT\nRequires-Dist: example>=1\n")
            package.writestr("adaptive_rsi-0.0.0.dist-info/RECORD", "")
        sbom = generate_sbom(wheel)
        if sbom["metadata"]["component"]["name"] != "adaptive-rsi":
            raise RuntimeError("SBOM metadata was not generated from the wheel")
        if sbom["components"][0]["properties"][0]["value"] != "example>=1":
            raise RuntimeError("SBOM lost dependency metadata")
    print("Updater signature, version and tampering checks passed")


def generate_sbom(wheel):
    """Build CycloneDX data from the wheel rather than a stale manual package list."""
    with zipfile.ZipFile(wheel) as package:
        metadata_name = next(name for name in package.namelist() if name.endswith(".dist-info/METADATA"))
        message = email.parser.Parser().parsestr(package.read(metadata_name).decode("utf-8"))
    name, version = message.get("Name", ""), message.get("Version", "")
    component = {
        "type": "library", "name": name, "version": version,
        "bom-ref": "pkg:pypi/" + name.lower() + "@" + version,
        "licenses": [{"license": {"name": message.get("License", "NOASSERTION")}}],
        "properties": [{"name": "requires_dist", "value": value}
                       for value in message.get_all("Requires-Dist", []) or []],
    }
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": component}, "components": [component]}


if __name__ == "__main__":
    main()
