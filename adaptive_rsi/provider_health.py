"""按项目和连接版本记录供应商故障，避免反复请求故障服务。"""
import time

from .config import fingerprint


def error_code(error):
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    text = str(error).lower()
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


def connection_key(config, provider_id, project):
    provider = next((p for p in config["providers"] if p["id"] == provider_id), {"id": provider_id})
    signature = fingerprint({k: v for k, v in provider.items() if k != "name"})
    return "provider-health:" + str(project or "global") + ":" + provider_id + ":" + signature


class ProviderPaused(RuntimeError):
    code = "provider_paused"


class ProviderHealth:
    def __init__(self, store):
        self.store = store

    def status(self, config, provider, project):
        return self.store.setting(connection_key(config, provider, project), {"failures": 0, "paused": False})

    def require(self, config, provider, project):
        if self.status(config, provider, project)["paused"]:
            raise ProviderPaused("Provider paused after repeated connection failures; resume in Model Center")

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
