"""Stable failure codes and stage artifacts for safe retries."""
import json
import time

from .config import fingerprint
from .store import encode


class BudgetExhausted(RuntimeError):
    pass


def failure(exc):
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, BudgetExhausted) or "budget" in lower:
        code, action = "budget", "wait_or_adjust_budget"
    elif any(s in lower for s in ["credential", "api key", "environment variable", "unauthorized", "401"]):
        code, action = "credentials", "check_model_settings"
    elif any(s in lower for s in ["rate limit", "429", "503", "timeout", "timed out", "provider", "connection"]):
        code, action = "provider", "retry_later"
    elif any(s in lower for s in ["dependency", "seed", "sandbox", "executable", "not installed"]):
        code, action = "environment", "check_environment"
    elif any(s in lower for s in ["head changed", "baseline", "stale"]):
        code, action = "baseline", "retry_new_baseline"
    else:
        code, action = "execution", "inspect_details"
    return {"code": code, "action": action, "detail": text}


def save(runtime, run, stage, data, signature):
    runtime.store.execute("INSERT OR REPLACE INTO stages(run,stage,signature,data,created) VALUES (?,?,?,?,?)",
                          (run["id"], stage, signature, encode(data), time.time()))


def cached(runtime, run, stage, signature):
    row = runtime.store.one("SELECT s.data,s.run FROM stages s JOIN runs r ON s.run=r.id "
                            "WHERE r.task=? AND s.stage=? AND s.signature=? AND r.id<>? "
                            "ORDER BY s.created DESC LIMIT 1", (run["task"], stage, signature, run["id"]))
    if row:
        runtime.store.event("stage_reused", "Reused verified stage inputs", run["project"], run["id"],
                            {"stage": stage, "from_run": row["run"]})
        return json.loads(row["data"])
    return None


def signature(run, task, base, strategies):
    from .build import BUILD_ID
    return fingerprint({"build": BUILD_ID, "config": json.loads(run["config"]), "base": base, "strategies": strategies,
                        "task": {k: task[k] for k in ["title", "body", "source", "evidence", "acceptance"]}})
