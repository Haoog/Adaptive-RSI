"""按真实调用记录归集任务用量，失败重试和未知用量均保留。"""
from collections import Counter, defaultdict
import datetime
import functools
from itertools import groupby
import math
import statistics
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store, decode_evidence
from .usage import PRICE_FIELDS, estimate, unit_prices


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
            # store 可按位置或关键字传入；位置参数缺失时回退 kwargs，保持各函数原有调用约定。
            store = kwargs.get("store") if len(args) <= store_arg else args[store_arg]
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
    group = "coalesce(t.id,'background:' || coalesce(c.project,'global'))"
    summary = store.one("SELECT " + TOTALS + ",count(DISTINCT t.id) AS tasks" + JOIN + scope, args)
    summary["reused"] = summary["reused_calls"] > 0
    buckets = {}
    rows = store.stream("SELECT c.created,c.tokens,c.input_tokens,c.output_tokens,"
                        "c.cache_read_tokens,c.cache_write_tokens,c.usage_known,c.state,"
                        "c.seconds," + _COST + " AS cost_usd," + _PRICED + " AS priced" +
                        JOIN + scope, args)
    try:
        for row in rows:
            if row["created"] is None:
                continue
            local = datetime.datetime.fromtimestamp(math.floor(row["created"]), zone).date()
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
        first = datetime.datetime.fromtimestamp(math.floor(start), zone).date() if start else min(buckets)
        # Local dates change on whole-second boundaries. Use the final second that
        # intersects [start, end) without rounding across midnight.
        last = datetime.datetime.fromtimestamp(math.ceil(end) - 1, zone).date()
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
    # Probe for a prior call per in-window group using the (run, created) index.
    # When one exists, every call in the window is a retry for that group.
    retry_rows = store.query("SELECT c.run,c.project,t.source AS kind,c.role,c.provider,c.model,c.effort,"
                             + group + " AS task_group,count(*)-1+EXISTS ("
                             "SELECT 1 FROM calls prior WHERE prior.run=c.run AND prior.created<?"
                             " AND prior.project IS c.project AND prior.role IS c.role"
                             " AND prior.provider IS c.provider AND prior.model IS c.model"
                             " AND prior.effort IS c.effort) AS retries" + JOIN + scope +
                             " AND c.run IS NOT NULL"
                             " GROUP BY c.run,c.role,c.provider,c.model,c.effort,c.project,t.source"
                             " HAVING retries>0 ORDER BY c.run,c.role,c.provider,c.model,c.effort,c.project LIMIT ?",
                             [start, *args, RETRY_GROUP_LIMIT + 1])
    retries_truncated = len(retry_rows) > RETRY_GROUP_LIMIT
    run_retries, task_retries, role_retries, stage_retries = {}, {}, {}, {}
    for row in retry_rows[:RETRY_GROUP_LIMIT]:
        extra = row["retries"]
        run_retries[row["run"]] = run_retries.get(row["run"], 0) + extra
        task_retries[row["task_group"]] = task_retries.get(row["task_group"], 0) + extra
        role_key = (row["role"], row["provider"], row["model"], row["effort"])
        role_retries[role_key] = role_retries.get(role_key, 0) + extra
        stage_key = (row["project"], row["kind"], row["role"], row["provider"], row["model"], row["effort"])
        stage_retries[stage_key] = stage_retries.get(stage_key, 0) + extra
    roles = store.query("SELECT c.role,c.provider,c.model,c.effort," + TOTALS + JOIN + scope +
                        " GROUP BY c.role,c.provider,c.model,c.effort ORDER BY tokens DESC", args)
    for row in roles:
        row["retry_calls"] = role_retries.get((row["role"], row["provider"], row["model"], row["effort"]), 0)
        row["reused"] = row["reused_calls"] > 0
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
        row["retry_calls"] = task_retries.get(row["id"], 0)
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
        current["calls"] += row["calls"] or 0
        current["tokens"] += row["tokens"] or 0
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


_AUTOMATIC_FINAL_STATES = frozenset(('completed', 'merged', 'failed', 'cancelled', 'expired', 'obsolete',
                                     'rejected', 'pr_closed', 'unverified_merge', 'reverted', 'superseded',
                                     'insufficient_evidence'))


