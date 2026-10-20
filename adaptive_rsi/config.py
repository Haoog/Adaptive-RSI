import copy
import json
import re
from urllib.parse import urlsplit


BUILTIN_PROVIDERS = [
    {"id": "openai", "name": "Codex", "protocol": "codex_login", "base_url": "", "env_key": ""},
    {"id": "ollama", "name": "Ollama", "protocol": "ollama", "base_url": "http://127.0.0.1:11434", "env_key": ""},
    {"id": "lmstudio", "name": "LM Studio", "protocol": "chat_completions", "base_url": "http://127.0.0.1:1234/v1", "env_key": ""},
]

DEFAULTS = {
    "sources": {"inspection": True, "issues": True},
    "schedule": {"light_minutes": 120, "issues_minutes": 60, "deep_minutes": 4320,
                 "weight": 1, "working_hours": [0, 24], "timezone": "Asia/Shanghai"},
    "providers": [],
    "models": {
        "analysis": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "planning": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "implementation": {"provider": "openai", "model": "gpt-6-luna", "effort": "max", "fallback": None},
        "review": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "evolution": {"provider": "openai", "model": "gpt-6.1-sol", "effort": "high", "fallback": None},
    },
    "review": {"block_on": "medium"},
    "triage": {"policy": "Prioritize evidenced defects and meaningful user benefit; avoid cosmetic churn and speculative rewrites."},
    "updates": {"mode": "notify", "interval_minutes": 1440},
    "limits": {"daily_calls": 30, "run_calls": 5, "run_seconds": 1800,
               "command_seconds": 300, "concurrency": 1, "retries": 1,
               "max_output_mb": 12, "max_changed_files": 40, "retention_days": 30},
    "sandbox": {"backend": "auto", "image": "python:3.12-slim", "memory_mb": 2048,
                "cpus": 2, "pids": 128},
    "github": {"repository": "", "labels": [], "exclude_labels": ["wontfix", "duplicate"],
               "max_issues": 100},
    "delivery": {"mode": "local", "auto_merge": False, "base_branch": "", "feedback_minutes": 1440},
    "checks": [],
    "environment": {"seed_paths": [], "setup": [], "max_seed_mb": 1024},
    "evolution": {"enabled": True, "every_completed": 5, "auto_promote": True,
                  "min_cases_per_split": 3, "repeats": 1, "max_calls": 40,
                  "max_cost_ratio": 1.25, "max_holdout_uses": 3,
                  "monitor_tasks": 5, "archive_limit": 50},
}


def merge(*layers):
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
    return result


