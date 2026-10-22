"""OpenCode 无界面执行、供应商导入和 JSONL 结果解析。"""
import json
import os
import platform
import re
import shutil
import threading
import time
import tempfile
from pathlib import Path

from .agent import SCHEMAS, validate_output
from .cli_worker import cli_home, run_cli


AUTH_LOCK = threading.RLock()


def auth_path():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/auth.json"


def sync_oauth(home, model, original):
    source = auth_path()
    updated = home / ".local/share/opencode/auth.json"
    if not source.is_file() or not updated.is_file():
        return []
    prefix = model.split("/", 1)[0]
    current = json.loads(source.read_text())
    refreshed = json.loads(updated.read_text()).get(prefix, {})
    if refreshed.get("type") != "oauth" or original.get(prefix, {}).get("type") != "oauth":
        return []
    if current.get(prefix) == original.get(prefix) and refreshed != original.get(prefix):
        # 仅写回此连接已有的 OAuth 字段，不采纳执行器写入的新服务或配置。
        current[prefix] = {key: refreshed.get(key, value) for key, value in original[prefix].items()}
        with tempfile.NamedTemporaryFile("w", dir=source.parent, prefix=".auth-rsi-", delete=False) as stream:
            temporary = stream.name
            json.dump(current, stream)
        os.replace(temporary, source)
        source.chmod(0o600)
    return [value for value in refreshed.values() if isinstance(value, str) and len(value) > 8]


def jsonc(text):
    # 保留字符串中的 URL、转义和注释符号。
    pattern = r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*[\s\S]*?\*/'
    text = re.sub(pattern, lambda match: match[1] or "", text)
    text = re.sub(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])', lambda match: match[1] or "", text)
    return json.loads(text)


def connection_config():
    directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "opencode"
    result = {}
    for file in [directory / "config.json", directory / "opencode.json", directory / "opencode.jsonc"]:
        if file.is_file():
            config = jsonc(file.read_text())
            result.update(config.get("provider", {}))
    explicit = os.environ.get("OPENCODE_CONFIG")
    for text in ([Path(explicit).read_text()] if explicit else []) + ([os.environ["OPENCODE_CONFIG_CONTENT"]] if os.environ.get("OPENCODE_CONFIG_CONTENT") else []):
        result.update(jsonc(text).get("provider", {}))
    return result


