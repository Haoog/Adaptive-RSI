#!/usr/bin/env python3
"""守护本机服务和更新后的子进程，供 systemd 持续跟踪。"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adaptive_rsi.cli import default_state


def live_session(state):
    session = json.loads((state / "server.json").read_text())
    request = urllib.request.Request(session.get("local_origin", session["origin"]) + "/api/health",
        headers={"Cookie": session["cookie"] + "=" + session["token"]})
    with urllib.request.urlopen(request, timeout=2) as response:
        if json.load(response).get("pid") != session["pid"]:
            raise ValueError("Runtime process changed")
    return session


def main():
    state = default_state().expanduser().resolve()
    stopped = False

    def shutdown(*unused):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    missing_since = None
    while not stopped:
        try:
            live_session(state)
            missing_since = None
        except (OSError, ValueError, KeyError):
            # 更新期间旧进程退出、新进程接管，给受监督恢复留出时间。
            if missing_since is None:
                missing_since = time.monotonic()
            restarting = (state / "installations/restart.json").exists()
            if not restarting or time.monotonic() - missing_since > 90:
                subprocess.run([sys.executable, str(Path(__file__).with_name("start.py")), "--no-open"], check=True)
                missing_since = None
        time.sleep(2)
    try:
        session = live_session(state)
        os.kill(session["pid"], signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass


if __name__ == "__main__":
    main()
