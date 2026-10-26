import argparse
import json
import os
import platform
import re
import shutil
import signal
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .process import run_process
from .runtime import Runtime
from .server import Server
from .store import Store


def default_state():
    return Path(os.environ.get("ADAPTIVE_RSI_HOME", str(Path.home() / ".local/share/adaptive-rsi")))


def doctor():
    result = {"python": sys.version.split()[0], "platform": platform.system(), "version": __version__}
    for tool in ["git", "codex", "opencode", "gh", "docker", "sandbox-exec"]:
        result[tool] = shutil.which(tool)
    def opencode_compatibility(capabilities=None, error=None, transport="host"):
        from .opencode import OpenCodeCompatibilityError
        result["opencode_transport"] = transport
        if capabilities:
            result["opencode_version"] = capabilities.version
            result["opencode_compatibility"] = "supported"
            result["opencode_commands"] = {
                "models": "api:/api/model" if capabilities.modern else "legacy:metadata",
                "run": "model#variant" if capabilities.modern else "variant-flag",
                "serve": "v2" if capabilities.modern else "v1",
            }
            return
        result["opencode_version"] = ((error.version or None)
                                      if isinstance(error, OpenCodeCompatibilityError) else None)
        result["opencode_compatibility"] = "unsupported"
        version = result["opencode_version"] or "version unknown"
        result["opencode_diagnostic"] = (
            "Detected OpenCode " + version +
            "; its command parameters are incompatible. Upgrade Adaptive RSI or temporarily use OpenCode 1.19.3. "
            "Adaptive RSI does not upgrade or downgrade OpenCode automatically.")
    if result["opencode"]:
        from .opencode import OpenCodeCompatibilityError, detect_opencode
        try:
            opencode_compatibility(detect_opencode(result["opencode"]))
        except OpenCodeCompatibilityError as exc:
            opencode_compatibility(error=exc)
    if result["codex"]:
        result["codex_authenticated"] = run_process(["codex", "login", "status"], timeout=10)["exit_code"] == 0
    if result["docker"]:
        info = run_process(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
        result["docker_running"] = info["exit_code"] == 0
    if not result["opencode"] and result.get("docker_running"):
        from .config import DEFAULTS
        from .opencode import OpenCodeCompatibilityError, detect_opencode
        image = DEFAULTS["sandbox"]["worker_image"]
        result["opencode_worker"] = image
        inspected = run_process(["docker", "image", "inspect", image], timeout=10)
        result["opencode_worker_available"] = inspected["exit_code"] == 0 and not inspected["error"]
        if result["opencode_worker_available"]:
            try:
                capabilities = detect_opencode(
                    "opencode", argv=[result["docker"], "run", "--rm", image, "opencode", "--version"])
                opencode_compatibility(capabilities, transport="docker")
            except OpenCodeCompatibilityError as exc:
                opencode_compatibility(error=exc, transport="docker")
        else:
            result["opencode_transport"] = "docker"
            result["opencode_compatibility"] = "worker_missing"
            result["opencode_diagnostic"] = (
                "OpenCode CLI is not installed on the host and the configured Docker worker image is unavailable. "
                "Install OpenCode 1.19.3/2.x on the host or build the worker image; Adaptive RSI does not change versions automatically.")
    if result["sandbox-exec"]:
        probe = run_process(["sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", "/usr/bin/true"], timeout=5)
        result["seatbelt_working"] = probe["exit_code"] == 0
    result["models"] = "Codex and native protocol routes validated per invocation; no paid probe or silent substitution"
    result["worker_support"] = "Codex/OpenCode: macOS Seatbelt or isolated Docker workers; native APIs: host-controlled tools with Seatbelt/Docker checks"
    return result


def api(root, path, data=None):
    session = json.loads((root / "server.json").read_text())
    headers = {"Cookie": session["cookie"] + "=" + session["token"]}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Origin": session["origin"], "X-RSI-CSRF": session["csrf"]})
    request = urllib.request.Request(session.get("local_origin", session["origin"]) + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=200 if path == "/api/projects/import" else 20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # 服务端只为固定文案发放用户提示；原样透传，不回显请求字段或异常栈。
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        message = payload.get("error") if isinstance(payload.get("error"), str) else ""
        code = payload.get("error_code") or payload.get("catalog_error")
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) else ""
        if message and code and code not in ("validation_error", "invalid_request"):
            raise RuntimeError(f"{message} ({code})") from None
        if message:
            raise RuntimeError(message) from None
        raise RuntimeError(f"HTTP Error {exc.code}") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="adaptive-rsi", description="Adaptive RSI local autonomous improvement runtime")
    parser.add_argument("--state-dir", type=Path, default=default_state())
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run scheduler and authenticated local control panel")
    serve.add_argument("--port", type=int, default=int(os.environ.get("ADAPTIVE_RSI_PORT", "0")))
    serve.add_argument("--public-url", default=os.environ.get("ADAPTIVE_RSI_PUBLIC_URL"), help="HTTPS origin served by a local reverse proxy")
    serve.add_argument("--open", action="store_true")
    serve.add_argument("--restart-session", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("update-worker", help=argparse.SUPPRESS)
    update = sub.add_parser("update", help="Check or apply an official release")
    update.add_argument("action", choices=["check", "apply"])
    update.add_argument("--confirm", action="store_true")
    imports = sub.add_parser("import", help="Import separate projects from GitHub URLs (paused)")
    imports.add_argument("urls", nargs="+")
    imports.add_argument("--directory")
    sub.add_parser("doctor")
    sub.add_parser("open")
    sub.add_parser("status")
    sub.add_parser("desktop-runtime", help=argparse.SUPPRESS)
    add = sub.add_parser("add")
    add.add_argument("path")
    add.add_argument("--name")
    add.add_argument("--enable", action="store_true")
    goal = sub.add_parser("goal")
    goal.add_argument("project")
    goal.add_argument("text")
    goal.add_argument("--overrides", help="JSON per-task overrides")
    goal.add_argument("--request-id", help="Reuse only when retrying the same submission")
    restore_parser = sub.add_parser("restore", help="Restore a backup into a NEW state directory, paused")
    restore_parser.add_argument("file", type=Path)
    for command in ["pause", "resume", "inspect", "issues", "evolve"]:
        p = sub.add_parser(command)
        p.add_argument("project")
        if command == "inspect":
            p.add_argument("--deep", action="store_true")
    config = sub.add_parser("config")
    config.add_argument("--project")
    config.add_argument("--json", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("task")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("task")
    deliver = sub.add_parser("deliver")
    deliver.add_argument("run")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("candidate")
    revert = sub.add_parser("revert-code")
    revert.add_argument("run")
    suite = sub.add_parser("import-case")
    suite.add_argument("project")
    suite.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(), indent=2, ensure_ascii=False))
            return
        root = args.state_dir.expanduser().resolve()
        if args.command == "restore":
            from .storage_tools import restore
            print(json.dumps(restore(args.file, root)))
            return
        if args.command == "desktop-runtime":
            from .desktop import run
            run(root)
            return
        if args.command == "update-worker":
            from .updates import supervise_restart
            supervise_restart(root)
            return
        if args.command == "serve":
            store = Store(root)
            runtime = Runtime(store)
            session = None
            if args.restart_session:
                session = json.loads((root / "restart-session.json").read_text())
                (root / "restart-session.json").unlink()
                runtime.updates.restore_startup()
            runtime.start()
            try:
                server = Server(runtime, args.port, session=session, public_url=args.public_url)
                session = {"origin": server.origin, "local_origin": server.local_origin, "public_url": server.origin if server.origin != server.local_origin else None, "cookie": server.cookie, "token": server.token, "csrf": server.csrf, "url": server.url, "pid": os.getpid()}
                from .updates import atomic_json
                session_file = root / "server.json"
                atomic_json(session_file, session)
                print("Adaptive RSI " + __version__ + "\n" + server.url + "\nKeep this process running. Ctrl+C stops workers safely.", flush=True)
                if args.open:
                    webbrowser.open(server.url)
                def shutdown(*unused):
                    runtime.stop.set()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                runtime.updates.shutdown = shutdown
                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
                server.serve_forever(poll_interval=0.3)
                server.server_close()
            finally:
                runtime.close()
                session_file = root / "server.json"
                if session_file.exists() and json.loads(session_file.read_text()).get("pid") == os.getpid():
                    session_file.unlink(missing_ok=True)
            return
        if args.command == "import":
            results = []
            for url in args.urls:
                results.append({"url": url, **api(root, "/api/projects/import", {"url": url, "directory": args.directory})})
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return
        if args.command == "update":
            if args.action == "apply" and not args.confirm:
                raise ValueError("Use update apply --confirm to install and restart when idle")
            print(json.dumps(api(root, "/api/updates/" + args.action, {"confirm": args.confirm}), ensure_ascii=False))
            return
        if args.command == "open":
            session = json.loads((root / "server.json").read_text())
            api(root, "/api/state")
            webbrowser.open(session["url"])
            print(session["url"])
            return
        if args.command == "status":
            state = api(root, "/api/state")
            print(json.dumps({k: state[k] for k in ["version", "projects", "tasks", "runs", "usage"]}, ensure_ascii=False, indent=2))
            return
        mapping = {
            "add": ("/api/projects", lambda: {"path": str(Path(args.path).expanduser().resolve()), "name": args.name, "enabled": args.enable}),
            "goal": ("/api/goal", lambda: {"project": args.project, "text": args.text, "overrides": json.loads(args.overrides) if args.overrides else {}, "request_id": args.request_id}),
            "pause": ("/api/pause", lambda: {"project": args.project, "paused": True}),
            "resume": ("/api/pause", lambda: {"project": args.project, "paused": False}),
            "inspect": ("/api/request", lambda: {"project": args.project, "kind": "deep" if args.deep else "light"}),
            "issues": ("/api/request", lambda: {"project": args.project, "kind": "issues"}),
            "evolve": ("/api/request", lambda: {"project": args.project, "kind": "evolve"}),
            "config": ("/api/config", lambda: {"project": args.project, "values": json.loads(args.json)}),
            "retry": ("/api/retry", lambda: {"task": args.task}),
            "cancel": ("/api/cancel", lambda: {"task": args.task}),
            "deliver": ("/api/deliver", lambda: {"run": args.run}),
            "rollback": ("/api/rollback", lambda: {"candidate": args.candidate}),
            "revert-code": ("/api/revert-code", lambda: {"run": args.run}),
            "import-case": ("/api/cases", lambda: {"project": args.project, "manifest": json.loads(args.file.read_text())}),
        }
        path, body = mapping[args.command]
        print(json.dumps(api(root, path, body()), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print("adaptive-rsi: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
