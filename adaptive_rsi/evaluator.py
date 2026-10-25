import concurrent.futures
import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from .adapters import detect, test_path
from .build import BUILD_ID
from .config import fingerprint
from .gitops import copy_source, git
from .process import Sandbox, run_process
from .workflow import (_go_test_options, environment_failure, node_test_options, npm_test_runner,
                       unittest_discovery_options)

MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py",
             "pom.xml", "build.gradle", "build.gradle.kts", "Makefile", "CMakeLists.txt", "Cargo.toml",
             "composer.json", "composer.lock", "phpunit.xml", "Gemfile", "Gemfile.lock", "Rakefile",
             "Package.swift", "pubspec.yaml", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock",
             "poetry.lock", "Pipfile", "Pipfile.lock", "requirements.txt", "requirements-dev.txt", "go.mod",
             "go.sum", "Cargo.lock", ".npmrc", ".yarnrc", ".yarnrc.yml", "rust-toolchain",
             "rust-toolchain.toml"}
MANIFESTS_LOWER = {name.lower() for name in MANIFESTS}
HARNESS_CONFIG_PREFIXES = ("jest.config.", "vitest.config.", "playwright.config.", "cypress.config.",
                           "test.config.", "karma.conf.", "wdio.conf.", "ava.config.")
HARNESS_CONFIGS = {".mocharc.js", ".mocharc.cjs", ".mocharc.mjs", ".mocharc.json", ".mocharc.yaml",
                   ".mocharc.yml", ".mocharc.toml"}
BASELINE_CACHE_LIMIT = 4
CACHE_STDERR_LIMIT = 16 * 1024
UNAVAILABLE_OUTPUT = re.compile(
    r"execvp\(\).*No such file|executable file not found|exec:.*not found", re.I)
DOCKER_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


