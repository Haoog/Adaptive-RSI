"""按项目和连接版本记录供应商故障，避免反复请求故障服务。"""
import json
import sqlite3
import threading
import time
import uuid

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


def connection_key(config, provider_id, project, model=None, root=None):
    provider = next((p for p in config["providers"] if p["id"] == provider_id), {"id": provider_id})
    fields = {k: v for k, v in provider.items() if k != "name"}
    if root is not None:
        from .credentials import Credentials
        generation = Credentials(root).epoch(provider)
        if generation:
            fields["credential_generation"] = generation
    signature = fingerprint(fields)
    key = "provider-health:" + str(project or "global") + ":" + provider_id + ":" + signature
    return key + ":" + fingerprint({"model": model}) if model else key


class ProviderPaused(RuntimeError):
    code = "provider_paused"
    failure_code = "provider"


class ProviderHealth:
    def __init__(self, store):
        self.store = store
        self._attempts = threading.local()

    def key(self, config, provider, project, model=None):
        return connection_key(config, provider, project, model, self.store.root)

    def account_key(self, config, provider):
        return "provider-account-health:" + self.key(config, provider, "shared").split(":", 2)[-1]

    def status(self, config, provider, project, model=None):
        key = self.key(config, provider, project)
        value = self.store.setting(key, {"failures": 0, "paused": False})
        shared = self.store.setting(self.account_key(config, provider), {})
        if shared.get("retry_at", 0) > value.get("retry_at", 0):
            value = {**value, "retry_at": shared["retry_at"], "cooldown_scope": "account"}
        return value

    def diagnostics(self, config, provider, project, model=None):
        state = self.status(config, provider, project, model)
        now = time.time()
        return {**{key: value for key, value in state.items() if key != "half_open_owner"},
                "admission": self.admission(config, provider, project, model),
                "cooldown_seconds": max(0, state.get("retry_at", 0) - now),
                "recovery_in_flight": state.get("half_open_until", 0) > now}

    @staticmethod
    def _half_open_ready(config, state, now):
        minutes = config.get("recovery", {}).get("provider_retry_minutes", 0)
        return bool(
            state.get("paused")
            and minutes
            and state.get("code") in ("rate_limit", "timeout", "connection")
            and now >= state.get("at", now) + minutes * 60
            and now >= state.get("half_open_until", 0)
            and now >= state.get("retry_at", 0)
        )

    def admission(self, config, provider, project, model=None):
        """Report readiness without consuming the single half-open attempt."""
        now = time.time()
        state = self.status(config, provider, project, model)
        half_open = self._half_open_ready(config, state, now)
        return {"ready": (not state.get("paused") or half_open) and state.get("retry_at", 0) <= now,
                "half_open": half_open}

    def require(self, config, provider, project, model=None):
        key = self.key(config, provider, project)
        now = time.time()
        attempt = {"id": uuid.uuid4().hex, "started": now, "key": key,
                   "account_key": "provider-account-health:" + provider + ":" + key.rsplit(":", 1)[-1],
                   "detail_key": key + ":" + fingerprint({"model": model}) if model else None}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            state = json.loads(row[0]) if row else {"paused": False}
            shared = db.execute("SELECT value FROM settings WHERE key=?", (attempt["account_key"],)).fetchone()
            if shared:
                retry_at = json.loads(shared[0]).get("retry_at", 0)
                if retry_at > state.get("retry_at", 0):
                    state["retry_at"] = retry_at
            if state.get("retry_at", 0) > now:
                error = ModelFailure("Provider Retry-After cooldown is pending", "rate_limit")
                error.retry_after = state["retry_at"] - now
                error.provider_id = provider
                error.model = model
                error.local_admission = True
                raise error
            attempt["generation"] = state.get("generation", 0)
            if self._half_open_ready(config, state, now):
                # Lease the recovered connection for the current bounded role, not
                # the entire command budget, so later stages can continue.
                state["half_open_until"] = now + config["limits"]["command_seconds"] + 30
                state["half_open_owner"] = attempt["id"]
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(state), key))
            elif state.get("paused"):
                attempt = None
        if attempt is None:
            error = ProviderPaused("Provider paused after repeated connection failures; resume in Model Center")
            error.provider_id = provider
            error.model = model
            error.local_admission = True
            raise error
        values = getattr(self._attempts, "values", {})
        values[(project, provider, model)] = attempt
        self._attempts.values = values
        return attempt

    def release(self, attempt):
        """Release only this attempt's recovery lease, including cancellation/tool failures."""
        if not attempt or not attempt.get("key"):
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?", (attempt["key"],)).fetchone()
            value = json.loads(row[0]) if row else {}
            if value.get("half_open_owner") == attempt["id"]:
                value.pop("half_open_owner", None)
                value.pop("half_open_until", None)
                db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(value), attempt["key"]))

    def record(self, config, provider, project, error=None, model=None, attempt=None):
        values = getattr(self._attempts, "values", {})
        identity = (project, provider, model)
        if attempt is None:
            attempt = values.pop(identity, None)
        elif values.get(identity, {}).get("id") == attempt.get("id"):
            values.pop(identity, None)
        key = attempt.get("key") if attempt else None
        key = key or self.key(config, provider, project)
        code = error_code(error) if error else None
        try:
            if getattr(error, "local_admission", False):
                return
            if error and code not in ["auth", "rate_limit", "timeout", "connection"]:
                return
            # 多项目执行线程的计数更新必须在同一事务内完成。
            import json
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                old = json.loads(row[0]) if row else {}
                if not error:
                    # A late success from an older attempt cannot clear a newer failure.
                    if (attempt is not None and (attempt.get("generation") != old.get("generation", 0) or
                            old.get("half_open_owner") not in (None, attempt["id"]))) or (
                            attempt is None and (old.get("paused") or old.get("retry_at", 0) > time.time())):
                        value = None
                    else:
                        value = {"failures": 0, "paused": False, "code": None, "at": time.time(),
                                 "generation": old.get("generation", 0)}
                else:
                    failures = old.get("failures", 0) + 1
                    value = {"failures": failures,
                             "paused": old.get("paused", False) or failures >= 3,
                             "code": code, "at": time.time(), "generation": old.get("generation", 0) + 1}
                if value is not None and error and getattr(error, "retry_after", 0):
                    value["retry_at"] = time.time() + error.retry_after
                if value is not None:
                    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
                    if code == "rate_limit" and value.get("retry_at"):
                        shared_key = attempt.get("account_key") if attempt else self.account_key(config, provider)
                        prior = db.execute("SELECT value FROM settings WHERE key=?", (shared_key,)).fetchone()
                        retry_at = max(value["retry_at"], json.loads(prior[0]).get("retry_at", 0) if prior else 0)
                        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                   (shared_key, json.dumps({"retry_at": retry_at, "at": time.time()})))
            if model and value is not None:
                # 明细写入只是诊断视图：准入状态已在上面的事务里提交，这里失败既不能
                # 丢掉已完成的模型结果，也不能改变调用前的暂停/冷却判定。
                try:
                    detail_key = (attempt.get("detail_key") if attempt else None) or key + ":" + fingerprint({"model": model})
                    detail = self.store.setting(detail_key, {})
                    detail.update(value)
                    detail["model"] = model
                    self.store.put_setting(detail_key, detail)
                except (sqlite3.Error, OSError) as detail_error:
                    import logging
                    logging.getLogger(__name__).warning(
                        "Provider health diagnostic detail write failed (%s); admission state is unchanged",
                        type(detail_error).__name__)

        finally:
            try:
                self.release(attempt)
            except Exception as cleanup_error:
                import logging
                logging.getLogger(__name__).warning("Provider lease cleanup failed (%s)", type(cleanup_error).__name__)

    def reset(self, config, provider, project, model=None):
        self.store.put_setting(self.key(config, provider, project), {"failures": 0, "paused": False})
        self.store.execute("DELETE FROM settings WHERE key=?", (self.account_key(config, provider),))
