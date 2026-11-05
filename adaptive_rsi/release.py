"""Pinned, explicitly authorized GitHub Actions delivery with durable reconciliation."""
import base64
import copy
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote, urlsplit

from .config import DEFAULTS, fingerprint, merge
from .gitops import origin_repository
from .process import Cancelled, checked
from .store import encode


KINDS = ("release", "deploy")
TERMINAL = {"workflow_succeeded", "failed", "dispatch_unknown", "blocked", "rerun_unknown"}
RERUN_CONFIRM_SECONDS = 300
RECONCILIATION_UNKNOWN = {"dispatch_unknown", "rerun_unknown", "reconciliation_blocked"}
# 活动回执连续对账失败的上限。达到后转入 reconciliation_blocked：停止自动轮询、
# 只保留一条错误事件，但仍允许只读的人工对账与关闭入口。
ADVANCE_FAILURE_LIMIT = 3
RECONCILIATION_AUDIT_LIMIT = 10
# A read-only query may read ten 100-run pages. Persist only the bounded
# evidence reconciliation reads (identity, status, timestamps) and cap the
# retained list, so one operation's settings never grow with the page size.
RECONCILIATION_CANDIDATE_LIMIT = 20
RECONCILIATION_SELECTABLE_LIMIT = 200
# A 404 on the workflow-scoped runs listing means the authorized workflow file
# is not resolvable on the default branch (renamed or deleted), not that the
# matching run is absent. Polling recovers automatically once the file is
# restored; the recheck is spaced so a missing file cannot flood GitHub or the
# event log. Operator-driven queries pass force=True and always re-check.
WORKFLOW_VISIBILITY_RECHECK_SECONDS = 900
WORKFLOW_NOT_VISIBLE_REASON = ("The authorized workflow file is not visible on the default branch; it may have been "
                               "renamed or deleted. Restore the file or re-authorize its replacement, then reconcile "
                               "the returned candidates; selecting never starts or repeats a run.")
RECONCILIATION_AUDIT_FIELDS = (
    "at", "source", "selected_run_id", "repository", "workflow", "ref",
    "operation_id", "sha", "workflow_sha", "candidate_count", "candidates", "previous",
)


class ReconciliationRejected(ValueError):
    """A fixed, user-safe refusal for manual workflow reconciliation."""


class ApiRejected(RuntimeError):
    """GitHub explicitly rejected a request before accepting its side effect."""

    def __init__(self, message, status, local=False):
        super().__init__(message)
        self.status = status
        # True 表示本机限流冷却在 `gh` 发出请求之前就拦下了它：GitHub 没有看到
        # 这次调用，也就谈不上拒绝，调用方应把它当成可自动重试的本地等待。
        self.local = local


_API_BACKOFF = {}
API_COOLDOWN_SECONDS = 60


def api(repo, endpoint, method="GET", fields=None):
    # Never sleep in the scheduler or automatically replay a write. All callers
    # of this transport share the primary/secondary limit cooldown.
    retry_at = _API_BACKOFF.get("github.com", 0)
    if time.monotonic() < retry_at:
        raise ApiRejected("GitHub rate limit cooldown active; request was not sent", 429, local=True)
    path = "repos/" + repo + ("/" + endpoint if endpoint else "")
    argv = ["gh", "api", path, "--method", method]
    for key, value in (fields or {}).items():
        argv += ["-f", key + "=" + str(value)]
    try:
        output = checked(argv)
    except RuntimeError as exc:
        status = re.search(r"\(HTTP (\d{3})\)", str(exc))
        if status and (int(status[1]) == 429 or int(status[1]) == 403 and
                       any(term in str(exc).lower() for term in ("rate limit", "abuse detection"))):
            _API_BACKOFF["github.com"] = time.monotonic() + API_COOLDOWN_SECONDS
        if status and 400 <= int(status[1]) < 500 and int(status[1]) != 408:
            raise ApiRejected(str(exc), int(status[1])) from exc
        raise
    try:
        return json.loads(output) if output.strip() else {}
    except ValueError:
        raise RuntimeError("gh api returned non-JSON output") from None


def workflow_content(repo, workflow, sha):
    data = api(repo, "contents/" + workflow + "?ref=" + quote(sha, safe=""))
    if data.get("encoding") != "base64" or data.get("type") != "file":
        raise ValueError("Workflow must be an existing repository file")
    return base64.b64decode(data["content"], validate=False).decode("utf-8")


def compatible_workflow(text):
    # Accept the deliberately small public contract; reject ambiguous YAML forms.
    if not re.search(r"(?m)^run-name:\s*['\"]?\$\{\{\s*inputs\.rsi_operation_id\s*\}\}['\"]?\s*(?:#.*)?$", text):
        return False
    def block(source, key, expected_indent=None):
        match = re.search(r"(?m)^([ ]*)" + key + r":[ ]*(?:#.*)?$", source)
        if not match or expected_indent is not None and len(match[1]) != expected_indent:
            return None
        indent = len(match[1])
        lines = []
        for line in source[match.end():].splitlines():
            if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
                break
            lines.append(line)
        return "\n".join(lines), indent
    events = block(text, r"['\"]?on['\"]?", 0)
    dispatch = block(events[0], "workflow_dispatch") if events else None
    inputs = block(dispatch[0], "inputs") if dispatch else None
    if not inputs:
        return False
    for name in ["rsi_operation_id", "rsi_sha"]:
        field = block(inputs[0], name)
        if (not field or field[1] <= inputs[1] or
                not re.search(r"(?m)^[ ]+required:[ ]*(?:true|'true'|\"true\")[ ]*(?:#.*)?$", field[0]) or
                not re.search(r"(?m)^[ ]+type:[ ]*(?:string|'string'|\"string\")[ ]*(?:#.*)?$", field[0])):
            return False
    return True


WORKFLOW_CHOICES_TTL = 300
_workflow_choices_cache = {}


def _validated_repository(runtime, project_id):
    """Return the configured repository only when it still matches the project origin."""
    project = runtime.project(project_id)
    repo = runtime.config(project_id)["github"]["repository"]
    if not repo or repo.lower() != origin_repository(project["path"]).lower():
        raise ValueError("Workflow repository must match the selected project's origin")
    return repo


def workflow_choices(runtime, project_id, refresh=False):
    repo = _validated_repository(runtime, project_id)
    info = api(repo, "")
    branch = info["default_branch"]
    sha = api(repo, "commits/" + quote(branch, safe=""))["sha"]
    cached = _workflow_choices_cache.get((repo, sha))
    now = time.time()
    if not refresh and cached and now - cached[0] < WORKFLOW_CHOICES_TTL:
        return cached[1]
    workflows = []
    for page in range(1, 11):
        batch = api(repo, "actions/workflows?per_page=100" + ("&page=" + str(page) if page > 1 else "")).get("workflows", [])
        if not isinstance(batch, list):
            raise ValueError("Invalid workflow directory response")
        workflows.extend(batch)
        if len(batch) < 100:
            break
    import concurrent.futures

    def inspect(item):
        path = item.get("path", "")
        if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path):
            return None
        try:
            compatible = item.get("state") == "active" and compatible_workflow(workflow_content(repo, path, sha))
        except (RuntimeError, ValueError, OSError):
            return {"workflow": path, "name": item.get("name", path), "compatible": False,
                    "reason": "Unable to inspect this workflow; refresh before authorizing"}
        return {"workflow": path, "name": item.get("name", path), "compatible": compatible,
                "reason": "" if compatible else "Requires explicit block-style YAML: workflow_dispatch inputs rsi_operation_id/rsi_sha (required string), and top-level run-name: ${{ inputs.rsi_operation_id }}; anchors, aliases and flow mappings require manual inspection"}
    # 逐个 workflow 拉取内容很慢；受控并发把对话框打开时间压到单次往返级别。
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        choices = [row for row in pool.map(inspect, workflows) if row]
    result = {"repository": repo, "sha": sha, "workflows": choices}
    # 缓存绑定精确的默认分支 SHA；分支前进后键自然失效。授权路径始终重新校验，不受缓存影响。
    has_transient_error = any(item.get("reason", "").startswith("Unable to inspect this workflow") for item in choices)
    if not has_transient_error:
        if len(_workflow_choices_cache) > 32:
            _workflow_choices_cache.clear()
        _workflow_choices_cache[(repo, sha)] = (now, result)
    else:
        _workflow_choices_cache.pop((repo, sha), None)
    return result


def authorize_workflow(runtime, project_id, kind, workflow):
    if kind not in KINDS:
        raise ValueError("Unknown automation kind")
    initial = runtime.config(project_id)
    repo = _validated_repository(runtime, project_id)
    # Authorization pins exactly one file, so validate only that file at the
    # default-branch SHA the tag binds. Enumerating the whole workflow directory
    # added one contents request per other workflow and no input to this
    # decision; the dialog path keeps using workflow_choices for the full list.
    if not isinstance(workflow, str) or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow):
        raise ValueError("Select a compatible existing workflow from this project")
    branch = api(repo, "")["default_branch"]
    sha = api(repo, "commits/" + quote(branch, safe=""))["sha"]
    try:
        compatible = compatible_workflow(workflow_content(repo, workflow, sha))
    except (RuntimeError, ValueError, OSError):
        compatible = False
    if not compatible:
        raise ValueError("Select a compatible existing workflow from this project")
    tag = "rsi-workflow/" + sha
    # GitHub rejects duplicate refs. Read before creation so repeated authorization is safe.
    refs = api(repo, "git/matching-refs/tags/" + tag)
    exact = [item for item in refs if item.get("ref") == "refs/tags/" + tag]
    if exact:
        if exact[0]["object"].get("type") != "commit" or exact[0]["object"].get("sha") != sha:
            raise ValueError("Trusted workflow tag points to a different commit")
    else:
        api(repo, "git/refs", "POST", {"ref": "refs/tags/" + tag, "sha": sha})
    with runtime.dispatch_lock:
        current = runtime.config(project_id)
        if current["github"]["repository"] != repo:
            raise ValueError("Workflow repository must match the selected project's origin")
        if current["delivery"][kind] != initial["delivery"][kind]:
            raise ValueError("Workflow authorization changed during inspection; refresh before authorizing")
        runtime.configure({"delivery": {kind: {"enabled": True, "workflow": workflow, "ref": tag, "sha": sha}}}, project_id)
        runtime.store.event("workflow_authorized", "Existing workflow explicitly authorized", project_id,
                            data={"kind": kind, "workflow": workflow, "ref": tag, "sha": sha})
        return runtime.config(project_id)["delivery"][kind]


