"""Versioned release installation and supervised restart, separate from project work."""
import hashlib
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import __version__


REPOSITORY = "Haoog/Adaptive-RSI"
RELEASE_API = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"


def primary_rate_limit_reset(exc):
    """Recognize only the official API's bounded primary quota reset."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 403 or exc.url != RELEASE_API:
        return None
    headers = exc.headers
    if headers is None or headers.get("X-RateLimit-Remaining") != "0":
        return None
    reset = headers.get("X-RateLimit-Reset", "")
    if not re.fullmatch(r"[0-9]{1,12}", reset):
        return None
    reset = int(reset)
    # GitHub's primary quota window is one hour; reject stale or unbounded waits.
    return reset if 0 < reset - time.time() <= 3600 else None


class UpdateInProgress(RuntimeError):
    code = "update_in_progress"


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Only stable major.minor.patch releases are supported")
    return tuple(int(x) for x in value.lstrip("v").split("."))


def automatic_allowed(status, policy="patch"):
    if not status.get("version"):
        return False
    current, target = version(status.get("current", __version__)), version(status["version"])
    return target > current and (policy == "stable" or policy == "minor" and target[0] == current[0] or
                                 policy == "patch" and target[:2] == current[:2])


def download(url, limit, progress=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"] or parsed.username or parsed.query:
        raise ValueError("Updates must come from official GitHub release URLs")
    request = urllib.request.Request(url, headers={"User-Agent": "Adaptive-RSI-updater", "Accept": "application/vnd.github+json" if parsed.hostname == "api.github.com" else "application/octet-stream"})
    deadline = time.monotonic() + 900
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlsplit(response.url)
        if final.scheme != "https" or final.hostname not in ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"]:
            raise ValueError("Untrusted update redirect")
        # 分块读取并提供进度；校验仍在完整读取后进行。
        headers = getattr(response, "headers", None)
        length = headers.get("Content-Length") if headers else None
        total = int(length) if length and length.isdigit() and int(length) <= limit else None
        data = bytearray()
        # read1 避免缓慢滴注一直填不满 read(n)，总预算包含连接阶段。
        read = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("Release download exceeded the total time budget")
            chunk = read(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("Release asset exceeds size limit")
            if time.monotonic() > deadline:
                raise ValueError("Release download exceeded the total time budget")
            if progress and total:
                progress(len(data), total)
    if progress and total:
        progress(len(data), total)
    return bytes(data)


def release_info(data, current=__version__):
    target = data.get("tag_name", "")
    if data.get("draft") or data.get("prerelease") or version(target) <= version(current):
        return {"state": "up_to_date", "current": current}
    target = target.lstrip("v")
    name = "adaptive-rsi-" + target + "-plugin.zip"
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    if name not in assets or name + ".sha256" not in assets:
        raise ValueError("Release is missing its plugin ZIP or SHA-256 file")
    prefix = "https://github.com/" + REPOSITORY + "/releases/download/"
    if not all(assets[k].startswith(prefix) for k in [name, name + ".sha256"]):
        raise ValueError("Release assets belong to another repository")
    return {"state": "available", "current": current, "version": target, "asset": assets[name],
            "checksum": assets[name + ".sha256"],
            "notes": str(data.get("body") or "")[:16000],
            "patch": version(target)[:2] == version(current)[:2]}


def extract_bundle(data, expected_hash, destination, target):
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash) or hashlib.sha256(data).hexdigest() != expected_hash.lower():
        raise ValueError("Release SHA-256 verification failed")
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = 0
        for item in archive.infolist():
            parts = Path(item.filename).parts
            total += item.file_size
            if not parts or parts[0] != "adaptive-rsi" or Path(item.filename).is_absolute() or ".." in parts or "\\" in item.filename or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Unsafe release archive path")
            if total > 128 * 1024 * 1024 or item.file_size > 16 * 1024 * 1024:
                raise ValueError("Unpacked release exceeds size limit")
        archive.extractall(destination)
    root = destination / "adaptive-rsi"
    manifest = json.loads((root / "update.json").read_text())
    if manifest != {"format": 1, "version": target, "python_min": [3, 9], "state_schema": 1}:
        raise ValueError("Unsupported update manifest or state migration")
    if json.loads((root / "plugin.json").read_text())["version"] != target:
        raise ValueError("Plugin manifest does not match release version")
    if not (root / "adaptive_rsi/cli.py").is_file() or not (root / "scripts/start.py").is_file():
        raise ValueError("Incomplete release bundle")
    return root


def atomic_json(path, data):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# 大状态目录的迁移和 WAL 恢复可能较慢；启动器和监督器采用相同健康检查预算。
RESTART_HEALTH_SECONDS = 60


def supervisor_active(state):
    import fcntl
    try:
        with (state / "installations/supervisor.lock").open("a+") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


def installed_root(state, fallback):
    marker = state / "installations/active.json"
    if not marker.exists():
        return Path(fallback).resolve()
    data = json.loads(marker.read_text())
    root = Path(data["path"]).resolve()
    root.relative_to((state / "installations").resolve())
    if not (root / "adaptive_rsi/cli.py").is_file():
        raise RuntimeError("Managed installation is incomplete; restore the previous installation")
    return root


class UpdateManager:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.lock = threading.Lock()
        self.installing = False
        self.restart_target = None
        self.shutdown = None
        self.recovering = False
        self.legacy_deadline = None

    def status(self):
        return self.store.setting("update-status", {"state": "unchecked", "current": __version__})

    def completed(self, status):
        awaiting_restart = self.restart_target is not None or self.recovering or self.legacy_deadline is not None
        if awaiting_restart and status.get("state") in ["installed", "rolled_back"] and status.get("current") == (self.restart_target or __version__):
            self.installing = False
            self.restart_target = None
            self.recovering = False
            self.legacy_deadline = None
            return True
        return False

    def restart_plan(self):
        try:
            return json.loads((self.store.root / "installations/restart.json").read_text())
        except FileNotFoundError:
            return None

    def observe_plan(self, plan):
        if "phase" not in plan or "state_schema" not in plan:
            # 0.3.0 supervisors have neither our lock nor a recoverable phase journal.
            self.recovering = False
            if self.legacy_deadline is None:
                self.legacy_deadline = time.monotonic() + 90
            return False
        self.legacy_deadline = None
        return True

    def restore_startup(self):
        status = self.status()
        if self.completed(status):
            return
        if status.get("state") == "recovery_required":
            self.installing = True
        if status.get("state") == "checking":
            previous = status.get("previous_status", {})
            self.store.put_setting("update-status", self.check_failure(previous, "Update check interrupted; retry is safe"))
        if status.get("state") == "downloading":
            self.store.put_setting("update-status", {**status, "state": "error", "reason": "Installation interrupted before restart; retry is safe"})
        # 恢复登录会话不代表正在升级；仅新版本等待监督进程确认安装。
        if status.get("state") == "restarting" and status.get("version") == __version__:
            self.restart_target = __version__
            self.installing = True
        if status.get("state") == "restarting":
            self.installing = True
            plan = self.restart_plan()
            if self.completed(self.status()):
                return
            if plan is None:
                self.recovering = False
                recovery_required(self.store.root, "Restart plan is missing; inspect installations before admitting work")
            elif self.observe_plan(plan):
                self.recovering = not supervisor_active(self.store.root)

    def is_busy(self):
        with self.runtime.dispatch_lock:
            status = self.status()
            # Completion can precede journal deletion or race an earlier recovery decision.
            if self.completed(status):
                (self.store.root / "update-ready").unlink(missing_ok=True)
                return False
            if status.get("state") != "restarting":
                self.recovering = False
            if self.installing and status.get("state") == "restarting":
                plan = self.restart_plan()
                status = self.status()
                if self.completed(status):
                    return False
                if status.get("state") != "restarting":
                    self.recovering = False
                elif plan is None:
                    self.recovering = False
                    recovery_required(self.store.root, "Restart plan disappeared before completion; preserve current state and inspect installations")
                elif not self.observe_plan(plan):
                    if time.monotonic() >= self.legacy_deadline:
                        latest = self.status()
                        if self.completed(latest):
                            return False
                        if latest.get("state") == "restarting":
                            recovery_required(self.store.root, "Legacy supervisor did not confirm installation within 90 seconds; preserve current database and inspect installations")
                elif time.time() - status.get("checked", time.time()) > 5 and not supervisor_active(self.store.root):
                    self.recovering = True
            if self.recovering and self.shutdown is not None:
                plan = self.restart_plan()
                latest = self.status()
                if self.completed(latest):
                    return False
                self.recovering = False
                if latest.get("state") != "restarting" or plan is None or not self.observe_plan(plan):
                    return self.installing
                # A replacement supervisor waits for this guarded service to release its lock.
                plan["pid"] = os.getpid()
                atomic_json(self.store.root / "installations/restart.json", plan)
                with (self.store.root / "update.log").open("ab") as log:
                    subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(self.store.root), "update-worker"],
                                     cwd=plan["previous"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
                service_pid = plan.get("rollback_pid") if plan.get("phase") in ["rollback_health_check", "rollback_ack"] else plan.get("candidate_pid")
                if service_pid != os.getpid():
                    self.shutdown()
            if not self.installing:
                (self.store.root / "update-ready").unlink(missing_ok=True)
            return self.installing

    def channel(self):
        """一次性判定安装渠道：桌面应用、内置自更新布局或直接 pip 安装。"""
        cached = getattr(self, "_channel", None)
        if cached:
            return cached
        if getattr(self, "desktop_managed", False):
            self._channel = "desktop"
        elif (self.store.root / "installations" / "active.json").exists():
            self._channel = "managed"
        else:
            parts = Path(__file__).resolve().parts
            self._channel = "pip" if "site-packages" in parts or "dist-packages" in parts else "managed"
        return self._channel

    def snapshot(self):
        busy = self.is_busy()
        return {**self.status(), "busy": busy, "channel": self.channel(),
                "desktop_managed": getattr(self, "desktop_managed", False)}

    def require_ready(self):
        if self.is_busy():
            raise UpdateInProgress("Software update in progress")

    def tick(self):
        if getattr(self, "desktop_managed", False):
            return
        config = self.runtime.config()["updates"]
        status = self.status()
        if self.cooling_down(status):
            return
        pending_install = status.get("version") and (status.get("requested") or
                          config["mode"] == "automatic" and automatic_allowed(status, config["auto_policy"]))
        reset_due = status.get("reason_code") == "github_primary_rate_limit" and not pending_install
        if config["mode"] != "manual" and (reset_due or time.time() - status.get("checked", 0) >= config["interval_minutes"] * 60):
            self.check_async()
        elif (status.get("state") in ["available", "waiting_idle"] or
              status.get("state") == "error" and status.get("version") and status.get("failures", 0) < 3 and time.time() >= status.get("retry_after", 0)) and (
                status.get("requested") or config["mode"] == "automatic" and automatic_allowed(status, config["auto_policy"])):
            if not self.work_busy():
                self.apply_async(requested=bool(status.get("requested")))

    def work_busy(self):
        return (getattr(self.runtime, "import_in_progress", False) or
                self.store.one("SELECT 1 FROM runs WHERE ended IS NULL") or
                getattr(getattr(self.runtime, "model_center", None), "busy", 0) or
                getattr(getattr(self.runtime, "auth_sessions", None), "busy", 0))

    @staticmethod
    def cooling_down(status):
        return (status.get("reason_code") == "github_primary_rate_limit" and
                time.time() < status.get("rate_limit_until", 0))

    @staticmethod
    def without_rate_limit(status):
        return {key: value for key, value in status.items() if key not in ["reason_code", "rate_limit_until"]}

    @staticmethod
    def rate_limit_failure(previous, reset):
        return {**previous, "state": "error", "current": __version__, "checked": time.time(),
                "reason": "GitHub HTTP 403", "reason_code": "github_primary_rate_limit", "rate_limit_until": reset}

    @staticmethod
    def check_failure(previous, reason):
        previous = UpdateManager.without_rate_limit(previous)
        # A failed observation cannot revoke consent for a specific pending target.
        pending = (previous.get("state") in ["waiting_idle", "error"] and
                   previous.get("requested") is True and previous.get("version"))
        return {**(previous if pending else {}), "state": "error", "current": __version__,
                "checked": time.time(), "reason": reason}

    def begin_check(self):
        if not self.lock.acquire(False):
            return None
        try:
            previous = self.status()
            if self.cooling_down(previous):
                self.lock.release()
                return None
            self.store.put_setting("update-status", {"state": "checking", "current": __version__,
                                                    "check_started": time.time(), "checked": previous.get("checked", 0),
                                                    "previous_status": previous})
            return previous
        except BaseException:
            self.lock.release()
            raise

    def check_async(self):
        previous = self.begin_check()
        if previous is None:
            return False
        try:
            threading.Thread(target=self.finish_check, args=(previous,), daemon=True, name="rsi-update-check").start()
        except BaseException:
            try:
                self.store.put_setting("update-status", previous)
            finally:
                self.lock.release()
            raise
        return True

    def check(self):
        previous = self.begin_check()
        if previous is None:
            return False
        self.finish_check(previous)
        return True

    def finish_check(self, previous):
        try:
            try:
                result = release_info(json.loads(download(RELEASE_API, 2 * 1024 * 1024)))
            except urllib.error.HTTPError as exc:
                reason = "GitHub HTTP " + str(exc.code)
                reset = primary_rate_limit_reset(exc)
                result = (self.rate_limit_failure(previous, reset) if reset is not None else
                          {"state": "no_release", "reason": reason} if exc.code == 404 else
                          self.check_failure(previous, reason))
            except Exception:
                result = self.check_failure(previous, "Unable to verify the official release feed")
            if (previous.get("state") in ["waiting_idle", "error"] and previous.get("requested") is True and
                    previous.get("version") == result.get("version") and result.get("state") == "available"):
                result.update(state="waiting_idle", requested=True)
            self.store.put_setting("update-status", {**result, "current": __version__, "checked": time.time()})
        finally:
            self.lock.release()
            self.runtime.wake.set()

    def apply_async(self, requested=True):
        if getattr(self, "desktop_managed", False):
            raise ValueError("This runtime is bundled with the desktop app; update the desktop package")
        if self.shutdown is None:
            raise RuntimeError("Updates require a running service with restart supervision")
        if not self.lock.acquire(False):
            raise ValueError("An update check or installation is already in progress")
        status = self.status()
        if self.cooling_down(status):
            self.lock.release()
            raise ValueError("GitHub API rate limit reset is pending")
        if status.get("state") not in ["available", "waiting_idle", "error"] or not status.get("version"):
            self.lock.release()
            raise ValueError("Check for an available release first")
        status = {**status, "requested": requested}
        with self.runtime.dispatch_lock:
            self.installing = True
        threading.Thread(target=self.apply, args=(status,), daemon=True, name="rsi-update-install").start()

    def apply(self, status):
        staging = None
        try:
            if self.cooling_down(self.status()):
                return
            status = self.without_rate_limit(status)
            if self.work_busy():
                self.store.put_setting("update-status", {**status, "state": "waiting_idle"})
                return
            self.store.put_setting("update-status", {**status, "state": "downloading"})
            # Re-read the official feed; never trust a stale or edited UI payload.
            fresh = release_info(json.loads(download(RELEASE_API, 2 * 1024 * 1024)))
            if fresh.get("version") != status.get("version"):
                raise ValueError("Release changed; check again before installing")
            policy = self.runtime.config()["updates"]
            if not status.get("requested") and (policy["mode"] != "automatic" or not automatic_allowed(fresh, policy["auto_policy"])):
                self.store.put_setting("update-status", {**fresh, "state": "available", "checked": time.time(), "reason": "Automatic update authorization changed"})
                return
            checksum = download(fresh["checksum"], 4096).decode("ascii").split()[0]
            last_report = [0.0]

            def report(done, total):
                # 进度写入限频；失败不阻断下载本身。
                moment = time.monotonic()
                if moment - last_report[0] < 1.0 and done < total:
                    return
                last_report[0] = moment
                try:
                    self.store.put_setting("update-status",
                                           {**status, "state": "downloading", "progress": done * 100.0 / total})
                except Exception:
                    pass
            data = download(fresh["asset"], 64 * 1024 * 1024, report)
            installs = self.store.root / "installations"
            installs.mkdir(exist_ok=True)
            import tempfile
            staging = Path(tempfile.mkdtemp(prefix="staging-", dir=installs))
            candidate = extract_bundle(data, checksum, staging, fresh["version"])
            final = installs / (fresh["version"] + "-" + checksum[:12])
            if final.exists():
                if final.is_symlink():
                    raise ValueError("Managed installation directory cannot be a symlink")
                def contents(directory):
                    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
                if contents(candidate) != contents(final):
                    raise ValueError("Previously staged installation differs from the verified bundle")
            else:
                os.replace(candidate, final)
            previous = Path(__file__).resolve().parents[1]
            self.store.put_setting("update-status", {**fresh, "checked": time.time(), "state": "restarting"})
            session = json.loads((self.store.root / "server.json").read_text())
            plan = {"candidate": str(final), "previous": str(previous), "target": fresh["version"], "pid": os.getpid(), "session": session,
                    "phase": "prepared", "state_schema": 1}
            plan_file = installs / "restart.json"
            atomic_json(plan_file, plan)
            log = (self.store.root / "update.log").open("ab")
            (self.store.root / "update.log").chmod(0o600)
            try:
                subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(self.store.root), "update-worker"],
                                 cwd=previous, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            finally:
                log.close()
            self.shutdown()
        except Exception as exc:
            reset = primary_rate_limit_reset(exc)
            if reset is not None:
                self.store.put_setting("update-status", self.rate_limit_failure(status, reset))
            else:
                failures = status.get("failures", 0) + 1
                self.store.put_setting("update-status", {**status, "state": "error", "reason": str(exc)[:500],
                                                        "failures": failures, "retry_after": time.time() + min(3600, 60 * 2 ** failures)})
            with self.runtime.dispatch_lock:
                self.installing = False
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            if self.status().get("state") != "restarting":
                with self.runtime.dispatch_lock:
                    self.installing = False
            self.lock.release()


def supervise_restart(state):
    """Run from the old installation; recover even when the new version cannot import."""
    import fcntl
    installs = state / "installations"
    with (installs / "supervisor.lock").open("a+") as supervisor_lock:
        try:
            fcntl.flock(supervisor_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if not (installs / "restart.json").exists():
            return
        _supervise_restart(state)


def candidate_health(plan):
    session = plan["session"]
    try:
        request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/health",
            headers={"Cookie": session["cookie"] + "=" + session["token"]})
        with urllib.request.urlopen(request, timeout=1) as response:
            health = json.load(response)
        return isinstance(health, dict) and health.get("version") == plan["target"] and health.get("pid") == plan.get("candidate_pid")
    except (OSError, ValueError):
        return False


def complete_rollback(state, plan):
    from .store import Store
    # 旧运行器仅识别 installed 作为解除屏障的握手；先通过健康检查才发送。
    store = Store(state)
    try:
        plan["phase"] = "rollback_ack"
        atomic_json(state / "installations/restart.json", plan)
        status = {"current": __version__, "checked": time.time(),
                  "reason": "Previous code and database restored and health verified"}
        store.put_setting("update-status", {**status, "state": "installed"})
        session = plan["session"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/state",
                    headers={"Cookie": session["cookie"] + "=" + session["token"]})
                with urllib.request.urlopen(request, timeout=1) as response:
                    ready = not json.load(response)["update"]["busy"]
                if ready:
                    store.put_setting("update-status", {**status, "state": "rolled_back"})
                    (state / "update-ready").write_text("ready")
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(0.2)
        return False
    finally:
        store.close()


def process_present(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def recovery_required(state, reason):
    from .store import Store
    Store(state).put_setting("update-status", {"state": "recovery_required", "current": __version__,
                                              "reason": reason, "checked": time.time()})


def valid_backup(path):
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
            return source.execute("PRAGMA quick_check").fetchone()[0] == "ok" and source.execute("SELECT count(*) FROM settings").fetchone() is not None
    except (OSError, sqlite3.Error):
        return False


def preserve_previous_pointers(state, plan):
    """Journal rollback pointers before any installation pointer can change."""
    if "previous_pointers" not in plan:
        installs = state / "installations"
        active, link = installs / "active.json", installs / "current"
        plan["previous_pointers"] = {"active": json.loads(active.read_text()) if active.exists() else None,
                                     "current": os.readlink(link) if link.is_symlink() else None}
        atomic_json(installs / "restart.json", plan)
    return plan["previous_pointers"]


def commit_installation(state, plan):
    from .store import Store
    installs = state / "installations"
    preserve_previous_pointers(state, plan)
    atomic_json(installs / "active.json", {"path": plan["candidate"], "version": plan["target"], "previous": plan["previous"]})
    link = installs / "current.next"
    link.unlink(missing_ok=True)
    link.symlink_to(plan["candidate"], target_is_directory=True)
    os.replace(link, installs / "current")
    plan["phase"] = "committed"
    atomic_json(installs / "restart.json", plan)
    Store(state).put_setting("update-status", {"state": "installed", "current": plan["target"], "checked": time.time()})
    try:
        (state / "update-ready").write_text("ready")
    except OSError:
        pass


def _supervise_restart(state):
    from .store import Store
    import fcntl
    installs = state / "installations"
    plan_path = installs / "restart.json"
    plan = json.loads(plan_path.read_text())
    if plan["pid"] == os.getpid():
        raise RuntimeError("Restart supervisor must be a separate process")
    if plan.get("state_schema", 1) != 1:
        recovery_required(state, "Unsupported state migration; refusing automatic rollback")
        return
    status = Store(state).setting("update-status", {})
    if status.get("state") == "installed" and plan.get("phase") != "rollback_ack":
        # Work may already have been admitted. An old journal never authorizes rollback.
        plan_path.unlink(missing_ok=True)
        return
    if status.get("state") == "recovery_required":
        return
    plan["supervisor_pid"] = os.getpid()
    atomic_json(plan_path, plan)
    phase = plan.get("phase", "prepared")
    if phase in ["rollback_health_check", "rollback_ack"]:
        rollback_plan = {**plan, "target": __version__, "candidate_pid": plan.get("rollback_pid")}
        deadline = time.monotonic() + RESTART_HEALTH_SECONDS
        while time.monotonic() < deadline and process_present(plan.get("rollback_pid")):
            if candidate_health(rollback_plan):
                if complete_rollback(state, plan):
                    plan_path.unlink(missing_ok=True)
                else:
                    recovery_required(state, "Restored service did not acknowledge the health-verified rollback")
                return
            time.sleep(0.2)
        recovery_required(state, "Rolled-back service cannot be verified; preserve the database and restart plan")
        return
    if phase == "committed":
        if candidate_health(plan):
            commit_installation(state, plan)
            plan_path.unlink(missing_ok=True)
        else:
            recovery_required(state, "Committed candidate cannot be verified; preserve current database and inspect the retained restart plan")
        return
    if phase == "health_check" and process_present(plan.get("candidate_pid")):
        deadline = time.monotonic() + RESTART_HEALTH_SECONDS
        while time.monotonic() < deadline and process_present(plan.get("candidate_pid")):
            if candidate_health(plan):
                commit_installation(state, plan)
                plan_path.unlink(missing_ok=True)
                return
            time.sleep(0.2)
        if process_present(plan.get("candidate_pid")):
            recovery_required(state, "An unverified candidate is still running; preserve database and inspect the retained restart plan")
            return
    elif phase not in ["prepared", "health_check"]:
        recovery_required(state, "Unknown restart phase; refusing to overwrite state")
        return
    # Wait for the old runtime to release its lock, rather than trusting a reused PID.
    with (state / "runtime.lock").open("a+") as lock:
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    recovery_required(state, "A service still owns runtime.lock; inspect the retained restart plan before recovery")
                    return
                time.sleep(0.2)
        backup = installs / "state-before-update.sqlite3"
        if not plan.get("backup_ready"):
            with contextlib.closing(sqlite3.connect(state / "state.sqlite3")) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
                source.backup(destination)
            plan["backup_ready"] = True
            atomic_json(plan_path, plan)
        if not valid_backup(backup):
            recovery_required(state, "Recovery backup is missing or corrupt; refusing an unsafe database rollback")
            return
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    session = plan["session"]
    port = urllib.parse.urlsplit(session.get("local_origin", session["origin"])).port
    log = (state / "server.log").open("ab")
    (state / "server.log").chmod(0o600)
    active = installs / "active.json"
    pointers = preserve_previous_pointers(state, plan)
    previous_active = pointers["active"]
    current_link = installs / "current"
    previous_link = pointers["current"]
    child = None
    try:
        atomic_json(state / "restart-session.json", session)
        child = subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve", "--port", str(port), "--restart-session"],
                                 cwd=plan["candidate"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        plan.update(phase="health_check", candidate_pid=child.pid)
        atomic_json(plan_path, plan)
        deadline = time.monotonic() + RESTART_HEALTH_SECONDS
        healthy = False
        while time.monotonic() < deadline and child.poll() is None:
            try:
                request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
                with urllib.request.urlopen(request, timeout=1) as response:
                    health = json.load(response)
                    healthy = health.get("version") == plan["target"] and health.get("pid") == child.pid
                if healthy:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        if not healthy:
            raise RuntimeError("New version failed its startup health check")
        commit_installation(state, plan)
    except Exception:
        if plan.get("phase") == "committed" or Store(state).setting("update-status", {}).get("state") == "installed":
            if Store(state).setting("update-status", {}).get("state") != "installed":
                recovery_required(state, "Installation commit was interrupted; preserve current database and restart plan")
            return
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if previous_active is None:
            active.unlink(missing_ok=True)
        else:
            atomic_json(active, previous_active)
        current_link.unlink(missing_ok=True)
        if previous_link is not None:
            current_link.symlink_to(previous_link, target_is_directory=True)
        # Restore only while no runtime owns the database; never overwrite a live service.
        with (state / "runtime.lock").open("a+") as recovery_lock:
            try:
                fcntl.flock(recovery_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                recovery_required(state, "Another runtime owns the database; recovery plan and database were preserved")
                return
            if not valid_backup(backup):
                recovery_required(state, "Recovery backup is corrupt; current database was preserved")
                return
            restore = installs / "state-restore.sqlite3"
            shutil.copy2(backup, restore)
            for suffix in ["-wal", "-shm"]:
                Path(str(state / "state.sqlite3") + suffix).unlink(missing_ok=True)
            os.replace(restore, state / "state.sqlite3")
        # 健康检查完成前保留调度屏障，避免旧服务启动后提前接纳工作。
        Store(state).put_setting("update-status", {"state": "restarting", "current": __version__, "version": __version__, "checked": time.time(), "reason": "Verifying restored service"})
        plan.update(phase="rollback_health_check")
        atomic_json(plan_path, plan)
        atomic_json(state / "restart-session.json", session)
        previous_child = subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve", "--port", str(port), "--restart-session"],
                         cwd=plan["previous"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        plan["rollback_pid"] = previous_child.pid
        atomic_json(plan_path, plan)
        # 回滚重启的旧版本同样要通过启动健康检查；不能无条件宣布就绪后留下没有服务在跑的状态。
        deadline = time.monotonic() + RESTART_HEALTH_SECONDS
        previous_healthy = False
        while time.monotonic() < deadline and previous_child.poll() is None:
            try:
                request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/health",
                    headers={"Cookie": session["cookie"] + "=" + session["token"]})
                with urllib.request.urlopen(request, timeout=1) as response:
                    health = json.load(response)
                previous_healthy = health.get("version") == __version__ and health.get("pid") == previous_child.pid
                if previous_healthy:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.2)
        if not previous_healthy:
            recovery_required(state, "Rolled-back service failed its startup health check; database was restored, inspect server.log and restart manually")
            return
        if not complete_rollback(state, plan):
            recovery_required(state, "Restored service did not acknowledge the health-verified rollback")
            return
    finally:
        final_state = Store(state).setting("update-status", {}).get("state")
        if final_state == "rolled_back" or final_state == "installed" and plan.get("phase") != "rollback_ack":
            plan_path.unlink(missing_ok=True)
        log.close()
