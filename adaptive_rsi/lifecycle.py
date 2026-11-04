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

# 屏障记录是 settings 表里的一条 JSON，可能来自旧版本结构、人工改库或部分失败的
# 写入。lease 协议读取 version/lease/phase；缺任意一个都会 KeyError，非映射记录
# 则在 .get 处 AttributeError，最终变成 server 通用 except 的无诊断 500，且准入被
# 永久锁死。固定语义：无法使用的记录等同于无屏障，与 expired/owner_exited 走同一
# 条 desktop_update_recovered 清理路径（先写审计事件再删除），修复手段不再依赖改库。
_REQUIRED_FIELDS = ("version", "lease", "phase")


def _usable_state(state):
    """Whether a saved barrier record supports the full lease protocol."""
    return (isinstance(state, dict) and
            all(isinstance(state.get(key), str) and state.get(key) for key in _REQUIRED_FIELDS))


def _field(state, key):
    return state.get(key) if isinstance(state, dict) else None


def barrier(runtime):
    with runtime.dispatch_lock:
        state = runtime.store.setting(KEY)
        if not state:
            return state
        reason = None
        if not _usable_state(state):
            reason = "invalid_record"
        else:
            expires = state.get("expires", 0)
            # 无法比较的 expires 同样会让状态接口抛 TypeError；非 installing 阶段视为已过期。
            expired = (state.get("phase") != "installing" and
                       (not isinstance(expires, (int, float)) or expires <= time.time()))
            owner_exited = False
            owner = state.get("owner_pid")
            if state.get("phase") == "installing" and type(owner) is int and owner > 1:
                try:
                    os.kill(owner, 0)
                except OSError as exc:
                    # Permission errors and unknown process state must keep admission closed.
                    owner_exited = exc.errno == errno.ESRCH
            if expired or owner_exited:
                reason = "owner_exited" if owner_exited else "drain_expired"
        if reason:
            with runtime.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM settings WHERE key=?", (KEY,))
                db.execute("INSERT INTO events(kind,message,data,created) VALUES "
                           "('desktop_update_recovered','Desktop update admission recovered',?,?)",
                           (encode({"phase": _field(state, "phase"), "version": _field(state, "version"),
                                    "reason": reason}), time.time()))
            runtime.wake.set()
            return None
        return state


def readiness(runtime, action="status", version=None, lease=None):
    with runtime.dispatch_lock:
        # barrier() 负责把无法使用的记录清理为 None，因此这里的 state 要么为空，
        # 要么已通过 _usable_state 校验：下面直接下标取 version/lease/phase 是安全的。
        state = barrier(runtime)
        if action not in ("status", "prepare", "commit", "cancel", "complete"):
            raise ValueError("Invalid desktop update action")
        if action != "status" and not getattr(runtime.updates, "desktop_managed", False):
            raise ValueError("Only a desktop-owned runtime can be replaced")
        if action == "prepare":
            if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("Invalid desktop update version")
            if state and (state["version"] != version or not isinstance(lease, str) or
                          not hmac.compare_digest(state["lease"], lease)):
                raise ValueError("Another desktop update owns admission")
            if not state:
                state = {"version": version, "lease": secrets.token_hex(24), "phase": "draining", "expires": time.time() + 3600}
            elif state["phase"] == "draining":
                state = {**state, "expires": time.time() + 3600}
            runtime.store.put_setting(KEY, state)
        elif action in ("cancel", "complete") and state:
            if not isinstance(lease, str) or not hmac.compare_digest(state["lease"], lease):
                raise ValueError("Desktop update lease changed")
            if action == "complete" and state["version"] != __version__:
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
            if not state or not isinstance(lease, str) or not hmac.compare_digest(state["lease"], lease):
                raise ValueError("Desktop update lease changed")
            if reasons:
                raise ValueError("Runtime still has active operations")
            state = {**state, "phase": "installing",
                     "owner_pid": getattr(runtime.updates, "desktop_owner_pid", None), "committed": time.time()}
            runtime.store.put_setting(KEY, state)
        return {"ready": not reasons, "reasons": reasons, "runs": runs,
                "barrier": state, "version": __version__}
