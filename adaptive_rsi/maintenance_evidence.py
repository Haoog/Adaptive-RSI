"""Versioned evidence from the maintenance entry points."""
import re
import time

from .config import fingerprint
from .store import decode_object, encode


def full_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is not None


def record_observation(db, task, observation, reason, *, state="queued"):
    signal = fingerprint(observation)
    return db.execute(
        "INSERT OR IGNORE INTO inspection_observations(task,signal,data,observed) VALUES (?,?,?,?)",
        (task, signal, encode({**observation, "reason": reason, "task_state": state}), time.time()),
    ).rowcount == 1


def task_observations(store, task):
    return [{**decode_object(row["data"]), "observed": row["observed"]} for row in store.query(
        "SELECT data,observed FROM inspection_observations WHERE task=? ORDER BY observed DESC,signal LIMIT 50",
        (task,),
    )]


REVIEW_BATCH = 20
REVIEW_WEEK = 7 * 86400
REVIEW_RETRY = 3600
SCHEDULE_PREFIX = "maintenance-weekly:"


def _local_git(path, *args):
    from .gitops import git

    return git(path, "--no-replace-objects", *args, local_only=True)


def main_head(project):
    path = project["path"]
    refs = _local_git(path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/main", "refs/heads/main").splitlines()
    ref = next((name for name in ("refs/remotes/origin/main", "refs/heads/main") if name in refs), None)
    if ref is None:
        raise ValueError("No local main or origin/main ref; refresh the repository first")
    return _local_git(path, "rev-parse", "--verify", ref + "^{commit}")


def fixed_check_signal(store, project, repository, head):
    entry = store.setting("maintenance-ci:" + project, {})
    if not repository or not isinstance(entry, dict) or entry.get("repository") != repository or not full_sha(head):
        return {"status": "investigate", "source": "github_actions"}
    checks = entry.get("checks")
    matches = [check for check in checks if isinstance(check, dict) and check.get("headSha") == head
               and check.get("headBranch") == "main" and check.get("event") == "push"
               and check.get("status") == "completed"
               and check.get("conclusion") in {"success", "failure", "timed_out", "action_required"}
               and type(check.get("databaseId")) is int] if isinstance(checks, list) else []
    return {"status": "version_matched" if matches else "investigate", "source": "github_actions",
            "repository": repository, "observed": entry.get("observed"), "version": head, "checks": matches}


def _cursor(db, project):
    row = db.execute("SELECT value FROM settings WHERE key=?", ("maintenance-cursor:" + project,)).fetchone()
    return decode_object(row["value"])["sha"] if row else None


def _recent_review_paths(store, project, history, base):
    # Only reuse cached paths for the preceding first-parent window on this main.
    shas = history("rev-list", "--first-parent", f"--max-count={REVIEW_BATCH}", base).splitlines()
    cached = {row["sha"]: decode_object(row["data"]) for row in store.query(
        "SELECT sha,data FROM maintenance_reviews WHERE project=? AND sha IN (" +
        ",".join("?" for _ in shas) + ")", (project, *shas))}
    last_change = {}
    for sha in reversed(shas):
        paths = cached.get(sha, {}).get("paths")
        if isinstance(paths, list):
            for path in paths[:250]:
                if isinstance(path, str):
                    last_change[path] = sha
    return shas, last_change


def _review_clues(paths, message, prior_shas, last_change, evidence, rollbacks):
    clues = []
    reverted = {}
    for target in re.findall(r"^This reverts commit ([0-9a-f]+)(?:\.|, reversing\nchanges made to [0-9a-f]+\.)$",
                             message, re.MULTILINE):
        if target in prior_shas:
            reverted.setdefault(target, {})["git_message"] = True
    for target, original_run in rollbacks.items():
        if target in prior_shas:
            reverted.setdefault(target, {})["delivery_runs"] = {"original": original_run, "rollback": evidence["run"]}
    for target, sources in reverted.items():
        clues.append({"kind": "explicit_revert", "source_sha": target, "sources": sources})

    overlaps = {}
    for path in paths:
        previous = last_change.get(path)
        if previous and previous not in reverted:
            overlaps.setdefault(previous, []).append(path)
    for previous, shared_paths in overlaps.items():
        clues.append({"kind": "possible_followup", "source_sha": previous, "paths": shared_paths,
                      "basis": "exact_path_overlap_only"})

    evidence = evidence or {}
    missing = []
    if not evidence:
        missing.append("linked_merged_run")
    if not evidence.get("candidate_checks"):
        missing.append("recorded_candidate_checks")
    if evidence.get("postmerge_verification") not in ("verified", "regressed"):
        missing.append("conclusive_version_matched_postmerge_record")
    if missing:
        clues.append({"kind": "validation_gap", "status": "insufficient_evidence", "missing": missing})
    return clues


def retrospective_preview(store, project, start=None):
    project_id, path = project["id"], project["path"]
    with store.connect() as db:
        cursor = _cursor(db, project_id)
    head = main_head(project)
    base = cursor or start
    if not full_sha(base) or (cursor and start and start != cursor):
        raise ValueError("Provide an initial full main SHA, or use the saved review cursor")
    history = lambda *args: _local_git(path, *args)
    try:
        distance = int(history("rev-list", "--first-parent", "--count", head, "^" + base))
        on_main = history("rev-parse", "--verify", f"{head}~{distance}") == base
    except RuntimeError as exc:
        raise ValueError("The review base is no longer on the first-parent history of main") from exc
    if not on_main:
        raise ValueError("The review base is no longer on the first-parent history of main")
    end = history("rev-parse", "--verify", f"{head}~{max(distance - REVIEW_BATCH, 0)}")
    shas = history("rev-list", "--first-parent", "--reverse", end, "^" + base).splitlines()
    evidence, rollbacks = {}, {}
    prior_shas, last_change = set(), {}
    if shas:
        recent_shas, last_change = _recent_review_paths(store, project_id, history, base)
        prior_shas.update(recent_shas)
        anchors = shas + recent_shas
        batch_shas = set(shas)
        rows = store.query("SELECT id,state,result FROM runs WHERE project=? AND state IN ('merged','reverted') "
                           "AND CASE WHEN json_valid(result) THEN json_extract(result,'$.merged_sha') END IN (" +
                           ",".join("?" for _ in anchors) + ") ORDER BY started DESC,id", (project_id, *anchors))
        for row in rows:
            result = decode_object(row["result"])
            if result.get("delivery_status") not in (None, "merged"):
                continue
            sha = result["merged_sha"]
            reverted_by = result.get("reverted_by")
            if row["state"] == "reverted" and isinstance(reverted_by, str) and reverted_by:
                rollbacks.setdefault(reverted_by, {}).setdefault(sha, row["id"])
            if sha not in batch_shas or sha in evidence:
                continue
            evaluation = result.get("evaluation")
            evaluation = evaluation if isinstance(evaluation, dict) else {}
            checks = evaluation.get("checks")
            checks = checks if isinstance(checks, dict) else {}
            groups = {}
            for label in ("candidate_full", "candidate", "acceptance_candidate", "test_first_candidate"):
                if isinstance(checks.get(label), list):
                    outcomes = [check["passed"] for check in checks[label]
                                if isinstance(check, dict) and type(check.get("passed")) is bool]
                    if outcomes:
                        groups[label] = {"total": len(outcomes), "passed": sum(outcomes)}
            feedback = result.get("feedback")
            feedback = feedback if isinstance(feedback, dict) else {}
            evidence[sha] = {"run": row["id"], "pr": result.get("pr"),
                             "evaluation": evaluation.get("decision"), "candidate_checks": groups,
                             "postmerge_verification": feedback.get("state") if feedback.get("head") == sha else None}
    commits = []
    parent = base
    for sha in shas:
        paths = history("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", parent, sha).split("\0")
        paths = list(filter(None, paths))
        shown_paths = paths[:250]
        subject, _, message = history("show", "-s", "--no-notes", "--no-show-signature",
                                      "--format=%s%n%b", sha).partition("\n")
        linked = evidence.get(sha)
        clues = _review_clues(shown_paths, message, prior_shas, last_change, linked,
                              rollbacks.get(linked["run"], {}) if linked else {})
        commits.append({"sha": sha, "subject": subject[:300], "paths": shown_paths,
                        "paths_omitted": max(0, len(paths) - 250), "evidence": linked, "review_clues": clues})
        prior_shas.add(sha)
        last_change.update(dict.fromkeys(shown_paths, sha))
        parent = sha
    preview = {"project": project_id, "cursor": cursor, "base": base, "end": end, "main": head, "commits": commits}
    return {**preview, "token": fingerprint(preview)}


def _confirmed_rule(item, commits):
    from .config import relative_path

    if not isinstance(item, dict) or not full_sha(item.get("sha")) or item["sha"] not in commits:
        raise ValueError("A confirmed rule must reference a commit in this review batch")
    paths = item.get("paths")
    commit = commits[item["sha"]]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 8 or any(
            not isinstance(path, str) or len(path) > 512 or path not in commit["paths"] for path in paths):
        raise ValueError("Choose 1..8 changed paths from the cited main commit")
    for path in paths:
        relative_path(path)
    rule = {"sha": item["sha"], "paths": sorted(set(paths)), "pr": (commit["evidence"] or {}).get("pr")}
    for field in ("rule", "reason", "validation"):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 800:
            raise ValueError("Rule, reason and a test or acceptance gap are required (1..800 characters each)")
        rule[field] = value.strip()
    return rule


def confirm_retrospective(store, project, start, token, rules):
    if not full_sha(token) or not isinstance(rules, list) or len(rules) > REVIEW_BATCH:
        raise ValueError("A preview token and up to 20 explicitly confirmed rules are required")
    preview = retrospective_preview(store, project, start)
    if preview["token"] != token:
        raise ValueError("The review preview changed; preview the range again")
    commits = {commit["sha"]: commit for commit in preview["commits"]}
    if not commits:
        raise ValueError("There are no new main commits in this review batch")
    confirmed = [_confirmed_rule(item, commits) for item in rules]
    project_id = project["id"]
    now = time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if _cursor(db, project_id) != preview["cursor"] or main_head(project) != preview["main"]:
            raise ValueError("Main or the review cursor changed; preview the range again")
        if db.execute("SELECT 1 FROM settings WHERE key=?", ("project-archived:" + project_id,)).fetchone():
            raise ValueError("Project is archived; restore it before recording a review")
        db.executemany("INSERT INTO maintenance_reviews VALUES (?,?,?,?)",
                       [(project_id, sha, encode(commit), now) for sha, commit in commits.items()])
        db.executemany("INSERT OR IGNORE INTO maintenance_rules VALUES (?,?,?,?)",
                       [(project_id, fingerprint(rule), encode(rule), now) for rule in confirmed])
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                   ("maintenance-cursor:" + project_id, encode({"sha": preview["end"]})))
    return {"cursor": preview["end"], "reviewed": len(commits)}


