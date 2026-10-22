"""桌面父进程通过私有管道取得会话；只关闭自己启动的运行器。"""
import json
import os
import signal
import sys
import threading
import urllib.parse
import urllib.request

from .runtime import Runtime
from .server import Server
from .store import Store
from .updates import atomic_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


def run(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = existing_session(root)
    if session:
        print(json.dumps({**session, "owned": False}), flush=True)
        sys.stdin.read()
        return
    runtime = Runtime(Store(root))
    runtime.updates.desktop_managed = True
    server = None
    try:
        runtime.start(paused=True)
        server = Server(runtime)
        session = {"origin": server.origin, "local_origin": server.local_origin,
                   "cookie": server.cookie, "token": server.token, "csrf": server.csrf,
                   "url": server.url, "pid": os.getpid()}
        atomic_json(root / "server.json", session)
        print(json.dumps({**session, "owned": True}), flush=True)

        def stop(*unused):
            runtime.stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

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
