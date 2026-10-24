"""OpenCode 无界面执行、供应商导入和 JSONL 结果解析。"""
import json
import os
import platform
import re
import shutil
import threading
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .agent import SCHEMAS, validate_output
from .cli_worker import clean_environment, cli_home, run_cli
from .process import run_process


AUTH_LOCK = threading.RLock()
SUPPORTED_OPENCODE_MAJORS = {1, 2}
MINIMUM_OPENCODE_VERSION = (1, 19, 3)


class OpenCodeCompatibilityError(RuntimeError):
    def __init__(self, code, version=""):
        self.code = code
        self.version = version
        super().__init__("OpenCode compatibility: " + code)


def _version_code(version):
    safe = re.sub(r"[^0-9.]", "", version)[:32]
    return "opencode_incompatible" + ("_" + safe if safe else "")


@dataclass(frozen=True)
class OpenCodeCapabilities:
    version: str
    release: tuple

    @property
    def modern(self):
        return self.release[0] >= 2

    @property
    def supported(self):
        return self.release[0] in SUPPORTED_OPENCODE_MAJORS and self.release >= MINIMUM_OPENCODE_VERSION

    @property
    def http_username(self):
        return "opencode" if self.modern else "adaptive-rsi"

    @property
    def incompatible_code(self):
        return _version_code(self.version)

    def models_argv(self, binary):
        if self.modern:
            return [binary, "models", "--standalone"]
        return [binary, "--pure", "models", "--verbose"]

    def run_argv(self, binary, model, effort):
        if self.modern:
            argv = [binary, "run", "--standalone", "--format", "json", "--agent", "rsi",
                    "--title", "Adaptive RSI", "--model", model + (("#" + effort) if effort != "default" else "")]
            return argv
        argv = [binary, "--pure", "run", "--format", "json", "--agent", "rsi",
                "--title", "Adaptive RSI", "--model", model]
        if effort != "default":
            argv += ["--variant", effort]
        return argv

    def serve_argv(self, binary, hostname, port):
        if self.modern:
            return [binary, "serve", "--hostname", hostname, "--port", str(port)]
        return [binary, "serve", "--pure", "--log-level", "ERROR", "--hostname", hostname, "--port", str(port)]


def parse_opencode_version(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"(?:^|\s)v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?(?:\s|$)", text[:4096])
    if not match:
        return None
    release = tuple(int(value) for value in match.groups())
    return release, ".".join(str(value) for value in release)


def detect_opencode(binary="opencode", argv=None, env=None):
    command = list(argv or [binary, "--version"])
    environment = env or {
        **clean_environment(),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "NO_COLOR": "1",
    }
    try:
        result = run_process(command, timeout=8, env=environment)
    except OSError:
        raise OpenCodeCompatibilityError("opencode_version_unknown") from None
    parsed = parse_opencode_version(result.get("stdout", ""))
    if result.get("exit_code") or result.get("error") or not parsed:
        raise OpenCodeCompatibilityError("opencode_version_unknown")
    release, version = parsed
    capabilities = OpenCodeCapabilities(version=version, release=release)
    if not capabilities.supported:
        raise OpenCodeCompatibilityError(capabilities.incompatible_code, version)
    return capabilities


def _host_auth_environment():
    environment = {
        **clean_environment(),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "NO_COLOR": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_PURE": "true",
    }
    for key in ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]:
        if os.environ.get(key):
            environment[key] = os.environ[key]
    return environment


def _isolated_environment(home):
    return {
        **clean_environment(),
        "HOME": str(home),
        "NO_COLOR": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_PURE": "true",
    }


