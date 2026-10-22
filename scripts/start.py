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
    log_path = state / "server.log"
    with log_path.open("ab") as log:
        log_path.chmod(0o600)
        subprocess.Popen([sys.executable, "-m", "adaptive_rsi", "--state-dir", str(state), "serve"],
                         cwd=root, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for attempt in range(40):
        time.sleep(0.2)
        session = live_session()
        if session:
            break
    if not session:
        raise SystemExit("Runtime did not start. See " + str(log_path))
print(session["url"])
if "--no-open" not in sys.argv:
    webbrowser.open(session["url"])