def configure_retrospective(store, project, enabled, start=None, now=None):
    if type(enabled) is not bool or (start is not None and (not enabled or not full_sha(start))):
        raise ValueError("Use a boolean weekly setting and an optional full starting SHA when enabling")
    project_id = project["id"]
    key = SCHEDULE_PREFIX + project_id
    now = time.time() if now is None else now
    with store.connect() as db:
        cursor = _cursor(db, project_id)
        previous = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    state = decode_object(previous["value"]) if previous else {}
    base = None if cursor else (start or state.get("start"))
    if enabled:
        retrospective_preview(store, project, start if cursor else base)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM projects WHERE id=? AND path=?", (project_id, project["path"])).fetchone():
            raise ValueError("Project not found")
        if db.execute("SELECT 1 FROM settings WHERE key=?", ("project-archived:" + project_id,)).fetchone():
            raise ValueError("Project is archived; restore it before starting work")
        if (previous != db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                or cursor != _cursor(db, project_id)):
            raise ValueError("Main or the review cursor changed; preview the range again")
        if enabled and (not state.get("enabled") or (not cursor and state.get("start") != base)):
            state.update(next_at=now, error=None)
        if enabled:
            state["start"] = base
        else:
            state["next_at"] = None
        state["enabled"] = enabled
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(state)))
    return state


