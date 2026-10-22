"""Bounded Issue synchronization with a durable, project-scoped continuation."""
import datetime
import heapq
import json
import re
import time

from .store import encode


ISSUE_COMMENT_TTL = 300
ISSUE_COMMENT_LIMIT = 20
ISSUE_COMMENT_PAGE_SIZE = 100
ISSUE_COMMENT_BODY_LIMIT = 4000


def issue_comment_context(runtime, project, task, config, checked=None, cancel=None):
    """Load a bounded, cached window of untrusted comments for an Issue task."""
    if task.get("source") != "issue":
        return {"status": "not_issue", "comments": [], "complete": True, "pages_complete": True,
                "omitted_count": 0, "warning": None, "failure_code": None, "updated_at": None, "fetched_at": None}
    try:
        evidence = json.loads(task.get("evidence") or "[]")
    except (TypeError, ValueError):
        evidence = []
    issue_evidence = next((item for item in evidence if item.get("number") is not None), {})
    number = issue_evidence.get("number")
    repo = config.get("github", {}).get("repository", "")
    if not isinstance(number, int) or number < 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return {"status": "unavailable", "comments": [], "complete": False,
                "failure_code": "invalid_issue_reference",
                "pages_complete": False, "omitted_count": None, "updated_at": None, "fetched_at": None,
                "warning": "Issue comment context is unavailable because the Issue reference or repository is invalid."}
    if checked is None:
        from .process import checked as run_checked
        checked = run_checked

    store = runtime.store
    key = "issue-comments:" + project["id"] + ":" + str(number)
    now = time.time()
    cached = store.setting(key, {})
    if (cached.get("repository") != repo or
            cached.get("issue_updated_at") != issue_evidence.get("updated")):
        cached = {}
    if cached and now - cached.get("checked_at", 0) < ISSUE_COMMENT_TTL:
        return _issue_comment_response(cached, "cached" if cached.get("status") != "unavailable" else "unavailable")

    cursor = cached.get("cursor")
    comments = {str(item["id"]): item for item in cached.get("comments", []) if item.get("id") is not None}
    total_seen = cached.get("total_seen", 0 if cursor is None else None)
    max_comment_id = cached.get("max_comment_id", 0)
    next_cursor = cursor
    pages_complete = False
    scan_started = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    watermark = (scan_started - datetime.timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    any_truncated = False
    try:
        page = 1
        while True:
            if cancel and cancel():
                from .process import Cancelled
                raise Cancelled("Issue comment retrieval cancelled")
            args = ["gh", "api", "--method", "GET", "repos/" + repo + "/issues/" + str(number) + "/comments",
                    "-f", "per_page=" + str(ISSUE_COMMENT_PAGE_SIZE), "-f", "page=" + str(page)]
            if cursor:
                args += ["-f", "since=" + cursor]
            rows = json.loads(checked(args, timeout=60))
            if not isinstance(rows, list):
                raise ValueError("Unexpected GitHub Issue comment response")
            for row in rows:
                if not isinstance(row, dict) or row.get("id") is None or not isinstance(row.get("body"), str):
                    raise ValueError("Unexpected GitHub Issue comment response")
                truncated = len(row["body"]) > ISSUE_COMMENT_BODY_LIMIT
                any_truncated = any_truncated or truncated
                comment = {"id": row["id"], "author": str((row.get("user") or {}).get("login", ""))[:100],
                           "body": row["body"][:ISSUE_COMMENT_BODY_LIMIT], "truncated": truncated,
                           "created_at": row.get("created_at", ""), "updated_at": row.get("updated_at", ""),
                           "url": row.get("html_url", "")}
                comment_id = int(comment["id"])
                if cursor is None:
                    total_seen = (total_seen or 0) + 1
                elif (comment_id > max_comment_id and comment["created_at"] and
                      comment["created_at"] >= cursor):
                    total_seen = None if total_seen is None else total_seen + 1
                max_comment_id = max(max_comment_id, comment_id)
                comments[str(comment["id"])] = comment
                comments = dict(sorted(comments.items(), key=lambda pair: (pair[1].get("updated_at", ""), int(pair[0])), reverse=True)[:ISSUE_COMMENT_LIMIT])
            if len(rows) < ISSUE_COMMENT_PAGE_SIZE:
                pages_complete = True
                break
            page += 1
        retained = list(comments.values())
        total_seen = max(total_seen, len(retained)) if total_seen is not None else None
        next_cursor = max(cursor or "", watermark)
        complete = pages_complete and total_seen is not None and total_seen <= ISSUE_COMMENT_LIMIT and not any_truncated
        warnings = []
        if not pages_complete:
            warnings.append("Comment retrieval did not finish; the latest comments may be incomplete.")
        if total_seen is None or total_seen > ISSUE_COMMENT_LIMIT:
            warnings.append("Only the 20 most recently updated comments are included; older Issue comments are omitted.")
        if any_truncated or any(item.get("truncated") for item in retained):
            warnings.append("One or more comments were truncated at 4,000 characters.")
            complete = False
        warning = " ".join(warnings) or None
        entry = {"repository": repo, "cursor": next_cursor, "comments": retained, "total_seen": total_seen,
                 "max_comment_id": max_comment_id, "complete": complete, "pages_complete": pages_complete, "warning": warning,
                 "issue_updated_at": issue_evidence.get("updated"), "status": "loaded", "checked_at": now, "last_success_at": now}
        store.put_setting(key, entry)
        return _issue_comment_response(entry, "loaded")
    except Exception as exc:
        from .process import Cancelled
        if isinstance(exc, Cancelled):
            raise
        status = "permission_denied" if "HTTP 403" in str(exc) else "authentication_required" if "HTTP 401" in str(exc) else "unavailable"
        warning = {"permission_denied": "GitHub denied access to Issue comments; comment context is missing.",
                   "authentication_required": "GitHub authentication is required to read Issue comments; comment context is missing.",
                   "unavailable": "Issue comments could not be read completely; comment context may be incomplete."}[status]
        entry = {**cached, "repository": repo, "issue_updated_at": issue_evidence.get("updated"),
                 "checked_at": now, "status": "unavailable", "warning": warning,
                 "complete": False, "failure_code": status}
        store.put_setting(key, entry)
        return _issue_comment_response(entry, "unavailable")


def _issue_comment_response(entry, status):
    return {"status": status, "comments": entry.get("comments", []), "complete": bool(entry.get("complete", False)),
            "pages_complete": bool(entry.get("pages_complete", False)), "omitted_count":
            max(0, entry["total_seen"] - len(entry.get("comments", []))) if isinstance(entry.get("total_seen"), int) else None,
            "warning": entry.get("warning"), "failure_code": entry.get("failure_code"), "updated_at": entry.get("cursor"),
            "fetched_at": entry.get("last_success_at")}


def sync(runtime, project, config, checked, cancel=None):
    repo = config["github"]["repository"]
    if not repo:
        raise ValueError("No GitHub repository configured")
    store = runtime.store
    key = "issue-sync:" + project["id"]
    progress = store.setting(key, {})
    from .config import fingerprint
    policy = fingerprint({"labels": config["github"]["labels"], "exclude": config["github"]["exclude_labels"]})
    if progress.get("repository") != repo or progress.get("policy") != policy:
        progress = {"repository": repo, "policy": policy}
    if not progress.get("started"):
        progress.update(started=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), page=1)
    consumed, complete = 0, False
    pending = progress.get("pending", {})

    def apply(issue):
        nonlocal consumed
        if "pull_request" in issue:
            return
        number = int(issue["number"])
        held = pending.pop(str(number), None)
        existing = store.one("SELECT * FROM tasks WHERE project=? AND fingerprint=?", (project["id"], "issue:" + str(number)))
        previous = json.loads(existing["evidence"]) if existing else []
        revised = not previous or previous[0].get("updated") != issue["updated_at"]
        labels = {label["name"] if isinstance(label, dict) else label for label in issue["labels"]}
        allowed = not set(config["github"]["exclude_labels"]) & labels and (
            not config["github"]["labels"] or set(config["github"]["labels"]) <= labels)
        if issue["state"].lower() != "open" or not allowed:
            if existing and existing["state"] in ["queued", "deferred", "blocked", "waiting_budget", "rejected", "insufficient_evidence", "failed", "interrupted"]:
                store.execute("UPDATE tasks SET state='obsolete',updated=? WHERE id=?", (time.time(), existing["id"]))
            elif existing and existing["state"] not in ["pr_open", "merged", "completed", "obsolete", "pr_closed", "reverted", "superseded"]:
                pending[str(number)] = {k: issue[k] for k in ["number", "updated_at", "html_url", "state", "labels"]}
                pending[str(number)].update(title=issue["title"][:300], body=(issue["body"] or "")[:30000])
                if not held or held["updated_at"] != issue["updated_at"]:
                    store.event("issue_revision", "Issue closed or excluded during execution; recheck before delivery", project["id"],
                                data={"task": existing["id"], "updated": issue["updated_at"]})
            return
        evidence = [{"number": number, "url": issue["html_url"], "updated": issue["updated_at"]}]
        if existing:
            if existing["state"] in ["queued", "deferred", "obsolete", "rejected", "blocked", "waiting_budget", "insufficient_evidence", "failed", "interrupted", "completed", "merged", "pr_closed"] and revised:
                store.execute("UPDATE tasks SET title=?,body=?,evidence=?,state='queued',available=0,updated=? WHERE id=?",
                    (issue["title"][:300], (issue["body"] or "")[:30000], encode(evidence), time.time(), existing["id"]))
            elif revised:
                # Keep active runs' evidence immutable. The new revision remains visible for next sync.
                pending[str(number)] = {k: issue[k] for k in ["number", "updated_at", "html_url", "state", "labels"]}
                pending[str(number)].update(title=issue["title"][:300], body=(issue["body"] or "")[:30000])
                if not held or held["updated_at"] != issue["updated_at"]:
                    store.event("issue_revision", "Issue changed during execution; recheck before delivery", project["id"],
                                data={"task": existing["id"], "updated": issue["updated_at"]})
        else:
            store.add_task(project["id"], "issue", "issue:" + str(number), issue["title"], issue["body"] or "", evidence, priority=60)
        consumed += 1

    for issue in list(pending.values()):
        apply(issue)
    # API 按页读取并持久化游标，不限制可同步的 Issue 总数。
    size = 100
    while True:
        if cancel and cancel():
            from .process import Cancelled
            raise Cancelled("Issue sync cancelled; continuation retained")
        args = ["gh", "api", "--method", "GET", "repos/" + repo + "/issues", "-f", "state=all",
                "-f", "sort=created", "-f", "direction=asc", "-f", "per_page=" + str(size),
                "-f", "page=" + str(progress["page"])]
        if progress.get("since"):
            args += ["-f", "since=" + progress["since"]]
        # A fixed since filter and creation order keep edits from moving existing rows.
        # The next cycle overlaps this cycle start to catch newly matching older Issues.
        rows = json.loads(checked(args, timeout=60))
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected GitHub Issue response")
        for issue in rows:
            apply(issue)
        progress["page"] += 1
        if len(rows) < size:
            complete = True
            # Overlap catches updates made while this cycle was draining.
            progress = {"repository": repo, "policy": policy, "since": progress["started"], "page": 1}
        progress["pending"] = pending
        store.put_setting(key, progress)
        if complete:
            break
    store.event("issues", str(consumed) + " Issue updates synchronized", project["id"],
                data={"complete": complete, "next_page": progress["page"]})


