"""Group lineage and conservative split-redo of failed grouped requirements."""
import json

from .config import fingerprint
from .store import decode_object


RETRYABLE_GROUP_STATES = ("failed", "blocked", "interrupted", "insufficient_evidence", "cancelled")


def grouped_body(rows):
    return "\n\n".join("[" + row["id"] + "] " + row["title"] + "\n" + (row["body"] or "") +
                        "\nAcceptance: " + (row["acceptance"] or "[]") for row in rows)


def split_preview(store, group, project, db=None):
    def query(sql, args):
        return [dict(row) for row in db.execute(sql, args)] if db is not None else store.query(sql, args)
    if group["state"] not in RETRYABLE_GROUP_STATES or not (group["fingerprint"] or "").startswith("batch:"):
        raise ValueError("Only a stopped failed or cancelled group can be split for retry")
    try:
        identifiers = json.loads(group["evidence"])[0]["batch_tasks"]
    except (TypeError, ValueError, IndexError, KeyError):
        raise ValueError("Task has no valid grouping evidence") from None
    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100 or
            any(not isinstance(value, str) for value in identifiers) or len(set(identifiers)) != len(identifiers)):
        raise ValueError("Task has no valid grouping evidence")
    found = {row["id"]: row for row in query("SELECT * FROM tasks WHERE project=? AND id IN (" +
             ",".join("?" for _ in identifiers) + ")", (project, *identifiers))}
    originals = [found.get(task) for task in identifiers]
    if any(row is None or row["state"] != "superseded" for row in originals):
        raise ValueError("Original task changed during restore")
    memberships = query("SELECT original_task,restored FROM task_group_members WHERE group_task=?", (group["id"],))
    if ({row["original_task"] for row in memberships} != set(identifiers) or
            any(row["restored"] is not None for row in memberships)):
        raise ValueError("Original task changed during restore")
    if query("SELECT 1 FROM runs WHERE task=? AND ended IS NULL", (group["id"],)):
        raise ValueError("Wait for the grouped run to stop before splitting")
    return {"originals": [{"id": row["id"], "title": row["title"], "state": row["state"]} for row in originals],
            "supplement_required": group["body"] != grouped_body(originals),
            "supplement_available": max(row["available"] for row in originals),
            "fresh_verification_required": True,
            "revision": fingerprint({"group": group, "originals": originals})}


def acceptance_evidence(store, task, project):
    """Map original criteria to actual independent check rows, never group summaries."""
    grouped = store.one("SELECT evidence FROM tasks WHERE id=? AND project=?", (task, project))
    try:
        originals = json.loads(grouped["evidence"])[0]["originals"]
    except (TypeError, ValueError, KeyError, IndexError):
        originals = []
    run = store.one("SELECT id,result FROM runs WHERE task=? AND project=? ORDER BY started DESC,id DESC LIMIT 1", (task, project))
    result = decode_object(run["result"]) if run else {}
    evaluation = result.get("evaluation") or {}
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    criteria = evaluation.get("criteria", [])
    criteria = criteria if isinstance(criteria, list) else []
    coverage = evaluation.get("coverage", [])
    coverage = coverage if isinstance(coverage, list) else []
    check_groups = evaluation.get("checks")
    checks = check_groups.get("acceptance_candidate", []) if isinstance(check_groups, dict) else []
    checks = checks if isinstance(checks, list) else []
    requirements = []
    for original in originals if isinstance(originals, list) else []:
        if not isinstance(original, dict):
            continue
        for index, criterion in enumerate(original.get("acceptance", [])):
            indexes = [i for i, value in enumerate(criteria) if value == criterion]
            names = {entry.get("name") for entry in coverage if isinstance(entry, dict) and
                     isinstance(entry.get("criteria"), list) and any(i in entry["criteria"] for i in indexes) and
                     isinstance(entry.get("name"), str)}
            linked = [{**check, "run": run["id"], "stage": "acceptance_candidate", "index": i}
                      for i, check in enumerate(checks) if isinstance(check, dict) and check.get("name") in names]
            state = "fail" if any(check.get("passed") is False for check in linked) else (
                "pass" if linked and names == {check.get("name") for check in linked} and
                all(check.get("passed") is True for check in linked) else "unknown")
            requirements.append({"task": original["id"], "index": index, "criterion": criterion,
                                 "state": state, "checks": linked})
    from .batching import verification_cost
    current_task = store.one("SELECT * FROM tasks WHERE id=? AND project=?", (task, project))
    actual = verification_cost(store, project, [current_task]) if current_task else None
    return {"requirements": requirements, "actual_verification_cost": actual,
            "all_passed": bool(requirements) and
            evaluation.get("decision") == "pass" and all(row["state"] == "pass" for row in requirements)}


def lineage(store, task, project, before=2**63 - 1):
    if not isinstance(before, int) or not 0 < before <= 2**63 - 1:
        raise ValueError("Invalid event pagination")
    if not store.one("SELECT 1 FROM tasks WHERE id=? AND project=?", (task, project)):
        raise ValueError("Task not found")
    groups = store.query("SELECT m.rowid AS cursor,g.id,g.title,g.state,g.created,m.restored,r.id AS run_id,r.state AS run_state,r.result AS run_result "
                         "FROM task_group_members m JOIN tasks g ON g.id=m.group_task "
                         "LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE task=g.id ORDER BY started DESC,id DESC LIMIT 1) "
                         "WHERE m.original_task=? AND g.project=? AND m.rowid<? "
                         "ORDER BY m.rowid DESC LIMIT 101", (task, project, before))
    for group in groups[:100]:
        result = decode_object(group.pop("run_result"))
        run_id, run_state = group.pop("run_id"), group.pop("run_state")
        run = {"id": run_id, "state": run_state, "summary": result.get("summary"), "pr": result.get("pr")} if run_id else None
        group["latest_run"] = run
        group["evidence_scope"] = "whole_group"
    members = store.query("SELECT t.id,t.title,t.state,m.restored FROM task_group_members m JOIN tasks t "
                          "ON t.id=m.original_task WHERE m.group_task=? AND t.project=? ORDER BY t.created,t.id LIMIT 101",
                          (task, project))
    return {"acceptance_evidence": acceptance_evidence(store, task, project),
            "groups": groups[:100], "groups_truncated": len(groups) > 100, "members": members,
            "next_before": groups[99]["cursor"] if len(groups) > 100 else None}
