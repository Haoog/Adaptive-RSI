"""为 CLI 执行器准备独立 HOME；Linux 只挂载本次工作区和必要凭据。"""
import contextlib
import json
import os
import platform
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .process import Sandbox, run_process


CODEX_AUTH_LOCK = threading.RLock()


def clean_environment():
    return {key: os.environ[key] for key in ["PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"]
            if key in os.environ}


def container_endpoint(value):
    parsed = urlsplit(value)
    # 0.0.0.0 是 Ollama 等本地服务的常见监听地址写法，同样指向宿主回环。
    if parsed.hostname in ["127.0.0.1", "localhost", "::1", "0.0.0.0"]:
        return urlunsplit(parsed._replace(netloc="host.docker.internal" + (":" + str(parsed.port) if parsed.port else "")))
    return value


@contextlib.contextmanager
def cli_home(workspace):
    parent = Path(workspace) / ".rsi-agent"
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="home-", dir=parent) as directory:
        home = Path(directory)
        home.chmod(0o700)
        yield home


def codex_home(home):
    destination = home / ".codex"
    destination.mkdir()
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if source.is_file():
        shutil.copyfile(source, destination / "auth.json")
        (destination / "auth.json").chmod(0o600)
        return json.loads(source.read_text())
    return {}


def sync_codex_oauth(home, original):
    """保留刷新后的登录，只更新既有字段并避免覆盖外部登录变更。"""
    source = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    target = home / ".codex/auth.json"
    if not original.get("tokens") or not source.is_file() or not target.is_file():
        return
    current, updated = json.loads(source.read_text()), json.loads(target.read_text())
    tokens = updated.get("tokens")
    if current != original or not isinstance(tokens, dict) or tokens == original["tokens"]:
        return
    current["tokens"] = {key: tokens.get(key, value) for key, value in original["tokens"].items()}
    if "last_refresh" in original:
        current["last_refresh"] = updated.get("last_refresh", original["last_refresh"])
    with tempfile.NamedTemporaryFile("w", dir=source.parent, prefix=".auth-rsi-", delete=False) as stream:
        temporary = stream.name
        json.dump(current, stream)
    os.replace(temporary, source)
    source.chmod(0o600)


def run_cli(argv, workspace, home, config, cancel=None, protected=(), read_only=False, environment=None, state=None, timeout=0):
    env = {**clean_environment(), **(environment or {}), "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
           "CODEX_HOME": str(home / ".codex"), "TMPDIR": "/tmp", "NO_COLOR": "1"}
    if platform.system() == "Darwin" and config["sandbox"]["backend"] != "docker":
        command = Sandbox(state or home.parent, config).worker(
            argv, workspace, protected=protected, read_only=read_only,
            output_dir=home.parent)
        # CLI 数据库和缓存位于独立 HOME，源文件仍遵守只读角色限制。
        return run_process(command, workspace, timeout=timeout, cancel=cancel,
                           output_mb=config["limits"]["max_output_mb"], env=env)
    image = config["sandbox"]["worker_image"]
    if run_process(["docker", "image", "inspect", image], timeout=10)["exit_code"]:
        raise RuntimeError("CLI worker image is not cached. Build deploy/Dockerfile.worker first: " + image)
    import uuid
    name = "adaptive-rsi-worker-" + uuid.uuid4().hex[:16]
    root = str(Path(workspace).resolve())
    limits = config["sandbox"]
    command = ["docker", "run", "--rm", "--name", name, "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--user", str(os.getuid()) + ":" + str(os.getgid()),
               "--pids-limit=" + str(limits["pids"]), "--memory=" + str(limits["memory_mb"]) + "m",
               "--cpus=" + str(limits["cpus"]),
               "--add-host", "host.docker.internal:host-gateway",
               "--tmpfs", "/tmp:rw,nosuid,size=256m", "--mount", "type=bind,src=" + root + ",dst=" + root +
               (",readonly" if read_only else ""), "-w", root]
    # 只读角色仅允许将 CLI 输出写入内部目录，不可写项目代码。
    if read_only:
        internal = str(Path(workspace).resolve() / ".rsi-agent")
        command += ["--mount", "type=bind,src=" + internal + ",dst=" + internal]
    metadata = Path(workspace) / ".git"
    if metadata.exists():
        command += ["--mount", "type=bind,src=" + str(metadata) + ",dst=" + str(metadata) + ",readonly"]
    for key in env:
        if key != "PATH":
            command += ["--env", key]
    # 容器使用镜像内 CLI，不继承宿主二进制绝对路径。
    command += [image, Path(argv[0]).name, *argv[1:]]
    try:
        return run_process(command, workspace, timeout=timeout, cancel=cancel,
                           output_mb=config["limits"]["max_output_mb"], env=env)
    finally:
        run_process(["docker", "rm", "--force", name], timeout=15)


def worker_available(config, binary=None):
    if platform.system() == "Darwin" and config["sandbox"]["backend"] != "docker":
        return bool(shutil.which("sandbox-exec") and (not binary or shutil.which(binary)))
    if not shutil.which("docker"):
        return False
    result = run_process(["docker", "image", "inspect", config["sandbox"]["worker_image"]], timeout=10)
    return result["exit_code"] == 0 and not result["error"]