def key(run_id, kind):
    return "automation:" + run_id + ":" + kind


def _read_automation(runtime, run_id, kind):
    diagnostic = (run_id, kind)
    row = runtime.store.one("SELECT value FROM settings WHERE key=?", (key(run_id, kind),))
    if row is None:
        with runtime.automation_receipt_failures_lock:
            runtime.automation_receipt_failures.pop(diagnostic, None)
        return None, None
    raw = row["value"]
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("state"), str) or not data["state"]:
            raise ValueError("Invalid automation state")
        if any(field in data and not isinstance(data[field], dict) for field in ("rollback", "reconciliation")):
            raise ValueError("Invalid automation details")
        active = {"dispatching", "submitted", "dispatch_unknown", "queued", "running", "rerunning", "rerun_unknown"}
        targets = [data] + ([data["rollback"]] if data.get("state") == "rolling_back" and "rollback" in data else [])
        if data.get("state") == "rolling_back" and "rollback" not in data:
            raise ValueError("Missing rollback details")
        for target in targets:
            if target.get("state") in active and any(not isinstance(target.get(field), str) or not target[field]
                    for field in ("repository", "workflow", "ref", "sha", "workflow_sha", "operation_id")):
                raise ValueError("Missing automation identity")
    except (ValueError, TypeError, RecursionError):
        data = {"state": "blocked", "receipt_error": "invalid_automation_receipt",
                "error": "Automation record is unreadable; repair the stored record before continuing"}
        with runtime.automation_receipt_failures_lock:
            if diagnostic not in runtime.automation_receipt_failures:
                run = runtime.store.one("SELECT project FROM runs WHERE id=?", (run_id,))
                runtime.store.event("automation_error", data["error"], run["project"] if run else None, run_id,
                                    {"kind": kind, "error_code": data["receipt_error"]})
                if len(runtime.automation_receipt_failures) >= 128:
                    runtime.automation_receipt_failures.pop(next(iter(runtime.automation_receipt_failures)))
                runtime.automation_receipt_failures[diagnostic] = True
    else:
        with runtime.automation_receipt_failures_lock:
            runtime.automation_receipt_failures.pop(diagnostic, None)
    return data, raw


def _require_automation(runtime, run_id, kind):
    data, _ = _read_automation(runtime, run_id, kind)
    if data and data.get("receipt_error"):
        raise ReconciliationRejected(data["error"])
    return data or {}


def normalized_policy(kind, policy):
    return merge(DEFAULTS["delivery"][kind], policy)


def pending(runtime, run):
    try:
        saved = json.loads(run["config"] or "{}")
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    delivery = saved.get("delivery") or {}
    live = runtime.config(run["project"])
    active = {"dispatching", "submitted", "dispatch_unknown", "queued", "running", "rerunning"}
    for kind in KINDS:
        if not delivery.get(kind, {}).get("enabled"):
            continue
        data, _ = _read_automation(runtime, run["id"], kind)
        data = data or {}
        if data.get("receipt_error"):
            continue
        if (normalized_policy(kind, live["delivery"][kind]) != normalized_policy(kind, delivery[kind]) or live["delivery"]["mode"] != "pr" or
                live["github"]["repository"] != saved.get("github", {}).get("repository")):
            if data.get("state") in active or data.get("state") == "rolling_back" and data.get("rollback", {}).get("state") in active:
                return True
            continue
        if kind == "deploy" and data.get("state") == "workflow_succeeded" and delivery[kind].get("health_url"):
            return True
        if data.get("state") == "health_failed":
            if data.get("rollback_unavailable") or not delivery[kind].get("automatic_rollback"):
                continue
        if kind == "deploy" and data.get("state") == "blocked" and data.get("blocked_by_release"):
            release, _ = _read_automation(runtime, run["id"], "release")
            release = release or {}
            if release.get("state") in {"failed", "rerun_unknown", "blocked", "reconciliation_blocked"}:
                continue
        if data.get("state") not in {"workflow_succeeded", "failed", "healthy", "health_unconfirmed", "rollback_health_unconfirmed", "rolled_back", "rollback_failed", "superseded", "rerun_unknown", "reconciliation_blocked"}:
            return True
    return False


def status(runtime, project_id, run_id):
    run = runtime.store.one("SELECT * FROM runs WHERE id=? AND project=?", (run_id, project_id))
    if not run:
        raise ValueError("Run not found in this project")
    return {kind: _read_automation(runtime, run_id, kind)[0] or {"state": "not_authorized"} for kind in KINDS}


_UNCONDITIONAL = object()


class _AutomationChanged(RuntimeError):
    """A concurrent action replaced the operation being reconciled."""


def _owner_record(value):
    """Only a dict naming the claiming run and its exact commit may own a target."""
    if not isinstance(value, dict):
        return None
    run, sha = value.get("run"), value.get("sha")
    if not isinstance(run, str) or not isinstance(sha, str) or not run or not sha:
        return None
    return value


