import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
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
from .updates import UpdateInProgress, atomic_json


# 只允许已知机器错误码离开服务端；异常文本可能包含路径、请求正文或凭据。
SAFE_ERROR_CODES = frozenset("""
unknown_provider invalid_provider invalid_mode invalid_key invalid_reference missing_key
keychain_unavailable keychain_locked keychain_failed credential_file_unavailable
builtin_provider provider_in_use update_in_progress connection_changed invalid_model
invalid_routes too_many_models unsupported_model_kind probe_failed default_effort_required
rate_limit auth connection unavailable unknown timeout too_large invalid_response
unsupported http_error provider_paused model_missing tool_format budget_exhausted
codex_missing codex_error opencode_missing opencode_error opencode_version_unknown
opencode_version_unsupported opencode_capability_missing invalid_credentials
invalid_backend invalid_auth_method invalid_auth_inputs invalid_auth_code invalid_auth_state
unknown_session auth_busy auth_credentials_busy service_closed process_interrupted
unsupported_auth_method invalid_api_key auth_protocol_error auth_timeout auth_expired
authorization_failed device_auth_unavailable auth_container_unavailable auth_worker_image_missing
""".split())


# 精确匹配固定业务校验文案；不使用前缀匹配，也不携带用户提交的字段或值。
SAFE_VALIDATION_MESSAGES = frozenset("""
Invalid command timeout: command_seconds must be 60..86400
Too many model providers
Provider requires id, name, base_url, env_key and optional protocol; never store API keys
Provider fields must be strings
Invalid, duplicate or reserved provider id
Provider URL requires HTTPS (HTTP allowed on loopback), without credentials or query
Provider requires a short display name
env_key must be an environment variable name, not a secret
Unsupported model protocol
Unsupported credential mode
Stored credentials require a reference, not an environment variable or key
Unexpected credential reference
Nested fallback routes are not supported
OpenCode model requires provider/model
Luna supports max, not ultra
Review blocking threshold must be low, medium or high
Triage policy requires 1..4000 characters
Invalid update mode or interval (minimum 60 minutes)
Automatic update policy must be patch, minor or stable
light_minutes must be between 5 and 525600
issues_minutes must be between 5 and 525600
deep_minutes must be between 5 and 525600
working_hours must be [start, end], 0 <= start < end <= 24
Maximum concurrency is 4
Unsupported sandbox
Invalid container image
Sandbox limits must be positive
Repository must be owner/name
max_issues must be 1..1000
Invalid issue labels
Project weight must be 1..10
Automatic merge requires PR delivery
Automation workflow must be a fixed .github/workflows YAML file
Automation requires a fixed trusted tag
Automation requires the trusted workflow tag's full commit SHA
Release/deploy requires an explicit project and a pinned workflow tag
Deployment health URL requires HTTPS without credentials, query or fragment
Automatic deployment rollback requires an explicit health URL
Feedback interval must be at least 5 minutes
Invalid base branch
Seed a dependency directory, not the entire repository
Invalid dependency snapshot size limit
Check must contain name, cwd, argv
Check requires a name and argv array
Invalid check arguments
Invalid evolution cost ratio
Invalid relative path
Path must remain inside the project
Select the root of an existing Git repository
Expected a GitHub repository URL
Use a GitHub repository URL such as https://github.com/owner/repository
Import directory already exists; register it as a local project instead
Project not found
Project and controller state must be in separate directory trees
Projects must not contain each other's directories
Goal must contain 1..30000 characters
Wait until active runs finish before importing projects
Wait for the active run to finish stopping before retrying or supplementing this task
Wait until active runs finish before registering projects so every worker uses the same isolation boundaries
Only project scan settings can inherit global defaults
An inherited setting cannot also have an override
""".strip().splitlines())


class RequestError(ValueError):
    """本模块固定的用户提示；其余异常一律通过安全错误边界处理。"""


