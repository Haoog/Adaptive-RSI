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

    def __init__(self, runtime, port=0, session=None):
        self.runtime = runtime
        self.token = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)
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
        self.send_response(code)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authenticated(self):
        if self.headers.get("Host") != self.server.origin.split("//")[1]:
            self.respond({"error": "Invalid Host"}, 403)
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie[self.server.cookie].value if self.server.cookie in cookie else ""
        except Exception:
            token = ""
        if not hmac.compare_digest(token, self.server.token):
            self.respond({"error": "Open the authenticated URL printed by adaptive-rsi serve/open"}, 401)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/" and "token" in params:
            if self.headers.get("Host") != self.server.origin.split("//")[1] or not hmac.compare_digest(params["token"][0], self.server.token):
                self.respond({"error": "Invalid login"}, 403)
                return
            self.send_response(303)
            self.headers_common()
            self.send_header("Set-Cookie", self.server.cookie + "=" + self.server.token + "; Path=/; HttpOnly; SameSite=Strict")
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
                self.respond({"version": __version__, "pid": os.getpid()})
            elif parsed.path == "/api/state":
                projects = store.query("SELECT * FROM projects ORDER BY created")
                for project in projects:
                    project["overrides"] = json.loads(project["config"])
                    project["config"] = runtime.config(project["id"])
                tasks = store.query("SELECT * FROM tasks ORDER BY updated DESC LIMIT 300")
                runs = store.query("SELECT id,project,task,state,calls,tokens,started,ended,base,result FROM runs ORDER BY started DESC LIMIT 100")
                for row in runs:
                    result = json.loads(row.pop("result"))
                    row["summary"] = result.get("summary", "")
                    row["reason"] = result.get("reason", "")
                    row["pr"] = result.get("pr")
                    row["value"] = result.get("value")
                    row["plan_reason"] = result.get("plan_reason", "")
                    row["plan_decision"] = result.get("plan_decision")
                    row["scope"] = result.get("scope", result.get("files", []))
                    row["review_decision"] = result.get("review", {}).get("decision")
                    row["evaluation_decision"] = result.get("evaluation", {}).get("decision")
                candidates = store.query("SELECT * FROM candidates ORDER BY created DESC LIMIT 100")
                for candidate in candidates:
                    evidence = json.loads(candidate.pop("evidence"))
                    candidate["content"] = json.loads(candidate["content"])
                    candidate["reason"] = evidence.get("reason", "")
                    candidate["trial_count"] = len(evidence.get("trials", []))
                    candidate["monitor"] = evidence.get("monitor", {})
                self.respond({"version": __version__, "csrf": self.server.csrf, "projects": projects, "tasks": tasks,
                              "runs": runs, "candidates": candidates, "config": runtime.config(), "update": runtime.updates.snapshot(),
                              "events": store.query("SELECT * FROM events ORDER BY seq DESC LIMIT 100"),
                              "schedules": store.query("SELECT * FROM schedules"),
                              "cases": store.query("SELECT project,split,count(*) AS count,sum(uses) AS uses FROM cases GROUP BY project,split"),
                              "quality": quality_summary(store),
                              "usage": store.query("SELECT project,count(*) AS calls,sum(tokens) AS tokens,sum(seconds) AS seconds,sum(state='failed') AS failed,sum(CASE WHEN unit_price IS NOT NULL THEN tokens*unit_price/1000000 END) AS estimate,sum(unit_price IS NULL) AS unpriced FROM calls WHERE created>? GROUP BY project", (time.time() - 86400,)),
                              "model_usage": store.query("SELECT project,provider,model,count(*) AS calls,sum(tokens) AS tokens,sum(seconds) AS seconds,sum(state='failed') AS failed,sum(CASE WHEN unit_price IS NOT NULL THEN tokens*unit_price/1000000 END) AS estimate,sum(unit_price IS NULL) AS unpriced FROM calls WHERE created>? GROUP BY project,provider,model", (time.time() - 86400,))})
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
                self.respond(run)
            elif parsed.path == "/api/patch":
                run = store.one("SELECT path FROM runs WHERE id=?", (params.get("id", [""])[0],))
                if not run or not run["path"]:
                    raise ValueError("Run has no artifacts")
                path = Path(run["path"]) / "candidate.patch"
                self.respond(path.read_bytes(), content_type="text/plain; charset=utf-8")
            elif parsed.path in ["/", "/index.html", "/app.js", "/model-center.js", "/i18n.js", "/style.css", "/model-center.css", "/favicon.svg"]:
                name = "index.html" if parsed.path == "/" else parsed.path[1:]
                path = Path(__file__).parent / "web" / name
                self.respond(path.read_bytes(), content_type=(mimetypes.guess_type(str(path))[0] or "application/octet-stream") + "; charset=utf-8")
            else:
                self.respond({"error": "Not found"}, 404)
        except (ValueError, OSError, RuntimeError) as exc:
            self.respond({"error": str(exc)}, 400)

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
            with runtime.dispatch_lock:
                path = urlparse(self.path).path
                runtime.updates.require_ready()
                if path == "/api/projects":
                    result = {"id": runtime.add_project(body["path"], body.get("name"), body.get("enabled", False))}
                elif path == "/api/projects/import":
                    result = {"id": runtime.import_project(body["url"], body.get("directory"))}
                elif path == "/api/updates/check":
                    runtime.updates.check_async()
                    result = {"started": True}
                elif path == "/api/updates/apply":
                    if body.get("confirm") is not True:
                        raise ValueError("Confirm installation and idle restart")
                    runtime.updates.apply_async()
                    result = {"started": True}
                elif path == "/api/config":
                    runtime.configure(body["values"], body.get("project"))
                    result = {"saved": True, "config": runtime.config(body.get("project"))}
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
            self.respond({"error": str(exc)}, 400)
