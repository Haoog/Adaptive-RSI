"""Read model directories without starting inference or storing credentials."""
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .config import validate
from .native import NoRedirect


MAX_BYTES = 4 * 1024 * 1024
MAX_MODELS = 1000
MAX_PAGES = 10
TIMEOUT = 20


class CatalogError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Model catalog: " + code)


def normalize(rows, protocol):
    models = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if protocol == "gemini":
            methods = row.get("supportedGenerationMethods")
            if methods is not None and (not isinstance(methods, list) or "generateContent" not in methods):
                continue
            model = row.get("name", "")
            if isinstance(model, str):
                model = model.removeprefix("models/")
        elif protocol == "ollama":
            model = row.get("model") or row.get("name")
        elif protocol == "codex_login":
            if row.get("hidden"):
                continue
            model = row.get("model") or row.get("id")
        else:
            model = row.get("id")
        if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9._/:-]{1,256}", model):
            continue
        name = row.get("displayName") or row.get("display_name") or model
        if not isinstance(name, str):
            name = model
        name = " ".join(name.split())[:160]
        item = {"id": model, "name": name}
        efforts = row.get("supportedReasoningEfforts")
        if protocol == "codex_login" and isinstance(efforts, list):
            item["efforts"] = sorted({effort.get("reasoningEffort") for effort in efforts if isinstance(effort, dict)
                                      and effort.get("reasoningEffort") in ["low", "medium", "high", "xhigh", "max", "ultra"]})
        kind = row.get("type")
        if kind in ["embedding", "embeddings", "image", "audio", "rerank"]:
            item["kind"] = kind
        models[model] = item
    return sorted(models.values(), key=lambda model: model["id"].casefold())


def get_json(url, headers, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogError("timeout")
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=min(8, remaining)) as response:
            raw = bytearray()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise CatalogError("too_large")
                if time.monotonic() >= deadline:
                    raise CatalogError("timeout")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise CatalogError("invalid_response")
        return data
    except urllib.error.HTTPError as exc:
        # Do not reflect provider bodies, URLs or credentials into browser errors.
        code = {401: "auth", 403: "auth", 404: "unsupported", 405: "unsupported", 429: "rate_limit"}.get(exc.code, "http_error")
        exc.close()
        raise CatalogError(code) from None
    except TimeoutError:
        raise CatalogError("timeout") from None
    except (urllib.error.URLError, OSError):
        raise CatalogError("connection") from None
    except (ValueError, UnicodeError):
        raise CatalogError("invalid_response") from None


def http_models(provider, key_override=None):
    protocol = provider.get("protocol", "codex")
    key_name = provider["env_key"]
    key = key_override if key_override is not None else os.environ.get(key_name, "") if key_name else ""
    if key_name and not key:
        raise CatalogError("missing_key")
    headers = {}
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["x-goog-api-key" if protocol == "gemini" else "Authorization"] = key if protocol == "gemini" else "Bearer " + key
    endpoint = provider["base_url"].rstrip("/") + ("/api/tags" if protocol == "ollama" else "/models")
    cursor, seen, rows = None, set(), []
    deadline = time.monotonic() + TIMEOUT
    for _ in range(MAX_PAGES):
        params = {}
        if protocol == "gemini":
            params["pageSize"] = 100
            if cursor:
                params["pageToken"] = cursor
        elif protocol == "anthropic":
            params["limit"] = 100
            if cursor:
                params["after_id"] = cursor
        elif cursor:
            params["after"] = cursor
        data = get_json(endpoint + ("?" + urllib.parse.urlencode(params) if params else ""), headers, deadline)
        batch = data.get("models" if protocol in ["gemini", "ollama"] else "data")
        if not isinstance(batch, list):
            raise CatalogError("invalid_response")
        rows.extend(batch[:MAX_MODELS])
        cursor = data.get("nextPageToken") if protocol == "gemini" else data.get("last_id") if data.get("has_more") else None
        if data.get("has_more") and not cursor:
            raise CatalogError("invalid_response")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen):
            raise CatalogError("invalid_response")
        if len(rows) >= MAX_MODELS or not cursor:
            return normalize(rows[:MAX_MODELS], protocol), bool(cursor or len(batch) > MAX_MODELS or len(rows) > MAX_MODELS)
        seen.add(cursor)
    return normalize(rows, protocol), True


