import json
import html
import re
import time
from pathlib import Path

from .config import merge
from .gitops import git, origin_repository, snapshot
from .process import Cancelled, checked
from .store import encode, uid


class StaleCandidate(RuntimeError):
    pass


def _public_text(value, limit=4000):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "＠")
    return text[:limit]


def pr_body(task, run, outcome):
    """Explain the delivered behavior and evidence without publishing private task text."""
    value = outcome.get("value") or {}
    review = outcome.get("review") or {}
    evaluation = outcome.get("evaluation") or {}
    files = outcome.get("files") or []
    lines = ["## 解决的问题", "", _public_text(task["title"], 500), ""]
    if value.get("benefit"):
        lines += ["## 预期效果", "", _public_text(value["benefit"]), ""]
    lines += ["## 改动", "", _public_text(outcome.get("summary")) or "完成经过验证的改进。", ""]
    if files:
        visible = files[:50]
        file_lines = ["- <code>" + html.escape(path).replace("`", "&#96;").replace("\n", "&#10;").replace("\r", "&#13;") + "</code>" for path in visible]
        if len(files) > 50:
            file_lines.append(f"- <code>... 还有 {len(files) - 50} 个文件 ...</code>")
        lines += ["修改文件：", "", *file_lines, ""]
    lines += ["## 验证", "",
              "- 原始回归与候选完整检查：" + ("通过" if evaluation.get("decision") == "pass" else "未通过"),
              "- 独立验收：" + ("通过" if (evaluation.get("improvement") or {}).get("decision") == "pass" else "见运行记录"),
              "- 独立代码评审：" + ("通过" if review.get("decision") == "pass" else "未通过"),
              "", "## 运行信息", "",
              "- 运行：`" + run["id"] + "`",
              "- 基线：`" + run["base"] + "`"]
    if value.get("inaction"):
        lines += ["", "如果不做这次修改：" + _public_text(value["inaction"])]
    return "\n".join(lines) + "\n"


def github_git(root, *args, timeout=60):
    return git(root, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args, timeout=timeout)


