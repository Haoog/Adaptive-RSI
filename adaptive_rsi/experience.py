"""Bounded retry evidence and persistent workflow decisions."""
import json
import re
import time


def safe_excerpt(value, limit=1200):
    text = str(value or "")
    text = re.sub(r"https?://\S+", "<URL redacted>", text)
    text = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted>", text)
    return text[:limit]


def retry_feedback(row):
    result = json.loads(row["result"])
    feedback = {key: row.get(key) for key in ["id", "base", "state"]}
    for key in ["reason", "summary", "patch_hash"]:
        if key in result:
            feedback[key] = safe_excerpt(result[key])
    if result.get("failure"):
        feedback["failure"] = {k: safe_excerpt(result["failure"].get(k)) for k in ["code", "action", "detail"]}
    feedback["review_findings"] = [{k: safe_excerpt(finding.get(k), 700) for k in
                                  ["severity", "path", "line", "title", "reason", "suggestion"]}
                                 for finding in result.get("review", {}).get("findings", [])[:8]
                                 if not str(finding.get("path", "")).startswith(".rsi-acceptance")]
    # Sealed acceptance files, commands, logs and protected samples never enter retry prompts.
    checks = result.get("evaluation", {}).get("checks", {})
    feedback["failed_regression_checks"] = [{"name": safe_excerpt(c.get("name"), 200),
                                             "exit_code": c.get("exit_code"),
                                             "stderr": safe_excerpt(c.get("stderr"), 1000)}
                                            for c in checks.get("candidate", []) if not c.get("passed")][:5]
    return feedback


def queue_status(store, project):
    now = time.time()
    paused = store.one("SELECT paused FROM projects WHERE id=?", (project,))
    busy = store.one("SELECT id FROM runs WHERE project=? AND ended IS NULL", (project,))
    rows = store.query("SELECT * FROM tasks WHERE project=? AND state='queued' ORDER BY " + store.queue_order(), (project, now))
    ready = 0
    for row in rows:
        dependency = store.one("SELECT p.id,p.state,p.title FROM task_dependencies d JOIN tasks p ON p.id=d.dependency WHERE d.task=?", (row["id"],))
        reason = "dependency" if dependency and dependency["state"] != "merged" else "backoff" if row["available"] > now else "paused" if paused and paused["paused"] else "running" if busy else "ready"
        if reason in ["running", "ready"]:
            ready += 1
        row.update(wait_reason=reason, queue_position=ready if reason in ["running", "ready"] else None,
                   dependency=dependency)
    return rows