def codex_models():
    binary = shutil.which("codex")
    if not binary:
        raise CatalogError("codex_missing")
    deadline = time.monotonic() + TIMEOUT
    with tempfile.TemporaryDirectory(prefix="rsi-models-") as directory:
        try:
            process = subprocess.Popen([binary, "app-server", "-c", 'model_provider="openai"'],
                                       cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise CatalogError("codex_missing") from None
        buffer, total = bytearray(), 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def send(message):
                process.stdin.write(json.dumps(message).encode() + b"\n")
                process.stdin.flush()

            def receive(request_id):
                nonlocal buffer, total
                while True:
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise CatalogError("invalid_response")
                        if message.get("id") == request_id:
                            if "error" in message:
                                raise CatalogError("codex_error")
                            result = message.get("result")
                            if not isinstance(result, dict):
                                raise CatalogError("invalid_response")
                            return result
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise CatalogError("timeout")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise CatalogError("codex_error")
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise CatalogError("too_large")
                    buffer.extend(chunk)

            try:
                send({"method": "initialize", "id": 0, "params": {
                    "clientInfo": {"name": "adaptive_rsi", "title": "Adaptive RSI", "version": __version__}}})
                receive(0)
                send({"method": "initialized", "params": {}})
                rows, cursor, seen = [], None, set()
                for request_id in range(1, MAX_PAGES + 1):
                    params = {"limit": 100, "includeHidden": False}
                    if cursor:
                        params["cursor"] = cursor
                    send({"method": "model/list", "id": request_id, "params": params})
                    data = receive(request_id)
                    batch = data.get("data")
                    if not isinstance(batch, list):
                        raise CatalogError("invalid_response")
                    rows.extend(batch[:MAX_MODELS])
                    cursor = data.get("nextCursor")
                    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096 or cursor in seen):
                        raise CatalogError("invalid_response")
                    if len(rows) >= MAX_MODELS or not cursor:
                        return normalize(rows[:MAX_MODELS], "codex_login"), bool(cursor or len(batch) > MAX_MODELS or len(rows) > MAX_MODELS)
                    seen.add(cursor)
                return normalize(rows, "codex_login"), True
            except (OSError, ValueError, UnicodeError):
                raise CatalogError("codex_error") from None
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdin.close()
                process.stdout.close()


def list_models(provider, key=None):
    if isinstance(provider, str):
        if provider == "opencode":
            return opencode_models()
        if provider == "openai":
            models, truncated = codex_models()
            return {"models": models, "truncated": truncated}
        if provider not in ["ollama", "lmstudio"]:
            raise CatalogError("invalid_provider")
        provider = {"id": "local_catalog", "name": provider, "env_key": "", **(
            {"protocol": "ollama", "base_url": "http://127.0.0.1:11434"} if provider == "ollama" else
            {"protocol": "chat_completions", "base_url": "http://127.0.0.1:1234/v1"})}
    try:
        provider = validate({"providers": [provider]})["providers"][0]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CatalogError("invalid_provider") from None
    models, truncated = http_models(provider, key)
    return {"models": models, "truncated": truncated}