def baseline(runtime, project, config):
    repo = config["github"]["repository"]
    if not repo:
        return project["path"], git(project["path"], "rev-parse", "HEAD"), ""
    if repo.lower() != origin_repository(project["path"]).lower():
        raise RuntimeError("Baseline repository must match project origin")
    branch = config["delivery"]["base_branch"] or checked(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"])
    # 基线锁按项目隔离：锁内的网络 fetch 不再让其他项目的基线刷新全局串行等待。
    with runtime.baseline_lock_for(project["id"]):
        cache = runtime.store.root / "remotes" / project["id"]
        if not cache.exists():
            checked(["git", "init", "--bare", "--quiet", str(cache)])
        for lock in cache.glob("*.lock"):
            try:
                lock.unlink()
            except OSError:
                pass
        github_git(cache, "fetch", "--quiet", "--no-tags", "https://github.com/" + repo + ".git",
            "+refs/heads/" + branch + ":refs/heads/rsi-baseline", timeout=180)
        sha = git(cache, "rev-parse", "refs/heads/rsi-baseline")
        runtime.store.put_setting("baseline:" + project["id"], {"sha": sha, "branch": branch})
        return str(cache), sha, branch


def pr_view(repo, url):
    return json.loads(checked(["gh", "pr", "view", url, "--repo", repo, "--json",
        "url,state,headRefOid,baseRefOid,baseRefName,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,mergeCommit,isDraft"]))


_PROTECTION_CACHE = {}


def merge_protection(config):
    """A bounded advisory read; GitHub still enforces the actual merge policy."""
    repo = config["github"]["repository"]
    branch = config["delivery"]["base_branch"]
    key = (repo, branch)
    cached = _PROTECTION_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    result = {"state": "unknown", "strict_checks": None, "branch": branch}
    try:
        from urllib.parse import quote
        if not branch:
            branch = json.loads(checked(["gh", "api", "repos/" + repo], timeout=10))["default_branch"]
        policy = json.loads(checked(["gh", "api", "repos/" + repo + "/branches/" + quote(branch, safe="") +
                                     "/protection"], timeout=10))
        strict = bool((policy.get("required_status_checks") or {}).get("strict"))
        result = {"state": "strict" if strict else "review_rules", "strict_checks": strict, "branch": branch}
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        pass
    if len(_PROTECTION_CACHE) >= 64:
        _PROTECTION_CACHE.clear()
    _PROTECTION_CACHE[key] = (time.monotonic(), result)
    return result


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
        source = store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))
        original = store.one("SELECT state FROM runs WHERE id=? AND project=?", (run["id"], project["id"]))
        if not original or original["state"] != "awaiting_approval":
            raise Cancelled("Approved candidate was superseded; publishing stopped")
        if (runtime.stop.is_set() or runtime.project(project["id"])["paused"] or
                not pending or pending["state"] != "planning" or (job and runtime.is_cancelled(job))):
            raise Cancelled("Publishing cancelled or project paused")
        if not source or source["state"] in ["cancelled", "expired", "obsolete"]:
            # 批准之后、发出外部写操作之前源任务被取消/过期时，授权不再成立。
            raise Cancelled("Source task is cancelled or expired; publishing stopped")
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
    authorize_write()
    identity = store.setting("run-baseline:" + run["id"])
    expected = {"repository": repo, "branch": base_branch, "sha": run["base"]}
    if identity != expected:
        raise StaleCandidate("Remote baseline changed; retry and re-evaluate before publishing")
    prefix = "fix" if task["source"] in ["issue", "rollback", "inspection"] else "feat"
    safe_id = re.sub(r"[^a-zA-Z0-9._-]", "-", run["id"])
    branch = prefix + "/rsi-" + safe_id
    if git(workspace, "branch", "--show-current") != branch:
        # 重试可复用同一候选提交上的分支，不能覆盖已有分支指向的其他提交。
        existing_head = git(workspace, "branch", "--list", branch, "--format=%(objectname)")
        if existing_head:
            if existing_head != git(workspace, "rev-parse", "HEAD"):
                raise RuntimeError("Existing delivery branch differs from evaluated candidate")
            git(workspace, "checkout", branch)
        else:
            git(workspace, "checkout", "-b", branch)
    title = re.sub(r"[\r\n\0]+", " ", task["title"]).strip()
    if git(workspace, "diff", "--cached", "--name-only"):
        git(workspace, "-c", "user.name=Adaptive RSI", "-c", "user.email=adaptive-rsi@localhost",
            "commit", "-m", "完成任务：" + title)
    sha = git(workspace, "rev-parse", "HEAD")
    existing = json.loads(checked(["gh", "pr", "list", "--repo", repo, "--state", "all", "--head", branch,
                                   "--limit", "1", "--json", "url"]))
    if existing:
        url = existing[0]["url"]
        existing_pr = pr_view(repo, url)
        if existing_pr["headRefOid"] != sha or existing_pr.get("baseRefName") != identity["branch"]:
            raise RuntimeError("Existing PR head or target differs from evaluated candidate")
        # Recover an observed remote fact before any later cancellation can discard it.
        # This path performs no remote write, so fresh publishing authorization is unnecessary.
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            original = db.execute("SELECT state,result FROM runs WHERE id=?", (run["id"],)).fetchone()
            receipt = json.loads(original["result"])
            receipt["pr"] = url
            state = "superseded" if original["state"] == "superseded" else "pr_open"
            if state == "superseded":
                receipt.update(replanned=True, close_pending=True)
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", ("delivery:" + run["id"], encode(
                {"head": sha, "base": run["base"], "branch": base_branch, "repository": repo})))
            db.execute("UPDATE runs SET state=?,result=? WHERE id=?", (state, encode(receipt), run["id"]))
            if state == "pr_open":
                db.execute("UPDATE tasks SET state='pr_open',updated=? WHERE id=? AND state='awaiting_approval'",
                           (time.time(), task["id"]))
    else:
        if current != run["base"]:
            raise StaleCandidate("Remote baseline changed; retry and re-evaluate before publishing")
        git(workspace, "remote", "set-url", "origin", "https://github.com/" + repo + ".git")
        authorize_write()
        github_git(workspace, "push", "origin", "HEAD:refs/heads/" + branch)
        body = Path(workspace).parent / "pr-body.md"
        body.write_text(pr_body(task, run, outcome), encoding="utf-8")
        authorize_write()
        url = checked(["gh", "pr", "create", "--repo", repo, "--head", branch, "--base", base_branch,
                       "--title", "完成任务：" + task["title"], "--body-file", str(body)])
    store.put_setting("delivery:" + run["id"], {"head": sha, "base": run["base"], "branch": base_branch,
                                               "repository": repo})
    store.event("pr", "PR created", project["id"], run["id"], {"url": url, "head": sha})
    return url