def _parse_auth_export(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise RuntimeError("OpenCode credential export returned an invalid response") from None
    if not isinstance(data, list):
        raise RuntimeError("OpenCode credential export returned an invalid response")
    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        provider = item.get("integrationID") or item.get("id")
        value = item.get("value")
        if not isinstance(provider, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", provider):
            continue
        if not isinstance(value, dict):
            continue
        result.append({**item, "integrationID": provider})
    return result


def _legacy_auth(rows, provider):
    """Convert one v2 credential export into the v1 isolated auth.json shape."""
    row = next((item for item in rows if item.get("integrationID") == provider), None)
    value = row.get("value") if isinstance(row, dict) else None
    if not isinstance(value, dict):
        return None
    if value.get("type") == "key" and isinstance(value.get("key"), str):
        return {"type": "api", "key": value["key"]}
    if value.get("type") in {"oauth", "api"}:
        return dict(value)
    return None


def _modern_auth(value):
    """Convert a v1 auth.json credential into the v2 export value shape."""
    if not isinstance(value, dict):
        return None
    if value.get("type") == "api" and isinstance(value.get("key"), str):
        return {"type": "key", "key": value["key"]}
    if value.get("type") in {"oauth", "api", "key"}:
        return dict(value)
    return None


def _modern_rows(provider, value, template=None):
    credential = _modern_auth(value)
    if credential is None:
        return []
    row = dict(template) if isinstance(template, dict) else {}
    row.update({
        "id": row.get("id") or provider + "-rsi",
        "integrationID": provider,
        "label": row.get("label") or provider,
        "active": row.get("active", True),
        "value": credential,
    })
    return [row]


def _write_legacy_auth(path, value):
    _private_json(path, value)
    path.chmod(0o600)


def _private_json(path, value):
    """Create credential JSON with mode 0600 before any bytes become visible."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, prefix=".rsi-auth-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def export_auth(binary, capabilities, environment=None, provider=None):
    if not capabilities.modern:
        return []
    command = [binary, "auth", "export", "--standalone"]
    if provider:
        command.append(provider)
    result = run_process(command, timeout=15, env=environment or _host_auth_environment())
    if result.get("exit_code") or result.get("error"):
        raise RuntimeError("OpenCode credential export failed")
    return _parse_auth_export(result.get("stdout", ""))


def import_auth(binary, environment, credentials, home):
    if not credentials:
        return
    target = Path(home) / ".rsi-opencode-auth-import.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _private_json(target, credentials)
        result = run_process([binary, "auth", "import", "--standalone", str(target)], timeout=20, env=environment)
        if result.get("exit_code") or result.get("error"):
            raise RuntimeError("OpenCode credential import failed")
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def auth_providers(binary=None, capabilities=None):
    """Return integration IDs with stored credentials without exposing secret values."""
    binary = binary or shutil.which("opencode")
    if not binary:
        if capabilities and capabilities.modern:
            return sorted({item["integrationID"] for item in load_auth_export()})
        data = json.loads(auth_path().read_text()) if auth_path().is_file() else {}
        return sorted(key for key, value in data.items()
                      if isinstance(value, dict) and value.get("type") in {"oauth", "api"})
    capabilities = capabilities or detect_opencode(binary)
    if not capabilities.modern:
        data = json.loads(auth_path().read_text()) if auth_path().is_file() else {}
        return sorted(key for key, value in data.items()
                      if isinstance(value, dict) and value.get("type") in {"oauth", "api"})
    result = run_process([binary, "auth", "list", "--format", "json", "--standalone"],
                         timeout=15, env=_host_auth_environment())
    if result.get("exit_code") or result.get("error"):
        raise RuntimeError("OpenCode credential listing failed")
    try:
        data = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError):
        raise RuntimeError("OpenCode credential listing returned an invalid response") from None
    if not isinstance(data, list):
        raise RuntimeError("OpenCode credential listing returned an invalid response")
    providers = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        connections = item.get("connections")
        if isinstance(connections, list) and any(isinstance(connection, dict) for connection in connections):
            providers.append(item["id"])
    return sorted(set(providers))


def logout_provider(provider, binary=None, capabilities=None):
    binary = binary or shutil.which("opencode")
    if not binary:
        return remove_auth_export(provider)
    capabilities = capabilities or detect_opencode(binary)
    if not capabilities.modern:
        return False
    result = run_process([binary, "auth", "logout", "--standalone", provider],
                         timeout=20, env=_host_auth_environment())
    if result.get("exit_code") or result.get("error"):
        raise RuntimeError("OpenCode credential logout failed")
    return True


def auth_path():
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/auth.json"


def install_legacy_credential(provider, credentials):
    """Persist one v2 credential in v1 auth.json without replacing other providers."""
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", str(provider)):
        return False
    value = _legacy_auth(credentials, provider)
    if value is None:
        return False
    path = auth_path()
    try:
        current = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current[provider] = value
    _write_legacy_auth(path, current)
    return True


def auth_export_path():
    """Host-side fallback for v2 credentials when no host CLI is installed."""
    return auth_path().with_name("auth-export.json")


def load_auth_export():
    try:
        value = json.loads(auth_export_path().read_text())
    except (FileNotFoundError, OSError, ValueError):
        return []
    return _parse_auth_export(json.dumps(value)) if isinstance(value, list) else []


def merge_auth_export(rows):
    """Persist only exported v2 credentials without logging their values."""
    incoming = [item for item in (rows or [])
                if isinstance(item, dict) and isinstance(item.get("integrationID"), str)]
    if not incoming:
        return
    path = auth_export_path()
    current = {item["integrationID"]: item for item in load_auth_export()}
    current.update({item["integrationID"]: item for item in incoming})
    _private_json(path, list(current.values()))


def remove_auth_export(provider):
    path = auth_export_path()
    current = load_auth_export()
    remaining = [item for item in current if item.get("integrationID") != provider]
    if len(remaining) == len(current):
        return False
    if remaining:
        _private_json(path, remaining)
    else:
        path.unlink(missing_ok=True)
    return True


def sync_oauth(home, model, original, capabilities=None, binary=None):
    if capabilities and capabilities.modern:
        provider = model.split("/", 1)[0]
        updated = export_auth(binary or shutil.which("opencode") or "opencode", capabilities,
                              _isolated_environment(home), provider)
        if not updated:
            return []
        current = export_auth(binary or shutil.which("opencode") or "opencode", capabilities,
                              _host_auth_environment(), provider)
        if current == original and updated != original:
            import_auth(binary or shutil.which("opencode") or "opencode", _host_auth_environment(), updated, home)
        return [value for item in updated for value in item.get("value", {}).values()
                if isinstance(value, str) and len(value) > 8]
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


def sync_legacy_worker_to_modern_host(home, model, original_export, original_legacy,
                                      host_binary, host_capabilities):
    """Write a v1 worker OAuth refresh back through the host v2 credential store."""
    provider = model.split("/", 1)[0]
    source = home / ".local/share/opencode/auth.json"
    if not source.is_file() or not isinstance(original_legacy, dict):
        return []
    try:
        refreshed = json.loads(source.read_text()).get(provider)
    except (OSError, ValueError):
        return []
    if not isinstance(refreshed, dict) or refreshed == original_legacy:
        return []
    if refreshed.get("type") != original_legacy.get("type"):
        return []
    merged = {key: refreshed.get(key, value) for key, value in original_legacy.items()}
    current = export_auth(host_binary, host_capabilities, _host_auth_environment(), provider)
    if current != original_export:
        return []
    template = original_export[0] if original_export else None
    rows = _modern_rows(provider, merged, template)
    if not rows:
        return []
    import_auth(host_binary, _host_auth_environment(), rows, home)
    return [value for value in merged.values() if isinstance(value, str) and len(value) > 8]


def sync_modern_worker_to_legacy_host(rows, model, original_legacy):
    """Write a v2 worker OAuth refresh back into the host v1 auth.json file."""
    provider = model.split("/", 1)[0]
    updated = _legacy_auth(rows, provider)
    if not isinstance(updated, dict) or not isinstance(original_legacy, dict) or updated == original_legacy:
        return []
    if updated.get("type") != original_legacy.get("type"):
        return []
    source = auth_path()
    try:
        current = json.loads(source.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return []
    if not isinstance(current, dict) or current.get(provider) != original_legacy:
        return []
    merged = {key: updated.get(key, value) for key, value in original_legacy.items()}
    current[provider] = merged
    _write_legacy_auth(source, current)
    return [value for value in merged.values() if isinstance(value, str) and len(value) > 8]


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


def prepare_home(home, model=None, read_only=True, container=False, capabilities=None, binary=None,
                 exported_auth=None):
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
    secrets = []
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
    if capabilities and capabilities.modern:
        if binary:
            credentials = exported_auth
            if credentials is None:
                with AUTH_LOCK:
                    credentials = export_auth(binary, capabilities, _host_auth_environment(), prefix)
            if prefix:
                credentials = [item for item in credentials if item.get("integrationID") == prefix]
            import_auth(binary, {**_isolated_environment(home), **environment}, credentials, home)
            for item in credentials:
                secrets += [value for value in item.get("value", {}).values()
                            if isinstance(value, str) and len(value) > 8]
    else:
        if isinstance(exported_auth, dict):
            auth = exported_auth
        else:
            with AUTH_LOCK:
                source = auth_path()
                auth = json.loads(source.read_text()) if source.is_file() else {}
        auth = {key: value for key, value in auth.items()
                if (prefix is None or key == prefix) and isinstance(value, dict)
                and value.get("type") in ["api", "oauth"]}
        if auth:
            for value in auth.values():
                secrets += [item for item in value.values() if isinstance(item, str) and len(item) > 8]
            target = home / ".local/share/opencode/auth.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(auth))
            target.chmod(0o600)
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
            # 语言标签大小写不敏感，避免 ```JSON 围栏导致解析失败。
            text = re.sub(r"^```(?i:json)?\s*|\s*```$", "", text)
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
        host_binary = shutil.which("opencode")
        container = platform.system() != "Darwin" or config["sandbox"]["backend"] == "docker"
        call = self.store.reserve_call(project, run, role, route, config)
        started = time.monotonic()
        try:
            with cli_home(workspace) as home:
                try:
                    host_capabilities = detect_opencode(host_binary) if host_binary else None
                    if container:
                        docker = shutil.which("docker")
                        if not docker:
                            raise RuntimeError("OpenCode Docker worker is required on this execution host")
                        image = config["sandbox"]["worker_image"]
                        inspected = run_process([docker, "image", "inspect", image], timeout=10)
                        if inspected["exit_code"] or inspected["error"]:
                            raise RuntimeError("OpenCode worker image is not cached: " + image)
                        capabilities = detect_opencode(
                            "opencode", argv=[docker, "run", "--rm", image, "opencode", "--version"],
                            env=clean_environment())
                        binary = "opencode"
                    elif host_capabilities:
                        capabilities, binary = host_capabilities, host_binary
                    else:
                        raise RuntimeError("OpenCode CLI is not available on the host or in a Docker worker")
                except OpenCodeCompatibilityError as exc:
                    raise RuntimeError("OpenCode command compatibility check failed: " + exc.code) from None
                provider_prefix = route["model"].split("/", 1)[0]
                host_export, host_legacy, host_original_export = [], {}, []
                # 仅在读取宿主凭据存储时持锁；隔离 home 与模型调用不串行。
                with AUTH_LOCK:
                    if host_capabilities and host_capabilities.modern:
                        host_export = export_auth(host_binary, host_capabilities, _host_auth_environment(),
                                                  provider_prefix)
                    elif host_capabilities:
                        host_legacy = json.loads(auth_path().read_text()) if auth_path().is_file() else {}
                    if capabilities.modern:
                        if host_capabilities and host_capabilities.modern:
                            original_auth = host_export
                        elif host_capabilities:
                            original_auth = _modern_rows(provider_prefix, host_legacy.get(provider_prefix))
                        else:
                            original_auth = [item for item in load_auth_export()
                                             if item.get("integrationID") == provider_prefix]
                    else:
                        if host_capabilities and host_capabilities.modern:
                            host_original_export = host_export
                            legacy = _legacy_auth(host_export, provider_prefix)
                            original_auth = {provider_prefix: legacy} if legacy else {}
                        else:
                            original_auth = host_legacy if host_capabilities else (
                                json.loads(auth_path().read_text()) if auth_path().is_file() else {})
                            original_auth = {key: value for key, value in original_auth.items()
                                             if key == provider_prefix and isinstance(value, dict)}
                environment, secrets = prepare_home(
                    home, route["model"], read_only=role != "implementation",
                    container=container, capabilities=capabilities,
                    binary=host_binary if host_capabilities and host_capabilities.modern else None,
                    exported_auth=original_auth)
                if capabilities.modern and not (host_capabilities and host_capabilities.modern) and original_auth:
                    import_path = home / ".rsi-auth-export.json"
                    import_path.write_text(json.dumps(original_auth))
                    import_path.chmod(0o600)
                    imported = run_cli(["opencode", "auth", "import", "--standalone", str(import_path)],
                                       workspace, home, config, cancel, protected, True,
                                       environment, self.store.root)
                    if imported["exit_code"] or imported["error"]:
                        raise RuntimeError("OpenCode credential import failed; see the redacted execution record")
                    for item in original_auth:
                        secrets += [value for value in item.get("value", {}).values()
                                    if isinstance(value, str) and len(value) > 8]
                task = home / "task.txt"
                task.write_text(prompt + "\nReturn ONLY a JSON object matching this schema as your final message:\n" + json.dumps(SCHEMAS[role]))
                argv = capabilities.run_argv(binary, route["model"], route["effort"])
                argv += ["Read the task in " + str(task) + ". Follow its instructions. Return the requested JSON."]
                self.store.event("model", role + ": OpenCode / " + route["model"] + " / " + route["effort"], project, run)
                try:
                    result = run_cli(argv, workspace, home, config, cancel, protected, role != "implementation", environment, self.store.root)
                finally:
                    if capabilities.modern and host_capabilities and host_capabilities.modern:
                        with AUTH_LOCK:
                            secrets += sync_oauth(home, route["model"], original_auth, capabilities,
                                                  host_binary)
                    elif not capabilities.modern and host_capabilities and host_capabilities.modern:
                        with AUTH_LOCK:
                            secrets += sync_legacy_worker_to_modern_host(
                                home, route["model"], host_original_export,
                                original_auth.get(provider_prefix), host_binary, host_capabilities)
                    elif not capabilities.modern:
                        with AUTH_LOCK:
                            secrets += sync_oauth(home, route["model"], original_auth, capabilities)
                    elif capabilities.modern:
                        try:
                            exported = run_cli(
                                ["opencode", "auth", "export", "--standalone", provider_prefix],
                                workspace, home, config, cancel, protected, True,
                                environment, self.store.root)
                            rows = _parse_auth_export(exported.get("stdout", "")) if not (
                                exported["exit_code"] or exported["error"]) else []
                            if rows:
                                with AUTH_LOCK:
                                    if host_capabilities:
                                        secrets += sync_modern_worker_to_legacy_host(
                                            rows, route["model"], original_auth.get(provider_prefix)
                                            if isinstance(original_auth, dict) else
                                            host_legacy.get(provider_prefix))
                                    else:
                                        merge_auth_export(rows)
                                for item in rows:
                                    secrets += [value for value in item.get("value", {}).values()
                                                if isinstance(value, str) and len(value) > 8]
                        except Exception:
                            # A failed credential refresh must not hide the model call result.
                            pass
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