def schedule_retrospectives(runtime, now):
    """Persist one pending batch per project and leave the review cursor to the user."""
    store = runtime.store
    projects = store.query(
        "SELECT p.id,p.path,s.key,s.value FROM settings s "
        "JOIN projects p ON p.id=substr(s.key, ?) WHERE s.key GLOB ? AND p.paused=0 "
        "AND NOT EXISTS (SELECT 1 FROM settings WHERE key='project-archived:' || p.id) "
        "AND CASE WHEN json_valid(s.value) THEN json_extract(s.value,'$.enabled')=1 "
        "AND json_extract(s.value,'$.next_at')<=? ELSE 0 END ORDER BY p.created",
        (len(SCHEDULE_PREFIX) + 1, SCHEDULE_PREFIX + "*", now))
    for project in projects:
        if runtime.stop.is_set():
            return
        with runtime.scheduler_scope("weekly_retrospective", project["id"]):
            _scheduled_preview(store, project, now)


def _scheduled_preview(store, project, now):
    project_id = project["id"]
    state = decode_object(project["value"])
    with store.connect() as db:
        cursor = _cursor(db, project_id)
    try:
        preview = retrospective_preview(store, project, None if cursor else state.get("start"))
        if preview["main"] != main_head(project):
            raise ValueError("Main or the review cursor changed; preview the range again")
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Local retrospective evidence could not be read"
        state.update(next_at=now + REVIEW_RETRY, error=message)
    else:
        # Ignore unchanged evidence on repeated weekly ticks. Keep the original generation time.
        if (state.get("summary") or {}).get("token") != preview["token"]:
            state["summary"] = {**preview, "generated_at": now}
        state.update(next_at=now + REVIEW_WEEK, error=None)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if _cursor(db, project_id) != cursor:
            return
        db.execute("UPDATE settings SET value=? WHERE key=? AND value=? "
                   "AND EXISTS (SELECT 1 FROM projects WHERE id=? AND path=? AND paused=0) "
                   "AND NOT EXISTS (SELECT 1 FROM settings WHERE key=?)",
                   (encode(state), project["key"], project["value"], project_id, project["path"],
                    "project-archived:" + project_id))


