#!/usr/bin/env python3
"""Start a detached local runtime, or reopen the existing authenticated panel."""
import fcntl
import json
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

HEALTH_TIMEOUT = 60


def live_session(session_file, timeout=2):
    try:
        session = json.loads(session_file.read_text())
        request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                return session
    except (OSError, ValueError, KeyError, TypeError):
        return None


def wait_for_session(session_file, deadline):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        session = live_session(session_file, timeout=min(2, remaining))
        if session:
            return session
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def ensure_session(state, runtime_root):
    session_file = state / "server.json"
    session = live_session(session_file)
    if session:
        return session
    deadline = time.monotonic() + HEALTH_TIMEOUT
    # 保持文件存在；删除锁文件会让不同启动器锁住不同 inode。
    with (state / "launcher.lock").open("a+") as launch_lock:
        try:
            fcntl.flock(launch_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            session = wait_for_session(session_file, deadline)
            if not session:
                raise SystemExit("Another launcher is starting the runtime; it did not become ready. See " + str(state / "server.log"))
            return session
        # 初检后另一启动器可能已经完成启动并释放锁，必须持锁复检。
        session = live_session(session_file, timeout=min(2, max(0.001, deadline - time.monotonic())))
        if session:
            return session
        log_path = state / "server.log"
        with log_path.open("ab") as log:
            log_path.chmod(0o600)
            subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve"],
                             cwd=runtime_root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        session = wait_for_session(session_file, deadline)
        if not session:
            raise SystemExit("Runtime did not start. See " + str(log_path))
        return session


def main():
    state = default_state().expanduser().resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    session = ensure_session(state, installed_root(state, root))
    print(session["url"])
    if "--no-open" not in sys.argv:
        webbrowser.open(session["url"])


if __name__ == "__main__":
    main()
