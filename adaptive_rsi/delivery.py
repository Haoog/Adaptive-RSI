import json
import time
from pathlib import Path

from .config import merge
from .gitops import git, origin_repository, snapshot
from .process import Cancelled, checked
from .store import encode


class StaleCandidate(RuntimeError):
    pass


def github_git(root, *args):
    return git(root, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args)


def baseline(runtime, project, config):
    repo = config["github"]["repository"]
    if not repo:
        return project["path"], git(project["path"], "rev-parse", "HEAD"), ""
    if repo.lower() != origin_repository(project["path"]).lower():
        raise RuntimeError("Baseline repository must match project origin")
    branch = config["delivery"]["base_branch"] or checked(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"])
    with runtime.baseline_lock:
        cache = runtime.store.root / "remotes" / project["id"]
        if not cache.exists():
            checked(["git", "init", "--bare", "--quiet", str(cache)])
        github_git(cache, "fetch", "--quiet", "--no-tags", "https://github.com/" + repo + ".git",
            "+refs/heads/" + branch + ":refs/heads/rsi-baseline")
        sha = git(cache, "rev-parse", "refs/heads/rsi-baseline")
        runtime.store.put_setting("baseline:" + project["id"], {"sha": sha, "branch": branch})
        return str(cache), sha, branch


def pr_view(repo, url):
    return json.loads(checked(["gh", "pr", "view", url, "--repo", repo, "--json",
        "url,state,headRefOid,baseRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,mergeCommit,isDraft"]))


def merge_gate(pr, head, base):
    if pr["headRefOid"] != head or pr["baseRefOid"] != base:
        return "stale"
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "waiting_ci"
    statuses = {check.get("conclusion") or check.get("state") or check.get("status") for check in checks}
    if statuses & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}:
        return "ci_failed"
    if statuses - {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "waiting_ci"
    if not any((c.get("conclusion") or c.get("state")) == "SUCCESS" for c in checks):
        return "waiting_ci"
    if pr.get("reviewDecision") not in ["", "APPROVED", None]:
        return "waiting_review"
    if pr.get("isDraft") or pr.get("mergeable") != "MERGEABLE" or pr.get("mergeStateStatus") != "CLEAN":
        return "waiting"
    return "ready"


def publish(runtime, project, task, run, workspace, config):
    store = runtime.store
    approval = store.one("SELECT id,evidence FROM tasks WHERE project=? AND source='system_publish' AND body=? AND state='planning'",
                         (project["id"], run["id"]))
    if not approval or run["state"] != "awaiting_approval":
        raise RuntimeError("Publishing requires explicit approval or persisted project policy")
    authorization = json.loads(approval["evidence"])
    def authorize_write():
        live = runtime.config(project["id"])
        job = store.one("SELECT * FROM runs WHERE task=? AND ended IS NULL", (approval["id"],))
        pending = store.one("SELECT state FROM tasks WHERE id=?", (approval["id"],))
        if (runtime.stop.is_set() or runtime.project(project["id"])["paused"] or
                not pending or pending["state"] != "planning" or (job and runtime.is_cancelled(job))):
            raise Cancelled("Publishing cancelled or project paused")
        if not authorization or authorization[0].get("authorization") not in ["project_policy", "per_candidate"]:
            raise RuntimeError("Publishing authorization is missing")
        if authorization[0]["authorization"] == "project_policy" and (
                json.loads(run["config"])["delivery"]["mode"] != "pr" or live["delivery"]["mode"] != "pr"):
            raise RuntimeError("Automatic PR authorization was revoked")
        if (live["github"]["repository"] != config["github"]["repository"] or
                live["delivery"]["base_branch"] != config["delivery"]["base_branch"]):
            raise StaleCandidate("Delivery target changed before publishing")
    authorize_write()
    if task["source"] == "issue" and not runtime.issue_still_open(project, task, config):
        raise StaleCandidate("Issue closed before delivery; synchronize before retrying")
    outcome = json.loads(run["result"])
    if outcome.get("evaluation", {}).get("decision") != "pass" or outcome.get("review", {}).get("decision") != "pass":
        raise RuntimeError("Delivery gates did not pass")
    repo = config["github"]["repository"]
    if not repo or repo.lower() != origin_repository(project["path"]).lower():
        raise RuntimeError("PR repository must match project origin")
    _, current, base_branch = baseline(runtime, project, config)
    identity = store.setting("run-baseline:" + run["id"])
    expected = {"repository": repo, "branch": base_branch, "sha": current}
    if current != run["base"] or identity != expected:
        raise StaleCandidate("Remote baseline changed; retry and re-evaluate before publishing")
    prefix = "fix" if task["source"] in ["issue", "rollback", "inspection"] else "feat"
    branch = prefix + "/rsi-" + run["id"]
    if git(workspace, "branch", "--show-current") != branch:
        git(workspace, "checkout", "-b", branch)
    if git(workspace, "diff", "--cached", "--name-only"):
        git(workspace, "-c", "user.name=Adaptive RSI", "-c", "user.email=adaptive-rsi@localhost",
            "commit", "-m", "完成任务：" + task["title"])
    sha = git(workspace, "rev-parse", "HEAD")
    existing = json.loads(checked(["gh", "pr", "list", "--repo", repo, "--state", "all", "--head", branch, "--json", "url"]))
    if existing:
        url = existing[0]["url"]
        existing_pr = pr_view(repo, url)
        if existing_pr["headRefOid"] != sha or existing_pr.get("baseRefName", base_branch) != base_branch:
            raise RuntimeError("Existing PR head differs from evaluated candidate")
    else:
        git(workspace, "remote", "set-url", "origin", "https://github.com/" + repo + ".git")
        authorize_write()
        github_git(workspace, "push", "origin", "HEAD:refs/heads/" + branch)
        body = Path(workspace).parent / "pr-body.md"
        body.write_text("完成任务：" + task["title"] + "\n\n价值评估、原始回归、候选完整测试、独立验收和代码评审已通过。\n\n"
                        "运行：`" + run["id"] + "`\n基线：`" + run["base"] + "`\n", encoding="utf-8")
        authorize_write()
        url = checked(["gh", "pr", "create", "--repo", repo, "--head", branch, "--base", base_branch,
                       "--title", "完成任务：" + task["title"], "--body-file", str(body)])
    store.put_setting("delivery:" + run["id"], {"head": sha, "base": run["base"], "branch": base_branch,
                                               "repository": repo})
    store.event("pr", "PR created", project["id"], run["id"], {"url": url, "head": sha})
    return url


def monitoring_due(outcome, config, now):
    if outcome.get("feedback", {}).get("investigation_pending"):
        return True
    if outcome.get("monitoring_completed"):
        return False
    return (now - outcome.get("merged_at", now) > config["limits"]["retention_days"] * 86400 or
            now - outcome.get("feedback_polled_at", outcome.get("feedback", {}).get("checked_at", 0)) >=
            config["delivery"]["feedback_minutes"] * 60)


def schedule(runtime, now=None):
    now = now or time.time()
    store = runtime.store
    if now - store.setting("delivery-scan", 0) < 60:
        return
    store.put_setting("delivery-scan", now)
    projects = set()
    from .release import pending
    for run in store.query("SELECT r.* FROM runs r JOIN projects p ON p.id=r.project "
                           "WHERE p.paused=0 AND r.state IN ('pr_open','merged')"):
        with runtime.scheduler_scope("delivery_run", run["project"], run["id"]):
            if (run["state"] == "pr_open" or pending(runtime, run) or
                    monitoring_due(json.loads(run["result"]), runtime.config(run["project"]), now)):
                projects.add(run["project"])
    for project_id in projects:
        with runtime.scheduler_scope("delivery_project", project_id):
            if now - store.setting("delivery-scheduled:" + project_id, 0) < 60:
                continue
            key = "system:delivery"
            task = store.one("SELECT id,state FROM tasks WHERE project=? AND fingerprint=?", (project_id, key))
            if task and task["state"] in ["queued", "planning"]:
                continue
            if task:
                store.execute("UPDATE tasks SET state='queued',available=0,updated=? WHERE id=?", (now, task["id"]))
            else:
                store.add_task(project_id, "system_delivery", key, "PR tracking and post-merge verification", priority=110)
            store.put_setting("delivery-scheduled:" + project_id, now)


def tick(runtime, now=None, project_id=None, active_run=None):
    now = now or time.time()
    store = runtime.store
    tick_key = "delivery-tick" + (":" + project_id if project_id else "")
    if now - store.setting(tick_key, 0) < 60:
        return
    store.put_setting(tick_key, now)

    def tracking_cancelled():
        if runtime.stop.is_set():
            return True
        if not active_run:
            return False
        active_id = active_run["id"] if isinstance(active_run, dict) else active_run
        current = store.one("SELECT * FROM runs WHERE id=?", (active_id,))
        return not current or runtime.is_cancelled(current)

    for run in store.query("SELECT * FROM runs WHERE state IN ('pr_open','merged') ORDER BY started"):
        if tracking_cancelled():
            return
        if project_id and run["project"] != project_id:
            continue
        project = runtime.project(run["project"])
        if project["paused"] or store.one("SELECT 1 FROM runs WHERE project=? AND ended IS NULL AND id<>?", (project["id"], active_run or "")):
            continue
        try:
            config = runtime.config(project["id"])
            outcome = json.loads(run["result"])
            repo = config["github"]["repository"]
            policy = store.setting("delivery:" + run["id"])
            if not policy or not repo or repo.lower() != origin_repository(project["path"]).lower():
                continue
            identity = store.setting("run-baseline:" + run["id"])
            sealed_target = identity == {"repository": repo, "branch": policy["branch"], "sha": policy["base"]}
            if run["state"] == "pr_open":
                pr = pr_view(repo, outcome["pr"])
                gate = merge_gate(pr, policy["head"], policy["base"])
                if pr.get("baseRefName", policy["branch"]) != policy["branch"] or (
                        config["delivery"]["base_branch"] and config["delivery"]["base_branch"] != policy["branch"]):
                    gate = "stale"
                if pr["state"] == "MERGED":
                    if (not sealed_target or pr["headRefOid"] != policy["head"] or
                            pr.get("baseRefName", policy["branch"]) != policy["branch"]):
                        outcome["delivery_status"] = "unverified_merge"
                        store.execute("UPDATE runs SET state='unverified_merge',result=? WHERE id=?", (encode(outcome), run["id"]))
                        store.execute("UPDATE tasks SET state='blocked',updated=? WHERE id=?", (now, run["task"]))
                    else:
                        outcome["merged_sha"] = pr["mergeCommit"]["oid"]
                        outcome["merged_at"] = now
                        outcome["delivery_status"] = "merged"
                        store.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (encode(outcome), run["id"]))
                        store.execute("UPDATE tasks SET state='merged',updated=? WHERE id=?", (now, run["task"]))
                        task = store.one("SELECT source,evidence FROM tasks WHERE id=?", (run["task"],))
                        if task["source"] == "rollback":
                            original_id = json.loads(task["evidence"])[0]["run"]
                            original = store.one("SELECT result,task FROM runs WHERE id=? AND project=?", (original_id, project["id"]))
                            if original:
                                result = json.loads(original["result"])
                                result["reverted_by"] = run["id"]
                                result["feedback"] = {**result.get("feedback", {}), "state": "reverted"}
                                store.execute("UPDATE runs SET state='reverted',result=? WHERE id=?", (encode(result), original_id))
                                store.execute("UPDATE tasks SET state='reverted',updated=? WHERE id=?", (now, original["task"]))
                elif pr["state"] == "CLOSED":
                    outcome["delivery_status"] = "closed"
                    store.execute("UPDATE runs SET state='pr_closed' WHERE id=?", (run["id"],))
                    store.execute("UPDATE tasks SET state='pr_closed',updated=? WHERE id=?", (now, run["task"]))
                else:
                    outcome["delivery_status"] = gate
                    # 同时要求运行时及当前项目授权，撤销授权立即阻止合并。
                    authorized = json.loads(run["config"])["delivery"]["auto_merge"] and config["delivery"]["auto_merge"]
                    authorized = authorized and sealed_target
                    if gate == "ready" and authorized and not runtime.project(project["id"])["paused"]:
                        _, remote, branch = baseline(runtime, project, config)
                        if remote == policy["base"] and branch == policy["branch"]:
                            task = store.one("SELECT * FROM tasks WHERE id=?", (run["task"],))
                            if task and task["source"] == "issue":
                                try:
                                    issue_current = runtime.issue_still_open(project, task, config)
                                except StaleCandidate as exc:
                                    issue_current = False
                                    outcome["issue_stale_reason"] = str(exc)
                                if not issue_current:
                                    if tracking_cancelled():
                                        return
                                    if not outcome.get("issue_stale_reason"):
                                        outcome["issue_stale_reason"] = "Issue is closed or no longer matches configured labels"
                                    outcome["delivery_status"] = "issue_changed"
                                    outcome["replanned"] = True
                                    store.execute("UPDATE runs SET state='superseded',result=? WHERE id=?", (encode(outcome), run["id"]))
                                    # Issue sync will refresh evidence and requeue this task if it is still eligible.
                                    store.execute("UPDATE tasks SET state='blocked',available=0,updated=? WHERE id=?", (now, run["task"]))
                                    if tracking_cancelled():
                                        return
                                    checked(["gh", "pr", "close", outcome["pr"], "--repo", repo])
                                    continue
                        if (remote == policy["base"] and branch == policy["branch"] and
                                runtime.config(project["id"])["delivery"]["auto_merge"] and
                                not runtime.project(project["id"])["paused"] and not tracking_cancelled()):
                            checked(["gh", "pr", "merge", outcome["pr"], "--repo", repo, "--squash",
                                     "--match-head-commit", policy["head"]])
                            outcome["delivery_status"] = "merge_submitted"
                    elif gate == "stale" and not outcome.get("replanned"):
                        if tracking_cancelled():
                            return
                        task = store.one("SELECT attempts FROM tasks WHERE id=?", (run["task"],))
                        state = "queued" if task["attempts"] <= config["limits"]["retries"] else "blocked"
                        store.execute("UPDATE tasks SET state=?,available=0,updated=? WHERE id=?", (state, now, run["task"]))
                        outcome["replanned"] = True
                        if tracking_cancelled():
                            return
                        checked(["gh", "pr", "close", outcome["pr"], "--repo", repo])
                        store.execute("UPDATE runs SET state='superseded' WHERE id=?", (run["id"],))
                store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
            if outcome.get("merged_sha"):
                active = store.one("SELECT * FROM runs WHERE id=?", (active_run,)) if active_run else None
                feedback(runtime, project, run, outcome, config, now,
                         (lambda: runtime.is_cancelled(active)) if active else None)
                from .release import advance
                outcome["automation"] = advance(runtime, project, run, outcome, tracking_cancelled)
            store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        except Exception as exc:
            store.event("delivery_error", str(exc), project["id"], run["id"])


