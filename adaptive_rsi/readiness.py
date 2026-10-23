"""Read-only preflight. Detection is deliberately separate from execution proof."""
import os
import platform
import shutil
import time
from pathlib import Path

from .adapters import detect
from .config import BUILTIN_PROVIDERS, compatibility
from .agent import CodexAgent
from .process import Sandbox, run_process


def budget(store, project, config, required=1, run=None):
    # 保留旧调用方的数据结构；用量只记录，不参与准入判断。
    return {"ready": True, "remaining": None, "minimum": required,
            "unit": "recorded_usage", "estimate": False}


def inspect(runtime, project, config=None, workspace=None, required=4, run=None):
    config = config or runtime.config(project["id"])
    root = Path(workspace or project["path"])
    modules = detect(root)
    checks = config["checks"] or modules["checks"]
    def item(code, ready, detail, action=None, reason=None, **fields):
        value = {"code": code, "ready": bool(ready), "detail": detail, **fields}
        if action:
            value["action"] = action
        if reason:
            value["reason"] = reason
        return value

    items = [item("checks", bool(checks), "Project checks are configured." if checks else
                  "No project checks are configured; set fixed checks before running tasks.", None if checks else "settings",
                  reason=None if checks else "checks_missing")]
    try:
        backend = Sandbox(runtime.store.root, config).backend()
        items.append(item("sandbox", True, "Execution backend is available: " + backend + ".", backend=backend))
        if backend == "seatbelt":
            missing = sorted({c["argv"][0] for c in checks + config["environment"]["setup"]
                              if not shutil.which(c["argv"][0]) and not (root / c.get("cwd", ".") / c["argv"][0]).is_file()})
            items.append(item("tools", not missing, "Missing executable(s): " + ", ".join(missing) if missing else
                               "Required executables are available.", "install" if missing else None,
                               reason="tools_missing" if missing else None, missing=missing))
    except RuntimeError as exc:
        items.append(item("sandbox", False, str(exc) or "No supported execution backend is available.", "install",
                          reason="sandbox_unavailable"))
    # Seed contents are frozen and checked again before paid calls in the pipeline.
    missing_seeds = sorted(p for p in config["environment"]["seed_paths"] if not (Path(project["path"]) / p).is_dir())
    items.append(item("dependencies", not missing_seeds,
                      "Missing dependency seed path(s): " + ", ".join(missing_seeds) if missing_seeds else
                      "Configured dependency paths are available.", "settings" if missing_seeds else None,
                      reason="dependencies_missing" if missing_seeds else None, missing=missing_seeds))
    providers = {p["id"]: p for p in BUILTIN_PROVIDERS + config["providers"]}
    opencode_checks = {}

    def opencode_status():
        container = platform.system() != "Darwin" or config["sandbox"]["backend"] == "docker"
        binary = shutil.which("opencode")
        image = config["sandbox"]["worker_image"]
        key = ("docker:" + image) if container else binary
        if not key:
            return None
        if key not in opencode_checks:
            from .opencode import OpenCodeCompatibilityError, detect_opencode
            try:
                if not container and binary:
                    opencode_checks[key] = ("ready", detect_opencode(binary))
                elif shutil.which("docker"):
                    from .cli_worker import clean_environment
                    inspected = run_process(["docker", "image", "inspect", image], timeout=10)
                    if inspected["exit_code"] or inspected["error"]:
                        return None
                    opencode_checks[key] = ("ready", detect_opencode(
                        "opencode", argv=["docker", "run", "--rm", image, "opencode", "--version"],
                        env=clean_environment()))
                else:
                    return None
            except OpenCodeCompatibilityError as exc:
                opencode_checks[key] = ("failed", exc)
        return opencode_checks[key]

    def route_diagnostic(route):
        provider_id = route.get("provider", "openai")
        provider = providers.get(provider_id)
        detail = "Model route " + provider_id + "/" + route.get("model", "") + " at effort " + route.get("effort", "default")
        route_fields = {"provider": provider_id, "model": route.get("model"), "effort": route.get("effort")}
        if not provider:
            return False, detail + " references an unknown provider.", "models", "provider_unknown", {**route_fields, "probe": {"state": "unavailable"}}
        from .agent import CodexAgent
        available = getattr(runtime.agent, "available", CodexAgent.available)(provider)
        if not available:
            return False, detail + " is unavailable because its provider runtime is missing.", "install", "provider_runtime_missing", {**route_fields, "probe": {"state": "unavailable"}}
        if provider.get("credential", "env") == "env" and provider.get("env_key"):
            if not os.environ.get(provider["env_key"]):
                return False, detail + " is missing credential environment variable " + provider["env_key"] + ".", "models", "credentials_missing", {**route_fields, "probe": {"state": "unavailable"}}
        if provider.get("credential") in ["keychain", "session", "file"]:
            from .credentials import Credentials
            try:
                if not Credentials(runtime.store.root).get(provider):
                    return False, detail + " is missing usable credentials.", "models", "credentials_missing", {**route_fields, "probe": {"state": "unavailable"}}
            except Exception as exc:
                return False, detail + " is missing usable credentials (" + str(exc) + ").", "models", "credentials_missing", {**route_fields, "probe": {"state": "unavailable"}}
        from .provider_health import ProviderHealth
        if ProviderHealth(runtime.store).status(config, provider_id, project["id"])["paused"]:
            return False, detail + " is paused after repeated provider failures.", "models", "provider_paused", {**route_fields, "probe": {"state": "unavailable"}}
        opencode = opencode_status() if provider.get("protocol") == "opencode" else None
        if opencode and opencode[0] == "failed":
            exc = opencode[1]
            version = exc.version or "unknown"
            route_fields.update({"opencode_version": version, "compatibility": "unsupported"})
            return (False, detail + " detected OpenCode " + version +
                    "; the command parameters are incompatible. Upgrade Adaptive RSI or temporarily use OpenCode 1.19.3. "
                    "Adaptive RSI does not upgrade or downgrade OpenCode automatically.",
                    "install", exc.code, {**route_fields, "probe": {"state": "unavailable"}})
        if opencode and opencode[0] == "ready":
            capabilities = opencode[1]
            route_fields.update({"opencode_version": capabilities.version, "compatibility": "supported"})
            detail += " Detected OpenCode " + capabilities.version + " with compatible command parameters."
        if provider.get("protocol", "codex") in ["codex", "codex_login", "opencode"] and isinstance(runtime.agent, CodexAgent):
            from .cli_worker import worker_available
            binary = "opencode" if provider.get("protocol") == "opencode" else "codex"
            if not worker_available(config, binary):
                return False, detail + " needs the " + binary + " worker and configured execution image.", "install", "worker_missing", {**route_fields, "probe": {"state": "unavailable"}}
        probe = {"state": "unverified", "effort": route.get("effort", "default")}
        reason = "model_unverified"
        catalog_warning = ""
        try:
            directory = runtime.model_center.directory(provider, project["id"])
            model = next((m for m in directory.get("models", []) if m.get("id") == route["model"]), None)
            if directory.get("fetched") and (not model or model.get("missing")):
                catalog_warning = " The selected model is absent from the latest catalog; verify that the provider exposes it."
                reason = "catalog_missing"
            if model:
                probe = (model.get("probes", {}) or {}).get(route.get("effort"), probe)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        if probe.get("state") == "fail":
            probe_detail = "The last tool test failed for the selected effort."
            reason = "model_probe_failed"
        elif probe.get("state") != "pass":
            probe_detail = "No tool test is recorded for the selected effort; this readiness check made no model call."
        else:
            probe_detail = "The selected effort has a passing tool test."
        return (True, detail + " is configured. " + probe_detail + catalog_warning,
                "models" if probe.get("state") != "pass" or catalog_warning else None, reason,
                {**route_fields, "probe": probe, "signature": runtime.model_center.signature(provider)})

    task_proofs = runtime.store.setting("task-proof-current:" + project["id"], {})
    model_evidence = []
    for role in ["planning", "implementation", "review"]:
        route = config["models"][role]
        primary = route_diagnostic(route)
        fallback = route_diagnostic(route["fallback"]) if route.get("fallback") else None
        selected = primary if primary[0] or not fallback or not fallback[0] else fallback
        ready = primary[0] or bool(fallback and fallback[0])
        detail = selected[1]
        if not primary[0] and fallback and fallback[0]:
            detail = "Primary route unavailable (" + primary[1] + "); fallback is ready. " + fallback[1]
        proof = task_proofs.get(role) if isinstance(task_proofs, dict) else None
        task_verified = bool(proof and proof.get("provider") == selected[4]["provider"] and
                             proof.get("model") == selected[4]["model"] and
                             proof.get("effort") == selected[4]["effort"] and
                             proof.get("signature") == selected[4].get("signature"))
        items.append(item("model", ready, detail, selected[2],
                          reason=selected[3], role=role, provider=selected[4]["provider"], model=selected[4]["model"],
                          effort=selected[4]["effort"], probe=selected[4]["probe"], fallback=route.get("fallback"),
                          **{key: selected[4][key] for key in ("opencode_version", "compatibility")
                             if key in selected[4]}))
        model_evidence.append({"role": role, "provider": selected[4]["provider"], "model": selected[4]["model"],
                               "effort": selected[4]["effort"], "configured": ready,
                               **{key: selected[4][key] for key in ("opencode_version", "compatibility")
                                  if key in selected[4]},
                               "tool_tested": selected[4]["probe"].get("state") == "pass",
                               "task_verified": task_verified})
    usage = budget(runtime.store, project["id"], config, required, run)
    proof = runtime.store.setting("verified-checks:" + project["id"])
    from .gitops import git
    verified = bool(proof and proof["base"] == git(root, "rev-parse", "HEAD") and
                    proof["compatibility"] == compatibility(config))
    return {"ready": all(i["ready"] for i in items), "items": items, "checks": checks,
            "execution_verified": verified, "model_evidence": model_evidence,
            "checked_at": time.time(), "budget": usage}
