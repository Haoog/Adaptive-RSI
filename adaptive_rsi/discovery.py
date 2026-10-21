"""Bounded Issue synchronization with a durable, project-scoped continuation."""
import datetime
import json
import time

from .store import encode


def sync(runtime, project, config, checked):
    repo = config["github"]["repository"]
    if not repo:
        raise ValueError("No GitHub repository configured")
    store = runtime.store
    key = "issue-sync:" + project["id"]
    progress = store.setting(key, {})
    from .config import fingerprint
    policy = fingerprint({"labels": config["github"]["labels"], "exclude": config["github"]["exclude_labels"],
                          "size": min(100, config["github"]["max_issues"])})
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
    # Drain a bounded number of pages per turn, then continue on the next scheduled run.
    size = min(100, config["github"]["max_issues"])
    for _ in range(max(1, config["github"]["max_issues"] // size)):
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


def related_task(backlog, finding):
    """Only collapse exact problem titles with matching source anchors."""
    from .context import terms
    title = terms(finding["title"])
    for row in backlog:
        if not title or title != terms(row["title"]):
            continue
        evidence = json.loads(row["evidence"])
        if any(e.get("path") == finding["path"] and e.get("quote") == finding["quote"] for e in evidence):
            return row["id"]
    return None


def value_priority(value, risk):
    """Transparent ordering bands; these are policy weights, not probabilities."""
    benefit = {"low": 10, "medium": 30, "high": 50}[value["impact"]]
    cost = {"low": 0, "medium": 8, "high": 16}[value["effort"]]
    uncertainty = {"high": 0, "medium": 8, "low": 25}[value["confidence"]]
    return max(1, min(99, 40 + benefit - cost - uncertainty - {"low": 0, "medium": 5, "high": 10}[risk]))
