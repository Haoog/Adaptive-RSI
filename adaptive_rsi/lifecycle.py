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
# 永久锁死。固定语义：无法使用的记录等同于无屏障，与 expired/owner_exited/
# install_expired 走同一条 desktop_update_recovered 清理路径（先写审计事件再删除），
# 修复手段不再依赖改库。
_REQUIRED_FIELDS = ("version", "lease", "phase")

# installing 阶段不像 draining 那样依赖 expires：commit 之后安装器崩溃、挂起，或 PID
# 被无关进程复用时，os.kill 仍会成功，屏障就再也没有清除路径，全部运行准入永久关闭。
# 因此 installing 记录以 commit 时间为锚点保留同样的硬上界；属主身份可核验时先按
# 身份不匹配清理（owner_exited），否则到点按 install_expired 清理。窗口内再次 commit
# （持有租约）会刷新 committed，供确实更长的安装使用。
INSTALL_WINDOW = 3600


def _process_start(pid):
    """Stable per-process marker, so a reused PID is not mistaken for the installer.

    Linux exposes it as field 22 of /proc/<pid>/stat. Platforms without procfs
    (notably macOS) return None; there the bounded install window is the only
    recovery path, so identity can only ever make recovery earlier, never later.
    """
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            raw = handle.read()
        return raw[raw.rindex(b")") + 2:].split()[19].decode("ascii")
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None


def _usable_state(state):
    """Whether a saved barrier record supports the full lease protocol."""
    return (isinstance(state, dict) and
            all(isinstance(state.get(key), str) and state.get(key) for key in _REQUIRED_FIELDS))


def _field(state, key):
    return state.get(key) if isinstance(state, dict) else None


def _matches(stored, lease):
    return (isinstance(stored, str) and isinstance(lease, str) and stored.isascii() and lease.isascii() and
            hmac.compare_digest(stored, lease))


def barrier(runtime):
    with runtime.dispatch_lock:
        state = runtime.store.setting(KEY)
        if not state:
            return state
        reason = None
        if not _usable_state(state):
            reason = "invalid_record"
        else:
            phase = state.get("phase")
            expires = state.get("expires", 0)
            # 无法比较的 expires 同样会让状态接口抛 TypeError；非 installing 阶段视为已过期。
            expired = (phase != "installing" and
                       (not isinstance(expires, (int, float)) or expires <= time.time()))
            owner_exited = install_expired = False
            owner = state.get("owner_pid")
            if phase == "installing":
                # 缺乏可信 committed 的旧记录没有时间锚点，仍只能由持有租约的
                # cancel/complete 清理；新记录一律受硬上界约束。
                committed = state.get("committed")
                if type(committed) in (int, float):
                    install_expired = committed + INSTALL_WINDOW <= time.time()
                if type(owner) is int and owner > 1:
                    try:
                        os.kill(owner, 0)
                    except OSError as exc:
                        # Permission errors and unknown process state must keep admission closed.
                        owner_exited = exc.errno == errno.ESRCH
                    else:
                        # 存活但启动标识不同 = PID 已被无关进程复用；无法核验
                        # 身份（非 procfs 平台或旧记录）时不提前清理。
                        recorded, current = state.get("owner_start"), _process_start(owner)
                        if isinstance(recorded, str) and isinstance(current, str) and recorded != current:
                            owner_exited = True
            if expired or owner_exited or install_expired:
                reason = "owner_exited" if owner_exited else ("install_expired" if install_expired else "drain_expired")
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
        # 要么已通过 _usable_state 校验；租约比较仍用 _matches 处理缺失/非 ASCII 输入。
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
            started = _process_start(owner)
            state = {**state, "phase": "installing", "owner_pid": owner, "committed": time.time(),
                     "owner_start": started}
            runtime.store.put_setting(KEY, state)
        return {"ready": not reasons, "reasons": reasons, "runs": runs,
                "barrier": state, "version": __version__}
