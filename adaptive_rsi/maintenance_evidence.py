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


def main_head(project):
    from .gitops import git
    path = project["path"]
    refs = git(path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/main", "refs/heads/main").splitlines()
    ref = next((name for name in ("refs/remotes/origin/main", "refs/heads/main") if name in refs), None)
    if ref is None:
        raise ValueError("No local main or origin/main ref; refresh the repository first")
    return git(path, "--no-replace-objects", "rev-parse", "--verify", ref + "^{commit}")


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


def retrospective_preview(store, project, start=None):
    from .gitops import git

    project_id, path = project["id"], project["path"]
    with store.connect() as db:
        cursor = _cursor(db, project_id)
    head = main_head(project)
    base = cursor or start
    if not full_sha(base) or (cursor and start and start != cursor):
        raise ValueError("Provide an initial full main SHA, or use the saved review cursor")
    history = lambda *args: git(path, "--no-replace-objects", *args)
    try:
        distance = int(history("rev-list", "--first-parent", "--count", head, "^" + base))
        on_main = history("rev-parse", "--verify", f"{head}~{distance}") == base
    except RuntimeError as exc:
        raise ValueError("The review base is no longer on the first-parent history of main") from exc
    if not on_main:
        raise ValueError("The review base is no longer on the first-parent history of main")
    end = history("rev-parse", "--verify", f"{head}~{max(distance - REVIEW_BATCH, 0)}")
    shas = history("rev-list", "--first-parent", "--reverse", end, "^" + base).splitlines()
    evidence = {}
    if shas:
        rows = store.query("SELECT id,result FROM runs WHERE project=? AND state='merged' AND json_valid(result) "
                           "AND json_extract(result,'$.merged_sha') IN (" + ",".join("?" for _ in shas) + ") "
                           "ORDER BY started DESC,id", (project_id, *shas))
        for row in rows:
            result = decode_object(row["result"])
            if result.get("delivery_status") not in (None, "merged"):
                continue
            evaluation = result.get("evaluation")
            evaluation = evaluation if isinstance(evaluation, dict) else {}
            checks = evaluation.get("checks")
            checks = checks if isinstance(checks, dict) else {}
            groups = {label: {"total": len(checks[label]),
                              "passed": sum(isinstance(check, dict) and check.get("passed") is True
                                            for check in checks[label])}
                      for label in ("candidate_full", "candidate", "acceptance_candidate", "test_first_candidate")
                      if isinstance(checks.get(label), list) and checks[label]}
            feedback = result.get("feedback")
            feedback = feedback if isinstance(feedback, dict) else {}
            evidence.setdefault(result["merged_sha"], {"run": row["id"], "pr": result.get("pr"),
                                "evaluation": evaluation.get("decision"), "candidate_checks": groups,
                                "postmerge_verification": feedback.get("state") if feedback.get("head") == result["merged_sha"] else None})
    commits = []
    parent = base
    for sha in shas:
        paths = history("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", parent, sha).split("\0")
        paths = list(filter(None, paths))
        commits.append({"sha": sha, "subject": history("show", "-s", "--format=%s", sha)[:300],
                        "paths": paths[:250], "paths_omitted": max(0, len(paths) - 250),
                        "evidence": evidence.get(sha)})
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


def maintenance_summary(store, project, repository):
    project_id = project["id"]
    with store.connect() as db:
        cursor = _cursor(db, project_id)
    try:
        head = main_head(project)
    except (ValueError, RuntimeError):
        head = None
    return {"cursor": cursor, "main": head, "signal": fixed_check_signal(store, project_id, repository, head),
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