def feedback(runtime, project, run, outcome, config, now, cancel=None):
    if not monitoring_due(outcome, config, now):
        return
    if outcome.get("feedback", {}).get("investigation_pending"):
        investigate(runtime, project, run, outcome)
    if now - outcome.get("merged_at", now) > config["limits"]["retention_days"] * 86400:
        outcome["monitoring_completed"] = True
        runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        return
    previous = outcome.get("feedback", {})
    if now - outcome.get("feedback_polled_at", previous.get("checked_at", 0)) < config["delivery"]["feedback_minutes"] * 60:
        return
    identity = runtime.store.setting("run-baseline:" + run["id"])
    if not identity or not identity.get("repository") or not identity.get("branch"):
        raise RuntimeError("Post-merge verification requires a sealed repository and target branch")
    pinned = merge(config, {"github": {"repository": identity["repository"]},
                            "delivery": {"base_branch": identity["branch"]}})
    source, head, branch = baseline(runtime, project, pinned)
    if branch != identity["branch"]:
        raise RuntimeError("Post-merge verification target differs from sealed delivery")
    if previous.get("head") == head:
        outcome["feedback_polled_at"] = now
        runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        return
    directory = Path(run["path"]) / "feedback" / str(int(now))
    baseline_tree = snapshot(source, directory / "baseline", outcome["merged_sha"])
    current_tree = snapshot(source, directory / "current", head)
    from .adapters import detect
    acceptance_path = Path(run["path"]) / "acceptance.json"
    acceptance = json.loads(acceptance_path.read_text()) if acceptance_path.exists() else None
    if acceptance:
        acceptance["change_kind"] = "preservation"
    checks = json.loads(run["config"])["checks"] or detect(baseline_tree)["checks"]
    saved = json.loads(run["config"])
    effective = {**config, "environment": saved["environment"]}
    result = runtime.evaluator.evaluate(baseline_tree, current_tree, checks, directory / "evaluation", effective,
                                       cancel=cancel, acceptance=acceptance, seeds=Path(run["path"]) / "dependencies")
    outcome["feedback_polled_at"] = now
    state = {"pass": "verified", "fail": "regressed"}.get(result["decision"], "insufficient_evidence")
    outcome["feedback"] = {"state": state,
                           "head": head, "checked_at": now, "evaluation": result}
    if result["decision"] == "fail" and (previous.get("state") != "regressed" or previous.get("head") != head):
        outcome["feedback"]["investigation_pending"] = True
    runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
    runtime.store.event("postmerge", "Post-merge verification: " + outcome["feedback"]["state"], project["id"], run["id"])
    if outcome["feedback"].get("investigation_pending"):
        investigate(runtime, project, run, outcome)


def investigate(runtime, project, run, outcome):
    report = outcome["feedback"]
    failures = []
    for label, rows in report["evaluation"].get("checks", {}).items():
        for check in rows:
            if not check.get("passed") and len(failures) < 5:
                failures.append({"group": label, "name": check.get("name", "")[:200],
                                 "output": (check.get("stderr", "") + check.get("stdout", ""))[-1200:]})
    body = "Investigate verified post-merge regression from run " + run["id"] + ". "
    body += "Reproduce and assess value before changing code. Mainline: " + report["head"] + ". "
    body += "Full evidence is retained in the run's feedback report. Bounded excerpts: " + encode(failures)
    task = runtime.store.add_task(project["id"], "goal", "postmerge:" + run["id"] + ":" + report["head"],
                                  "Investigate verified post-merge regression", body,
                                  evidence=[{"run": run["id"], "head": report["head"], "checks": failures}], priority=100)
    report["investigation_pending"] = False
    report["investigation_task"] = task
    runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
    runtime.wake.set()