def configuration_error(exc, raw_config):
    """保留可修复的配置诊断，绝不返回底层异常文本。"""
    if isinstance(exc, json.JSONDecodeError):
        return "Project configuration is invalid: Expecting value (invalid JSON)"
    try:
        values = json.loads(raw_config or "{}")
        timezone = values.get("schedule", {}).get("timezone")
    except (ValueError, TypeError, AttributeError):
        timezone = None
    # 仅回显配置中显式设置的短时区名称；路径、URL 和凭据不能匹配此格式。
    from zoneinfo import ZoneInfoNotFoundError
    if isinstance(exc, ZoneInfoNotFoundError) and isinstance(timezone, str) and re.fullmatch(
            r"[A-Za-z_]{1,32}/[A-Za-z_]{1,32}(?:/[A-Za-z_]{1,32})?", timezone):
        return "Project configuration is invalid: Unknown timezone " + timezone
    if isinstance(exc, ValueError) and str(exc) in SAFE_VALIDATION_MESSAGES:
        return "Project configuration is invalid: " + str(exc)
    return "Project configuration is invalid; check field names, types, model routes and timezone"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
    # 未认证的慢速连接不能无限占用线程；同时打开的连接数超过上限时直接拒绝。
    max_connections = 64

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
        self._connection_gate = threading.BoundedSemaphore(self.max_connections)
        self._session_lock = threading.RLock()
        self._backup_gate = threading.BoundedSemaphore(2)
        self._security_lock = threading.Lock()
        self._failure_window = 0.0
        self._failures = 0
        self._audit_after = 0.0
        self._audit_suppressed = 0
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

    def process_request(self, request, client_address):
        if not self._connection_gate.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_gate.release()

    def cookie_header(self, host):
        return (self.cookie + "=" + self.token + "; Path=/; HttpOnly; SameSite=Strict" +
                ("; Secure" if self.origin.startswith("https://") and host == urlparse(self.origin).netloc else ""))

    def security_failure(self, reason, authentication=False):
        """全监听器有界计数；不信任代理 IP 头，也不保留攻击者可扩展的键。"""
        now = time.monotonic()
        with self._security_lock:
            if now >= self._failure_window:
                self._failure_window, self._failures = now + 60, 0
            if authentication:
                self._failures += 1
            limited = authentication and self._failures > 30
            audit = now >= self._audit_after
            suppressed = self._audit_suppressed
            if audit:
                self._audit_after, self._audit_suppressed = now + 60, 0
            else:
                self._audit_suppressed += 1
        if audit:
            try:
                self.runtime.store.event("security_failure", "Console request rejected",
                                         data={"reason": reason, "suppressed": suppressed})
            except (OSError, RuntimeError, sqlite3.Error):
                pass  # 审计不可用不能改变认证判断或回显数据库异常。
        return max(1, int(self._failure_window - now) + 1) if limited else 0

    def rotate_session(self):
        """先私有原子落盘，成功后发布新会话；失败时保留现有登录。"""
        with self._session_lock:
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            url = self.origin + "/?token=" + token
            path = self.runtime.store.root / "server.json"
            # 不读取旧 session 文件，避免跟随链接读取任意文件；会话字段均由监听器生成。
            session = {"origin": self.origin, "local_origin": self.local_origin,
                       "public_url": self.origin if self.origin != self.local_origin else None,
                       "cookie": self.cookie, "token": token, "csrf": csrf,
                       "url": url, "pid": os.getpid()}
            atomic_json(path, session)
            self.token, self.csrf, self.url = token, csrf, url
            return {"url": url, "csrf": csrf}