def monitoring_due(outcome, config, now):
    from .storage_tools import pending_investigation
    if pending_investigation(outcome.get("feedback", {})) is not None:
        return True
    if outcome.get("monitoring_completed"):
        return False
    return (now - outcome.get("merged_at", now) > config["limits"]["retention_days"] * 86400 or
            now - outcome.get("feedback_polled_at", outcome.get("feedback", {}).get("checked_at", 0)) >=
            config["delivery"]["feedback_minutes"] * 60)


def _update_pr_task(store, run, state, now):
    # Tracking may finish after the user requeued or revised this task.
    store.execute("UPDATE tasks SET state=?,updated=? WHERE id=? AND state='pr_open' "
                  "AND NOT EXISTS (SELECT 1 FROM runs WHERE task=? AND "
                  "(started>? OR (started=? AND id>?)))",
                  (state, now, run["task"], run["task"], run["started"], run["started"], run["id"]))


def _supersede_pr(store, run, outcome, task_state, now):
    """Detach tracking from the task and persist close intent before remote writes."""
    outcome.update(replanned=True, close_pending=True)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE runs SET state='superseded',result=? WHERE id=? AND state='pr_open'",
                             (encode(outcome), run["id"])).rowcount
        if changed:
            db.execute("UPDATE tasks SET state=?,available=0,updated=? WHERE id=? AND state='pr_open' "
                       "AND NOT EXISTS (SELECT 1 FROM runs WHERE task=? AND "
                       "(started>? OR (started=? AND id>?)))",
                       (task_state, now, run["task"], run["task"], run["started"], run["started"], run["id"]))
    return bool(changed)


def _close_superseded_pr(store, run, outcome, repo, pr, cancelled):
    # A failed/lost response leaves the intent durable. Always observe remote state
    # before retrying, including manual closes and merges while the worker was down.
    if pr["state"] == "OPEN":
        if cancelled():
            return
        checked(["gh", "pr", "close", outcome["pr"], "--repo", repo])
        outcome["close_status"] = "close_submitted"
    else:
        outcome["close_status"] = pr["state"].lower()
    outcome["close_pending"] = False
    store.execute("UPDATE runs SET result=? WHERE id=? AND state='superseded'", (encode(outcome), run["id"]))
    store.remote_wait(run["id"], None, time.time())


