import argparse
import json
import os
import platform
import shutil
import signal
import sys
import threading
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
    for tool in ["git", "codex", "gh", "docker", "sandbox-exec"]:
        result[tool] = shutil.which(tool)
    if result["codex"]:
        result["codex_authenticated"] = run_process(["codex", "login", "status"], timeout=10)["exit_code"] == 0
    if result["docker"]:
        info = run_process(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=8)
        result["docker_running"] = info["exit_code"] == 0
    if result["sandbox-exec"]:
        probe = run_process(["sandbox-exec", "-p", "(version 1)(allow default)(deny network*)", "/usr/bin/true"], timeout=5)
        result["seatbelt_working"] = probe["exit_code"] == 0
    result["models"] = "Validated per invocation by Codex; no paid probe or silent substitution"
    result["worker_support"] = "macOS Seatbelt; other platforms currently analysis/config only"
    return result


def api(root, path, data=None):
    session = json.loads((root / "server.json").read_text())
    headers = {"Cookie": session["cookie"] + "=" + session["token"]}
    if data is not None:
        headers.update({"Content-Type": "application/json", "Origin": session["origin"], "X-RSI-CSRF": session["csrf"]})
    request = urllib.request.Request(session["origin"] + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="adaptive-rsi", description="Adaptive RSI local autonomous improvement runtime")
    parser.add_argument("--state-dir", type=Path, default=default_state())
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run scheduler and authenticated local control panel")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--open", action="store_true")
    sub.add_parser("doctor")
    sub.add_parser("open")
    sub.add_parser("status")
    add = sub.add_parser("add")
    add.add_argument("path")
    add.add_argument("--name")
    add.add_argument("--enable", action="store_true")
    goal = sub.add_parser("goal")
    goal.add_argument("project")
    goal.add_argument("text")
    goal.add_argument("--overrides", help="JSON per-task overrides")
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
        if args.command == "serve":
            store = Store(root)
            runtime = Runtime(store)
            runtime.start()
            try:
                server = Server(runtime, args.port)
                session = {"origin": server.origin, "cookie": server.cookie, "token": server.token, "csrf": server.csrf, "url": server.url, "pid": os.getpid()}
                session_file = root / "server.json"
                session_file.write_text(json.dumps(session))
                session_file.chmod(0o600)
                print("Adaptive RSI " + __version__ + "\n" + server.url + "\nKeep this process running. Ctrl+C stops workers safely.", flush=True)
                if args.open:
                    webbrowser.open(server.url)
                def shutdown(*unused):
                    runtime.stop.set()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
                server.serve_forever(poll_interval=0.3)
                server.server_close()
            finally:
                runtime.close()
                (root / "server.json").unlink(missing_ok=True)
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
            "goal": ("/api/goal", lambda: {"project": args.project, "text": args.text, "overrides": json.loads(args.overrides) if args.overrides else {}}),
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