def validate(config):
    def shape(values, template, path=""):
        for key, value in values.items():
            if key not in template:
                raise ValueError("Unknown setting: " + path + key)
            expected = template[key]
            if isinstance(expected, dict):
                if not isinstance(value, dict):
                    raise ValueError(path + key + " must be an object")
                shape(value, expected, path + key + ".")
            elif expected is not None and not (type(value) is type(expected) or
                                               type(expected) is float and type(value) in [int, float]):
                raise ValueError(path + key + " has the wrong type")
    shape(config, DEFAULTS)
    config = merge(DEFAULTS, config)
    providers = {"openai", "ollama", "lmstudio"}
    if len(config["providers"]) > 100:
        raise ValueError("Too many model providers")
    for provider in config["providers"]:
        if not isinstance(provider, dict) or set(provider) - {"id", "name", "base_url", "env_key", "protocol", "credential", "secret_ref"} or not {"id", "name", "base_url", "env_key"}.issubset(provider):
            raise ValueError("Provider requires id, name, base_url, env_key and optional protocol; never store API keys")
        if not all(isinstance(value, str) for value in provider.values()):
            raise ValueError("Provider fields must be strings")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", provider["id"]) or provider["id"] in providers:
            raise ValueError("Invalid, duplicate or reserved provider id")
        providers.add(provider["id"])
        endpoint = urlsplit(provider["base_url"])
        local = endpoint.hostname in ["localhost", "127.0.0.1", "::1"]
        if any(c.isspace() or ord(c) < 32 for c in provider["base_url"]) or \
                not endpoint.hostname or endpoint.username is not None or endpoint.password is not None or \
                endpoint.query or endpoint.fragment or not (endpoint.scheme == "https" or endpoint.scheme == "http" and local):
            raise ValueError("Provider URL requires HTTPS (HTTP allowed on loopback), without credentials or query")
        if not provider["name"].strip() or len(provider["name"]) > 100:
            raise ValueError("Provider requires a short display name")
        if provider["env_key"] and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", provider["env_key"]):
            raise ValueError("env_key must be an environment variable name, not a secret")
        if provider.get("protocol", "codex") not in ["codex", "responses", "chat_completions", "anthropic", "gemini", "ollama"]:
            raise ValueError("Unsupported model protocol")
        mode = provider.get("credential", "env")
        if mode not in ["env", "keychain", "session", "none"]:
            raise ValueError("Unsupported credential mode")
        if mode in ["keychain", "session"]:
            if provider["env_key"] or not re.fullmatch(r"[a-f0-9]{32}", provider.get("secret_ref", "")):
                raise ValueError("Stored credentials require a reference, not an environment variable or key")
        elif provider.get("secret_ref"):
            raise ValueError("Unexpected credential reference")
    for role, route in config["models"].items():
        for item in [route, route.get("fallback")]:
            if item is None:
                continue
            if not isinstance(item, dict) or set(item) - {"provider", "model", "effort", "fallback"} or \
                    not isinstance(item.get("model"), str) or not re.fullmatch(r"[a-zA-Z0-9._/:-]+", item["model"]):
                raise ValueError("Invalid model route for " + role)
            if not isinstance(item.get("provider", "openai"), str) or item.get("provider", "openai") not in providers:
                raise ValueError("Unknown model provider for " + role)
            if item is not route and item.get("fallback") is not None:
                raise ValueError("Nested fallback routes are not supported")
            if item.get("effort") not in ["default", "low", "medium", "high", "xhigh", "max", "ultra"]:
                raise ValueError("Invalid reasoning effort for " + role)
            if "luna" in item["model"] and item["effort"] == "ultra":
                raise ValueError("Luna supports max, not ultra")
    if config["review"]["block_on"] not in ["low", "medium", "high"]:
        raise ValueError("Review blocking threshold must be low, medium or high")
    if not config["triage"]["policy"].strip() or len(config["triage"]["policy"]) > 4000:
        raise ValueError("Triage policy requires 1..4000 characters")
    if config["updates"]["mode"] not in ["manual", "notify", "automatic"] or not 60 <= config["updates"]["interval_minutes"] <= 525600:
        raise ValueError("Invalid update mode or interval (minimum 60 minutes)")
    for name in ["light_minutes", "issues_minutes", "deep_minutes"]:
        if not 5 <= config["schedule"][name] <= 525600:
            raise ValueError(name + " must be between 5 and 525600")
    hours = config["schedule"]["working_hours"]
    if len(hours) != 2 or not all(type(x) is int for x in hours) or not 0 <= hours[0] < hours[1] <= 24:
        raise ValueError("working_hours must be [start, end], 0 <= start < end <= 24")
    from zoneinfo import ZoneInfo
    ZoneInfo(config["schedule"]["timezone"])
    for key, value in config["limits"].items():
        if value < (0 if key == "retries" else 1) or value > 10000000:
            raise ValueError("Invalid positive limit: " + key)
    if config["limits"]["concurrency"] > 4:
        raise ValueError("Maximum concurrency is 4")
    if config["sandbox"]["backend"] not in ["auto", "seatbelt", "docker"]:
        raise ValueError("Unsupported sandbox")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", config["sandbox"]["image"]):
        raise ValueError("Invalid container image")
    for key in ["memory_mb", "cpus", "pids"]:
        if config["sandbox"][key] < 1:
            raise ValueError("Sandbox limits must be positive")
    repo = config["github"]["repository"]
    if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Repository must be owner/name")
    if not 1 <= config["github"]["max_issues"] <= 1000:
        raise ValueError("max_issues must be 1..1000")
    for field in ["labels", "exclude_labels"]:
        if not all(isinstance(x, str) and len(x) <= 100 for x in config["github"][field]):
            raise ValueError("Invalid issue labels")
    if not 1 <= config["schedule"]["weight"] <= 10:
        raise ValueError("Project weight must be 1..10")
    delivery = config["delivery"]
    if delivery["mode"] not in ["local", "pr"] or delivery["auto_merge"] and delivery["mode"] != "pr":
        raise ValueError("Automatic merge requires PR delivery")
    if not 5 <= delivery["feedback_minutes"] <= 525600:
        raise ValueError("Feedback interval must be at least 5 minutes")
    if delivery["base_branch"] and (delivery["base_branch"].startswith("-") or
            not re.fullmatch(r"[A-Za-z0-9._/-]+", delivery["base_branch"]) or ".." in delivery["base_branch"]):
        raise ValueError("Invalid base branch")
    for path in config["environment"]["seed_paths"]:
        relative_path(path)
        if path in [".", ""]:
            raise ValueError("Seed a dependency directory, not the entire repository")
    if not 1 <= config["environment"]["max_seed_mb"] <= 100000:
        raise ValueError("Invalid dependency snapshot size limit")
    for check in config["checks"] + config["environment"]["setup"]:
        if not isinstance(check, dict) or set(check) - {"name", "cwd", "argv"}:
            raise ValueError("Check must contain name, cwd, argv")
        if not check.get("name") or not isinstance(check.get("argv"), list) or not check["argv"]:
            raise ValueError("Check requires a name and argv array")
        if not all(isinstance(x, str) and "\0" not in x and len(x) < 2000 for x in check["argv"]):
            raise ValueError("Invalid check arguments")
        relative_path(check.get("cwd", "."))
    evo = config["evolution"]
    for key in ["every_completed", "min_cases_per_split", "repeats", "max_calls",
                "max_holdout_uses", "monitor_tasks", "archive_limit"]:
        if not 1 <= evo[key] <= 1000:
            raise ValueError("Invalid evolution limit: " + key)
    if not 1 <= evo["max_cost_ratio"] <= 10:
        raise ValueError("Invalid evolution cost ratio")
    return config


def relative_path(value):
    from pathlib import PurePosixPath
    if not isinstance(value, str) or "\\" in value or "\0" in value:
        raise ValueError("Invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError("Path must remain inside the project")
    return value


def fingerprint(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compatibility(config):
    return fingerprint({key: config[key] for key in ["models", "providers", "review", "sandbox", "checks", "environment"]})