def schedule(runtime, now=None):
    now = now or time.time()
    store = runtime.store
    if now - store.setting("delivery-scan", 0) < 60:
        return
    store.put_setting("delivery-scan", now)
    projects = set()
    from .release import pending
    for run in store.query("SELECT r.* FROM runs r JOIN projects p ON p.id=r.project "
                           "WHERE p.paused=0 AND (r.state IN ('pr_open','merged') OR "
                           "(r.state='superseded' AND json_extract(r.result,'$.close_pending')=1))"):
        with runtime.scheduler_scope("delivery_run", run["project"], run["id"]):
            if (run["state"] in ["pr_open", "superseded"] or pending(runtime, run) or
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
    # 入口处归一化：调用方约定传运行 id 字符串；若误传 run 字典则取其 id，
    # 其余真值非字符串转为字符串，空值保持 falsy，后续一律使用归一后的值参与查询。
    if isinstance(active_run, dict):
        active_run = active_run.get("id")
    if active_run and not isinstance(active_run, str):
        active_run = str(active_run)
    tick_key = "delivery-tick" + (":" + project_id if project_id else "")
    if now - store.setting(tick_key, 0) < 60:
        return
    store.put_setting(tick_key, now)

    def tracking_cancelled():
        if runtime.stop.is_set():
            return True
        if not active_run:
            return False
        current = store.one("SELECT * FROM runs WHERE id=?", (active_run,))
        return not current or runtime.is_cancelled(current)

    for run in store.query("SELECT * FROM runs WHERE (state IN ('pr_open','merged') OR "
                           "(state='superseded' AND json_extract(result,'$.close_pending')=1))" +
                           (" AND project=?" if project_id else "") + " ORDER BY started",
                           (project_id,) if project_id else ()):
        if tracking_cancelled():
            return
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
            pr = pr_view(repo, outcome["pr"]) if run["state"] in ["pr_open", "superseded"] else None
            if run["state"] == "pr_open":
                task = store.one("SELECT state FROM tasks WHERE id=?", (run["task"],))
                newer = store.one("SELECT 1 FROM runs WHERE task=? AND "
                                  "(started>? OR (started=? AND id>?))",
                                  (run["task"], run["started"], run["started"], run["id"]))
                if not task or task["state"] != "pr_open" or newer:
                    # Also recover older databases where a requeue preceded a failed close.
                    if _supersede_pr(store, run, outcome, task["state"] if task else "blocked", now):
                        run["state"] = "superseded"
            if run["state"] == "superseded":
                _close_superseded_pr(store, run, outcome, repo, pr, tracking_cancelled)
                continue
            if run["state"] == "pr_open":
                gate = merge_gate(pr, policy["head"], policy["base"])
                if pr.get("baseRefName", policy["branch"]) != policy["branch"] or (
                        config["delivery"]["base_branch"] and config["delivery"]["base_branch"] != policy["branch"]):
                    gate = "stale"
                if pr["state"] == "MERGED":
                    if (not sealed_target or pr["headRefOid"] != policy["head"] or
                            pr.get("baseRefName", policy["branch"]) != policy["branch"]):
                        outcome["delivery_status"] = "unverified_merge"
                        store.execute("UPDATE runs SET state='unverified_merge',result=? WHERE id=?", (encode(outcome), run["id"]))
                        _update_pr_task(store, run, "blocked", now)
                    else:
                        outcome["merged_sha"] = pr["mergeCommit"]["oid"]
                        outcome["merged_at"] = now
                        outcome["delivery_status"] = "merged"
                        store.execute("UPDATE runs SET state='merged',result=? WHERE id=?", (encode(outcome), run["id"]))
                        _update_pr_task(store, run, "merged", now)
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
                    _update_pr_task(store, run, "pr_closed", now)
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
                                    # Issue sync will refresh evidence and requeue this task if it is still eligible.
                                    if _supersede_pr(store, run, outcome, "blocked", now):
                                        _close_superseded_pr(store, run, outcome, repo,
                                                             pr_view(repo, outcome["pr"]), tracking_cancelled)
                                    continue
                        # Fetch and Issue checks can outlive a settings change. Re-read the complete
                        # local authorization under the same lock used by configuration writes.
                        merge_admitted = False
                        with runtime.dispatch_lock:
                            live_project = runtime.project(project["id"])
                            live_config = runtime.config(project["id"])
                            live_run = store.one("SELECT * FROM runs WHERE id=? AND project=?",
                                                 (run["id"], project["id"]))
                            live_policy = store.setting("delivery:" + run["id"])
                            live_identity = store.setting("run-baseline:" + run["id"])
                            if (remote == policy["base"] and branch == policy["branch"] and
                                    live_run and live_run["state"] == "pr_open" and
                                    json.loads(live_run["config"])["delivery"]["auto_merge"] and
                                    json.loads(live_run["result"]).get("pr") == outcome["pr"] and
                                    live_config["delivery"]["mode"] == "pr" and
                                    live_config["delivery"]["auto_merge"] and
                                    live_config["github"]["repository"] == repo and
                                    live_config["delivery"]["base_branch"] == config["delivery"]["base_branch"] and
                                    (not live_config["delivery"]["base_branch"] or
                                     live_config["delivery"]["base_branch"] == policy["branch"]) and
                                    repo.lower() == origin_repository(live_project["path"]).lower() and
                                    live_policy == policy and live_identity == identity and
                                    live_identity == {"repository": repo, "branch": policy["branch"], "sha": policy["base"]} and
                                    not live_project["paused"] and not tracking_cancelled()):
                                merge_admitted = True
                                store.put_setting("merge-intent:" + run["id"], {"head": policy["head"], "at": now})
                        if merge_admitted:
                            checked(["gh", "pr", "merge", outcome["pr"], "--repo", repo, "--squash",
                                     "--match-head-commit", policy["head"]])
                            outcome["delivery_status"] = "merge_submitted"
                    elif gate == "stale" and not outcome.get("replanned"):
                        if tracking_cancelled():
                            return
                        task = store.one("SELECT attempts FROM tasks WHERE id=?", (run["task"],))
                        state = "queued" if task["attempts"] <= config["limits"]["retries"] else "blocked"
                        if _supersede_pr(store, run, outcome, state, now):
                            _close_superseded_pr(store, run, outcome, repo,
                                                 pr_view(repo, outcome["pr"]), tracking_cancelled)
                        continue
                status = outcome.get("delivery_status")
                store.remote_wait(run["id"], status if status in ["waiting_ci", "waiting_review", "waiting", "merge_submitted"] else None, now)
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
    from .storage_tools import pending_investigation
    if pending_investigation(outcome.get("feedback", {})) is not None:
        investigate(runtime, project, run, outcome)
        if not monitoring_due(outcome, config, now):
            return
    # Expiration ends monitoring even when its local artifacts were not restored.
    # Pending investigations above must finish (or remain retryable) before completion.
    if now - outcome.get("merged_at", now) > config["limits"]["retention_days"] * 86400:
        outcome["monitoring_completed"] = True
        runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
        return
    from .storage_tools import MISSING_ARTIFACTS, run_artifact_path, unavailable_feedback
    if now - outcome.get("feedback_polled_at", 0) < config["delivery"]["feedback_minutes"] * 60:
        return
    saved = json.loads(run["config"])

    def missing_artifacts():
        previous = outcome.get("feedback", {})
        outcome["feedback_polled_at"] = now
        if previous.get("state") != "insufficient_evidence" or previous.get("reason") != MISSING_ARTIFACTS:
            outcome["feedback"] = unavailable_feedback(previous)
            runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
            runtime.store.event("postmerge", MISSING_ARTIFACTS, project["id"], run["id"])
        else:
            runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))

    try:
        output_name = "feedback/" + str(time.time_ns()) + "-" + uid()
        directory = run_artifact_path(runtime.store, run, output_name)

        def output_path(name):
            target = run_artifact_path(runtime.store, run, output_name + "/" + name)
            if target != directory / name:
                raise ValueError(MISSING_ARTIFACTS)
            return target

        # Check actual destinations, including Git metadata and the evaluator report,
        # before any fetch or write. An old output tree must never be reused.
        for name in ("baseline", "baseline/.git/config", "current", "current/.git/config",
                     "evaluation", "evaluation/report.json"):
            output_path(name)
        if directory.exists():
            raise ValueError(MISSING_ARTIFACTS)
        if not run_artifact_path(runtime.store, run).is_dir():
            raise ValueError(MISSING_ARTIFACTS)
        acceptance_path = run_artifact_path(runtime.store, run, "acceptance.json")
        acceptance = json.loads(acceptance_path.read_text()) if acceptance_path.exists() else None
        if acceptance_path.exists():
            from .agent import SCHEMAS, object_schema, validate_output
            if not isinstance(acceptance, dict):
                raise ValueError(MISSING_ARTIFACTS)
            validate_output({key: acceptance.get(key) for key in ("hash", "criteria", "checks", "spec")},
                            object_schema({"hash": {"type": "string"},
                                           "criteria": {"type": "array", "items": {"type": "string"}},
                                           "checks": {"type": "array", "items": object_schema({
                                               "name": {"type": "string"}, "argv": {"type": "array", "items": {"type": "string"}},
                                               "cwd": {"type": "string"}})}, "spec": SCHEMAS["acceptance"]}))
        expected_acceptance = outcome.get("evaluation", {}).get("acceptance_hash")
        if expected_acceptance and (not acceptance or acceptance.get("hash") != expected_acceptance):
            raise ValueError(MISSING_ARTIFACTS)
        seeds = run_artifact_path(runtime.store, run, "dependencies")
        for relative in saved["environment"]["seed_paths"]:
            if not run_artifact_path(runtime.store, run, "dependencies/" + relative).is_dir():
                raise ValueError(MISSING_ARTIFACTS)
    except (ValueError, OSError, RuntimeError):
        missing_artifacts()
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
    try:
        # Fetching can take time; recheck destinations before exclusively creating output.
        for name in ("baseline", "baseline/.git/config", "current", "current/.git/config",
                     "evaluation", "evaluation/report.json"):
            output_path(name)
        directory.parent.mkdir(exist_ok=True)
        directory.mkdir(mode=0o700)  # Exclusive: refuse collisions instead of overwriting evidence.
    except (ValueError, OSError, RuntimeError):
        missing_artifacts()
        return
    baseline_tree = snapshot(source, output_path("baseline"), outcome["merged_sha"])
    current_tree = snapshot(source, output_path("current"), head)
    from .adapters import detect
    if acceptance:
        acceptance["change_kind"] = "preservation"
    checks = saved["checks"] or detect(baseline_tree)["checks"]
    effective = {**config, "environment": saved["environment"]}
    output_path("evaluation/report.json")
    result = runtime.evaluator.evaluate(baseline_tree, current_tree, checks, output_path("evaluation"), effective,
                                       cancel=cancel, acceptance=acceptance, seeds=seeds)
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
    from .storage_tools import pending_investigation
    feedback_status = outcome["feedback"]
    report = pending_investigation(feedback_status)
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
    feedback_status.update(investigation_pending=False, investigation_task=task)
    runtime.store.execute("UPDATE runs SET result=? WHERE id=?", (encode(outcome), run["id"]))
    runtime.wake.set()
