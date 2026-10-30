import datetime
import json
import os
import platform
import re
import shutil
import time
from pathlib import Path

from .process import Cancelled, Sandbox, capture_cancel, run_process


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
REVIEW_AREAS = ["correctness", "security", "maintainability", "architecture", "tests", "scope"]
VERDICT = {"type": "string", "enum": ["pass", "fail", "insufficient_evidence"]}
SCHEMAS = {
    "probe": object_schema({"summary": TEXT}),
    "analysis": object_schema({"findings": {"type": "array", "items": object_schema({
        "title": TEXT, "body": TEXT, "path": TEXT, "quote": TEXT,
        "acceptance": STRINGS, "priority": {"type": "integer"}})}}),
    "triage": object_schema({"findings": {"type": "array", "items": object_schema({
        "index": {"type": "integer"},
        "decision": {"type": "string", "enum": ["ready", "deferred", "rejected"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reason": TEXT})}}),
    "planning": object_schema({"decision": {"type": "string", "enum": ["ready", "obsolete", "blocked", "deferred", "rejected"]},
                               "reason": TEXT, "scope": {"type": "array", "items": {"type": "string", "description": "A literal repository-relative file or directory path only. No descriptions, colons, globs, or annotations."}}, "acceptance": STRINGS,
                               "value": object_schema({"impact": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                                   "benefit": TEXT, "cost": TEXT, "inaction": TEXT, "evidence": STRINGS}),
                               "risk": {"type": "string", "enum": ["low", "medium", "high"]}}),
    "acceptance": object_schema({"files": {"type": "array", "items": object_schema({"path": TEXT, "content": TEXT})},
        "checks": {"type": "array", "items": object_schema({"name": TEXT, "argv": STRINGS, "criteria": {"type": "array", "items": {"type": "integer"}}})}}),
    "implementation": object_schema({"summary": TEXT}),
    "review": object_schema({"decision": VERDICT, "reason": TEXT,
        "assessments": {"type": "array", "items": object_schema({
            "area": {"type": "string", "enum": REVIEW_AREAS}, "decision": VERDICT, "reason": TEXT})},
        "findings": {"type": "array", "items": object_schema({
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "area": {"type": "string", "enum": REVIEW_AREAS}, "path": TEXT,
            "line": {"type": "integer"}, "title": TEXT, "reason": TEXT, "suggestion": TEXT})}}),
    "evolution": object_schema({"kind": {"type": "string", "enum": ["strategy", "meta_strategy", "adapter"]},
                                "guidance": TEXT, "triggers": STRINGS, "rationale": TEXT}),
}

# Optional for older adapters; production planners are instructed to return it explicitly.
SCHEMAS["planning"]["properties"]["change_kind"] = {"type": "string", "enum": ["behavior", "performance", "refactor", "documentation", "configuration", "ui"]}
SCHEMAS["planning"]["properties"]["performance_threshold"] = object_schema({
    "metric": TEXT, "unit": TEXT, "target": {"type": "number"}, "tolerance": {"type": "number"},
    "direction": {"type": "string", "enum": ["lower", "higher"]}})


def validate_output(value, schema):
    kind = schema["type"]
    types = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}
    if type(value) not in types[kind]:
        raise RuntimeError("Agent returned invalid structured output")
    if "enum" in schema and value not in schema["enum"]:
        raise RuntimeError("Agent returned an unknown decision")
    if kind == "object":
        if not set(schema["required"]).issubset(value) or not set(value).issubset(schema["properties"]):
            raise RuntimeError("Agent output fields do not match the schema")
        for key, item in value.items():
            validate_output(item, schema["properties"][key])
    if kind == "array":
        for item in value:
            validate_output(item, schema["items"])


# 单次请求的输出预算策略（按协议与已知型号家族），永远不超过能力上限。
# 老 opus/claude-2、无版本号 opus 别名，以及最初的 claude-3-opus/sonnet/haiku（含日期后缀与
# 供应商前缀）保持 4096 的历史保守值：这些型号的真实输出上限就是 4096，抬高只会变成 400。
# 明确支持的 4.x opus/sonnet、haiku 4 与 3-5/3-7 家族只在各自上限内提高；其余（含未知型号）
# 停在历史 8k 包络，绝不把整个 Claude 家族盲目放大到 32000。
# 顺序即优先级：更具体的旧 3.0 型号必须排在前面的 claude-3 通配之前，否则会被抬到 8192。
ANTHROPIC_REQUEST_OUTPUT = (
    (r"claude-2|claude-instant", 4096),
    (r"claude-(opus|sonnet)-4", 16384),
    (r"claude-haiku-4", 8192),
    (r"claude-3-(opus|sonnet|haiku)\b", 4096),
    (r"claude-3", 8192),
    (r"claude-opus", 4096),
)
ANTHROPIC_DEFAULT_REQUEST_OUTPUT = 8192

# 能力规则核对日期：只说明本地这些规则最近一次核对官方页面/既有验证的时间，
# 不代表供应商的模型变更日期，也不代表每个型号都在当天重新测试过。
CAPABILITY_RULE_REVIEWED_AT = "2026-10-30"
ANTHROPIC_MODELS_DOC = "https://docs.anthropic.com/en/docs/about-claude/models/overview"
OPENAI_MODELS_DOC = "https://platform.openai.com/docs/models"
GEMINI_MODELS_DOC = "https://ai.google.dev/gemini-api/docs/models"

# 只收录 2026-10-30 现场核对成功的逐型号官方页；日期后缀必须与页面一致，
# 任意变体或未知日期一律落入 heuristic，不能冒充 documented。
# 每条为 (正则, 上下文上限, 输出上限, 官方来源)。顺序即优先级。
CAPABILITY_RULES = (
    (r"(?:^|[/:.])claude-opus-4-5(?:-20251101)?(?:-v\d+:\d+)?$", 200000, 64000,
     "https://platform.claude.com/docs/en/models/opus-4-5/overview"),
    (r"(?:^|[/:.])claude-sonnet-4-5(?:-20250929)?(?:-v\d+:\d+)?$", 200000, 64000,
     "https://platform.claude.com/docs/en/models/sonnet-4-5/overview"),
    (r"(?:^|[/:.])claude-haiku-4-5(?:-20251001)?(?:-v\d+:\d+)?$", 200000, 64000,
     "https://platform.claude.com/docs/en/models/haiku-4-5/overview"),
    (r"(?:^|[/:.])gpt-5$", 400000, 128000, "https://developers.openai.com/api/docs/models/gpt-5"),
    (r"(?:^|[/:.])gpt-4\.1$", 1047576, 32768, "https://developers.openai.com/api/docs/models/gpt-4.1"),
    (r"(?:^|[/:.])gpt-4o(?:-2024-08-06)?$", 128000, 16384,
     "https://developers.openai.com/api/docs/models/gpt-4o"),
    (r"(?:^|[/:.])o1$", 200000, 100000, "https://developers.openai.com/api/docs/models/o1"),
    (r"(?:^|[/:.])o3$", 200000, 100000, "https://developers.openai.com/api/docs/models/o3"),
    (r"(?:^|[/:.])o4-mini$", 200000, 100000, "https://developers.openai.com/api/docs/models/o4-mini"),
    (r"(?:^|[/:.])gemini-2\.5-pro$", 1048576, 65536,
     "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro"),
    (r"(?:^|[/:.])gemini-2\.5-flash$", 1048576, 65536,
     "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash"),
    (r"(?:^|[/:.])gemini-2\.5-flash-lite$", 1048576, 65536,
     "https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite"),
    (r"(?:^|[/:.])gemini-2\.0-flash$", 1048576, 8192,
     "https://ai.google.dev/gemini-api/docs/models/gemini-2.0-flash"),
)
# 旧型号/旧快照没有当前官方逐型号页可引用：保留保守数值，但绝不带来源或核对日期。
CAPABILITY_HEURISTIC_RULES = (
    (r"(?:^|[/:.])claude-(?:opus|sonnet)-4(?:-1)?(?:-\d{8})?(?:-v\d+:\d+)?$", 200000, 32000),
    (r"(?:^|[/:.])claude-3-(?:opus|sonnet|haiku)(?:-\d{8})?(?:-v\d+:\d+)?$", 200000, 4096),
    (r"(?:^|[/:.])gpt-4o-2024-05-13$", 128000, 4096),
    (r"(?:^|[/:.])gemini-1\.5-pro(?:-\d{3})?$", 2097152, 8192),
    (r"(?:^|[/:.])gemini-1\.5-flash(?:-\d{3})?$", 1048576, 8192),
)
CAPABILITY_OVERRIDE_FIELDS = ("context_tokens", "max_output_tokens", "source_url", "note", "updated_at")


def _capability_efforts(model):
    value = str(model or "").lower()
    if "claude" in value:
        return ["low", "medium", "high", "max"]
    if "gemini" in value:
        return ["default"]
    if re.search(r"gpt-[0-9]|\bo[134]\b", value):
        return ["default", "low", "medium", "high", "xhigh", "max"]
    return ["default"]


def normalize_capability_override(value):
    """校验并规范化人工覆盖；不读取模型基础值，调用方负责最终上限一致性检查。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Model capability override must be an object")
    unknown = set(value) - set(CAPABILITY_OVERRIDE_FIELDS)
    if unknown:
        raise ValueError("Unknown model capability override field: " + sorted(unknown)[0])
    result = {}
    for name in ("context_tokens", "max_output_tokens"):
        limit = value.get(name)
        if limit is None:
            continue
        # bool 是 int 的子类，但 True 不能当作 1 token 的能力上限。
        if type(limit) is not int or limit <= 0:
            raise ValueError("Model capability {} must be a positive integer".format(name))
        result[name] = limit
    if result.get("context_tokens") is not None and result.get("max_output_tokens") is not None and \
            result["max_output_tokens"] > result["context_tokens"]:
        raise ValueError("max_output_tokens cannot exceed context_tokens")
    source_url = value.get("source_url")
    if source_url is not None:
        if not isinstance(source_url, str) or len(source_url) > 500 or \
                not re.fullmatch(r"https?://[^\s]+", source_url):
            raise ValueError("Model capability source_url must be an http(s) URL")
        result["source_url"] = source_url
    note = value.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note.strip()) > 500:
            raise ValueError("Model capability note must be at most 500 characters")
        if note.strip():
            result["note"] = note.strip()
    updated_at = value.get("updated_at")
    if updated_at is not None:
        if not isinstance(updated_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", updated_at):
            raise ValueError("Model capability updated_at must be YYYY-MM-DD")
        try:
            datetime.date.fromisoformat(updated_at)
        except ValueError:
            raise ValueError("Model capability updated_at must be YYYY-MM-DD") from None
        result["updated_at"] = updated_at
    return result


def model_capabilities(model, protocol="", override=None):
    """Return conservative routing metadata; unknown models never inherit premium limits.

    ``source``/``confidence``/``evidence`` distinguish documented model rules from
    family inference, conservative defaults, and persistent manual overrides.
    ``updated_at`` is the rule-review date for documented rules (or the manual
    override date), never a claim about when the vendor changed the model.
    """
    value = str(model or "").lower()
    result = {"id": model, "context_tokens": 128000, "max_output_tokens": 16384,
              "efforts": [*_capability_efforts(model)], "tools": True,
              "streaming": protocol in ("responses", "anthropic", "chat_completions", "gemini"),
              "streaming_implemented": False, "metadata_version": 1, "updated_at": None,
              "source": "conservative_default", "confidence": "heuristic", "evidence": "heuristic",
              "source_url": None, "note": None, "date_semantics": None}
    for pattern, context_tokens, max_output_tokens, source_url in CAPABILITY_RULES:
        if re.search(pattern, value):
            result.update(context_tokens=context_tokens, max_output_tokens=max_output_tokens,
                          source="known_model", confidence="known", evidence="documented",
                          source_url=source_url, updated_at=CAPABILITY_RULE_REVIEWED_AT,
                          date_semantics="rule_reviewed")
            break
    else:
        heuristic = next(((context, output) for pattern, context, output in CAPABILITY_HEURISTIC_RULES
                          if re.search(pattern, value)), None)
        if heuristic:
            result.update(context_tokens=heuristic[0], max_output_tokens=heuristic[1],
                          source="inferred_family", confidence="heuristic", evidence="heuristic")
        elif "claude" in value:
            result.update(context_tokens=200000, max_output_tokens=8192,
                          source="inferred_family")
        elif "gemini" in value:
            result.update(source="inferred_family")
        elif re.search(r"gpt-[0-9]|\bo[134]\b", value):
            result.update(source="inferred_family")
    if "luna" in value:
        # 本地别名没有可引用的官方上限；保留历史上的 32k 上限，但标为推断。
        result.update(max_output_tokens=min(result["max_output_tokens"], 32768),
                      efforts=["default", "low", "medium", "high", "xhigh", "max"],
                      source="inferred_family", confidence="heuristic", evidence="heuristic")
    if override is not None:
        limits = normalize_capability_override(override)
        context_tokens = limits.get("context_tokens", result["context_tokens"])
        max_output_tokens = limits.get("max_output_tokens", result["max_output_tokens"])
        if max_output_tokens > context_tokens:
            raise ValueError("max_output_tokens cannot exceed context_tokens")
        result.update({name: limit for name, limit in limits.items()
                       if name in ("context_tokens", "max_output_tokens")})
        result.update(source="manual", confidence="manual", evidence="manual",
                      source_url=limits.get("source_url", None), note=limits.get("note", None),
                      updated_at=limits.get("updated_at"),
                      date_semantics="manual_override" if limits.get("updated_at") else None,
                      manual_fields=[name for name in ("context_tokens", "max_output_tokens")
                                     if name in limits])
    result["request_output_tokens"] = request_output_budget(result["id"], protocol, result["max_output_tokens"])
    return result


def request_output_budget(model, protocol, max_output_tokens=None):
    """单次请求的输出预算；永远不超过模型能力上限。

    max_output_tokens 是“模型能产多少”的能力元数据，用于上下文/响应字节上限和界面展示；
    每次请求下发的输出预算是另一件事：未知型号保持保守，已知家族只在能力之内提高。
    Anthropic 必须在握手参数 max_tokens 中给出该预算，不能一律下发家族最大能力。
    """
    limit = max_output_tokens if type(max_output_tokens) is int and max_output_tokens > 0 else None
    value = str(model or "").lower()
    if protocol == "anthropic":
        budget = next((size for pattern, size in ANTHROPIC_REQUEST_OUTPUT if re.search(pattern, value)),
                      ANTHROPIC_DEFAULT_REQUEST_OUTPUT)
    else:
        budget = limit or 16384
    return max(1, min(budget, limit)) if limit else max(1, budget)


def provider_arguments(route, config, environment=None):
    provider_id = route.get("provider", "openai")
    if provider_id == "openai":
        return ["-c", 'model_provider="openai"']
    provider = next((p for p in config["providers"] if p["id"] == provider_id), None)
    if provider is None:
        raise RuntimeError("Unknown model provider: " + provider_id)
    if provider["env_key"] and not (environment if environment is not None else os.environ).get(provider["env_key"]):
        raise RuntimeError("Missing provider environment variable: " + provider["env_key"])
    settings = {"name": provider["name"], "base_url": provider["base_url"],
                "wire_api": "responses", "requires_openai_auth": False}
    if provider["env_key"]:
        settings["env_key"] = provider["env_key"]
    argv = ["-c", "model_provider=" + json.dumps(provider_id)]
    for key, value in settings.items():
        argv += ["-c", "model_providers." + provider_id + "." + key + "=" + json.dumps(value)]
    return argv


def workspace_signature(workspace, max_bytes):
    """Fingerprint tracked edits and new source files before considering a fallback."""
    import hashlib
    import os
    from .gitops import git

    root = Path(workspace).resolve()
    digest = hashlib.sha256()
    remaining = max_bytes
    try:
        changed = git(root, "diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv", "HEAD", "--").split("\0")
        files = [name for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name]
        files = sorted(set(changed) | set(files))
    except RuntimeError:
        # Standalone executor tests and probes may use a plain directory instead of a project snapshot.
        files = []
        for parent, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name != ".git" and not name.startswith(".rsi-")]
            files.extend((Path(parent) / name).relative_to(root).as_posix() for name in names
                         if not name.startswith(".rsi-"))
    for name in sorted(files):
        if not name:
            continue
        if any(part.startswith(".rsi-") for part in Path(name).parts):
            continue
        path = root / name
        digest.update(name.encode("utf-8", "surrogateescape"))
        if path.is_symlink():
            digest.update(b"symlink\0" + os.readlink(path).encode("utf-8", "surrogateescape"))
            continue
        if not path.exists():
            digest.update(b"missing\0")
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            # 父目录符号链接指向快照之外：按可比较性错误处理，不让未分类 ValueError 替换原始异常。
            raise RuntimeError("Workspace contains a symlink escaping the snapshot: " + name) from None
        if not resolved.is_file():
            continue
        digest.update(str(resolved.stat().st_mode & 0o777).encode())
        size = resolved.stat().st_size
        if size > remaining:
            raise RuntimeError("Workspace output is too large to compare safely before fallback")
        remaining -= size
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


class CodexAgent:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def available(provider):
        protocol = provider.get("protocol", "codex")
        if protocol in ["codex", "codex_login", "opencode"]:
            return bool(shutil.which("opencode" if protocol == "opencode" else "codex") or
                        platform.system() == "Linux" and shutil.which("docker"))
        return True

    def invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        from .provider_health import ModelFailure, ProviderHealth, error_code, provider_text_code
        from .native import ProviderUnavailable
        health = ProviderHealth(self.store)
        primary = config["models"][role]
        routes = [primary] + ([primary["fallback"]] if primary.get("fallback") else [])
        workspace_before = (workspace_signature(workspace, config["limits"]["max_output_mb"] * 1024 * 1024)
                            if role == "implementation" and len(routes) > 1 else None)
        for index, route in enumerate(routes):
            provider_id = route.get("provider", "openai")
            attempt = None
            try:
                attempt = health.require(config, provider_id, project, route["model"])
                effective = {**config, "models": {**config["models"], role: {**route, "fallback": None}}}
                result = self._invoke(role, prompt, workspace, project, run, effective, cancel, protected)
            except Exception as exc:
                exc.provider_id = provider_id
                exc.role = role
                try:
                    health.record(config, provider_id, project, exc, route["model"], attempt=attempt)
                except Exception as health_error:
                    import logging
                    logging.getLogger(__name__).warning("Provider failure recording failed (%s)", type(health_error).__name__)
                if isinstance(exc, Cancelled):
                    raise
                # 仅不可用且未修改工作区的路由允许备用；原生执行器负责拦截半成品。
                # 仅旧适配器无代码的 CLI 自身诊断允许文本兼容；明确的 execution
                # 或认证/额度故障绝不被消息中的 timeout 等关键词覆盖。
                known = getattr(exc, "failure_code", None) or getattr(exc, "code", None)
                legacy_cli = not known and str(exc).startswith(("Codex call failed:", "OpenCode call failed:"))
                code = provider_text_code(str(exc)) if legacy_cli else error_code(exc)
                unavailable = bool(known or legacy_cli) and code in {
                    "rate_limit", "timeout", "connection", "model_missing", "provider_paused"}
                if not known and isinstance(exc, ProviderUnavailable):
                    unavailable = True
                can_fallback = (index + 1 < len(routes) and unavailable and
                                getattr(exc, "fallback_allowed", True))
                if can_fallback and workspace_before is not None and \
                        workspace_signature(workspace, config["limits"]["max_output_mb"] * 1024 * 1024) != workspace_before:
                    raise ModelFailure("Primary implementation failed after changing the workspace; fallback refused to prevent mixing partial edits",
                                       "execution") from None
                if can_fallback:
                    self.store.event("fallback", "Primary provider unavailable; using configured fallback", project, run)
                    continue
                raise
            health.record(config, provider_id, project, model=route["model"], attempt=attempt)
            return result

    def _invoke(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        from .resources import execution_resources
        with execution_resources.acquire(cancel=cancel, priority="model"):
            return self._invoke_with_resources(role, prompt, workspace, project, run, config, cancel, protected)

    def _invoke_with_resources(self, role, prompt, workspace, project, run, config, cancel=None, protected=()):
        route = config["models"][role]
        from .config import BUILTIN_PROVIDERS
        provider = next((p for p in [*config["providers"], *BUILTIN_PROVIDERS] if p["id"] == route.get("provider", "openai")), None)
        if provider and provider.get("protocol") == "opencode":
            from .opencode import OpenCodeAgent
            return OpenCodeAgent(self.store).invoke(role, prompt, workspace, project, run, config, route, cancel, protected)
        if provider and provider.get("protocol", "codex") not in ["codex", "codex_login"]:
            from .native import NativeAgent
            return NativeAgent(self.store).invoke(role, prompt, workspace, project, run,
                                                 config, route, provider, cancel, protected)
        binary = shutil.which("codex")
        container = platform.system() != "Darwin" or config["sandbox"]["backend"] == "docker"
        if not binary and not container:
            raise RuntimeError("Codex CLI is not installed for this route")
        binary = "codex" if container else str(Path(binary).resolve())
        billing_config = config
        environment = None
        secret = ""
        if provider and provider.get("credential", "env") in ["keychain", "session", "file"]:
            from .credentials import Credentials
            secret = Credentials(self.store.root).get(provider)
            environment = {**os.environ, "RSI_PROVIDER_API_KEY": secret}
            config = {**config, "providers": [{**p, "env_key": "RSI_PROVIDER_API_KEY"} if p["id"] == provider["id"] else p for p in config["providers"]]}
        if container:
            from .cli_worker import container_endpoint
            config = {**config, "providers": [{**p, "base_url": container_endpoint(p["base_url"])} for p in config["providers"]]}
        provider_args = provider_arguments(route, config, environment)
        call = self.store.reserve_call(project, run, role, route, billing_config)
        started = time.time()
        try:
            sandbox = Sandbox(self.store.root, config)
            agent_dir = Path(workspace) / ".rsi-agent" / call
            agent_dir.mkdir(parents=True)
            schema_path = agent_dir / "schema.json"
            # Codex structured output requires every declared property in required.
            schema_path.write_text(json.dumps({**SCHEMAS[role], "required": list(SCHEMAS[role]["properties"])}))
            output_path = agent_dir / "result.json"
            prompt_path = agent_dir / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            mode = "workspace-write" if role == "implementation" else "read-only"
            argv = [binary, "-a", "never", "exec", "--ignore-user-config", "--ignore-rules",
                    "--ephemeral", "--model", route["model"]] + provider_args
            if role == "probe":
                argv += ["--skip-git-repo-check"]
            if secret:
                argv += ["-c", 'shell_environment_policy.exclude=["RSI_PROVIDER_API_KEY"]']
            if route["effort"] != "default":
                argv += ["-c", "model_reasoning_effort=" + json.dumps(route["effort"])]
            if container:
                argv += ["--sandbox", "danger-full-access", "-c", 'shell_environment_policy.inherit="all"',
                         "-c", 'shell_environment_policy.include_only=["PATH","HOME","LANG","LC_ALL","TMPDIR"]']
            else:
                argv += sandbox.codex_permissions(workspace, protected, read_only=role != "implementation",
                                                  output_file=output_path if role != "implementation" else None)
            argv += ["-c", "sandbox_workspace_write.network_access=false",
                     "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                     "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                     "--json", "--color", "never", "--output-schema", str(schema_path),
                     "--output-last-message", str(output_path), "-C", str(workspace),
                     "Read the task in " + str(prompt_path) + ". Follow its instructions; return the requested JSON."]
            self.store.event("model", role + ": " + route.get("provider", "openai") + " / " + route["model"] + " / " + route["effort"], project, run)
            auth_secrets = []
            if container:
                from .cli_worker import cli_home, codex_home, run_cli, sync_codex_oauth, CODEX_AUTH_LOCK
                login = provider and provider.get("protocol") == "codex_login"
                with cli_home(workspace) as home:
                    # 仅在拷贝与回写宿主凭据时持锁；隔离 home 内的模型调用不串行。
                    if login:
                        with CODEX_AUTH_LOCK:
                            original_auth = codex_home(home)
                    else:
                        original_auth = {}
                    (home / ".codex").mkdir(exist_ok=True)
                    auth_secrets += list((original_auth.get("tokens") or {}).values())
                    forwarded = {}
                    if provider and provider.get("env_key"):
                        key = provider["env_key"]
                        forwarded[key] = (environment or os.environ).get(key, "")
                    if secret:
                        forwarded["RSI_PROVIDER_API_KEY"] = secret
                    try:
                        result, cancelled = capture_cancel(run_cli, argv, workspace, home, config, cancel, protected,
                                                           role != "implementation", forwarded, self.store.root, timeout=3600)
                    finally:
                        if login:
                            auth_file = home / ".codex/auth.json"
                            if auth_file.is_file():
                                auth_secrets += list((json.loads(auth_file.read_text()).get("tokens") or {}).values())
                            with CODEX_AUTH_LOCK:
                                sync_codex_oauth(home, original_auth)
            else:
                # 模型挂起不能永久占用并发槽位；上限 1 小时，仅靠取消无法覆盖所有情况。
                result, cancelled = capture_cancel(run_process, argv, workspace, 3600, cancel,
                                                   config["limits"]["max_output_mb"], environment)
            for secret_value in [secret, *auth_secrets]:
                if not isinstance(secret_value, str) or len(secret_value) <= 8:
                    # 过短的值会在脱敏替换时破坏 JSON 与统计记录，不进入脱敏列表。
                    continue
                for field in ["stdout", "stderr"]:
                    result[field] = result[field].replace(secret_value, "[REDACTED]")
            (agent_dir / "events.jsonl").write_text(result["stdout"], encoding="utf-8")
            usage = None
            for line in result["stdout"].splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        from .usage import usage as normalized_usage
                        value = event.get("usage", {})
                        cached = int(value.get("cached_input_tokens", 0))
                        current = normalized_usage(max(0, int(value.get("input_tokens", 0)) - cached),
                                                   int(value.get("output_tokens", 0)), cached)
                        if usage is None:
                            usage = current
                        else:
                            for key in ["tokens", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]:
                                usage[key] += current[key]
                except (ValueError, TypeError):
                    continue
            self.store.record_usage(call, run, usage, time.time() - started)
            if cancelled is not None:
                raise cancelled
            from .provider_health import cli_failure
            problem = cli_failure("Codex", result)
            if problem is not None:
                raise problem
            data = json.loads(output_path.read_text(encoding="utf-8"))
            validate_output(data, SCHEMAS[role])
            self.store.execute("UPDATE calls SET state='completed' WHERE id=?", (call,))
            return data
        except Exception:
            self.store.execute("UPDATE calls SET state='failed',seconds=? WHERE id=? AND state='reserved'",
                               (time.time() - started, call))
            raise
