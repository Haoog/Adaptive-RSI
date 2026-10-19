import json
import shutil
import time
from pathlib import Path

from .process import Sandbox, run_process


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
SCHEMAS = {
    "analysis": object_schema({"findings": {"type": "array", "items": object_schema({
        "title": TEXT, "body": TEXT, "path": TEXT, "quote": TEXT,
        "acceptance": STRINGS, "priority": {"type": "integer"}})}}),
    "planning": object_schema({"decision": {"type": "string", "enum": ["ready", "obsolete", "blocked"]},
                               "reason": TEXT, "scope": STRINGS, "acceptance": STRINGS,
                               "risk": {"type": "string", "enum": ["low", "medium", "high"]}}),
    "implementation": object_schema({"summary": TEXT}),
    "review": object_schema({"decision": {"type": "string", "enum": ["pass", "fail", "insufficient_evidence"]}, "reason": TEXT}),
    "evolution": object_schema({"kind": {"type": "string", "enum": ["strategy", "meta_strategy", "adapter"]},
                                "guidance": TEXT, "triggers": STRINGS, "rationale": TEXT}),
}


def validate_output(value, schema):
    kind = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int}
    if type(value) is not types[kind]:
        raise RuntimeError("Agent returned invalid structured output")
    if "enum" in schema and value not in schema["enum"]:
        raise RuntimeError("Agent returned an unknown decision")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise RuntimeError("Agent output fields do not match the schema")
        for key, item in value.items():
            validate_output(item, schema["properties"][key])
    if kind == "array":
        if len(value) > 100:
            raise RuntimeError("Agent output array is too large")
        for item in value:
            validate_output(item, schema["items"])


class CodexAgent:
    def __init__(self, store):
        self.store = store

    def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("Codex CLI is not installed")
        routes = [config["models"][role]]
        if routes[0].get("fallback"):
            routes.append(routes[0]["fallback"])
        for index, route in enumerate(routes):
            call = self.store.reserve_call(project, run, role, route, config)
            sandbox = Sandbox(self.store.root, config)
            agent_dir = Path(workspace) / ".rsi-agent" / call
            agent_dir.mkdir(parents=True)
            schema_path = agent_dir / "schema.json"
            schema_path.write_text(json.dumps(SCHEMAS[role]))
            output_path = agent_dir / "result.json"
            prompt_path = agent_dir / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            mode = "workspace-write" if role == "implementation" else "read-only"
            argv = [binary, "-a", "never", "exec", "--ignore-user-config", "--ignore-rules",
                    "--ephemeral", "--model", route["model"], "-c", 'model_reasoning_effort="' + route["effort"] + '"',
                    "--sandbox", mode, "-c", "sandbox_workspace_write.network_access=false",
                    "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                    "--json", "--color", "never", "--output-schema", str(schema_path),
                    "--output-last-message", str(output_path), "-C", str(workspace),
                    "Read the task in " + str(prompt_path) + ". Follow its instructions; return the requested JSON."]
            argv = sandbox.worker(argv, workspace, protected, read_only=role != "implementation", output_dir=agent_dir)
            self.store.event("model", role + ": " + route["model"] + " / " + route["effort"], project, run)
            started = time.time()
            try:
                result = run_process(argv, workspace, config["limits"]["run_seconds"], cancel,
                                     config["limits"]["max_output_mb"])
                (agent_dir / "events.jsonl").write_text(result["stdout"], encoding="utf-8")
                tokens = 0
                for line in result["stdout"].splitlines():
                    try:
                        event = json.loads(line)
                        if event.get("type") == "turn.completed":
                            usage = event.get("usage", {})
                            tokens += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                    except (ValueError, TypeError):
                        continue
                self.store.execute("UPDATE calls SET state=?,tokens=?,seconds=? WHERE id=?",
                                   ("failed" if result["exit_code"] or result["error"] else "completed", tokens, time.time() - started, call))
                self.store.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (tokens, run))
                if result["exit_code"] or result["error"]:
                    error = (result["stderr"] or result["error"] or result["stdout"])[-2000:]
                    # Fallback is explicit, recorded, and only used for provider/model errors.
                    unavailable = any(s in error.lower() for s in ["model not found", "not supported", "does not exist", "rate limit", "usage limit"])
                    if unavailable and index + 1 < len(routes):
                        self.store.event("fallback", error, project, run)
                        continue
                    raise RuntimeError("Codex call failed: " + error)
                data = json.loads(output_path.read_text(encoding="utf-8"))
                validate_output(data, SCHEMAS[role])
                return data
            except Exception:
                self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=? AND state='reserved'",
                                   (time.time() - started, call))
                raise
        raise RuntimeError("No model route available")
