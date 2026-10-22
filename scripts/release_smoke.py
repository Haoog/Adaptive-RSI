#!/usr/bin/env python3
"""Exercise built artifacts with a real venv and a supervised local service restart."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import venv
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adaptive_rsi import __version__
from adaptive_rsi.store import Store
from adaptive_rsi.updates import RELEASE_API, REPOSITORY, download, extract_bundle, supervise_restart, version


def command(*argv, cwd):
    subprocess.run(list(map(str, argv)), cwd=cwd, check=True, timeout=90)


class NoMetadataRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def latest_release():
    # Credentials belong only to this fixed API request; asset downloads stay anonymous.
    headers = {"User-Agent": "Adaptive-RSI-release-smoke", "Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    request = urllib.request.Request(RELEASE_API, headers=headers)
    opener = urllib.request.build_opener(NoMetadataRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return {}
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Release metadata exceeds size limit")
    return json.loads(data)


def main():
    wheel = list((ROOT / "dist").glob("adaptive_rsi-" + __version__ + "-*.whl"))
    if len(wheel) != 1:
        raise ValueError("Smoke requires exactly one freshly built wheel")
    bundle = ROOT / "dist" / ("adaptive-rsi-" + __version__ + "-plugin.zip")
    checksum = bundle.with_suffix(".zip.sha256").read_text().split()[0]
    with tempfile.TemporaryDirectory(prefix="rsi-release-smoke-") as directory:
        temp = Path(directory).resolve()
        candidate = extract_bundle(bundle.read_bytes(), checksum, temp / "bundle", __version__)
        environment = temp / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        previous = None
        latest = latest_release()
        if latest and version(latest["tag_name"]) < version(__version__):
            asset = next((item for item in latest.get("assets", []) if item["name"].endswith(".whl")), None)
            if not asset:
                raise ValueError("Previous official release lacks a wheel for upgrade verification")
            prefix = "https://github.com/" + REPOSITORY + "/releases/download/"
            if not asset["browser_download_url"].startswith(prefix):
                raise ValueError("Previous wheel belongs to another repository")
            previous = temp / asset["name"]
            previous.write_bytes(download(asset["browser_download_url"], 64 * 1024 * 1024))
            command(python, "-m", "pip", "install", "--no-index", "--no-deps", previous, cwd=temp)
            command(python, "-I", "-c", "from adaptive_rsi.store import Store; s=Store('wheel-state'); s.put_setting('smoke-sentinel', {'preserved': True})", cwd=temp)
        command(python, "-m", "pip", "install", "--no-index", "--no-deps", wheel[0], cwd=temp)
        command(python, "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", wheel[0], cwd=temp)
        code = "from adaptive_rsi import __version__; assert __version__ == " + repr(__version__)
        if previous:
            code += "; from adaptive_rsi.store import Store; assert Store('wheel-state').setting('smoke-sentinel') == {'preserved': True}"
        command(python, "-I", "-c", code, cwd=temp)
        command(python, "-m", "adaptive_rsi", "--help", cwd=temp)
        state = temp / "service-state"
        store = Store(state)
        store.put_setting("config", {"updates": {"mode": "manual"}})
        store.put_setting("smoke-sentinel", {"preserved": True})
        store.put_setting("update-status", {"state": "restarting", "version": __version__})
        installs = state / "installations"
        installs.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = "http://127.0.0.1:" + str(port)
        session = {"origin": origin, "cookie": "rsi_" + str(port), "token": "isolated-smoke-token", "csrf": "isolated-smoke-csrf",
                   "url": origin + "/?token=isolated-smoke-token", "pid": -1}
        (installs / "restart.json").write_text(json.dumps({"candidate": str(candidate), "previous": str(ROOT),
            "target": __version__, "pid": -1, "session": session, "state_schema": 1}))
        children = []
        real_popen = subprocess.Popen
        def track(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        try:
            with patch("adaptive_rsi.updates.subprocess.Popen", side_effect=track):
                supervise_restart(state)
            if store.setting("update-status").get("state") != "installed":
                raise RuntimeError("Exact ZIP failed supervised startup: " + (state / "server.log").read_text()[-6000:])
            assert store.setting("smoke-sentinel") == {"preserved": True}
            request = urllib.request.Request(origin + "/api/health", headers={"Cookie": session["cookie"] + "=isolated-smoke-token"})
            with urllib.request.urlopen(request, timeout=3) as response:
                health = json.load(response)
            assert health["version"] == __version__
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        print(json.dumps({"version": __version__, "zip_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                          "wheel_install": True, "wheel_upgrade_from_official_release": bool(previous),
                          "zip_supervised_startup": True, "state_preserved": True}))


if __name__ == "__main__":
    main()
