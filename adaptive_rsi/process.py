import csv
import contextlib
import contextvars
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


PROBE_TTL_SECONDS = 30
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE = {}
# 每个探测键保留一个进行中标记：并发线程等待首个结果，而不是各自排队重复探测。
_PROBE_INFLIGHT = {}
_OPERATION_CANCEL = contextvars.ContextVar("rsi_operation_cancel", default=None)


@contextlib.contextmanager
def operation_cancellation(cancel):
    token = _OPERATION_CANCEL.set(cancel)
    try:
        yield
    finally:
        _OPERATION_CANCEL.reset(token)


class _ProbeFlight:
    def __init__(self):
        self.done = threading.Event()
        self.error = None


def _cached_probe(key, probe):
    """Cache process-local sandbox probe results for a short interval."""
    while True:
        with _PROBE_LOCK:
            cached = _PROBE_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < PROBE_TTL_SECONDS:
                return cached[1]
            flight = _PROBE_INFLIGHT.get(key)
            if flight is None:
                flight = _PROBE_INFLIGHT[key] = _ProbeFlight()
                leader = True
            else:
                leader = False
        if not leader:
            # 首个线程正在锁外探测（docker info 等可能耗时 8-10 秒），
            # 等待它的结果而不是重复同样的探测；失败时共享同一异常。
            flight.done.wait()
            if flight.error is not None:
                raise flight.error
            continue
        try:
            value = probe()
        except BaseException as exc:
            with _PROBE_LOCK:
                flight.error = exc
                _PROBE_INFLIGHT.pop(key, None)
                flight.done.set()
            raise
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = (time.monotonic(), value)
            if len(_PROBE_CACHE) > 32:
                oldest = min(_PROBE_CACHE, key=lambda item: _PROBE_CACHE[item][0])
                _PROBE_CACHE.pop(oldest, None)
            _PROBE_INFLIGHT.pop(key, None)
            flight.done.set()
        return value


class Cancelled(RuntimeError):
    def __init__(self, message, result=None):
        self.result = result
        super().__init__(message)


def capture_cancel(function, *args, **kwargs):
    """Let CLI adapters retain partial evidence before propagating cancellation."""
    try:
        return function(*args, **kwargs), None
    except Cancelled as exc:
        if exc.result is None:
            raise
        return exc.result, exc


class ProcessError(RuntimeError):
    """Local command failure; command output never grants provider semantics."""
    failure_code = "execution"

    def __init__(self, result, message=None):
        self.result = result
        self.code = "process_" + process_error_kind(result)
        super().__init__(message or (result.get("error") or result.get("stderr") or
                                    result.get("stdout") or "Command failed")[-2000:])


def process_error_kind(result):
    # Exact legacy controller messages support injected/older runners too.
    return result.get("error_kind") or {
        "Command timed out": "timeout", "Command output limit exceeded": "output_limit",
    }.get(result.get("error"), "exit")

def run_process(argv, cwd=None, timeout=300, cancel=None, output_mb=12, env=None, input_data=None):
    output_mb = max(1, int(output_mb or 12))
    limit_bytes = output_mb * 1024 * 1024
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
        if input_data is not None:
            stdin.write(input_data)
            stdin.seek(0)
        process = subprocess.Popen([str(x) for x in argv], cwd=cwd, stdin=subprocess.DEVNULL if input_data is None else stdin,
                                   stdout=stdout, stderr=stderr, env=env, start_new_session=True)
        reason = None
        while process.poll() is None:
            if reason is None:
                try:
                    requested = bool(cancel and cancel())
                except Exception:
                    # 取消检查依赖数据库；查询失败时继续轮询，避免遗留正在运行的子进程。
                    requested = False
                if requested:
                    reason = "Cancelled"
                elif timeout and time.monotonic() - started > timeout:
                    reason = "Command timed out"
                elif os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > limit_bytes:
                    reason = "Command output limit exceeded"
            if reason:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            # A short command should wake us as soon as it exits. Keep the same
            # upper bound between cancellation, timeout and output-limit checks.
            try:
                process.wait(timeout=0.08)
            except subprocess.TimeoutExpired:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if reason is None and (os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size >
                               limit_bytes):
            reason = "Command output limit exceeded"
        # Kill descendants still in this process group before touching artifacts.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        def tail(stream):
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - output_mb * 1024 * 1024))
            return stream.read().decode("utf-8", "replace")
        result = {"argv": [str(x) for x in argv], "exit_code": process.returncode,
                  "stdout": tail(stdout), "stderr": tail(stderr),
                  "seconds": round(time.monotonic() - started, 3), "error": reason,
                  "error_kind": {"Command timed out": "timeout",
                                 "Command output limit exceeded": "output_limit",
                                 "Cancelled": "cancelled"}.get(reason, "exit" if process.returncode else None)}
        if reason == "Cancelled":
            raise Cancelled(reason, result)
        return result


