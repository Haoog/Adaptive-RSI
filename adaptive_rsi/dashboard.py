"""Project-scoped read models; pagination cannot hide another project's work."""
import json


ACTIVE = ["queued", "planning", "executing", "evaluating", "pr_open"]
ATTENTION = ["waiting_budget", "awaiting_approval", "blocked", "failed", "insufficient_evidence", "interrupted", "regressed", "unverified_merge", "ci_failed"]

# Derive attention from the latest attempt without changing the PR tracking state.
TASKS = """(SELECT t.*, CASE
    WHEN t.state='pr_open' AND json_extract((SELECT result FROM runs WHERE task=t.id ORDER BY started DESC,id LIMIT 1), '$.delivery_status')='ci_failed' THEN 'ci_failed'
    WHEN t.state='merged' AND json_extract((SELECT result FROM runs WHERE task=t.id ORDER BY started DESC,id LIMIT 1), '$.feedback.state')='regressed' THEN 'regressed'
    ELSE t.state END AS display_state FROM tasks t)"""


def records(store, project=None, limit=50, offset=0, group="all", include_system=True):
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    clause, args = ("project=?", [project]) if project else ("1=1", [])
    task_clause = clause
    if not include_system:
        task_clause += " AND source NOT LIKE 'system_%'"
    if group in ["active", "attention", "history"]:
        states = ACTIVE if group == "active" else ATTENTION if group == "attention" else ACTIVE + ATTENTION
        task_clause += " AND display_state " + ("NOT " if group == "history" else "") + "IN (" + ",".join("?" for _ in states) + ")"
        args += states
    total = store.one("SELECT count(*) AS n FROM " + TASKS + " WHERE " + task_clause, args)["n"]
    tasks = store.query("SELECT * FROM " + TASKS + " WHERE " + task_clause + " ORDER BY updated DESC,id LIMIT ? OFFSET ?", (*args, limit, offset))
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
    for row in runs:
        result = json.loads(row.pop("result"))
        for key in ["summary", "reason", "pr", "value", "plan_reason", "plan_decision", "failure", "stage", "delivery_status"]:
            row[key] = result.get(key)
        row["scope"] = result.get("scope", result.get("files", []))
        row["review_decision"] = result.get("review", {}).get("decision")
        row["evaluation_decision"] = result.get("evaluation", {}).get("decision")
    return {"tasks": tasks, "runs": runs,
            "events": store.query("SELECT * FROM events WHERE " + ("(project=? OR project IS NULL)" if project else "1=1") + " ORDER BY seq DESC LIMIT 100", base_args),
            "pagination": {"total": total, "limit": limit, "offset": offset, "has_more": offset + limit < total},
            "task_counts": store.query("SELECT display_state AS state,count(*) AS count FROM " + TASKS + " WHERE " + clause + " GROUP BY display_state", base_args)}
