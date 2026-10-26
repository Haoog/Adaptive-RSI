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
CREDENTIAL_MODES = {"keychain", "session", "file"}
MAX_RETIRED_REFERENCES = 4096
_UNSET_INHERIT = object()


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
            items.append({**provider, "builtin": builtin, "credential_status": "opencode" if provider["id"] == "opencode" else "login" if provider["id"] == "openai"
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

    def _credential_in_use(self, reference, mode):
        """当前配置和未结束运行的封存配置都可能继续使用旧凭据。"""
        configs = [self.runtime.config()]
        for row in self.store.query("SELECT id FROM projects"):
            configs.append(self.runtime.config(row["id"]))
        for row in self.store.query("SELECT config FROM runs WHERE ended IS NULL"):
            try:
                config = json.loads(row["config"])
            except (ValueError, TypeError):
                return True
            if (not isinstance(config, dict) or not isinstance(config.get("providers"), list)
                    or any(not isinstance(provider, dict) for provider in config["providers"])):
                # 无法证明旧快照不再引用凭据时，保守保留，待快照修复或运行结束后重试。
                return True
            configs.append(config)
        for config in configs:
            for provider in [*BUILTINS, *config["providers"]]:
                if provider.get("secret_ref") == reference and provider.get("credential") == mode:
                    return True
        return False

    def _pending_credentials(self):
        try:
            pending = self.store.setting("credential-cleanup-pending", {})
        except (ValueError, TypeError):
            raise CenterError("invalid_credential_registry") from None
        if not isinstance(pending, dict) or len(pending) > MAX_RETIRED_REFERENCES:
            raise CenterError("invalid_credential_registry")
        normalized = {}
        for reference, modes in pending.items():
            try:
                Credentials.check_reference(reference)
            except CredentialError:
                raise CenterError("invalid_credential_registry") from None
            # 兼容最初的 reference -> mode 格式，新的条目保留同一引用的各存储后端。
            modes = [modes] if isinstance(modes, str) else modes
            if (not isinstance(modes, list) or not 1 <= len(modes) <= len(CREDENTIAL_MODES)
                    or any(not isinstance(mode, str) or mode not in CREDENTIAL_MODES for mode in modes)):
                raise CenterError("invalid_credential_registry")
            normalized[reference] = sorted(set(modes))
        return normalized

    def _retire_credential(self, provider):
        if provider and provider.get("credential") in CREDENTIAL_MODES and provider.get("secret_ref"):
            # 在创建凭据或改变配置前持久登记；不保存密钥材料。
            pending = self._pending_credentials()
            reference, mode = provider["secret_ref"], provider["credential"]
            Credentials.check_reference(reference)
            if reference not in pending and len(pending) >= MAX_RETIRED_REFERENCES:
                raise CenterError("credential_registry_full")
            pending[reference] = sorted(set(pending.get(reference, []) + [mode]))
            self.store.put_setting("credential-cleanup-pending", pending)

    def _forget_credential(self, provider):
        pending = self._pending_credentials()
        reference, mode = provider["secret_ref"], provider["credential"]
        modes = pending.get(reference, [])
        if mode in modes:
            modes.remove(mode)
            if not modes:
                del pending[reference]
            self.store.put_setting("credential-cleanup-pending", pending)

    def cleanup_credentials(self):
        """重试清理已登记且没有当前配置或未结束运行引用的凭据。"""
        with self.runtime.dispatch_lock:
            if self.runtime.updates.is_busy():
                return
            pending = self._pending_credentials()
            for reference, modes in list(pending.items()):
                for mode in list(modes):
                    if self._credential_in_use(reference, mode):
                        continue
                    try:
                        self.credentials.delete(reference, mode)
                    except (CredentialError, OSError):
                        self.store.event("credential_cleanup_failed",
                                         "Retired provider credential could not be deleted; cleanup will retry")
                    else:
                        modes.remove(mode)
                if not modes:
                    del pending[reference]
            self.store.put_setting("credential-cleanup-pending", pending)

    def save_provider(self, data, authentication, project=None):
        with self.runtime.dispatch_lock:
            return self._save_provider(data, authentication, project)

    def _save_provider(self, data, authentication, project=None):
        if not isinstance(data, dict) or not isinstance(authentication, dict):
            raise CenterError("invalid_provider")
        if set(data) - {"id", "name", "base_url", "protocol", "env_key"} or set(authentication) - {"mode", "value"}:
            raise CenterError("invalid_provider")
        self._pending_credentials()
        config = self.runtime.config(project)
        provider = {**data}
        provider.setdefault("id", "provider_" + uid()[:10])
        old = next((p for p in config["providers"] if p["id"] == provider["id"]), None)
        mode = authentication.get("mode", "none")
        if mode not in ["keychain", "session", "file", "env", "none"]:
            raise CenterError("invalid_mode")
        provider["credential"] = mode
        provider["env_key"] = provider.get("env_key", "") if mode == "env" else ""
        secret = authentication.get("value", "")
        if not isinstance(secret, str):
            raise CredentialError("invalid_key")
        fresh = False
        if mode in ["keychain", "session", "file"]:
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
        registered = False
        try:
            if old and (old.get("secret_ref"), old.get("credential")) != (provider.get("secret_ref"), mode):
                self._retire_credential(old)
            if fresh:
                self._retire_credential(provider)
                registered = True
                self.credentials.put(provider["secret_ref"], mode, secret)
            self.runtime.configure({"providers": providers}, project)
        except BaseException:
            if registered:
                try:
                    # configure 可能在配置已提交、事件写入失败时抛出；先证明新密钥未被引用。
                    if not self.runtime.updates.is_busy() and not self._credential_in_use(provider["secret_ref"], mode):
                        self.credentials.delete(provider["secret_ref"], mode)
                        self._forget_credential(provider)
                except Exception:
                    # 持久意向保留供维护重试，不以回滚故障覆盖原始异常。
                    pass
            raise
        if fresh:
            self._forget_credential(provider)
        if old and self.signature(old) != self.signature(provider):
            previous = self.directory(old, project)
            preferences = [{k: v for k, v in model.items() if k in ["id", "name", "name_override", "enabled", "manual", "favorite", "unit_price"]}
                           for model in previous["models"]]
            for model in preferences:
                model["missing"] = not model.get("manual", False)
            self.store.put_setting(self.key(provider, project), {"models": preferences, "fetched": None, "truncated": False})
        self.cleanup_credentials()
        return {"id": provider["id"], "saved": True}

    def remove_provider(self, provider_id, project=None):
        with self.runtime.dispatch_lock:
            return self._remove_provider(provider_id, project)

    def _remove_provider(self, provider_id, project=None):
        self._pending_credentials()
        config = self.runtime.config(project)
        removed = self.provider(provider_id, project)
        if provider_id in {p["id"] for p in BUILTINS}:
            raise CenterError("builtin_provider")
        providers = [p for p in config["providers"] if p["id"] != provider_id]
        self._retire_credential(removed)
        try:
            self.runtime.configure({"providers": providers}, project)
        except ValueError:
            raise CenterError("provider_in_use") from None
        self.cleanup_credentials()
        return {"removed": True}

    @staticmethod
    def _has_custom_name(model):
        # 旧目录没有标记；仅将手动模型中不同于 ID 的名称视为用户名称。
        return model.get("name_override", bool(model.get("manual") and model.get("name")
                                               and model["name"] != model["id"]))

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
                custom_name = self._has_custom_name(old)
                models.append({**model, "enabled": old.get("enabled", model.get("kind") not in NON_AGENT),
                               "manual": old.get("manual", False), "missing": False, "name_override": custom_name,
                               **({"name": old["name"]} if custom_name else {}),
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
        if "name" in data and (not isinstance(data["name"], str)
                               or len(data["name"].strip()) > 160 and data["name"].strip() != data["id"]):
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
            if "name" in data:
                name = data["name"].strip()
                model.update(name=name or data["id"], name_override=bool(name and name != data["id"]))
            model.update({k: v for k, v in data.items() if k in ["enabled", "favorite", "unit_price"]})
            if data.get("manual"):
                model.update(manual=True, missing=False)
            self.store.put_setting(self.key(provider, project), directory)
        return {"saved": True}

    def resume_provider(self, provider_id, project=None):
        self.provider(provider_id, project)
        ProviderHealth(self.store).reset(self.runtime.config(project), provider_id, project or "model-center")
        self.store.event("provider_resumed", "Provider resumed by user", project, data={"provider": provider_id})
        return {"saved": True, "resumed_tasks": self.runtime.resume_provider_tasks(provider_id, project)}

    def save_routes(self, routes, project=None, inherit=_UNSET_INHERIT):
        with self.runtime.dispatch_lock:
            return self._save_routes(routes, project, inherit)

    def _save_routes(self, routes, project, inherit):
        global_models = self.runtime.config()["models"]
        if not isinstance(routes, dict) or set(routes) != set(global_models):
            raise CenterError("invalid_routes")
        if inherit is not _UNSET_INHERIT:
            if (not project or not isinstance(inherit, list)
                    or any(not isinstance(role, str) or role not in global_models for role in inherit)):
                raise CenterError("invalid_routes")
            # 在同一配置锁内解析继承意图，旧草稿值不能成为项目覆盖。
            routes = {**routes, **{role: copy.deepcopy(global_models[role]) for role in inherit}}
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
        return {"saved": True, "models": self.runtime.config(project)["models"]}

    def probe(self, provider_id, model_id, effort="default", project=None):
        config = self.runtime.config(project)
        route = {"provider": provider_id, "model": model_id, "effort": effort, "fallback": None}
        validate(merge(config, {"models": {"implementation": route}}))
        provider = self.provider(provider_id, project)
        signature = self.signature(provider)
        config = copy.deepcopy(config)
        config["models"]["probe"] = route
        run = "model-test-" + uid()
        with self.runtime.dispatch_lock:
            if self.runtime.updates.is_busy():
                raise CenterError("update_in_progress")
            self.busy += 1
        try:
            with tempfile.TemporaryDirectory(prefix="rsi-model-test-") as workspace:
                expected = "RSI_OK"
                prompt = 'Call finish with {"summary":"RSI_OK"}. Do not read files or run commands.'
                if provider.get("protocol", "codex") in ["codex", "codex_login", "opencode"]:
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
            try:
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
            except Exception:
                # 失败记录写入不成功时，绝不用目录更新错误掩盖原始探测错误。
                pass
            raise CenterError(code) from None
        finally:
            with self.runtime.dispatch_lock:
                self.busy -= 1