def empty_test_run(text):
    empty = re.search(r"Ran 0 tests|no tests ran|No tests found|No tests were found|no test files|running 0 tests|"
                      r"No examples found|No tests executed|No test is available|Total tests: 0|Tests run: 0|# tests 0|"
                      r"1\.\.0(?:\s|$)|0 runs, 0 assertions|\b0 (?:tests?|examples?|passing)\b|no tests to run|"
                      r"(?:^|\n)\s*(?:\u2139\s*)?tests\s+0\b|executed\s+0\s+of\s+0", text, re.I)
    # positive 表示「确实有测试执行过」；覆盖更多框架的通过/失败行，减少把真实运行误判为空跑。
    positive = re.search(r"(?:Ran|running) [1-9]\d* tests?|[1-9]\d* passed|[1-9]\d* examples?|"
                         r"[1-9]\d* passing|Tests run: [1-9]\d*|Total tests: [1-9]\d*|# tests [1-9]\d*|"
                         r"(?:^|\n)\s*(?:\u2139\s*)?tests\s+[1-9]\d*\b|"
                         r"(?m)^(?:ok|PASS|FAIL)\s|\bAll tests? passed\b|\b[1-9]\d* (?:failures?|errors?)\b", text, re.I)
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
    baseline_harness = set()
    explicit_checks = configured_check_files(baseline, checks)
    for parent, dirs, files in os.walk(baseline, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", ".rsi-agent", ".rsi-home"}]
        for name in files:
            src = Path(parent) / name
            relative = src.relative_to(baseline)
            if not (test_path(relative) or relative in explicit_checks or _harness_path(relative) or
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
            baseline_harness.add(relative)
            parts = relative.parts
            test_dir = next((i for i, part in enumerate(parts[:-1]) if part.lower() in {"test", "tests", "spec", "specs", "__tests__"}), None)
            protected.add(target.joinpath(*parts[:test_dir + 1]) if test_dir is not None else dst)
    for parent, dirs, files in os.walk(target, followlinks=False):
        for name in list(dirs):
            cargo_dir = Path(parent) / name
            if name == ".cargo" and cargo_dir.is_symlink():
                cargo_dir.unlink()
                dirs.remove(name)
        dirs[:] = [name for name in dirs if name not in {".git", ".rsi-agent", ".rsi-home"}]
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(target)
            if _harness_path(relative) and relative not in baseline_harness:
                path.unlink()
    return sorted(protected, key=str)


def _added_test_files(candidate):
    try:
        raw = git(candidate, "diff", "HEAD", "--diff-filter=A", "--name-only", "-z")
        untracked = git(candidate, "ls-files", "--others", "--exclude-standard", "-z")
    except RuntimeError:
        return []
    return sorted({path for path in (raw + "\0" + untracked).split("\0") if path and test_path(path)})


def _test_symbols(root, filename):
    path = Path(root) / filename
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    if path.suffix == ".go":
        return re.findall(r"(?m)^\s*func\s+(Test[A-Z]\w*)\s*\(", source)
    if path.suffix.lower() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return [match[1] for match in re.findall(r"\b(?:test|it)\s*\(\s*(['\"])([^'\"]+)\1", source)]
    if path.suffix == ".rs":
        return re.findall(r"(?m)#\[test\]\s*(?:#\[[^\]]+\]\s*)*fn\s+([A-Za-z_]\w*)", source)
    return []


def _isolated_test_check(checks, root, filename, regression_symbols=()):
    root = Path(root).resolve()
    target = (root / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    names = {str(name) for name in regression_symbols}
    for check in checks:
        argv = list(check.get("argv", []))
        if not argv or not all(isinstance(arg, str) for arg in argv):
            continue
        cwd = (root / check.get("cwd", ".")).resolve()
        try:
            cwd.relative_to(root)
        except ValueError:
            continue
        relative = Path(os.path.relpath(target, cwd)).as_posix()
        if ".." in Path(relative).parts:
            continue
        lower = [arg.lower() for arg in argv]
        command = None
        command_cwd = check.get("cwd", ".")
        suffix = Path(filename).suffix.lower()
        python_test = suffix == ".py"
        javascript_test = suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
        node_test = suffix in {".js", ".mjs", ".cjs"}

        if python_test and "-m" in lower:
            module_index = lower.index("-m")
            if module_index + 1 < len(lower) and lower[module_index + 1] == "unittest":
                options = unittest_discovery_options(argv)
                if options is not None:
                    original_start = options["start"] or "."
                    original_top = options["top"] or original_start
                    start_dir = (cwd / original_start).resolve()
                    top_dir = (cwd / original_top).resolve()
                    try:
                        start_dir.relative_to(root)
                        top_dir.relative_to(root)
                        target.parent.relative_to(start_dir)
                        start_dir.relative_to(top_dir)
                    except ValueError:
                        continue
                    # unittest discovery patterns match basenames, so refuse ambiguity.
                    if any(path != target for path in target.parent.rglob(target.name) if path.is_file()):
                        continue
                    start_arg = Path(os.path.relpath(target.parent, cwd)).as_posix()
                    command = (argv[:module_index + 2] + ["discover"] + options["flags"] +
                               ["-s", start_arg, "-t", original_top, "-p", target.name])
        if command is None and python_test and "-m" in lower:
            module_index = lower.index("-m")
            if module_index + 1 < len(lower) and lower[module_index + 1] == "pytest":
                command = argv[:module_index + 2] + [relative]
        if command is None and python_test and any(Path(item).name == "pytest" for item in lower):
            runner = next(index for index, item in enumerate(lower) if Path(item).name == "pytest")
            command = argv[:runner + 1] + [relative]
        if command is None and suffix == ".go" and any(Path(item).name == "go" for item in lower):
            flags = _go_test_options(argv)
            if names and flags is not None:
                go_index = next(index for index, item in enumerate(lower) if Path(item).name == "go")
                runner = lower.index("test", go_index)
                parent = Path(relative).parent.as_posix()
                package = "." if parent == "." else "./" + parent
                pattern = "^(" + "|".join(re.escape(name) for name in sorted(names)) + ")$"
                command = argv[:runner + 1] + flags + ["-run", pattern, package]
        if command is None and node_test and Path(lower[0]).name == "node":
            options = node_test_options(argv)
            if options is not None:
                command = [argv[0]] + options + [relative]
        if command is None and Path(lower[0]).name == "npm" and lower[1:] in (["test"], ["run", "test"]):
            package = root / check.get("cwd", ".") / "package.json"
            try:
                script = json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")
                runner = npm_test_runner(script)
            except (OSError, ValueError, TypeError):
                runner = None
            if runner and ((runner[0] == "node" and node_test) or
                           (runner[0] != "node" and javascript_test)):
                command = (runner + [relative] if runner[0] == "node" else
                           [argv[0], "exec", "--"] + runner + [relative])
        if command is None:
            for runner_name in ["jest", "vitest"]:
                if javascript_test and any(Path(item).name == runner_name for item in lower):
                    runner = next(index for index, item in enumerate(lower) if Path(item).name == runner_name)
                    command = argv[:runner + 1] + (["run"] if runner_name == "vitest" else []) + [relative]
                    break
        if command is None:
            for runner_name in ["phpunit", "rspec"]:
                if ((runner_name == "phpunit" and suffix == ".php") or
                        (runner_name == "rspec" and suffix == ".rb")) and \
                        any(Path(item).name == runner_name for item in lower):
                    runner = next(index for index, item in enumerate(lower) if Path(item).name == runner_name)
                    command = argv[:runner + 1] + [relative]
                    break
        if command:
            return {"name": "isolated regression: " + check.get("name", "test check"),
                    "argv": command, "cwd": command_cwd}
    return None


def _changed_files(candidate):
    try:
        tracked = git(candidate, "diff", "HEAD", "--name-only", "-z")
        untracked = git(candidate, "ls-files", "--others", "--exclude-standard", "-z")
    except RuntimeError:
        return None
    return sorted({path for path in (tracked + "\0" + untracked).split("\0") if path})


def _harness_path(path):
    name = Path(path).name.lower()
    parts = Path(path).parts
    cargo_config = name in {"config", "config.toml"} and ".cargo" in parts[:-1]
    return (cargo_config or name in MANIFESTS_LOWER or name in HARNESS_CONFIGS or name.startswith(HARNESS_CONFIG_PREFIXES) or
            name.startswith("requirements") and name.endswith((".txt", ".in")) or
            name.endswith((".csproj", ".fsproj", ".sln", ".props", ".targets")))


def _changed_harness(files, checks, baseline, added_tests=()):
    explicit_checks = configured_check_files(baseline, checks)
    additions = set(added_tests)
    return any(path not in additions and
               (test_path(path) or _harness_path(path) or Path(path) in explicit_checks)
               for path in files)


def _overlay_added_tests(candidate, target, paths):
    protected = []
    for relative in paths:
        source = Path(candidate) / relative
        destination = Path(target) / relative
        if source.is_symlink() or not source.is_file() or destination.exists():
            raise RuntimeError("Added regression test is not a new regular file")
        for parent in [destination.parent, *destination.parents]:
            if parent == Path(target).parent:
                break
            if parent.is_symlink():
                raise RuntimeError("Added regression test overlaps a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        protected.append(destination)
    return protected


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
                        overlay, seeds, hidden, test_first_files=(), test_first_check=None):
        target = copy_source(source, Path(directory) / label)
        self._seed_target(target, seeds, config, clear=True)
        harness = candidate if label == "candidate_full" else baseline
        protected = freeze_harness(harness, target, checks)
        if label == "baseline" and test_first_files:
            protected.extend(_overlay_added_tests(candidate, target, test_first_files))
        elif label in {"candidate", "candidate_full"} and test_first_files:
            for relative in test_first_files:
                path = target / relative
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("Added regression test is not a new regular file")
                protected.append(path)
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
        outcome = {"rows": rows}
        if test_first_check is not None and label in {"baseline", "candidate"}:
            focused = sandbox.check(test_first_check, target, cancel, protected, hidden)
            focused_text = focused["stdout"] + focused["stderr"]
            focused["empty_tests"] = empty_test_run(focused_text)
            focused["passed"] = focused["exit_code"] == 0 and not focused["error"] and not focused["empty_tests"]
            outcome["test_first_row"] = focused
        return outcome

    def _acceptance_stage(self, label, source, baseline, acceptance, directory, config, cancel, seeds, hidden):
        from .config import relative_path
        target = copy_source(source, Path(directory) / label)
        if (target / ".rsi-acceptance").exists():
            raise RuntimeError("Candidate overlaps independent acceptance namespace")
        protected = freeze_harness(baseline, target, acceptance["checks"])
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
        outcome = self._acceptance_stage("acceptance_baseline", baseline, baseline, acceptance, directory,
                                         execution_config, cancel, seeds, hidden)
        if "setup" in outcome:
            return {"decision": "insufficient_evidence",
                    "reason": "Independent acceptance environment preparation failed",
                    "setup": outcome["setup"], "rows": [],
                    "execution_environment": execution_identity}
        self._cache_put(cache_scope, key, outcome["rows"])
        return {**outcome, "execution_environment": execution_identity}

    def evaluate(self, baseline, candidate, checks, directory, config, cancel=None, overlay=None, seeds=None,
                 acceptance=None, acceptance_baseline=None, cache_scope=None, cache_base=None,
                 environment_digest=None, test_first=False, test_first_kind="behavior", changed_files=None):
        if not checks:
            return {"decision": "insufficient_evidence", "reason": "No executable acceptance checks. Configure checks before delivery.", "checks": []}
        results = {}
        hidden = [row["path"] for row in self.store.query("SELECT path FROM projects")]
        execution_config, execution_identity = (
            self._execution_environment(config) if cache_scope and cache_base else (config, None))
        if changed_files is None:
            changed_files = _changed_files(candidate)
        added_tests = _added_test_files(candidate) if test_first else []
        existing_test_files = ([path for path in git(baseline, "ls-files").splitlines() if test_path(path)]
                               if test_first else [])
        existing_test_symbols = ({symbol for path in existing_test_files
                                  for symbol in _test_symbols(baseline, path)} if test_first else set())
        regression_test_symbols = ([symbol for path in added_tests for symbol in _test_symbols(candidate, path)
                                   if symbol not in existing_test_symbols] if test_first else [])
        full_checks = list(checks)
        for check in detect(candidate)["checks"]:
            if not any(row["argv"] == check["argv"] and row.get("cwd", ".") == check.get("cwd", ".")
                       for row in full_checks):
                full_checks.append(check)
        changed_harness = _changed_harness(changed_files or [], full_checks, baseline, added_tests)
        if test_first and changed_harness:
            return {"decision": "insufficient_evidence",
                    "reason": "Test-first proof cannot modify the existing test harness", "checks": []}
        test_first_check = None
        if test_first:
            if len(added_tests) != 1:
                return {"decision": "insufficient_evidence",
                        "reason": "Test-first proof requires exactly one new regression test file", "checks": []}
            test_first_check = _isolated_test_check(checks, baseline, added_tests[0], regression_test_symbols)
            if test_first_check is None:
                return {"decision": "insufficient_evidence",
                        "reason": "Configured test runner cannot execute the new regression test in isolation",
                        "checks": []}
        reuse_full = (changed_files is not None and full_checks == list(checks) and not overlay and
                      not changed_harness)
        baseline_key = None if test_first else self._cache_key(
            cache_scope, cache_base, checks, execution_config, environment_digest, execution_identity)
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
                seeds, hidden, added_tests if test_first else (), test_first_check)
        if acceptance and "acceptance_baseline" not in results:
            physical = "acceptance_baseline"
            if acceptance_baseline is not None:
                physical += "_" + fingerprint({
                    "current": execution_identity,
                    "early": acceptance_baseline.get("execution_environment"),
                    "nonce": time.time_ns()})[:12]
            stages["acceptance_baseline"] = lambda: self._acceptance_stage(
                physical, baseline, baseline, acceptance, directory, execution_config, cancel, seeds, hidden)
        stages["candidate"] = lambda: self._standard_stage(
            "candidate", candidate, baseline, candidate, checks, directory, execution_config, cancel, overlay,
            seeds, hidden, added_tests if test_first else (), test_first_check)
        if not reuse_full:
            stages["candidate_full"] = lambda: self._standard_stage(
                "candidate_full", candidate, baseline, candidate, checks, directory, execution_config, cancel,
                overlay, seeds, hidden, added_tests if test_first else ())
        if acceptance:
            stages["acceptance_candidate"] = lambda: self._acceptance_stage(
                "acceptance_candidate", candidate, baseline, acceptance, directory, execution_config, cancel,
                seeds, hidden)
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
            if "test_first_row" in outcome:
                results["test_first_" + label] = [outcome["test_first_row"]]
        if reuse_full:
            results["candidate_full"] = list(results.get("candidate", []))
        if cached_baseline is None and "baseline" in results:
            self._cache_put(cache_scope, baseline_key, results["baseline"])
        if acceptance and "acceptance_baseline" in outcomes:
            self._cache_put(cache_scope, acceptance_key, results["acceptance_baseline"])
        passing = all(row["passed"] for label in ["candidate", "candidate_full", "acceptance_candidate"]
                      for row in results.get(label, []))
        unavailable = any(not row["passed"] and UNAVAILABLE_OUTPUT.search(row["stderr"])
            for label in ["candidate", "candidate_full", "acceptance_candidate"] for row in results.get(label, []))
        data = {"decision": "insufficient_evidence" if unavailable else "pass" if passing else "fail",
                "reason": "Regression and full candidate checks passed" if passing else "Candidate failed independent checks",
                "harness_hash": fingerprint(checks), "candidate_full_reused": reuse_full, "checks": results,
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
        elif test_first:
            from .workflow import improvement
            data["improvement"] = improvement(results, {
                "mode": "test_first", "change_kind": test_first_kind, "regression_test_files": added_tests,
                "existing_test_files": existing_test_files, "regression_test_symbols": regression_test_symbols})
            if passing and data["improvement"]["decision"] != "pass":
                data.update(decision="insufficient_evidence",
                            reason="Checks passed but the new regression test did not prove the planned change")
        if unavailable:
            data["reason"] = "Required check executable is unavailable; candidate remains unverified"
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