def opencode_models():
    from .cli_worker import cli_home, clean_environment, run_cli
    from .config import DEFAULTS
    from .opencode import (OpenCodeCompatibilityError, detect_opencode, prepare_home)
    from .process import run_process
    with tempfile.TemporaryDirectory(prefix="rsi-opencode-models-") as directory:
        with cli_home(directory) as home:
            binary = shutil.which("opencode")
            if binary:
                try:
                    capabilities = detect_opencode(binary)
                except OpenCodeCompatibilityError as exc:
                    raise CatalogError(exc.code) from None
                environment, _ = prepare_home(home, capabilities=capabilities, binary=binary)
                environment.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                                    "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CACHE_HOME": str(home / ".cache"),
                                    "XDG_STATE_HOME": str(home / ".local/state")})
            elif shutil.which("docker"):
                image = DEFAULTS["sandbox"]["worker_image"]
                try:
                    capabilities = detect_opencode(
                        "opencode", argv=["docker", "run", "--rm", image, "opencode", "--version"],
                        env=clean_environment())
                except OpenCodeCompatibilityError as exc:
                    raise CatalogError(exc.code) from None
                environment, _ = prepare_home(home, capabilities=capabilities)
            else:
                raise CatalogError("opencode_missing")
            def execute(argv):
                if binary:
                    return run_process([binary, *argv[1:]], directory, timeout=TIMEOUT,
                                       env={**clean_environment(), **environment})
                return run_cli(argv, directory, home, DEFAULTS, environment=environment,
                               read_only=True, timeout=TIMEOUT)
            if capabilities.modern:
                result = execute(["opencode", "api", "GET", "/api/model", "--standalone"])
                if not result["exit_code"] and not result["error"]:
                    try:
                        models = parse_opencode_v2_models(result["stdout"])
                    except (ValueError, TypeError):
                        models = None
                    if models is not None:
                        return {"models": models[:MAX_MODELS], "truncated": len(models) > MAX_MODELS}
                if _incompatible_output(result):
                    raise CatalogError(capabilities.incompatible_code)
                # v2 的纯 ID 列表是 API 不可用时的保守兜底，不包含 variants。
                result = execute(capabilities.models_argv("opencode"))
            else:
                result = execute(capabilities.models_argv("opencode"))
            if result["exit_code"] or result["error"]:
                if _incompatible_output(result):
                    raise CatalogError(capabilities.incompatible_code)
                raise CatalogError("opencode_error")
            if capabilities.modern:
                models = parse_opencode_v2_ids(result["stdout"])
            else:
                try:
                    models = parse_opencode_v1_models(result["stdout"])
                except ValueError:
                    raise CatalogError("invalid_response") from None
            return {"models": models[:MAX_MODELS], "truncated": len(models) > MAX_MODELS}


def _incompatible_output(result):
    text = (result.get("stderr", "") + "\n" + result.get("stdout", "")).lower()
    return any(marker in text for marker in [
        "unknown option", "unknown flag", "unexpected argument", "invalid option",
        "invalid value for flag", "unrecognized option"])


def parse_opencode_v2_models(text):
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("invalid OpenCode model response")
    models = {}
    for row in data["data"]:
        if not isinstance(row, dict):
            continue
        provider = row.get("providerID")
        model_id = row.get("modelID")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            continue
        full_id = provider + "/" + model_id
        if not re.fullmatch(r"[a-zA-Z0-9._/:-]{1,256}", full_id):
            continue
        capabilities = row.get("capabilities", {})
        if isinstance(capabilities, dict) and capabilities.get("tools") is False:
            continue
        variants = row.get("variants", [])
        efforts = sorted({item.get("id") for item in variants
                          if isinstance(item, dict) and isinstance(item.get("id"), str)
                          and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", item["id"])})
        name = row.get("name") if isinstance(row.get("name"), str) else model_id
        models[full_id] = {"id": full_id, "name": provider + " / " + name[:160], "efforts": efforts}
    return sorted(models.values(), key=lambda model: model["id"].casefold())


def parse_opencode_v2_ids(text):
    models = {}
    for line in text.splitlines():
        model = line.strip()
        if re.fullmatch(r"[a-zA-Z0-9._/:-]+/[a-zA-Z0-9._/:-]+", model):
            models[model] = {"id": model, "name": model, "efforts": []}
    return sorted(models.values(), key=lambda model: model["id"].casefold())


def parse_opencode_v1_models(text):
    models = []
    decoder = json.JSONDecoder()
    while text.strip():
        line, _, text = text.lstrip().partition("\n")
        if not re.fullmatch(r"[a-zA-Z0-9._/:-]+/[a-zA-Z0-9._/:-]+", line):
            continue
        try:
            metadata, end = decoder.raw_decode(text.lstrip())
            text = text.lstrip()[end:]
        except ValueError:
            if models:
                # stdout 截断或版本差异可能让最后一行缺少元数据；跳过坏行而不是丢弃整个目录。
                text = ""
                continue
            raise ValueError("invalid OpenCode metadata") from None
        if not isinstance(metadata, dict) or metadata.get("capabilities", {}).get("toolcall") is False:
            continue
        variants = metadata.get("variants", {})
        efforts = sorted(variants) if isinstance(variants, dict) else []
        provider = metadata.get("providerID", line.split("/")[0])
        name = metadata.get("name", line)
        if not isinstance(provider, str):
            provider = line.split("/")[0]
        if not isinstance(name, str):
            name = line
        models.append({"id": line, "name": provider + " / " + name, "efforts": efforts})
    return models
