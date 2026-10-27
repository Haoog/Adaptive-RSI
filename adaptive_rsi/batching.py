"""Explicit task batching with conflict previews and per-item receipts."""
import json
import time

from .config import fingerprint
from .store import encode, uid


def preserve_acceptance(task, plan):
    """Keep every original criterion in a grouped task's downstream plan."""
    if not (task.get("fingerprint") or "").startswith("batch:") or plan.get("decision") != "ready":
        return
    try:
        required = json.loads(task["acceptance"] or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid grouped task acceptance criteria") from exc
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError("Invalid grouped task acceptance criteria")
    plan["acceptance"] = list(dict.fromkeys([*plan["acceptance"], *required]))


def execute(runtime, project, tasks, action, request=None, preview=True, expected=None):
    if (not isinstance(tasks, list) or not 1 <= len(tasks) <= 100 or
            any(not isinstance(task, str) for task in tasks) or len(set(tasks)) != len(tasks)):
        raise ValueError("Batch requires 1..100 unique task IDs")
    if action not in ("retry", "cancel", "approve", "group", "ungroup"):
        raise ValueError("Unknown batch action")
    runtime.project(project)
    with runtime.dispatch_lock:
        rows = [runtime.store.one("SELECT * FROM tasks WHERE id=? AND project=?", (task, project)) for task in tasks]
        if any(row is None for row in rows):
            raise ValueError("Batch task not found in project")
        conflicts, scopes = [], {}
        for row in rows:
            paths = set()
            try:
                for evidence in json.loads(row["evidence"] or "[]"):
                    if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
                        paths.add(evidence["path"])
            except (ValueError, TypeError):
                pass
            scopes[row["id"]] = paths
        for index, row in enumerate(rows):
            for other in rows[index + 1:]:
                shared = scopes[row["id"]] & scopes[other["id"]]
                if shared:
                    conflicts.append({"tasks": [row["id"], other["id"]], "paths": sorted(shared)})
        signature = fingerprint({"action": action, "tasks": sorted(tasks), "project": project})
        report = {"action": action, "tasks": [{"id": row["id"], "state": row["state"], "title": row["title"]} for row in rows],
                  "conflicts": conflicts, "unknown_scope": [task for task in tasks if not scopes[task]],
                  "signature": signature}
        if action == "approve":
            candidates = []
            for row in rows:
                run = runtime.store.one("SELECT id,result FROM runs WHERE task=? AND project=? AND state='awaiting_approval' ORDER BY started DESC LIMIT 1", (row["id"], project))
                try:
                    result = json.loads(run["result"]) if run else {}
                    if not isinstance(result, dict):
                        result = {}
                except (TypeError, ValueError):
                    result = {}
                files = result.get("files", [])
                files = [path for path in files if isinstance(path, str)] if isinstance(files, list) else []
                candidates.append({"task": row["id"], "run": run["id"] if run else None,
                                   "risk": result.get("risk"), "files": files[:20],
                                   "files_truncated": len(files) > 20, "patch_hash": result.get("patch_hash")})
            report["candidates"] = candidates
            report["revision"] = fingerprint({"tasks": [(row["id"], row["state"]) for row in rows], "candidates": candidates})
        if preview:
            return report
        if not isinstance(request, str) or not 1 <= len(request) <= 100:
            raise ValueError("Batch requires a request ID")
        key = "batch:" + project + ":" + request
        prior = runtime.store.setting(key)
        if prior:
            if prior["signature"] != signature:
                raise ValueError("Batch request ID reused with different tasks")
            if action in ("group", "ungroup") or prior.get("complete"):
                return prior
            report = prior
        elif action == "approve" and expected != report["revision"]:
            raise ValueError("Batch approval preview changed; inspect it again")
        if action == "ungroup":
            if len(rows) != 1 or rows[0]["state"] != "queued":
                raise ValueError("Only one queued grouped task can be restored")
            try:
                evidence = json.loads(rows[0]["evidence"] or "[]")
                originals = evidence[0]["batch_tasks"]
                if (not rows[0]["fingerprint"].startswith("batch:") or not isinstance(originals, list) or
                        not originals or any(not isinstance(item, str) for item in originals) or
                        len(set(originals)) != len(originals)):
                    raise ValueError("Invalid batch evidence")
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise ValueError("Task has no valid grouping evidence") from exc
            now = time.time()
            with runtime.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM runs WHERE task=?", (rows[0]["id"],)).fetchone():
                    raise ValueError("Grouped task has already started")
                if db.execute("UPDATE tasks SET state='cancelled',updated=? WHERE id=? AND state='queued'",
                              (now, rows[0]["id"])).rowcount != 1:
                    raise ValueError("Grouped task changed during restore")
                for original in originals:
                    if db.execute("UPDATE tasks SET state='queued',updated=? WHERE id=? AND project=? AND state='superseded'",
                                  (now, original, project)).rowcount != 1:
                        raise ValueError("Original task changed during restore")
                report.update(complete=True, receipts=[{"id": task, "state": "restored"} for task in originals])
                db.execute("INSERT INTO events(project,kind,message,data,created) VALUES (?,'tasks_ungrouped','Grouped tasks restored',?,?)",
                           (project, encode(report), now))
                db.execute("INSERT INTO settings VALUES (?,?)", (key, encode(report)))
        elif action == "group":
            # The grouped goal is planned and verified as a whole; original requirements remain evidence.
            if any(row["state"] != "queued" or row["source"] != "goal" for row in rows):
                raise ValueError("Only queued user tasks can be grouped")
            overrides = [json.loads(row["overrides"] or "{}") for row in rows]
            if any(value != overrides[0] for value in overrides[1:]):
                raise ValueError("Grouped tasks must use identical overrides")
            acceptance = []
            for row in rows:
                criteria = json.loads(row["acceptance"] or "[]")
                if not isinstance(criteria, list):
                    raise ValueError("Invalid task acceptance criteria")
                acceptance.extend(criteria)
            if any(runtime.store.one("SELECT 1 FROM task_dependencies WHERE task=? OR dependency=?", (task, task)) for task in tasks):
                raise ValueError("Resolve explicit dependencies before grouping")
            body = "\n\n".join("[" + row["id"] + "] " + row["title"] + "\n" + (row["body"] or "") +
                                  "\nAcceptance: " + (row["acceptance"] or "[]") for row in rows)
            if len(body) > 30000:
                raise ValueError("Grouped requirements exceed 30000 characters")
            group = uid()
            now = time.time()
            with runtime.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO tasks(id,project,source,fingerprint,title,body,evidence,acceptance,state,priority,overrides,created,updated) "
                           "VALUES (?,?,?,?,?,?,?,?,'queued',100,?,?,?)",
                           (group, project, "goal", "batch:" + request, rows[0]["title"], body,
                            encode([{"batch_tasks": tasks, "conflicts": conflicts,
                                     "originals": [{"id": row["id"], "source": row["source"],
                                                    "evidence": json.loads(row["evidence"] or "[]"),
                                                    "acceptance": json.loads(row["acceptance"] or "[]")}
                                                   for row in rows]}]), encode(acceptance),
                            encode(overrides[0]), now, now))
                for row in rows:
                    changed = db.execute("UPDATE tasks SET state='superseded',updated=? WHERE id=? AND state='queued'", (now, row["id"]))
                    if changed.rowcount != 1:
                        raise ValueError("Batch task changed during grouping")
                report.update(id=group, complete=True, receipts=[{"id": task, "state": "grouped", "group": group} for task in tasks])
                db.execute("INSERT INTO events(project,kind,message,data,created) VALUES (?,'tasks_grouped','Tasks combined for shared verification',?,?)",
                           (project, encode(report), now))
                db.execute("INSERT INTO settings VALUES (?,?)", (key, encode(report)))
        else:
            if not prior:
                report["receipts"] = []
                runtime.store.put_setting(key, report)
            report["receipts"] = []
            for row in rows:
                receipt_key = key + ":" + row["id"]
                receipt = runtime.store.setting(receipt_key)
                if receipt and receipt.get("state") == "executing":
                    receipt = {"id": row["id"], "state": "unknown", "error_code": "batch_result_unknown"}
                if receipt is None:
                    runtime.store.put_setting(receipt_key, {"id": row["id"], "state": "executing"})
                    try:
                        if action == "retry":
                            runtime.retry(row["id"])
                        elif action == "cancel":
                            runtime.cancel_task(row["id"])
                        else:
                            candidate = next((item for item in report["candidates"] if item["task"] == row["id"]), None)
                            if not candidate or not candidate["run"]:
                                raise ValueError("No verified candidate awaiting approval")
                            runtime.deliver(candidate["run"])
                        receipt = {"id": row["id"], "state": "accepted"}
                    except ValueError:
                        receipt = {"id": row["id"], "state": "rejected", "error_code": "batch_item_ineligible"}
                    runtime.store.put_setting(receipt_key, receipt)
                report["receipts"].append(receipt)
                runtime.store.put_setting(key, report)
            report["complete"] = True
            runtime.store.put_setting(key, report)
        runtime.wake.set()
        return report
