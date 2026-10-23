"""从独立验收记录统计质量，不把模型自报或测试连接算成任务成功。"""
import json
import time


def quality_summary(store, project=None):
    projects = {}
    failures = {}
    for run in store.query("SELECT r.project,r.task,r.state,r.result,t.source FROM runs r JOIN tasks t ON t.id=r.task "
                           "WHERE r.ended IS NOT NULL AND r.started>?" + (" AND r.project=?" if project else ""),
                           (time.time() - 30 * 86400, project) if project else (time.time() - 30 * 86400,)):
        if run["source"].startswith("system_"):
            continue
        item = projects.setdefault(run["project"], {"project": run["project"], "runs": 0, "passed": 0,
                                                    "evaluated": 0, "reviewed": 0, "review_blocked": 0, "repeat_failures": 0, "postmerge_verified": 0, "postmerge_regressed": 0})
        item["runs"] += 1
        if run["state"] in ["failed", "blocked", "insufficient_evidence"]:
            key = (run["project"], run["task"])
            failures[key] = failures.get(key, 0) + 1
        result = json.loads(run["result"])
        feedback = result.get("feedback", {}).get("state")
        if feedback in ["verified", "regressed"]:
            item["postmerge_" + feedback] += 1
        if result.get("evaluation"):
            item["evaluated"] += 1
        if result.get("evaluation", {}).get("decision") == "pass" and result.get("review", {}).get("decision") == "pass":
            item["passed"] += 1
        if result.get("review"):
            item["reviewed"] += 1
            item["review_blocked"] += result["review"].get("decision") != "pass"
    for (project, _), count in failures.items():
        if count > 1:
            projects[project]["repeat_failures"] += 1
    return list(projects.values())


def workflow_metrics(store, project=None):
    """Logical task outcomes; elapsed spans and model call durations remain separate."""
    cutoff = time.time() - 30 * 86400
    tasks = store.query("SELECT * FROM tasks WHERE source NOT LIKE 'system_%' AND created>?" +
                        (" AND project=?" if project else ""), (cutoff, project) if project else (cutoff,))
    result = {"window_days": 30, "tasks": len(tasks), "accepted": 0, "delivered": 0,
              "first_pass": 0, "infrastructure_failures": 0, "repeated_failures": 0,
              "requirements_revised": 0, "elapsed_samples": 0, "elapsed_seconds": 0,
              "queue_seconds": 0, "phase_seconds": {}, "model_seconds": 0,
              "accepted_tokens": 0, "accepted_unknown_calls": 0, "stage_reused": 0,
              "delivered_tokens": 0, "delivered_unknown_calls": 0, "remote_wait_seconds": {}, "remote_wait_samples": 0}
    for task in tasks:
        runs = store.query("SELECT * FROM runs WHERE task=? ORDER BY started,id", (task["id"],))
        code_attempts = []
        accepted = None
        failures = 0
        for run in runs:
            evidence = json.loads(run["result"])
            infrastructure = evidence.get("failure", {}).get("code") in ["provider", "credentials", "environment"]
            result["infrastructure_failures"] += infrastructure
            if not infrastructure:
                code_attempts.append(run)
            if evidence.get("evaluation", {}).get("decision") == "pass" and evidence.get("review", {}).get("decision") == "pass":
                accepted = accepted or run
            failures += run["state"] in ["failed", "insufficient_evidence"]
            for phase in store.query("SELECT phase,started,ended FROM phase_times WHERE run=? AND ended IS NOT NULL", (run["id"],)):
                name = phase["phase"]
                result["phase_seconds"][name] = result["phase_seconds"].get(name, 0) + max(0, phase["ended"] - phase["started"])
            for wait in store.query("SELECT * FROM remote_waits WHERE run=? AND ended IS NOT NULL", (run["id"],)):
                name = wait["reason"]
                result["remote_wait_samples"] += 1
                result["remote_wait_seconds"][name] = result["remote_wait_seconds"].get(name, 0) + max(0, wait["ended"] - wait["started"])
        result["repeated_failures"] += failures > 1
        result["requirements_revised"] += bool(store.one("SELECT 1 FROM task_revisions WHERE task=? LIMIT 1", (task["id"],)))
        if runs:
            result["queue_seconds"] += max(0, runs[0]["started"] - task["created"])
        result["delivered"] += task["state"] == "merged"
        if accepted:
            result["accepted"] += 1
            result["first_pass"] += bool(code_attempts and code_attempts[0]["id"] == accepted["id"])
            if accepted["ended"]:
                result["elapsed_samples"] += 1
                result["elapsed_seconds"] += max(0, accepted["ended"] - task["created"])
            usage = store.one("SELECT sum(c.tokens) AS tokens,sum(c.usage_known=0) AS unknown,sum(c.seconds) AS seconds FROM calls c JOIN runs r ON r.id=c.run WHERE r.task=?", (task["id"],))
            result["accepted_tokens"] += usage["tokens"] or 0
            result["accepted_unknown_calls"] += usage["unknown"] or 0
            result["model_seconds"] += usage["seconds"] or 0
            if task["state"] == "merged":
                result["delivered_tokens"] += usage["tokens"] or 0
                result["delivered_unknown_calls"] += usage["unknown"] or 0
    events = store.query("SELECT kind,count(*) AS n FROM events WHERE created>?" + (" AND project=?" if project else "") + " GROUP BY kind",
                         (cutoff, project) if project else (cutoff,))
    result["events"] = {row["kind"]: row["n"] for row in events}
    result["stage_reused"] = result["events"].get("stage_reused", 0)
    return result
