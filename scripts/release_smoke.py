#!/usr/bin/env python3
"""Exercise built artifacts with a real venv and a supervised local service restart."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
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


def write_proof(directory, package_version, source_sha, source_tree, workflow_ref, workflow_source_sha=None):
    """Record the exact source tree and bytes exercised by release smoke."""
    if not re.fullmatch(r"[a-f0-9]{40}", source_sha or ""):
        raise ValueError("Release proof requires an exact source SHA")
    if not re.fullmatch(r"[a-f0-9]{40}", source_tree or ""):
        raise ValueError("Release proof requires an exact source tree")
    if not workflow_ref or "@refs/" not in workflow_ref:
        raise ValueError("Release proof requires a trusted workflow reference")
    if workflow_source_sha is not None and not re.fullmatch(r"[a-f0-9]{40}", workflow_source_sha):
        raise ValueError("Release proof requires an exact workflow source SHA")
    directory = Path(directory)
    files = {path.name: path for path in directory.iterdir()
             if path.is_file() and not path.is_symlink() and path.name != "release-proof.json"}
    if not files:
        raise ValueError("Release proof requires built assets")
    proof = {
        "format": 1,
        "version": package_version,
        "source_sha": source_sha,
        "source_tree": source_tree,
        "workflow_ref": workflow_ref,
        "artifacts": {name: hashlib.sha256(files[name].read_bytes()).hexdigest() for name in sorted(files)},
    }
    if workflow_source_sha is not None:
        proof["workflow_source_sha"] = workflow_source_sha
    destination = directory / "release-proof.json"
    destination.write_text(json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return destination


class NoMetadataRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def latest_release():
    # Credentials belong only to this fixed API request; asset downloads stay anonymous.
    headers = {"User-Agent": "Adaptive-RSI-release-smoke", "Accept": "application/vnd.github+json"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    opener = urllib.request.build_opener(NoMetadataRedirect())
    # runner 偶发的 DNS/网络抖动不应当作发版失败；短暂退避后重试。
    data = None
    for attempt in range(3):
        request = urllib.request.Request(RELEASE_API, headers=headers)
        try:
            with opener.open(request, timeout=20) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            if exc.code not in (403, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2)
        except urllib.error.URLError:
            if attempt == 2:
                raise
            time.sleep(2)
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
        command(python, "-I", "-c", '''
import json, tempfile, threading, urllib.request
from pathlib import Path
from adaptive_rsi.store import Store
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
with tempfile.TemporaryDirectory() as folder:
    runtime = Runtime(Store(Path(folder) / "state"), agent=object())
    server = Server(runtime)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        request = urllib.request.Request(server.local_origin + "/api/contract",
            headers={"Cookie": server.cookie + "=" + server.token})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["state_schema"]["type"] == "object"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        runtime.close()
''', cwd=temp)
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
        proof_values = [os.environ.get("RSI_SOURCE_SHA"), os.environ.get("RSI_SOURCE_TREE"),
                        os.environ.get("RSI_WORKFLOW_REF"), os.environ.get("RSI_WORKFLOW_SOURCE_SHA")]
        if any(proof_values) and not all(proof_values):
            raise ValueError("Release proof metadata must be provided together")
        proof = write_proof(ROOT / "dist", __version__, *proof_values) if all(proof_values) else None
        print(json.dumps({"version": __version__, "zip_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                          "wheel_install": True, "wheel_upgrade_from_official_release": bool(previous),
                          "zip_supervised_startup": True, "state_preserved": True,
                          "release_proof": proof.name if proof else None}))


if __name__ == "__main__":
    main()