def problem_title(title):
    return " ".join(title.split()).casefold()


def finding_identity(finding):
    from .config import fingerprint
    # 同一行可能包含多个缺陷；代码引用本身不能证明它们是同一个问题。
    return fingerprint({"path": finding["path"], "quote": finding["quote"],
                        "title": problem_title(finding["title"])})


def related_task(backlog, finding):
    """Only collapse the same problem with matching source anchors."""
    title = problem_title(finding["title"])
    for row in backlog:
        if not title or title != problem_title(row["title"]):
            continue
        evidence = json.loads(row["evidence"])
        if any(e.get("path") == finding["path"] and e.get("quote") == finding["quote"] for e in evidence):
            return row["id"]
    return None


def related_work(store, project, task, limit=12):
    """Provide evidence of overlapping work without merging distinct reports."""
    from .context import terms
    evidence = json.loads(task.get("evidence") or "[]")
    paths = {item["path"] for item in evidence if item.get("path")}
    words = terms(task["title"] + " " + task.get("body", ""))
    def ranked(rows):
        for row in rows:
            result = json.loads(row["result"] or "{}")
            anchors = json.loads(row["evidence"] or "[]")
            other_paths = {item["path"] for item in anchors if item.get("path")}
            other_paths.update(result.get("files", []))
            shared = sorted(paths & other_paths)
            overlap = len(words & terms(row["title"]))
            if not shared and (not words or overlap < 2):
                continue
            yield (len(shared) * 10 + overlap, row["updated"],
                   {"id": row["id"], "title": row["title"], "state": row["state"],
                    "shared_paths": shared, "summary": str(result.get("summary", ""))[:1500],
                    "pr": result.get("pr"), "base": result.get("base")})

    # 流式遍历完整历史，仅保留最高分结果，并列时保持原顺序。
    with store.connect() as db:
        rows = db.execute("SELECT t.id,t.title,t.state,t.evidence,t.updated,"
                          "(SELECT result FROM runs WHERE task=t.id ORDER BY started DESC,id LIMIT 1) AS result "
                          "FROM tasks t WHERE project=? AND id<>? AND source NOT LIKE 'system_%' "
                          "AND state NOT IN ('rejected','obsolete','cancelled','superseded') ORDER BY updated DESC",
                          (project, task["id"]))
        order = lambda item: (-item[0], -item[1])
        selected = (sorted(ranked(rows), key=order)[:limit] if limit < 0 else
                    heapq.nsmallest(limit, ranked(rows), key=order))
    return [item[2] for item in selected]


def value_priority(value, risk):
    """Transparent ordering bands; these are policy weights, not probabilities."""
    benefit = {"low": 10, "medium": 30, "high": 50}[value["impact"]]
    cost = {"low": 0, "medium": 8, "high": 16}[value["effort"]]
    uncertainty = {"high": 0, "medium": 8, "low": 25}[value["confidence"]]
    return max(1, min(99, 40 + benefit - cost - uncertainty - {"low": 0, "medium": 5, "high": 10}[risk]))
