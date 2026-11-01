"""Stable failure codes and stage artifacts for safe retries."""
import json
import re
import time

from .config import fingerprint
from .store import encode


class BudgetExhausted(RuntimeError):
    failure_code = "budget"
    code = "budget_exhausted"


# 额度类故障只匹配异常类型或明确的配额/额度耗尽措辞；"上下文/输出超限"
# （无论旧消息里的 "output budget" 还是新消息里的 "output limit"）属于确定性
# 执行错误，不得归入 budget，否则会在无额度的 waiting_budget 状态下无限重试。
QUOTA_EXHAUSTED = ("budget exhausted", "budget insufficient", "insufficient budget",
                   "insufficient quota", "quota exceeded", "exceeded your current quota",
                   "credit balance", "resource_exhausted")


# 结构化故障码对应的处理动作；raise 侧已知故障成因时优先携带，不再依赖消息文本猜测。
FAILURE_ACTIONS = {"provider": "retry_later", "budget": "wait_or_adjust_budget",
                   "credentials": "check_model_settings", "environment": "check_environment",
                   "baseline": "retry_new_baseline", "execution": "inspect_details"}


def failure(exc):
    text = str(exc)
    lower = text.lower()
    structured = getattr(exc, "failure_code", None)
    if isinstance(structured, str) and structured in FAILURE_ACTIONS:
        code, action = structured, FAILURE_ACTIONS[structured]
    elif isinstance(exc, BudgetExhausted) or any(s in lower for s in QUOTA_EXHAUSTED):
        code, action = "budget", "wait_or_adjust_budget"
    elif any(s in lower for s in ["credential", "api key", "environment variable", "unauthorized", "401"]):
        code, action = "credentials", "check_model_settings"
    elif "opencode_incompatible" in lower or "opencode command compatibility" in lower:
        code, action = "environment", "check_environment"
    elif any(s in lower for s in ["rate limit", "429", "503", "timeout", "timed out", "provider", "connection"]):
        code, action = "provider", "retry_later"
    elif any(s in lower for s in ["dependency", "seed", "sandbox", "executable", "not installed"]):
        code, action = "environment", "check_environment"
    elif any(s in lower for s in ["head changed", "baseline", "stale"]):
        code, action = "baseline", "retry_new_baseline"
    else:
        code, action = "execution", "inspect_details"
    result = {"code": code, "action": action, "detail": text}
    request_id = getattr(exc, "provider_request_id", None)
    if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        result["provider_request_id"] = request_id
    stop = getattr(exc, "stop_reason", None)
    if isinstance(stop, str) and stop in {"output_limit", "safety", "refusal", "paused", "stop_sequence", "text_only", "unknown"}:
        result["stop_reason"] = stop
        result["action"] = {"output_limit": "adjust_output_limit", "safety": "review_provider_policy",
                            "refusal": "review_provider_policy", "paused": "inspect_details",
                            "stop_sequence": "check_model_settings", "text_only": "check_tool_support",
                            "unknown": "inspect_details"}[stop]
    return result


def save(runtime, run, stage, data, signature):
    runtime.store.execute("INSERT OR REPLACE INTO stages(run,stage,signature,data,created) VALUES (?,?,?,?,?)",
                          (run["id"], stage, signature, encode(data), time.time()))


def cached(runtime, run, stage, signature, accept=None, normalize=None):
    row = runtime.store.one("SELECT s.data,s.run FROM stages s JOIN runs r ON s.run=r.id "
                            "WHERE r.task=? AND s.stage=? AND s.signature=? AND r.id<>? "
                            "ORDER BY s.created DESC LIMIT 1", (run["task"], stage, signature, run["id"]))
    if row:
        data = json.loads(row["data"])
        if normalize is not None:
            try:
                data = normalize(data)
            except (RuntimeError, KeyError, TypeError, ValueError, AttributeError) as exc:
                # Only this stored record is unusable; the caller redoes this stage
                # while every other reused stage and the recovery itself continue.
                runtime.store.event("stage_invalidated", "Cached stage result is unusable; verification will run again",
                                    run["project"], run["id"],
                                    {"stage": stage, "reason": "reused_record_invalid", "from_run": row["run"],
                                     "error": str(exc)[:200]})
                return None
        if accept is not None and not accept(data):
            runtime.store.event("stage_invalidated", "Cached stage result requires rework", run["project"], run["id"],
                                {"stage": stage, "reason": "result_requires_rework", "from_run": row["run"]})
            return None
        runtime.store.event("stage_reused", "Reused verified stage inputs", run["project"], run["id"],
                            {"stage": stage, "from_run": row["run"]})
        return data
    prior = runtime.store.one("SELECT s.run FROM stages s JOIN runs r ON r.id=s.run WHERE r.task=? AND s.stage=? AND r.id<>? ORDER BY s.created DESC LIMIT 1", (run["task"], stage, run["id"]))
    if prior:
        runtime.store.event("stage_invalidated", "Stage inputs changed; verification will run again", run["project"], run["id"],
                            {"stage": stage, "reason": "input_signature_changed", "from_run": prior["run"]})
    return None


def enforce_cached_review(review, config):
    """Rebuild one stored review record through the full review gate.

    Stored records are enforce_review output, so they also carry derived
    fields (reported_decision, blocking_threshold) that validate_output
    rejects; keep the schema fields, then let enforce_review re-derive the
    decision from findings and assessments. A stale record cannot reuse a
    weaker verdict, and a record that cannot be trusted raises exactly as
    a fresh output would; cached() turns that into per-stage isolation.
    """
    from .agent import SCHEMAS
    from .review import enforce_review
    fields = SCHEMAS["review"]["properties"]
    return enforce_review({key: value for key, value in review.items() if key in fields}, config)


def signature(run, task, base, strategies, configuration=None):
    from .build import BUILD_ID
    return fingerprint({"build": BUILD_ID,
                        "config": json.loads(run["config"]) if configuration is None else configuration,
                        "base": base, "strategies": strategies,
                        "task": {k: task[k] for k in ["title", "body", "source", "evidence", "acceptance"]}})
