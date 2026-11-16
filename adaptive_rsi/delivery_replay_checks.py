"""Fail-closed replay of a sealed evaluation on an advanced Git baseline.

The caller owns the returned directory and must independently decide whether to
publish. This module neither updates the run nor writes to a remote.
"""
import json
import shutil
from pathlib import Path

from .delivery import baseline_change
from .environment import frozen_dependencies
from .evaluator import _added_test_files, _test_symbols
from .gitops import git, snapshot
from .config import fingerprint
from .workflow import improvement


class ReplayRefused(ValueError):
    """The original evidence or the replay does not establish a safe pass."""


def sealed_dependency_digest(seeds, config):
    """Hash frozen seeds without Git discovery or modifying original evidence."""
    return frozen_dependencies(seeds, config)["sha256"]


def _acceptance(directory, evaluation, source):
    expected = source.get("acceptance_hash")
    if expected != evaluation.get("acceptance_hash"):
        raise ReplayRefused("acceptance fingerprints disagree")
    path = directory / "acceptance.json"
    if not expected:
        if path.exists():
            raise ReplayRefused("unexpected acceptance evidence")
        return None
    if not source.get("contract_hash") or not source.get("criteria_hash"):
        raise ReplayRefused("historical acceptance binding missing; re-evaluation required")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        from .agent import SCHEMAS, validate_output
        validate_output(value["spec"], SCHEMAS["acceptance"])
        checks = [{"name": item["name"], "argv": item["argv"], "cwd": "."}
                  for item in value["spec"]["checks"]]
        if (value["hash"] != expected or fingerprint(value["spec"]) != expected
                or value["checks"] != checks or value["criteria"] != evaluation["criteria"]
                or not isinstance(value["contract"], dict)
                or value["contract"] != evaluation["contract"]
                or fingerprint(value["contract"]) != source.get("contract_hash")
                or fingerprint(value["criteria"]) != source.get("criteria_hash")
                or value["change_kind"] != value["contract"].get("kind")
                or value["change_kind"] != source["change_kind"]):
            raise ValueError("acceptance changed")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise ReplayRefused("sealed acceptance evidence is missing or changed") from None
    return value


