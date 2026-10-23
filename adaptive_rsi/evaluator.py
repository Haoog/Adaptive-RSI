import concurrent.futures
import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from .adapters import test_path
from .build import BUILD_ID
from .config import fingerprint
from .gitops import copy_source
from .process import Sandbox, run_process
from .workflow import environment_failure

MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py",
             "pom.xml", "build.gradle", "build.gradle.kts", "Makefile", "CMakeLists.txt", "Cargo.toml",
             "composer.json", "phpunit.xml", "Gemfile", "Rakefile", "Package.swift", "pubspec.yaml"}
BASELINE_CACHE_LIMIT = 4
CACHE_STDERR_LIMIT = 16 * 1024
UNAVAILABLE_OUTPUT = re.compile(
    r"execvp\(\).*No such file|executable file not found|exec:.*not found", re.I)
DOCKER_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


def empty_test_run(text):
    empty = re.search(r"Ran 0 tests|no tests ran|No tests found|No tests were found|no test files|running 0 tests|"
                      r"No examples found|No tests executed|No test is available|Total tests: 0|Tests run: 0|# tests 0|0 runs, 0 assertions", text, re.I)
    positive = re.search(r"(?:Ran|running) [1-9]\d* tests?|[1-9]\d* passed|[1-9]\d* examples?|"
                         r"Tests run: [1-9]\d*|Total tests: [1-9]\d*|# tests [1-9]\d*", text, re.I)
    return bool(empty and not positive)


def configured_check_files(baseline, checks):
    root = Path(baseline).resolve()
    files = set()
    for check in checks:
        cwd = (root / check.get("cwd", ".")).resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            raise RuntimeError("Configured check directory escapes the project") from None
        for argument in check["argv"]:
            if not isinstance(argument, str) or not argument or argument.startswith("-"):
                continue
            candidate = Path(argument)
            path = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                files.add(relative)
    return files


