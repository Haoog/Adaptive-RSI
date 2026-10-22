import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .build import BUILD_ID
from .dashboard import records
from .adapters import detect
from .catalog import CatalogError, list_models
from .credentials import CredentialError
from .model_center import CenterError
from .telemetry import quality_summary
from .config import compatibility
from .gitops import git
from .evolution import Evolution
from .updates import UpdateInProgress


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # 本地控制台不需要反向 DNS；解析阻塞不能拖住更新后的启动。
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def __init__(self, runtime, port=0, session=None, public_url=None):
        if public_url:
            parsed = urlparse(public_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ["", "/"] or parsed.query or parsed.fragment:
                raise ValueError("Public URL must be an HTTPS origin without path, credentials or query")
        self.runtime = runtime
        self.assets = {p.name: p.read_bytes() for p in (Path(__file__).parent / "web").iterdir() if p.is_file()}
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.local_origin = "http://127.0.0.1:" + str(self.server_port)
        self.origin = (public_url or (session or {}).get("public_url") or self.local_origin).rstrip("/")
        self.allowed_hosts = {urlparse(self.origin).netloc, urlparse(self.local_origin).netloc}
        self.cookie = "rsi_" + str(self.server_port)
        self.url = self.origin + "/?token=" + self.token
        if session:
            if session["origin"] != self.origin or session["cookie"] != self.cookie:
                raise RuntimeError("Restart session does not match this listener")
            self.token, self.csrf, self.url = session["token"], session["csrf"], session["url"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")

    def respond(self, data, code=200, content_type="application/json; charset=utf-8"):
        payload = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith("application/json") else data
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            self.send_response(code)
            self.headers_common()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Aborted browser fetches are normal during navigation; do not retry a response to a closed socket.
            self.close_connection = True

    def authenticated(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.respond({"error": "Invalid Host"}, 403)
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie[self.server.cookie].value if self.server.cookie in cookie else ""
        except Exception:
            token = ""
        if not hmac.compare_digest(token, self.server.token):
            if self.command == "GET" and urlparse(self.path).path in ["/", "/index.html"]:
                self.respond((Path(__file__).parent / "web" / "login.html").read_bytes(), 401, "text/html; charset=utf-8")
            else:
                self.respond({"error": "Open the authenticated URL printed by adaptive-rsi serve/open", "error_code": "session_required"}, 401)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/login.css" and self.headers.get("Host") in self.server.allowed_hosts:
            self.respond((Path(__file__).parent / "web" / "login.css").read_bytes(), content_type="text/css; charset=utf-8")
            return
        if parsed.path == "/" and "token" in params:
            if self.headers.get("Host") not in self.server.allowed_hosts or not hmac.compare_digest(params["token"][0], self.server.token):
                self.respond({"error": "Invalid login"}, 403)
                return
            self.send_response(303)
            self.headers_common()
            self.send_header("Set-Cookie", self.server.cookie + "=" + self.server.token + "; Path=/; HttpOnly; SameSite=Strict" +
                             ("; Secure" if self.server.origin.startswith("https://") and self.headers.get("Host") == urlparse(self.server.origin).netloc else ""))
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.authenticated():
            return
        try:
            runtime = self.server.runtime
            store = runtime.store
            if parsed.path == "/api/health":
                self.respond({"version": __version__, "build": BUILD_ID, "pid": os.getpid(), "api_version": 1})
            elif parsed.path == "/api/usage":
                from .usage_report import report
                self.respond(report(store, params))
            elif parsed.path == "/api/state":
                legacy = "sections" not in params
                requested = {part.strip() for value in params.get("sections", []) for part in value.split(",") if part.strip()}
                unknown = requested - {"core", "candidates", "analytics"}
                if unknown:
                    raise ValueError("Unknown state section: " + ", ".join(sorted(unknown)))
                try:
                    global_config = runtime.config()
                except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
                    raise ValueError("Global configuration is invalid: " + str(exc)) from None
                projects = store.query("SELECT * FROM projects ORDER BY created")
                raw_project_configs = {}
                for project in projects:
                    raw_config = project["config"]
                    raw_project_configs[project["id"]] = raw_config
                    overrides = None
                    try:
                        overrides = json.loads(raw_config or "{}")
                        if not isinstance(overrides, dict):
                            raise ValueError("Project configuration must be a JSON object")
                        project["overrides"] = overrides
                        project["config"] = runtime.config(project["id"])
                    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
                        project["configuration_error"] = str(exc)
                        project["configured_paused"] = bool(project["paused"])
                        project["paused"] = True
                        project["overrides"] = overrides
                        project["raw_overrides"] = raw_config
                        project["config"] = global_config
                selected = params.get("project", [None])[0] or (projects[0]["id"] if projects else None)
                if selected not in {p["id"] for p in projects}:
                    selected = projects[0]["id"] if projects else None
                rows = records(store, selected, params.get("limit", [50])[0], params.get("offset", [0])[0],
                               params.get("filter", ["all"])[0], params.get("system", ["true"])[0] == "true")
                schedules = store.query("SELECT * FROM schedules")
                project_by_id = {project["id"]: project for project in projects}
                for schedule in schedules:
                    project = project_by_id.get(schedule["project"])
                    if project and project.get("configuration_error"):
                        schedule["next_effective"] = None
                        continue
                    try:
                        schedule["next_effective"] = runtime.next_effective_schedule(schedule["project"], schedule["due"])
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        if project:
                            project["configuration_error"] = str(exc)
                            project["configured_paused"] = bool(project["paused"])
                            project["paused"] = True
                            project["raw_overrides"] = raw_project_configs.get(project["id"])
                            project["config"] = global_config
                        schedule["next_effective"] = None
                data = {"version": __version__, "build": BUILD_ID, "selected_project": selected,
                        "csrf": self.server.csrf, "projects": projects, **rows, "config": global_config,
                        "update": runtime.updates.snapshot(), "schedules": schedules}
                include_candidates = legacy or "candidates" in requested
                include_analytics = legacy or "analytics" in requested
                if include_candidates:
                    candidates = store.query("SELECT * FROM candidates " + ("WHERE project=? " if selected else "") + "ORDER BY created DESC LIMIT 100", (selected,) if selected else ())
                    for candidate in candidates:
                        evidence = json.loads(candidate.pop("evidence"))
                        candidate["content"] = json.loads(candidate["content"])
                        candidate["reason"] = evidence.get("reason", "")
                        candidate["trial_count"] = len(evidence.get("trials", []))
                        candidate["monitor"] = evidence.get("monitor", {})
                    data["candidates"] = candidates
                if include_analytics:
                    data.update({
                        "cases": store.query("SELECT project,split,count(*) AS count,sum(uses) AS uses FROM cases GROUP BY project,split"),
                        "quality": quality_summary(store, selected),
                        "usage": store.query("SELECT project,count(*) AS calls,sum(tokens) AS tokens,sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,sum(cache_read_tokens) AS cache_read_tokens,sum(cache_write_tokens) AS cache_write_tokens,sum(usage_known=0) AS unknown_usage,sum(seconds) AS seconds,sum(state='failed') AS failed,sum(CASE WHEN unit_price IS NOT NULL THEN tokens*unit_price/1000000 END) AS estimate,sum(unit_price IS NULL) AS unpriced FROM calls WHERE created>? GROUP BY project", (time.time() - 86400,)),
                        "model_usage": store.query("SELECT project,provider,model,unit,count(*) AS calls,sum(tokens) AS tokens,sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,sum(cache_read_tokens) AS cache_read_tokens,sum(cache_write_tokens) AS cache_write_tokens,sum(usage_known=0) AS unknown_usage,sum(seconds) AS seconds,sum(state='failed') AS failed,sum(CASE WHEN unit_price IS NOT NULL THEN tokens*unit_price/1000000 END) AS estimate,sum(unit_price IS NULL) AS unpriced FROM calls WHERE created>? GROUP BY project,provider,model,unit", (time.time() - 86400,))})
                if not legacy:
                    data["sections"] = ["core", *sorted(requested - {"core"})]
                self.respond(data)
            elif parsed.path == "/api/readiness":
                from .readiness import inspect
                self.respond(inspect(runtime, runtime.project(params.get("project", [""])[0])))
            elif parsed.path == "/api/auth/status":
                self.respond(runtime.auth_sessions.snapshot())
            elif parsed.path == "/api/auth/providers":
                self.respond(runtime.auth_sessions.providers(params.get("backend", ["opencode"])[0]))
            elif parsed.path == "/api/auth/session":
                self.respond(runtime.auth_sessions.status(params.get("id", [""])[0]))
            elif parsed.path == "/api/release-status":
                self.respond(runtime.release_status(params.get("project", [""])[0], params.get("run", [""])[0]))
            elif parsed.path == "/api/release-workflows":
                self.respond(runtime.workflow_choices(params.get("project", [""])[0]))
            elif parsed.path == "/api/task-runs":
                task = store.one("SELECT project FROM tasks WHERE id=?", (params.get("task", [""])[0],))
                if not task or task["project"] != params.get("project", [""])[0]:
                    raise ValueError("Task not found")
                self.respond(store.query("SELECT id,project,task,state,started,ended FROM runs WHERE task=? ORDER BY started DESC LIMIT 100", (params["task"][0],)))
            elif parsed.path == "/api/capabilities":
                project = runtime.project(params.get("project", [""])[0])
                data = detect(project["path"])
                proof = store.setting("verified-checks:" + project["id"])
                if proof and proof["base"] == git(project["path"], "rev-parse", "HEAD") and proof["compatibility"] == compatibility(runtime.config(project["id"])):
                    verified = {(c.get("cwd", "."), tuple(c["argv"])) for c in proof["checks"]}
                    for module in data["modules"]:
                        if module["checks"] and all((c.get("cwd", "."), tuple(c["argv"])) in verified for c in module["checks"]):
                            module["status"] = "executable"
                            module["verified_at"] = proof["at"]
                self.respond(data)
            elif parsed.path == "/api/model-center":
                self.respond(runtime.model_center.snapshot(params.get("project", [None])[0] or None))
            elif parsed.path == "/api/run":
                run = store.one("SELECT * FROM runs WHERE id=?", (params.get("id", [""])[0],))
                if not run:
                    raise ValueError("Run not found")
                run["result"] = json.loads(run["result"])
                run["config"] = json.loads(run["config"])
                run["stages"] = store.query("SELECT stage,created FROM stages WHERE run=? ORDER BY created", (run["id"],))
                for stage in run["stages"]:
                    saved = store.one("SELECT data FROM stages WHERE run=? AND stage=?", (run["id"], stage["stage"]))
                    stage["decision"] = json.loads(saved["data"]).get("decision")
                self.respond(run)
            elif parsed.path == "/api/patch":
                run = store.one("SELECT path FROM runs WHERE id=?", (params.get("id", [""])[0],))
                if not run or not run["path"]:
                    raise ValueError("Run has no artifacts")
                path = Path(run["path"]) / "candidate.patch"
                self.respond(path.read_bytes(), content_type="text/plain; charset=utf-8")
            elif parsed.path in ["/", "/index.html", "/app.js", "/model-center.js", "/auth.js", "/desktop.js", "/usage.js", "/i18n.js", "/style.css", "/model-center.css", "/favicon.svg"]:
                name = "index.html" if parsed.path == "/" else parsed.path[1:]
                path = Path(__file__).parent / "web" / name
                payload = self.server.assets[name]
                if name == "index.html":
                    payload = payload.replace(b'<html lang=', ('<html data-build="' + BUILD_ID + '" lang=').encode(), 1)
                self.respond(payload, content_type=(mimetypes.guess_type(str(path))[0] or "application/octet-stream") + "; charset=utf-8")
            else:
                self.respond({"error": "Not found"}, 404)
        except (ValueError, OSError, RuntimeError) as exc:
            self.respond({"error": str(exc), **({"error_code": exc.code} if hasattr(exc, "code") else {})}, 400)

    def do_POST(self):
        if not self.authenticated():
            return
        if self.headers.get("Origin") != self.server.origin or not hmac.compare_digest(self.headers.get("X-RSI-CSRF", ""), self.server.csrf):
            self.respond({"error": "Invalid origin or CSRF token"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError("Invalid request size")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected application/json")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            runtime = self.server.runtime
            path = urlparse(self.path).path
            if path.startswith("/api/auth/"):
                with runtime.dispatch_lock:
                    runtime.updates.require_ready()
                    auth = runtime.auth_sessions
                    if path == "/api/auth/start":
                        result = auth.start(body.get("backend"), body.get("provider"), body.get("method", 0), body.get("inputs"))
                    elif path == "/api/auth/complete":
                        result = auth.complete(body.get("id"), body.get("code", ""))
                    elif path == "/api/auth/cancel":
                        result = auth.cancel(body.get("id"))
                    elif path == "/api/auth/logout":
                        result = auth.logout(body.get("backend"), body.get("provider"))
                    elif path == "/api/auth/key":
                        result = auth.set_api_key(body.get("backend"), body.get("provider"), body.get("key"))
                    else:
                        self.respond({"error": "Not found"}, 404)
                        return
                self.respond(result)
                return
            if path.startswith("/api/model-center/"):
                center = runtime.model_center
                project = body.get("project") or None
                if path == "/api/model-center/fetch":
                    result = center.fetch(body.get("provider"), project)
                elif path == "/api/model-center/probe":
                    result = center.probe(body.get("provider"), body.get("model"), body.get("effort", "default"), project)
                else:
                    with runtime.dispatch_lock:
                        runtime.updates.require_ready()
                        if path == "/api/model-center/provider":
                            result = center.save_provider(body.get("provider"), body.get("authentication", {}), project)
                        elif path == "/api/model-center/remove":
                            result = center.remove_provider(body.get("provider"), project)
                        elif path == "/api/model-center/model":
                            result = center.edit_model(body.get("provider"), body.get("model"), project)
                        elif path == "/api/model-center/routes":
                            result = center.save_routes(body.get("models"), project)
                        elif path == "/api/model-center/resume":
                            result = center.resume_provider(body.get("provider"), project)
                        else:
                            self.respond({"error": "Not found"}, 404)
                            return
                self.respond(result)
                return
            if urlparse(self.path).path == "/api/models":
                try:
                    self.respond(list_models(body.get("provider")))
                except CatalogError as exc:
                    self.respond({"error": str(exc), "catalog_error": exc.code}, 400)
                return
            if path == "/api/projects/import":
                runtime.updates.require_ready()
                self.respond({"id": runtime.import_project(body["url"], body.get("directory"))})
                return
            with runtime.dispatch_lock:
                path = urlparse(self.path).path
                runtime.updates.require_ready()
                if path == "/api/projects":
                    result = {"id": runtime.add_project(body["path"], body.get("name"), body.get("enabled", False))}
                elif path == "/api/updates/check":
                    runtime.updates.check_async()
                    result = {"started": True}
                elif path == "/api/updates/apply":
                    if body.get("confirm") is not True:
                        raise ValueError("Confirm installation and idle restart")
                    runtime.updates.apply_async()
                    result = {"started": True}
                elif path == "/api/config":
                    runtime.configure(body["values"], body.get("project"), body.get("inherit"))
                    result = {"saved": True, "config": runtime.config(body.get("project"))}
                elif path == "/api/config/repair":
                    result = runtime.repair_project_config(body["project"], body.get("confirmation"))
                elif path == "/api/release-retry":
                    result = runtime.retry_release(body.get("project"), body.get("run"), body.get("kind"))
                elif path == "/api/release-authorize":
                    result = runtime.authorize_workflow(body.get("project"), body.get("kind"), body.get("workflow"))
                elif path == "/api/goal":
                    result = {"id": runtime.goal(body["project"], body["text"], body.get("overrides"))}
                elif path == "/api/request":
                    result = {"id": runtime.request(body["project"], body["kind"])}
                elif path == "/api/pause":
                    runtime.pause(body["project"], body["paused"])
                    result = {"saved": True}
                elif path == "/api/retry":
                    runtime.retry(body["task"])
                    result = {"saved": True}
                elif path == "/api/cancel":
                    runtime.cancel_task(body["task"])
                    result = {"saved": True}
                elif path == "/api/deliver":
                    result = {"id": runtime.deliver(body["run"])}
                elif path == "/api/rollback":
                    runtime.rollback_strategy(body["candidate"])
                    result = {"saved": True}
                elif path == "/api/revert-code":
                    result = {"id": runtime.revert_code(body["run"])}
                elif path == "/api/cases":
                    result = {"id": Evolution(runtime).import_case(body["project"], body["manifest"])}
                else:
                    self.respond({"error": "Not found"}, 404)
                    return
                self.respond(result)
        except UpdateInProgress as exc:
            self.respond({"error": str(exc), "error_code": exc.code}, 503)
        except (CenterError, CredentialError, CatalogError) as exc:
            self.respond({"error": str(exc), "catalog_error": exc.code}, 400)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
            status = 409 if getattr(exc, "code", "") == "auth_credentials_busy" else 400
            self.respond({"error": str(exc), **({"error_code": exc.code} if hasattr(exc, "code") else {})}, status)