def _history_cost(row):
    prices = unit_prices({key: row[key + '_price'] for key in PRICE_FIELDS})
    unit = unit_prices({'input': row['unit_price']}).get('input')
    # Keep malformed prices out of known-cost samples without treating an explicit zero as missing.
    known_usage = row['usage_known'] == 1 and row['call_state'] in ('completed', 'failed')
    cost, basis = estimate(row, prices, unit) if known_usage else (None, 'unknown')
    route = (row['role'], row['provider'], row['model'], row['effort'], row['unit'],
             (unit, *(prices.get(key) for key in PRICE_FIELDS)))
    issue = None if cost is not None else 'unknown_price' if known_usage else 'unknown_usage'
    return route, cost, basis, issue


def _route_fields(route):
    role, provider, model, effort, unit, prices = route
    return dict(role=role, provider=provider, model=model, effort=effort, call_unit=unit,
                prices_usd_per_million=dict(zip(('mixed', *PRICE_FIELDS), prices)))


def _history_bucket():
    return {'samples': 0, 'costs': [], 'unknown_usage': 0, 'unknown_price': 0, 'missing_calls': 0,
            'basis': Counter()}


def _add_sample(bucket, costs, bases, issues):
    bucket['samples'] += 1
    bucket['basis'].update(bases)
    for issue in issues:
        bucket[issue] += 1
    if not issues:
        bucket['costs'].append(math.fsum(costs))


def _cost_summary(bucket, distinct_tasks=None):
    interval = (_interval(bucket['costs'])
                if distinct_tasks is None or distinct_tasks >= REFERENCE_MIN_SAMPLES else None)
    return {'samples': bucket['samples'], 'known_cost_samples': len(bucket['costs']),
            'unknown_usage_samples': bucket['unknown_usage'], 'unknown_price_samples': bucket['unknown_price'],
            'samples_with_missing_calls': bucket['missing_calls'], 'priced_calls_by_basis': dict(bucket['basis']),
            'cost_usd': interval, 'cost_status': 'historical_interval' if interval else 'insufficient_samples'}


