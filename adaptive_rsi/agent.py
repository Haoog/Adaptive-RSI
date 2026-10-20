import json
import os
import shutil
import time
from pathlib import Path

from .process import Sandbox, run_process


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
REVIEW_AREAS = ["correctness", "security", "maintainability", "architecture", "tests", "scope"]
VERDICT = {"type": "string", "enum": ["pass", "fail", "insufficient_evidence"]}
SCHEMAS = {
    "probe": object_schema({"summary": TEXT}),
    "analysis": object_schema({"findings": {"type": "array", "items": object_schema({
        "title": TEXT, "body": TEXT, "path": TEXT, "quote": TEXT,
        "acceptance": STRINGS, "priority": {"type": "integer"}})}}),
    "planning": object_schema({"decision": {"type": "string", "enum": ["ready", "obsolete", "blocked", "deferred", "rejected"]},
                               "reason": TEXT, "scope": {"type": "array", "items": {"type": "string", "description": "A literal repository-relative file or directory path only. No descriptions, colons, globs, or annotations."}}, "acceptance": STRINGS,
                               "value": object_schema({"impact": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "benefit": TEXT, "cost": TEXT, "inaction": TEXT, "evidence": STRINGS}),
                               "risk": {"type": "string", "enum": ["low", "medium", "high"]}}),
    "acceptance": object_schema({"files": {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})},
        "checks": {"type": "array", "items": object_schema({"name": TEXT, "argv": STRINGS, "criteria": {"type": "array", "items": {"type": "integer"}}})}}),
    "implementation": object_schema({"summary": TEXT}),
    "review": object_schema({"decision": VERDICT, "reason": TEXT,
        "assessments": {"type": "array", "items": object_schema({
            "area": {"type": "string", "enum": REVIEW_AREAS}, "decision": VERDICT, "reason": TEXT})},
        "findings": {"type": "array", "items": object_schema({
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "area": {"type": "string", "enum": REVIEW_AREAS}, "path": TEXT,
            "line": {"type": "integer"}, "title": TEXT, "reason": TEXT, "suggestion": TEXT})}}),
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


def provider_arguments(route, config, environment=None):
    provider_id = route.get("provider", "openai")
    if provider_id in ["ollama", "lmstudio"]:
        return ["--oss", "--local-provider", provider_id]
    if provider_id == "openai":
        return ["-c", 'model_provider="openai"']
    provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
    if provider is None:
        raise RuntimeError("Unknown model provider: " + provider_id)
    if provider["env_key"] and not (environment if environment is not None else os.environ).get(provider["env_key"]):
        raise RuntimeError("Missing provider environment variable: " + provider["env_key"])
    settings = {"name": provider["name"], "base_url": provider["base_url"],
                "wire_api": "responses", "requires_openai_auth": False}
    if provider["env_key"]:
        settings["env_key"] = provider["env_key"]
    argv = ["-c", "model_provider=" + json.dumps(provider_id)]
    for key, value in settings.items():
        argv += ["-c", "model_providers." + provider_id + "." + key + "=" + json.dumps(value)]
    return argv


class CodexAgent:
    def __init__(self, store):
        self.store = store

    def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        from .provider_health import ProviderHealth, ProviderPaused
        from .native import ProviderUnavailable
        health = ProviderHealth(self.store)
        primary = config["models"][role]
        routes = [primary] + ([primary["fallback"]] if primary.get("fallback") else [])
        for index, route in enumerate(routes):
            provider_id = route.get("provider", "openai")
            try:
                health.require(config, provider_id, project)
                effective = {**config, "models": {**config["models"], role: {**route, "fallback": None}}}
                result = self._invoke(role, prompt, workspace, project, run, effective, cancel, protected)
            except Exception as exc:
                health.record(config, provider_id, project, exc)
                # 仅不可用且未修改工作区的路由允许备用；原生执行器负责拦截半成品。
                codex_unavailable = str(exc).startswith("Codex call failed:") and any(
                    text in str(exc).lower() for text in ["model not found", "not supported", "does not exist", "rate limit", "usage limit"])
                if index + 1 < len(routes) and (isinstance(exc, (ProviderUnavailable, ProviderPaused)) or codex_unavailable):
                    self.store.event("fallback", "Primary provider unavailable; using configured fallback", project, run)
                    continue
                raise
            health.record(config, provider_id, project)
            return result

    def _invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        route = config["models"][role]
        provider = next((p for p in config["providers"] if p["id"] == route.get("provider")), None)
        if provider and provider.get("protocol", "codex") != "codex":
            from .native import NativeAgent
            return NativeAgent(self.store).invoke(role, prompt, workspace, project, run,
                                                 config, route, provider, cancel, protected)
        binary = shutil.which("codex")
        if not binary:
            raise RuntimeError("Codex CLI is not installed for this route")
        billing_config = config
        environment = None
        secret = ""
        if provider and provider.get("credential", "env") in ["keychain", "session"]:
            from .credentials import Credentials
            secret = Credentials(self.store.root).get(provider)
            environment = {**os.environ, "RSI_PROVIDER_API_KEY": secret}
            config = {**config, "providers": [{**p, "env_key": "RSI_PROVIDER_API_KEY"} if p["id"] == provider["id"] else p for p in config["providers"]]}
        provider_args = provider_arguments(route, config, environment)
        call = self.store.reserve_call(project, run, role, route, billing_config)
        started = time.time()
        try:
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
                    "--ephemeral", "--model", route["model"]] + provider_args
            if secret:
                argv += ["-c", 'shell_environment_policy.exclude=["RSI_PROVIDER_API_KEY"]']
            if route["effort"] != "default":
                argv += ["-c", "model_reasoning_effort=" + json.dumps(route["effort"])]
            argv += sandbox.codex_permissions(workspace, protected, read_only=role != "implementation")
            argv += ["-c", "sandbox_workspace_write.network_access=false",
                     "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                     "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                     "--json", "--color", "never", "--output-schema", str(schema_path),
                     "--output-last-message", str(output_path), "-C", str(workspace),
                     "Read the task in " + str(prompt_path) + ". Follow its instructions; return the requested JSON."]
            self.store.event("model", role + ": " + route.get("provider", "openai") + " / " + route["model"] + " / " + route["effort"], project, run)
            result = run_process(argv, workspace, config["limits"]["run_seconds"], cancel,
                                 config["limits"]["max_output_mb"], environment)
            if secret:
                for field in ["stdout", "stderr"]:
                    result[field] = result[field].replace(secret, "[REDACTED]")
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
            self.store.execute("UPDATE calls SET tokens=?,seconds=? WHERE id=?", (tokens, time.time() - started, call))
            self.store.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (tokens, run))
            if result["exit_code"] or result["error"]:
                error = (result["stderr"] or result["error"] or result["stdout"])[-2000:]
                raise RuntimeError("Codex call failed: " + error)
            data = json.loads(output_path.read_text(encoding="utf-8"))
            validate_output(data, SCHEMAS[role])
            self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
            return data
        except Exception:
            self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=? AND state='reserved'",
                               (time.time() - started, call))
            raise
