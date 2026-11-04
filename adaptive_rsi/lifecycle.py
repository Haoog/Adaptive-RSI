"""Admission barrier for replacement of a desktop-owned runtime."""
import hmac
import errno
import os
import re
import secrets
import time

from . import __version__
from .store import encode


KEY = "desktop-update-barrier"


def _observable(state):
    """Only a record with a lease and version can be owned, cancelled or completed."""
    return (isinstance(state, dict) and isinstance(state.get("lease"), str) and bool(state["lease"]) and
            isinstance(state.get("version"), str) and bool(state["version"]))


def _matches(stored, lease):
    return (isinstance(stored, str) and isinstance(lease, str) and stored.isascii() and lease.isascii() and
            hmac.compare_digest(stored, lease))


def _recover(runtime, state, reason):
    with runtime.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM settings WHERE key=?", (KEY,))
        db.execute("INSERT INTO events(kind,message,data,created) VALUES "
                   "('desktop_update_recovered','Desktop update admission recovered',?,?)",
                   (encode({"phase": state.get("phase"), "version": state.get("version"),
                            "reason": reason}), time.time()))
    runtime.wake.set()


def barrier(runtime):
    with runtime.dispatch_lock:
        state = runtime.store.setting(KEY)
        if not state:
            return state
        if not _observable(state):
            # 损坏记录没有可用租约，也判定不了属主；保留只会永久关闭准入。
            _recover(runtime, state if isinstance(state, dict) else {}, "corrupt")
            return None
        expires = state.get("expires")
        expired = (state.get("phase") != "installing" and
                   (not isinstance(expires, (int, float)) or not expires > time.time()))
        owner_exited = False
        owner = state.get("owner_pid")
        if state.get("phase") == "installing" and type(owner) is int and owner > 1:
            try:
                os.kill(owner, 0)
            except OSError as exc:
                # Permission errors and unknown process state must keep admission closed.
                owner_exited = exc.errno == errno.ESRCH
        if expired or owner_exited:
            _recover(runtime, state, "owner_exited" if owner_exited else "drain_expired")
            return None
        return state


def readiness(runtime, action="status", version=None, lease=None):
    with runtime.dispatch_lock:
        state = barrier(runtime)
        if action not in ("status", "prepare", "commit", "cancel", "complete"):
            raise ValueError("Invalid desktop update action")
        if action != "status" and not getattr(runtime.updates, "desktop_managed", False):
            raise ValueError("Only a desktop-owned runtime can be replaced")
        if action == "prepare":
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("Invalid desktop update version")
            if state and (state.get("version") != version or not _matches(state.get("lease"), lease)):
                raise ValueError("Another desktop update owns admission")
            if not state:
                state = {"version": version, "lease": secrets.token_hex(24), "phase": "draining", "expires": time.time() + 3600}
            elif state.get("phase") == "draining":
                state = {**state, "expires": time.time() + 3600}
            runtime.store.put_setting(KEY, state)
        elif action in ("cancel", "complete") and state:
            if not _matches(state.get("lease"), lease):
                raise ValueError("Desktop update lease changed")
            if action == "complete" and state.get("version") != __version__:
                raise ValueError("Desktop runtime version does not match the update")
            runtime.store.execute("DELETE FROM settings WHERE key=?", (KEY,))
            runtime.wake.set()
            state = None
        runs = runtime.store.query("SELECT id,project,state FROM runs WHERE ended IS NULL")
        reasons = ["running_tasks"] if runs else []
        for name, busy in (("model_probe", getattr(runtime.model_center, "busy", 0)),
                           ("model_catalog", len(getattr(runtime.model_center, "catalog_inflight", ()))),
                           ("authentication", getattr(runtime.auth_sessions, "busy", 0)),
                           ("project_import", runtime.import_in_progress),
                           ("maintenance", runtime.maintenance_lock.locked())):
            if busy:
                reasons.append(name)
        operations = getattr(runtime, "operations", None)
        if operations and operations.busy():
            reasons.append("background_operations")
        if action == "commit":
            if not state or not _matches(state.get("lease"), lease):
                raise ValueError("Desktop update lease changed")
            if reasons:
                raise ValueError("Runtime still has active operations")
            owner = getattr(runtime.updates, "desktop_owner_pid", None)
            if type(owner) is not int or owner <= 1:
                # 没有可观测的安装器属主时写入 installing，屏障的过期与属主退出两条
                # 清除路径都不可达，会永久关闭全部运行准入。拒绝 commit 并释放
                # draining 屏障，避免失败的更新把准入再挡到租约过期。
                if state.get("phase") == "draining":
                    runtime.store.execute("DELETE FROM settings WHERE key=?", (KEY,))
                    runtime.wake.set()
                    state = None
                raise ValueError("Desktop update has no observable installer owner")
            state = {**state, "phase": "installing", "owner_pid": owner, "committed": time.time()}
            runtime.store.put_setting(KEY, state)
        return {"ready": not reasons, "reasons": reasons, "runs": runs,
                "barrier": state, "version": __version__}
