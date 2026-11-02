"""独立 CLI 认证会话；授权材料只经专用 API 返回，不进入项目事件。"""
import base64
import copy
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

from .cli_worker import CODEX_AUTH_LOCK, clean_environment
from .opencode import (AUTH_LOCK, OpenCodeCompatibilityError, auth_path, auth_providers,
                       detect_opencode, export_auth, import_auth, install_legacy_credential,
                       load_auth_export, logout_provider, merge_auth_export, remove_auth_export,
                       _parse_auth_export)
from .process import docker_mount, run_process


ACTIVE = {"starting", "awaiting_authorization", "completing"}
TERMINAL = {"completed", "cancelled", "expired", "failed", "interrupted"}
MAX_BYTES = 1024 * 1024
# providers 发现结果要完整启动一个 worker（Docker 模式下是一个容器），缓存短 TTL 内的重复查询。
PROVIDERS_CACHE_TTL = 25
# 父服务退出或崩溃时控制管道关闭；整个认证进程组随即停止。
_SUPERVISOR = """import os,subprocess,sys,threading
fd=int(sys.argv[1])
container=sys.argv[2]
home=sys.argv[3]
cleanup='''import os,shutil,signal,subprocess,sys
if sys.argv[2]:
 try: subprocess.run(['docker','rm','--force',sys.argv[2]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
 except Exception: pass
try: os.killpg(int(sys.argv[1]),signal.SIGKILL)
except (ProcessLookupError,PermissionError): pass
shutil.rmtree(sys.argv[3],ignore_errors=True)
'''
def watch():
 try:
  while os.read(fd,1): pass
 finally:
  subprocess.Popen([sys.executable,'-c',cleanup,str(os.getpgrp()),container,home],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
threading.Thread(target=watch,daemon=True).start()
raise SystemExit(subprocess.call(sys.argv[4:],close_fds=True))
"""


class AuthError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Authentication: " + code)


AuthenticationError = AuthError


def _read_json(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            raise AuthError("invalid_credentials")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise AuthError("invalid_credentials")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise AuthError("invalid_credentials") from None


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".auth-rsi-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _codex_path():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", value):
        raise AuthError("invalid_provider")
    return value


def _url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise AuthError("invalid_response")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AuthError("invalid_response")
    return value


