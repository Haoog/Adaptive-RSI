"""按项目和连接版本记录供应商故障，避免反复请求故障服务。"""
import json
import time

from .config import fingerprint


PROVIDER_CODES = {"auth", "rate_limit", "timeout", "connection", "model_missing", "provider_paused"}
FAILURE_CODES = {"auth": "credentials", "budget_exhausted": "budget", **{
    code: "provider" for code in PROVIDER_CODES - {"auth"}}}


class ModelFailure(RuntimeError):
    """A failure classified at the model adapter, separate from project tools."""
    def __init__(self, message, code):
        self.code = code
        self.failure_code = FAILURE_CODES.get(code, "execution")
        super().__init__(message)


def error_code(error):
    # Local/explicit nonprovider failures must win over provider-looking output.
    failure = getattr(error, "failure_code", None)
    if failure in ("execution", "environment", "baseline", "budget", "credentials"):
        code = getattr(error, "code", None)
        if failure == "execution" and code in {"tool_format", "process_timeout", "process_exit", "process_output_limit"}:
            return code
        return {"budget": "budget_exhausted", "credentials": "auth"}.get(failure, failure)
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    if failure == "provider":
        return "provider"
    return text_error_code(str(error))


def text_error_code(message):
    """Legacy provider diagnostics only; never pass arbitrary project output here."""
    text = message.lower()
    for words, code in [(["budget exhausted"], "budget_exhausted"),
                        (["http 401", "http 403", "unauthorized"], "auth"),
                        (["http 429", "rate limit", "usage limit"], "rate_limit"),
                        (["timed out", "timeout"], "timeout"),
                        (["connection failed", "http 502", "http 503", "http 504"], "connection"),
                        (["http 404", "model not found", "does not exist"], "model_missing"),
                        (["supported tool", "tool call", "structured output", "valid json", "schema", "finish must"], "tool_format")]:
        if any(word in text for word in words):
            return code
    return "probe_failed"


def http_failure(message, status):
    code = {401: "auth", 403: "auth", 404: "model_missing", 429: "rate_limit",
            502: "connection", 503: "connection", 504: "timeout"}.get(status, "execution")
    return ModelFailure(message, code)


def provider_text_code(message):
    from .recovery import QUOTA_EXHAUSTED
    if any(word in message.lower() for word in QUOTA_EXHAUSTED):
        return "budget_exhausted"
    code = text_error_code(message)
    if code in PROVIDER_CODES:
        return code
    # Compatibility with CLI versions that do not emit HTTP status/error JSON.
    lower = message.lower()
    if "not supported" in lower:
        return "model_missing"
    if "connection" in lower:
        return "connection"
    return "execution"


def cli_failure(backend, result):
    """Translate controller limits and CLI diagnostics, excluding stdout tool/text events."""
    from .process import process_error_kind
    kind = process_error_kind(result)
    message = backend + " call failed: " + str(result.get("error") or result.get("stderr") or
                                               "see the redacted execution record")[-2000:]
    if kind in ("timeout", "output_limit"):
        return ModelFailure(message, "timeout" if kind == "timeout" else "execution")
    # A structured CLI failure is authoritative even when stderr mentions other failures.
    errors = []
    for line in (result.get("stdout") or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
            continue
        error = event.get("error", event.get("message"))
        if isinstance(error, str):
            errors.append(ModelFailure(message, provider_text_code(error)))
        elif isinstance(error, dict):
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            status = data.get("statusCode", error.get("statusCode"))
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
            if status is not None:
                errors.append(http_failure(message, status))
            elif error.get("name") == "ProviderAuthError":
                errors.append(ModelFailure(message, "auth"))
            else:
                detail = data.get("message") or error.get("message") or ""
                errors.append(ModelFailure(message, provider_text_code(str(detail))))
    if errors:
        return errors[-1]
    if result.get("exit_code") or result.get("error"):
        # Older CLIs report their own failures only on stderr. stdout can contain
        # arbitrary command output and is deliberately excluded from this fallback.
        return ModelFailure(message, provider_text_code(result.get("stderr") or ""))
    return None


def connection_key(config, provider_id, project):
    provider = next((p for p in config["providers"] if p["id"] == provider_id), {"id": provider_id})
    signature = fingerprint({k: v for k, v in provider.items() if k != "name"})
    return "provider-health:" + str(project or "global") + ":" + provider_id + ":" + signature


class ProviderPaused(RuntimeError):
    code = "provider_paused"
    failure_code = "provider"


class ProviderHealth:
    def __init__(self, store):
        self.store = store

    def status(self, config, provider, project):
        return self.store.setting(connection_key(config, provider, project), {"failures": 0, "paused": False})

    def require(self, config, provider, project):
        if self.status(config, provider, project)["paused"]:
            error = ProviderPaused("Provider paused after repeated connection failures; resume in Model Center")
            error.provider_id = provider
            raise error

    def record(self, config, provider, project, error=None):
        code = error_code(error) if error else None
        if error and code not in ["auth", "rate_limit", "timeout", "connection"]:
            return
        key = connection_key(config, provider, project)
        # 多项目执行线程的计数更新必须在同一事务内完成。
        import json
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            old = json.loads(row[0]) if row else {}
            failures = old.get("failures", 0) + 1 if error else 0
            value = {"failures": failures, "paused": old.get("paused", False) or failures >= 3,
                     "code": code, "at": time.time()}
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def reset(self, config, provider, project):
        self.store.put_setting(connection_key(config, provider, project), {"failures": 0, "paused": False})
