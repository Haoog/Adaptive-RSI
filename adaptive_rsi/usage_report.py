"""按真实调用记录归集任务用量，失败重试和未知用量均保留。"""
import math
import time


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
    start, end = float(value("start", now - 7 * 86400)), float(value("end", now))
    if not all(math.isfinite(x) for x in [start, end]) or start < 0 or end <= start:
        raise ValueError("Invalid usage time range")
    limit, offset = int(value("limit", 25)), int(value("offset", 0))
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
    daily = store.query("SELECT cast(c.created/86400 AS INTEGER)*86400 AS day," + TOTALS + JOIN + scope +
                        " GROUP BY day ORDER BY day", args)
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
              "tasks": tasks, "total": count, "offset": offset, "limit": limit, "timezone": "UTC"}
    if task:
        result["calls"] = store.query("SELECT c.id,c.run,c.role,c.provider,c.model,c.effort,c.state,c.created,c.unit,"
                                     "c.tokens,c.input_tokens,c.output_tokens,c.cache_read_tokens,c.cache_write_tokens,"
                                     "c.usage_known,c.seconds,c.unit_price" + JOIN + scope +
                                     " ORDER BY c.created DESC,c.id LIMIT ? OFFSET ?", [*args, limit, offset])
        result["call_total"] = summary["calls"]
    return result
