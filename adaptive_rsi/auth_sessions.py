"""独立 CLI 认证会话；授权材料只经专用 API 返回，不进入项目事件。"""
import base64
import copy
import json
import logging
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
# 容器就绪探测（docker image inspect）同为子进程级成本；授权面板 1.5s 轮询期间不能每次重跑。
CONTAINER_CACHE_TTL = 25
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
    def __init__(self, code, detail=None):
        self.code = code
        # 仅用于内部判断与排障，不进入异常文本或 HTTP 响应。
        self.detail = detail
        super().__init__("Authentication: " + code)


class AuthAttemptFailed(AuthError):
    """现代 CLI 确认本次 attempt 已失败，但不能推断具体失败原因。"""

    def __init__(self, detail=None):
        super().__init__("authorization_failed", detail)


def _http_error_detail(status, raw):
    """提取 HTTP 错误体中的诊断字段；只保留有界字符串。"""
    detail = {"http_status": status}
    try:
        value = json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        value = None
    if not isinstance(value, dict):
        return detail
    for key in ("error", "message"):
        item = value.get(key)
        if isinstance(item, str) and item:
            detail[key] = item[:512]
        elif isinstance(item, dict) and isinstance(item.get("message"), str) and item["message"]:
            detail.setdefault("message", item["message"][:512])
    data = value.get("data")
    if isinstance(data, dict):
        for key in ("error", "message"):
            item = data.get(key)
            if isinstance(item, str) and item and key not in detail:
                detail[key] = item[:512]
        attempt = data.get("status")
        if isinstance(attempt, str) and attempt:
            detail["attempt_status"] = attempt[:64]
    attempt = value.get("status")
    if isinstance(attempt, str) and attempt and "attempt_status" not in detail:
        detail["attempt_status"] = attempt[:64]
    return detail


AuthenticationError = AuthError
_ACCOUNT_TEXT_LIMIT = 160


def _account_entries(account):
    """把 account/read 的身份载荷收敛成可展示的 label/plan，其余字段一律丢弃。"""
    if not isinstance(account, dict):
        return []
    label = account.get("email")
    if not isinstance(label, str) or not label.strip():
        label = account.get("type")
    plan = account.get("planType")
    if not isinstance(plan, str) or not plan.strip():
        plan = account.get("plan")
    label = label.strip()[:_ACCOUNT_TEXT_LIMIT] if isinstance(label, str) else ""
    plan = plan.strip()[:_ACCOUNT_TEXT_LIMIT] if isinstance(plan, str) else ""
    return [{"label": label, "plan": plan}] if label or plan else []


def _account_rows(value):
    """只接受已经收敛的展示行，防止状态文件或旧会话带出额外字段。"""
    rows = []
    if not isinstance(value, list):
        return rows
    for row in value:
        if len(rows) == 2:
            break
        if not isinstance(row, dict):
            continue
        label = row.get("label") if isinstance(row.get("label"), str) else ""
        plan = row.get("plan") if isinstance(row.get("plan"), str) else ""
        label, plan = label.strip()[:_ACCOUNT_TEXT_LIMIT], plan.strip()[:_ACCOUNT_TEXT_LIMIT]
        if label or plan:
            rows.append({"label": label, "plan": plan})
    return rows


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
    except (OSError, ValueError, RecursionError):
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
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _quarantine_invalid(path):
    """把无法解析的旧 auth.json 留档，返回备份路径；原文件不存在时返回 None。"""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(path.name + ".invalid-" + stamp)
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = path.with_name(path.name + ".invalid-" + stamp + "-" + str(suffix))
    try:
        os.replace(path, backup)
    except FileNotFoundError:
        return None
    except OSError:
        # 留档失败时保持原有失败语义，绝不覆盖可能仍可人工恢复的原始字节。
        raise AuthError("invalid_credentials") from None
    try:
        backup.chmod(0o600)
    except OSError:
        pass
    return backup


def _read_json_for_write(path):
    """写路径读取：损坏的旧文件先留档再视为空，用户仍可覆盖或退出登录。"""
    try:
        return _read_json(path)
    except AuthError:
        _quarantine_invalid(path)
        return {}


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
        # close() 中吞掉的清理异常留作诊断；单项失败不能阻断其余 fd/目录回收。
        self.close_errors = []
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
                try:
                    os.close(self.control)
                except OSError as exc:
                    # fd 可能已被继承方或系统关闭（EBADF）；不能阻断后续进程/目录回收。
                    self.close_errors.append(exc)
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
                        try:
                            stream.close()
                        except (OSError, ValueError) as exc:
                            # 子进程已退出且 stdin 仍有未 flush 的缓冲时，close() 会再
                            # flush 一次并抛 BrokenPipeError；fd 已释放，目录清理必须继续。
                            self.close_errors.append(exc)
            try:
                self.directory.cleanup()
            except (OSError, ValueError) as exc:
                # 临时 Home 落在坏盘/NFS 上时删除失败不应中断服务关闭；
                # 进程与容器已回收，残留目录交给系统或下次启动清理。
                self.close_errors.append(exc)


