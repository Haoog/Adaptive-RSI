"""从独立验收记录统计质量，不把模型自报或测试连接算成任务成功。"""
import json
import time
from .config import fingerprint


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
    """Logical task outcomes; elapsed spans and model call durations remain separate.

    派生数据各用一条聚合查询取回，避免逐任务 N+1 查询；输出结构与逐任务版本一致。
    """
    cutoff = time.time() - 30 * 86400
    scope = "SELECT id FROM tasks WHERE source NOT LIKE 'system_%' AND created>?" + (" AND project=?" if project else "")
    scope_args = (cutoff, project) if project else (cutoff,)
    tasks = store.query("SELECT * FROM tasks WHERE id IN (" + scope + ")", scope_args)
    result = {"window_days": 30, "tasks": len(tasks), "accepted": 0, "delivered": 0,
              "first_pass": 0, "infrastructure_failures": 0, "repeated_failures": 0,
              "requirements_revised": 0, "elapsed_samples": 0, "elapsed_seconds": 0,
              "queue_seconds": 0, "phase_seconds": {}, "model_seconds": 0,
              "accepted_tokens": 0, "accepted_unknown_calls": 0, "stage_reused": 0,
              "delivered_tokens": 0, "delivered_unknown_calls": 0, "remote_wait_seconds": {}, "remote_wait_samples": 0,
              "to_pr_seconds": 0, "to_pr_samples": 0, "to_merge_seconds": 0, "to_merge_samples": 0,
              "interventions": 0, "stale_rework": 0, "invalidations": {}}
    runs_by_task = {}
    for run in store.query("SELECT * FROM runs WHERE task IN (" + scope + ") ORDER BY task,started,id", scope_args):
        runs_by_task.setdefault(run["task"], []).append(run)
    run_scope = "SELECT id FROM runs WHERE task IN (" + scope + ")"
    implemented = {row["run"] for row in store.query(
        "SELECT DISTINCT run FROM phase_times WHERE phase='implementation' AND run IN (" + run_scope + ")", scope_args)}
    implemented |= {row["run"] for row in store.query(
        "SELECT DISTINCT run FROM calls WHERE role='implementation' AND run IN (" + run_scope + ")", scope_args)}
    pr_created_at = {row["run"]: row["at"] for row in store.query(
        "SELECT run,min(created) AS at FROM events WHERE kind='pr' AND run IN (" + run_scope + ") GROUP BY run", scope_args)}
    usage_by_task = {row["task"]: row for row in store.query(
        "SELECT r.task AS task,sum(c.tokens) AS tokens,sum(c.usage_known=0) AS unknown,sum(c.seconds) AS seconds "
        "FROM calls c JOIN runs r ON r.id=c.run WHERE r.task IN (" + scope + ") GROUP BY r.task", scope_args)}
    revised = {row["task"] for row in store.query(
        "SELECT DISTINCT task FROM task_revisions WHERE task IN (" + scope + ")", scope_args)}
    interventions = {row["task"]: row["n"] for row in store.query(
        "SELECT json_extract(data,'$.task') AS task,count(*) AS n FROM events WHERE "
        "kind IN ('manual_retry','requirements_revised','queue_settings','cancelled','delivery_approved') GROUP BY 1")}
    phase_rows = store.query(
        "SELECT run,phase,ended-started AS seconds FROM phase_times WHERE ended IS NOT NULL AND run IN (" + run_scope + ")", scope_args)
    wait_rows = store.query(
        "SELECT run,reason,ended-started AS seconds FROM remote_waits WHERE ended IS NOT NULL AND run IN (" + run_scope + ")", scope_args)
    for task in tasks:
        runs = runs_by_task.get(task["id"], [])
        code_attempts = []
        accepted = None
        failure_signatures = []
        merged_at = []
        pr_created = []
        for run in runs:
            evidence = json.loads(run["result"])
            infrastructure = evidence.get("failure", {}).get("code") in ["provider", "credentials", "environment"]
            result["infrastructure_failures"] += infrastructure
            if not infrastructure and (run["id"] in implemented or evidence.get("evaluation")):
                code_attempts.append(run)
            if evidence.get("evaluation", {}).get("decision") == "pass" and evidence.get("review", {}).get("decision") == "pass":
                accepted = accepted or run
            if run["state"] in ["failed", "insufficient_evidence"]:
                checks = evidence.get("evaluation", {}).get("checks", {})
                failure_signatures.append(fingerprint({"code": evidence.get("failure", {}).get("code"),
                    "checks": [(stage, check.get("name"), check.get("exit_code")) for stage in ["candidate", "candidate_full", "acceptance_candidate"] for check in checks.get(stage, []) if not check.get("passed")],
                    "findings": [(f.get("path"), f.get("title")) for f in evidence.get("review", {}).get("findings", [])]}))
            if evidence.get("merged_at"):
                merged_at.append(evidence["merged_at"])
            if pr_created_at.get(run["id"]):
                pr_created.append(pr_created_at[run["id"]])
            if run["state"] == "superseded" or evidence.get("failure", {}).get("code") == "baseline":
                result["stale_rework"] += 1
        result["repeated_failures"] += len(failure_signatures) > len(set(failure_signatures))
        if pr_created:
            result["to_pr_samples"] += 1
            result["to_pr_seconds"] += max(0, min(pr_created) - task["created"])
        if merged_at:
            result["to_merge_samples"] += 1
            result["to_merge_seconds"] += max(0, min(merged_at) - task["created"])
        result["interventions"] += interventions.get(task["id"], 0)
        result["requirements_revised"] += task["id"] in revised
        if runs:
            result["queue_seconds"] += max(0, runs[0]["started"] - task["created"])
        result["delivered"] += task["state"] == "merged"
        if accepted:
            result["accepted"] += 1
            result["first_pass"] += bool(code_attempts and code_attempts[0]["id"] == accepted["id"])
            if accepted["ended"]:
                result["elapsed_samples"] += 1
                result["elapsed_seconds"] += max(0, accepted["ended"] - task["created"])
            usage = usage_by_task.get(task["id"], {})
            result["accepted_tokens"] += usage.get("tokens") or 0
            result["accepted_unknown_calls"] += usage.get("unknown") or 0
            result["model_seconds"] += usage.get("seconds") or 0
            if task["state"] == "merged":
                result["delivered_tokens"] += usage.get("tokens") or 0
                result["delivered_unknown_calls"] += usage.get("unknown") or 0
    for phase in phase_rows:
        name = phase["phase"]
        result["phase_seconds"][name] = result["phase_seconds"].get(name, 0) + max(0, phase["seconds"])
    for wait in wait_rows:
        name = wait["reason"]
        result["remote_wait_samples"] += 1
        result["remote_wait_seconds"][name] = result["remote_wait_seconds"].get(name, 0) + max(0, wait["seconds"])
    events = store.query("SELECT kind,count(*) AS n FROM events WHERE created>?" + (" AND project=?" if project else "") + " GROUP BY kind",
                         (cutoff, project) if project else (cutoff,))
    result["events"] = {row["kind"]: row["n"] for row in events}
    result["stage_reused"] = result["events"].get("stage_reused", 0)
    result["invalidations"] = {kind: result["events"].get(kind, 0) for kind in ["stage_invalidated", "requirements_revised", "triage_recheck", "issue_revision"]}
    return result
