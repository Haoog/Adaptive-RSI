"""管理供应商配置、持久模型目录和用户主动发起的能力测试。"""
import copy
import json
import platform
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .agent import CodexAgent
from .catalog import CatalogError, list_models
from .config import BUILTIN_PROVIDERS as BUILTINS, fingerprint, merge, validate
from .credentials import CredentialError, Credentials
from .store import encode, uid
from .provider_health import ProviderHealth, error_code


NON_AGENT = {"embedding", "embeddings", "image", "audio", "rerank"}


class CenterError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("Model center: " + code)


class ModelCenter:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.credentials = Credentials(self.store.root)
        self.lock = threading.RLock()
        self.busy = 0

    def provider(self, provider_id, project=None):
        config = self.runtime.config(project)
        provider = next((p for p in [*BUILTINS, *config["providers"]] if p["id"] == provider_id), None)
        if not provider:
            raise CenterError("unknown_provider")
        return provider

    @staticmethod
    def signature(provider):
        return fingerprint({k: v for k, v in provider.items() if k != "name"})

    def key(self, provider, project=None):
        return "model-center:" + (project or "global") + ":" + provider["id"] + ":" + self.signature(provider)

    def directory(self, provider, project=None):
        value = self.store.setting(self.key(provider, project))
        if value is None and project:
            global_provider = next((p for p in [*BUILTINS, *self.runtime.config()["providers"]] if p["id"] == provider["id"]), None)
            if global_provider and self.signature(global_provider) == self.signature(provider):
                value = self.store.setting(self.key(provider))
        return copy.deepcopy(value or {"models": [], "fetched": None, "truncated": False})

    def snapshot(self, project=None):
        config = self.runtime.config(project)
        items = []
        for provider in [*BUILTINS, *config["providers"]]:
            directory = self.directory(provider, project)
            known = {m["id"] for m in directory["models"]}
            for route in config["models"].values():
                for item in [route, route.get("fallback")]:
                    if item and item.get("provider", "openai") == provider["id"] and item["model"] not in known:
                        directory["models"].append({"id": item["model"], "name": item["model"], "enabled": True,
                                                    "manual": False, "missing": bool(directory["fetched"]), "configured": True})
                        known.add(item["model"])
            builtin = provider["id"] in {p["id"] for p in BUILTINS}
            items.append({**provider, "builtin": builtin, "credential_status": "login" if provider["id"] == "openai"
                          else self.credentials.status(provider), "catalog": directory,
                          "health": ProviderHealth(self.store).status(config, provider["id"], project or "model-center")})
        task_proofs = {}
        if project:
            for role, route in config["models"].items():
                provider = self.provider(route.get("provider", "openai"), project)
                call = {"role": role, "provider": provider["id"], "model": route["model"], "effort": route["effort"]}
                key = "task-proof:" + project + ":" + fingerprint({"call": call, "provider": self.signature(provider)})
                proof = self.store.setting(key)
                if proof:
                    task_proofs[role] = proof
        override = self.store.one("SELECT config FROM projects WHERE id=?", (project,)) if project else None
        return {"providers": items, "models": config["models"], "task_proofs": task_proofs, "overrides": json.loads(override["config"]).get("models", {}) if override else {},
                "keychain": platform.system() == "Darwin", "project": project,
                "global_models": self.runtime.config()["models"]}

    def save_provider(self, data, authentication, project=None):
        if not isinstance(data, dict) or not isinstance(authentication, dict):
            raise CenterError("invalid_provider")
        if set(data) - {"id", "name", "base_url", "protocol", "env_key"} or set(authentication) - {"mode", "value"}:
            raise CenterError("invalid_provider")
        config = self.runtime.config(project)
        provider = {**data}
        provider.setdefault("id", "provider_" + uid()[:10])
        old = next((p for p in config["providers"] if p["id"] == provider["id"]), None)
        mode = authentication.get("mode", "none")
        if mode not in ["keychain", "session", "env", "none"]:
            raise CenterError("invalid_mode")
        provider["credential"] = mode
        provider["env_key"] = provider.get("env_key", "") if mode == "env" else ""
        secret = authentication.get("value", "")
        if not isinstance(secret, str):
            raise CredentialError("invalid_key")
        fresh = False
        if mode in ["keychain", "session"]:
            if secret:
                provider["secret_ref"] = uuid.uuid4().hex
                fresh = True
            elif old and old.get("credential") == mode and old.get("base_url") == provider.get("base_url") and old.get("protocol", "codex") == provider.get("protocol", "codex"):
                provider["secret_ref"] = old["secret_ref"]
            else:
                raise CredentialError("missing_key")
        providers = [provider if p["id"] == provider["id"] else p for p in config["providers"]]
        if not old:
            providers.append(provider)
        updated = merge(config, {"providers": providers})
        validate(updated)
        if not project:
            for row in self.store.query("SELECT config FROM projects"):
                validate(merge(updated, json.loads(row["config"])))
        if fresh:
            self.credentials.put(provider["secret_ref"], mode, secret)
        try:
            self.runtime.configure({"providers": providers}, project)
        except Exception:
            if fresh:
                self.credentials.delete(provider["secret_ref"], mode)
            raise
        if old and self.signature(old) != self.signature(provider):
            previous = self.directory(old, project)
            preferences = [{k: v for k, v in model.items() if k in ["id", "name", "enabled", "manual", "favorite", "unit_price"]}
                           for model in previous["models"]]
            for model in preferences:
                model["missing"] = not model.get("manual", False)
            self.store.put_setting(self.key(provider, project), {"models": preferences, "fetched": None, "truncated": False})
        return {"id": provider["id"], "saved": True}

    def remove_provider(self, provider_id, project=None):
        config = self.runtime.config(project)
        self.provider(provider_id, project)
        if provider_id in {p["id"] for p in BUILTINS}:
            raise CenterError("builtin_provider")
        providers = [p for p in config["providers"] if p["id"] != provider_id]
        try:
            self.runtime.configure({"providers": providers}, project)
        except ValueError:
            raise CenterError("provider_in_use") from None
        return {"removed": True}

    def fetch(self, provider_id, project=None):
        with self.runtime.dispatch_lock:
            if self.runtime.updates.is_busy():
                raise CenterError("update_in_progress")
        provider = self.provider(provider_id, project)
        signature = self.signature(provider)
        key = self.key(provider, project)
        try:
            secret = None if provider["id"] in {p["id"] for p in BUILTINS} else self.credentials.get(provider)
            result = list_models(provider_id if provider["id"] in {p["id"] for p in BUILTINS} else provider, secret)
        except (CatalogError, CredentialError) as exc:
            with self.runtime.dispatch_lock, self.lock:
                if not self.runtime.updates.is_busy():
                    previous = self.directory(provider, project)
                    previous.update(error=exc.code, attempted=time.time())
                    self.store.put_setting(key, previous)
            raise
        with self.runtime.dispatch_lock, self.lock:
            if self.runtime.updates.is_busy():
                raise CenterError("update_in_progress")
            if self.signature(self.provider(provider_id, project)) != signature:
                raise CenterError("connection_changed")
            previous = self.directory(provider, project)
            before = {m["id"]: m for m in previous["models"]}
            models = []
            for model in result["models"]:
                old = before.pop(model["id"], {})
                models.append({**model, "enabled": old.get("enabled", model.get("kind") not in NON_AGENT), "manual": False, "missing": False,
                               **{k: old[k] for k in ["verified", "probes", "last_attempt", "favorite", "unit_price"] if k in old}})
            models.extend({**model, "missing": not model.get("manual", False)} for model in before.values())
            directory = {"models": models, "fetched": time.time(), "truncated": result["truncated"]}
            self.store.put_setting(key, directory)
        return directory

    def edit_model(self, provider_id, data, project=None):
        if not isinstance(data, dict) or set(data) - {"id", "name", "enabled", "manual", "favorite", "unit_price"} or not re.fullmatch(r"[a-zA-Z0-9._/:-]{1,256}", str(data.get("id", ""))):
            raise CenterError("invalid_model")
        if "enabled" in data and type(data["enabled"]) is not bool or "manual" in data and type(data["manual"]) is not bool:
            raise CenterError("invalid_model")
        if "name" in data and (not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 160):
            raise CenterError("invalid_model")
        if "favorite" in data and type(data["favorite"]) is not bool:
            raise CenterError("invalid_model")
        if "unit_price" in data and data["unit_price"] is not None and (type(data["unit_price"]) not in [int, float] or not 0 <= data["unit_price"] <= 1000000):
            raise CenterError("invalid_model")
        with self.lock:
            provider = self.provider(provider_id, project)
            directory = self.directory(provider, project)
            model = next((m for m in directory["models"] if m["id"] == data["id"]), None)
            if not model:
                if len(directory["models"]) >= 1500:
                    raise CenterError("too_many_models")
                model = {"id": data["id"], "name": data["id"], "enabled": True, "manual": True, "missing": False}
                directory["models"].append(model)
            if model.get("kind") in NON_AGENT and data.get("enabled"):
                raise CenterError("unsupported_model_kind")
            model.update({k: v for k, v in data.items() if k in ["name", "enabled", "favorite", "unit_price"]})
            self.store.put_setting(self.key(provider, project), directory)
        return {"saved": True}

    def resume_provider(self, provider_id, project=None):
        self.provider(provider_id, project)
        ProviderHealth(self.store).reset(self.runtime.config(project), provider_id, project or "model-center")
        self.store.event("provider_resumed", "Provider resumed by user", project, data={"provider": provider_id})
        return {"saved": True}

    def save_routes(self, routes, project=None):
        if not isinstance(routes, dict) or set(routes) != set(self.runtime.config()["models"]):
            raise CenterError("invalid_routes")
        config = self.runtime.config(project)
        validate(merge(config, {"models": routes}))
        if project:
            override = json.loads(self.runtime.project(project)["config"])
            override.pop("models", None)
            global_models = self.runtime.config()["models"]
            differences = {role: route for role, route in routes.items() if route != global_models[role]}
            if differences:
                override["models"] = differences
            validate(merge(self.runtime.config(), override))
            self.store.execute("UPDATE projects SET config=? WHERE id=?", (encode(override), project))
            self.store.event("config", "Model routes updated", project, data={"models": differences})
            self.runtime.wake.set()
        else:
            self.runtime.configure({"models": routes})
        return {"saved": True}

    def probe(self, provider_id, model_id, effort="default", project=None):
        config = self.runtime.config(project)
        route = {"provider": provider_id, "model": model_id, "effort": effort, "fallback": None}
        validate(merge(config, {"models": {"implementation": route}}))
        provider = self.provider(provider_id, project)
        signature = self.signature(provider)
        config = copy.deepcopy(config)
        config["models"]["probe"] = route
        config["limits"]["run_seconds"] = min(60, config["limits"]["run_seconds"])
        config["limits"]["run_calls"] = 1
        run = "model-test-" + uid()
        with self.runtime.dispatch_lock:
            if self.runtime.updates.is_busy():
                raise CenterError("update_in_progress")
            self.busy += 1
        try:
            with tempfile.TemporaryDirectory(prefix="rsi-model-test-") as workspace:
                expected = "RSI_OK"
                prompt = 'Call finish with {"summary":"RSI_OK"}. Do not read files or run commands.'
                if provider.get("protocol", "codex") in ["codex", "codex_login"]:
                    expected = uuid.uuid4().hex
                    (Path(workspace) / "probe.txt").write_text(expected)
                    prompt = 'Read probe.txt using a file-reading tool. Return JSON with summary equal to its exact content. Do not modify files or run other commands.'
                result = CodexAgent(self.store).invoke("probe", prompt,
                                                       workspace, project or "model-center", run, config, protected=[p["path"] for p in self.store.query("SELECT path FROM projects")])
            if result != {"summary": expected}:
                raise CenterError("probe_failed")
            with self.runtime.dispatch_lock, self.lock:
                if self.signature(self.provider(provider_id, project)) != signature:
                    raise CenterError("connection_changed")
                self.edit_model(provider_id, {"id": model_id}, project)
                directory = self.directory(provider, project)
                model = next(m for m in directory["models"] if m["id"] == model_id)
                model["verified"] = {"at": time.time(), "effort": effort, "kind": "structured_tool"}
                model.pop("last_attempt", None)
                model.setdefault("probes", {})[effort] = {**model["verified"], "state": "pass"}
                self.store.put_setting(self.key(provider, project), directory)
            return {"verified": model["verified"], "calls": self.store.query("SELECT state,tokens,seconds FROM calls WHERE run=?", (run,))}
        except Exception as exc:
            code = "default_effort_required" if "requires effort=default" in str(exc) else error_code(exc)
            with self.runtime.dispatch_lock, self.lock:
                if self.signature(self.provider(provider_id, project)) == signature:
                    self.edit_model(provider_id, {"id": model_id}, project)
                    directory = self.directory(provider, project)
                    model = next(m for m in directory["models"] if m["id"] == model_id)
                    proof = {"at": time.time(), "effort": effort, "state": "fail", "code": code}
                    if code in ["budget_exhausted", "provider_paused", "missing_key", "keychain_locked", "keychain_unavailable", "keychain_failed", "default_effort_required"]:
                        model["last_attempt"] = proof
                    else:
                        model.pop("last_attempt", None)
                        model.setdefault("probes", {})[effort] = proof
                        if model.get("verified", {}).get("effort") == effort:
                            model.pop("verified", None)
                    self.store.put_setting(self.key(provider, project), directory)
            raise CenterError(code) from None
        finally:
            with self.runtime.dispatch_lock:
                self.busy -= 1