def checked(argv, cwd=None, timeout=60, env=None, cancel=None):
    cancel = cancel if cancel is not None else _OPERATION_CANCEL.get()
    result = run_process(argv, cwd, timeout, env=env, cancel=cancel)
    if result["exit_code"] or result["error"]:
        raise ProcessError(result)
    return result["stdout"].rstrip("\n")


def quote_sb(path):
    return json.dumps(str(Path(path).resolve()))


def docker_mount(source, destination, read_only=False):
    """Docker --mount 按 CSV 解析，必须对完整 key=value 字段编码。"""
    fields = ["type=bind", "src=" + str(source), "dst=" + str(destination)]
    if read_only:
        fields.append("readonly")
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(fields)
    return stream.getvalue()[:-2]  # 仅移除 writer 添加的 CRLF，保留路径中的换行。


def scrub_tree(root, state=None):
    """Never follow project symlinks into host files or evaluator data."""
    root = Path(root).resolve()
    sensitive = [Path.home() / ".ssh", Path.home() / ".aws"]
    if state:
        sensitive.append(Path(state).resolve())
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [x for x in dirs if x != ".git"]
        for name in dirs + files:
            item = Path(parent) / name
            if item.is_symlink():
                try:
                    resolved = item.resolve()
                    resolved.relative_to(root)
                except (ValueError, RuntimeError):
                    try:
                        resolved = item.resolve()
                    except RuntimeError:
                        continue
                    for target in sensitive:
                        try:
                            resolved.relative_to(target)
                            raise RuntimeError("External symlink cannot enter protected host state: " + str(item.relative_to(root)))
                        except ValueError:
                            pass


