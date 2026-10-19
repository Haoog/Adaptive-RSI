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


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("Only stable major.minor.patch releases are supported")
    return tuple(int(x) for x in value.lstrip("v").split("."))


def download(url, limit):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"] or parsed.username or parsed.query:
        raise ValueError("Updates must come from official GitHub release URLs")
    request = urllib.request.Request(url, headers={"User-Agent": "Adaptive-RSI-updater", "Accept": "application/vnd.github+json" if parsed.hostname == "api.github.com" else "application/octet-stream"})
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urllib.parse.urlsplit(response.url)
        if final.scheme != "https" or final.hostname not in ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"]:
            raise ValueError("Untrusted update redirect")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Release asset exceeds size limit")
    return data


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
            "checksum": assets[name + ".sha256"], "patch": version(target)[:2] == version(current)[:2]}


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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)


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
        self.shutdown = None

    def status(self):
        return self.store.setting("update-status", {"state": "unchecked", "current": __version__})

    def tick(self):
        config = self.runtime.config()["updates"]
        status = self.status()
        if config["mode"] != "manual" and time.time() - status.get("checked", 0) >= config["interval_minutes"] * 60:
            self.check_async()
        elif status.get("state") in ["available", "waiting_idle"] and (status.get("requested") or config["mode"] == "automatic" and status.get("patch")):
            if not self.store.one("SELECT 1 FROM runs WHERE ended IS NULL") and not getattr(getattr(self.runtime, "model_center", None), "busy", 0):
                self.apply_async()

    def check_async(self):
        if self.lock.locked():
            return
        threading.Thread(target=self.check, daemon=True, name="rsi-update-check").start()

    def check(self):
        if not self.lock.acquire(False):
            return
        try:
            checked = time.time()
            try:
                result = release_info(json.loads(download(RELEASE_API, 2 * 1024 * 1024)))
            except urllib.error.HTTPError as exc:
                result = {"state": "no_release" if exc.code == 404 else "error", "reason": "GitHub HTTP " + str(exc.code)}
            except Exception:
                result = {"state": "error", "reason": "Unable to verify the official release feed"}
            self.store.put_setting("update-status", {**result, "current": __version__, "checked": checked})
        finally:
            self.lock.release()
            self.runtime.wake.set()

    def apply_async(self):
        if self.shutdown is None:
            raise RuntimeError("Updates require a running service with restart supervision")
        if not self.lock.acquire(False):
            raise ValueError("An update check or installation is already in progress")
        status = self.status()
        if status.get("state") not in ["available", "waiting_idle"]:
            self.lock.release()
            raise ValueError("Check for an available release first")
        status = {**status, "requested": True}
        with self.runtime.dispatch_lock:
            self.installing = True
        threading.Thread(target=self.apply, args=(status,), daemon=True, name="rsi-update-install").start()

    def apply(self, status):
        staging = None
        try:
            if self.store.one("SELECT 1 FROM runs WHERE ended IS NULL") or getattr(getattr(self.runtime, "model_center", None), "busy", 0):
                self.store.put_setting("update-status", {**status, "state": "waiting_idle"})
                return
            self.store.put_setting("update-status", {**status, "state": "downloading"})
            # Re-read the official feed; never trust a stale or edited UI payload.
            fresh = release_info(json.loads(download(RELEASE_API, 2 * 1024 * 1024)))
            if fresh.get("version") != status.get("version"):
                raise ValueError("Release changed; check again before installing")
            checksum = download(fresh["checksum"], 4096).decode("ascii").split()[0]
            data = download(fresh["asset"], 64 * 1024 * 1024)
            installs = self.store.root / "installations"
            installs.mkdir(exist_ok=True)
            import tempfile
            staging = Path(tempfile.mkdtemp(prefix="staging-", dir=installs))
            candidate = extract_bundle(data, checksum, staging, fresh["version"])
            final = installs / (fresh["version"] + "-" + checksum[:12])
            if final.exists():
                raise ValueError("This release was already staged; inspect the previous update result")
            os.replace(candidate, final)
            previous = Path(__file__).resolve().parents[1]
            self.store.put_setting("update-status", {**fresh, "checked": time.time(), "state": "restarting"})
            session = json.loads((self.store.root / "server.json").read_text())
            plan = {"candidate": str(final), "previous": str(previous), "target": fresh["version"], "pid": os.getpid(), "session": session}
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
            self.store.put_setting("update-status", {**status, "state": "error", "reason": str(exc)[:500]})
            self.installing = False
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
            if self.status().get("state") != "restarting":
                self.installing = False
            self.lock.release()


def supervise_restart(state):
    """Run from the old installation; recover even when the new version cannot import."""
    from .store import Store
    import fcntl
    installs = state / "installations"
    plan_path = installs / "restart.json"
    plan = json.loads(plan_path.read_text())
    if plan["pid"] == os.getpid():
        raise RuntimeError("Restart supervisor must be a separate process")
    # Wait for the old runtime to release its lock, rather than trusting a reused PID.
    with (state / "runtime.lock").open("a+") as lock:
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    Store(state).put_setting("update-status", {"state": "error", "reason": "Old service did not stop; update aborted", "current": __version__})
                    return
                time.sleep(0.2)
        backup = installs / "state-before-update.sqlite3"
        with contextlib.closing(sqlite3.connect(state / "state.sqlite3")) as source, contextlib.closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    session = plan["session"]
    port = urllib.parse.urlsplit(session["origin"]).port
    log = (state / "server.log").open("ab")
    (state / "server.log").chmod(0o600)
    active = installs / "active.json"
    previous_active = json.loads(active.read_text()) if active.exists() else None
    current_link = installs / "current"
    previous_link = os.readlink(current_link) if current_link.is_symlink() else None
    child = None
    try:
        atomic_json(state / "restart-session.json", session)
        child = subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve", "--port", str(port), "--restart-session"],
                                 cwd=plan["candidate"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 25
        healthy = False
        while time.monotonic() < deadline and child.poll() is None:
            try:
                request = urllib.request.Request(session["origin"] + "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
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
        atomic_json(installs / "active.json", {"path": plan["candidate"], "version": plan["target"], "previous": plan["previous"]})
        link = installs / "current.next"
        link.unlink(missing_ok=True)
        link.symlink_to(plan["candidate"], target_is_directory=True)
        os.replace(link, installs / "current")
        Store(state).put_setting("update-status", {"state": "installed", "current": plan["target"], "checked": time.time()})
        (state / "update-ready").write_text("ready")
    except Exception:
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
        for suffix in ["-wal", "-shm"]:
            Path(str(state / "state.sqlite3") + suffix).unlink(missing_ok=True)
        shutil.copy2(backup, state / "state.sqlite3")
        Store(state).put_setting("update-status", {"state": "rolled_back", "current": __version__, "checked": time.time(), "reason": "New version failed startup; previous code and database restored"})
        atomic_json(state / "restart-session.json", session)
        subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve", "--port", str(port), "--restart-session"],
                         cwd=plan["previous"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        (state / "update-ready").write_text("ready")
    finally:
        plan_path.unlink(missing_ok=True)
        log.close()