class _Process:
    def __init__(self, backend, config=None, cancel=None):
        self.backend = backend
        self.directory = tempfile.TemporaryDirectory(prefix="rsi-auth-")
        self.home = Path(self.directory.name)
        self.home.chmod(0o700)
        self.process = None
        self.control = None
        self.close_lock = threading.RLock()
        self.is_closed = False
        self.config = config or {}
        self.container = self.config.get("sandbox", {}).get("backend") == "docker" or not shutil.which(backend)
        self.container_name = "adaptive-rsi-auth-" + secrets.token_hex(12) if self.container else ""
        self.publish = False
        self.cancel = cancel or threading.Event()
        self.environment = {**clean_environment(), "HOME": str(self.home), "NO_COLOR": "1",
                            "XDG_CONFIG_HOME": str(self.home / ".config"),
                            "XDG_DATA_HOME": str(self.home / ".local/share"),
                            "XDG_CACHE_HOME": str(self.home / ".cache"),
                            "XDG_STATE_HOME": str(self.home / ".local/state"),
                            "CODEX_HOME": str(self.home / ".codex")}

    def binary(self):
        if not self.container:
            return shutil.which(self.backend)
        docker = shutil.which("docker")
        if not docker:
            raise AuthError(self.backend + "_missing")
        self.image = self.config.get("sandbox", {}).get("worker_image", "adaptive-rsi-worker:local")
        try:
            result = subprocess.run([docker, "image", "inspect", self.image], timeout=10,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise AuthError("auth_container_unavailable") from None
        if result.returncode:
            raise AuthError("auth_worker_image_missing")
        return self.backend

    def launch(self, argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL):
        if self.cancel.is_set():
            raise AuthError("service_closed")
        if self.container:
            limits = self.config.get("sandbox", {})
            docker = shutil.which("docker")
            command = [docker, "run", "--rm", "--name", self.container_name, "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--user", str(os.getuid()) + ":" + str(os.getgid()),
                       "--pids-limit=128", "--memory=" + str(limits.get("memory_mb", 2048)) + "m",
                       "--cpus=" + str(limits.get("cpus", 2)), "--tmpfs", "/tmp:rw,nosuid,size=256m",
                       "--mount", docker_mount(self.home, self.home),
                       "--workdir", str(self.home)]
            if stdin == subprocess.PIPE:
                command.append("-i")
            if self.publish:
                command += ["--publish", "127.0.0.1::4096"]
            for key in self.environment:
                if key != "PATH":
                    # Docker 从 Popen env 继承变量值；argv 仅保留名称，避免暴露认证密码。
                    command += ["--env", key]
            argv = command + [self.image, *argv]
        read_fd, self.control = os.pipe()
        try:
            self.process = subprocess.Popen([sys.executable, "-c", _SUPERVISOR, str(read_fd), self.container_name, str(self.home), *argv],
                                            cwd=self.home, env=self.environment, stdin=stdin, stdout=stdout,
                                            stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(read_fd,))
        except OSError:
            self.close()
            raise AuthError(self.backend + "_missing") from None
        finally:
            os.close(read_fd)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def close(self):
        with self.close_lock:
            if self.is_closed:
                return
            self.is_closed = True
            if self.container_name and self.process:
                try:
                    subprocess.run([shutil.which("docker") or "docker", "rm", "--force", self.container_name],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.control is not None:
                os.close(self.control)
                self.control = None
            if self.process:
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        self.process.kill()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # SIGKILL 后仍处 D 状态的进程留给系统收尸；关闭流程不能被无限挂起。
                    pass
                for stream in (self.process.stdin, self.process.stdout):
                    if stream:
                        stream.close()
            self.directory.cleanup()


class CodexAuth(_Process):
    def __init__(self, config=None, cancel=None):
        super().__init__("codex", config, cancel)
        self.messages = queue.Queue(maxsize=128)
        self.notifications = []
        self.request_id = 0
        try:
            binary = self.binary()
        except AuthError:
            self.close()
            raise
        codex_home = self.home / ".codex"
        codex_home.mkdir(mode=0o700)
        # 文件存储与执行器读取 auth.json 的约定一致，禁止加载用户配置。
        (codex_home / "config.toml").write_text('cli_auth_credentials_store = "file"\nmodel_provider = "openai"\n[analytics]\nenabled = false\n')
        self.environment["RUST_LOG"] = "off"
        try:
            self.launch([binary, "app-server", "--listen", "stdio://"], subprocess.PIPE, subprocess.PIPE)
            threading.Thread(target=self._read, daemon=True).start()
            self.request("initialize", {"clientInfo": {"name": "adaptive_rsi_auth", "version": "1"}})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    @property
    def credentials(self):
        return self.home / ".codex/auth.json"

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_BYTES + 1)
                if not line or len(line) > MAX_BYTES:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    break
                self.messages.put_nowait(message)
        except (OSError, ValueError, queue.Full):
            pass
        try:
            self.messages.put_nowait(None)
        except queue.Full:
            pass

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message).encode() + b"\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise AuthError("process_interrupted") from None

    def request(self, method, params, timeout=25):
        self.request_id += 1
        request_id = self.request_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            if self.cancel.is_set():
                raise AuthError("service_closed")
            try:
                message = self.messages.get(timeout=min(0.25, max(0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise AuthError("auth_timeout") from None
                continue
            if message is None:
                raise AuthError("process_interrupted")
            if message.get("id") == request_id:
                if "error" in message:
                    raise AuthError("device_auth_unavailable" if method == "account/login/start" else "auth_protocol_error")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AuthError("invalid_response")
                return result
            if message.get("method"):
                if len(self.notifications) >= 128:
                    raise AuthError("invalid_response")
                self.notifications.append(message)

    def start(self, provider=None, method=0, inputs=None):
        result = self.request("account/login/start", {"type": "chatgptDeviceCode"})
        self.login_id = result.get("loginId")
        if not isinstance(self.login_id, str) or not isinstance(result.get("userCode"), str):
            raise AuthError("invalid_response")
        return {"verification_url": _url(result.get("verificationUrl")), "user_code": result["userCode"][:256],
                "method": "auto", "instructions": ""}

    def wait(self, stop, deadline):
        while not stop.is_set() and time.time() < deadline:
            if self.notifications:
                message = self.notifications.pop(0)
            else:
                try:
                    message = self.messages.get(timeout=min(0.25, max(0, deadline - time.time())))
                except queue.Empty:
                    continue
            if message is None:
                raise AuthError("process_interrupted")
            if message.get("method") == "account/login/completed":
                params = message.get("params", {})
                if params.get("loginId") == self.login_id:
                    if params.get("success") is not True:
                        raise AuthError("authorization_failed")
                    result = self.request("account/read", {"refreshToken": False})
                    if not result.get("account"):
                        raise AuthError("authorization_failed")
                    return True
        return False


class OpenCodeAuth(_Process):
    def __init__(self, config=None, cancel=None):
        super().__init__("opencode", config, cancel)
        try:
            binary = self.binary()
            if self.container:
                image = self.config.get("sandbox", {}).get("worker_image", "adaptive-rsi-worker:local")
                self.capabilities = detect_opencode(
                    "opencode", argv=[shutil.which("docker"), "run", "--rm", image, "opencode", "--version"],
                    env=clean_environment())
            else:
                self.capabilities = detect_opencode(binary)
        except AuthError:
            self.close()
            raise
        except OpenCodeCompatibilityError as exc:
            self.close()
            raise AuthError(exc.code) from None
        except Exception:
            self.close()
            raise AuthError("opencode_version_unknown") from None
        password = secrets.token_urlsafe(32)
        self.header = "Basic " + base64.b64encode((self.capabilities.http_username + ":" + password).encode()).decode()
        self.environment.update(OPENCODE_SERVER_USERNAME=self.capabilities.http_username,
                                OPENCODE_SERVER_PASSWORD=password,
                                OPENCODE_DISABLE_PROJECT_CONFIG="true", OPENCODE_DISABLE_AUTOUPDATE="true",
                                OPENCODE_DISABLE_MODELS_FETCH="true", OPENCODE_PURE="true")
        _write_json(self.home / ".config/opencode/opencode.json", {
            "plugin": [], "mcp": {}, "instructions": [], "permission": {"*": "deny"},
            "autoupdate": False, "share": "disabled"})
        # 保留官方供应商模型缓存，既不导入用户配置，也不触发模型目录下载。
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "opencode/models.json"
        if cache.is_file() and cache.stat().st_size <= 20 * MAX_BYTES:
            target = self.home / ".cache/opencode/models.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache, target)
        port = self._port()
        self.base = "http://127.0.0.1:" + str(port)
        try:
            self.publish = self.container
            self._serve(binary, port)
        except Exception:
            self.close()
            raise

    @property
    def credentials(self):
        return self.home / ".local/share/opencode/auth.json"

    def _port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def _serve(self, binary, port):
        # 启动回环服务并等待就绪。端口可能在探测与实际绑定之间被其他进程抢占，
        # 此时服务进程会立即退出：未超时且进程已死时换一个新端口重新 launch 一次。
        deadline = time.monotonic() + 25
        self.launch(self.capabilities.serve_argv(
            binary, "0.0.0.0" if self.container else "127.0.0.1", 4096 if self.container else port))
        retried = False
        while True:
            if self.cancel.is_set():
                raise AuthError("service_closed")
            try:
                if self.container and self.base.endswith(":" + str(port)):
                    mapping = subprocess.run([shutil.which("docker"), "port", self.container_name, "4096/tcp"],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, timeout=3)
                    match = re.fullmatch(rb"127\.0\.0\.1:(\d+)\s*", mapping.stdout)
                    if not match:
                        raise AuthError("auth_container_unavailable")
                    self.base = "http://127.0.0.1:" + match[1].decode()
                self.http("GET", "/api/info" if self.capabilities.modern else "/global/health", timeout=1)
                return
            except AuthError:
                if not self.alive() and time.monotonic() < deadline and not retried:
                    retried = True
                    if self.control is not None:
                        try:
                            os.close(self.control)
                        except OSError:
                            pass
                        self.control = None
                    port = self._port()
                    self.base = "http://127.0.0.1:" + str(port)
                    self.launch(self.capabilities.serve_argv(
                        binary, "0.0.0.0" if self.container else "127.0.0.1", 4096 if self.container else port))
                    continue
                if not self.alive() or time.monotonic() >= deadline:
                    raise AuthError("auth_protocol_error") from None
                time.sleep(0.05)

    def http(self, method, path, body=None, timeout=25):
        request = urllib.request.Request(self.base + path,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": self.header, "Content-Type": "application/json"},
                                         method=method)
        try:
            # 禁用代理和重定向，内部密码只发送给本次回环服务。
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise AuthError("invalid_response")
            if not raw:
                return None
            return json.loads(raw)
        except (OSError, ValueError, urllib.error.URLError):
            raise AuthError("auth_protocol_error") from None

    def providers(self):
        if self.capabilities.modern:
            return self._v2_providers()
        value = self.http("GET", "/provider/auth")
        if not isinstance(value, dict):
            raise AuthError("invalid_response")
        result = []
        for provider, methods in value.items():
            _identifier(provider)
            if not isinstance(methods, list):
                continue
            items = []
            for index, method in enumerate(methods):
                if not isinstance(method, dict) or method.get("type") not in {"oauth", "api"}:
                    continue
                label = str(method.get("label", ""))[:160]
                # 只开放已审查的内置设备授权；普通 browser 方法会启动额外回调端口。
                supported = method["type"] == "api" or (
                    (provider == "openai" and "headless" in label.lower()) or
                    provider in {"github-copilot", "xai"})
                prompts = []
                for prompt in method.get("prompts", []):
                    if not isinstance(prompt, dict) or prompt.get("type") not in {"text", "select"}:
                        supported = False
                        continue
                    prompts.append({key: value for key, value in prompt.items()
                                    if key in {"type", "key", "message", "placeholder", "options", "when"}})
                items.append({"index": index, "label": label, "type": method["type"], "supported": supported,
                              "prompts": prompts,
                              "unsupported_reason": "" if supported else "unsupported_auth_method"})
            if items:
                result.append({"id": provider, "name": provider, "methods": items})
        return result

    def start(self, provider, method, inputs=None):
        if self.capabilities.modern:
            return self._v2_start(provider, method, inputs)
        self.provider, self.method = provider, method
        rows = next((row["methods"] for row in self.providers() if row["id"] == provider), [])
        selected = next((item for item in rows if item["index"] == method), None)
        if not selected or not selected["supported"] or selected["type"] != "oauth":
            raise AuthError("unsupported_auth_method")
        inputs = inputs or {}
        prompts = selected.get("prompts", [])
        if set(inputs) - {prompt.get("key") for prompt in prompts}:
            raise AuthError("invalid_auth_inputs")
        for prompt in prompts:
            condition = prompt.get("when")
            if condition:
                matched = inputs.get(condition.get("key")) == condition.get("value")
                if matched != (condition.get("op") == "eq"):
                    continue
            value = inputs.get(prompt.get("key"), "")
            if not value or (prompt["type"] == "select" and value not in {
                option.get("value") for option in prompt.get("options", [])}):
                raise AuthError("invalid_auth_inputs")
        result = self.http("POST", "/provider/" + quote(provider, safe="") + "/oauth/authorize",
                           {"method": method, "inputs": inputs})
        if not isinstance(result, dict) or result.get("method") not in {"auto", "code"}:
            raise AuthError("invalid_response")
        return {"verification_url": _url(result.get("url")), "method": result["method"],
                "instructions": str(result.get("instructions", ""))[:2048], "user_code": ""}

    def complete(self, code="", timeout=600):
        if self.capabilities.modern:
            return self._v2_complete(code, timeout)
        result = self.http("POST", "/provider/" + quote(self.provider, safe="") + "/oauth/callback",
                           {"method": self.method, **({"code": code} if code else {})}, timeout=timeout)
        if result is not True:
            raise AuthError("authorization_failed")
        return True

    def _v2_integrations(self):
        value = self.http("GET", "/api/integration")
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise AuthError("invalid_response")
        return value["data"]

    @staticmethod
    def _v2_prompt(field):
        if not isinstance(field, dict) or not isinstance(field.get("key"), str) or field.get("hidden"):
            return None
        options = []
        for option in field.get("options", []):
            if isinstance(option, dict) and isinstance(option.get("value"), str):
                options.append({"value": option["value"],
                                "label": str(option.get("label", option["value"]))[:160]})
        when = field.get("when")
        if isinstance(when, list):
            when = when[0] if when and isinstance(when[0], dict) else None
        prompt = {"type": "select" if options else "text", "key": field["key"],
                  "message": str(field.get("title", field["key"]))[:160]}
        if isinstance(field.get("placeholder"), str):
            prompt["placeholder"] = field["placeholder"][:160]
        if options:
            prompt["options"] = options
        if isinstance(when, dict) and isinstance(when.get("key"), str) and when.get("op") in {"eq", "neq"}:
            prompt["when"] = {key: when.get(key) for key in ("key", "op", "value")}
        return prompt

    def _v2_providers(self):
        result, methods_by_index = [], {}
        host_connected = set()
        if self.container:
            host_connected = {item["integrationID"] for item in load_auth_export()}
        binary = shutil.which("opencode")
        if binary and not self.container:
            try:
                host_connected = set(auth_providers(binary, self.capabilities))
            except (RuntimeError, OSError):
                host_connected = set()
        for integration in self._v2_integrations():
            if not isinstance(integration, dict):
                continue
            provider = integration.get("id")
            if not isinstance(provider, str):
                continue
            try:
                _identifier(provider)
            except AuthError:
                continue
            connections = integration.get("connections", [])
            connected = provider in host_connected or (
                isinstance(connections, list) and any(
                    isinstance(connection, dict) and connection.get("type") == "credential" for connection in connections))
            items = []
            for index, method in enumerate(integration.get("methods", [])):
                if not isinstance(method, dict) or method.get("type") not in {"key", "oauth"}:
                    continue
                form = method.get("form") if isinstance(method.get("form"), list) else []
                prompts = [prompt for prompt in (self._v2_prompt(field) for field in form) if prompt]
                defaults = {field["key"]: field["default"] for field in form
                            if isinstance(field, dict) and field.get("hidden") and
                            isinstance(field.get("key"), str) and isinstance(field.get("default"), str)}
                label = str(method.get("label") or ("API key" if method["type"] == "key" else "OAuth"))[:160]
                method_id = str(method.get("id", "")).lower()
                device_oauth = method["type"] == "oauth" and (
                    "device" in method_id or "headless" in method_id or
                    "device" in label.lower() or "headless" in label.lower())
                supported = (method["type"] == "oauth" and device_oauth) or (method["type"] == "key" and not prompts)
                item = {"index": index, "label": label,
                        "type": "api" if method["type"] == "key" else "oauth", "supported": supported,
                        "prompts": prompts, "defaults": defaults,
                        "unsupported_reason": "" if supported else "unsupported_auth_method"}
                if method["type"] == "oauth" and isinstance(method.get("id"), str):
                    item["method_id"] = method["id"]
                items.append(item)
                methods_by_index[(provider, index)] = method
            if items:
                result.append({"id": provider, "name": str(integration.get("name", provider))[:160],
                               "methods": items, "connected": connected})
        self._v2_methods = methods_by_index
        result.sort(key=lambda row: (not row["connected"], row["name"].casefold()))
        return result

    def _v2_start(self, provider, method, inputs):
        rows = next((row["methods"] for row in self.providers() if row["id"] == provider), [])
        selected = next((item for item in rows if item["index"] == method), None)
        if not selected or not selected["supported"] or selected["type"] != "oauth":
            raise AuthError("unsupported_auth_method")
        method_data = self._v2_methods.get((provider, method), {})
        method_id = method_data.get("id")
        if not isinstance(method_id, str):
            raise AuthError("invalid_response")
        inputs = {**selected.get("defaults", {}), **(inputs or {})}
        prompts = selected.get("prompts", [])
        allowed = {prompt.get("key") for prompt in prompts} | set(selected.get("defaults", {}))
        if set(inputs) - allowed:
            raise AuthError("invalid_auth_inputs")
        for prompt in prompts:
            condition = prompt.get("when")
            if condition:
                matched = inputs.get(condition.get("key")) == condition.get("value")
                if matched != (condition.get("op") == "eq"):
                    continue
            value = inputs.get(prompt.get("key"), "")
            if not value or (prompt["type"] == "select" and value not in {
                    option.get("value") for option in prompt.get("options", [])}):
                raise AuthError("invalid_auth_inputs")
        result = self.http("POST", "/api/integration/" + quote(provider, safe="") + "/connect/oauth",
                           {"methodID": method_id, "answer": inputs or None, "label": None})
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise AuthError("invalid_response")
        data = result["data"]
        if not isinstance(data.get("attemptID"), str) or data.get("mode") not in {"auto", "code"}:
            raise AuthError("invalid_response")
        self.provider, self.method, self.method_id = provider, method, method_id
        self.attempt_id = data["attemptID"]
        return {"verification_url": _url(data.get("url")), "method": data["mode"],
                "instructions": str(data.get("instructions", ""))[:2048], "user_code": ""}

    def _v2_complete(self, code="", timeout=600):
        base = "/api/integration/" + quote(self.provider, safe="") + "/connect/oauth/" + quote(self.attempt_id, safe="")
        if code:
            result = self.http("POST", base + "/complete", {"code": code}, timeout=timeout)
            if result is not None:
                raise AuthError("authorization_failed")
            return True
        deadline = time.monotonic() + max(1, timeout)
        interval = 0.5
        while time.monotonic() < deadline:
            value = self.http("GET", base, timeout=min(25, max(1, deadline - time.monotonic())))
            data = value.get("data") if isinstance(value, dict) else None
            status = data.get("status") if isinstance(data, dict) else None
            if status == "complete":
                return True
            if status in {"failed", "expired"}:
                raise AuthError("authorization_failed" if status == "failed" else "auth_expired")
            if status != "pending":
                raise AuthError("invalid_response")
            # 阶梯退避轮询：0.5s 起步逐步加倍、3s 封顶，截止时间与状态处理不变。
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            interval = min(3.0, interval * 2)
        raise AuthError("auth_timeout")

    def set_api_key(self, provider, key):
        result = self.http("POST", "/api/integration/" + quote(provider, safe="") + "/connect/key",
                           {"key": key, "answer": None, "label": None})
        if result is not None:
            raise AuthError("authorization_failed")
        return True

    def _worker_auth(self, argv, timeout=20):
        if not self.container:
            return None
        docker = shutil.which("docker")
        if not docker:
            raise AuthError("auth_container_unavailable")
        result = run_process([docker, "exec", self.container_name, "opencode"] + list(argv),
                             timeout=timeout, output_mb=self.config.get("limits", {}).get("max_output_mb", 12))
        if result.get("exit_code") or result.get("error"):
            raise AuthError("auth_protocol_error")
        return result.get("stdout", "")

    def export_credential(self, provider):
        try:
            if self.container:
                text = self._worker_auth(["auth", "export", "--standalone", provider])
                credentials = _parse_auth_export(text)
            else:
                binary = shutil.which("opencode")
                if not binary:
                    raise AuthError("auth_protocol_error")
                credentials = export_auth(binary, self.capabilities, {
                    **clean_environment(),
                    "HOME": str(self.home),
                    "NO_COLOR": "1",
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"),
                    "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                    "OPENCODE_DISABLE_AUTOUPDATE": "true",
                    "OPENCODE_DISABLE_MODELS_FETCH": "true",
                    "OPENCODE_PURE": "true",
                }, provider)
        except RuntimeError:
            raise AuthError("auth_protocol_error") from None
        return [item for item in credentials if item.get("integrationID") == provider]

    def install_credential(self, provider, credentials):
        if not credentials:
            raise AuthError("invalid_credentials")
        binary = shutil.which("opencode")
        host_capabilities = None
        if binary:
            try:
                host_capabilities = detect_opencode(binary)
            except OpenCodeCompatibilityError as exc:
                raise AuthError(exc.code) from None
        if not host_capabilities or not host_capabilities.modern:
            if host_capabilities:
                if not install_legacy_credential(provider, credentials):
                    raise AuthError("invalid_credentials")
            else:
                merge_auth_export(credentials)
            return
        try:
            import_auth(binary, {
                **clean_environment(),
                "HOME": os.environ.get("HOME", str(Path.home())),
                "NO_COLOR": "1",
                **{key: os.environ[key] for key in ["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]
                   if os.environ.get(key)},
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_MODELS_FETCH": "true",
                "OPENCODE_PURE": "true",
            }, credentials, self.home)
        except RuntimeError:
            raise AuthError("auth_protocol_error") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AuthSessions:
    def __init__(self, root, factory=None, ttl=600, config=None, now=None):
        self.root = Path(root)
        self.file = self.root / "auth-sessions.json"
        self.lock = threading.RLock()
        self.sessions = {}
        self.workers = {}
        self.discoveries = set()
        self.threads = set()
        self.closed = False
        self.shutdown = threading.Event()
        self.ttl = ttl
        self.config = config
        self.now = now or time.time
        self.custom_factory = factory is not None
        # backend -> (expires_at, rows)；测试注入 factory 时不缓存，便于断言每次真实发现。
        self.providers_cache = {}
        # 状态快照的子进程发现结果同样短 TTL 缓存，登录/登出/密钥变更时失效。
        self.status_cache = {}
        self.cache_generations = {backend: 0 for backend in ("codex", "opencode")}
        self.factory = factory or (lambda backend, cancel=None: (CodexAuth if backend == "codex" else OpenCodeAuth)(
            self.config() if callable(self.config) else self.config, cancel=cancel or self.shutdown))
        try:
            previous = _read_json(self.file)
        except AuthError:
            previous = {}
        for session_id, item in previous.items():
            if (isinstance(item, dict) and isinstance(session_id, str) and item.get("id") == session_id
                    and item.get("backend") in {"codex", "opencode"} and item.get("state") in ACTIVE | TERMINAL
                    and isinstance(item.get("expires_at"), (float, int))):
                item = {key: value for key, value in item.items() if key in {"id", "backend", "provider", "state", "expires_at", "error"}}
                if item.get("state") in ACTIVE:
                    item.update(state="interrupted", error="process_interrupted")
                self.sessions[session_id] = item
        if previous:
            self._persist()

    def _persist(self):
        recent = set(list(self.sessions)[-30:])
        self.sessions = {key: item for key, item in self.sessions.items() if key in recent or item["state"] in ACTIVE}
        rows = list(self.sessions.items())
        _write_json(self.file, {key: {field: value for field, value in item.items()
                                     if field in {"id", "backend", "provider", "state", "expires_at", "error"}}
                                for key, item in rows})

    @staticmethod
    def _public(item):
        """对外快照只含稳定字段；以 `_` 开头的内部标记不离开进程。"""
        return copy.deepcopy({key: value for key, value in item.items() if not key.startswith("_")})

    def _invalidate_caches(self, backend):
        # 调用方持有 self.lock；阻止失效前启动的发现任务重新发布旧结果。
        self.cache_generations[backend] += 1
        self.providers_cache.pop(backend, None)
        self.status_cache.pop(backend, None)

    def _backend(self, backend):
        if not isinstance(backend, str) or backend not in {"codex", "opencode"}:
            raise AuthError("invalid_backend")
        return backend

    @property
    def busy(self):
        with self.lock:
            ids = [key for key, item in self.sessions.items() if item["state"] in ACTIVE]
        return sum(self.status(key)["state"] in ACTIVE for key in ids)

    def snapshot(self):
        self.busy
        result = {}
        config = self.config() if callable(self.config) else self.config or {}
        container = config.get("sandbox", {}).get("backend") == "docker"
        for backend in ("codex", "opencode"):
            available = bool(shutil.which("docker") if container or not shutil.which(backend) else shutil.which(backend))
            try:
                # 凭据写入采用原子替换；状态读取不等待可能持续整个模型调用的 CLI 锁。
                if backend == "codex":
                    data = _read_json(_codex_path())
                    authenticated = bool(data.get("tokens") or data.get("OPENAI_API_KEY"))
                    result[backend] = {"state": "authenticated" if authenticated else "signed_out",
                                       "available": available, "transport": "docker" if container or not shutil.which(backend) else "host"}
                else:
                    if self.custom_factory:
                        data = _read_json(auth_path())
                        providers = sorted(key for key, value in data.items()
                                           if isinstance(value, dict) and value.get("type") in {"oauth", "api"})
                        capabilities = None
                    else:
                        with self.lock:
                            generation = self.cache_generations[backend]
                            cached = self.status_cache.get(backend)
                        if cached and cached[0] > time.monotonic():
                            capabilities, providers = cached[1]
                        else:
                            binary = shutil.which("opencode")
                            if binary:
                                capabilities = detect_opencode(binary)
                                providers = auth_providers(binary, capabilities)
                            else:
                                # Linux/Docker deployments can keep v1 credentials on the host
                                # even when only the worker image contains the CLI.
                                capabilities = None
                                docker = shutil.which("docker")
                                if docker:
                                    try:
                                        image = config.get("sandbox", {}).get("worker_image", "adaptive-rsi-worker:local")
                                        inspected = run_process([docker, "image", "inspect", image], timeout=10)
                                        if inspected["exit_code"] or inspected["error"]:
                                            capabilities = None
                                        else:
                                            capabilities = detect_opencode(
                                                "opencode", argv=[docker, "run", "--rm", image, "opencode", "--version"],
                                                env=clean_environment())
                                    except (OpenCodeCompatibilityError, OSError):
                                        capabilities = None
                            providers = auth_providers(None, capabilities)
                            with self.lock:
                                if not self.closed and generation == self.cache_generations[backend]:
                                    self.status_cache[backend] = (time.monotonic() + 25, (capabilities, providers))
                    result[backend] = {"state": "authenticated" if providers else "signed_out",
                                       "available": available,
                                       "transport": "docker" if container or not shutil.which(backend) else "host",
                                       "authenticated_providers": providers}
                    if capabilities:
                        result[backend]["version"] = capabilities.version
                        result[backend]["compatibility"] = "supported"
            except OpenCodeCompatibilityError as exc:
                result[backend] = {"state": "signed_out", "available": available,
                                   "transport": "docker" if container or not shutil.which(backend) else "host",
                                   "error": exc.code, "authenticated_providers": []}
                if exc.version:
                    result[backend]["version"] = exc.version
            except AuthError:
                result[backend] = {"state": "signed_out", "available": available, "error": "invalid_credentials"}
            except RuntimeError as exc:
                if backend == "opencode" and str(exc).startswith("OpenCode credential listing"):
                    result[backend] = {"state": "signed_out", "available": available,
                                       "transport": "docker" if container or not shutil.which(backend) else "host",
                                       "error": "opencode_error", "authenticated_providers": []}
                else:
                    raise
            except (OSError, ValueError):
                result[backend] = {"state": "signed_out", "available": available, "error": "invalid_credentials"}
        with self.lock:
            result["sessions"] = [{key: item.get(key) for key in ("id", "backend", "provider", "state", "expires_at", "error")}
                                  for item in list(self.sessions.values())[-30:]]
        return result

    def providers(self, backend="opencode"):
        self._backend(backend)
        with self.lock:
            if self.closed:
                raise AuthError("service_closed")
            generation = self.cache_generations[backend]
            if not self.custom_factory:
                cached = self.providers_cache.get(backend)
                if cached and cached[0] > time.monotonic():
                    return copy.deepcopy(cached[1])
        if backend != "opencode":
            return []
        worker = self.factory(backend)
        try:
            with self.lock:
                if self.closed:
                    raise AuthError("service_closed")
                self.discoveries.add(worker)
            rows = worker.providers()
        finally:
            with self.lock:
                self.discoveries.discard(worker)
            worker.close()
        # worker 已关闭，rows 只是纯数据，可安全缓存供短 TTL 内复用。
        with self.lock:
            if not self.closed and not self.custom_factory and generation == self.cache_generations[backend]:
                self.providers_cache[backend] = (time.monotonic() + PROVIDERS_CACHE_TTL, copy.deepcopy(rows))
        return rows

    def _thread(self, target):
        def run():
            try:
                target()
            finally:
                with self.lock:
                    self.threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        self.threads.add(thread)
        thread.start()

    def start(self, backend, provider=None, method=0, inputs=None):
        """reserve + launch 的便捷组合；更新准入场景在临界区内只调用 reserve。"""
        session = self.reserve(backend, provider, method, inputs)
        return self.launch(session["id"], method, inputs)

    def reserve(self, backend, provider=None, method=0, inputs=None):
        """快速登记会话：返回时 state 已是 starting，busy 立即计入该会话。

        调用方（服务端更新准入检查）在 dispatch_lock 临界区内调用本方法，
        因此这里只做校验、登记与缓存失效，不启动 worker 线程。
        """
        self._backend(backend)
        if backend == "opencode":
            _identifier(provider)
        elif provider not in (None, "openai"):
            raise AuthError("invalid_provider")
        if not isinstance(method, int) or isinstance(method, bool) or method < 0 or method > 100:
            raise AuthError("invalid_auth_method")
        if inputs is not None and (not isinstance(inputs, dict) or len(inputs) > 30 or any(
            not isinstance(key, str) or not isinstance(value, str) or len(key) > 100 or len(value) > 2048
            for key, value in inputs.items())):
            raise AuthError("invalid_auth_inputs")
        self.busy
        with self.lock:
            if self.closed:
                raise AuthError("service_closed")
            if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                raise AuthError("auth_busy")
            session_id = secrets.token_urlsafe(24)
            self.sessions[session_id] = {"id": session_id, "backend": backend, "provider": provider,
                                         "state": "starting", "expires_at": self.now() + self.ttl, "error": "",
                                         "_reserved": True}
            self._persist()
            # 认证状态即将变化，缓存的供应商发现结果不再可靠。
            self._invalidate_caches(backend)
        return self._public(self.sessions[session_id])

    def launch(self, session_id, method=0, inputs=None):
        """在锁外启动会话 worker；慢操作（握手、凭据写入、complete）不回锁。

        reserve 与 launch 之间隔着服务端的 dispatch_lock 与调度延迟；若这段时间
        越过 expires_at，这里看到的唯一变化是 starting→expired。这是正常终态，
        返回该会话状态即可，不能对刚登记、worker 从未启动的会话抛
        invalid_auth_state。只 reserve 未 launch 的会话仍由 status()/busy 的
        过期扫描兜底回收（更新准入可能在登记后拦下 launch）。
        """
        if not isinstance(method, int) or isinstance(method, bool) or method < 0 or method > 100:
            raise AuthError("invalid_auth_method")
        self.status(session_id)
        with self.lock:
            item = self.sessions[session_id]
            # _reserved 在登记时写入、launch 时一次性消费：既标识「worker 尚未派发」，
            # 也保证同一会话不会被 launch 两次而并行起两个 worker。
            if not item.pop("_reserved", False):
                raise AuthError("invalid_auth_state")
            if item["state"] in ACTIVE and self.now() >= item["expires_at"]:
                self._update(session_id, state="expired", error="auth_expired")
            if item["state"] == "expired":
                return self._public(item)
            if item["state"] != "starting":
                raise AuthError("invalid_auth_state")
        self._thread(lambda: self._run(session_id, method, inputs))
        return self.status(session_id)

    def _update(self, session_id, **values):
        with self.lock:
            item = self.sessions[session_id]
            if item["state"] not in ACTIVE:
                return False
            item.update(values)
            if item["state"] in TERMINAL:
                for field in ("verification_url", "user_code", "instructions", "_code"):
                    item.pop(field, None)
            self._persist()
            return True

    def _run(self, session_id, method, inputs):
        worker = None
        try:
            item = self.status(session_id)
            stop = threading.Event()
            with self.lock:
                if self.sessions[session_id]["state"] not in ACTIVE:
                    return
                self.workers[session_id] = (None, stop)
            worker = self.factory(item["backend"]) if self.custom_factory else self.factory(item["backend"], stop)
            with self.lock:
                if self.sessions[session_id]["state"] not in ACTIVE:
                    return
                self.workers[session_id] = (worker, stop)
            fields = worker.start(item["provider"], method, inputs)
            if not self._update(session_id, state="awaiting_authorization", **fields):
                return
            if item["backend"] == "codex":
                success = worker.wait(stop, item["expires_at"])
            elif fields["method"] == "auto":
                success = worker.complete(timeout=max(1, item["expires_at"] - self.now()))
            else:
                while not stop.wait(0.1) and self.now() < item["expires_at"]:
                    with self.lock:
                        code = self.sessions[session_id].pop("_code", None)
                    if code is not None:
                        success = worker.complete(code, timeout=max(1, item["expires_at"] - self.now()))
                        break
                else:
                    success = False
            with self.lock:
                current = self.sessions[session_id]
                if current["state"] not in ACTIVE:
                    return
                if not success or self.now() >= item["expires_at"]:
                    self._update(session_id, state="expired", error="auth_expired")
                    return
                self._update(session_id, state="completing")
            self._save(worker, item["backend"], item["provider"], session_id, stop)
        except AuthError as exc:
            expired = self.now() >= self.sessions[session_id]["expires_at"]
            self._update(session_id, state="expired" if expired else "failed", error="auth_expired" if expired else exc.code)
        except Exception:
            self._update(session_id, state="failed", error="auth_protocol_error")
        finally:
            with self.lock:
                self.workers.pop(session_id, None)
            if worker:
                worker.close()

    def _save(self, worker, backend, provider, session_id, stop):
        if backend == "codex":
            data = _read_json(worker.credentials)
            if not isinstance(data.get("tokens"), dict) or not data["tokens"].get("refresh_token"):
                raise AuthError("invalid_credentials")
        else:
            modern = bool(getattr(getattr(worker, "capabilities", None), "modern", False))
            if modern:
                value = worker.export_credential(provider)
                if not isinstance(value, list) or not value:
                    raise AuthError("invalid_credentials")
            else:
                data = _read_json(worker.credentials)
                value = data.get(provider)
                if not isinstance(value, dict) or value.get("type") not in {"oauth", "api"}:
                    raise AuthError("invalid_credentials")
        credential_lock = CODEX_AUTH_LOCK if backend == "codex" else AUTH_LOCK
        # 执行器可能长时间持锁；等待期间仍允许取消、过期和关闭服务。
        while not credential_lock.acquire(timeout=0.05):
            if stop.is_set() or self.status(session_id)["state"] not in ACTIVE:
                return
        try:
            with self.lock:
                current_session = self.sessions[session_id]
                if self.closed or stop.is_set() or current_session["state"] not in ACTIVE:
                    return
                if self.now() >= current_session["expires_at"]:
                    self._update(session_id, state="expired", error="auth_expired")
                    return
                if backend == "codex":
                    _write_json(_codex_path(), data)
                elif modern:
                    worker.install_credential(provider, value)
                else:
                    current = _read_json(auth_path())
                    current[provider] = value
                    _write_json(auth_path(), current)
                # 授权期间的状态/供应商查询可能重新填充旧缓存，保存凭据后再次失效。
                self._invalidate_caches(backend)
                self._update(session_id, state="completed")
        finally:
            credential_lock.release()

    def status(self, session_id):
        expired_worker = None
        with self.lock:
            if not isinstance(session_id, str) or session_id not in self.sessions:
                raise AuthError("unknown_session")
            item = self.sessions[session_id]
            if item["state"] in ACTIVE and self.now() >= item["expires_at"]:
                self._update(session_id, state="expired", error="auth_expired")
                # 锁内只做状态判定并摘除 workers 引用；close() 含 docker rm、killpg、
                # wait 及目录清理等慢操作，放到锁外执行。close() 自身幂等，
                # 与 cancel()/_run()/close() 的并发路径一致，不会双重关闭。
                expired_worker = self.workers.pop(session_id, None)
                if expired_worker:
                    expired_worker[1].set()
            result = self._public(item)
        if expired_worker and expired_worker[0]:
            expired_worker[0].close()
        return result

    def complete(self, session_id, code=""):
        if not isinstance(code, str) or len(code) > 8192:
            raise AuthError("invalid_auth_code")
        self.status(session_id)
        with self.lock:
            item = self.sessions[session_id]
            if item["state"] != "awaiting_authorization" or item.get("method") != "code":
                raise AuthError("invalid_auth_state")
            if not code.strip():
                raise AuthError("invalid_auth_code")
            self.sessions[session_id]["_code"] = code.strip()
            self._update(session_id, state="completing")
        return self.status(session_id)

    def cancel(self, session_id):
        self.status(session_id)
        with self.lock:
            self._update(session_id, state="cancelled")
            worker = self.workers.get(session_id)
        if worker:
            worker[1].set()
            if worker[0]:
                worker[0].close()
        return self.status(session_id)

    def logout(self, backend, provider=None):
        self._backend(backend)
        if backend == "opencode":
            _identifier(provider)
        credential_lock = CODEX_AUTH_LOCK if backend == "codex" else AUTH_LOCK
        if not credential_lock.acquire(blocking=False):
            raise AuthError("auth_credentials_busy")
        workers = []
        try:
            with self.lock:
                if self.closed:
                    raise AuthError("service_closed")
                for session_id, item in self.sessions.items():
                    if item["backend"] == backend and item["state"] in ACTIVE:
                        self._update(session_id, state="cancelled")
                        if session_id in self.workers:
                            workers.append(self.workers[session_id])
                if backend == "codex":
                    path = _codex_path()
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    binary = shutil.which("opencode")
                    removed_export = remove_auth_export(provider)
                    try:
                        modern = bool(not self.custom_factory and binary and logout_provider(provider, binary))
                    except (OpenCodeCompatibilityError, RuntimeError):
                        raise AuthError("auth_protocol_error") from None
                    if not modern or removed_export:
                        path = auth_path()
                        data = _read_json(path)
                        if provider in data:
                            data.pop(provider, None)
                            _write_json(path, data)
        finally:
            credential_lock.release()
            for worker, stop in workers:
                stop.set()
                if worker:
                    worker.close()
        with self.lock:
            self._invalidate_caches(backend)
        return self.snapshot()

    def set_api_key(self, backend, provider, key):
        if self._backend(backend) != "opencode":
            raise AuthError("unsupported_auth_method")
        _identifier(provider)
        if not isinstance(key, str) or not key.strip() or len(key) > 8192:
            raise AuthError("invalid_api_key")
        if not any(row["id"] == provider and any(item["type"] == "api" and item.get("supported", True)
                                                 for item in row["methods"])
                   for row in self.providers(backend)):
            raise AuthError("unsupported_auth_method")
        if not AUTH_LOCK.acquire(blocking=False):
            raise AuthError("auth_credentials_busy")
        worker = None
        try:
            with self.lock:
                if self.closed:
                    raise AuthError("service_closed")
                if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                    raise AuthError("auth_busy")
            worker = self.factory(backend)
            modern = bool(getattr(getattr(worker, "capabilities", None), "modern", False))
            if modern and hasattr(worker, "set_api_key") and hasattr(worker, "export_credential"):
                worker.set_api_key(provider, key.strip())
                credentials = worker.export_credential(provider)
                if not credentials:
                    raise AuthError("invalid_credentials")
                worker.install_credential(provider, credentials)
            else:
                data = _read_json(auth_path())
                data[provider] = {"type": "api", "key": key.strip()}
                _write_json(auth_path(), data)
        finally:
            if worker:
                worker.close()
            AUTH_LOCK.release()
        with self.lock:
            self._invalidate_caches(backend)
        return self.snapshot()

    def close(self):
        with self.lock:
            self.closed = True
            self.shutdown.set()
            self.providers_cache.clear()
            self.status_cache.clear()
            workers = list(self.workers.values())
            discoveries = list(self.discoveries)
            for key, item in self.sessions.items():
                if item["state"] in ACTIVE:
                    self._update(key, state="interrupted", error="process_interrupted")
            threads = list(self.threads)
        for worker, stop in workers:
            stop.set()
            if worker:
                worker.close()
        for worker in discoveries:
            worker.close()
        for thread in threads:
            thread.join(timeout=2)