def prepare_home(home, model=None, read_only=True, container=False):
    providers = connection_config()
    prefix = model.split("/", 1)[0] if model else None
    if prefix:
        providers = {key: value for key, value in providers.items() if key == prefix}
    # 只导入连接配置，不继承插件、MCP、Agent 或项目权限。
    providers = {key: {field: value for field, value in provider.items()
                       if field in ["name", "npm", "api", "env", "options", "models", "whitelist", "blacklist"]}
                 for key, provider in providers.items()}
    if container:
        from .cli_worker import container_endpoint
        for provider in providers.values():
            options = provider.get("options", {})
            if isinstance(options.get("baseURL"), str):
                options["baseURL"] = container_endpoint(options["baseURL"])
    permission = {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow"}
    if not read_only:
        permission.update(edit="allow", bash="allow")
    config = {"provider": providers, "permission": permission,
              "agent": {"rsi": {"mode": "primary", "permission": permission}},
              "autoupdate": False, "share": "disabled", "plugin": [], "mcp": {}, "instructions": []}
    directory = home / ".config/opencode"
    directory.mkdir(parents=True)
    target = directory / "opencode.json"
    target.write_text(json.dumps(config))
    target.chmod(0o600)
    source = auth_path()
    secrets = []
    if source.is_file():
        auth = json.loads(source.read_text())
        auth = {key: value for key, value in auth.items()
                if (prefix is None or key == prefix) and value.get("type") in ["api", "oauth"]}
        for value in auth.values():
            secrets += [item for item in value.values() if isinstance(item, str) and len(item) > 8]
        target = home / ".local/share/opencode/auth.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(auth))
        target.chmod(0o600)
    environment = {"OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
                   "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_PURE": "true"}
    # 仅传递所选连接引用的变量；GitHub、控制器和其他服务凭据不会进入工具环境。
    raw = json.dumps(providers)
    names = set(re.findall(r'\{env:([A-Za-z_][A-Za-z0-9_]*)\}', raw))
    for provider in providers.values():
        names.update(provider.get("env", []))
    standard = {"openai": ["OPENAI_API_KEY"], "anthropic": ["ANTHROPIC_API_KEY"],
                "google": ["GOOGLE_GENERATIVE_AI_API_KEY", "GOOGLE_API_KEY"],
                "deepseek": ["DEEPSEEK_API_KEY"], "openrouter": ["OPENROUTER_API_KEY"],
                "opencode": ["OPENCODE_API_KEY"]}
    for key, values in standard.items():
        if prefix is None or prefix == key:
            names.update(values)
    for name in names:
        if name in os.environ:
            environment[name] = os.environ[name]
            secrets.append(os.environ[name])
    for provider in providers.values():
        options = provider.get("options", {})
        key = options.get("apiKey")
        if isinstance(key, str) and not key.startswith("{env:"):
            secrets.append(key)
        headers = options.get("headers", {})
        if isinstance(headers, dict):
            secrets.extend(value for value in headers.values() if isinstance(value, str) and value)
    return environment, secrets


def parse_events(text, strict=True):
    final, usage, seen, errors = [], [], set(), []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part", {})
        if event.get("type") == "error":
            errors.append("OpenCode reported an execution error")
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            final.append(part["text"])
        if event.get("type") == "step_finish" and part.get("id") not in seen:
            if part.get("id"):
                seen.add(part["id"])
            if isinstance(part.get("tokens"), dict):
                usage.append(part["tokens"])
    if errors:
        if strict:
            raise RuntimeError(errors[-1])
        return None, usage
    for text in reversed(final):
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            return json.loads(text), usage
        except ValueError:
            continue
    if strict:
        raise RuntimeError("OpenCode did not return valid structured output")
    return None, usage


def check_provider_errors(text):
    from .native import ProviderUnavailable
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        error = event.get("error", {})
        if not isinstance(error, dict):
            continue
        data = error.get("data", {})
        status = data.get("statusCode") if isinstance(data, dict) else None
        if status in [404, 429, 502, 503, 504]:
            raise ProviderUnavailable("OpenCode model API HTTP " + str(status))
        if status in [401, 403] or error.get("name") == "ProviderAuthError":
            raise RuntimeError("OpenCode model API unauthorized; check the runtime machine's authentication")


class OpenCodeAgent:
    def __init__(self, store):
        self.store = store

    def invoke(self, role, prompt, workspace, project, run, config, route, cancel=None, protected=()):
        binary = shutil.which("opencode") or "opencode"
        call = self.store.reserve_call(project, run, role, route, config)
        started = time.monotonic()
        try:
            with AUTH_LOCK, cli_home(workspace) as home:
                original_auth = json.loads(auth_path().read_text()) if auth_path().is_file() else {}
                environment, secrets = prepare_home(home, route["model"], read_only=role != "implementation",
                                                    container=platform.system() != "Darwin" or config["sandbox"]["backend"] == "docker")
                task = home / "task.txt"
                task.write_text(prompt + "\nReturn ONLY a JSON object matching this schema as your final message:\n" + json.dumps(SCHEMAS[role]))
                argv = [binary, "--pure", "run", "--format", "json", "--agent", "rsi", "--title", "Adaptive RSI", "--model", route["model"]]
                if route["effort"] != "default":
                    argv += ["--variant", route["effort"]]
                argv += ["Read the task in " + str(task) + ". Follow its instructions. Return the requested JSON."]
                self.store.event("model", role + ": OpenCode / " + route["model"] + " / " + route["effort"], project, run)
                try:
                    result = run_cli(argv, workspace, home, config, cancel, protected, role != "implementation", environment, self.store.root)
                finally:
                    secrets += sync_oauth(home, route["model"], original_auth)
                raw = result["stdout"]
                for secret in secrets:
                    if secret:
                        raw = raw.replace(secret, "[REDACTED]")
                directory = Path(workspace) / ".rsi-agent" / call
                directory.mkdir()
                (directory / "events.jsonl").write_text(raw)
                data, steps = parse_events(raw, strict=False)
                from .usage import opencode_usage
                self.store.record_usage(call, run, opencode_usage(steps), time.monotonic() - started)
                check_provider_errors(raw)
                if result["exit_code"] or result["error"]:
                    raise RuntimeError("OpenCode call failed; see the redacted execution record")
                if data is None:
                    raise RuntimeError("OpenCode did not return valid structured output; see the redacted execution record")
                validate_output(data, SCHEMAS[role])
                self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
                return data
        except Exception:
            self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=?", (time.monotonic() - started, call))
            raise