def freeze_harness(baseline, target, checks=()):
    """Host-owned baseline tests/build entrypoints overwrite candidate-authored gate changes."""
    protected = set()
    explicit_checks = configured_check_files(baseline, checks)
    for parent, dirs, files in os.walk(baseline, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", ".rsi-agent", ".rsi-home"}]
        for name in files:
            src = Path(parent) / name
            relative = src.relative_to(baseline)
            if not (test_path(relative) or relative in explicit_checks or name in MANIFESTS or
                    name.endswith((".csproj", ".fsproj", ".sln"))):
                continue
            dst = target / relative
            # Reject directory/symlink tricks before the host copies protected files.
            for item in [dst] + list(dst.parents):
                if item == target.parent:
                    break
                if item.is_symlink():
                    raise RuntimeError("Candidate symlink overlaps protected harness")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                shutil.rmtree(dst)
            shutil.copy2(src, dst)
            parts = relative.parts
            test_dir = next((i for i, part in enumerate(parts[:-1]) if part.lower() in {"test", "tests", "spec", "specs", "__tests__"}), None)
            protected.add(target.joinpath(*parts[:test_dir + 1]) if test_dir is not None else dst)
    return sorted(protected, key=str)


class Evaluator:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _cache_key(cache_scope, cache_base, checks, config, environment_digest, execution_identity,
                   acceptance_hash=None):
        if not cache_scope or not cache_base or not execution_identity:
            return None
        return fingerprint({"build": BUILD_ID, "scope": cache_scope, "base": cache_base, "checks": checks,
                            "execution": execution_identity,
                            "environment": config["environment"], "sandbox": config["sandbox"],
                            "dependency_digest": environment_digest, "acceptance": acceptance_hash})

    def _execution_environment(self, config):
        """Resolve an environment identity safe enough for cross-run reuse.

        Docker runs are pinned to a freshly inspected immutable image ID. Other
        backends depend on host toolchains that cannot be identified reliably,
        so they intentionally receive no cross-run cache identity.
        """
        resolved = copy.deepcopy(config)
        backend = Sandbox(self.store.root, config).backend()
        resolved["sandbox"]["backend"] = backend
        identity = {"build": BUILD_ID, "backend": backend}
        if backend != "docker":
            return resolved, None
        image = config["sandbox"]["image"]
        inspected = run_process(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=10)
        image_id = inspected["stdout"].strip()
        if inspected["exit_code"] or inspected["error"] or not DOCKER_IMAGE_ID.fullmatch(image_id):
            return resolved, None
        resolved["sandbox"]["image"] = image_id
        identity["image_id"] = image_id
        return resolved, identity

    def _cache_get(self, scope, key):
        if not key:
            return None
        entries = self.store.setting("evaluation-baseline:" + scope, {})
        entry = entries.get(key) if isinstance(entries, dict) else None
        return copy.deepcopy(entry["rows"]) if entry and isinstance(entry.get("rows"), list) else None

    def _cache_put(self, scope, key, rows):
        if not key:
            return
        if any(row.get("error") or UNAVAILABLE_OUTPUT.search(row.get("stderr") or "") for row in rows):
            return
        cache_key = "evaluation-baseline:" + scope
        entries = self.store.setting(cache_key, {})
        if not isinstance(entries, dict):
            entries = {}
        compact = []
        for row in rows:
            item = {field: value for field, value in row.items() if field != "stdout"}
            item["environment_error"] = environment_failure(row)
            if item.get("stderr"):
                item["stderr"] = item["stderr"][-CACHE_STDERR_LIMIT:]
            item["cached"] = True
            compact.append(item)
        entries[key] = {"rows": compact, "at": time.time()}
        entries = dict(sorted(entries.items(), key=lambda pair: pair[1].get("at", 0), reverse=True)[:BASELINE_CACHE_LIMIT])
        self.store.put_setting(cache_key, entries)

    @staticmethod
    def _seed_target(target, seeds, config, clear=False):
        if not seeds:
            return
        from .environment import restore
        if clear:
            for relative in config["environment"]["seed_paths"]:
                seeded = target / relative
                if seeded.exists():
                    if seeded.is_symlink():
                        seeded.unlink()
                    else:
                        shutil.rmtree(seeded)
        restore(seeds, target, config)

    def _standard_stage(self, label, source, baseline, candidate, checks, directory, config, cancel,
                        overlay, seeds, hidden):
        target = copy_source(source, Path(directory) / label)
        self._seed_target(target, seeds, config, clear=True)
        harness = candidate if label == "candidate_full" else baseline
        protected = freeze_harness(harness, target, checks)
        if overlay:
            from .process import scrub_tree
            scrub_tree(overlay)
            for parent, dirs, files in os.walk(overlay, followlinks=False):
                for name in files:
                    src = Path(parent) / name
                    dst = target / src.relative_to(overlay)
                    if dst.is_symlink() or any(p.is_symlink() for p in dst.parents):
                        raise RuntimeError("Symlink overlaps evaluation overlay")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    protected.append(dst)
        sandbox = Sandbox(self.store.root, config)
        for setup in config["environment"]["setup"]:
            result = sandbox.check(setup, target, cancel, protected, hidden)
            if result["exit_code"] or result["error"]:
                return {"setup": result}
        effective_checks = list(checks)
        if label == "candidate_full":
            from .adapters import detect
            for check in detect(target)["checks"]:
                if not any(row["argv"] == check["argv"] and row.get("cwd", ".") == check.get("cwd", ".")
                           for row in effective_checks):
                    effective_checks.append(check)
        rows = []
        for check in effective_checks:
            result = sandbox.check(check, target, cancel, protected, hidden)
            text = result["stdout"] + result["stderr"]
            empty = empty_test_run(text)
            result["passed"] = result["exit_code"] == 0 and not result["error"] and not empty
            result["empty_tests"] = empty
            rows.append(result)
        return {"rows": rows}

    def _acceptance_stage(self, label, source, acceptance, directory, config, cancel, seeds, hidden):
        from .config import relative_path
        target = copy_source(source, Path(directory) / label)
        if (target / ".rsi-acceptance").exists():
            raise RuntimeError("Candidate overlaps independent acceptance namespace")
        protected = []
        for item in acceptance["spec"]["files"]:
            dst = target / relative_path(item["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(item["content"], encoding="utf-8")
            protected.append(dst)
        self._seed_target(target, seeds, config)
        sandbox = Sandbox(self.store.root, config)
        for setup in config["environment"]["setup"]:
            result = sandbox.check(setup, target, cancel, protected, hidden)
            if result["exit_code"] or result["error"]:
                return {"setup": result}
        rows = []
        for check in acceptance["checks"]:
            result = sandbox.check(check, target, cancel, protected, hidden)
            result["passed"] = result["exit_code"] == 0 and not result["error"] and not empty_test_run(
                result["stdout"] + result["stderr"])
            rows.append(result)
        return {"rows": rows}

    @staticmethod
    def _run_stages(stages, limit):
        if not stages:
            return {}
        workers = min(3, max(1, limit), len(stages))
        if workers == 1 or len(stages) == 1:
            return {label: task() for label, task in stages.items()}
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                   thread_name_prefix="rsi-eval") as pool:
            futures = {pool.submit(task): label for label, task in stages.items()}
            try:
                for future in concurrent.futures.as_completed(futures):
                    results[futures[future]] = future.result()
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        return results

    def evaluate_acceptance_baseline(self, baseline, acceptance, directory, config, cancel=None, seeds=None,
                                     cache_scope=None, cache_base=None, environment_digest=None):
        """Run the sealed baseline acceptance early, optionally in parallel with implementation."""
        execution_config, execution_identity = (
            self._execution_environment(config) if cache_scope and cache_base else (config, None))
        key = self._cache_key(cache_scope, cache_base, acceptance["checks"], execution_config,
                              environment_digest, execution_identity, acceptance.get("hash"))
        if acceptance.get("change_kind") == "performance":
            key = None
        cached = self._cache_get(cache_scope, key)
        if cached is not None:
            return {"rows": cached, "execution_environment": execution_identity}
        hidden = [row["path"] for row in self.store.query("SELECT path FROM projects")]
        outcome = self._acceptance_stage("acceptance_baseline", baseline, acceptance, directory, execution_config,
                                         cancel, seeds, hidden)
        if "setup" in outcome:
            return {"decision": "insufficient_evidence",
                    "reason": "Independent acceptance environment preparation failed",
                    "setup": outcome["setup"], "rows": [],
                    "execution_environment": execution_identity}
        self._cache_put(cache_scope, key, outcome["rows"])
        return {**outcome, "execution_environment": execution_identity}

    def evaluate(self, baseline, candidate, checks, directory, config, cancel=None, overlay=None, seeds=None,
                 acceptance=None, acceptance_baseline=None, cache_scope=None, cache_base=None,
                 environment_digest=None):
        if not checks:
            return {"decision": "insufficient_evidence", "reason": "No executable acceptance checks. Configure checks before delivery.", "checks": []}
        results = {}
        hidden = [row["path"] for row in self.store.query("SELECT path FROM projects")]
        execution_config, execution_identity = (
            self._execution_environment(config) if cache_scope and cache_base else (config, None))
        baseline_key = self._cache_key(cache_scope, cache_base, checks, execution_config,
                                       environment_digest, execution_identity)
        cached_baseline = self._cache_get(cache_scope, baseline_key)
        if cached_baseline is not None:
            results["baseline"] = cached_baseline
        acceptance_key = self._cache_key(cache_scope, cache_base, acceptance["checks"], execution_config,
                                         environment_digest, execution_identity,
                                         acceptance.get("hash")) if acceptance else None
        if acceptance and acceptance.get("change_kind") == "performance":
            acceptance_key = None
        cached_acceptance = None
        acceptance_identity_matches = (
            acceptance_baseline is not None and
            acceptance_baseline.get("execution_environment") == execution_identity)
        if acceptance and acceptance_identity_matches:
            if acceptance_baseline.get("decision") == "insufficient_evidence":
                results["acceptance_baseline"] = []
            else:
                results["acceptance_baseline"] = acceptance_baseline.get("rows", [])
        elif acceptance:
            cached_acceptance = self._cache_get(cache_scope, acceptance_key)
            if cached_acceptance is not None:
                results["acceptance_baseline"] = cached_acceptance
        if (acceptance and acceptance_identity_matches and
                acceptance_baseline.get("decision") == "insufficient_evidence"):
            data = {"decision": "insufficient_evidence", "reason": acceptance_baseline["reason"],
                    "setup": acceptance_baseline.get("setup"), "checks": results,
                    "execution_environment": execution_identity}
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        stages = {}
        if "baseline" not in results:
            stages["baseline"] = lambda: self._standard_stage(
                "baseline", baseline, baseline, candidate, checks, directory, execution_config, cancel, overlay,
                seeds, hidden)
        if acceptance and "acceptance_baseline" not in results:
            physical = "acceptance_baseline"
            if acceptance_baseline is not None:
                physical += "_" + fingerprint({
                    "current": execution_identity,
                    "early": acceptance_baseline.get("execution_environment"),
                    "nonce": time.time_ns()})[:12]
            stages["acceptance_baseline"] = lambda: self._acceptance_stage(
                physical, baseline, acceptance, directory, execution_config, cancel, seeds, hidden)
        stages["candidate"] = lambda: self._standard_stage(
            "candidate", candidate, baseline, candidate, checks, directory, execution_config, cancel, overlay,
            seeds, hidden)
        stages["candidate_full"] = lambda: self._standard_stage(
            "candidate_full", candidate, baseline, candidate, checks, directory, execution_config, cancel, overlay,
            seeds, hidden)
        if acceptance:
            stages["acceptance_candidate"] = lambda: self._acceptance_stage(
                "acceptance_candidate", candidate, acceptance, directory, execution_config, cancel, seeds, hidden)
        outcomes = self._run_stages(stages, int(config.get("limits", {}).get("concurrency", 1)))
        for label in ["baseline", "acceptance_baseline", "candidate", "candidate_full", "acceptance_candidate"]:
            if label not in outcomes:
                continue
            outcome = outcomes[label]
            if "setup" in outcome:
                data = {"decision": "insufficient_evidence",
                        "reason": "Independent acceptance environment preparation failed"
                        if label.startswith("acceptance") else "Isolated dependency preparation failed",
                        "setup": outcome["setup"], "checks": results,
                        "execution_environment": execution_identity}
                Path(directory).mkdir(parents=True, exist_ok=True)
                (Path(directory) / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return data
            results[label] = outcome["rows"]
        if cached_baseline is None and "baseline" in results:
            self._cache_put(cache_scope, baseline_key, results["baseline"])
        if acceptance and "acceptance_baseline" in outcomes:
            self._cache_put(cache_scope, acceptance_key, results["acceptance_baseline"])
        passing = all(row["passed"] for label in ["candidate", "candidate_full", "acceptance_candidate"]
                      for row in results.get(label, []))
        unavailable = any(not row["passed"] and UNAVAILABLE_OUTPUT.search(row["stderr"])
            for label in ["candidate", "candidate_full", "acceptance_candidate"] for row in results.get(label, []))
        data = {"decision": "insufficient_evidence" if unavailable else "pass" if passing else "fail",
                "reason": "Regression, full candidate and independent acceptance passed" if passing else "Candidate failed independent checks",
                "harness_hash": fingerprint(checks), "checks": results,
                "execution_environment": execution_identity}
        if acceptance:
            from .workflow import improvement
            data["improvement"] = improvement(results, acceptance)
            if passing and data["improvement"]["decision"] != "pass":
                data.update(decision="insufficient_evidence", reason="Checks passed but task-specific improvement is unproven")
            data["acceptance_hash"] = acceptance["hash"]
            data["criteria"] = acceptance["criteria"]
            data["coverage"] = [{"name": check["name"], "criteria": check["criteria"]} for check in acceptance["spec"]["checks"]]
            data["contract"] = acceptance.get("contract")
        if unavailable:
            data["reason"] = "Required check executable is unavailable; candidate remains unverified"
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
