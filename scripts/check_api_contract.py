#!/usr/bin/env python3
"""Fail when the checked-in state contract cannot describe the live sample."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check_keys(value, schema, root, path="$"):
    errors = []
    expected = schema.get("type")
    if expected == "object" and not isinstance(value, dict):
        return [path + " must be an object"]
    if expected == "array" and not isinstance(value, list):
        return [path + " must be an array"]
    if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return [path + " must be an integer"]
    if expected == "string" and not isinstance(value, str):
        return [path + " must be a string"]
    if expected == "boolean" and not isinstance(value, bool):
        return [path + " must be a boolean"]
    if expected and isinstance(expected, list) and not any(
            value is None if item == "null" else
            isinstance(value, str) if item == "string" else
            isinstance(value, bool) if item == "boolean" else
            isinstance(value, dict) if item == "object" else False for item in expected):
        return [path + " has the wrong type"]
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(path + "." + key + " is required")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                errors.extend(check_keys(value[key], child, root, path + "." + key))
    if isinstance(value, list) and "items" in schema and value:
        errors.extend(check_keys(value[0], schema["items"], root, path + "[0]"))
    return errors


def main():
    schema = json.loads((ROOT / "adaptive_rsi/schemas/api-state-v2.schema.json").read_text())
    sample = {
        "version": "0.0.0", "build": "fixture", "selected_project": None, "csrf": "fixture",
        "projects": [], "tasks": [], "runs": [], "events": [],
        "pagination": {"total": 0, "limit": 50, "offset": 0, "has_more": False},
        "config": {}, "config_revision": 0, "global_config_revision": 0, "schedules": []
    }
    errors = check_keys(sample, schema, schema)
    if errors:
        raise SystemExit("API state contract failed:\n" + "\n".join(errors))
    print("API state contract passed")


if __name__ == "__main__":
    main()
