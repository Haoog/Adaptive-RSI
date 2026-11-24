"""从独立验收记录统计质量，不把模型自报或测试连接算成任务成功。"""
import json
import time
from .config import fingerprint
from .store import decode_object, decode_evidence
from .usage_report import one_snapshot


MERGE_STATES = ("verified", "regressed")


def merge_attribution(evidence):
    """Attribute post-merge feedback to the original merge or to the later mainline.

    显式的 merge 子记录优先。没有它时，历史记录只有 head 恰好等于合并提交才
    属于原合并；head 已在主线之后的结果只能算后续主线回归，不能归给原修复。
    """
    feedback = evidence.get("feedback") if isinstance(evidence, dict) else None
    if not isinstance(feedback, dict):
        feedback = {}
    merge = feedback.get("merge")
    if not isinstance(merge, dict):
        merge = {}
    merged_sha = evidence.get("merged_sha") if isinstance(evidence, dict) else None
    original_sha = merge.get("sha") if isinstance(merge.get("sha"), str) else merged_sha
    original = merge.get("state") if merge.get("state") in MERGE_STATES else None
    head = feedback.get("head") if isinstance(feedback.get("head"), str) else None
    if original is None and head and head == merged_sha:
        original = feedback.get("state") if feedback.get("state") in MERGE_STATES else None
    mainline = (feedback.get("state") if feedback.get("state") in MERGE_STATES and head
                and head != original_sha else None)
    return {"postmerge_state": original, "mainline_state": mainline}


