"""Pinned, explicitly authorized GitHub Actions delivery with durable reconciliation."""
import base64
import json
import re
import time
import urllib.request
from urllib.parse import quote, urlsplit

from .config import fingerprint
from .gitops import origin_repository
from .process import Cancelled, checked
from .store import encode


KINDS = ("release", "deploy")
TERMINAL = {"workflow_succeeded", "failed", "dispatch_unknown", "blocked", "rerun_unknown"}
RERUN_CONFIRM_SECONDS = 300


class ApiRejected(RuntimeError):
    """GitHub explicitly rejected a request before accepting its side effect."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def api(repo, endpoint, method="GET", fields=None):
    path = "repos/" + repo + ("/" + endpoint if endpoint else "")
    argv = ["gh", "api", path, "--method", method]
    for key, value in (fields or {}).items():
        argv += ["-f", key + "=" + str(value)]
    try:
        output = checked(argv)
    except RuntimeError as exc:
        status = re.search(r"\(HTTP (\d{3})\)", str(exc))
        if status and 400 <= int(status[1]) < 500 and int(status[1]) != 408:
            raise ApiRejected(str(exc), int(status[1])) from exc
        raise
    return json.loads(output) if output.strip() else {}


def workflow_content(repo, workflow, sha):
    data = api(repo, "contents/" + workflow + "?ref=" + quote(sha, safe=""))
    if data.get("encoding") != "base64" or data.get("type") != "file":
        raise ValueError("Workflow must be an existing repository file")
    return base64.b64decode(data["content"], validate=False).decode("utf-8")


def compatible_workflow(text):
    # Accept the deliberately small public contract; reject ambiguous YAML forms.
    if not re.search(r"(?m)^run-name:\s*['\"]?\$\{\{\s*inputs\.rsi_operation_id\s*\}\}['\"]?\s*$", text):
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
        if (not field or field[1] <= inputs[1] or not re.search(r"(?m)^[ ]+required:[ ]*true[ ]*$", field[0]) or
                not re.search(r"(?m)^[ ]+type:[ ]*string[ ]*$", field[0])):
            return False
    return True


WORKFLOW_CHOICES_TTL = 300
_workflow_choices_cache = {}


def workflow_choices(runtime, project_id):
    project = runtime.project(project_id)
    repo = runtime.config(project_id)["github"]["repository"]
    if not repo or repo.lower() != origin_repository(project["path"]).lower():
        raise ValueError("Workflow repository must match the selected project's origin")
    info = api(repo, "")
    branch = info["default_branch"]
    sha = api(repo, "commits/" + quote(branch, safe=""))["sha"]
    cached = _workflow_choices_cache.get((repo, sha))
    now = time.time()
    if cached and now - cached[0] < WORKFLOW_CHOICES_TTL:
        return cached[1]
    workflows = api(repo, "actions/workflows?per_page=100").get("workflows", [])
    import concurrent.futures

    def inspect(item):
        path = item.get("path", "")
        if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path):
            return None
        compatible = item.get("state") == "active" and compatible_workflow(workflow_content(repo, path, sha))
        return {"workflow": path, "name": item.get("name", path), "compatible": compatible,
                "reason": "" if compatible else "Requires workflow_dispatch inputs rsi_operation_id/rsi_sha and run-name: ${{ inputs.rsi_operation_id }}"}
    # 逐个 workflow 拉取内容很慢；受控并发把对话框打开时间压到单次往返级别。
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        choices = [row for row in pool.map(inspect, workflows) if row]
    result = {"repository": repo, "sha": sha, "workflows": choices}
    # 缓存绑定精确的默认分支 SHA；分支前进后键自然失效。授权路径始终重新校验，不受缓存影响。
    if len(_workflow_choices_cache) > 32:
        _workflow_choices_cache.clear()
    _workflow_choices_cache[(repo, sha)] = (now, result)
    return result


def authorize_workflow(runtime, project_id, kind, workflow):
    if kind not in KINDS:
        raise ValueError("Unknown automation kind")
    with runtime.dispatch_lock:
        choices = workflow_choices(runtime, project_id)
        if not any(item["workflow"] == workflow and item["compatible"] for item in choices["workflows"]):
            raise ValueError("Select a compatible existing workflow from this project")
        repo, sha = choices["repository"], choices["sha"]
        tag = "rsi-workflow/" + sha
        # GitHub rejects duplicate refs. Read before creation so repeated authorization is safe.
        refs = api(repo, "git/matching-refs/tags/" + tag)
        exact = [item for item in refs if item.get("ref") == "refs/tags/" + tag]
        if exact:
            if exact[0]["object"].get("type") != "commit" or exact[0]["object"].get("sha") != sha:
                raise ValueError("Trusted workflow tag points to a different commit")
        else:
            api(repo, "git/refs", "POST", {"ref": "refs/tags/" + tag, "sha": sha})
        runtime.configure({"delivery": {kind: {"enabled": True, "workflow": workflow, "ref": tag, "sha": sha}}}, project_id)
        runtime.store.event("workflow_authorized", "Existing workflow explicitly authorized", project_id,
                            data={"kind": kind, "workflow": workflow, "ref": tag, "sha": sha})
        return runtime.config(project_id)["delivery"][kind]


def key(run_id, kind):
    return "automation:" + run_id + ":" + kind


def pending(runtime, run):
    saved = json.loads(run["config"])
    delivery = saved["delivery"]
    live = runtime.config(run["project"])
    active = {"dispatching", "submitted", "dispatch_unknown", "queued", "running", "rerunning"}
    for kind in KINDS:
        if not delivery.get(kind, {}).get("enabled"):
            continue
        data = runtime.store.setting(key(run["id"], kind), {})
        if (live["delivery"][kind] != delivery[kind] or live["delivery"]["mode"] != "pr" or
                live["github"]["repository"] != saved["github"]["repository"]):
            if data.get("state") in active or data.get("state") == "rolling_back" and data.get("rollback", {}).get("state") in active:
                return True
            continue
        if kind == "deploy" and data.get("state") == "workflow_succeeded" and delivery[kind].get("health_url"):
            return True
        if data.get("state") == "health_failed":
            if data.get("rollback_unavailable") or not delivery[kind].get("automatic_rollback"):
                continue
        if data.get("state") not in {"workflow_succeeded", "failed", "healthy", "rolled_back", "rollback_failed", "superseded", "rerun_unknown"}:
            return True
    return False


def status(runtime, project_id, run_id):
    run = runtime.store.one("SELECT * FROM runs WHERE id=? AND project=?", (run_id, project_id))
    if not run:
        raise ValueError("Run not found in this project")
    return {kind: runtime.store.setting(key(run_id, kind), {"state": "not_authorized"}) for kind in KINDS}


def save(runtime, run, kind, data):
    with runtime.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key(run["id"], kind), encode(data)))
        row = db.execute("SELECT result FROM runs WHERE id=?", (run["id"],)).fetchone()
        if row:
            result = json.loads(row["result"])
            result.setdefault("automation", {})[kind] = data
            db.execute("UPDATE runs SET result=? WHERE id=?", (encode(result), run["id"]))
    return data


def authorized(runtime, project, run, kind, policy, cancel=None):
    if run["project"] != project["id"]:
        raise ValueError("Automation run belongs to a different project")
    live = runtime.config(project["id"])
    saved = json.loads(run["config"])
    if runtime.stop.is_set() or runtime.project(project["id"])["paused"] or (cancel and cancel()):
        raise Cancelled("Release/deploy paused or cancelled")
    identity = runtime.store.setting("delivery:" + run["id"], {})
    repo = live["github"]["repository"]
    if (live["delivery"]["mode"] != "pr" or not policy.get("enabled") or
            live["delivery"][kind] != policy or saved["delivery"].get(kind) != policy or
            repo != saved["github"]["repository"] or repo != identity.get("repository", repo) or
            repo.lower() != origin_repository(project["path"]).lower()):
        raise ValueError("Release/deploy authorization revoked or target changed")
    return repo


def validate_pin(repo, policy):
    tag = api(repo, "git/ref/tags/" + quote(policy["ref"], safe="/"))
    if tag.get("object", {}).get("type") != "commit" or tag["object"].get("sha") != policy["sha"]:
        raise ValueError("Trusted workflow tag changed; authorize the workflow again")
    if not compatible_workflow(workflow_content(repo, policy["workflow"], policy["sha"])):
        raise ValueError("Pinned workflow does not implement the RSI dispatch contract")


def find_remote(repo, data):
    endpoint = "actions/workflows/" + quote(data["workflow"].split("/")[-1], safe="") + "/runs?event=workflow_dispatch&per_page=100"
    found = []
    for page in range(1, 11):
        runs = api(repo, endpoint + "&page=" + str(page)).get("workflow_runs", [])
        found.extend(item for item in runs if item.get("display_title") == data["operation_id"])
        if len(runs) < 100:
            break
    if len(found) > 1:
        raise ValueError("Multiple workflow runs match one operation; inspect GitHub before retrying")
    return found[0] if found else None


def reconcile(runtime, run, kind, data, persist=True):
    repo = data["repository"]
    remote = (api(repo, "actions/runs/" + str(data["workflow_run_id"])) if data.get("workflow_run_id")
              else find_remote(repo, data))
    if not remote:
        return data
    if (remote.get("head_sha") != data["workflow_sha"] or remote.get("display_title") != data["operation_id"] or
            remote.get("event") != "workflow_dispatch" or remote.get("path", "").split("@")[0] != data["workflow"]):
        raise ValueError("Workflow run identity differs from the authorized operation")
    if data.get("rerun_pending") or data.get("state") in {"rerunning", "rerun_unknown"}:
        if remote.get("run_attempt", 1) <= data.get("previous_attempt", 1):
            requested = data.setdefault("rerun_requested_at", data.get("checked_at", time.time()))
            data.update(rerun_pending=True, checked_at=time.time())
            if time.time() - requested >= RERUN_CONFIRM_SECONDS:
                data.update(state="rerun_unknown", error="GitHub has not confirmed a new run attempt; inspect the original run. Reconciliation never repeats an uncertain rerun request.")
            return save(runtime, run, kind, data) if persist else data
        data.update(rerun_pending=False, rerun_receipt="confirmed", error="")
    data.update(workflow_run_id=remote["id"], url=remote.get("html_url", ""),
                attempt=remote.get("run_attempt", 1), checked_at=time.time(), conclusion=remote.get("conclusion"))
    if remote.get("status") == "completed":
        data["state"] = "workflow_succeeded" if remote.get("conclusion") == "success" else "failed"
        if kind == "deploy":
            data["health"] = "unverified"
    else:
        data["state"] = "running" if remote.get("status") == "in_progress" else "queued"
    return save(runtime, run, kind, data) if persist else data


def health_check(url, sha):
    class SameOrigin(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            original, target = urlsplit(url), urlsplit(newurl)
            if (target.scheme != "https" or (target.hostname, target.port or 443) != (original.hostname, original.port or 443) or
                    target.username is not None or target.password is not None or target.query or target.fragment):
                raise ValueError("Deployment health redirect changed the authorized origin")
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    result = {"url": url, "sha": sha, "checked_at": time.time(), "healthy": False}
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Adaptive-RSI-health"})
        with urllib.request.build_opener(SameOrigin()).open(request, timeout=10) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("Deployment health response exceeds size limit")
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("sha") != sha or not (data.get("healthy") is True or data.get("status") == "ok"):
                raise ValueError("Health response must confirm the exact deployment SHA and healthy status")
            result["healthy"] = response.status == 200
            result["status_code"] = response.status
    except Exception as exc:
        result["error"] = str(exc)[:500]
    return result


def stable_key(project, repo, url):
    return "deployment-stable:" + fingerprint({"project": project, "repository": repo, "health_url": url})


def owner_key(project, repo, policy):
    return "deployment-owner:" + fingerprint({"project": project, "repository": repo,
                                              "target": policy.get("health_url") or policy["workflow"]})


def deployment_terminal(data, policy):
    if data.get("state") == "workflow_succeeded":
        return not policy.get("health_url")
    if data.get("state") == "health_failed":
        return not policy.get("automatic_rollback") or data.get("rollback_unavailable", False)
    return data.get("state") in {"healthy", "failed", "rolled_back", "rollback_failed", "superseded"}


def deployment_health(runtime, project, run, data, policy, cancel):
    if not policy.get("health_url"):
        return data
    owner = runtime.store.setting(owner_key(project["id"], data["repository"], policy))
    if owner and owner["run"] != run["id"]:
        data.update(state="superseded", error="A newer deployment owns this target; no stale rollback will be issued")
        return data
    if data["state"] == "workflow_succeeded":
        authorized(runtime, project, run, "deploy", policy, cancel)
        report = health_check(policy["health_url"], data["sha"])
        with runtime.dispatch_lock:
            authorized(runtime, project, run, "deploy", policy, cancel)
            current_owner = runtime.store.setting(owner_key(project["id"], data["repository"], policy))
            if current_owner and current_owner["run"] != run["id"]:
                data.update(state="superseded", error="A newer deployment owns this target; old health evidence was discarded")
                return data
            data.update(health=report, state="healthy" if report["healthy"] else "health_failed")
            if not report["healthy"]:
                data["rollback_target"] = runtime.store.setting(stable_key(project["id"], data["repository"], policy["health_url"]))
            save(runtime, run, "deploy", data)
            if report["healthy"]:
                runtime.store.put_setting(stable_key(project["id"], data["repository"], policy["health_url"]),
                                          {"sha": data["sha"], "run": run["id"], "health": report})
    if data["state"] == "health_failed" and policy.get("automatic_rollback"):
        previous = data.get("rollback_target")
        if not previous or previous["sha"] == data["sha"]:
            data["rollback_unavailable"] = True
            return data
        with runtime.dispatch_lock:
            repo = authorized(runtime, project, run, "deploy", policy, cancel)
            validate_pin(repo, policy)
            authorized(runtime, project, run, "deploy", policy, cancel)
            current_owner = runtime.store.setting(owner_key(project["id"], repo, policy))
            if current_owner and current_owner["run"] != run["id"]:
                data.update(state="superseded", error="A newer deployment owns this target; rollback was cancelled")
                return data
            rollback = {**{k: data[k] for k in ["repository", "workflow", "ref", "workflow_sha"]},
                        "sha": previous["sha"], "operation_id": data["operation_id"] + "-rollback", "state": "dispatching"}
            data.update(state="rolling_back", rollback=rollback)
            save(runtime, run, "deploy", data)
            try:
                api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                    {"ref": policy["ref"], "inputs[rsi_operation_id]": rollback["operation_id"], "inputs[rsi_sha]": rollback["sha"]})
                rollback.update(state="submitted", dispatch_receipt="accepted")
            except ApiRejected as exc:
                rollback.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                                dispatch_http_status=exc.status)
                data.update(state="rollback_failed", error=rollback["error"])
                save(runtime, run, "deploy", data)
                raise
            except Exception as exc:
                rollback.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
                save(runtime, run, "deploy", data)
                raise
    if data["state"] == "rolling_back":
        rollback = reconcile(runtime, run, "deploy", data["rollback"], persist=False)
        data["rollback"] = rollback
        if rollback["state"] in {"failed", "rerun_unknown"}:
            data["state"] = "rollback_failed"
            if rollback["state"] == "rerun_unknown":
                data["error"] = rollback["error"]
        elif rollback["state"] == "workflow_succeeded":
            authorized(runtime, project, run, "deploy", policy, cancel)
            rollback["health"] = health_check(policy["health_url"], rollback["sha"])
            data["state"] = "rolled_back" if rollback["health"]["healthy"] else "rollback_failed"
    return data


def advance(runtime, project, run, outcome, cancel=None):
    if not re.fullmatch(r"[a-f0-9]{40}", outcome.get("merged_sha", "")):
        return {}
    saved = json.loads(run["config"])
    result = {}
    for kind in KINDS:
        policy = saved["delivery"].get(kind, {})
        data = runtime.store.setting(key(run["id"], kind))
        if not policy.get("enabled") and not data:
            continue
        try:
            if (not data or data.get("state") in {"pending", "blocked", "waiting_verification", "blocked_verification", "waiting_target", "waiting_release"}) and outcome.get("feedback", {}).get("state") != "verified":
                state = "waiting_verification" if not outcome.get("feedback") else "blocked_verification"
                data = {"state": state, "error": "Post-merge verification must pass before release/deploy"}
                result[kind] = save(runtime, run, kind, data)
                continue
            if data and data.get("state") in {"dispatching", "submitted", "dispatch_unknown", "queued", "running", "rerunning", "rerun_unknown"}:
                data = reconcile(runtime, run, kind, data)
            if kind == "deploy" and data and data["state"] in {"workflow_succeeded", "health_failed", "rolling_back"}:
                data = deployment_health(runtime, project, run, data, policy, cancel)
                save(runtime, run, kind, data)
            if data and data["state"] not in {"pending", "blocked", "waiting_verification", "blocked_verification", "waiting_target", "waiting_release"}:
                result[kind] = data
                continue
            if kind == "deploy" and saved["delivery"].get("release", {}).get("enabled"):
                if result.get("release", {}).get("state") != "workflow_succeeded":
                    data = {**(data or {}), "state": "waiting_release"}
                    result[kind] = save(runtime, run, kind, data)
                    continue
            with runtime.dispatch_lock:
                repo = authorized(runtime, project, run, kind, policy, cancel)
                expected_owner = None
                if kind == "deploy":
                    owner = runtime.store.setting(owner_key(project["id"], repo, policy))
                    expected_owner = owner
                    if owner and owner["run"] != run["id"]:
                        previous = runtime.store.setting(key(owner["run"], "deploy"), {})
                        if not deployment_terminal(previous, policy):
                            result[kind] = save(runtime, run, kind, {"state": "waiting_target", "error": "Another deployment is still active on this target"})
                            continue
                        comparison = api(repo, "compare/" + owner["sha"] + "..." + outcome["merged_sha"])
                        if comparison.get("status") != "ahead":
                            result[kind] = save(runtime, run, kind, {"state": "superseded", "error": "Deployment target already has this or a newer commit; refusing a stale or diverged deployment"})
                            continue
                validate_pin(repo, policy)
                authorized(runtime, project, run, kind, policy, cancel)
                if kind == "deploy" and runtime.store.setting(owner_key(project["id"], repo, policy)) != expected_owner:
                    result[kind] = save(runtime, run, kind, {"state": "waiting_target", "error": "Deployment ownership changed during validation"})
                    continue
                if not data or not data.get("operation_id"):
                    operation = "rsi-" + fingerprint({"project": project["id"], "run": run["id"], "kind": kind,
                                                      "repository": repo, "sha": outcome["merged_sha"]})[:32]
                    data = {"state": "pending", "repository": repo, "sha": outcome["merged_sha"], "kind": kind,
                            "workflow": policy["workflow"], "ref": policy["ref"], "workflow_sha": policy["sha"],
                            "operation_id": operation, "created_at": time.time(), "attempt": 0}
                # Persist the write intent first. Ambiguous failures are reconciled, never re-dispatched.
                data.update(state="dispatching", error="")
                save(runtime, run, kind, data)
                if kind == "deploy":
                    runtime.store.put_setting(owner_key(project["id"], repo, policy), {"run": run["id"], "sha": data["sha"]})
                try:
                    api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                        {"ref": policy["ref"], "inputs[rsi_operation_id]": data["operation_id"], "inputs[rsi_sha]": data["sha"]})
                except ApiRejected as exc:
                    data.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                                dispatch_http_status=exc.status)
                    raise
                except Exception as exc:
                    data.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
                    raise
                data.update(state="submitted", dispatch_receipt="accepted")
                save(runtime, run, kind, data)
                runtime.store.event("automation_dispatched", "Authorized " + kind + " workflow submitted", project["id"], run["id"], data)
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
            runtime.store.event("automation_error", str(exc)[:500], project["id"], run["id"], {"kind": kind})
        if data:
            result[kind] = save(runtime, run, kind, data)
    return result


def retry(runtime, project_id, run_id, kind):
    if kind not in KINDS:
        raise ValueError("Unknown automation kind")
    with runtime.dispatch_lock:
        run = runtime.store.one("SELECT * FROM runs WHERE id=? AND project=? AND state='merged'", (run_id, project_id))
        if not run:
            raise ValueError("Only a verified merged run in this project can be retried")
        policy = json.loads(run["config"])["delivery"].get(kind, {})
        project = runtime.project(project_id)
        repo = authorized(runtime, project, run, kind, policy)
        if kind == "deploy":
            owner = runtime.store.setting(owner_key(project_id, repo, policy))
            if owner and owner["run"] != run_id:
                raise ValueError("A newer deployment owns this target; stale delivery cannot be retried")
        if json.loads(run["result"]).get("feedback", {}).get("state") != "verified":
            raise ValueError("Post-merge verification must pass before retrying delivery")
        data = runtime.store.setting(key(run_id, kind))
        if not data or data.get("state") == "blocked":
            raise ValueError("Restore the original authorization and let tracking retry the pending operation")
        if not data.get("operation_id"):
            raise ValueError("This operation has no failed GitHub workflow to retry")
        if kind == "deploy" and data.get("state") == "health_failed":
            data.update(state="workflow_succeeded", error="")
            save(runtime, run, kind, data)
            runtime.wake.set()
            return data
        target = data
        if kind == "deploy" and data.get("state") == "rollback_failed":
            target = data["rollback"]
            if target.get("state") == "workflow_succeeded":
                data.update(state="rolling_back", error="")
                save(runtime, run, kind, data)
                runtime.wake.set()
                return data
        if (target.get("state") == "failed" and target.get("dispatch_receipt") == "rejected" and
                not target.get("workflow_run_id")):
            validate_pin(repo, policy)
            authorized(runtime, project, run, kind, policy)
            target.update(state="dispatching", error="", dispatch_receipt="pending")
            if target is not data:
                data.update(state="rolling_back", rollback=target, error="")
            save(runtime, run, kind, data)
            try:
                api(repo, "actions/workflows/" + quote(policy["workflow"].split("/")[-1], safe="") + "/dispatches", "POST",
                    {"ref": policy["ref"], "inputs[rsi_operation_id]": target["operation_id"], "inputs[rsi_sha]": target["sha"]})
            except ApiRejected as exc:
                target.update(state="failed", error=str(exc)[:500], dispatch_receipt="rejected",
                              dispatch_http_status=exc.status)
                if target is not data:
                    data.update(state="rollback_failed", error=target["error"])
                save(runtime, run, kind, data)
                raise
            except Exception as exc:
                target.update(state="dispatch_unknown", error=str(exc)[:500], dispatch_receipt="unknown")
                if target is not data:
                    data.update(state="rolling_back", error=target["error"])
                save(runtime, run, kind, data)
                runtime.wake.set()
                raise
            target.update(state="submitted", error="", dispatch_receipt="accepted")
            if target is not data:
                data.update(state="rolling_back", error="")
            save(runtime, run, kind, data)
            runtime.wake.set()
            return data
        unconfirmed_rerun = target.get("rerun_pending") or target.get("state") in {"rerunning", "rerun_unknown"}
        target = reconcile(runtime, run, kind, target, persist=False)
        if unconfirmed_rerun:
            if target is not data:
                data.update(state="rollback_failed" if target["state"] in {"failed", "rerun_unknown"} else "rolling_back", rollback=target, error=target.get("error", ""))
            save(runtime, run, kind, data)
            runtime.wake.set()
            return data
        if target.get("state") != "failed" or not target.get("workflow_run_id"):
            raise ValueError("Only a confirmed failed workflow run can be retried; uncertain dispatches require reconciliation")
        validate_pin(repo, policy)
        authorized(runtime, project, run, kind, policy)
        target.update(state="rerunning", error="", previous_attempt=target.get("attempt", 1),
                      rerun_pending=True, rerun_receipt="pending", rerun_requested_at=time.time())
        if target is not data:
            data.update(state="rolling_back", rollback=target)
        save(runtime, run, kind, data)
        try:
            api(repo, "actions/runs/" + str(target["workflow_run_id"]) + "/rerun", "POST")
        except ApiRejected as exc:
            target.update(state="failed", error=str(exc)[:500], rerun_pending=False, rerun_receipt="rejected", rerun_http_status=exc.status)
            if target is not data:
                data.update(state="rollback_failed", error=target["error"], rollback=target)
            save(runtime, run, kind, data)
            raise
        except Exception as exc:
            target.update(error=str(exc)[:500], rerun_receipt="unknown")
            save(runtime, run, kind, data)
            runtime.wake.set()
            raise
        target["rerun_receipt"] = "accepted"
        save(runtime, run, kind, data)
        runtime.wake.set()
        return data
