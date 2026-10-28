"""Admission barrier for replacement of a desktop-owned runtime."""
import hmac
import re
import secrets
import time

from . import __version__


KEY = "desktop-update-barrier"


def barrier(runtime):
    state = runtime.store.setting(KEY)
    if state and state.get("phase") != "installing" and state.get("expires", 0) <= time.time():
        runtime.store.execute("DELETE FROM settings WHERE key=?", (KEY,))
        runtime.wake.set()
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
            state = {**state, "phase": "installing"}
            runtime.store.put_setting(KEY, state)
        return {"ready": not reasons, "reasons": reasons, "runs": runs,
                "barrier": state, "version": __version__}
