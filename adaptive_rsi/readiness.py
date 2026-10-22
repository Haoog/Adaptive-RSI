"""Read-only preflight. Detection is deliberately separate from execution proof."""
import os
import shutil
import time
from pathlib import Path

from .adapters import detect
from .config import BUILTIN_PROVIDERS, compatibility
from .agent import CodexAgent
from .process import Sandbox


def budget(store, project, config, required=1, run=None):
    # 保留旧调用方的数据结构；用量只记录，不参与准入判断。
    return {"ready": True, "remaining": None, "minimum": required,
            "unit": "recorded_usage", "estimate": False}


def inspect(runtime, project, config=None, workspace=None, required=4, run=None):
    config = config or runtime.config(project["id"])
    root = Path(workspace or project["path"])
    modules = detect(root)
    checks = config["checks"] or modules["checks"]
    items = [{"code": "checks", "ready": bool(checks)}]
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
        from .agent import CodexAgent
        ready = getattr(runtime.agent, "available", CodexAgent.available)(provider)
        if provider.get("credential", "env") == "env" and provider.get("env_key"):
            ready = ready and bool(os.environ.get(provider["env_key"]))
        if provider.get("credential") in ["keychain", "session", "file"]:
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
        def executable(item):
            provider = providers[item.get("provider", "openai")]
            if not route_ready(item):
                return False
            if provider.get("protocol", "codex") in ["codex", "codex_login", "opencode"]:
                from .cli_worker import worker_available
                if isinstance(runtime.agent, CodexAgent):
                    return worker_available(config, "opencode" if provider.get("protocol") == "opencode" else "codex")
            return True
        ready = executable(route) or bool(route.get("fallback") and executable(route["fallback"]))
        items.append({"code": "model", "role": role, "ready": ready})
    usage = budget(runtime.store, project["id"], config, required, run)
    proof = runtime.store.setting("verified-checks:" + project["id"])
    from .gitops import git
    verified = bool(proof and proof["base"] == git(root, "rev-parse", "HEAD") and
                    proof["compatibility"] == compatibility(config))
    return {"ready": all(i["ready"] for i in items), "items": items, "checks": checks,
            "execution_verified": verified, "checked_at": time.time(), "budget": usage}