def save(runtime, run, kind, data, expected=_UNCONDITIONAL, ownership=None, stable=None):
    with runtime.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if expected is not _UNCONDITIONAL:
            current = db.execute("SELECT value FROM settings WHERE key=?", (key(run["id"], kind),)).fetchone()
            if (current["value"] if current else None) != expected:
                raise _AutomationChanged("Automation changed during reconciliation")
        if ownership is not None:
            owner_name, previous, owner = ownership
            current = db.execute("SELECT value FROM settings WHERE key=?", (owner_name,)).fetchone()
            try:
                # A structurally invalid record is no ownership at all, matching
                # deployment_owner; the write below then repairs it.
                current_value = _owner_record(json.loads(current["value"])) if current and current["value"] else None
            except (TypeError, ValueError):
                current_value = None
            if current_value != previous:
                raise _AutomationChanged("Deployment ownership changed during reconciliation")
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (owner_name, encode(owner)))
        if stable is not None:
            stable_name, stable_val = stable
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (stable_name, encode(stable_val)))
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key(run["id"], kind), encode(data)))
        row = db.execute("SELECT result FROM runs WHERE id=?", (run["id"],)).fetchone()
        if row:
            try:
                result = json.loads(row["result"] or "{}")
            except (TypeError, ValueError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            result.setdefault("automation", {})[kind] = data
            db.execute("UPDATE runs SET result=? WHERE id=?", (encode(result), run["id"]))
    return data


def authorized(runtime, project, run, kind, policy, cancel=None):
    if run["project"] != project["id"]:
        raise ValueError("Automation run belongs to a different project")
    live = runtime.config(project["id"])
    try:
        saved = json.loads(run["config"] or "{}")
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if runtime.stop.is_set() or runtime.project(project["id"])["paused"] or (cancel and cancel()):
        raise Cancelled("Release/deploy paused or cancelled")
    identity = runtime.store.setting("delivery:" + run["id"], {})
    repo = live["github"]["repository"]
    effective = normalized_policy(kind, policy)
    if (live["delivery"]["mode"] != "pr" or not policy.get("enabled") or
            normalized_policy(kind, live["delivery"][kind]) != effective or
            normalized_policy(kind, saved.get("delivery", {}).get(kind, {})) != effective or
            repo != saved.get("github", {}).get("repository") or repo != identity.get("repository", repo) or
            repo.lower() != origin_repository(project["path"]).lower()):
        raise ValueError("Release/deploy authorization revoked or target changed")
    return repo


def validate_pin(repo, policy):
    tag = api(repo, "git/ref/tags/" + quote(policy["ref"], safe="/"))
    if tag.get("object", {}).get("type") != "commit" or tag["object"].get("sha") != policy["sha"]:
        raise ValueError("Trusted workflow tag changed; authorize the workflow again")
    if not compatible_workflow(workflow_content(repo, policy["workflow"], policy["sha"])):
        raise ValueError("Pinned workflow does not implement the RSI dispatch contract")


def _bounded_text(value, limit=500):
    return value[:limit] if isinstance(value, str) else None


def _run_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _candidate(repo, remote):
    """Normalize one GitHub workflow run into bounded, auditable evidence."""
    if not isinstance(remote, dict) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return None
    run_id = _run_number(remote.get("id"))
    if run_id is None:
        return None
    path = _bounded_text(remote.get("path")) or ""
    return {
        "id": run_id,
        # Construct the link from the authorized repository and numeric run id;
        # never trust a remote-provided URL for an operator-facing link.
        "url": "https://github.com/" + repo + "/actions/runs/" + str(run_id),
        "status": _bounded_text(remote.get("status"), 40) or "unknown",
        "conclusion": _bounded_text(remote.get("conclusion"), 40),
        "attempt": _run_number(remote.get("run_attempt")),
        "created_at": _bounded_text(remote.get("created_at"), 64),
        "updated_at": _bounded_text(remote.get("updated_at"), 64),
        "head_sha": _bounded_text(remote.get("head_sha"), 64),
        "head_branch": _bounded_text(remote.get("head_branch"), 300),
        "display_title": _bounded_text(remote.get("display_title"), 300),
        "workflow": path.split("@")[0] or None,
        "path": path or None,
        "event": _bounded_text(remote.get("event"), 40),
    }


def _candidate_identity_mismatch(data, candidate):
    """Return the first identity field that differs from the authorized operation."""
    if not isinstance(candidate, dict):
        return "candidate"
    if candidate.get("display_title") != data.get("operation_id"):
        return "operation_id"
    if candidate.get("event") != "workflow_dispatch":
        return "event"
    if candidate.get("workflow") != data.get("workflow"):
        return "workflow"
    if candidate.get("head_sha") != data.get("workflow_sha"):
        return "target_sha"
    if candidate.get("head_branch") != data.get("ref"):
        return "ref"
    return None


def _bounded_candidates(candidates):
    """Keep the leading candidates from the read-only query within the retained limit."""
    return [item for item in candidates if isinstance(item, dict)][:RECONCILIATION_CANDIDATE_LIMIT]


def _audit_entry(value):
    """Rebuild one manual-reconciliation audit from the bounded evidence schema.

    Older records also carried the untouched GitHub page; rebuilding the entry
    drops those raw payloads while keeping identity, status and timestamps.
    """
    if not isinstance(value, dict):
        return None
    entry = {field: value[field] for field in RECONCILIATION_AUDIT_FIELDS if field in value}
    candidates = value.get("candidates")
    candidates = [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []
    total = value.get("candidate_count")
    entry["candidates"] = candidates[:RECONCILIATION_CANDIDATE_LIMIT]
    entry["candidate_count"] = total if type(total) is int and total >= len(candidates) else len(candidates)
    previous = value.get("previous")
    entry["previous"] = previous if isinstance(previous, dict) else {}
    return entry


def _record_reconciliation(data, repo, remotes, state, reason, checked_at=None):
    found = [item for item in (_candidate(repo, remote) for remote in remotes or []) if item]
    candidates = _bounded_candidates(found)
    data["reconciliation"] = {
        "state": state,
        "candidate_count": len(found),
        "candidate_run_ids": [item["id"] for item in candidates],
        "candidates": candidates,
        "checked_at": checked_at if checked_at is not None else time.time(),
        "redispatch_allowed": False,
        "reason": reason,
    }
    return candidates


def _remote_runs(repo, data, force=False):
    visibility = data.get("remote_visibility") or {}
    if (not force and visibility.get("state") == "workflow_not_visible"
            and time.time() < visibility.get("next_check_at", 0)):
        return None
    endpoint = "actions/workflows/" + quote(data["workflow"].split("/")[-1], safe="") + "/runs?event=workflow_dispatch&per_page=100"
    found = []
    try:
        for page in range(1, 11):
            runs = api(repo, endpoint + "&page=" + str(page)).get("workflow_runs", [])
            found.extend(item for item in runs if isinstance(item, dict) and item.get("display_title") == data["operation_id"])
            if len(runs) < 100:
                break
    except ApiRejected as exc:
        if exc.status != 404:
            raise
        first = visibility.get("checked_at") if visibility.get("state") == "workflow_not_visible" else None
        data["remote_visibility"] = {"state": "workflow_not_visible", "http_status": 404,
                                     "workflow": data.get("workflow"), "checked_at": first or time.time(),
                                     "next_check_at": time.time() + WORKFLOW_VISIBILITY_RECHECK_SECONDS}
        return None
    data.pop("remote_visibility", None)
    return found


def find_remote(repo, data):
    found = _remote_runs(repo, data)
    if found is None:
        _record_reconciliation(data, repo, [], "workflow_not_visible", WORKFLOW_NOT_VISIBLE_REASON,
                               checked_at=(data.get("remote_visibility") or {}).get("checked_at"))
        return None
    if len(found) > 1:
        _record_reconciliation(data, repo, found, "manual_inspection_required",
                               "Multiple workflow runs match this operation ID. Compare repository, workflow, ref, exact target SHA and operation ID before selecting one.")
        return None
    if found:
        _record_reconciliation(data, repo, found, "matched",
                               "One matching workflow run is visible. Reading its status never starts another run.")
        return found[0]
    _record_reconciliation(data, repo, [], "awaiting_remote_visibility",
                           "GitHub has not published a matching workflow run yet. Wait and query again; manual run IDs are not accepted.")
    return None


def _existing_remote(repo, data):
    try:
        remote = api(repo, "actions/runs/" + str(data["workflow_run_id"]))
    except ApiRejected as exc:
        if exc.status != 404:
            raise
        data["remote_visibility"] = {"state": "run_not_visible", "http_status": 404,
                                     "workflow_run_id": data["workflow_run_id"]}
        if data.get("state") in {"rerunning", "rerun_unknown"} or data.get("rerun_pending"):
            data["state"] = "rerun_unknown"
        elif data.get("state") in {"submitted", "queued", "running", "dispatching", "dispatch_unknown"}:
            data["state"] = "dispatch_unknown"
        return None
    data.pop("remote_visibility", None)
    return remote


def reconcile(runtime, run, kind, data, persist=True):
    repo = data["repository"]
    remote = (_existing_remote(repo, data) if data.get("workflow_run_id")
              else find_remote(repo, data))
    if not remote:
        return save(runtime, run, kind, data) if persist else data
    if (remote.get("head_sha") != data["workflow_sha"] or remote.get("display_title") != data["operation_id"] or
            remote.get("event") != "workflow_dispatch" or remote.get("path", "").split("@")[0] != data["workflow"]):
        raise ValueError("Workflow run identity differs from the authorized operation")
    if data.get("rerun_pending") or data.get("state") in {"rerunning", "rerun_unknown"}:
        if remote.get("run_attempt", 1) <= data.get("previous_attempt", 1):
            requested = data.setdefault("rerun_requested_at", data.get("checked_at", time.time()))
            data.update(rerun_pending=True, checked_at=time.time())
            if time.time() - requested >= RERUN_CONFIRM_SECONDS:
                data.update(state="rerun_unknown", error="GitHub has not confirmed a new run attempt; inspect the original run. Reconciliation never repeats an uncertain rerun request.")
            # 只读对账已确认的 rerun_confirmed / manual_inspection_required 证据，
            # 不能被一次回落到旧 attempt 的轮询覆写。
            if (data.get("reconciliation") or {}).get("state") not in {"rerun_confirmed", "manual_inspection_required"}:
                _record_reconciliation(data, repo, [remote],
                                       "rerun_unconfirmed" if data.get("state") == "rerun_unknown" else "rerun_pending",
                                       "GitHub has not confirmed a new run attempt. Inspect the original run; reconciliation never repeats an uncertain rerun request.")
            return save(runtime, run, kind, data) if persist else data
        data.update(rerun_pending=False, rerun_receipt="confirmed", error="")
    _record_reconciliation(data, repo, [remote], "matched",
                           "Workflow run status was read from GitHub; this never starts another run.")
    from datetime import datetime
    def timestamp(name):
        try:
            return datetime.fromisoformat(remote[name].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return None
    created, started = timestamp("created_at"), timestamp("run_started_at")
    finished = timestamp("updated_at") if remote.get("status") == "completed" else None
    # Reconciled timing covers queue, execution, build and reverification phases.
    # Missing phases stay explicit None (unknown) rather than omitted or defaulted to 0;
    # GitHub workflow_run timestamps do not isolate build or reverification phases.
    data["timing"] = {"created_at": remote.get("created_at"), "started_at": remote.get("run_started_at"),
                      "completed_at": remote.get("updated_at") if finished is not None else None,
                      "completed_at_basis": "updated_at" if finished is not None else None,
                      "queue_seconds": started - created if created is not None and started is not None and started >= created else None,
                      "execution_seconds": finished - started if started is not None and finished is not None and finished >= started else None,
                      "build_seconds": None, "reverification_seconds": None}
    data.update(workflow_run_id=remote["id"], url=remote.get("html_url", ""),
                attempt=remote.get("run_attempt", 1), checked_at=time.time(), conclusion=remote.get("conclusion"))
    if remote.get("status") == "completed":
        data["state"] = "workflow_succeeded" if remote.get("conclusion") == "success" else "failed"
        if kind == "deploy":
            data["health"] = "unverified"
    else:
        data["state"] = "running" if remote.get("status") == "in_progress" else "queued"
    return save(runtime, run, kind, data) if persist else data


def _reconciliation_operation(runtime, project_id, run_id, kind, target):
    if kind not in KINDS:
        raise ReconciliationRejected("Unknown automation kind")
    if target not in {"operation", "rollback"}:
        raise ReconciliationRejected("Unknown reconciliation target")
    run = runtime.store.one("SELECT * FROM runs WHERE id=? AND project=? AND state='merged'", (run_id, project_id))
    if not run:
        raise ReconciliationRejected("Only a verified merged run in this project can be reconciled")
    try:
        saved = json.loads(run["config"])
        policy = saved["delivery"].get(kind, {})
    except (TypeError, ValueError, KeyError, AttributeError):
        raise ReconciliationRejected("This run has no valid delivery authorization to reconcile") from None
    data, raw = _read_automation(runtime, run_id, kind)
    if data and data.get("receipt_error"):
        raise ReconciliationRejected(data["error"])
    if not isinstance(data, dict) or not data:
        raise ReconciliationRejected("This operation has no delivery record to reconcile")
    if target == "rollback":
        rollback = data.get("rollback")
        if kind != "deploy" or not isinstance(rollback, dict):
            raise ReconciliationRejected("This operation has no rollback record to reconcile")
        return runtime.project(project_id), run, saved, policy, data, rollback, raw
    return runtime.project(project_id), run, saved, policy, data, data, raw


def _reconciliation_repository(runtime, project, saved, data):
    """Bind the operation to the saved, live and origin repository identities."""
    repo = data.get("repository")
    try:
        live = runtime.config(project["id"])
        saved_repo = (saved.get("github") or {}).get("repository")
        live_repo = (live.get("github") or {}).get("repository")
        origin = origin_repository(project["path"])
    except (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError):
        raise ReconciliationRejected("Workflow run identity differs from the authorized operation") from None
    if (not isinstance(repo, str) or not repo or repo != saved_repo or repo != live_repo or
            repo.lower() != (origin or "").lower()):
        raise ReconciliationRejected("Workflow run identity differs from the authorized operation")
    return repo


def _require_unknown(target_data):
    if target_data.get("state") not in RECONCILIATION_UNKNOWN:
        raise ReconciliationRejected("Manual reconciliation is only available while a workflow start or retry is unconfirmed")


def _reconciliation_view(kind, target, data, repo, selectable=None):
    reconciliation = data.get("reconciliation") if isinstance(data.get("reconciliation"), dict) else {}
    total = reconciliation.get("candidate_count")
    if total is None or not isinstance(total, int):
        total = len(selectable) if isinstance(selectable, list) else len(reconciliation.get("candidates") or [])
    view = {
        "kind": kind,
        "target": target,
        "state": data.get("state"),
        "reconciliation_state": reconciliation.get("state"),
        "repository": repo,
        "workflow": data.get("workflow"),
        "ref": data.get("ref"),
        "operation_id": data.get("operation_id"),
        "sha": data.get("sha"),
        "workflow_sha": data.get("workflow_sha"),
        "checked_at": reconciliation.get("checked_at"),
        "reason": reconciliation.get("reason") or data.get("error", ""),
        "candidate_count": total,
        "candidates": reconciliation.get("candidates") or [],
        "manual_reconciliation": data.get("manual_reconciliation"),
        # Abandoning is the only exit once GitHub stopped exposing the workflow
        # (404 on the listing or on the recorded run).
        "can_abandon": bool(target == "operation" and data.get("state") in RECONCILIATION_UNKNOWN and
                            (data.get("remote_visibility") or {}).get("state") in
                            {"workflow_not_visible", "run_not_visible"}),
    }
    if selectable is not None:
        view["selectable_candidates"] = [item for item in selectable if isinstance(item, dict)][:RECONCILIATION_SELECTABLE_LIMIT]
    return view


def candidates(runtime, project_id, run_id, kind, target="operation"):
    """Read-only re-query of the runs GitHub already exposes for this operation."""
    with runtime.automation_lock_for(project_id, kind):
        project, run, saved, policy, data, target_data, original = _reconciliation_operation(
            runtime, project_id, run_id, kind, target)
        repo = _reconciliation_repository(runtime, project, saved, target_data)
        _require_unknown(target_data)
        existing = _run_number(target_data.get("workflow_run_id"))
        if existing:
            remote = _existing_remote(repo, target_data)
            remotes = [remote] if isinstance(remote, dict) else []
        else:
            remotes = _remote_runs(repo, target_data, force=True) or []
        normalized = [item for item in (_candidate(repo, remote) for remote in remotes) if item]
        previous = _run_number(target_data.get("previous_attempt")) or _run_number(target_data.get("attempt")) or 1
        remote_visibility = (target_data.get("remote_visibility") or {}).get("state")
        if remote_visibility == "run_not_visible":
            state = "run_not_visible"
            reason = "Workflow run is not visible; check repository access before reconciling"
        elif remote_visibility == "workflow_not_visible":
            state = "workflow_not_visible"
            reason = WORKFLOW_NOT_VISIBLE_REASON
        elif existing:
            attempt = normalized[0].get("attempt") if normalized else None
            if attempt is not None and attempt > previous:
                state = "rerun_confirmed"
                reason = "GitHub now reports a new run attempt. Selecting it only reads its status and never repeats the rerun request."
            else:
                state = "rerun_unconfirmed"
                reason = "GitHub has not confirmed a new run attempt. Inspect the original run; reconciliation never repeats an uncertain rerun request."
        elif len(normalized) > 1:
            state = "manual_inspection_required"
            reason = "Multiple workflow runs match this operation ID. Compare repository, workflow, ref, exact target SHA and operation ID before selecting one."
        elif normalized:
            state = "matched"
            reason = "One matching workflow run is visible. Reading its status never starts another run."
        else:
            state = "awaiting_remote_visibility"
            reason = "GitHub has not published a matching workflow run yet. Wait and query again; manual run IDs are not accepted."
        _record_reconciliation(target_data, repo, remotes, state, reason)
        try:
            save(runtime, run, kind, data, expected=original)
        except _AutomationChanged:
            raise ReconciliationRejected("Automation changed during reconciliation; reload before checking again") from None
        return _reconciliation_view(kind, target, target_data, repo, selectable=normalized)


def resolve(runtime, project_id, run_id, kind, workflow_run_id, target="operation", source="console"):
    """Associate one server-returned run, then read its status from GitHub.

    This function never dispatches or reruns a workflow. It only binds an
    existing run after exact identity checks and delegates state changes to
    :func:`reconcile`, which is a read-only GitHub query.
    """
    selected = _run_number(workflow_run_id)
    if selected is None:
        raise ReconciliationRejected("Select a workflow run returned by the current read-only query")
    origin = source if isinstance(source, str) and re.fullmatch(r"[a-z_]{1,32}", source) else "console"
    with runtime.automation_lock_for(project_id, kind):
        project, run, saved, policy, data, target_data, original = _reconciliation_operation(
            runtime, project_id, run_id, kind, target)
        original_data = copy.deepcopy(data)
        def reject_missing() -> NoReturn:
            original_target = original_data["rollback"] if target == "rollback" else original_data
            original_target["remote_visibility"] = copy.deepcopy(target_data["remote_visibility"])
            original_target["state"] = target_data["state"]
            try:
                save(runtime, run, kind, original_data, expected=original)
            except _AutomationChanged:
                pass
            if target_data["remote_visibility"].get("state") == "workflow_not_visible":
                raise ReconciliationRejected(WORKFLOW_NOT_VISIBLE_REASON)
            raise ReconciliationRejected("Workflow run is not visible; check repository access before reconciling")
        repo = _reconciliation_repository(runtime, project, saved, target_data)
        _require_unknown(target_data)
        try:
            repo = authorized(runtime, project, run, kind, policy)
        except Cancelled:
            raise ReconciliationRejected("Release/deploy is paused or cancelled; restore the original authorization before reconciling") from None
        except ValueError:
            raise ReconciliationRejected("Release/deploy authorization was revoked or the target changed; restore the original authorization before reconciling") from None
        if kind == "deploy":
            try:
                owner = deployment_owner(runtime, project["id"], repo, policy)
            except ValueError:
                raise ReconciliationRejected("Deployment ownership is ambiguous; reconcile it before selecting") from None
            if owner and owner["run"] != run["id"]:
                raise ReconciliationRejected("A newer deployment owns this target; this operation can no longer be reconciled")
        existing = _run_number(target_data.get("workflow_run_id"))
        if existing:
            remote = _existing_remote(repo, target_data)
            if remote is None:
                reject_missing()
            remotes = [remote] if isinstance(remote, dict) else []
        else:
            remotes = _remote_runs(repo, target_data, force=True)
            if remotes is None:
                reject_missing()
        normalized = [item for item in (_candidate(repo, remote) for remote in remotes) if item]
        exact = [item for item in normalized if item["id"] == selected and
                 _candidate_identity_mismatch(target_data, item) is None]
        if len(exact) != 1:
            raise ReconciliationRejected("The selected workflow run does not match this operation's repository, workflow, ref, exact target SHA or operation ID")
        if existing and existing != selected:
            raise ReconciliationRejected("The selected workflow run does not match the operation's existing workflow run")
        candidate = exact[0]
        if target_data.get("state") == "rerun_unknown":
            attempt = candidate.get("attempt")
            previous = _run_number(target_data.get("previous_attempt")) or _run_number(target_data.get("attempt")) or 1
            if attempt is None or attempt <= previous:
                raise ReconciliationRejected("GitHub has not confirmed a new attempt for this rerun; do not repeat the rerun request")
        now = time.time()
        audit = _audit_entry({
            "at": now,
            "source": origin,
            "selected_run_id": selected,
            "repository": repo,
            "workflow": target_data.get("workflow"),
            "ref": target_data.get("ref"),
            "operation_id": target_data.get("operation_id"),
            "sha": target_data.get("sha"),
            "workflow_sha": target_data.get("workflow_sha"),
            "candidate_count": len(normalized),
            "candidates": normalized,
            "previous": {
                "state": target_data.get("state"),
                "reconciliation_state": (target_data.get("reconciliation") or {}).get("state"),
                "error": target_data.get("error", ""),
                "checked_at": target_data.get("checked_at"),
                "candidate_run_ids": (target_data.get("reconciliation") or {}).get("candidate_run_ids", []),
            },
        })
        target_data["workflow_run_id"] = selected
        target_data["manual_reconciliation"] = audit
        history = target_data.get("manual_reconciliations")
        retained = [_audit_entry(item) for item in history] if isinstance(history, list) else []
        target_data["manual_reconciliations"] = [item for item in retained if item][-(RECONCILIATION_AUDIT_LIMIT - 1):] + [audit]
        _record_reconciliation(target_data, repo, [candidate], "selected",
                               "An operator selected this workflow run from the server-returned candidates. Its status is read from GitHub; this never starts or repeats a run.", checked_at=now)
        try:
            reconciled = reconcile(runtime, run, kind, target_data, persist=False)
        except (ValueError, KeyError, TypeError):
            raise ReconciliationRejected("Workflow run identity differs from the authorized operation") from None
        if isinstance(reconciled, dict) and reconciled.get("remote_visibility", {}).get("state") == "run_not_visible":
            reject_missing()
        if not isinstance(reconciled, dict) or reconciled.get("state") in RECONCILIATION_UNKNOWN:
            raise ReconciliationRejected("GitHub did not return a confirmed status for the selected workflow run; query again before selecting")
        # 人工对账已成功读取 GitHub：恢复后的失败重新从零计数，避免一次瞬时错误
        # 立刻再次转入 reconciliation_blocked。
        for field in ("advance_failures", "last_advance_error", "last_advance_error_at"):
            target_data.pop(field, None)
        if target == "rollback":
            data["rollback"] = reconciled
            if reconciled.get("state") in {"queued", "running", "workflow_succeeded"}:
                data.update(state="rolling_back", error="")
        try:
            save(runtime, run, kind, data, expected=original)
        except _AutomationChanged:
            raise ReconciliationRejected("Automation changed during reconciliation; reload before selecting") from None
        runtime.store.event("automation_reconciled", "Manual reconciliation associated an existing workflow run",
                            project_id, run_id,
                            {"kind": kind, "target": target, "workflow_run_id": selected,
                             "source": origin, "at": now})
        return {"kind": kind, "target": target, "state": target_data.get("state"), "operation": data,
                "candidates": target_data.get("reconciliation", {}).get("candidates", []),
                "manual_reconciliation": audit}


def abandon(runtime, project_id, run_id, kind, reason, target="operation", source="console"):
    """Close an unconfirmed workflow start that GitHub no longer exposes.

    ``dispatch_unknown``/``rerun_unknown`` only ends through reconciliation, but
    once the workflow file or the recorded run answers 404 there is no run left
    to bind. This is the single operator exit: it requires a written reason,
    re-reads GitHub first and refuses whenever a run is still visible, then
    records ``failed`` plus an audit entry. It never dispatches or reruns.
    """
    note = reason.strip()[:500] if isinstance(reason, str) else ""
    if not note:
        raise ReconciliationRejected("An operator reason is required to abandon an unconfirmed operation")
    if target != "operation":
        raise ReconciliationRejected("Only an unconfirmed workflow start can be abandoned")
    origin = source if isinstance(source, str) and re.fullmatch(r"[a-z_]{1,32}", source) else "console"
    with runtime.automation_lock_for(project_id, kind):
        project, run, saved, policy, data, target_data, original = _reconciliation_operation(
            runtime, project_id, run_id, kind, target)
        repo = _reconciliation_repository(runtime, project, saved, target_data)
        _require_unknown(target_data)
        existing = _run_number(target_data.get("workflow_run_id"))
        remote = _existing_remote(repo, target_data) if existing else None
        remotes = [] if existing else (_remote_runs(repo, target_data, force=True) or [])
        if isinstance(remote, dict) or remotes:
            raise ReconciliationRejected("GitHub still exposes this workflow; reconcile the visible run instead of abandoning it")
        visibility = (target_data.get("remote_visibility") or {}).get("state")
        if visibility not in {"workflow_not_visible", "run_not_visible"}:
            raise ReconciliationRejected("GitHub still exposes this workflow; wait for its run instead of abandoning it")
        now = time.time()
        reconciliation = target_data.get("reconciliation") if isinstance(target_data.get("reconciliation"), dict) else {}
        audit = _audit_entry({
            "at": now, "source": origin, "repository": repo, "candidate_count": 0, "candidates": [],
            "workflow": target_data.get("workflow"), "ref": target_data.get("ref"),
            "operation_id": target_data.get("operation_id"), "sha": target_data.get("sha"),
            "workflow_sha": target_data.get("workflow_sha"),
            "previous": {"state": target_data.get("state"), "error": target_data.get("error", ""),
                         "reconciliation_state": reconciliation.get("state"),
                         "checked_at": target_data.get("checked_at"),
                         "candidate_run_ids": reconciliation.get("candidate_run_ids", [])},
        })
        audit.update(outcome="failed", reason=note, visibility=visibility)
        target_data["operator_abandon"] = audit
        target_data["state"] = "failed"
        target_data["error"] = WORKFLOW_NOT_VISIBLE_REASON
        _record_reconciliation(target_data, repo, [], "abandoned",
                               "An operator closed this unconfirmed operation because GitHub no longer exposes its workflow. No run was started or rerun.",
                               checked_at=now)
        try:
            save(runtime, run, kind, data, expected=original)
        except _AutomationChanged:
            raise ReconciliationRejected("Automation changed during reconciliation; reload before abandoning") from None
        runtime.store.event("automation_abandoned", "Operator abandoned an unconfirmed workflow operation",
                            project_id, run_id,
                            {"kind": kind, "reason": note, "at": now, "visibility": visibility,
                             "previous_state": audit["previous"]["state"], "workflow": target_data.get("workflow"),
                             "operation_id": target_data.get("operation_id")})
        runtime.wake.set()
        return {"kind": kind, "target": target, "state": target_data["state"], "operation": data,
                "operator_abandon": audit}


def health_check(url, sha, timeout=15):
    from .process import checked
    argv = ([sys.executable, "internal-health-check"] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "adaptive_rsi.health_transport"])
    try:
        result = json.loads(checked([*argv, url, sha, str(timeout)],
                                    cwd=Path(__file__).resolve().parents[1], timeout=timeout))
        if not isinstance(result, dict) or result.get("sha") != sha:
            raise ValueError("Invalid health transport result")
        return result
    except Cancelled:
        raise
    except (OSError, RuntimeError, ValueError):
        return {"url": url, "sha": sha, "checked_at": time.time(), "healthy": None,
                "error": "Health transport failed or exceeded its total deadline"}


class _HealthDeadlineExceeded(ValueError):
    """The endpoint did not finish answering within the total deadline; its health stays unknown."""


def direct_health_check(url, sha, timeout=15):
    class SameOrigin(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            original, target = urlsplit(url), urlsplit(newurl)
            if (target.scheme != "https" or (target.hostname, target.port or 443) != (original.hostname, original.port or 443) or
                    target.username is not None or target.password is not None or target.query or target.fragment):
                raise ValueError("Deployment health redirect changed the authorized origin")
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    result = {"url": url, "sha": sha, "checked_at": time.time(), "healthy": False}
    deadline = time.monotonic() + timeout
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Adaptive-RSI-health"})
        with urllib.request.build_opener(SameOrigin()).open(request, timeout=min(10, timeout)) as response:
            body = bytearray()
            while len(body) <= 65536:
                if time.monotonic() >= deadline:
                    raise _HealthDeadlineExceeded("Deployment health check exceeded the total deadline")
                chunk = response.read1(min(8192, 65537 - len(body))) if hasattr(response, "read1") else response.read(8192)
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("Deployment health response exceeds size limit")
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("sha") != sha or not (data.get("healthy") is True or data.get("status") == "ok"):
                raise ValueError("Health response must confirm the exact deployment SHA and healthy status")
            result["healthy"] = response.status == 200
            result["status_code"] = response.status
    except _HealthDeadlineExceeded as exc:
        result["healthy"] = None
        result["error"] = str(exc)[:500]
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if not isinstance(exc, urllib.error.HTTPError):
            result["healthy"] = None
        result["error"] = str(exc)[:500]
    except Exception as exc:
        result["error"] = str(exc)[:500]
    return result


def stable_key(project, repo, url):
    return "deployment-stable:" + fingerprint({"project": project, "repository": repo, "health_url": url})


def owner_key(project, repo, policy):
    url = policy.get("health_url")
    if url:
        parsed = urlsplit(url)
        target = {"origin": parsed.hostname.lower(), "port": parsed.port or 443,
                  "path": parsed.path or "/", "query": parsed.query}
    else:
        target = {"repository": repo.lower(), "workflow": policy["workflow"]}
    return "deployment-owner:" + fingerprint(target)


def _owner_alert(runtime, name, project):
    """Report an unreadable ownership record once; it is still treated as no owner."""
    diagnostic = (name, "deployment-owner")
    with runtime.automation_receipt_failures_lock:
        if diagnostic in runtime.automation_receipt_failures:
            return
        runtime.store.event("automation_error",
                            "Deployment owner record is unreadable; repair the stored record before relying on it",
                            project, None, {"kind": "deploy", "error_code": "invalid_deployment_owner"})
        if len(runtime.automation_receipt_failures) >= 128:
            runtime.automation_receipt_failures.pop(next(iter(runtime.automation_receipt_failures)))
        runtime.automation_receipt_failures[diagnostic] = True


def deployment_owner(runtime, project, repo, policy):
    name = owner_key(project, repo, policy)
    with runtime.dispatch_lock:
        stored = runtime.store.setting(name)
        current = _owner_record(stored)
        if current:
            with runtime.automation_receipt_failures_lock:
                runtime.automation_receipt_failures.pop((name, "deployment-owner"), None)
            return current
        if stored:
            _owner_alert(runtime, name, project)
        migration = "deployment-owner-migrated:" + name.split(":", 1)[1]
        if runtime.store.setting(migration, False):
            return None
        legacy = []
        for row in runtime.store.query("SELECT key,value FROM settings WHERE key LIKE 'deployment-owner:%'"):
            try:
                receipt = _owner_record(json.loads(row["value"]))
                if not receipt:
                    continue
                run = runtime.store.one("SELECT project,config,started FROM runs WHERE id=?", (receipt["run"],))
                if not run:
                    continue
                config = json.loads(run["config"])
                previous_policy = config["delivery"]["deploy"]
                previous_repo = config["github"]["repository"]
                old_name = "deployment-owner:" + fingerprint({"project": run["project"], "repository": previous_repo,
                    "target": previous_policy.get("health_url") or previous_policy["workflow"]})
                if row["key"] == old_name and owner_key(run["project"], previous_repo, previous_policy) == name:
                    state = _require_automation(runtime, receipt["run"], "deploy")
                    legacy.append((not deployment_terminal(state, previous_policy), run["started"], receipt))
            except ReconciliationRejected:
                raise
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if not legacy:
            runtime.store.put_setting(migration, True)
            return None
        if sum(active for active, _, _ in legacy) > 1:
            raise ValueError("Multiple legacy deployments own this target; reconcile them before deploying")
        receipt = max(legacy, key=lambda item: (item[0], item[1]))[2]
        runtime.store.put_setting(name, receipt)
        return receipt


def deployment_terminal(data, policy):
    if data.get("state") == "workflow_succeeded":
        return not policy.get("health_url")
    if data.get("state") == "health_failed":
        return not policy.get("automatic_rollback") or data.get("rollback_unavailable", False)
    if data.get("state") == "health_unconfirmed":
        return False
    return data.get("state") in {"healthy", "failed", "rolled_back", "rollback_failed", "superseded"}


def health_sample(policy, sha):
    """All declared endpoints must agree on one SHA within a shared deadline."""
    urls = [policy["health_url"], *policy.get("health_urls", [])]
    if len(urls) == 1:
        return health_check(urls[0], sha)
    deadline = time.monotonic() + 15
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    def probe(url):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"url": url, "sha": sha, "healthy": None, "error": "Shared health deadline exhausted"}
        return health_check(url, sha, timeout=min(10, remaining))
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as executor:
        futures = [executor.submit(copy_context().run, probe, url) for url in urls]
        reports = [future.result() for future in futures]
    return {"url": urls[0], "sha": sha, "checked_at": time.time(), "checks": reports,
            "healthy": (False if any(report.get("healthy") is False for report in reports) else
                        None if any(report.get("healthy") is None for report in reports) else
                        all(report.get("healthy") is True for report in reports))}


def observe_health(data, report, policy, now):
    """Persist bounded observations; successes must be consecutive across ticks."""
    if report.get("healthy") is None:
        data["health_successes"] = 0
        data["health"] = report
        data["health_observations"] = [*data.get("health_observations", []), report][-10:]
        first = data.setdefault("health_observation_started_at", now)
        if now - first >= max(60, policy.get("health_attempts", 1) * (15 + policy.get("health_interval_seconds", 10))):
            return "health_unconfirmed"
        data["health_retry_at"] = now + policy.get("health_interval_seconds", 10)
        return "pending"
    data["health_attempts"] = data.get("health_attempts", 0) + 1
    data["health_successes"] = data.get("health_successes", 0) + 1 if report.get("healthy") is True else 0
    data["health"] = report
    data["health_observations"] = [*data.get("health_observations", []), report][-10:]
    data["health_gate"] = {"required_consecutive": policy.get("health_consecutive_successes", 1),
                           "observed_consecutive": data["health_successes"],
                           "round_limit": policy.get("health_attempts", 1),
                           "rounds": data["health_attempts"]}
    if data["health_successes"] >= policy.get("health_consecutive_successes", 1):
        return "healthy"
    if data["health_attempts"] >= policy.get("health_attempts", 1):
        return "health_failed"
    data["health_retry_at"] = now + policy.get("health_interval_seconds", 10)
    return "pending"


def deployment_health(runtime, project, run, data, policy, cancel, persist=save):
    data = copy.deepcopy(data)
    if not policy.get("health_url"):
        return data
    owner = deployment_owner(runtime, project["id"], data["repository"], policy)
    if owner and owner["run"] != run["id"]:
        data.update(state="superseded", error="A newer deployment owns this target; no stale rollback will be issued")
        return data
    if data["state"] == "workflow_succeeded":
        now = time.time()
        first = data.setdefault("health_started_at", now)
        if now < first + policy.get("health_warmup_seconds", 0) or now < data.get("health_retry_at", 0):
            persist(runtime, run, "deploy", data)
            return data
        authorized(runtime, project, run, "deploy", policy, cancel)
        report = health_sample(policy, data["sha"])
        with runtime.dispatch_lock:
            authorized(runtime, project, run, "deploy", policy, cancel)
            current_owner = deployment_owner(runtime, project["id"], data["repository"], policy)
            if current_owner and current_owner["run"] != run["id"]:
                data.update(state="superseded", error="A newer deployment owns this target; old health evidence was discarded")
                return data
            observed = observe_health(data, report, policy, time.time())
            if observed == "pending":
                persist(runtime, run, "deploy", data)
                return data
            data["state"] = observed
            if observed == "health_failed":
                data["rollback_target"] = runtime.store.setting(stable_key(project["id"], data["repository"], policy["health_url"]))
            if observed == "healthy":
                for field in ("rollback_unavailable", "rollback_blocked_reason", "rollback_recovery_notes"):
                    data.pop(field, None)
                stable = (stable_key(project["id"], data["repository"], policy["health_url"]),
                          {"sha": data["sha"], "run": run["id"], "health": report})
                persist(runtime, run, "deploy", data, stable=stable)
            else:
                persist(runtime, run, "deploy", data)
    if data["state"] == "health_failed" and policy.get("automatic_rollback"):
        if policy.get("rollback_data_policy", "manual") != "backward_compatible":
            data.update(rollback_unavailable=True, rollback_blocked_reason="data_compatibility",
                        rollback_recovery_notes=policy.get("rollback_recovery_notes", ""),
                        error="Automatic rollback requires an explicit backward-compatible data policy; inspect migrations and backups before manually restoring data and deploying a compatible version")
            return data
        previous = data.get("rollback_target")
        if not previous or previous["sha"] == data["sha"]:
            data["rollback_unavailable"] = True
            return data
        repo = authorized(runtime, project, run, "deploy", policy, cancel)
        validate_pin(repo, policy)
        with runtime.dispatch_lock:
            authorized(runtime, project, run, "deploy", policy, cancel)
            current_owner = deployment_owner(runtime, project["id"], repo, policy)
            if current_owner and current_owner["run"] != run["id"]:
                data.update(state="superseded", error="A newer deployment owns this target; rollback was cancelled")
                return data
            rollback = {**{k: data[k] for k in ["repository", "workflow", "ref", "workflow_sha"]},
                        "sha": previous["sha"], "operation_id": data["operation_id"] + "-rollback", "state": "dispatching"}
            data.update(state="rolling_back", rollback=rollback)
            persist(runtime, run, "deploy", data)
        try:
            api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                {"ref": policy["ref"], "inputs[rsi_operation_id]": rollback["operation_id"], "inputs[rsi_sha]": rollback["sha"]})
            rollback.update(state="submitted", dispatch_receipt="accepted")
        except ApiRejected as exc:
            rollback.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                            dispatch_http_status=exc.status)
            data.update(state="rollback_failed", error=rollback["error"])
            persist(runtime, run, "deploy", data)
            raise
        except Exception as exc:
            rollback.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
            persist(runtime, run, "deploy", data)
            raise
    if data["state"] == "rolling_back":
        rollback = reconcile(runtime, run, "deploy", data["rollback"], persist=False)
        data["rollback"] = rollback
        if rollback["state"] in {"failed", "rerun_unknown"}:
            data["state"] = "rollback_failed"
            if rollback["state"] == "rerun_unknown":
                data["error"] = rollback["error"]
            persist(runtime, run, "deploy", data)
        elif rollback["state"] == "workflow_succeeded":
            now = time.time()
            first = rollback.setdefault("health_started_at", now)
            if now < first + policy.get("health_warmup_seconds", 0) or now < rollback.get("health_retry_at", 0):
                persist(runtime, run, "deploy", data)
                return data
            authorized(runtime, project, run, "deploy", policy, cancel)
            report = health_sample(policy, rollback["sha"])
            with runtime.dispatch_lock:
                authorized(runtime, project, run, "deploy", policy, cancel)
                current_owner = deployment_owner(runtime, project["id"], data["repository"], policy)
                if current_owner and current_owner["run"] != run["id"]:
                    data.update(state="superseded", error="A newer deployment owns this target; old rollback health evidence was discarded")
                    persist(runtime, run, "deploy", data)
                    return data
                observed = observe_health(rollback, report, policy, time.time())
                if observed == "pending":
                    persist(runtime, run, "deploy", data)
                    return data
                if observed == "health_unconfirmed":
                    rollback["health_unconfirmed"] = True
                data["state"] = ("rolled_back" if observed == "healthy" else
                                 "rollback_health_unconfirmed" if observed == "health_unconfirmed" else "rollback_failed")
                if observed == "healthy":
                    owner_name = owner_key(project["id"], data["repository"], policy)
                    ownership = (owner_name, current_owner, {"run": run["id"], "sha": rollback["sha"]})
                    stable = (stable_key(project["id"], data["repository"], policy["health_url"]),
                              {"sha": rollback["sha"], "run": run["id"], "health": report})
                    persist(runtime, run, "deploy", data, ownership=ownership, stable=stable)
                else:
                    persist(runtime, run, "deploy", data)
    return data


