"""桌面父进程通过私有管道取得会话；只关闭自己启动的运行器。"""
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request

from .runtime import Runtime
from .server import Server
from .store import Store
from .updates import atomic_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def installer_owner():
    """The native owner is separate from a onefile engine's bootloader parent."""
    value = os.environ.get("ADAPTIVE_RSI_DESKTOP_OWNER_PID", "")
    if not 1 <= len(value) <= 10 or not value.isascii() or not value.isdigit() or not 1 < int(value) < 2**31:
        return None
    owner = int(value)
    try:
        os.kill(owner, 0)
    except OSError:
        return None
    return owner


def existing_session(root):
    try:
        session = json.loads((root / "server.json").read_text())
        origin = session.get("local_origin", session["origin"])
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment:
            return None
        request = urllib.request.Request(origin + "/api/health", headers={"Cookie": session["cookie"] + "=" + session["token"]})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=2) as response:
            health = json.load(response)
        if health["pid"] != session["pid"]:
            return None
        return session
    except (OSError, ValueError, KeyError, TypeError):
        return None


def drain_before_shutdown(runtime, server):
    """Close the runtime while the HTTP server still answers shutdown-status.

    The status endpoint reads the runtime phase directly, so the listening
    socket must stay open until the drain has finished. Only then may the
    server stop accepting requests.
    """
    completed = False
    try:
        runtime.close()
        completed = True
    finally:
        # Leave one short polling window so the overlay can read phase=completed
        # before the listener disappears; the app exits immediately afterwards.
        if completed:
            time.sleep(0.5)
        server.shutdown()


def run(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = existing_session(root)
    if session:
        print(json.dumps({**session, "owned": False}), flush=True)
        def watch_stdin():
            sys.stdin.read()
        t = threading.Thread(target=watch_stdin, daemon=True)
        t.start()
        marker = root / "server.json"
        while t.is_alive():
            time.sleep(2)
            try:
                current = json.loads(marker.read_text())
                if current.get("token") != session.get("token"):
                    session = current
                    print(json.dumps({**session, "owned": False, "rotated": True}), flush=True)
            except (OSError, ValueError):
                pass
        return
    runtime = Runtime.open(root)
    runtime.updates.desktop_managed = True
    runtime.updates.desktop_owner_pid = installer_owner()
    server = None
    try:
        from .lifecycle import barrier
        runtime.start(paused=not bool(barrier(runtime)))
        server = Server(runtime)
        session = {"origin": server.origin, "local_origin": server.local_origin,
                   "cookie": server.cookie, "token": server.token, "csrf": server.csrf,
                   "url": server.url, "pid": os.getpid()}
        atomic_json(root / "server.json", session)
        print(json.dumps({**session, "owned": True}), flush=True)

        stopping = threading.Event()

        def stop(*unused):
            if stopping.is_set():
                return
            stopping.set()
            runtime.stop.set()
            threading.Thread(target=drain_before_shutdown, args=(runtime, server),
                             daemon=True).start()

        for sig in [signal.SIGTERM, signal.SIGINT]:
            signal.signal(sig, stop)

        def watch_parent():
            sys.stdin.read()
            stop()

        threading.Thread(target=watch_parent, daemon=True).start()
        server.serve_forever(poll_interval=0.2)
    finally:
        if server:
            server.server_close()
        runtime.close()
        marker = root / "server.json"
        try:
            if json.loads(marker.read_text()).get("pid") == os.getpid():
                marker.unlink()
        except (OSError, ValueError):
            pass