def policy_history(db, start, end, project=None):
    """Observe run-stage cost/quality and whole-task automatic cost from a read-only snapshot."""
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= 4102444800):
        raise ValueError('Invalid history time range')
    scope = ' AND p.project=?' if project else ''
    args = [start, end, start, end, *([project] if project else [])]
    rows = db.execute('WITH selected AS (SELECT DISTINCT p.task FROM runs p WHERE '
                      '(p.ended>=? AND p.ended<? OR p.started>=? AND p.started<?)' + scope + ') '
                      'SELECT t.id AS task,t.project,t.source,t.state AS task_state,t.created AS task_created,'
                      't.updated AS task_updated,r.id AS run,r.started,r.ended,r.calls AS recorded_calls,r.result,'
                      'c.id AS call,c.state AS call_state,c.created AS call_created,c.role,c.provider,c.model,c.effort,'
                      'c.unit,c.usage_known,c.tokens,c.unit_price,' +
                      ','.join('c.' + field + suffix for suffix in ('_price', '_tokens') for field in PRICE_FIELDS) +
                      ' FROM selected s JOIN tasks t ON t.id=s.task JOIN runs r ON r.task=t.id '
                      'LEFT JOIN calls c ON c.run=r.id ORDER BY t.id,r.id,c.id', args)
    stages, automatic = defaultdict(_history_bucket), defaultdict(_history_bucket)
    stage_quality, automatic_coverage = defaultdict(Counter), defaultdict(Counter)
    stage_tasks = Counter()
    observed_runs = ungrouped_runs = 0
    for _, task_rows in groupby(rows, key=lambda row: row['task']):
        task_stage_keys, priced_stage_keys, quality_stage_keys = set(), set(), set()
        routes, task_costs, task_bases, task_issues = set(), [], [], set()
        outside, ended_in_window = False, False
        for _, run_rows in groupby(task_rows, key=lambda row: row['run']):
            run = [dict(row) for row in run_rows]
            first = run[0]
            scope_key = (first['project'], first['source'])
            ended = first['ended']
            finished = ended is not None and start <= ended < end
            ended_in_window |= finished
            inside = finished and first['started'] is not None and start <= first['started'] <= ended
            outside |= not inside
            calls = [row for row in run if row['call'] is not None]
            missing = not calls or (first['recorded_calls'] or 0) > len(calls)
            if missing:
                task_issues.add('missing_calls')
            run_routes = defaultdict(list)
            for row in calls:
                at = row['call_created']
                if at is None or not start <= at < end:
                    outside = True
                    missing = True
                    continue
                route, cost, basis, issue = _history_cost(row)
                run_routes[route].append((cost, basis, issue))
            if inside:
                observed_runs += 1
                ungrouped_runs += not run_routes
            evidence = decode_evidence(first['result']) if inside else {}
            decisions = [evidence.get(field, {}).get('decision') for field in ('evaluation', 'review')]
            passed_checks = decisions == ['pass', 'pass']
            observed_quality = passed_checks or 'fail' in decisions
            for route, observations in run_routes.items():
                costs = [cost for cost, _, _ in observations if cost is not None]
                bases = [basis for cost, basis, _ in observations if cost is not None]
                issues = {issue for _, _, issue in observations if issue}
                if missing:
                    issues.add('missing_calls')
                routes.add(route)
                task_costs.extend(costs)
                task_bases.extend(bases)
                task_issues.update(issues)
                if inside:
                    key = (*scope_key, route)
                    _add_sample(stages[key], costs, bases, issues)
                    task_stage_keys.add(key)
                    if not issues:
                        priced_stage_keys.add(key)
                    if observed_quality:
                        quality_stage_keys.add(key)
                        stage_quality[key]['observed'] += 1
                        stage_quality[key]['passed'] += passed_checks
        for key in task_stage_keys:
            stage_tasks[key] += 1
        for key in priced_stage_keys:
            stage_quality[key]['priced_tasks'] += 1
        for key in quality_stage_keys:
            stage_quality[key]['quality_tasks'] += 1
        if not (first['source'] or '').startswith('system_') or not ended_in_window:
            continue
        coverage = automatic_coverage[scope_key]
        coverage['tasks_with_ended_attempt'] += 1
        if (outside or first['task_created'] is None or first['task_created'] < start or
                first['task_updated'] is None or first['task_updated'] >= end or
                first['task_state'] not in _AUTOMATIC_FINAL_STATES):
            coverage['excluded_incomplete_or_window_boundary'] += 1
            continue
        key = (*scope_key, tuple(sorted(routes, key=repr)))
        _add_sample(automatic[key], task_costs, task_bases, task_issues)
        coverage['eligible_tasks'] += 1
        coverage['unknown_cost_tasks'] += bool(task_issues)
    stage_groups = []
    for (project_id, source, route), bucket in sorted(stages.items(), key=lambda item: repr(item[0])):
        evidence = stage_quality[(project_id, source, route)]
        observed, passed = evidence['observed'], evidence['passed']
        enough_quality = evidence['quality_tasks'] >= REFERENCE_MIN_SAMPLES
        stage_groups.append(dict(project=project_id, source=source, **_route_fields(route),
                                 **_cost_summary(bucket, evidence['priced_tasks']),
                                 distinct_tasks=stage_tasks[(project_id, source, route)],
                                 known_cost_distinct_tasks=evidence['priced_tasks'],
                                 quality={'observed_runs': observed, 'passed_runs': passed,
                                          'missing_runs': bucket['samples'] - observed,
                                          'distinct_tasks': evidence['quality_tasks'],
                                          'pass_fraction': passed / observed if enough_quality else None,
                                          'status': 'observed' if enough_quality else 'insufficient_samples'}))
    automatic_groups = [dict(project=project_id, source=source, routes=[_route_fields(route) for route in routes],
                             **_cost_summary(bucket))
                        for (project_id, source, routes), bucket in sorted(automatic.items(), key=lambda item: repr(item[0]))]
    return {'format': 1, 'provenance': 'local_sqlite_snapshot',
            'window': {'start': start, 'end': end, 'project': project}, 'min_samples': REFERENCE_MIN_SAMPLES,
            'stage_history': {'unit': 'run_stage', 'status': 'observed' if any(
                group['cost_usd'] or group['quality']['status'] == 'observed' for group in stage_groups)
                else 'insufficient_samples', 'observed_runs': observed_runs,
                'runs_without_grouped_calls': ungrouped_runs, 'groups': stage_groups},
            'automatic_task_cost': {'unit': 'whole_task_all_attempts',
                                    'status': 'historical_interval' if any(group['cost_usd'] for group in automatic_groups)
                                    else 'insufficient_samples', 'groups': automatic_groups,
                                    'coverage': [dict(project=key[0], source=key[1], **value)
                                                 for key, value in sorted(automatic_coverage.items())]},
            'interpretation': 'Descriptive observation. Quality is a run-level outcome and may repeat across cohorts. '
                              'Intervals are historical quartiles of fully priced observations at recorded tariffs.'}