def _feedback_layers(feedback):
    """Walk restore/missing-artifact wrappers without hiding older evidence."""
    while isinstance(feedback, dict):
        yield feedback
        feedback = feedback.get("previous")


def feedback_target_status(outcome, target_sha):
    """Return ``(eligible, legacy)`` for one exact publication target.

    New receipts carry an independent merge record created by delivery
    post-merge tracking. Historical receipts are accepted only when their
    recorded head is the same commit; a verified receipt without any head is
    reported as legacy so the caller can pin it locally at publication time.
    """
    if not isinstance(outcome, dict) or not target_sha:
        return False, False
    feedback = outcome.get("feedback")
    if not isinstance(feedback, dict):
        return False, False
    verified = False
    for layer in _feedback_layers(feedback):
        merge = layer.get("merge")
        if isinstance(merge, dict) and merge.get("sha"):
            return merge.get("sha") == target_sha and merge.get("state") == "verified", False
        head = layer.get("head")
        if isinstance(head, str) and head:
            return layer.get("state") == "verified" and head == target_sha, False
        verified = verified or layer.get("state") == "verified"
    # A verified receipt without a recorded head never qualifies on its own.
    return False, bool(verified and target_sha == outcome.get("merged_sha"))


class _PinUnavailable(RuntimeError):
    def __init__(self, message, deferred=False):
        super().__init__(message)
        self.deferred = deferred