@one_snapshot()
def quality_summary(store, project=None):
    projects = {}
    failures = {}
    for run in store.query("SELECT r.project,r.task,r.state,r.result,t.source FROM runs r JOIN tasks t ON t.id=r.task "
                           "WHERE r.ended IS NOT NULL AND r.started>?" + (" AND r.project=?" if project else ""),
                           (time.time() - 30 * 86400, project) if project else (time.time() - 30 * 86400,)):
        if run["source"].startswith("system_"):
            continue
        item = projects.setdefault(run["project"], {"project": run["project"], "runs": 0, "passed": 0,
                                                    "evaluated": 0, "reviewed": 0, "review_blocked": 0, "repeat_failures": 0,
                                                    "postmerge_verified": 0, "postmerge_regressed": 0,
                                                    "mainline_regressed": 0})
        item["runs"] += 1
        if run["state"] in ["failed", "blocked", "insufficient_evidence"]:
            key = (run["project"], run["task"])
            failures[key] = failures.get(key, 0) + 1
        result = decode_evidence(run["result"])
        attribution = merge_attribution(result)
        if attribution["postmerge_state"]:
            item["postmerge_" + attribution["postmerge_state"]] += 1
        if attribution["mainline_state"] == "regressed":
            item["mainline_regressed"] += 1
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
    全部读取共享一个只读事务，响应只能对应某一个已提交快照。
    """
    with store.snapshot():
        return _workflow_metrics(store, project)


def _completion_metrics(store, result, cutoff, now, project):
    """Count each logical task at its first evidenced completion of each kind."""
    terminal = "('awaiting_approval','pr_open','merged','completed')"
    merged_at = "json_extract(CASE WHEN json_valid(r.result) THEN r.result ELSE '{}' END,'$.merged_at')"
    scope = (
        "SELECT id FROM tasks WHERE source NOT LIKE 'system_%' AND id IN ("
        "SELECT r.task FROM events e JOIN runs r ON r.id=e.run "
        "WHERE e.kind IN ('awaiting_approval','pr') AND e.created>? AND e.created<=? "
        "UNION SELECT r.task FROM runs r WHERE r.state IN " + terminal + " AND r.ended>? AND r.ended<=? "
        "UNION SELECT r.task FROM runs r WHERE " + merged_at + ">? AND " + merged_at + "<=?)"
        + (" AND project=?" if project else ""))
    args = (cutoff, now) * 3 + ((project,) if project else ())
    created = {row["id"]: row["created"] for row in store.query(
        "SELECT id,created FROM tasks WHERE id IN (" + scope + ")", args)}
    if not created:
        return
    scope = "SELECT value FROM json_each(?)"
    args = (json.dumps(list(created)),)
    run_scope = "SELECT id FROM runs WHERE task IN (" + scope + ")"
    implemented = {row["run"] for row in store.query(
        "SELECT run FROM phase_times WHERE phase='implementation' AND run IN (" + run_scope + ") "
        "UNION SELECT run FROM calls WHERE role='implementation' AND run IN (" + run_scope + ")", args * 2)}

    def passed(evidence):
        return (evidence.get("evaluation", {}).get("decision") == "pass" and
                evidence.get("review", {}).get("decision") == "pass")

    def record(index, task, at, run):
        if type(at) in (int, float) and created[task] <= at <= now:
            index[task] = min(index.get(task, (at, run)), (at, run))

    accepted, delivered, pr_created, attempts = {}, {}, {}, {}
    acceptance_events = {}
    for row in store.query(
            "SELECT r.task,e.run,e.kind,e.created,e.data FROM events e JOIN runs r ON r.id=e.run "
            "WHERE e.kind IN ('awaiting_approval','pr') AND r.task IN (" + scope + ")", args):
        if row["kind"] == "pr":
            record(pr_created, row["task"], row["created"], row["run"])
        else:
            evidence = decode_evidence(row["data"])
            events = acceptance_events.setdefault(row["run"], [])
            if passed(evidence):
                events.append(row["created"])
    for row in store.query(
            "SELECT id,task,state,started,ended,result FROM runs WHERE task IN (" + scope + ")", args):
        task, run = row["task"], row["id"]
        evidence = decode_evidence(row["result"])
        if run in acceptance_events:
            # A later transition can replace the result with status-only evidence.
            # An explicit invalid or non-passing verdict still takes precedence.
            if passed(evidence) or not any(key in evidence for key in ("evaluation", "review", "evidence_error")):
                for at in acceptance_events[run]:
                    record(accepted, task, at, run)
                    record(attempts, task, row["started"], run)
        elif row["state"] in ("awaiting_approval", "pr_open", "merged", "completed") and passed(evidence):
            # Legacy runs can prove completion with a terminal verdict and run end.
            record(accepted, task, row["ended"], run)
        record(delivered, task, evidence.get("merged_at"), run)
        infrastructure = evidence.get("failure", {}).get("code") in {"provider", "credentials", "environment"}
        if not infrastructure and (run in implemented or evidence.get("evaluation")):
            record(attempts, task, row["started"], run)
    for index in (accepted, delivered, pr_created):
        for task in [task for task, (at, _) in index.items() if at <= cutoff]:
            del index[task]
    result.update(accepted=len(accepted), delivered=len(delivered),
                  elapsed_samples=len(accepted), to_merge_samples=len(delivered), to_pr_samples=len(pr_created),
                  first_pass=sum(attempts.get(task, (None, None))[1] == run for task, (_, run) in accepted.items()))
    for index, key in ((accepted, "elapsed"), (delivered, "to_merge"), (pr_created, "to_pr")):
        result[key + "_seconds"] = sum(at - created[task] for task, (at, _) in index.items())
    for row in store.query(
            "SELECT r.task,c.tokens,c.usage_known,c.seconds,c.created FROM calls c JOIN runs r ON r.id=c.run "
            "WHERE r.task IN (" + scope + ")", args):
        task = row["task"]
        for index, prefix in ((accepted, "accepted"), (delivered, "delivered")):
            if task in index and row["created"] is not None and created[task] <= row["created"] <= index[task][0]:
                result[prefix + "_tokens"] += row["tokens"] or 0
                result[prefix + "_unknown_calls"] += row["usage_known"] == 0
                if prefix == "accepted":
                    result["model_seconds"] += row["seconds"] or 0


def _workflow_metrics(store, project):
    now = time.time()
    cutoff = now - 30 * 86400
    window_args = (cutoff, now, project) if project else (cutoff, now)
    scope = "SELECT id FROM tasks WHERE source NOT LIKE 'system_%' AND created>? AND created<=?" + (" AND project=?" if project else "")
    tasks = store.query("SELECT * FROM tasks WHERE id IN (" + scope + ")", window_args)
    result = {"window_days": 30, "tasks": len(tasks), "accepted": 0, "delivered": 0,
              "first_pass": 0, "infrastructure_failures": 0, "repeated_failures": 0,
              "requirements_revised": 0, "elapsed_samples": 0, "elapsed_seconds": 0,
              "queue_seconds": 0, "queue_samples": 0, "phase_seconds": {}, "model_seconds": 0,
              "accepted_tokens": 0, "accepted_unknown_calls": 0, "stage_reused": 0,
              "delivered_tokens": 0, "delivered_unknown_calls": 0, "remote_wait_seconds": {}, "remote_wait_samples": 0, "remote_wait_samples_by_reason": {},
              "to_pr_seconds": 0, "to_pr_samples": 0, "to_merge_seconds": 0, "to_merge_samples": 0,
              "interventions": 0, "stale_rework": 0, "invalidations": {},
              "postmerge_verified": 0, "postmerge_regressed": 0, "mainline_regressed": 0,
              "value_feedback": {"helpful": 0, "unnecessary": 0, "false_positive": 0},
              "rated_runs": 0, "unrated_runs": 0}
    runs_by_task = {}
    for run in store.query("SELECT * FROM runs WHERE task IN (" + scope + ") ORDER BY task,started IS NULL,started,id", window_args):
        runs_by_task.setdefault(run["task"], []).append(run)
    run_scope = "SELECT id FROM runs WHERE task IN (" + scope + ")"
    ratings = {}
    for row in store.query("SELECT substr(key,16) AS run,value FROM settings WHERE key>='value-feedback:' "
                           "AND key<'value-feedback;' AND substr(key,16) IN (" + run_scope + ")", window_args):
        value = decode_object(row["value"])
        if value.get("rating") in result["value_feedback"]:
            ratings[row["run"]] = value["rating"]
    revised = {row["task"] for row in store.query(
        "SELECT DISTINCT task FROM task_revisions WHERE task IN (" + scope + ")", window_args)}
    interventions = {row["task"]: row["n"] for row in store.query(
        "SELECT json_extract(CASE WHEN json_valid(data) THEN data ELSE '{}' END,'$.task') AS task,count(*) AS n FROM events WHERE "
        "created>? AND created<=? AND kind IN ('manual_retry','requirements_revised','queue_settings','cancelled','delivery_approved')" +
        (" AND project=?" if project else "") + " GROUP BY 1", window_args)}
    phase_rows = store.query(
        "SELECT run,phase,coalesce(duration,max(0,ended-started)) AS seconds FROM phase_times WHERE ended IS NOT NULL AND run IN (" + run_scope + ")", window_args)
    wait_rows = store.query(
        "SELECT w.reason,count(*) AS samples,sum(coalesce(w.duration,w.ended-w.started)) AS seconds "
        "FROM remote_waits w JOIN runs r ON r.id=w.run JOIN tasks t ON t.id=r.task "
        "WHERE w.ended>? AND w.ended<=? AND w.started<=w.ended AND t.source NOT LIKE 'system_%'"
        + (" AND t.project=?" if project else "") + " GROUP BY w.reason", window_args)
    for task in tasks:
        runs = runs_by_task.get(task["id"], [])
        failure_signatures = []
        for run in runs:
            evidence = decode_evidence(run["result"])
            if run["ended"] is not None:
                rating = ratings.get(run["id"])
                if rating:
                    result["rated_runs"] += 1
                    result["value_feedback"][rating] += 1
                else:
                    result["unrated_runs"] += 1
                attribution = merge_attribution(evidence)
                if attribution["postmerge_state"]:
                    result["postmerge_" + attribution["postmerge_state"]] += 1
                result["mainline_regressed"] += attribution["mainline_state"] == "regressed"
            infrastructure = evidence.get("failure", {}).get("code") in ["provider", "credentials", "environment"]
            result["infrastructure_failures"] += infrastructure
            if run["state"] in ["failed", "insufficient_evidence"]:
                checks = evidence.get("evaluation", {}).get("checks", {})
                failure_signatures.append(fingerprint({"code": evidence.get("failure", {}).get("code"),
                    "checks": [(stage, check.get("name"), check.get("exit_code")) for stage in ["candidate", "candidate_full", "acceptance_candidate"] for check in checks.get(stage, []) if not check.get("passed")],
                    "findings": [(f.get("path"), f.get("title")) for f in evidence.get("review", {}).get("findings", [])]}))
            if run["state"] == "superseded" or evidence.get("failure", {}).get("code") == "baseline":
                result["stale_rework"] += 1
        result["repeated_failures"] += len(failure_signatures) > len(set(failure_signatures))
        result["interventions"] += interventions.get(task["id"], 0)
        result["requirements_revised"] += task["id"] in revised
        if runs and runs[0]["started"] is not None and task["created"] <= runs[0]["started"] <= now:
            result["queue_samples"] += 1
            result["queue_seconds"] += runs[0]["started"] - task["created"]
    _completion_metrics(store, result, cutoff, now, project)
    for phase in phase_rows:
        name = phase["phase"]
        result["phase_seconds"][name] = result["phase_seconds"].get(name, 0) + max(0, phase["seconds"])
    for wait in wait_rows:
        name = wait["reason"]
        result["remote_wait_samples"] += wait["samples"]
        result["remote_wait_samples_by_reason"][name] = wait["samples"]
        result["remote_wait_seconds"][name] = wait["seconds"]
    events = store.query("SELECT kind,count(*) AS n FROM events WHERE created>? AND created<=?" + (" AND project=?" if project else "") + " GROUP BY kind",
                         window_args)
    result["events"] = {row["kind"]: row["n"] for row in events}
    result["stage_reused"] = result["events"].get("stage_reused", 0)
    result["invalidations"] = {kind: result["events"].get(kind, 0) for kind in ["stage_invalidated", "requirements_revised", "triage_recheck", "issue_revision"]}
    return result