def maintenance_summary(store, project, repository):
    project_id = project["id"]
    with store.connect() as db:
        cursor = _cursor(db, project_id)
    try:
        head = main_head(project)
    except (ValueError, RuntimeError):
        head = None
    return {"cursor": cursor, "main": head, "signal": fixed_check_signal(store, project_id, repository, head),
            "schedule": store.setting(SCHEDULE_PREFIX + project_id, {}), "paused": bool(project["paused"]),
            "rules": [decode_object(row["data"]) for row in store.query(
                "SELECT data FROM maintenance_rules WHERE project=? ORDER BY confirmed DESC,id LIMIT 50", (project_id,))],
            "reviews": [decode_object(row["data"]) for row in store.query(
                "SELECT data FROM maintenance_reviews WHERE project=? ORDER BY rowid DESC LIMIT 20", (project_id,))]}


def applicable_rules(store, project, paths, repository, base):
    """Use only confirmed rules for exact paths and commits in the active baseline."""
    from .gitops import git

    paths = sorted({path for path in paths if isinstance(path, str) and path})
    if not paths or not full_sha(base):
        return []
    candidates = store.query(
        "SELECT data FROM maintenance_rules WHERE project=? AND EXISTS "
        "(SELECT 1 FROM json_each(maintenance_rules.data,'$.paths') rule_path "
        "JOIN json_each(?) requested ON rule_path.value=requested.value) "
        "ORDER BY confirmed DESC,id LIMIT 12", (project, encode(paths)))
    rules = []
    for row in candidates:
        rule = decode_object(row["data"])
        if not full_sha(rule.get("sha")):
            continue
        try:
            git(repository, "--no-replace-objects", "merge-base", "--is-ancestor", rule["sha"], base)
        except RuntimeError:
            continue
        rules.append(rule)
        if len(rules) == 3:
            break
    return rules