class CodexAuth(_Process):
    def __init__(self, config=None, cancel=None):
        super().__init__("codex", config, cancel)
        self.messages = queue.Queue(maxsize=128)
        self.notifications = []
        self.request_id = 0
        # account/read 的载荷只留在进程内；对外快照只取收敛后的 label/plan。
        self.account = {}
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
        # codex app-server 只有 chatgptDeviceCode 一种登录方式；非零 method 或 inputs
        # 不能静默丢弃，必须与 opencode 路径同样显式拒绝。
        if method != 0 or inputs:
            raise AuthError("unsupported_auth_method")
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
                    account = result.get("account")
                    if not account:
                        raise AuthError("authorization_failed")
                    self.account = account if isinstance(account, dict) else {}
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
        except urllib.error.HTTPError as exc:
            # 非 2xx 的错误体常带 provider 的 message/attempt 状态；保留有界诊断
            # 信息供内部判断，但只以白名单错误码对外。错误体仅用于诊断，读取失败
            # 不应掩盖原始 HTTP 状态。
            try:
                raw = exc.read(MAX_BYTES + 1)
            except Exception:
                raw = b""
            finally:
                exc.close()
            if len(raw) > MAX_BYTES:
                raw = b""
            raise AuthError("auth_protocol_error", detail=_http_error_detail(exc.code, raw)) from None
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
            credentials = [connection for connection in connections
                           if isinstance(connection, dict) and connection.get("type") == "credential"
                           ] if isinstance(connections, list) else []
            connected = provider in host_connected or bool(credentials)
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
                               "methods": items, "connected": connected,
                               "connection_count": len(credentials)})
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
            try:
                result = self.http("POST", base + "/complete", {"code": code}, timeout=timeout)
            except AuthError as exc:
                if exc.code != "auth_protocol_error":
                    raise
                # POST 的 HTTP 错误和网络失败共用错误码；provider 错误体已给出
                # attempt 终态时直接使用，避免再发一次 GET 反推。detail 缺失或
                # 状态不明确时保留原有反查；查询失败时绝不重复提交验证码。
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                status = detail.get("attempt_status")
                if status == "complete":
                    return True
                if status == "failed":
                    raise AuthAttemptFailed(detail) from exc
                if status == "expired":
                    raise AuthError("auth_expired", detail=detail) from exc
                # pending 是尝试仍在处理的正常中间态：验证码可能已被消费，此时
                # 既不能再发 POST，也不能当成失败，直接进入下面的退避轮询。
                if status != "pending":
                    try:
                        value = self.http("GET", base, timeout=min(25, max(1, timeout)))
                    except AuthError:
                        raise exc
                    data = value.get("data") if isinstance(value, dict) else None
                    status = data.get("status") if isinstance(data, dict) else None
                    if status == "complete":
                        return True
                    if status == "failed":
                        raise AuthAttemptFailed() from exc
                    if status == "expired":
                        raise AuthError("auth_expired") from exc
                    # 反查也看到 pending：与 POST 返回空响应一致，继续轮询同一 attempt。
                    if status != "pending":
                        raise
            else:
                if result is not None:
                    raise AuthError("authorization_failed")
            # CLI 对失败的 attempt 也可能返回空响应；必须核对 attempt 状态，
            # 否则会把未完成授权当成成功，随后在导出凭据时才暴露成 invalid_credentials。
            # 状态推进和查询抖动都按有界退避重试，避免验证码已被消费却因一次瞬时错误失败。
            deadline = time.monotonic() + max(1, min(timeout, 10))
            interval = 0.2
            failures = 0
            while True:
                try:
                    value = self.http("GET", base, timeout=min(25, max(1, deadline - time.monotonic())))
                except AuthError as exc:
                    if exc.code != "auth_protocol_error":
                        raise
                    failures += 1
                    if failures >= 3:
                        raise
                else:
                    failures = 0
                    data = value.get("data") if isinstance(value, dict) else None
                    status = data.get("status") if isinstance(data, dict) else None
                    if status == "complete":
                        return True
                    if status == "failed":
                        raise AuthAttemptFailed()
                    if status == "expired":
                        raise AuthError("auth_expired")
                    if status != "pending":
                        raise AuthError("invalid_response")
                if time.monotonic() >= deadline:
                    raise AuthError("auth_timeout")
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
                interval = min(1.0, interval * 2)
        deadline = time.monotonic() + max(1, timeout)
        interval = 0.5
        failures = 0
        while time.monotonic() < deadline:
            try:
                value = self.http("GET", base, timeout=min(25, max(1, deadline - time.monotonic())))
            except AuthError as exc:
                if exc.code != "auth_protocol_error":
                    raise
                failures += 1
                if failures >= 10:
                    raise
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
                interval = min(3.0, interval * 2)
                continue
            failures = 0
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
        self.view_lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.public_sessions = {}
        self.sessions = {}
        self.workers = {}
        self.discoveries = set()
        self.threads = set()
        # 线程登记不占用 self.lock：后者在 install_credential/logout 期间被长时间持有，
        # 过期快照的后台刷新若等待它，请求线程仍会被阻塞。
        self.threads_lock = threading.Lock()
        self.closed = False
        # close() 期间状态文件写失败时保留首个异常，供排查；不向调用方抛出。
        self._close_error = None
        # 构造时整理旧文件的写盘失败（只读目录、磁盘满）只记录，不阻断服务启动。
        self._persist_error = None
        self.shutdown = threading.Event()
        self.ttl = ttl
        self.config = config
        self.now = now or time.time
        self.custom_factory = factory is not None
        # backend -> (expires_at, rows)；测试注入 factory 时不缓存，便于断言每次真实发现。
        self.providers_cache = {}
        self.provider_flights: dict = {}
        # 状态快照的子进程发现结果同样短 TTL 缓存，登录/登出/密钥变更时失效。
        self.status_cache = {}
        # 容器就绪（docker image inspect）按 (docker 路径, 镜像) 做进程级短 TTL 缓存；
        # 镜像存在性不随 backend 凭据变化，但仍随 close()/凭据变更一起失效。
        self.container_cache = {}
        # 过期条目由后台线程刷新；同一镜像最多一个刷新任务，避免面板并发打开时重复探测。
        self.container_refreshes = set()
        # 过期条目由后台线程刷新；同一后端最多一个刷新任务，避免面板并发打开时重复探测。
        self.status_refreshes = set()
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
                item = {key: value for key, value in item.items() if key in {"id", "backend", "provider", "state", "expires_at", "error", "accounts"}}
                item["accounts"] = _account_rows(item.get("accounts"))
                if item.get("state") in ACTIVE:
                    item.update(state="interrupted", error="process_interrupted")
                self.sessions[session_id] = item
        if previous:
            try:
                self._persist()
            except OSError as exc:
                # 状态目录只读/磁盘满时原文件仍完好：保留已加载会话继续启动，
                # 等目录恢复可写后由下一次 reserve/_update 正常落盘。
                self._persist_error = exc
                self._publish_sessions()
        else:
            self._publish_sessions()

    def _publish_sessions(self):
        view = {key: self._public(item) for key, item in self.sessions.items()}
        with self.view_lock:
            self.public_sessions = view

    def _persist(self):
        recent = set(list(self.sessions)[-30:])
        retained = {key: item for key, item in self.sessions.items()
                    if key in recent or item["state"] in ACTIVE or key in self.workers}
        stored = {}
        for key, item in retained.items():
            row = {field: value for field, value in item.items()
                   if field in {"id", "backend", "provider", "state", "expires_at", "error"}}
            accounts = _account_rows(item.get("accounts"))
            if accounts:
                row["accounts"] = accounts
            stored[key] = row
        _write_json(self.file, stored)
        self.sessions = retained
        self._publish_sessions()

    @staticmethod
    def _public(item):
        """对外快照按白名单复制稳定字段；私有标记与新增敏感字段默认不离开进程。"""
        result = copy.deepcopy({key: item[key] for key in
                                ("id", "backend", "provider", "state", "expires_at", "error", "attempt_generation",
                                 "method", "verification_url", "user_code", "instructions") if key in item})
        result["accounts"] = _account_rows(item.get("accounts"))
        return result

    def _invalidate_caches(self, backend):
        # 调用方持有 self.lock；阻止失效前启动的发现任务重新发布旧结果。
        with self.cache_lock:
            self.cache_generations[backend] += 1
            self.providers_cache.pop(backend, None)
            self.status_cache.pop(backend, None)
            self.container_cache.clear()

    def _backend(self, backend):
        if not isinstance(backend, str) or backend not in {"codex", "opencode"}:
            raise AuthError("invalid_backend")
        return backend

    @property
    def busy(self):
        with self.view_lock:
            ids = [key for key, item in self.public_sessions.items() if item["state"] in ACTIVE]
        count = 0
        for key in ids:
            try:
                result = self.status(key)
            except AuthError as exc:
                if exc.code != "unknown_session":
                    raise
                continue
            # status() 的非阻塞回收在锁竞争时可能返回陈旧 ACTIVE 快照；
            # busy 语义是「仍在进行」，过期行即便尚未被扫描也不再占用名额。
            if result["state"] in ACTIVE and self.now() < result["expires_at"]:
                count += 1
        return count

    def snapshot(self, _refresh=False):
        self.busy
        result = {}
        config = self.config() if callable(self.config) else self.config or {}
        container = config.get("sandbox", {}).get("backend") == "docker"
        readiness = None
        def container_error():
            nonlocal readiness
            if readiness is None:
                docker = shutil.which("docker")
                if not docker:
                    readiness = "auth_container_unavailable"
                else:
                    image = config.get("sandbox", {}).get("worker_image", "adaptive-rsi-worker:local")
                    readiness = self._container_readiness(docker, image, _refresh)
            return readiness
        for backend in ("codex", "opencode"):
            available = bool(shutil.which("docker") if container or not shutil.which(backend) else shutil.which(backend))

            with self.cache_lock:
                generation = self.cache_generations[backend]
                cached = self.status_cache.get(backend)
            if not self.custom_factory and cached and cached[0] > time.monotonic() and isinstance(cached[1], dict):
                result[backend] = copy.deepcopy(cached[1])
                result[backend]["available"] = available
                continue
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
                        with self.cache_lock:
                            generation = self.cache_generations[backend]
                            cached = self.status_cache.get(backend)
                        if cached and cached[0] > time.monotonic():
                            if isinstance(cached[1], dict):
                                result[backend] = copy.deepcopy(cached[1])
                                result[backend]["available"] = available
                                continue
                            capabilities, providers = cached[1]
                        else:
                            if cached and not _refresh:
                                # 过期缓存先返回陈旧值，把子进程探测移出请求线程。
                                stale = self._stale_status(backend, generation, available, container)
                                if stale is not None:
                                    result[backend] = stale
                                    continue
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
                                        if container_error():
                                            capabilities = None
                                        else:
                                            capabilities = detect_opencode(
                                                "opencode", argv=[docker, "run", "--rm", image, "opencode", "--version"],
                                                env=clean_environment())
                                    except (OpenCodeCompatibilityError, OSError):
                                        capabilities = None
                                providers = auth_providers(None, capabilities)
                            with self.cache_lock:
                                if not self.closed and generation == self.cache_generations[backend]:
                                    self.status_cache[backend] = (time.monotonic() + 25, (capabilities, providers))
                    result[backend] = self._status_view(backend, available, container, capabilities, providers)
            except OpenCodeCompatibilityError as exc:
                result[backend] = {"state": "signed_out", "available": available,
                                   "transport": "docker" if container or not shutil.which(backend) else "host",
                                   "error": exc.code, "authenticated_providers": []}
                if exc.version:
                    result[backend]["version"] = exc.version
            except AuthError:
                result[backend] = {"state": "signed_out", "available": available, "error": "invalid_credentials"}
            except RecursionError:
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
            if backend == "opencode" and not self.custom_factory and result[backend].get("error"):
                with self.cache_lock:
                    if not self.closed and generation == self.cache_generations[backend]:
                        self.status_cache[backend] = (time.monotonic() + 25, copy.deepcopy(result[backend]))
        if not self.custom_factory:
            for backend in ("codex", "opencode"):
                if container or not shutil.which(backend):
                    error = container_error()
                    if error:
                        result[backend]["available"] = False
                        result[backend]["transport"] = "docker"
                        result[backend].setdefault("error", error)
        with self.view_lock:
            result["sessions"] = [{key: item.get(key) for key in
                                   ("id", "backend", "provider", "state", "expires_at", "error", "accounts")}
                                  for item in list(self.public_sessions.values())[-30:]]
        return result

    @staticmethod
    def _status_view(backend, available, container, capabilities, providers):
        view = {"state": "authenticated" if providers else "signed_out",
                "available": available,
                "transport": "docker" if container or not shutil.which(backend) else "host",
                "authenticated_providers": providers}
        if capabilities:
            view["version"] = capabilities.version
            view["compatibility"] = "supported"
        return view

    def _stale_status(self, backend, generation, available, container):
        """过期缓存先返回陈旧值，并确保同一后端只有一个后台刷新线程。

        代际不一致（登录/登出/密钥变更已失效缓存）或缓存已被清空时返回 None，
        调用方改为同步探测，保持 _invalidate_caches 之后必须读到新数据的语义。
        """
        if self.custom_factory:
            return None
        spawn = False
        with self.cache_lock:
            if self.closed or generation != self.cache_generations[backend]:
                return None
            cached = self.status_cache.get(backend)
            if not cached:
                return None
            if isinstance(cached[1], dict):
                result = copy.deepcopy(cached[1])
                result["available"] = available
            else:
                result = self._status_view(backend, available, container, *cached[1])
            if backend not in self.status_refreshes:
                self.status_refreshes.add(backend)
                spawn = True
        if spawn:
            try:
                self._thread(lambda: self._refresh_status(backend))
            except Exception:
                with self.cache_lock:
                    self.status_refreshes.discard(backend)
                return result
        result["refreshing"] = True
        return result

    def _refresh_status(self, backend):
        """后台重跑完整快照；_refresh=True 跳过陈旧值分支，回写仍受代际校验保护。"""
        try:
            self.snapshot(_refresh=True)
        except Exception:
            # 后台探测失败不影响服务，保留旧值，下一次请求会再次尝试刷新。
            pass
        finally:
            with self.cache_lock:
                self.status_refreshes.discard(backend)

    def _container_readiness(self, docker, image, refreshing=False):
        """返回 worker 镜像就绪状态；过期缓存先返回旧值并转入后台 inspect。

        冷启动没有旧值时仍同步探测一次，与 status_cache 的取舍一致。
        refreshing=True 供后台刷新使用，跳过陈旧值直接重跑。
        只要有过期旧值就不在请求线程探测：已有刷新在跑时同样直接返回旧值，
        否则并发请求会各自同步 inspect、叠加最长 10s 的阻塞并重复探测。
        """
        key = (docker, image)
        with self.cache_lock:
            cached = self.container_cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            spawn = (bool(cached) and not refreshing and not self.closed
                     and key not in self.container_refreshes)
            if spawn:
                self.container_refreshes.add(key)
        if cached and not refreshing:
            if spawn:
                try:
                    self._thread(lambda: self._refresh_container(key))
                except Exception:
                    with self.cache_lock:
                        self.container_refreshes.discard(key)
            return cached[1]
        return self._inspect_container(key)

    def _refresh_container(self, key):
        try:
            self._inspect_container(key)
        finally:
            with self.cache_lock:
                self.container_refreshes.discard(key)

    def _inspect_container(self, key):
        docker, image = key
        try:
            inspected = run_process([docker, "image", "inspect", image], timeout=10)
            readiness = "auth_worker_image_missing" if inspected["exit_code"] or inspected["error"] else ""
        except OSError:
            readiness = "auth_container_unavailable"
        with self.cache_lock:
            if not self.closed:
                self.container_cache[key] = (time.monotonic() + CONTAINER_CACHE_TTL, readiness)
        return readiness

    def providers(self, backend="opencode"):
        self._backend(backend)
        with self.lock:
            if self.closed:
                raise AuthError("service_closed")
            with self.cache_lock:
                generation = self.cache_generations[backend]
                if not self.custom_factory:
                    cached = self.providers_cache.get(backend)
                    if cached and cached[0] > time.monotonic():
                        return copy.deepcopy(cached[1])
            if backend != "opencode":
                return []
            key = (backend, generation)
            flight = self.provider_flights.get(key)
            leader = flight is None
            if leader:
                flight: dict = {"done": threading.Event()}
                self.provider_flights[key] = flight
        if not leader:
            while not flight["done"].wait(.05):
                if self.shutdown.is_set():
                    raise AuthError("service_closed")
            if self.closed:
                raise AuthError("service_closed")
            if "error" in flight:
                raise flight["error"]
            return copy.deepcopy(flight["rows"])
        worker = None
        try:
            worker = self.factory(backend)
            with self.lock:
                if self.closed:
                    raise AuthError("service_closed")
                self.discoveries.add(worker)
            rows = worker.providers()
            completed_worker, worker = worker, None
            with self.lock:
                self.discoveries.discard(completed_worker)
            completed_worker.close()
            with self.lock:
                flight["rows"] = copy.deepcopy(rows)
                with self.cache_lock:
                    if not self.closed and not self.custom_factory and generation == self.cache_generations[backend]:
                        self.providers_cache[backend] = (time.monotonic() + PROVIDERS_CACHE_TTL, copy.deepcopy(rows))
            return rows
        except BaseException as exc:
            flight["error"] = exc
            raise
        finally:
            try:
                if worker:
                    with self.lock:
                        self.discoveries.discard(worker)
                    worker.close()
            except BaseException as exc:
                flight["error"] = exc
                raise
            finally:
                with self.lock:
                    self.provider_flights.pop(key, None)
                    flight["done"].set()

    def _thread(self, target):
        def run():
            try:
                target()
            finally:
                with self.threads_lock:
                    self.threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        with self.threads_lock:
            # 与 close() 的 closed 置位+快照同锁：先于 start() 登记，让 close() 能看到
            # 即将启动的线程；已关闭时不再启动新线程。start 失败则摘除，否则 close()
            # 会对一个从未启动的线程 join() 而抛 RuntimeError。
            if self.closed:
                return
            self.threads.add(thread)
        try:
            thread.start()
        except BaseException:
            with self.threads_lock:
                self.threads.discard(thread)
            raise

    def start(self, backend, provider=None, method=0, inputs=None):
        """reserve + launch 的便捷组合；更新准入场景在临界区内只调用 reserve。

        reserve 与 launch 之间 ttl 耗尽时返回 expired 终态，不抛 invalid_auth_state。
        """
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
            # busy 的非阻塞过期扫描在锁竞争时可能跳过；这里持锁，必须先把同后端
            # 已过 expires_at 却仍为 ACTIVE 的行收成 expired，否则过期会话会
            # 在 TTL 内持续 auth_busy。
            for session_key, item in list(self.sessions.items()):
                if (item["backend"] == backend and item["state"] in ACTIVE
                        and self.now() >= item["expires_at"]):
                    self._update(session_key, state="expired", error="auth_expired")
            if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                raise AuthError("auth_busy")
            session_id = secrets.token_urlsafe(24)
            self.sessions[session_id] = {"id": session_id, "backend": backend, "provider": provider,
                                         "state": "starting", "expires_at": self.now() + self.ttl, "error": "",
                                         "_reserved": True, "_method": method, "_inputs": copy.deepcopy(inputs)}
            try:
                self._persist()
            except OSError:
                # 状态文件写失败时不能留下只在内存里的会话：它会让同一后端在 TTL 内
                # 持续 auth_busy，而调用方只收到一次写入错误。
                self.sessions.pop(session_id, None)
                raise
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
        if inputs is not None and (not isinstance(inputs, dict) or len(inputs) > 30 or any(
            not isinstance(key, str) or not isinstance(value, str) or len(key) > 100 or len(value) > 2048
            for key, value in inputs.items())):
            raise AuthError("invalid_auth_inputs")
        self.status(session_id)
        with self.lock:
            item = self.sessions.get(session_id)
            if item is None:
                raise AuthError("unknown_session")
            if method != item.get("_method", method):
                raise AuthError("invalid_auth_method")
            if inputs != item.get("_inputs", inputs):
                raise AuthError("invalid_auth_inputs")
            inputs = copy.deepcopy(item.get("_inputs", inputs))
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

    @staticmethod
    def _terminate(item, **values):
        """就地写入新状态，并在终态清掉只对进行中会话有效的交互字段。"""
        item.update(values)
        if item["state"] in TERMINAL:
            for field in ("verification_url", "user_code", "instructions", "_code", "_retry_capable"):
                item.pop(field, None)

    def _update(self, session_id, **values):
        with self.lock:
            item = self.sessions.get(session_id)
            if item is None:
                raise AuthError("unknown_session")
            if item["state"] not in ACTIVE:
                return False
            previous = dict(item)
            self._terminate(item, **values)
            try:
                self._persist()
            except OSError:
                # 写盘失败时把内存回滚到旧值：调用方会收到异常，若内存停在新状态，
                # busy/准入会按一份从未落盘的状态拒绝后续登录（内存终态、磁盘旧态）。
                item.clear()
                item.update(previous)
                raise
            return True

    def _run(self, session_id, method, inputs):
        worker = None
        try:
            item = self.status(session_id)
            stop = threading.Event()
            with self.lock:
                entry = self.sessions.get(session_id)
                if entry is None or entry["state"] not in ACTIVE:
                    return
                self.workers[session_id] = (None, stop)
            worker = self.factory(item["backend"]) if self.custom_factory else self.factory(item["backend"], stop)
            with self.lock:
                entry = self.sessions.get(session_id)
                if entry is None or entry["state"] not in ACTIVE:
                    return
                self.workers[session_id] = (worker, stop)
            fields = worker.start(item["provider"], method, inputs)
            if not self._update(session_id, state="awaiting_authorization", attempt_generation=1, **fields):
                return
            if item["backend"] == "codex":
                success = worker.wait(stop, item["expires_at"])
            elif fields["method"] == "auto":
                success = worker.complete(timeout=max(1, item["expires_at"] - self.now()))
            else:
                attempts = 0
                while not stop.wait(0.1) and self.now() < item["expires_at"]:
                    with self.lock:
                        entry = self.sessions.get(session_id)
                        if entry is None:
                            return
                        code = entry.pop("_code", None)
                        retry_capable = entry.pop("_retry_capable", False)
                    if code is not None:
                        try:
                            success = worker.complete(code, timeout=max(1, item["expires_at"] - self.now()))
                        except AuthAttemptFailed:
                            attempts += 1
                            if attempts >= 5 or not retry_capable:
                                raise
                            with self.lock:
                                current = self.sessions.get(session_id)
                                if current is None:
                                    return
                                if self.closed or stop.is_set() or current["state"] not in ACTIVE:
                                    return
                                if self.now() >= item["expires_at"]:
                                    self._update(session_id, state="expired", error="auth_expired")
                                    return
                                self._update(session_id, state="starting", error="authorization_failed",
                                             verification_url="", user_code="", instructions="")
                            fields = worker.start(item["provider"], method, inputs)
                            with self.lock:
                                current = self.sessions.get(session_id)
                                if current is None:
                                    return
                                if self.closed or stop.is_set() or current["state"] not in ACTIVE:
                                    return
                                if self.now() >= item["expires_at"]:
                                    self._update(session_id, state="expired", error="auth_expired")
                                    return
                                if fields.get("method") != "code":
                                    raise AuthError("authorization_failed")
                                self._update(session_id, state="awaiting_authorization",
                                             error="authorization_failed", attempt_generation=attempts + 1, **fields)
                            continue
                        break
                else:
                    success = False
            with self.lock:
                current = self.sessions.get(session_id)
                if current is None:
                    return
                if current["state"] not in ACTIVE:
                    return
                if self.now() >= item["expires_at"]:
                    self._update(session_id, state="expired", error="auth_expired")
                    return
                if not success:
                    self._update(session_id, state="failed", error="authorization_failed")
                    return
                values = {"state": "completing"}
                if item["backend"] == "codex":
                    values["accounts"] = _account_entries(getattr(worker, "account", None))
                self._update(session_id, **values)
            self._save(worker, item["backend"], item["provider"], session_id, stop)
        except Exception as exc:
            if not isinstance(exc, AuthError):
                # 不记录异常文本或 traceback，第三方错误中可能包含授权材料。
                logging.getLogger(__name__).warning("Auth session %s failed unexpectedly: %s",
                                                    session_id, type(exc).__name__)
            entry = self.sessions.get(session_id)
            if entry is None:
                return
            expired = self.now() >= entry["expires_at"]
            values = {"state": "expired" if expired else "failed",
                      "error": "auth_expired" if expired else exc.code if isinstance(exc, AuthError) else "auth_protocol_error"}
            try:
                self._update(session_id, **values)
            except OSError:
                # worker 将退出，已不能重试；写盘失败也必须释放内存中的 busy 会话。
                # 下次成功写盘会保存该终态，进程重启则沿用 interrupted 恢复规则。
                with self.lock:
                    item = self.sessions.get(session_id)
                    if item is None:
                        return
                    if item["state"] in ACTIVE:
                        self._terminate(item, **values)
                        self._publish_sessions()
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
            with self.lock:
                entry = self.sessions.get(session_id)
            if stop.is_set() or entry is None or entry["state"] not in ACTIVE:
                return
            if self.now() >= entry["expires_at"]:
                self._update(session_id, state="expired", error="auth_expired")
                return
        try:
            with self.lock:
                current_session = self.sessions.get(session_id)
                if current_session is None:
                    return
                if self.closed or stop.is_set() or current_session["state"] not in ACTIVE:
                    return
                if self.now() >= current_session["expires_at"]:
                    self._update(session_id, state="expired", error="auth_expired")
                    return
                if backend == "codex":
                    # 与 opencode 分支一致：合并写回，保留用户手放的 OPENAI_API_KEY
                    # 和新版 CLI 写入的未知字段，只覆盖凭据本身的同名字段。
                    merged = _read_json_for_write(_codex_path())
                    merged.update(data)
                    _write_json(_codex_path(), merged)
                elif modern:
                    worker.install_credential(provider, value)
                else:
                    current = _read_json_for_write(auth_path())
                    current[provider] = value
                    _write_json(auth_path(), current)
                # 授权期间的状态/供应商查询可能重新填充旧缓存，保存凭据后再次失效。
                self._invalidate_caches(backend)
                self._update(session_id, state="completed")
        finally:
            credential_lock.release()

    def status(self, session_id):
        expired_worker = None
        with self.view_lock:
            item = copy.deepcopy(self.public_sessions.get(session_id)) if isinstance(session_id, str) else None
        if item is None:
            raise AuthError("unknown_session")
        if item["state"] not in ACTIVE or self.now() < item["expires_at"]:
            return item
        if not self.lock.acquire(blocking=False):
            return item
        try:
            item = self.sessions.get(session_id) if isinstance(session_id, str) else None
            if item is None:
                raise AuthError("unknown_session")
            if item["state"] in ACTIVE and self.now() >= item["expires_at"]:
                self._update(session_id, state="expired", error="auth_expired")
                # 锁内只做状态判定并摘除 workers 引用；close() 含 docker rm、killpg、
                # wait 及目录清理等慢操作，放到锁外执行。close() 自身幂等，
                # 与 cancel()/_run()/close() 的并发路径一致，不会双重关闭。
                expired_worker = self.workers.pop(session_id, None)
                if expired_worker:
                    expired_worker[1].set()
            result = self._public(item)
        finally:
            self.lock.release()
        if expired_worker and expired_worker[0]:
            expired_worker[0].close()
        return result

    def complete(self, session_id, code="", attempt_generation=None):
        if not isinstance(code, str) or len(code) > 8192:
            raise AuthError("invalid_auth_code")
        self.status(session_id)
        with self.lock:
            item = self.sessions.get(session_id)
            if item is None:
                raise AuthError("unknown_session")
            if item["state"] in ACTIVE and self.now() >= item["expires_at"]:
                self._update(session_id, state="expired", error="auth_expired")
            if item["state"] != "awaiting_authorization" or item.get("method") != "code":
                raise AuthError("invalid_auth_state")
            generation = item.get("attempt_generation", 1)
            if (attempt_generation is None and generation != 1) or (attempt_generation is not None and
                    (type(attempt_generation) is not int or attempt_generation != generation)):
                raise AuthError("invalid_auth_state")
            if not code.strip():
                raise AuthError("invalid_auth_code")
            self._update(session_id, state="completing", _code=code.strip(),
                         _retry_capable=attempt_generation is not None)
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
                if backend == "codex":
                    path = _codex_path()
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    binary = shutil.which("opencode")
                    try:
                        remove_auth_export(provider)
                        if not self.custom_factory and binary:
                            logout_provider(provider, binary)
                    except (OpenCodeCompatibilityError, RuntimeError, OSError):
                        raise AuthError("auth_protocol_error") from None
                    path = auth_path()
                    data = _read_json_for_write(path)
                    if provider in data:
                        data.pop(provider, None)
                        _write_json(path, data)
                # 凭据移除全部成功后再取消会话：移除失败时若已把会话标成 cancelled，
                # 用户虽收到 auth_protocol_error，进行中的登录却已被终止，重试也无从恢复。
                for session_id, item in self.sessions.items():
                    if item["backend"] == backend and item["state"] in ACTIVE:
                        self._update(session_id, state="cancelled")
                        if session_id in self.workers:
                            workers.append(self.workers[session_id])
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
        with self.lock:
            if self.closed:
                raise AuthError("service_closed")
            # 与 reserve() 同理：锁内的 busy 判定必须先清掉已过期但仍标记
            # ACTIVE 的同后端行，否则一次过期的尝试会在 TTL 内持续 auth_busy。
            for session_key, item in list(self.sessions.items()):
                if (item["backend"] == backend and item["state"] in ACTIVE
                        and self.now() >= item["expires_at"]):
                    self._update(session_key, state="expired", error="auth_expired")
            if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                raise AuthError("auth_busy")
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
            with self.lock:
                self.discoveries.add(worker)
            rows = worker.providers()
            if not any(row["id"] == provider and any(item["type"] == "api" and item.get("supported", True)
                                                     for item in row["methods"]) for row in rows):
                raise AuthError("unsupported_auth_method")
            with self.lock:
                if self.closed:
                    raise AuthError("service_closed")
                if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                    raise AuthError("auth_busy")
            modern = bool(getattr(getattr(worker, "capabilities", None), "modern", False))
            if modern and hasattr(worker, "set_api_key") and hasattr(worker, "export_credential"):
                worker.set_api_key(provider, key.strip())
                credentials = worker.export_credential(provider)
                if not credentials:
                    raise AuthError("invalid_credentials")
                with self.lock:
                    if self.closed:
                        raise AuthError("service_closed")
                    if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                        raise AuthError("auth_busy")
                    worker.install_credential(provider, credentials)
                    self._invalidate_caches(backend)
            else:
                with self.lock:
                    if self.closed:
                        raise AuthError("service_closed")
                    if any(item["state"] in ACTIVE and item["backend"] == backend for item in self.sessions.values()):
                        raise AuthError("auth_busy")
                    data = _read_json_for_write(auth_path())
                    data[provider] = {"type": "api", "key": key.strip()}
                    _write_json(auth_path(), data)
                    self._invalidate_caches(backend)
        finally:
            try:
                if worker:
                    with self.lock:
                        self.discoveries.discard(worker)
                    worker.close()
            finally:
                AUTH_LOCK.release()
        return self.snapshot()

    def close(self):
        with self.lock:
            self.closed = True
            self.shutdown.set()
            with self.cache_lock:
                self.providers_cache.clear()
                self.status_cache.clear()
                self.container_cache.clear()
            workers = list(self.workers.values())
            discoveries = list(self.discoveries)
            first_error = None
            for key, item in list(self.sessions.items()):
                if item["state"] in ACTIVE:
                    try:
                        self._update(key, state="interrupted", error="process_interrupted")
                    except OSError as exc:
                        # 状态文件不可写不能中断关闭：内存就地落到终态（worker 确实被中断），
                        # 磁盘旧记录交给下次启动把 ACTIVE 归一化为 interrupted。
                        first_error = first_error or exc
                        self._terminate(item, state="interrupted", error="process_interrupted")
                        self._publish_sessions()
            # closed 已在上面置位；此后 _thread() 不再启动新线程，这里只需在
            # threads_lock 下取快照，避免漏掉刚登记、尚未 start 的线程。
            with self.threads_lock:
                threads = list(self.threads)
        for worker, stop in workers:
            stop.set()
            if worker:
                try:
                    worker.close()
                except BaseException as exc:
                    # 单个 worker 关闭失败（如 stdin 残留缓冲 + 子进程已死）不能中止
                    # 关停：其余 worker、发现任务与线程回收仍必须执行，异常只留作诊断。
                    first_error = first_error or exc
        for worker in discoveries:
            try:
                worker.close()
            except BaseException as exc:
                first_error = first_error or exc
        for thread in threads:
            thread.join(timeout=2)
        self._close_error = first_error
