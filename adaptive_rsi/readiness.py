"""Read-only preflight. Detection is deliberately separate from execution proof."""
import datetime
import os
import platform
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import detect
from .config import BUILTIN_PROVIDERS, compatibility
from .process import Sandbox


def budget(store, project, config, required=1, run=None):
    midnight = datetime.datetime.now(ZoneInfo(config["schedule"]["timezone"])).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    used = store.one("SELECT count(*) AS n FROM calls WHERE project=? AND created>=?", (project, midnight))["n"]
    run_used = store.one("SELECT count(*) AS n FROM calls WHERE run=?", (run,))["n"] if run else 0
    remaining = min(config["limits"]["daily_calls"] - used, config["limits"]["run_calls"] - run_used)
    return {"ready": remaining >= required, "remaining": max(0, remaining), "minimum": required,
            "unit": "codex_session_or_http_request", "estimate": True}


def inspect(runtime, project, config=None, workspace=None, required=4, run=None):
    config = config or runtime.config(project["id"])
    root = Path(workspace or project["path"])
    modules = detect(root)
    checks = config["checks"] or modules["checks"]
    items = [{"code": "checks", "ready": bool(checks)}]
    items.append({"code": "worker", "ready": platform.system() == "Darwin" and bool(shutil.which("sandbox-exec"))})
    try:
        backend = Sandbox(runtime.store.root, config).backend()
        items.append({"code": "sandbox", "ready": True, "backend": backend})
        if backend == "seatbelt":
            missing = sorted({c["argv"][0] for c in checks + config["environment"]["setup"]
                              if not shutil.which(c["argv"][0]) and not (root / c.get("cwd", ".") / c["argv"][0]).is_file()})
            items.append({"code": "tools", "ready": not missing, "missing": missing})
    except RuntimeError:
        items.append({"code": "sandbox", "ready": False})
    # Seed contents are frozen and checked again before paid calls in the pipeline.
    items.append({"code": "dependencies", "ready": all((Path(project["path"]) / p).is_dir() for p in config["environment"]["seed_paths"])})
    providers = {p["id"]: p for p in BUILTIN_PROVIDERS + config["providers"]}
    def route_ready(route):
        provider = providers[route.get("provider", "openai")]
        protocol = provider.get("protocol", "codex")
        ready = bool(shutil.which("codex")) if protocol in ["codex", "codex_login"] else True
        if provider.get("credential", "env") == "env" and provider.get("env_key"):
            ready = ready and bool(os.environ.get(provider["env_key"]))
        if provider.get("credential") in ["keychain", "session"]:
            from .credentials import Credentials
            try:
                ready = ready and bool(Credentials(runtime.store.root).get(provider))
            except Exception:
                ready = False
        from .provider_health import ProviderHealth
        ready = ready and not ProviderHealth(runtime.store).status(config, provider["id"], project["id"])["paused"]
        return ready
    for role in ["planning", "implementation", "review"]:
        route = config["models"][role]
        ready = route_ready(route) or bool(route.get("fallback") and route_ready(route["fallback"]))
        items.append({"code": "model", "role": role, "ready": ready})
    usage = budget(runtime.store, project["id"], config, required, run)
    items.append({"code": "budget", **usage})
    proof = runtime.store.setting("verified-checks:" + project["id"])
    from .gitops import git
    verified = bool(proof and proof["base"] == git(root, "rev-parse", "HEAD") and
                    proof["compatibility"] == compatibility(config))
    return {"ready": all(i["ready"] for i in items), "items": items, "checks": checks,
            "execution_verified": verified, "checked_at": time.time(), "budget": usage}
