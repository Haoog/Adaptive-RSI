"""Bounded cancellation impact previews with transactional execution receipts."""
import time

from .config import fingerprint
from .store import encode
from .store import decode_object


CANCELLABLE = ("queued", "planning", "executing", "evaluating", "blocked", "waiting_budget",
               "deferred", "insufficient_evidence", "failed", "interrupted", "awaiting_approval")


def intervention_reason(reason):
    if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
        raise ValueError("Intervention reason must be at most 2000 characters")
    return reason


def intervention_history(store, task, project, before=2**63 - 1):
    if type(before) is not int or not 0 < before <= 2**63 - 1:
        raise ValueError("Invalid event pagination")
    if not store.one("SELECT 1 FROM tasks WHERE id=? AND project=?", (task, project)):
        raise ValueError("Task not found")
    rows = store.query("SELECT seq,run,kind,created,data FROM events WHERE project=? AND seq<? "
                       "AND kind IN ('manual_retry','requirements_revised','queue_settings','cancelled','delivery_approved') "
                       "AND json_valid(data) AND json_extract(data,'$.task')=? ORDER BY seq DESC LIMIT 101",
                       (project, before, task))
    for row in rows:
        row["data"] = decode_object(row["data"])
    return {"task": task, "project": project, "entries": rows[:100], "has_more": len(rows) > 100,
            "next_before": rows[99]["seq"] if len(rows) > 100 else None, "limit": 100}


def cancellation_preview(db, task):
    root = db.execute("SELECT id,project,title,state,updated FROM tasks WHERE id=?", (task,)).fetchone()
    if root is None:
        raise ValueError("Task not found")
    if root["state"] not in CANCELLABLE:
        raise ValueError("This task has finished and can no longer be cancelled")
    rows = db.execute(
        "WITH RECURSIVE affected(id) AS (SELECT ? UNION "
        "SELECT d.task FROM task_dependencies d JOIN affected a ON d.dependency=a.id "
        "JOIN tasks t ON t.id=d.task WHERE t.project=? LIMIT 1001) "
        "SELECT t.id,t.title,t.state,t.updated FROM tasks t JOIN affected a ON a.id=t.id ORDER BY t.id LIMIT 1001",
        (task, root["project"])).fetchall()
    truncated = len(rows) > 1000
    affected = [dict(row) for row in rows if row["id"] != task][:999]
    for row in affected:
        row["cancellable"] = row["state"] in CANCELLABLE
    edges = [tuple(row) for row in db.execute(
        "WITH RECURSIVE affected(id) AS (SELECT ? UNION "
        "SELECT d.task FROM task_dependencies d JOIN affected a ON d.dependency=a.id "
        "JOIN tasks t ON t.id=d.task WHERE t.project=? LIMIT 1001) "
        "SELECT d.task,d.dependency FROM task_dependencies d JOIN affected a ON a.id=d.task "
        "ORDER BY d.task,d.dependency", (task, root["project"]))]
    return {"task": dict(root), "affected": affected,
            "revision": fingerprint({"root": dict(root), "tasks": [dict(row) for row in rows], "edges": edges}),
            "limit": 1000, "truncated": truncated}


def cancel(runtime, task, cascade=False, expected=None, reason=None):
    intervention_reason(reason)
    if type(cascade) is not bool:
        raise ValueError("Cascade cancellation must be a boolean")
    if cascade and not isinstance(expected, str):
        raise ValueError("Preview dependent cancellations before confirming")
    with runtime.dispatch_lock, runtime.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        plan = cancellation_preview(db, task)
        if cascade and plan["truncated"]:
            raise ValueError("Cancellation impact is truncated; cancel only the current task")
        if expected is not None and expected != plan["revision"]:
            raise ValueError("Cancellation preview changed; review it again")
        receipts = []
        now = time.time()
        for row in [plan["task"], *plan["affected"]]:
            selected = row["id"] == task or cascade and row["cancellable"]
            if not selected:
                receipts.append({"id": row["id"], "state": "unchanged", "previous_state": row["state"]})
                continue
            db.execute("UPDATE tasks SET state='cancelled',updated=? WHERE id=?", (now, row["id"]))
            db.execute("INSERT INTO events(project,run,kind,message,data,created) "
                       "VALUES (?,NULL,'cancelled','Cancellation requested',?,?)",
                       (plan["task"]["project"], encode({"task": row["id"], "root": task,
                         "cascade": cascade, "preview_revision": plan["revision"], "reason": reason,
                         "before": {"state": row["state"]}, "after": {"state": "cancelled"}}), now))
            receipts.append({"id": row["id"], "state": "cancelled", "previous_state": row["state"]})
    runtime.wake.set()
    return {"saved": True, "revision": plan["revision"], "cascade": cascade, "receipts": receipts,
            "impact_truncated": plan["truncated"]}
