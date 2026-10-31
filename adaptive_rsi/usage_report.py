"""按真实调用记录归集任务用量，失败重试和未知用量均保留。"""
import datetime
import functools
import math
import statistics
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store


DAILY_MAX_DAYS = 93
RUN_LIMIT = 50
STAGE_LIMIT = 100
RETRY_GROUP_LIMIT = 5000
REFERENCE_GROUPS = 20
REFERENCE_SAMPLES = 200
REFERENCE_MIN_SAMPLES = 8
REFERENCE_SOURCE = "completed_calls_same_project_kind_role_model_before_window"

_COUNTS = "(c.input_tokens+c.output_tokens+c.cache_read_tokens+c.cache_write_tokens)"
_KNOWN = "coalesce(c.usage_known,0)"
_COMPONENT = ("(" + _KNOWN + "=1 AND " + _COUNTS + "=c.tokens AND "
              "(c.input_price IS NOT NULL OR c.output_price IS NOT NULL OR "
              "c.cache_read_price IS NOT NULL OR c.cache_write_price IS NOT NULL) AND "
              "(c.input_tokens=0 OR c.input_price IS NOT NULL) AND "
              "(c.output_tokens=0 OR c.output_price IS NOT NULL) AND "
              "(c.cache_read_tokens=0 OR c.cache_read_price IS NOT NULL) AND "
              "(c.cache_write_tokens=0 OR c.cache_write_price IS NOT NULL))")
_UNIT = "(" + _KNOWN + "=1 AND c.unit_price IS NOT NULL AND c.unit_price>=0)"
_PRICED = "(" + _COMPONENT + " OR " + _UNIT + ")"
_COST = ("CASE WHEN " + _COMPONENT + " THEN (c.input_tokens*coalesce(c.input_price,0)"
         "+c.output_tokens*coalesce(c.output_price,0)"
         "+c.cache_read_tokens*coalesce(c.cache_read_price,0)"
         "+c.cache_write_tokens*coalesce(c.cache_write_price,0))/1000000.0 "
         "WHEN " + _UNIT + " THEN c.tokens*c.unit_price/1000000.0 END")
TOTALS = ("count(*) AS calls,coalesce(sum(c.tokens),0) AS tokens,\n"
          "coalesce(sum(c.input_tokens),0) AS input_tokens,coalesce(sum(c.output_tokens),0) AS output_tokens,\n"
          "coalesce(sum(c.cache_read_tokens),0) AS cache_read_tokens,\n"
          "coalesce(sum(c.cache_write_tokens),0) AS cache_write_tokens,\n"
          "coalesce(sum(c.seconds),0) AS model_seconds,\n"
          "coalesce(sum(coalesce(c.usage_known,0)=0),0) AS unknown_calls,\n"
          "coalesce(sum(c.state='reserved'),0) AS pending_calls,\n"
          "coalesce(sum(c.state='failed'),0) AS failed_calls,\n"
          "sum(" + _COST + ") AS estimated_usd,\n"
          "coalesce(sum(" + _PRICED + "),0) AS priced_calls,\n"
          "coalesce(sum(NOT " + _PRICED + "),0) AS unpriced_calls,\n"
          "coalesce(sum(CASE WHEN " + _COMPONENT + " THEN 1 ELSE 0 END),0) AS component_priced_calls,\n"
          "coalesce(sum(CASE WHEN " + _UNIT + " AND NOT " + _COMPONENT + " THEN 1 ELSE 0 END),0) AS unit_price_priced_calls,\n"
          "coalesce(sum(CASE WHEN NOT " + _PRICED + " AND " + _KNOWN + "=1 THEN 1 ELSE 0 END),0) AS known_unpriced_calls,\n"
          "coalesce(sum(c.cache_read_tokens>0),0) AS reused_calls,\n"
          "CASE WHEN coalesce(sum(c.input_tokens),0)+coalesce(sum(c.cache_read_tokens),0)>0 THEN "
          "1.0*coalesce(sum(c.cache_read_tokens),0)/"
          "(coalesce(sum(c.input_tokens),0)+coalesce(sum(c.cache_read_tokens),0)) END AS reuse_ratio")
JOIN = " FROM calls c LEFT JOIN runs r ON r.id=c.run LEFT JOIN tasks t ON t.id=r.task "


def _bucket():
    return {"calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "model_seconds": 0,
            "unknown_calls": 0, "failed_calls": 0, "estimated_usd": None,
            "priced_calls": 0, "unpriced_calls": 0, "reused_calls": 0}


