#!/usr/bin/env python3
"""Fail when the checked-in state contract cannot describe the live sample."""
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ROOT = Path(__file__).resolve().parents[1]


def check_keys(value, schema, root, path="$"):
    errors = []
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return check_keys(value, target, root, path)
    expected = schema.get("type")
    if isinstance(expected, list):
        if any(not check_keys(value, {**schema, "type": kind}, root, path) for kind in expected):
            return []
        return [path + " has the wrong type or constraints"]
    if expected == "null" and value is not None:
        return [path + " must be null"]
    if expected == "number" and (type(value) not in (int, float) or not math.isfinite(value)):
        return [path + " must be a finite number"]
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
    if "enum" in schema and value not in schema["enum"]:
        errors.append(path + " must match an allowed value")
    if type(value) in (int, float):
        for bound, invalid in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n)):
            if bound in schema and invalid(schema[bound]):
                errors.append(path + " violates " + bound)
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
        for index, item in enumerate(value):
            errors.extend(check_keys(item, schema["items"], root, path + "[" + str(index) + "]"))
            if len(errors) >= 100:
                break
    return errors[:100]


def main():
    from adaptive_rsi.runtime import Runtime
    from adaptive_rsi.server import Server
    from adaptive_rsi.store import Store
    schema = json.loads((ROOT / "adaptive_rsi/schemas/api-state-v2.schema.json").read_text())
    errors = []
    with tempfile.TemporaryDirectory(prefix="rsi-contract-") as directory:
        runtime = Runtime(Store(Path(directory) / "state"), agent=object())
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def validate(query):
                request = urllib.request.Request(server.local_origin + "/api/state?" + query,
                                                 headers={"Cookie": server.cookie + "=" + server.token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    errors.extend(check_keys(json.load(response), schema, schema))
            validate("sections=core")
            store = runtime.store
            for project in ("one", "two"):
                store.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,?)",
                              (project, project, str(Path(directory) / project), time.time()))
            for index, state in enumerate(("queued", "blocked", "completed")):
                task = store.add_task("one", "goal", str(index), "Contract " + state)
                store.execute("UPDATE tasks SET state=? WHERE id=?", (state, task))
                if state != "queued":
                    store.execute("INSERT INTO runs(id,project,task,state,result,started,ended) VALUES (?,?,?,?,?,?,?)",
                                  ("run-" + str(index), "one", task, state,
                                   '{' if state == "blocked" else json.dumps({"summary": "done"}),
                                   time.time(), time.time()))
            for query in ("project=one&sections=core", "project=one&sections=core,analytics",
                          "project=one&sections=core,candidates", "project=one&limit=1&offset=1",
                          "project=one&filter=history", "project=two&sections=core"):
                validate(query)
            request = urllib.request.Request(server.local_origin + "/api/contract",
                headers={"Cookie": server.cookie + "=" + server.token})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert json.load(response)["state_schema"] == schema
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            runtime.close()
    if errors:
        raise SystemExit("API state contract failed:\n" + "\n".join(errors))
    print("API state contract passed")


if __name__ == "__main__":
    main()
