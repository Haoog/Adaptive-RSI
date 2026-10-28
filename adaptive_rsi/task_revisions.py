"""Exact requirement revisions and the verification invalidations they caused."""
import difflib

from .store import decode_object


def revision_page(store, task, project, before=2**63 - 1):
    if type(before) is not int or not 0 < before <= 2**63 - 1:
        raise ValueError("Invalid revision pagination")
    if not store.one("SELECT 1 FROM tasks WHERE id=? AND project=?", (task, project)):
        raise ValueError("Task not found")
    rows = store.query("SELECT id,supplement,created FROM task_revisions WHERE task=? AND id<? "
                       "ORDER BY id DESC LIMIT 101", (task, before))
    return {"revisions": rows[:100], "has_more": len(rows) > 100,
            "next_before": rows[99]["id"] if len(rows) > 100 else None}


def revision_detail(store, revision, project):
    row = store.one("SELECT v.id,v.task,v.previous_body,v.supplement,v.created,s.after_body,s.impact "
                    "FROM task_revisions v JOIN tasks t ON t.id=v.task "
                    "LEFT JOIN task_revision_snapshots s ON s.revision=v.id WHERE v.id=? AND t.project=?",
                    (revision, project))
    if row is None:
        raise ValueError("Task revision not found")
    row["snapshot_available"] = row["after_body"] is not None
    row["impact"] = decode_object(row["impact"]) if row["impact"] else None
    row["diff"] = None
    if row["snapshot_available"]:
        before = (row["previous_body"] or "").splitlines()
        after = row["after_body"].splitlines()
        if max(len(before), len(after)) <= 2000:
            row["diff"] = "\n".join(difflib.unified_diff(before, after, fromfile="before", tofile="after", lineterm=""))
        else:
            row["diff_unavailable"] = "line_limit"
    return row