def _interval(values):
    """Return deterministic per-call quartiles; small samples are not an interval."""
    if len(values) < REFERENCE_MIN_SAMPLES:
        return None
    q25, median, q75 = statistics.quantiles(sorted(values), n=4, method="inclusive")
    return {"p25": q25, "median": median, "p75": q75, "samples": len(values)}


def one_snapshot(*, store_arg=0):
    """Serve every read in a report from the same SQLite snapshot.

    Summary, trends, groups and call details are derived from one transaction so a
    concurrent write can never make the response contradict its own totals. Nested
    snapshots reuse the outer transaction.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            store = kwargs.get("store") if store_arg == "store" else args[store_arg]
            if not isinstance(store, Store):
                raise TypeError("one_snapshot requires the decorated function to take a Store")
            with store.snapshot():
                return function(*args, **kwargs)
        return wrapped
    return decorate


@one_snapshot()
def report(store, params):
    def value(key, default=None):
        return params.get(key, [default])[0]

    now = time.time()
    try:
        start, end = float(value("start", now - 7 * 86400)), float(value("end", now))
    except (ValueError, TypeError):
        raise ValueError("Invalid usage time range") from None
    if not all(math.isfinite(x) for x in [start, end]) or start < 0 or end <= start or end > 4102444800:
        raise ValueError("Invalid usage time range")
    timezone = value("timezone", "UTC")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid usage timezone") from None
    try:
        limit, offset = int(value("limit", 25)), int(value("offset", 0))
    except (ValueError, TypeError):
        raise ValueError("Invalid usage pagination") from None
    if not 1 <= limit <= 100 or not 0 <= offset <= 2**63 - 1:
        raise ValueError("Invalid usage pagination")
    project, task = value("project"), value("task")
    where, args = ["c.created>=?", "c.created<?"], [start, end]
    if project:
        if not store.one("SELECT id FROM projects WHERE id=?", (project,)):
            raise ValueError("Project not found")
        where.append("c.project=?")
        args.append(project)
    if task:
        where.append("t.id=?")
        args.append(task)
    query = value("q", "").strip()[:200]
    if query:
        where.append("instr(lower(coalesce(t.title,'') || ' ' || c.model || ' ' || c.provider),lower(?))>0")
        args.append(query)
    scope = " WHERE " + " AND ".join(where)
    background = "(t.id IS NULL OR substr(t.source,1,7)='system_')"
    summary = store.one("SELECT " + TOTALS + ",count(DISTINCT t.id) AS tasks" + JOIN + scope, args)
    summary["reused"] = summary["reused_calls"] > 0
    buckets = {}
    rows = store.stream("SELECT c.created,c.tokens,c.input_tokens,c.output_tokens,"
                        "c.cache_read_tokens,c.cache_write_tokens,c.usage_known,c.state,"
                        "c.seconds," + _COST + " AS cost_usd," + _PRICED + " AS priced" +
                        JOIN + scope, args)
    try:
        for row in rows:
            local = datetime.datetime.fromtimestamp(row["created"], zone).date()
            bucket = buckets.setdefault(local, _bucket())
            bucket["calls"] += 1
            bucket["tokens"] += row["tokens"] or 0
            bucket["input_tokens"] += row["input_tokens"] or 0
            bucket["output_tokens"] += row["output_tokens"] or 0
            bucket["cache_read_tokens"] += row["cache_read_tokens"] or 0
            bucket["cache_write_tokens"] += row["cache_write_tokens"] or 0
            bucket["model_seconds"] += row["seconds"] or 0
            bucket["unknown_calls"] += int(not row["usage_known"])
            bucket["failed_calls"] += int(row["state"] == "failed")
            bucket["reused_calls"] += int((row["cache_read_tokens"] or 0) > 0)
            if row["priced"]:
                bucket["priced_calls"] += 1
                bucket["estimated_usd"] = (bucket["estimated_usd"] or 0) + (row["cost_usd"] or 0)
            else:
                bucket["unpriced_calls"] += 1
    finally:
        # 显式关闭生成器，连接立即归还连接池而不是等 GC。
        rows.close()
    daily = []
    if buckets:
        first = datetime.datetime.fromtimestamp(start, zone).date() if start else min(buckets)
        last = datetime.datetime.fromtimestamp(end - 0.001, zone).date()
        if (last - first).days <= DAILY_MAX_DAYS:
            day = first
            while day <= last:
                buckets.setdefault(day, _bucket())
                day += datetime.timedelta(days=1)
        else:
            monthly = {}
            for day, values in buckets.items():
                month = day.replace(day=1)
                target = monthly.setdefault(month, _bucket())
                for key, amount in values.items():
                    if amount is not None:
                        target[key] = (target[key] or 0) + amount
            day = first.replace(day=1)
            while day <= last:
                monthly.setdefault(day, _bucket())
                day = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            buckets = monthly
    for day, bucket in sorted(buckets.items()):
        midnight = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
        denominator = bucket["input_tokens"] + bucket["cache_read_tokens"]
        daily.append({"day": midnight.timestamp(), "date": day.isoformat(), **bucket,
                      "reused": bucket["reused_calls"] > 0,
                      "reuse_ratio": bucket["cache_read_tokens"] / denominator if denominator else None})
    retry_rows = store.query("SELECT c.run AS run,r.task AS task,c.project,t.source AS kind,c.role,"
                             "c.provider,c.model,c.effort,count(*) AS calls" + JOIN + scope +
                             " AND c.run IS NOT NULL"
                             " GROUP BY c.run,c.role,c.provider,c.model,c.effort,c.project,t.source"
                             " HAVING count(*)>1 ORDER BY c.run,c.role LIMIT ?",
                             [*args, RETRY_GROUP_LIMIT + 1])
    retries_truncated = len(retry_rows) > RETRY_GROUP_LIMIT
    run_retries, task_retries, role_retries, stage_retries = {}, {}, {}, {}
    for row in retry_rows[:RETRY_GROUP_LIMIT]:
        extra = row["calls"] - 1
        run_retries[row["run"]] = run_retries.get(row["run"], 0) + extra
        if row["task"]:
            task_retries[row["task"]] = task_retries.get(row["task"], 0) + extra
        role_key = (row["role"], row["provider"], row["model"], row["effort"])
        role_retries[role_key] = role_retries.get(role_key, 0) + extra
        stage_key = (row["project"], row["kind"], row["role"], row["provider"], row["model"], row["effort"])
        stage_retries[stage_key] = stage_retries.get(stage_key, 0) + extra
    roles = store.query("SELECT c.role,c.provider,c.model,c.effort," + TOTALS + JOIN + scope +
                        " GROUP BY c.role,c.provider,c.model,c.effort ORDER BY tokens DESC", args)
    for row in roles:
        row["retry_calls"] = role_retries.get((row["role"], row["provider"], row["model"], row["effort"]), 0)
        row["reused"] = row["reused_calls"] > 0
    group = "coalesce(t.id,'background:' || coalesce(c.project,'global'))"
    count = store.one("SELECT count(*) AS n FROM (SELECT " + group + JOIN + scope + " GROUP BY 1)", args)["n"]
    sort = {"tokens": "tokens DESC", "recent": "last_call DESC", "duration": "model_seconds DESC",
            "cost": "estimated_usd DESC"}.get(value("sort", "recent"))
    if sort is None:
        raise ValueError("Invalid usage sort")
    tasks = store.query("SELECT " + group + " AS id,t.id AS task,t.title,t.source,t.source AS kind,t.state,"
                        "c.project," + background + " AS background,count(DISTINCT r.id) AS attempts,"
                        "min(c.created) AS first_call,max(c.created) AS last_call,"
                        "group_concat(DISTINCT c.provider || '/' || c.model) AS models," + TOTALS + JOIN + scope +
                        " GROUP BY 1 ORDER BY " + sort + ",id LIMIT ? OFFSET ?", [*args, limit, offset])
    for row in tasks:
        row["retry_calls"] = task_retries.get(row["task"], 0)
        row["reused"] = row["reused_calls"] > 0
    runs = store.query("SELECT r.id,r.task,r.project,t.title,t.source AS kind,r.state,r.started,r.ended,"
                       "max(c.created) AS last_call,"
                       "(SELECT count(*) FROM events e WHERE e.run=r.id AND e.kind='stage_reused') AS recovered_stages,"
                       + TOTALS + JOIN + scope +
                       " AND r.id IS NOT NULL GROUP BY r.id ORDER BY last_call DESC,r.id LIMIT ?",
                       [*args, RUN_LIMIT + 1])
    runs_truncated = len(runs) > RUN_LIMIT
    runs = runs[:RUN_LIMIT]
    for row in runs:
        row["retry_calls"] = run_retries.get(row["id"], 0)
        row["reused"] = row["reused_calls"] > 0
    stages = store.query("SELECT c.project,t.source AS kind,c.role,c.provider,c.model,c.effort," + TOTALS +
                         JOIN + scope + " GROUP BY c.project,t.source,c.role,c.provider,c.model,c.effort"
                         " ORDER BY tokens DESC,c.project,c.role LIMIT ?", [*args, STAGE_LIMIT + 1])
    stages_truncated = len(stages) > STAGE_LIMIT
    stages = stages[:STAGE_LIMIT]
    for row in stages:
        row["retry_calls"] = stage_retries.get((row["project"], row["kind"], row["role"], row["provider"],
                                                row["model"], row["effort"]), 0)
        row["reused"] = row["reused_calls"] > 0
    references = []
    reference_groups = {}
    for row in stages:
        if not row["project"] or not row["kind"]:
            continue
        key = (row["project"], row["kind"], row["role"], row["provider"], row["model"])
        current = reference_groups.setdefault(key, {"calls": 0, "tokens": 0, "model_seconds": 0})
        current["calls"] += row["calls"]
        current["tokens"] += row["tokens"]
        current["model_seconds"] += row["model_seconds"]
    for key, current in sorted(reference_groups.items(), key=lambda item: item[1]["tokens"], reverse=True)[:REFERENCE_GROUPS]:
        project_id, kind, role, provider, model = key
        conditions = (" WHERE c.project=? AND t.source=? AND c.role=? AND c.provider=? AND c.model=?"
                      " AND c.created<? AND c.state='completed'")
        filters = (project_id, kind, role, provider, model, start)
        denominator = store.one("SELECT count(*) AS n" + JOIN + conditions, filters)["n"]
        history_rows = store.query("SELECT c.tokens,c.seconds,c.usage_known" + JOIN + conditions +
                                   " ORDER BY c.created DESC LIMIT ?", (*filters, REFERENCE_SAMPLES))
        tokens = _interval([row["tokens"] for row in history_rows if row["usage_known"]])
        seconds = _interval([row["seconds"] for row in history_rows if row["seconds"] is not None])
        status = ("ok" if tokens and seconds else "partial" if tokens or seconds else "insufficient")
        references.append({
            "project": project_id, "kind": kind, "role": role, "provider": provider, "model": model,
            "current": current,
            "history": {
                "status": status, "denominator": denominator, "samples": len(history_rows),
                "sample_limit": REFERENCE_SAMPLES, "truncated": denominator > len(history_rows),
                "source": REFERENCE_SOURCE,
                "token_samples": sum(1 for row in history_rows if row["usage_known"]),
                "second_samples": sum(1 for row in history_rows if row["seconds"] is not None),
                "token_status": "ok" if tokens else "insufficient",
                "second_status": "ok" if seconds else "insufficient",
                "tokens": tokens, "seconds": seconds,
            },
        })
    result = {"start": start, "end": end, "summary": summary, "daily": daily, "roles": roles,
              "tasks": tasks, "runs": runs, "stages": stages, "references": {
                  "min_samples": REFERENCE_MIN_SAMPLES, "sample_limit": REFERENCE_SAMPLES,
                  "source": REFERENCE_SOURCE, "window": {"before": start, "metric": "per_call"},
                  "truncated": stages_truncated or len(reference_groups) > REFERENCE_GROUPS,
                  "groups": references},
              "total": count, "offset": offset, "limit": limit, "timezone": timezone,
              "runs_truncated": runs_truncated, "stages_truncated": stages_truncated,
              "retries_truncated": retries_truncated,
              "meta": {
                  "call_denominator": summary["calls"],
                  "price_denominator": {"priced": summary["priced_calls"],
                                        "unpriced": summary["unpriced_calls"],
                                        "unknown_usage": summary["unknown_calls"],
                                        "known_usage_unpriced": summary["known_unpriced_calls"]},
                  "pricing_basis": {"components": summary["component_priced_calls"],
                                    "unit_price": summary["unit_price_priced_calls"]},
                  "reference_source": REFERENCE_SOURCE,
                  "retry_definition": "calls_beyond_first_per_run_and_stage",
                  "bounded_windows": {"runs": RUN_LIMIT, "stages": STAGE_LIMIT,
                                      "retry_groups": RETRY_GROUP_LIMIT,
                                      "references": REFERENCE_GROUPS,
                                      "reference_samples": REFERENCE_SAMPLES},
              }}
    if task:
        result["calls"] = store.query("SELECT c.id,c.run,c.role,c.provider,c.model,c.effort,c.state,c.created,c.unit,"
                                      "c.tokens,c.input_tokens,c.output_tokens,c.cache_read_tokens,c.cache_write_tokens,"
                                      "c.usage_known,c.seconds,c.unit_price,c.input_price,c.output_price,"
                                      "c.cache_read_price,c.cache_write_price,c.price_basis," +
                                      _COST + " AS estimated_usd," + _PRICED + " AS priced" + JOIN + scope +
                                      " ORDER BY c.created DESC,c.id LIMIT ? OFFSET ?", [*args, limit, offset])
        result["call_total"] = summary["calls"]
    return result
