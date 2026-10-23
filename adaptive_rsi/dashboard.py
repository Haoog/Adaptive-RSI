"""Project-scoped read models; pagination cannot hide another project's work."""
import json


ACTIVE = ["queued", "planning", "executing", "evaluating", "pr_open", "releasing", "deploying"]
ATTENTION = ["waiting_budget", "awaiting_approval", "blocked", "failed", "insufficient_evidence", "interrupted", "regressed", "unverified_merge", "ci_failed", "release_failed", "deploy_failed", "expired"]

# Derive attention from the latest attempt without changing the PR tracking state.
TASKS = """(SELECT t.*, CASE
    WHEN t.state='pr_open' AND json_extract(r.result, '$.delivery_status')='ci_failed' THEN 'ci_failed'
    WHEN t.state='merged' AND json_extract(r.result, '$.feedback.state')='regressed' THEN 'regressed'
    WHEN t.state='merged' AND json_extract(r.result, '$.automation.deploy.state') IN ('failed','health_failed','rollback_failed','blocked','blocked_verification','dispatch_unknown','rerun_unknown') THEN 'deploy_failed'
    WHEN t.state='merged' AND json_extract(r.result, '$.automation.release.state') IN ('failed','blocked','blocked_verification','dispatch_unknown','rerun_unknown') THEN 'release_failed'
    WHEN t.state='merged' AND json_extract(r.result, '$.automation.deploy.state') IN ('pending','dispatching','submitted','queued','running','rerunning','rolling_back','waiting_target','waiting_verification','waiting_release') THEN 'deploying'
    WHEN t.state='merged' AND json_extract(r.result, '$.automation.release.state') IN ('pending','dispatching','submitted','queued','running','rerunning','waiting_verification') THEN 'releasing'
    ELSE t.state END AS display_state FROM tasks t LEFT JOIN runs r
    ON r.id=(SELECT id FROM runs WHERE task=t.id ORDER BY started DESC,id LIMIT 1))"""


def records(store, project=None, limit=50, offset=0, group="all", include_system=True):
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    clause, args = ("project=?", [project]) if project else ("1=1", [])
    count_clause = clause
    if not include_system:
        count_clause += " AND source NOT LIKE 'system_%'"
    task_clause = count_clause
    if group in ["active", "attention", "history"]:
        states = ACTIVE if group == "active" else ATTENTION if group == "attention" else ACTIVE + ATTENTION
        task_clause += " AND display_state " + ("NOT " if group == "history" else "") + "IN (" + ",".join("?" for _ in states) + ")"
        args += states
    total = store.one("SELECT count(*) AS n FROM " + TASKS + " WHERE " + task_clause, args)["n"]
    import time
    from .experience import queue_status
    tasks = store.query("SELECT * FROM " + TASKS + " tasks WHERE " + task_clause +
                        " ORDER BY " + store.queue_order(display=True) +
                        " LIMIT ? OFFSET ?", (*args, time.time(), time.time(), limit, offset))
    queue = {row["id"]: row for row in queue_status(store, project)} if project else {}
    for task in tasks:
        if task["id"] in queue:
            task.update({k: queue[task["id"]][k] for k in ["wait_reason", "queue_position", "dependency"]})
    base_args = (project,) if project else ()
    # Include the latest attempt for every visible task, even if it is older than the history window.
    task_ids = [t["id"] for t in tasks]
    runs = store.query("SELECT id,project,task,state,calls,tokens,started,ended,base,result FROM runs WHERE " + clause +
                       " ORDER BY started DESC LIMIT 100", base_args)
    if task_ids:
        latest = store.query("SELECT r.id,r.project,r.task,r.state,r.calls,r.tokens,r.started,r.ended,r.base,r.result "
                             "FROM runs r WHERE r.task IN (" + ",".join("?" for _ in task_ids) + ") "
                             "AND r.id=(SELECT id FROM runs WHERE task=r.task ORDER BY started DESC,id LIMIT 1)", task_ids)
        runs = sorted({r["id"]: r for r in runs + latest}.values(), key=lambda r: r["started"], reverse=True)
    active = store.query("SELECT id,project,task,state,calls,tokens,started,ended,base,result FROM runs WHERE " + clause + " AND ended IS NULL", base_args)
    runs = sorted({r["id"]: r for r in runs + active}.values(), key=lambda r: r["started"], reverse=True)
    if runs:
        stage_rows = store.query(
            "SELECT s.run,s.stage,s.created,json_extract(s.data,'$.decision') AS decision "
            "FROM stages s JOIN (SELECT run,max(rowid) AS rowid FROM stages WHERE run IN ("
            + ",".join("?" for _ in runs) + ") GROUP BY run) latest ON latest.rowid=s.rowid",
            tuple(r["id"] for r in runs))
        latest_stage = {}
        for stage in stage_rows:
            latest_stage[stage["run"]] = stage
        by_run = {run["id"]: latest_stage.get(run["id"]) for run in runs}
    else:
        by_run = {}
    for row in runs:
        result = json.loads(row.pop("result"))
        for key in ["summary", "reason", "pr", "value", "plan_reason", "plan_decision", "failure", "stage", "delivery_status", "automation"]:
            row[key] = result.get(key)
        row["scope"] = result.get("scope", result.get("files", []))
        row["review_decision"] = result.get("review", {}).get("decision")
        row["evaluation_decision"] = result.get("evaluation", {}).get("decision")
        stage = by_run.get(row["id"])
        row["last_stage"] = stage["stage"] if stage else None
        row["last_stage_at"] = stage["created"] if stage else None
        row["last_stage_decision"] = stage["decision"] if stage else None
    return {"tasks": tasks, "runs": runs,
            "events": store.query("SELECT * FROM events WHERE " + ("(project=? OR project IS NULL)" if project else "1=1") + " ORDER BY seq DESC LIMIT 100", base_args),
            "pagination": {"total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total},
            "task_counts": store.query("SELECT display_state AS state,count(*) AS count FROM " + TASKS + " WHERE " + count_clause + " GROUP BY display_state", base_args)}
