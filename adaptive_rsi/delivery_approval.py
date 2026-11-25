"""Bind a publication grant to the evaluated version and expose its receipts."""
import json

from .config import fingerprint


RUN_FIELDS = ("task", "project", "base", "result", "config", "path")
TASK_FIELDS = ("id", "project", "source", "fingerprint", "title", "body", "evidence", "acceptance", "overrides")
APPROVAL_PREFIX = "delivery-approval:"


def object_value(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def version(run, generation, target):
    result = object_value(run["result"])
    patch = result.get("patch_hash")
    if not isinstance(patch, str) or not patch:
        return None  # Historical runs without a version still face the existing publish gates.
    if generation:
        return {"generation": generation["generation"], "commit": generation["commit"],
                "base": generation["parent"], "tree": generation["tree"],
                "patch": generation["patch_hash"], "verification": generation["evidence_hash"],
                "target": target}
    verification = fingerprint({key: result.get(key) for key in ("evaluation", "review")})
    # An original candidate is identified by base + patch. Its first delivery
    # commit may be created after approval, so its head is shown in the receipt.
    return {"generation": None, "base": run["base"], "patch": patch,
            "verification": verification, "target": target}


def verification_contract(config):
    return fingerprint({key: config.get(key) for key in ('validation', 'checks', 'detected_checks', 'environment',
                                                        'sandbox', 'review', 'specification')})


def approval_version(run, task, generation, target, config):
    candidate = version(run, generation, target)
    if candidate is None:
        return None
    return {"candidate": candidate, "run_evidence": fingerprint({key: run[key] for key in RUN_FIELDS}),
            "task_evidence": fingerprint({key: task[key] for key in TASK_FIELDS}),
            "contract": verification_contract(config), "repository": config['github']['repository'],
            "branch": config['delivery']['base_branch']}


def authority(grant):
    if (isinstance(grant, dict) and grant.get('authorization') in ('per_candidate', 'project_policy') and
            (set(grant) == {'authorization'} or
             (set(grant) == {'authorization', 'version_bound'} and grant['version_bound'] is True))):
        return grant['authorization']
    return None


def setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    value = object_value(row[0])
    if not value:
        raise ValueError("Delivery authorization or version record is malformed")
    return value


def details(store, run, config):
    run_id = run["id"]
    records = {row["key"].split(":", 1)[0]: object_value(row["value"]) for row in store.query(
        "SELECT key,value FROM settings WHERE key IN (?,?,?,?,?,?)",
        tuple(prefix + run_id for prefix in (APPROVAL_PREFIX, "delivery-generation:", "run-baseline:",
                                             "delivery:", "delivery-push:", "delivery-generation-pr:")))}
    approval = records.get("delivery-approval")
    receipt = records.get("delivery")
    result = object_value(run["result"])
    generation = records.get("delivery-generation")
    task = store.one("SELECT * FROM tasks WHERE id=?", (run["task"],))
    try:
        current = approval_version(run, task, generation, records.get("run-baseline"), config) if task else None
    except KeyError:
        current = None
    if not any((current, approval, receipt, generation)):
        return None
    if receipt and result.get("pr"):
        state = "recorded"
        event = store.one("SELECT created FROM events WHERE run=? AND kind='pr' ORDER BY seq LIMIT 1", (run_id,))
        receipt = {**receipt, "pr": result["pr"], "merged_sha": result.get("merged_sha"),
                   "time": event['created'] if event else None}
    elif records.get("delivery-push") or records.get("delivery-generation-pr"):
        state = "reconciliation"
    elif approval and (run["state"] != "awaiting_approval" or approval.get("version") != current):
        state = "invalidated"
    elif approval:
        state = "approved"
    else:
        state = "awaiting_approval" if run["state"] == "awaiting_approval" else None
    return {"candidate": current["candidate"] if current else None,
            "revision": fingerprint(current) if current else None, "approval": approval, "state": state,
            "receipt": receipt if state == 'recorded' else None}
