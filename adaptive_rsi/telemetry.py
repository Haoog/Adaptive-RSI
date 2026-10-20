"""从独立验收记录统计质量，不把模型自报或测试连接算成任务成功。"""
import json
import time


def quality_summary(store):
    projects = {}
    failures = {}
    for run in store.query("SELECT r.project,r.task,r.state,r.result,t.source FROM runs r JOIN tasks t ON t.id=r.task WHERE r.ended IS NOT NULL AND r.started>?", (time.time() - 30 * 86400,)):
        if run["source"].startswith("system_"):
            continue
        item = projects.setdefault(run["project"], {"project": run["project"], "runs": 0, "passed": 0,
                                                    "evaluated": 0, "reviewed": 0, "review_blocked": 0, "repeat_failures": 0})
        item["runs"] += 1
        if run["state"] in ["failed", "blocked", "insufficient_evidence"]:
            key = (run["project"], run["task"])
            failures[key] = failures.get(key, 0) + 1
        result = json.loads(run["result"])
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