def _pin_legacy_feedback(runtime, run, outcome, target_sha):
    """Pin a historical verified receipt to the exact merge being published.

    This runs immediately before the first publication write. It performs only
    a deterministic local check against the persisted run and delivery receipt;
    it never calls a model or writes to a remote service.
    """
    feedback = outcome.get("feedback")
    if not isinstance(feedback, dict):
        return False
    target_layer = None
    for layer in _feedback_layers(feedback):
        if isinstance(layer.get("merge"), dict) and layer["merge"].get("sha"):
            return False
        if layer.get("state") == "regressed":
            return False
        if layer.get("state") == "verified" and not layer.get("head"):
            target_layer = layer
            break
    if target_layer is None:
        return False
    if (not re.fullmatch(r"[a-f0-9]{40}", target_sha or "") or
            target_sha != outcome.get("merged_sha") or run.get("state") != "merged"):
        return False
    receipt = runtime.store.setting("delivery:" + run["id"])
    try:
        repository = json.loads(run["config"])["github"]["repository"]
    except (TypeError, ValueError, KeyError):
        return False
    if not isinstance(receipt, dict) or not repository or receipt.get("repository") != repository:
        return False
    import tempfile
    from .adapters import detect
    from .gitops import snapshot
    from .storage_tools import run_artifact_path
    try:
        config = json.loads(run["config"] or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    retry_key = "legacy-release-pin:" + run["id"] + ":" + target_sha
    previous = runtime.store.setting(retry_key, {})
    if previous.get("retry_at", 0) > time.time():
        raise _PinUnavailable(previous.get("error", "Legacy release verification could not run locally; retry is temporarily deferred"), deferred=True)
    try:
        source = runtime.project(run["project"])["path"]
        with tempfile.TemporaryDirectory(prefix="legacy-release-", dir=runtime.store.root) as folder:
            folder = Path(folder)
            baseline = snapshot(source, folder / "baseline", target_sha)
            candidate = snapshot(source, folder / "candidate", target_sha)
            checks = config["checks"] or detect(baseline)["checks"]
            seeds = run_artifact_path(runtime.store, run, "dependencies") if config["environment"]["seed_paths"] else None
            evaluation = runtime.evaluator.evaluate(baseline, candidate, checks, folder / "evaluation", config,
                                                    seeds=seeds, changed_files=[])
            if evaluation.get("decision") != "pass":
                # 确定性失败与本地执行异常同样退避：固定 checks 对同一目标
                # sha 名义确定，靠每分钟重跑翻转结果只能靠 flaky，不能留在
                # blocked_verification 被下个 tick 立即再次完整评估。
                message = ("Legacy release verification did not pass: " +
                           str(evaluation.get("decision") or "unknown"))[:500]
                runtime.store.put_setting(retry_key, {"retry_at": time.time() + 600, "error": message})
                raise _PinUnavailable(message)
    except Cancelled:
        raise
    except (ValueError, OSError, RuntimeError) as exc:
        message = ("Legacy release verification could not run locally: " + type(exc).__name__ + ": " + str(exc))[:500]
        runtime.store.put_setting(retry_key, {"retry_at": time.time() + 600, "error": message})
        raise _PinUnavailable(message) from exc
    target_layer["head"] = target_sha
    target_layer["head_pinned"] = {"at": time.time(), "basis": "exact_target_fixed_checks", "evaluation": evaluation}
    runtime.store.execute("UPDATE runs SET result=? WHERE id=? AND project=?",
                          (encode(outcome), run["id"], run["project"]))
    return True


def _rollback_retry_allowed(data, rollback):
    """Preserve the known-healthy rollback contract instead of reusing merge evidence."""
    sha = rollback.get("sha")
    if not re.fullmatch(r"[a-f0-9]{40}", sha or ""):
        return False
    known = data.get("rollback_target")
    if known is None:
        # Legacy receipts may only rerun a workflow already dispatched from the
        # known-healthy target; they can never authorize a fresh rollback dispatch.
        return bool(rollback.get("workflow_run_id"))
    return isinstance(known, dict) and known.get("sha") == sha


def _retry_target_allowed(runtime, run, outcome, data, target):
    """Apply exact-target qualification, or the known-healthy rollback contract."""
    if target is not data:
        return _rollback_retry_allowed(data, target)
    eligible, legacy = feedback_target_status(outcome, target.get("sha"))
    if legacy:
        return _pin_legacy_feedback(runtime, run, outcome, target.get("sha"))
    return eligible


def _advance_failure_count(data):
    """Bound a stored consecutive-failure counter; corrupted values restart at zero."""
    value = data.get("advance_failures")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, ADVANCE_FAILURE_LIMIT - 1)
    return 0