def replay_on_advanced_baseline(runtime, project, run, workspace, outcome, source, current, config, directory):
    """Return (candidate tree, evidence) after re-running all sealed gates.

    ``directory`` must be a new, caller-owned directory; evidence and snapshots
    persist there for inspection. Failures raise ReplayRefused and leave run.base
    untouched. The evaluator's cancellation exception propagates unchanged.
    """
    previous = run["base"]
    if baseline_change(source, previous, current)["relation"] != "advanced":
        raise ReplayRefused("baseline is not a verified advance")
    evaluation = outcome.get("evaluation") or {}
    sealed = evaluation.get("source")
    if not isinstance(sealed, dict) or evaluation.get("decision") != "pass":
        raise ReplayRefused("original passing evaluation is missing")
    if "execution_environment" not in evaluation:
        raise ReplayRefused("historical execution environment missing; re-evaluation required")
    try:
        identity = runtime.evaluator.execution_identity(config)
    except Exception:
        raise ReplayRefused("execution environment cannot be verified") from None
    if identity != evaluation["execution_environment"]:
        raise ReplayRefused("execution environment changed; re-evaluation required")
    checks = sealed.get("checks")
    if (not isinstance(checks, list) or not checks or not all(isinstance(x, dict) for x in checks)
            or fingerprint(checks) != evaluation.get("harness_hash")
            or ("checks_spec" in outcome and outcome["checks_spec"] != checks)):
        raise ReplayRefused("fixed checks or harness fingerprint changed")
    if (sealed.get("base") != previous or outcome.get("base") != previous
            or not isinstance(sealed.get("test_first"), bool)
            or not isinstance(sealed.get("change_kind"), str) or not sealed["change_kind"]
            or sealed.get("validation") != config.get("validation")):
        raise ReplayRefused("sealed evaluation context changed or missing")
    patch = git(workspace, "diff", previous, "--binary", "--no-ext-diff", "--no-textconv")
    if not patch or fingerprint(patch) != sealed.get("patch_hash") or outcome.get("patch_hash") != sealed["patch_hash"]:
        raise ReplayRefused("candidate patch changed")
    files = outcome.get("files")
    actual = sorted(x for x in git(workspace, "diff", previous, "--name-only", "-z").split("\0") if x)
    if not isinstance(files, list) or sorted(files) != actual:
        raise ReplayRefused("changed-file evidence changed")
    run_dir = Path(run["path"])
    acceptance = _acceptance(run_dir, evaluation, sealed)
    seeds = run_dir / "dependencies"
    if not seeds.is_dir() or not isinstance(sealed.get("environment"), str):
        raise ReplayRefused("sealed dependency evidence is missing")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        if sealed_dependency_digest(seeds, config) != sealed["environment"]:
            raise ValueError("seed identity changed")
        shutil.copytree(seeds, directory / 'dependencies', symlinks=True)
        seeds = directory / 'dependencies'
        digest = sealed_dependency_digest(seeds, config)
    except (OSError, RuntimeError, ValueError, KeyError):
        raise ReplayRefused("sealed dependency evidence is invalid") from None
    if digest != sealed["environment"]:
        raise ReplayRefused("sealed dependency digest changed")
    baseline = snapshot(source, directory / "baseline", current)
    candidate = snapshot(source, directory / "candidate", current)
    patch_path = directory / "candidate.patch"
    patch_path.write_text(patch + "\n", encoding="utf-8")
    try:
        git(candidate, "apply", "--index", str(patch_path.resolve()))
    except RuntimeError:
        raise ReplayRefused("candidate patch does not apply") from None
    evaluated_tree = git(candidate, "write-tree")
    result = runtime.evaluator.evaluate(
        baseline, candidate, checks, directory / "evaluation", config,
        cancel=lambda: runtime.is_cancelled(run), seeds=seeds, acceptance=acceptance,
        cache_scope=project["id"], cache_base=current, environment_digest=digest,
        test_first=sealed["test_first"], test_first_kind=sealed["change_kind"], changed_files=files)
    if (git(candidate, "write-tree") != evaluated_tree or
            git(candidate, "diff", "--no-ext-diff", "--no-textconv") or
            git(candidate, "ls-files", "--others", "--exclude-standard", "-z")):
        raise ReplayRefused("replay evaluator changed candidate")
    if not isinstance(result, dict) or result.get("decision") != "pass":
        raise ReplayRefused("replay evaluator did not pass")
    if result.get("execution_environment") != evaluation["execution_environment"]:
        raise ReplayRefused("replay execution environment changed")
    if result.get("harness_hash") != evaluation["harness_hash"]:
        raise ReplayRefused("replay harness fingerprint changed")
    if acceptance and (result.get("acceptance_hash") != acceptance["hash"]
                       or result.get("contract") != acceptance["contract"]):
        raise ReplayRefused("replay acceptance evidence changed")
    if sealed["test_first"]:
        added = _added_test_files(candidate, baseline)
        if added is None:
            raise ReplayRefused("new test evidence unavailable")
        existing = [p for p in git(baseline, "ls-files", "-z").split("\0") if p]
        from .evaluator import test_path
        existing = [p for p in existing if test_path(p)]
        old_symbols = {s for p in existing for s in _test_symbols(baseline, p)}
        symbols = [s for p in added for s in _test_symbols(candidate, p) if s not in old_symbols]
        gate = improvement(result.get("checks", {}), {"mode": "test_first", "change_kind": sealed["change_kind"],
                           "regression_test_files": added, "existing_test_files": existing,
                           "regression_test_symbols": symbols})
        if gate.get("decision") != "pass":
            raise ReplayRefused("independent test-first improvement gate did not pass")
        result["replay_test_first_improvement"] = gate
    result["source"] = {**sealed, "base": current, "patch_hash": fingerprint(
        git(candidate, "diff", current, "--binary", "--no-ext-diff", "--no-textconv"))}
    (directory / "replay-evidence.json").write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
    return candidate, result
