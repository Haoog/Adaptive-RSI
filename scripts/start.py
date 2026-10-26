#!/usr/bin/env python3
"""Start a detached local runtime, or reopen the existing authenticated panel."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from adaptive_rsi.cli import default_state
from adaptive_rsi.updates import installed_root

state = default_state().expanduser().resolve()
state.mkdir(mode=0o700, parents=True, exist_ok=True)
state.chmod(0o700)
root = installed_root(state, root)
session_file = state / "server.json"


def live_session():
    try:
        session = json.loads(session_file.read_text())
        request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/state", headers={"Cookie": session["cookie"] + "=" + session["token"]})
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status == 200:
                return session
    except (OSError, ValueError, KeyError):
        return None


session = live_session()
if not session:
    # 两个启动入口同时探测会各自 spawn 一个服务；用非阻塞文件锁串行化启动。
    import fcntl
    launch_lock = (state / "launcher.lock").open("a+")
    try:
        fcntl.flock(launch_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # 另一个启动器正在拉起服务：等它写出会话即可，不再重复 spawn。
        launch_lock.close()
        for attempt in range(200):
            time.sleep(0.2)
            session = live_session()
            if session:
                break
        if not session:
            raise SystemExit("Another launcher is starting the runtime; it did not become ready. See " + str(state / "server.log"))
        print(session["url"])
        if "--no-open" not in sys.argv:
            webbrowser.open(session["url"])
        raise SystemExit(0)
    log_path = state / "server.log"
    with log_path.open("ab") as log:
        log_path.chmod(0o600)
        subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve"],
                         cwd=root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    # 大状态目录的首次启动可能超过 10 秒；等待上限 30 秒。
    for attempt in range(150):
        time.sleep(0.2)
        session = live_session()
        if session:
            break
    launch_lock.close()
    if not session:
        raise SystemExit("Runtime did not start. See " + str(log_path))
print(session["url"])
if "--no-open" not in sys.argv:
    webbrowser.open(session["url"])
