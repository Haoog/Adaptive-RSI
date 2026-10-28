"""Generate an inventory from locked crates and the actual engine environment."""
import hashlib
import json
from pathlib import Path
import re


def components(root, require_inventory=False):
    root = Path(root)
    result = []
    lock = root / "desktop/src-tauri/Cargo.lock"
    cargo_receipt = root / "desktop/.generated/cargo-inventory.json"
    licenses = {}
    if cargo_receipt.exists():
        receipt = json.loads(cargo_receipt.read_text())
        if receipt["lock_sha256"] != hashlib.sha256(lock.read_bytes()).hexdigest():
            raise ValueError("Cargo inventory does not match the locked build inputs")
        licenses = {(package["name"], package["version"]): package.get("license") for package in receipt["packages"]}
    elif require_inventory:
        raise ValueError("Formal releases require Cargo dependency metadata")
    for block in lock.read_text().split("[[package]]")[1:]:
        fields = dict(re.findall(r'(?m)^([a-z_]+) = "([^"\n]*)"$', block))
        if not fields.get("source", "").startswith("registry+"):
            continue
        name, version = fields["name"], fields["version"]
        checksum = fields.get("checksum", "")
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("Locked crate is missing its content checksum: " + name)
        license_name = licenses.get((name, version))
        result.append({"type": "library", "name": name, "version": version,
                       "purl": "pkg:cargo/" + name + "@" + version,
                       "hashes": [{"alg": "SHA-256", "content": checksum}],
                       "licenses": [{"license": {"name": license_name}}] if license_name else [],
                       "properties": [{"name": "rsi:license-status", "value": "metadata" if license_name else "unavailable"}]})
    inventory = root / "desktop/.generated/engine-inventory.json"
    if inventory.exists():
        observed = json.loads(inventory.read_text())
        result.extend(observed["components"])
    else:
        # Local asset-format tests do not build an engine. Formal builds must supply it.
        result.append({"type": "application", "name": "engine-inventory-unavailable",
                       "properties": [{"name": "rsi:inventory-status", "value": "incomplete"}]})
    return sorted(result, key=lambda item: (item["name"], item.get("version", "")))


def engine_inventory(destination):
    import importlib.metadata
    import platform
    result = [{"type": "platform", "name": "CPython", "version": platform.python_version()}]
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        item = {"type": "library", "name": name, "version": distribution.version,
                "purl": "pkg:pypi/" + re.sub(r"[-_.]+", "-", name.lower()) + "@" + distribution.version}
        license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License", "")
        if license_name and len(license_name) <= 200:
            item["licenses"] = [{"license": {"name": license_name}}]
        record = distribution.read_text("RECORD")
        item["properties"] = [{"name": "rsi:distribution-record-sha256",
                               "value": hashlib.sha256(record.encode()).hexdigest() if record else "unavailable"}]
        result.append(item)
    Path(destination).write_text(json.dumps({"components": result}, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    import sys
    engine_inventory(sys.argv[1])