class Handler(BaseHTTPRequestHandler):
    # 慢速或悬挂的连接最多占用线程 30 秒；超时由 handle_one_request 关闭连接。
    timeout = 30

    def log_message(self, *args):
        pass

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def respond(self, data, code=200, content_type="application/json; charset=utf-8", headers=None):
        payload = json.dumps(data, ensure_ascii=False).encode() if content_type.startswith("application/json") else data
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            self.send_response(code)
            self.headers_common()
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Aborted browser fetches are normal during navigation; do not retry a response to a closed socket.
            self.close_connection = True

    def api_error(self, exc):
        if isinstance(exc, RequestError):
            self.respond({"error": str(exc)}, 400)
            return
        code = getattr(exc, "code", None)
        code = code if isinstance(code, str) and (code in SAFE_ERROR_CODES or
                    re.fullmatch(r"opencode_incompatible_[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", code)) else None
        if isinstance(exc, UpdateInProgress):
            self.respond({"error": "Software update in progress", "error_code": "update_in_progress"}, 503)
        elif code:
            key = "catalog_error" if isinstance(exc, (CenterError, CredentialError, CatalogError)) else "error_code"
            self.respond({"error": "Request failed: " + code, key: code},
                         409 if code == "auth_credentials_busy" else 400)
        elif isinstance(exc, ValueError) and str(exc) in SAFE_VALIDATION_MESSAGES:
            self.respond({"error": str(exc), "error_code": "validation_error"}, 400)
        elif isinstance(exc, (ValueError, KeyError, TypeError)):
            path = urlparse(self.path).path
            if path in {"/api/config", "/api/model-center/routes"}:
                self.respond({"error": "Check configuration field names, types, model routes and timezone",
                              "error_code": "invalid_configuration"}, 400)
            else:
                self.respond({"error": "Invalid request", "error_code": "invalid_request"}, 400)
        else:
            self.respond({"error": "Request could not be completed", "error_code": "internal_error"}, 500)

    def backup(self, path, params):
        if not self.server._backup_gate.acquire(blocking=False):
            self.respond({"error": "Backup downloads busy"}, 429, headers={"Retry-After": "1"})
            return
        try:
            # 同一个 fd 用于检查、长度和传输；不跟随符号链接，不打开 FIFO 阻塞线程。
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags)
            except FileNotFoundError:
                raise RequestError("Backup not found") from None
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise RequestError("Invalid backup file")
                size = info.st_size
                if size > 1024 * 1024 * 1024:
                    raise RequestError("Backup exceeds download size limit")
                if params.get("format", [""])[0] == "json":
                    import base64
                    offset = int(params.get("offset", [0])[0])
                    if not 0 <= offset <= size:
                        raise RequestError("Invalid backup offset")
                    stream.seek(offset)
                    chunk = stream.read(min(512 * 1024, size - offset))
                    self.respond({"data": base64.b64encode(chunk).decode("ascii"),
                                  "size": size, "offset": offset, "length": len(chunk)})
                    return
                self.send_response(200)
                self.headers_common()
                self.send_header("Content-Type", "application/vnd.sqlite3")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                remaining = size
                deadline = time.monotonic() + 120
                try:
                    while remaining:
                        budget = deadline - time.monotonic()
                        if budget <= 0:
                            break
                        self.connection.settimeout(min(self.timeout, budget))
                        chunk = stream.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except OSError:
                    pass  # 响应头已发出；断线或磁盘错误时只关闭连接，不再追加 JSON。
                finally:
                    self.close_connection = True
        finally:
            self.server._backup_gate.release()

    def authentication_failure(self):
        retry = self.server.security_failure("session_required", authentication=True)
        if retry:
            self.respond({"error": "Too many authentication failures", "error_code": "session_required"},
                         429, headers={"Retry-After": str(retry)})
        elif self.command == "GET" and urlparse(self.path).path in ["/", "/index.html"]:
            self.respond(self.server.assets["login.html"], 401, "text/html; charset=utf-8")
        else:
            self.respond({"error": "Open the authenticated URL printed by adaptive-rsi serve/open", "error_code": "session_required"}, 401)
        return False

    @staticmethod
    def _token_matches(received, expected):
        """常量时间比较；非 ASCII 输入按不匹配处理而不是抛 TypeError 重置连接。"""
        try:
            return hmac.compare_digest(received.encode("utf-8", "surrogatepass"),
                                       expected.encode("utf-8", "surrogatepass"))
        except (AttributeError, ValueError):
            return False

    def authenticated(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.server.security_failure("invalid_host")
            self.respond({"error": "Invalid Host"}, 403)
            return False
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie[self.server.cookie].value if self.server.cookie in cookie else ""
        except Exception:
            token = ""
        if not self._token_matches(token, self.server.token):
            return self.authentication_failure()
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/login.css" and self.headers.get("Host") in self.server.allowed_hosts:
            self.respond(self.server.assets["login.css"], content_type="text/css; charset=utf-8")
            return
        if parsed.path in ["/install", "/install.js", "/install.css", "/i18n.js", "/manifest.webmanifest", "/sw.js", "/icon-192.png", "/icon-512.png", "/favicon.svg"]:
            if self.headers.get("Host") not in self.server.allowed_hosts:
                self.server.security_failure("invalid_host")
                self.respond({"error": "Invalid Host"}, 403)
                return
            name = "install.html" if parsed.path == "/install" else parsed.path[1:]
            self.respond(self.server.assets[name], content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
            return
        if parsed.path == "/" and "token" in params:
            valid = self.headers.get("Host") in self.server.allowed_hosts and self._token_matches(params["token"][0], self.server.token)
            if not valid:
                # 旧启动地址携带的 token 可能已轮换；已持有有效会话时仍直接回到控制台。
                if not self.authenticated():
                    return
            self.send_response(303)
            self.headers_common()
            if valid:
                self.send_header("Set-Cookie", self.server.cookie_header(self.headers.get("Host")))
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
            elif parsed.path == "/api/mobile":
                self.respond({"install_url": self.server.origin + "/install", "remote": self.server.origin.startswith("https://")})
            elif parsed.path == "/api/attention":
                data = records(store, group="attention", include_system=False, limit=200)
                data["projects"] = store.query("SELECT id,name FROM projects ORDER BY created")
                from .store import encode, uid
                store.execute("INSERT OR IGNORE INTO settings VALUES ('instance-id',?)", (encode(uid()),))
                data["instance"] = store.setting("instance-id")
                from .dashboard import TASKS
                data["notifications"] = store.query("SELECT t.id,t.project,t.title,t.state,t.display_state,t.updated,r.id AS run,r.result FROM " + TASKS + " t LEFT JOIN runs r ON r.id=(SELECT id FROM runs WHERE task=t.id ORDER BY started DESC,id DESC LIMIT 1) WHERE t.source NOT LIKE 'system_%' ORDER BY t.updated DESC LIMIT 200")
                self.respond(data)
            elif parsed.path == "/api/storage":
                from .storage_tools import overview
                self.respond(overview(store))
            elif parsed.path == "/api/diagnostics":
                from .storage_tools import diagnostics
                self.respond(diagnostics(store))
            elif parsed.path == "/api/backup":
                identifier = params.get("id", [""])[0]
                if len(identifier) != 16 or any(c not in "0123456789abcdef" for c in identifier):
                    raise RequestError("Invalid backup ID")
                path = store.root / "backups" / (identifier + ".sqlite3")
                self.backup(path, params)
            elif parsed.path == "/api/task":
                task = store.one("SELECT * FROM tasks WHERE id=? AND project=?", (params.get("id", [""])[0], params.get("project", [""])[0]))
                if not task:
                    raise RequestError("Task not found")
                task["revisions"] = store.query("SELECT id,supplement,created FROM task_revisions WHERE task=? ORDER BY id", (task["id"],))
                task["decision"] = store.one("SELECT data,created FROM task_decisions WHERE task=?", (task["id"],))
                task["dependency"] = store.one("SELECT dependency FROM task_dependencies WHERE task=?", (task["id"],))
                self.respond(task)
            elif parsed.path == "/api/workflow-metrics":
                from .telemetry import workflow_metrics
                self.respond(workflow_metrics(store, params.get("project", [None])[0]))
            elif parsed.path == "/api/state":
                legacy = "sections" not in params
                requested = {part.strip() for value in params.get("sections", []) for part in value.split(",") if part.strip()}
                unknown = requested - {"core", "candidates", "analytics"}
                if unknown:
                    raise RequestError("Unknown state section; expected core, candidates or analytics")
                try:
                    global_config = runtime.config()
                except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
                    raise RequestError("Global configuration is invalid") from None
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
                        project["configuration_error"] = configuration_error(exc, raw_config)
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
                capacity = store.one("SELECT count(*) AS n FROM runs WHERE ended IS NULL")["n"] >= global_config["limits"]["concurrency"]
                opening = None
                for task in rows["tasks"]:
                    if task.get("wait_reason") == "ready" and capacity:
                        task["wait_reason"] = "capacity"
                    if task.get("wait_reason") in ["ready", "running"] and selected:
                        # 同一请求内项目相同，工作时段开头只需计算一次；
                        # 配置异常时跳过标注，控制台仍需返回以展示修复入口。
                        try:
                            if opening is None:
                                opening = runtime.next_effective_schedule(selected, time.time())
                        except (AttributeError, ValueError, TypeError, KeyError):
                            break
                        if opening > time.time() + 1:
                            task.update(wait_reason="working_hours", available=opening)
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
                            project["configuration_error"] = configuration_error(exc, raw_project_configs.get(project["id"]))
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
                    raise RequestError("Task not found")
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
                    raise RequestError("Run not found")
                run["result"] = json.loads(run["result"])
                run["config"] = json.loads(run["config"])
                run["stages"] = store.query("SELECT stage,created FROM stages WHERE run=? ORDER BY created", (run["id"],))
                run["phases"] = store.query("SELECT phase,started,ended FROM phase_times WHERE run=? ORDER BY id", (run["id"],))
                for stage in run["stages"]:
                    saved = store.one("SELECT data FROM stages WHERE run=? AND stage=?", (run["id"], stage["stage"]))
                    stage["decision"] = json.loads(saved["data"]).get("decision")
                self.respond(run)
            elif parsed.path == "/api/patch":
                from .storage_tools import run_artifact_path
                run = store.one("SELECT id,path FROM runs WHERE id=?", (params.get("id", [""])[0],))
                if not run:
                    raise RequestError("Run has no artifacts")
                try:
                    path = run_artifact_path(store, run, "candidate.patch")
                    if not path.is_file():
                        raise ValueError("Missing patch")
                    payload = path.read_bytes()
                except (ValueError, OSError):
                    raise RequestError("Run has no artifacts in this state directory") from None
                self.respond(payload, content_type="text/plain; charset=utf-8")
            elif parsed.path in ["/", "/index.html", "/app.js", "/experience.js", "/model-center.js", "/auth.js", "/desktop.js", "/usage.js", "/i18n.js", "/lang-bootstrap.js", "/style.css", "/model-center.css", "/favicon.svg"]:
                name = "index.html" if parsed.path == "/" else parsed.path[1:]
                path = Path(__file__).parent / "web" / name
                payload = self.server.assets[name]
                if name == "index.html":
                    payload = payload.replace(b'<html lang=', ('<html data-build="' + BUILD_ID + '" lang=').encode(), 1)
                self.respond(payload, content_type=(mimetypes.guess_type(str(path))[0] or "application/octet-stream") + "; charset=utf-8")
            else:
                self.respond({"error": "Not found"}, 404)
        except Exception as exc:
            self.api_error(exc)

    def do_POST(self):
        if not self.authenticated():
            return
        if self.headers.get("Origin") not in {self.server.origin, self.server.local_origin} or not self._token_matches(self.headers.get("X-RSI-CSRF", ""), self.server.csrf):
            self.server.security_failure("origin_or_csrf")
            self.respond({"error": "Invalid origin or CSRF token"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise RequestError("Invalid request size")
            if self.headers.get_content_type() != "application/json":
                raise RequestError("Expected application/json")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise RequestError("Expected an object")
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
                    self.api_error(exc)
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
                    started = runtime.updates.check_async()
                    result = {"started": started, "update": runtime.updates.snapshot()}
                elif path == "/api/updates/apply":
                    if body.get("confirm") is not True:
                        raise RequestError("Confirm installation and idle restart")
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
                    result = {"id": runtime.goal(body["project"], body["text"], body.get("overrides"), body.get("request_id"))}
                elif path == "/api/task/revise":
                    runtime.revise_task(body["task"], body["supplement"])
                    result = {"saved": True}
                elif path == "/api/task/schedule":
                    runtime.set_dependency(body["task"], body.get("dependency"), body.get("urgency", 0))
                    result = {"saved": True}
                elif path == "/api/backup":
                    from .storage_tools import backup
                    result = backup(runtime.store)
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
                elif path == "/api/session/rotate":
                    # 轮换后旧链接立即失效；响应携带新 cookie，当前浏览器保持登录。
                    with self.server._session_lock:
                        result = self.server.rotate_session()
                        self.respond(result, headers={"Set-Cookie": self.server.cookie_header(self.headers.get("Host"))})
                    return
                else:
                    self.respond({"error": "Not found"}, 404)
                    return
                self.respond(result)
        except Exception as exc:
            self.api_error(exc)