def advance(runtime, project, run, outcome, cancel=None):
    if not re.fullmatch(r"[a-f0-9]{40}", outcome.get("merged_sha", "")):
        return {}
    try:
        saved = json.loads(run["config"] or "{}")
    except (TypeError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    result = {}
    for kind in KINDS:
        policy = saved.get("delivery", {}).get(kind, {})
        data, expected = _read_automation(runtime, run["id"], kind)
        if data and data.get("receipt_error"):
            result[kind] = data
            continue
        if kind == "deploy" and result.get("release", {}).get("receipt_error"):
            result[kind] = result["release"].copy()
            continue

        def persist(runtime, run, kind, value, ownership=None, stable=None):
            nonlocal expected
            extra = {}
            if ownership is not None:
                extra["ownership"] = ownership
            if stable is not None:
                extra["stable"] = stable
            saved = save(runtime, run, kind, value, expected=expected, **extra)
            expected = encode(saved)
            return saved

        if not policy.get("enabled") and not data:
            continue
        try:
            feedback = outcome.get("feedback")
            if not isinstance(feedback, dict):
                # A malformed historical receipt cannot prove verification.
                feedback = {}
            waiting = (not data or data.get("state") in {"pending", "blocked", "waiting_verification",
                                                         "blocked_verification", "waiting_target", "waiting_release"})
            # Qualification is bound to the exact commit that will be dispatched.
            # A later mainline pass cannot authorize this merge, and a legacy
            # verified receipt without a head is pinned locally only here, right
            # before a real publication attempt.
            target_sha = data.get("sha") if isinstance(data, dict) and data.get("sha") else outcome["merged_sha"]
            eligible, legacy = feedback_target_status(outcome, target_sha)
            if waiting and legacy:
                eligible = _pin_legacy_feedback(runtime, run, outcome, target_sha)
            if waiting and not eligible:
                state = "waiting_verification" if not feedback else "blocked_verification"
                error = ("Post-merge verification covers a later mainline commit, not the merge commit to release"
                         if isinstance(feedback.get("head"), str) and feedback["head"] != outcome["merged_sha"] else
                         "Post-merge verification must pass before release/deploy")
                data = {"state": state, "error": error}
                result[kind] = persist(runtime, run, kind, data)
                continue
            if data and data.get("state") in {"dispatching", "submitted", "dispatch_unknown", "queued", "running", "rerunning", "rerun_unknown"}:
                try:
                    data = reconcile(runtime, run, kind, data, persist=False)
                except Cancelled:
                    raise
                except Exception as exc:
                    # 永久性对账错误（403、身份不一致等）若原样上抛，下一次 tick 会
                    # 再次读取同一必然失败的远端，并每次写一条 automation_error。
                    # 连续失败达到上限后转入终态：停止自动轮询，只在转入时记一条事件，
                    # 人工仍可经 candidates/resolve/abandon 收口（这些路径不派发、不重跑）。
                    failures = _advance_failure_count(data) + 1
                    data["advance_failures"] = failures
                    data["last_advance_error"] = str(exc)[:500]
                    data["last_advance_error_at"] = time.time()
                    if failures >= ADVANCE_FAILURE_LIMIT:
                        data["state"] = "reconciliation_blocked"
                        data["error"] = str(exc)[:500]
                        runtime.store.event("automation_error", str(exc)[:500], project["id"], run["id"],
                                            {"kind": kind, "state": "reconciliation_blocked"})
                    result[kind] = persist(runtime, run, kind, data)
                    continue
                for field in ("advance_failures", "last_advance_error", "last_advance_error_at"):
                    data.pop(field, None)
                persist(runtime, run, kind, data)
            if kind == "deploy" and data and data["state"] in {"workflow_succeeded", "health_failed", "rolling_back"}:
                try:
                    data = deployment_health(runtime, project, run, data, policy, cancel, persist=persist)
                except Exception:
                    current, raw = _read_automation(runtime, run["id"], kind)
                    if raw != expected or current and current.get("receipt_error"):
                        raise _AutomationChanged("Automation receipt changed during health tracking")
                    data = current
                    raise
                persist(runtime, run, kind, data)
            if data and data["state"] not in {"pending", "blocked", "waiting_verification", "blocked_verification", "waiting_target", "waiting_release"}:
                result[kind] = data
                continue
            if kind == "deploy" and saved["delivery"].get("release", {}).get("enabled"):
                if result.get("release", {}).get("state") != "workflow_succeeded":
                    if result.get("release", {}).get("state") in {"failed", "rerun_unknown", "blocked", "reconciliation_blocked"}:
                        data = {**(data or {}), "state": "blocked", "blocked_by_release": True,
                                "error": ("Release rerun result is unconfirmed; reconcile release before deployment"
                                          if result["release"]["state"] == "rerun_unknown" else
                                          "Release reconciliation is blocked; restore access and reconcile release before deployment"
                                          if result["release"]["state"] == "reconciliation_blocked" else
                                          "Release authorization is blocked; restore it before deployment"
                                          if result["release"]["state"] == "blocked" else
                                          "Release workflow failed; retry release before deployment")}
                        result[kind] = persist(runtime, run, kind, data)
                        continue
                    # 到达这里说明合并后检查已通过；继承的等待提示已过期，进入等待发布时一并清除。
                    data = {**(data or {}), "state": "waiting_release", "error": ""}
                    data.pop("blocked_by_release", None)
                    result[kind] = persist(runtime, run, kind, data)
                    continue
            repo = authorized(runtime, project, run, kind, policy, cancel)
            if data:
                data.pop("blocked_by_release", None)
            expected_owner = None
            if kind == "deploy":
                owner = deployment_owner(runtime, project["id"], repo, policy)
                expected_owner = owner
                if owner and owner["run"] != run["id"]:
                    previous = _require_automation(runtime, owner["run"], "deploy")
                    if not deployment_terminal(previous, policy):
                        result[kind] = persist(runtime, run, kind, {"state": "waiting_target", "error": "Another deployment is still active on this target"})
                        continue
                    comparison = api(repo, "compare/" + owner["sha"] + "..." + outcome["merged_sha"])
                    if comparison.get("status") != "ahead":
                        result[kind] = persist(runtime, run, kind, {"state": "superseded", "error": "Deployment target already has this or a newer commit; refusing a stale or diverged deployment"})
                        continue
            try:
                validate_pin(repo, policy)
            except ApiRejected as exc:
                identity = {"repository": repo, "workflow": policy["workflow"], "ref": policy["ref"],
                            "workflow_sha": policy["sha"], "sha": outcome["merged_sha"], "kind": kind}
                if not data:
                    data = {"state": "pending", **identity}
                for field, value in identity.items():
                    data.setdefault(field, value)
                data.setdefault("operation_id", "rsi-" + fingerprint(
                    {"project": project["id"], "run": run["id"], "kind": kind,
                     "repository": repo, "sha": outcome["merged_sha"]})[:32])
                if exc.local:
                    # 冷却在请求发出前拦下：GitHub 从未看到这次校验，也没有产生
                    # 任何远端副作用。保持待发并让下一个调度轮询自动重试，不能
                    # 记成「GitHub 拒绝」的终态失败。
                    data.update(state="pending", error=str(exc)[:500],
                                retry_at=time.time() + API_COOLDOWN_SECONDS)
                    data.pop("dispatch_receipt", None)
                    data.pop("dispatch_http_status", None)
                    result[kind] = persist(runtime, run, kind, data)
                    continue
                # Dispatch 前的只读校验被 GitHub 明确拒绝：显式落到失败态并留下
                # 人工介入入口（retry 通道按被拒回执重新派发），不能留在 waiting_*
                # 状态被轮询无限重试。
                data.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                            dispatch_http_status=exc.status)
                raise
            with runtime.dispatch_lock:
                authorized(runtime, project, run, kind, policy, cancel)
                if kind == "deploy" and deployment_owner(runtime, project["id"], repo, policy) != expected_owner:
                    result[kind] = persist(runtime, run, kind, {"state": "waiting_target", "error": "Deployment ownership changed during validation"})
                    continue
                if not data or not data.get("operation_id"):
                    operation = "rsi-" + fingerprint({"project": project["id"], "run": run["id"], "kind": kind,
                                                      "repository": repo, "sha": outcome["merged_sha"]})[:32]
                    data = {"state": "pending", "repository": repo, "sha": outcome["merged_sha"], "kind": kind,
                            "workflow": policy["workflow"], "ref": policy["ref"], "workflow_sha": policy["sha"],
                            "operation_id": operation, "created_at": time.time(), "attempt": 0}
                # Persist the exact policy and transmitted input identities. The
                # workflow's own environment selection is still not observable here.
                data["authorization"] = {"policy_hash": fingerprint(normalized_policy(kind, policy)),
                                         "inputs_hash": fingerprint({"rsi_operation_id": data["operation_id"], "rsi_sha": data["sha"]}),
                                         "health_targets": [url for url in [policy.get("health_url"), *policy.get("health_urls", [])] if url],
                                         "github_environment": "unknown"}
                # Persist the write intent first. Ambiguous failures are reconciled, never re-dispatched.
                data.update(state="dispatching", error="")
                ownership = ((owner_key(project["id"], repo, policy), expected_owner,
                              {"run": run["id"], "sha": data["sha"]}) if kind == "deploy" else None)
                persist(runtime, run, kind, data, ownership=ownership)
            try:
                api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                    {"ref": policy["ref"], "inputs[rsi_operation_id]": data["operation_id"], "inputs[rsi_sha]": data["sha"]})
            except ApiRejected as exc:
                if exc.local:
                    # POST 同样没有发出：把已持久化的 dispatching 退回 pending，
                    # 保留派发意图等待冷却结束后自动重发，而不是记为未知或失败。
                    data.update(state="pending", error=str(exc)[:500],
                                retry_at=time.time() + API_COOLDOWN_SECONDS)
                    data.pop("dispatch_receipt", None)
                    data.pop("dispatch_http_status", None)
                    result[kind] = persist(runtime, run, kind, data)
                    continue
                data.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                            dispatch_http_status=exc.status)
                raise
            except Exception as exc:
                data.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
                raise
            data.update(state="submitted", dispatch_receipt="accepted")
            persist(runtime, run, kind, data)
            runtime.store.event("automation_dispatched", "Authorized " + kind + " workflow submitted", project["id"], run["id"], data)
        except _AutomationChanged:
            result[kind] = _read_automation(runtime, run["id"], kind)[0] or {}
            continue
        except Cancelled:
            if data and data.get("state") in {"pending", "blocked"}:
                data.update(state="blocked", error="Project paused or operation cancelled")
        except Exception as exc:
            if not data:
                data = {"state": "blocked", "error": str(exc)[:500]}
            else:
                data["error"] = str(exc)[:500]
                if data["state"] in {"pending", "blocked"}:
                    data["state"] = "blocked"
            if not isinstance(exc, _PinUnavailable) or not exc.deferred:
                runtime.store.event("automation_error", str(exc)[:500], project["id"], run["id"], {"kind": kind})
        if data:
            try:
                result[kind] = persist(runtime, run, kind, data)
            except _AutomationChanged:
                result[kind] = _read_automation(runtime, run["id"], kind)[0] or {}
    return result


