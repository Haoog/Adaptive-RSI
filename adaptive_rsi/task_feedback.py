"""User observations and summaries derived from existing task evidence."""
import json
import re
import time

from .store import decode_evidence, decode_object, encode
from .telemetry import merge_attribution
from .usage_report import one_snapshot

RATINGS = {"helpful", "unnecessary", "false_positive"}


@one_snapshot()
def dashboard_usage(store):
    """Use the detailed report's pricing rules for the existing 24-hour cards."""
    from .usage_report import TOTALS
    base = "SELECT "
    aliases = ",sum(c.seconds) AS seconds,sum(c.state='failed') AS failed"
    source = " FROM calls c WHERE c.created>? GROUP BY "
    cutoff = time.time() - 86400
    result = {}
    for key, group in (("usage", "c.project"), ("model_usage", "c.project,c.provider,c.model,c.unit")):
        rows = store.query(base + group + "," + TOTALS + aliases + source + group, (cutoff,))
        for row in rows:
            row.update(estimate=row.pop("estimated_usd"), unpriced=row.pop("unpriced_calls"),
                       unknown_usage=row.pop("unknown_calls"))
        result[key] = rows
    return result


def read(store, run):
    value = store.setting("value-feedback:" + run)
    if not isinstance(value, dict) or value.get("rating") not in RATINGS:
        return None
    return value


def save(store, project, task, run, rating, note=""):
    if not all(isinstance(item, str) and item for item in (project, task, run)):
        raise ValueError("Project, task and run are required")
    if rating is not None and (not isinstance(rating, str) or rating not in RATINGS):
        raise ValueError("Invalid value feedback rating")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("Feedback note must be at most 2000 characters")
    now = time.time()
    value = {"project": project, "task": task, "run": run, "rating": rating,
             "note": note, "updated": now} if rating else None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        owner = db.execute("SELECT r.ended FROM runs r JOIN tasks t ON t.id=r.task "
                           "WHERE r.id=? AND r.task=? AND r.project=? AND t.project=?",
                           (run, task, project, project)).fetchone()
        if not owner:
            raise ValueError("Run not found in this task and project")
        if owner["ended"] is None:
            raise ValueError("Feedback requires a finished run")
        key = "value-feedback:" + run
        if value is None:
            db.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, encode(value)))
        db.execute("INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)",
                   (project, run, "value_feedback", "User value feedback updated", encode(value or {
                       "task": task, "rating": None}), now))
    return value


def summary(store, run):
    with store.connect() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if not row:
            raise ValueError("Run not found")
        result = decode_evidence(row["result"])
        counts = db.execute("SELECT count(*) AS attempts FROM runs WHERE task=?", (row["task"],)).fetchone()
        usage = db.execute("SELECT count(*) AS calls,coalesce(sum(tokens),0) AS tokens,"
                           "coalesce(sum(seconds),0) AS seconds,coalesce(sum(usage_known=0),0) AS unknown_calls "
                           "FROM calls WHERE run=?", (run,)).fetchone()
        reused = db.execute("SELECT data FROM events WHERE run=? AND kind='stage_reused' ORDER BY seq", (run,)).fetchall()
        interventions = db.execute("SELECT count(*) AS n FROM events WHERE (run=? OR "
                                   "json_extract(CASE WHEN json_valid(data) THEN data ELSE '{}' END,'$.task')=?) AND kind IN "
                                   "('manual_retry','requirements_revised','queue_settings','cancelled','delivery_approved')", (run, row["task"])).fetchone()
        saved_feedback = db.execute("SELECT value FROM settings WHERE key=?", ("value-feedback:" + run,)).fetchone()
    checks = result.get("evaluation", {}).get("checks", {})
    check_totals = {key: {"passed": sum(item.get("passed") is True for item in rows if isinstance(item, dict)),
                          "total": len(rows)} for key, rows in checks.items() if isinstance(rows, list)}
    sha = result.get("merged_sha")
    attribution = merge_attribution(result)
    value = decode_object(saved_feedback["value"]) if saved_feedback else None
    if not isinstance(value, dict) or value.get("rating") not in RATINGS:
        value = None
    return {"run": run, "task": row["task"], "project": row["project"],
            "summary": result.get("summary") or result.get("reason"), "files": result.get("files", []),
            "checks": check_totals, "usage": dict(usage), "attempts": counts["attempts"],
            "reused_stages": sorted({data["stage"] for item in reused
                                      if isinstance((data := decode_object(item["data"])).get("stage"), str)}),
            "delivery_sha": sha if isinstance(sha, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) else None,
            "postmerge_state": attribution["postmerge_state"],
            "mainline_state": attribution["mainline_state"],
            "interventions": interventions["n"], "value_feedback": value}