class Sandbox:
    def __init__(self, state_root, config):
        self.state = Path(state_root).resolve()
        self.config = config

    def backend(self):
        requested = self.config["sandbox"]["backend"]
        if requested in ["auto", "seatbelt"] and platform.system() == "Darwin" and shutil.which("sandbox-exec"):
            return "seatbelt"
        docker = shutil.which("docker")
        if requested in ["auto", "docker"] and docker:
            key = ("backend", requested, platform.system(), docker)
            info = _cached_probe(key, lambda: run_process(
                ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8))
            if info["exit_code"] == 0:
                return "docker"
        raise RuntimeError("No working sandbox. Enable Docker or use macOS sandbox-exec.")

    def worker(self, argv, workspace, protected=(), read_only=False, output_dir=None):
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            raise RuntimeError("Use the isolated Docker CLI worker on Linux")
        workspace = Path(workspace).resolve()
        allowed = ["/System", "/Library", "/usr", "/bin", "/sbin", "/opt", "/dev", "/private/var/db",
                   "/private/var/select", "/private/etc", "/Applications", sys.base_prefix, workspace]
        binary = shutil.which(str(argv[0]))
        if binary:
            executable = Path(binary).resolve()
            packages = next((p for p in executable.parents if p.name == "node_modules"), executable.parent)
            allowed.append(packages)
        profile = '(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow network*)'
        profile += '(allow file-read-metadata)(allow file-read* (literal "/"))'
        profile += '(allow file-read* ' + " ".join('(subpath %s)' % quote_sb(p) for p in allowed) + ')'
        profile += '(deny file-read* file-write* (subpath %s))' % quote_sb(self.state)
        # Codex 启动需要检查父目录元数据；控制器文件内容和目录枚举仍不可读。
        profile += '(allow file-read-metadata (subpath %s))' % quote_sb(self.state)
        profile += '(allow file-read* file-write* (subpath %s))' % quote_sb(workspace)
        if read_only:
            profile += '(deny file-write* (subpath %s))' % quote_sb(workspace)
            if output_dir:
                profile += '(allow file-write* (subpath %s))' % quote_sb(output_dir)
        profile += '(deny file-write* (subpath %s))' % quote_sb(workspace / ".git")
        for path in protected:
            profile += '(deny file-read* file-write* (subpath %s))' % quote_sb(path)
        profile += '(deny file-read* (subpath %s)(subpath %s))' % (quote_sb(Path.home() / ".ssh"), quote_sb(Path.home() / ".aws"))
        # CLI applies workspace-write/read-only too; outer sandbox hides controller state.
        return ["sandbox-exec", "-p", profile] + argv

    def codex_permissions(self, workspace, protected=(), read_only=False, output_file=None):
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            raise RuntimeError("Codex workers require macOS Seatbelt")
        workspace = Path(workspace).resolve()
        paths = {":root": "deny", ":minimal": "read", str(self.state): "deny", str(workspace): "read" if read_only else "write",
                 str(workspace / ".git"): "read"}
        for path in ["/System", "/usr", "/bin", "/sbin", "/Library/Developer/CommandLineTools", "/opt", "/private/var/db", "/private/var/select",
                     "/private/etc", Path.home() / ".local/share/uv", sys.base_prefix]:
            paths[str(Path(path).resolve())] = "read"
        if read_only and output_file:
            output = Path(output_file).resolve()
            output.relative_to(workspace)
            paths[str(output)] = "write"
        for path in list(protected) + [Path.home() / ".ssh", Path.home() / ".aws", Path.home() / ".codex",
                                        Path.home() / ".config/gh"]:
            paths[str(Path(path).resolve())] = "deny"
        binary = shutil.which("codex")
        if binary:
            paths[str(Path(binary).resolve())] = "read"
        # 使用 Codex 自身的单层 Seatbelt；旧 CLI 不支持命名权限时会明确失败。
        table = "{filesystem={" + ",".join(
            json.dumps(path) + "=" + json.dumps(mode) for path, mode in paths.items()) + "},network={enabled=false}}"
        return ["-c", "permissions.rsi=" + table, "-c", 'default_permissions="rsi"',
                "-c", 'shell_environment_policy.inherit="all"',
                "-c", 'shell_environment_policy.include_only=["PATH","LANG","LC_ALL","TMPDIR"]']

    def command(self, argv, workspace, cwd=".", protected=(), hidden=(), writable=None):
        workspace = Path(workspace).resolve()
        location = (workspace / cwd).resolve()
        location.relative_to(workspace)
        writes = None if writable is None else [Path(item).resolve() for item in writable]
        if writes is not None:
            for item in writes:
                item.relative_to(workspace)
        if writes is not None and workspace in writes:
            writes = None
        backend = self.backend()
        if backend == "docker":
            limits = self.config["sandbox"]
            image = limits["image"]
            docker = shutil.which("docker")
            found = _cached_probe(("image", docker, image), lambda: run_process(
                ["docker", "image", "inspect", image], timeout=10))
            if found["exit_code"]:
                raise RuntimeError("Sandbox image is not cached. Pull it explicitly: " + image)
            command = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=" + str(limits["pids"]),
                    "--memory=" + str(limits["memory_mb"]) + "m", "--cpus=" + str(limits["cpus"]),
                    "--user=" + str(os.getuid()) + ":" + str(os.getgid()),
                    "--mount", docker_mount(workspace, "/workspace", read_only=writes is not None),
                    "--tmpfs", "/tmp:rw,nosuid,size=256m", "--env", "HOME=/tmp",
                    "-w", "/workspace/" + cwd]
            import uuid
            command += ["--name", "adaptive-rsi-check-" + uuid.uuid4().hex[:16]]
            if writes is not None:
                for item in writes:
                    if item.exists():
                        command += ["--mount", docker_mount(item, "/workspace/" + item.relative_to(workspace).as_posix())]
            for item in protected:
                item = Path(item).resolve()
                relative = item.relative_to(workspace).as_posix()
                command += ["--mount", docker_mount(item, "/workspace/" + relative, read_only=True)]
            return command + [image] + list(argv), None
        # Read only toolchains/system files; tests get no network, credentials, or controller data.
        allowed = ["/System", "/Library", "/usr", "/bin", "/sbin", "/opt", "/dev", "/private/var/db", "/private/var/select",
                   "/private/etc", "/Applications", str(workspace), str(Path.home() / ".local/share/uv"), sys.base_prefix]
        profile = '(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-read-metadata)'
        profile += '(allow file-read* (literal "/"))'
        profile += '(allow file-read* ' + " ".join('(subpath %s)' % quote_sb(p) for p in allowed) + ')'
        profile += '(allow file-read* file-write* (subpath %s))' % quote_sb(workspace)
        profile += '(deny file-read* file-write* (subpath %s))' % quote_sb(self.state)
        profile += '(allow file-read* file-write* (subpath %s))' % quote_sb(workspace)
        if writes is not None:
            # The later scoped grant follows the workspace-wide deny. Protected paths remain denied.
            profile += '(deny file-write* (subpath %s))' % quote_sb(workspace)
            for item in writes:
                profile += '(allow file-write* (subpath %s))' % quote_sb(item)
        for item in protected:
            profile += '(deny file-write* (subpath %s))' % quote_sb(item)
        for item in hidden:
            profile += '(deny file-read* file-write* (subpath %s))' % quote_sb(item)
        profile += '(deny network*)'
        return ["sandbox-exec", "-p", profile] + list(argv), location

    def check(self, check, workspace, cancel=None, protected=(), hidden=(), writable=None):
        from .resources import execution_resources
        with execution_resources.acquire(cancel=cancel):
            return self._check(check, workspace, cancel, protected, hidden, writable)

    def _check(self, check, workspace, cancel=None, protected=(), hidden=(), writable=None):
        scrub_tree(workspace)
        argv, cwd = self.command(check["argv"], workspace, check.get("cwd", "."), protected, hidden, writable)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path(workspace) / ".rsi-home"),
               "TMPDIR": str(Path(workspace) / ".rsi-tmp"), "CI": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GOPROXY": "off", "GOTOOLCHAIN": "local", "NO_COLOR": "1"}
        try:
            Path(env["HOME"]).mkdir(exist_ok=True)
            Path(env["TMPDIR"]).mkdir(exist_ok=True)
        except OSError:
            env["HOME"] = str(Path(tempfile.gettempdir()) / "rsi-home")
            env["TMPDIR"] = str(Path(tempfile.gettempdir()) / "rsi-tmp")
            Path(env["HOME"]).mkdir(mode=0o700, exist_ok=True)
            Path(env["TMPDIR"]).mkdir(mode=0o700, exist_ok=True)
        cleanup = argv[0] == "docker"
        try:
            result = run_process(argv, cwd, self.config["limits"]["command_seconds"], cancel,
                                 self.config["limits"]["max_output_mb"], env)
            # --rm 由 Docker 在容器退出后清理；只有超时、取消或输出超限才需要强制移除。
            cleanup = cleanup and bool(result["error"])
        finally:
            if cleanup:
                name = argv[argv.index("--name") + 1] if "--name" in argv and argv.index("--name") + 1 < len(argv) else None
                if name:
                    run_process(["docker", "rm", "--force", name], timeout=15)
        result["argv"] = check["argv"]
        result["name"] = check["name"]
        return result
