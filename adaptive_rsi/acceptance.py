import copy
from pathlib import Path

from .agent import SCHEMAS, validate_output
from .config import fingerprint, relative_path


def unique_check_names(spec):
    checks = spec.get("checks") if isinstance(spec, dict) else None
    if not isinstance(checks, list) or not checks:
        return False
    names = {check["name"] for check in checks if isinstance(check, dict) and isinstance(check.get("name"), str)}
    return len(names) == len(checks)


def seal(spec, criteria, directory):
    validate_output(spec, SCHEMAS["acceptance"])
    spec = copy.deepcopy(spec)
    if not spec["files"] or not spec["checks"]:
        raise RuntimeError("Independent acceptance must contain executable checks")
    if not unique_check_names(spec):
        raise RuntimeError("Independent acceptance check names must be unique")
    covered = set()
    directory = Path(directory)
    directory.mkdir()
    seen = set()
    for item in spec["files"]:
        path = relative_path(item["path"])
        if not path.startswith(".rsi-acceptance/") or path in seen or len(item["content"]) > 100000:
            raise RuntimeError("Invalid independent acceptance file")
        seen.add(path)
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    checks = []
    for check in spec["checks"]:
        if not check["argv"] or not check["criteria"] or any(i < 0 or i >= len(criteria) for i in check["criteria"]):
            raise RuntimeError("Independent acceptance lacks criterion coverage")
        covered.update(check["criteria"])
        if check["argv"][0] == "python":
            check["argv"][0] = "python3"
        checks.append({"name": check["name"], "argv": check["argv"], "cwd": "."})
    if covered != set(range(len(criteria))):
        raise RuntimeError("Independent acceptance does not cover every criterion")
    return {"hash": fingerprint(spec), "criteria": criteria, "checks": checks, "spec": spec}
