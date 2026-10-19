import copy
import json
import re


DEFAULTS = {
    "sources": {"inspection": True, "issues": True},
    "schedule": {"light_minutes": 120, "issues_minutes": 60, "deep_minutes": 4320,
                 "working_hours": [0, 24], "timezone": "Asia/Shanghai"},
    "models": {
        "analysis": {"model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "planning": {"model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "implementation": {"model": "gpt-6-luna", "effort": "max", "fallback": None},
        "review": {"model": "gpt-6.1-sol", "effort": "high", "fallback": None},
        "evolution": {"model": "gpt-6.1-sol", "effort": "high", "fallback": None},
    },
    "limits": {"daily_calls": 30, "run_calls": 5, "run_seconds": 1800,
               "command_seconds": 300, "concurrency": 1, "retries": 1,
               "max_output_mb": 12, "max_changed_files": 40, "retention_days": 30},
    "sandbox": {"backend": "auto", "image": "python:3.12-slim", "memory_mb": 2048,
                "cpus": 2, "pids": 128},
    "github": {"repository": "", "labels": [], "exclude_labels": ["wontfix", "duplicate"],
               "max_issues": 100},
    "delivery": {"mode": "local", "auto_merge": False, "base_branch": ""},
    "checks": [],
    "environment": {"seed_paths": [], "setup": [], "max_seed_mb": 1024},
    "evolution": {"enabled": True, "every_completed": 5, "auto_promote": True,
                  "min_cases_per_split": 3, "repeats": 1, "max_calls": 28,
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
    for role, route in config["models"].items():
        for item in [route, route.get("fallback")]:
            if item is None:
                continue
            if not isinstance(item, dict) or not re.fullmatch(r"[a-zA-Z0-9._/-]+", item.get("model", "")):
                raise ValueError("Invalid model route for " + role)
            if item.get("effort") not in ["low", "medium", "high", "xhigh", "max", "ultra"]:
                raise ValueError("Invalid reasoning effort for " + role)
            if "luna" in item["model"] and item["effort"] == "ultra":
                raise ValueError("Luna supports max, not ultra")
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
    if config["delivery"]["mode"] not in ["local", "pr"]:
        raise ValueError("Delivery mode must be local or pr")
    if config["delivery"]["auto_merge"] and config["delivery"]["mode"] != "pr":
        raise ValueError("Auto merge requires PR delivery")
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
    return fingerprint({key: config[key] for key in ["models", "sandbox", "checks", "environment"]})
