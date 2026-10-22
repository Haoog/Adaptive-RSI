"""Bounded Issue synchronization with a durable, project-scoped continuation."""
import datetime
import json
import time

from .store import encode


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
            if existing and existing["state"] in ["queued", "deferred", "blocked", "waiting_budget", "rejected", "insufficient_evidence"]:
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
            if existing["state"] in ["queued", "deferred", "obsolete", "rejected", "blocked", "waiting_budget", "insufficient_evidence", "completed", "merged", "pr_closed"] and revised:
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
    rows = store.query("SELECT t.id,t.title,t.state,t.evidence,t.updated,"
                       "(SELECT result FROM runs WHERE task=t.id ORDER BY started DESC,id LIMIT 1) AS result "
                       "FROM tasks t WHERE project=? AND id<>? AND source NOT LIKE 'system_%' "
                       "AND state NOT IN ('rejected','obsolete','cancelled','superseded') ORDER BY updated DESC",
                       (project, task["id"]))
    ranked = []
    for row in rows:
        result = json.loads(row["result"] or "{}")
        anchors = json.loads(row["evidence"] or "[]")
        other_paths = {item["path"] for item in anchors if item.get("path")}
        other_paths.update(result.get("files", []))
        shared = sorted(paths & other_paths)
        overlap = len(words & terms(row["title"]))
        if not shared and (not words or overlap < 2):
            continue
        ranked.append((len(shared) * 10 + overlap, row["updated"],
                       {"id": row["id"], "title": row["title"], "state": row["state"],
                        "shared_paths": shared, "summary": str(result.get("summary", ""))[:1500],
                        "pr": result.get("pr"), "base": result.get("base")}))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in ranked[:limit]]


def value_priority(value, risk):
    """Transparent ordering bands; these are policy weights, not probabilities."""
    benefit = {"low": 10, "medium": 30, "high": 50}[value["impact"]]
    cost = {"low": 0, "medium": 8, "high": 16}[value["effort"]]
    uncertainty = {"high": 0, "medium": 8, "low": 25}[value["confidence"]]
    return max(1, min(99, 40 + benefit - cost - uncertainty - {"low": 0, "medium": 5, "high": 10}[risk]))
