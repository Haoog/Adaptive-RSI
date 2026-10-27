"""按真实调用记录归集任务用量，失败重试和未知用量均保留。"""
import datetime
import math
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TOTALS = """count(*) AS calls,coalesce(sum(c.tokens),0) AS tokens,
coalesce(sum(c.input_tokens),0) AS input_tokens,coalesce(sum(c.output_tokens),0) AS output_tokens,
coalesce(sum(c.cache_read_tokens),0) AS cache_read_tokens,
coalesce(sum(c.cache_write_tokens),0) AS cache_write_tokens,
coalesce(sum(c.seconds),0) AS model_seconds,
coalesce(sum(c.usage_known=0),0) AS unknown_calls,
coalesce(sum(c.state='reserved'),0) AS pending_calls,
coalesce(sum(c.state='failed'),0) AS failed_calls,
sum(CASE WHEN c.usage_known=1 AND c.unit_price IS NOT NULL THEN c.tokens*c.unit_price/1000000 END) AS estimated_usd,
coalesce(sum(c.unit_price IS NULL OR c.usage_known=0),0) AS unpriced_calls"""
JOIN = " FROM calls c LEFT JOIN runs r ON r.id=c.run LEFT JOIN tasks t ON t.id=r.task "


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
    buckets = {}
    rows = store.stream("SELECT c.created,c.tokens,c.input_tokens,c.output_tokens,"
                        "c.cache_read_tokens,c.cache_write_tokens,c.usage_known,c.state,"
                        "c.seconds,c.unit_price" + JOIN + scope, args)
    try:
        for row in rows:
            local = datetime.datetime.fromtimestamp(row["created"], zone).date()
            bucket = buckets.setdefault(local, {
                "calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "model_seconds": 0,
                "unknown_calls": 0, "failed_calls": 0, "estimated_usd": None})
            bucket["calls"] += 1
            bucket["tokens"] += row["tokens"] or 0
            bucket["input_tokens"] += row["input_tokens"] or 0
            bucket["output_tokens"] += row["output_tokens"] or 0
            bucket["cache_read_tokens"] += row["cache_read_tokens"] or 0
            bucket["cache_write_tokens"] += row["cache_write_tokens"] or 0
            bucket["model_seconds"] += row["seconds"] or 0
            bucket["unknown_calls"] += int(not row["usage_known"])
            bucket["failed_calls"] += int(row["state"] == "failed")
            if row["usage_known"] and row["unit_price"] is not None:
                bucket["estimated_usd"] = (bucket["estimated_usd"] or 0) + (row["tokens"] or 0) * row["unit_price"] / 1000000
    finally:
        # 显式关闭生成器，连接立即归还连接池而不是等 GC。
        rows.close()
    daily = []
    if buckets:
        first = datetime.datetime.fromtimestamp(start, zone).date() if start else min(buckets)
        last = datetime.datetime.fromtimestamp(end - 0.001, zone).date()
        if (last - first).days <= 93:
            day = first
            while day <= last:
                buckets.setdefault(day, {"calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0,
                                        "cache_read_tokens": 0, "cache_write_tokens": 0, "model_seconds": 0,
                                        "unknown_calls": 0, "failed_calls": 0, "estimated_usd": None})
                day += datetime.timedelta(days=1)
        else:
            monthly = {}
            for day, values in buckets.items():
                month = day.replace(day=1)
                target = monthly.setdefault(month, {key: None if key == "estimated_usd" else 0 for key in values})
                for key, amount in values.items():
                    if amount is not None:
                        target[key] = (target[key] or 0) + amount
            day = first.replace(day=1)
            while day <= last:
                monthly.setdefault(day, {key: None if key == "estimated_usd" else 0 for key in next(iter(buckets.values()))})
                day = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
            buckets = monthly
    for day, bucket in sorted(buckets.items()):
        midnight = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
        daily.append({"day": midnight.timestamp(), "date": day.isoformat(), **bucket})
    roles = store.query("SELECT c.role,c.provider,c.model,c.effort," + TOTALS + JOIN + scope +
                        " GROUP BY c.role,c.provider,c.model,c.effort ORDER BY tokens DESC", args)
    group = "coalesce(t.id,'background:' || coalesce(c.project,'global'))"
    count = store.one("SELECT count(*) AS n FROM (SELECT " + group + JOIN + scope + " GROUP BY 1)", args)["n"]
    sort = {"tokens": "tokens DESC", "recent": "last_call DESC", "duration": "model_seconds DESC"}.get(value("sort", "recent"))
    if sort is None:
        raise ValueError("Invalid usage sort")
    tasks = store.query("SELECT " + group + " AS id,t.id AS task,t.title,t.source,t.state,c.project,"
                        + background + " AS background,count(DISTINCT r.id) AS attempts,"
                        "min(c.created) AS first_call,max(c.created) AS last_call,"
                        "group_concat(DISTINCT c.provider || '/' || c.model) AS models," + TOTALS + JOIN + scope +
                        " GROUP BY 1 ORDER BY " + sort + ",id LIMIT ? OFFSET ?", [*args, limit, offset])
    result = {"start": start, "end": end, "summary": summary, "daily": daily, "roles": roles,
              "tasks": tasks, "total": count, "offset": offset, "limit": limit, "timezone": timezone}
    if task:
        result["calls"] = store.query("SELECT c.id,c.run,c.role,c.provider,c.model,c.effort,c.state,c.created,c.unit,"
                                     "c.tokens,c.input_tokens,c.output_tokens,c.cache_read_tokens,c.cache_write_tokens,"
                                     "c.usage_known,c.seconds,c.unit_price" + JOIN + scope +
                                     " ORDER BY c.created DESC,c.id LIMIT ? OFFSET ?", [*args, limit, offset])
        result["call_total"] = summary["calls"]
    return result