def verified_feedback(run):
    """Only a well-formed verified receipt can unlock a retry."""
    try:
        outcome = json.loads(run["result"] or "{}")
    except (TypeError, ValueError):
        return {}
    feedback = outcome.get("feedback") if isinstance(outcome, dict) else None
    return feedback if isinstance(feedback, dict) else {}


def retry(runtime, project_id, run_id, kind):
    if kind not in KINDS:
        raise ValueError("Unknown automation kind")
    with runtime.automation_lock_for(project_id, kind):
        run = runtime.store.one("SELECT * FROM runs WHERE id=? AND project=? AND state='merged'", (run_id, project_id))
        if not run:
            raise ValueError("Only a verified merged run in this project can be retried")
        try:
            run_config = json.loads(run["config"] or "{}")
        except (TypeError, ValueError):
            run_config = {}
        if not isinstance(run_config, dict):
            run_config = {}
        policy = run_config.get("delivery", {}).get(kind, {})
        project = runtime.project(project_id)
        data, raw = _read_automation(runtime, run_id, kind)
        if data and data.get("receipt_error"):
            raise ReconciliationRejected(data["error"])
        repo = authorized(runtime, project, run, kind, policy)
        if not data or data.get("state") == "blocked":
            raise ValueError("Restore the original authorization and let tracking retry the pending operation")
        if data.get("state") == "reconciliation_blocked":
            # 回执并未确认失败，重跑可能重复已存在的 run；只能先人工只读对账。
            raise ValueError("Reconciliation is blocked; restore GitHub access and reconcile this operation instead of retrying")
        if not data.get("operation_id"):
            raise ValueError("This operation has no failed GitHub workflow to retry")
        is_preflight_rejected = (
            kind == "deploy" and
            data.get("state") == "failed" and
            data.get("dispatch_receipt") == "rejected" and
            not data.get("workflow_run_id")
        )
        if kind == "deploy" and not is_preflight_rejected:
            owner = deployment_owner(runtime, project_id, repo, policy)
            if owner and owner["run"] != run_id:
                raise ValueError("A newer deployment owns this target; stale delivery cannot be retried")
        try:
            outcome = json.loads(run["result"] or "{}")
        except (TypeError, ValueError):
            outcome = {}
        if not isinstance(outcome, dict):
            outcome = {}
        if kind == "deploy" and data.get("state") in {"health_failed", "health_unconfirmed"}:
            if not _retry_target_allowed(runtime, run, outcome, data, data):
                raise ValueError("Post-merge verification must pass for the exact target commit before retrying delivery")
            expected_health = raw
            data.update(state="workflow_succeeded", error="", health_attempts=0)
            for field in ("health_started_at", "health_observation_started_at", "health_retry_at", "health_successes", "health_observations", "health_gate",
                          "rollback_unavailable", "rollback_blocked_reason", "rollback_recovery_notes"):
                data.pop(field, None)
            with runtime.dispatch_lock:
                authorized(runtime, project, run, kind, policy)
                owner = deployment_owner(runtime, project_id, repo, policy)
                if owner and owner["run"] != run_id:
                    raise ValueError("A newer deployment owns this target; stale delivery cannot be retried")
                save(runtime, run, kind, data, expected=expected_health)
            runtime.wake.set()
            return data
        expected = raw
        expected_owner = None

        def persist_retry(require_authorization=False):
            nonlocal expected
            with runtime.dispatch_lock:
                if require_authorization:
                    authorized(runtime, project, run, kind, policy)
                ownership = None
                if require_authorization and kind == "deploy":
                    if target.get("state") == "dispatching":
                        if deployment_owner(runtime, project_id, repo, policy) != expected_owner:
                            raise ValueError("Deployment ownership changed during validation")
                        ownership = (owner_key(project_id, repo, policy), expected_owner,
                                     {"run": run_id, "sha": target["sha"]})
                    else:
                        owner = deployment_owner(runtime, project_id, repo, policy)
                        if owner and owner["run"] != run_id:
                            raise ValueError("A newer deployment owns this target; stale delivery cannot be retried")
                save(runtime, run, kind, data, expected=expected, ownership=ownership)
                expected = encode(data)

        target = data
        rollback = data.get("rollback", {})
        is_rollback = kind == "deploy" and (data.get("state") in {"rollback_failed", "rollback_health_unconfirmed"} or
                data.get("state") == "rolling_back" and (rollback.get("rerun_pending") or
                    rollback.get("state") in {"rerunning", "rerun_unknown"}))
        if is_rollback:
            target = rollback
            if target.get("state") == "workflow_succeeded":
                if not _retry_target_allowed(runtime, run, outcome, data, target):
                    raise ValueError("Rollback retry requires known healthy target evidence")
                for field in ("health_started_at", "health_observation_started_at", "health_retry_at", "health_successes", "health_observations", "health_gate", "health_unconfirmed"):
                    target.pop(field, None)
                target["health_attempts"] = 0
                data.update(state="rolling_back", error="")
                persist_retry(require_authorization=True)
                runtime.wake.set()
                return data
        if not _retry_target_allowed(runtime, run, outcome, data, target):
            raise ValueError("Post-merge verification must pass for the exact target commit before retrying delivery")
        if (target.get("state") == "failed" and target.get("dispatch_receipt") == "rejected" and
                not target.get("workflow_run_id")):
            if kind == "deploy":
                expected_owner = deployment_owner(runtime, project_id, repo, policy)
                if expected_owner and expected_owner["run"] != run_id:
                    previous = _require_automation(runtime, expected_owner["run"], "deploy")
                    if not deployment_terminal(previous, policy):
                        raise ValueError("Another deployment is still active on this target")
                    comparison = api(repo, "compare/" + expected_owner["sha"] + "..." + target["sha"])
                    if comparison.get("status") != "ahead":
                        raise ValueError("Deployment target already has this or a newer commit; refusing a stale or diverged deployment")
            validate_pin(repo, policy)
            authorized(runtime, project, run, kind, policy)
            target.update(state="dispatching", error="", dispatch_receipt="pending")
            if kind == "deploy" and target is not data:
                data.update(state="rolling_back", rollback=target, error="")
            persist_retry(require_authorization=True)
            try:
                api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                    {"ref": policy["ref"], "inputs[rsi_operation_id]": target["operation_id"], "inputs[rsi_sha]": target["sha"]})
            except ApiRejected as exc:
                target.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                              dispatch_http_status=exc.status)
                if kind == "deploy" and target is not data:
                    data.update(state="rollback_failed", error=target["error"])
                persist_retry()
                raise
            except Exception as exc:
                target.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
                if kind == "deploy" and target is not data:
                    data.update(state="rolling_back", error=target["error"])
                persist_retry()
                runtime.wake.set()
                raise
            target.update(state="submitted", error="", dispatch_receipt="accepted")
            if kind == "deploy" and target is not data:
                data.update(state="rolling_back", error="")
            persist_retry()
            runtime.wake.set()
            return data
        unconfirmed_rerun = target.get("rerun_pending") or target.get("state") in {"rerunning", "rerun_unknown"}
        target = reconcile(runtime, run, kind, target, persist=False)
        if is_rollback:
            data["rollback"] = target
        elif target is not data:
            data.update(target)
            target = data
        if unconfirmed_rerun:
            if kind == "deploy" and target is not data:
                data.update(state="rollback_failed" if target["state"] in {"failed", "rerun_unknown"} else "rolling_back", rollback=target, error=target.get("error", ""))
            persist_retry()
            runtime.wake.set()
            return data
        if target.get("state") != "failed" or not target.get("workflow_run_id"):
            raise ValueError("Only a confirmed failed workflow run can be retried; uncertain dispatches require reconciliation")
        validate_pin(repo, policy)
        authorized(runtime, project, run, kind, policy)
        target.update(state="rerunning", error="", previous_attempt=target.get("attempt", 1),
                      rerun_pending=True, rerun_receipt="pending", rerun_requested_at=time.time())
        if kind == "deploy" and target is not data:
            data.update(state="rolling_back", rollback=target)
        persist_retry(require_authorization=True)
        try:
            api(repo, "actions/runs/" + str(target["workflow_run_id"]) + "/rerun", "POST")
        except ApiRejected as exc:
            target.update(state="failed", error=str(exc)[:500], rerun_pending=False, rerun_receipt="rejected", rerun_http_status=exc.status)
            if kind == "deploy" and target is not data:
                data.update(state="rollback_failed", error=target["error"], rollback=target)
            persist_retry()
            raise
        except Exception as exc:
            target.update(state="rerun_unknown", error=str(exc)[:500], rerun_receipt="unknown")
            if kind == "deploy" and target is not data:
                data.update(state="rollback_failed", error=target["error"], rollback=target)
            persist_retry()
            runtime.wake.set()
            raise
        target["rerun_receipt"] = "accepted"
        persist_retry()
        runtime.wake.set()
        return data
