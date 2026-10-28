import concurrent.futures
import copy
import json
import os
import re
import shutil
import time
import threading
import contextlib
from pathlib import Path

from .adapters import detect, test_path
from .build import BUILD_ID
from .config import fingerprint
from .gitops import copy_source, git
from .process import Sandbox, run_process
from .store import encode
from .workflow import (_go_test_options, environment_failure, node_test_options, npm_test_runner,
                       unittest_discovery_options, pytest_options)

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


def host_tool_versions(checks):
    """Probe only known host tools, never execute candidate-provided binaries."""
    import hashlib
    flags = {"python": "--version", "python3": "--version", "node": "--version", "npm": "--version",
             "git": "--version", "go": "version", "cargo": "--version", "rustc": "--version",
             "java": "-version", "mvn": "--version", "ruby": "--version", "php": "--version"}
    result = {}
    for name in sorted({check.get("argv", [""])[0] for check in checks if check.get("argv")}):
        if name not in flags:
            result[name] = {"state": "unavailable", "reason": "not_an_allowlisted_host_probe"}
            continue
        path = shutil.which(name)
        if not path:
            result[name] = {"state": "missing"}
            continue
        from .process import run_process as probe_process
        probe = probe_process([path, flags[name]], timeout=5)
        try:
            digest = hashlib.sha256(Path(path).resolve().read_bytes()).hexdigest()
        except OSError:
            digest = None
        result[name] = {"state": "available" if probe["exit_code"] == 0 and not probe["error"] else "unavailable",
                        "path": path, "binary_sha256": digest,
                        "version": (probe["stdout"] + probe["stderr"])[:4096].strip()}
    return result


def test_identities(text):
    """Record reported identities; opaque runners remain explicitly unverified."""
    executed, skipped = [], []
    if len(text) > 4 * 1024 * 1024:
        return {"state": "unavailable", "executed": [], "skipped": []}
    from xml.etree import ElementTree
    start = text.find("<testsuite")
    if start >= 0:
        for ending in ("</testsuites>", "</testsuite>"):
            end = text.rfind(ending)
            if end < start:
                continue
            try:
                root = ElementTree.fromstring(text[start:end + len(ending)])
            except ElementTree.ParseError:
                continue
            for case in root.iter("testcase"):
                identity = ".".join(filter(None, [case.get("classname"), case.get("name")]))
                skip = case.find("skipped")
                if skip is not None:
                    skipped.append({"id": identity, "reason": skip.get("message") or skip.text or "unspecified"})
                else:
                    executed.append(identity)
            break
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get("Test") and event.get("Package"):
            identity = event["Package"] + "/" + event["Test"]
            if event.get("Action") in ("pass", "fail"):
                executed.append(identity)
            elif event.get("Action") == "skip":
                skipped.append({"id": identity, "reason": "unspecified by go event"})
        match = re.match(r"^(test\S+ \([^)]+\)) \.\.\. (.*)$", line)
        if match:
            if match[2].startswith("skipped"):
                skipped.append({"id": match[1], "reason": match[2][7:].strip()})
            else:
                executed.append(match[1])
        match = re.match(r"^(\S+::\S+) (PASSED|FAILED|SKIPPED)(.*)$", line)
        if match:
            if match[2] == "SKIPPED":
                skipped.append({"id": match[1], "reason": match[3].strip() or "unspecified"})
            else:
                executed.append(match[1])
    return {"state": "reported" if executed or skipped else "unavailable",
            "executed": sorted(set(executed)), "skipped": skipped,
            "limitation": "Runner output is diagnostic; sealed check exit status remains authoritative"}


def verify_test_identities(rows, required):
    executed = {identity for row in rows for identity in row.get("test_identities", {}).get("executed", [])}
    missing = sorted(set(required) - executed)
    return {"valid": bool(required) and not missing, "required": required, "missing": missing,
            "basis": "runner_reported_exact_test_ids"}


def collect_test_artifacts(root, maximum=2 * 1024 * 1024):
    """Collect only fixed workspace-relative reports with a total byte cap."""
    import hashlib
    import stat
    root = Path(root).resolve()
    rows, remaining = {}, maximum
    for relative in ("junit.xml", "test-results.xml", "coverage.xml", "lcov.info", "coverage/lcov.info"):
        path = root / relative
        if any(parent.is_symlink() for parent in [path] + list(path.parents) if parent != root):
            rows[relative] = {"state": "rejected", "reason": "symlink"}
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            rows[relative] = {"state": "missing"}
            continue
        except OSError:
            rows[relative] = {"state": "rejected", "reason": "unreadable"}
            continue
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > remaining:
                rows[relative] = {"state": "rejected", "reason": "type_or_size_limit"}
                continue
            data = stream.read(remaining + 1)
        if len(data) > remaining:
            rows[relative] = {"state": "rejected", "reason": "size_limit"}
            continue
        remaining -= len(data)
        rows[relative] = {"state": "collected", "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "content": data.decode("utf-8", "replace")}
    return rows


def affected_checks(changed, checks, graph):
    """Close over reverse package dependencies; uncertainty always runs all checks."""
    if not changed or not isinstance(graph, dict) or not graph:
        return list(checks)
    names = {check["name"] for check in checks}
    for spec in graph.values():
        if (not isinstance(spec, dict) or not isinstance(spec.get("paths"), list)
                or not spec["paths"] or any(not isinstance(p, str) or not p or p.startswith("/") or ".." in Path(p).parts for p in spec["paths"])
                or not isinstance(spec.get("depends_on"), list) or not isinstance(spec.get("checks"), list)
                or any(dep not in graph for dep in spec["depends_on"]) or not set(spec["checks"]) <= names):
            return list(checks)
    selected = set()
    for path in changed:
        matches = {name for name, spec in graph.items() if any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in spec["paths"])}
        if not matches:
            return list(checks)
        selected.update(matches)
    while True:
        expanded = selected | {name for name, spec in graph.items() if set(spec["depends_on"]) & selected}
        if expanded == selected:
            break
        selected = expanded
    needed = {name for package in selected for name in graph[package]["checks"]}
    return [check for check in checks if check["name"] in needed] or list(checks)


def structured_test_summary(text):
    outcomes = {}
    detected = False
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("Action") in ("pass", "fail", "skip") and "Package" in event:
            detected = True
            if isinstance(event.get("Test"), str) and isinstance(event["Package"], str):
                outcomes[(event["Package"], event["Test"])] = event["Action"]
    return {"format": "go_test_json", "counting": "top_level_tests",
            "executed": sum("/" not in name and action in ("pass", "fail") for (_, name), action in outcomes.items()),
            "subtests": sum("/" in name and action in ("pass", "fail") for (_, name), action in outcomes.items()),
            "skipped": sum(action == "skip" for action in outcomes.values())} if detected else None


def junit_test_summary(text):
    """Read bounded JUnit XML from stdout without trusting it for gate decisions."""
    from xml.etree import ElementTree
    start = text.find("<testsuite")
    if start < 0 or len(text) > 4 * 1024 * 1024:
        return None
    for ending in ("</testsuites>", "</testsuite>", "/>"):
        end = text.rfind(ending)
        if end < start:
            continue
        try:
            root = ElementTree.fromstring(text[start:end + len(ending)])
        except ElementTree.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        if not suites:
            continue
        try:
            values = {name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
                      for name in ("tests", "failures", "errors", "skipped")}
            if any(value < 0 for value in values.values()):
                return None
        except ValueError:
            continue
        return {"format": "junit", "executed": values["tests"], **values}
    return None


def clarity_test_summary(text):
    """Recognize bounded pytest CLI summaries without parsing arbitrary output."""
    match = re.search(r"(?m)^=+ ([1-9]\d*) passed(?:, (\d+) failed)?(?:, (\d+) skipped)? in ",
                      text)
    if not match:
        return None
    passed, failed, skipped = (int(value or 0) for value in match.groups())
    return {"format": "pytest_cli", "executed": passed + failed + skipped,
            "passed": passed, "failed": failed, "skipped": skipped}


def empty_test_run(text):
    structured = structured_test_summary(text)
    if structured is not None:
        return structured["executed"] == 0
    empty = re.search(r"Ran 0 tests|no tests ran|No tests found|No tests were found|no test files|running 0 tests|"
                      r"No examples found|No tests executed|No test is available|Total tests: 0|Tests run: 0|# tests 0|"
                      r"1\.\.0(?:\s|$)|0 runs, 0 assertions|\b0 (?:tests?|examples?|passing)\b|no tests to run|"
                      r"(?:^|\n)\s*(?:\u2139\s*)?tests\s+0\b|executed\s+0\s+of\s+0", text, re.I)
    # positive 表示「确实有测试执行过」；覆盖更多框架的通过/失败行，减少把真实运行误判为空跑。
    # 行首锚点用 (?:^|\n) 而不是内联 (?m)：Python 3.11 起禁用位于模式中段的内联旗标。
    positive = re.search(r"(?:Ran|running) [1-9]\d* tests?|[1-9]\d* passed|[1-9]\d* examples?|"
                         r"[1-9]\d* passing|Tests run: [1-9]\d*|Total tests: [1-9]\d*|# tests [1-9]\d*|"
                         r"(?:^|\n)\s*(?:\u2139\s*)?tests\s+[1-9]\d*\b|test result: (?:ok|FAILED)\b|"
                         r"(?:^|\n)\s*(?:ok|PASS|FAIL)\s|\bAll tests? passed\b|\b[1-9]\d* (?:failures?|errors?)\b|"
                         r"[✓✔]\s+[1-9]\d*|[1-9]\d*\s+passed\s+in\s+[\d.]+", text, re.I)
    return bool(empty and not positive)


def configured_check_files(baseline, checks):
    root = Path(baseline).resolve()
    files = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("argv"), list):
            continue
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
            if src.is_symlink():
                try:
                    if not src.resolve().exists():
                        continue
                except OSError:
                    continue
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
                options = pytest_options(argv[module_index + 2:])
                command = argv[:module_index + 2] + options + [relative] if options is not None else None
        if command is None and python_test and any(Path(item).name == "pytest" for item in lower):
            runner = next(index for index, item in enumerate(lower) if Path(item).name == "pytest")
            options = pytest_options(argv[runner + 1:])
            command = argv[:runner + 1] + options + [relative] if options is not None else None
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
            timeout = check.get("timeout_seconds", 300)
            return {"name": "isolated regression: " + check.get("name", "test check"),
                    "argv": command, "cwd": command_cwd,
                    "timeout_seconds": timeout}
    return None


def _changed_files(candidate):
    try:
        tracked = git(candidate, "diff", "HEAD", "--name-only", "-z")
        untracked = git(candidate, "ls-files", "--others", "--exclude-standard", "-z")
    except RuntimeError:
        return None
    return sorted({path for path in (tracked + "\0" + untracked).split("\0") if path})


def _committed_changes(baseline, candidate):
    """Files committed on top of the baseline tree, even when the worktree is clean."""
    try:
        base = git(baseline, "rev-parse", "HEAD")
        head = git(candidate, "rev-parse", "HEAD")
        if base == head:
            return []
        return sorted({path for path in git(candidate, "diff", "--name-only", "-z", base, head, "--").split("\0") if path})
    except RuntimeError:
        return None


def _harness_differs(baseline, candidate, checks):
    """True when a file executed by a configured check differs between trees."""
    for relative in configured_check_files(baseline, checks):
        left, right = Path(baseline) / relative, Path(candidate) / relative
        try:
            if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
                return True
        except OSError:
            return True
    return False


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

    def validate_controls(self, source, checks, controls, directory, config, cancel=None):
        """Execute host-owned criterion counterexamples through the same sandbox gate."""
        from .config import relative_path
        if not controls or len(controls) > 32 or not checks:
            return {"decision": "insufficient_evidence", "controls": []}
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=False)
        sandbox = Sandbox(self.store.root, config)
        def execute(target):
            rows = []
            for setup in config["environment"]["setup"]:
                result = sandbox.check(setup, target, cancel)
                if result["error"] or result["exit_code"]:
                    return [], False
            for check in checks:
                result = sandbox.check(check, target, cancel)
                result["passed"] = result["exit_code"] == 0 and not result["error"] and not empty_test_run(result["stdout"] + result["stderr"])
                rows.append(result)
            return rows, True
        positive, ready = execute(copy_source(source, root / "positive"))
        outcomes = []
        seen = set()
        for index, control in enumerate(controls):
            if (not isinstance(control, dict) or not control.get("id") or control["id"] in seen
                    or not control.get("source") or not control.get("criterion") or not control.get("files")):
                raise ValueError("Controls require unique IDs, source, criterion and bounded fixture files")
            seen.add(control["id"])
            target = copy_source(source, root / str(index))
            total = 0
            for item in control["files"]:
                path = target / relative_path(item["path"])
                if any(p.is_symlink() for p in [path] + list(path.parents)):
                    raise ValueError("Control overlaps symlink")
                total += len(item["content"].encode())
                if total > 1024 * 1024:
                    raise ValueError("Control fixture too large")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["content"], encoding="utf-8")
            rows, prepared = execute(target)
            detected = prepared and any(row["exit_code"] not in (None, 0) and not row["error"]
                                        and not environment_failure(row) for row in rows)
            outcomes.append({"id": control["id"], "source": control["source"], "criterion": control["criterion"],
                             "fixture_hash": fingerprint(control["files"]), "detected": detected, "rows": rows})
        passed = ready and all(row["passed"] for row in positive) and all(row["detected"] for row in outcomes)
        return {"decision": "pass" if passed else "insufficient_evidence", "positive": positive, "controls": outcomes,
                "limitation": "Harness sensitivity only; not a substitute for real requirements acceptance"}

    def seal_checkpoint(self, name, inputs, result, *, effect):
        if effect != "read_only":
            raise ValueError("Only explicitly read-only stages can be sealed")
        data = {"build": BUILD_ID, "inputs": copy.deepcopy(inputs), "result": copy.deepcopy(result), "effect": effect}
        self.store.put_setting("sealed-read-checkpoint:" + name, {**data, "sha256": fingerprint(data)})

    def reuse_checkpoint(self, name, inputs):
        entry = self.store.setting("sealed-read-checkpoint:" + name)
        if not isinstance(entry, dict):
            return None
        data = {key: value for key, value in entry.items() if key != "sha256"}
        if (data.get("effect") != "read_only" or data.get("build") != BUILD_ID
                or data.get("inputs") != inputs or fingerprint(data) != entry.get("sha256")):
            return None
        return copy.deepcopy(data["result"])

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
        entry = self.store.setting("evaluation-baseline-entry:" + scope + ":" + key)
        if not entry:
            # One-time compatibility for caches written before per-key entries.
            entries = self.store.setting("evaluation-baseline:" + scope, {})
            entry = entries.get(key) if isinstance(entries, dict) else None
        return copy.deepcopy(entry["rows"]) if entry and isinstance(entry.get("rows"), list) else None

    def _cache_put(self, scope, key, rows):
        if not key:
            return
        if any(row.get("error") or UNAVAILABLE_OUTPUT.search(row.get("stderr") or "") for row in rows):
            return
        compact = []
        for row in rows:
            item = {field: value for field, value in row.items() if field != "stdout"}
            item["environment_error"] = environment_failure(row)
            if item.get("stderr"):
                item["stderr"] = item["stderr"][-CACHE_STDERR_LIMIT:]
            item["cached"] = True
            compact.append(item)
        # A CAS upsert per key prevents concurrent project checks from
        # dropping each other's independently computed cache entries.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM settings WHERE key=?",
                             ("evaluation-baseline-entry:" + scope + ":" + key,)).fetchone()
            previous = json.loads(row["value"]) if row else None
            if previous and previous.get("at", 0) > time.time() - 60:
                return
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                       ("evaluation-baseline-entry:" + scope + ":" + key,
                        encode({"rows": compact, "at": time.time()})))
        entries = self.store.query(
            "SELECT key,value FROM settings WHERE key>=? AND key<? ORDER BY key",
            ("evaluation-baseline-entry:" + scope + ":", "evaluation-baseline-entry:" + scope + ";"))
        if len(entries) > BASELINE_CACHE_LIMIT:
            stale = sorted(entries, key=lambda item: json.loads(item["value"]).get("at", 0))
            for item in stale[:len(entries) - BASELINE_CACHE_LIMIT]:
                self.store.execute("DELETE FROM settings WHERE key=?", (item["key"],))

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
                        overlay, seeds, hidden, test_first_files=(), test_first_check=None, run_checks=None):
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
        if run_checks is not None:
            # The supplementary stage executes only checks that the candidate
            # stage did not run; the configured checks are reused from there.
            effective_checks = [check for check in effective_checks if check in run_checks]
        rows = []
        for check in effective_checks:
            result = sandbox.check(check, target, cancel, protected, hidden)
            text = result["stdout"] + result["stderr"]
            empty = empty_test_run(text)
            result["passed"] = result["exit_code"] == 0 and not result["error"] and not empty
            result["empty_tests"] = empty
            result["test_summary"] = (structured_test_summary(text) or junit_test_summary(text) or
                                      clarity_test_summary(text))
            result["test_identities"] = test_identities(text)
            result["artifacts"] = collect_test_artifacts(target)
            rows.append(result)
        outcome = {"rows": rows}
        if test_first_check is not None and label in {"baseline", "candidate"}:
            outcome["test_first_rows"] = []
            for check in test_first_check if isinstance(test_first_check, list) else [test_first_check]:
                focused = sandbox.check(check, target, cancel, protected, hidden)
                focused_text = focused["stdout"] + focused["stderr"]
                focused["empty_tests"] = empty_test_run(focused_text)
                focused["passed"] = focused["exit_code"] == 0 and not focused["error"] and not focused["empty_tests"]
                outcome["test_first_rows"].append(focused)
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
            result["test_identities"] = test_identities(result["stdout"] + result["stderr"])
            result["artifacts"] = collect_test_artifacts(target)
            rows.append(result)
        return {"rows": rows}

    @staticmethod
    def _run_stages(stages, limit, stop=None):
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
                if stop:
                    stop.set()
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
        from .resources import execution_resources
        exclusive = acceptance and acceptance.get("change_kind") == "performance"
        with execution_resources.acquire(exclusive=True, cancel=cancel, priority="benchmark") if exclusive else contextlib.nullcontext():
            return self._evaluate(baseline, candidate, checks, directory, config, cancel, overlay, seeds,
                                  acceptance, acceptance_baseline, cache_scope, cache_base,
                                  environment_digest, test_first, test_first_kind, changed_files)

    def _evaluate(self, baseline, candidate, checks, directory, config, cancel=None, overlay=None, seeds=None,
                  acceptance=None, acceptance_baseline=None, cache_scope=None, cache_base=None,
                  environment_digest=None, test_first=False, test_first_kind="behavior", changed_files=None):
        stage_stop = threading.Event()
        original_cancel = cancel
        cancel = lambda: stage_stop.is_set() or bool(original_cancel and original_cancel())
        if not checks:
            return {"decision": "insufficient_evidence", "reason": "No executable acceptance checks. Configure checks before delivery.", "checks": []}
        results = {}
        hidden = [row["path"] for row in self.store.query("SELECT path FROM projects")]
        execution_config, execution_identity = (
            self._execution_environment(config) if cache_scope and cache_base else (config, None))
        if changed_files is None:
            changed_files = _changed_files(candidate)
        elif not changed_files:
            # A clean snapshot reports no working-tree delta even when the
            # candidate committed test or harness edits; compare committed
            # history so frozen regression evidence cannot be reused.
            committed = _committed_changes(baseline, candidate)
            if committed:
                changed_files = committed
        added_tests = _added_test_files(candidate) if test_first else []
        existing_test_files = ([path for path in git(baseline, "ls-files", "-z").split("\0") if path and test_path(path)]
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
        # Without a repository the harness comparison cannot prove that the
        # candidate did not rewrite tests, so compare the files that the
        # configured checks actually execute.
        additions = (full_checks if changed_harness or _harness_differs(baseline, candidate, full_checks) else
                     [check for check in full_checks if check not in checks])
        if test_first and changed_harness:
            return {"decision": "insufficient_evidence",
                    "reason": "Test-first proof cannot modify the existing test harness", "checks": []}
        test_first_check = None
        if test_first:
            if not 1 <= len(added_tests) <= 8:
                return {"decision": "insufficient_evidence",
                        "reason": "Test-first proof requires 1..8 new regression test files", "checks": []}
            test_first_check = [_isolated_test_check(checks, baseline, filename, regression_test_symbols) for filename in added_tests]
            if any(check is None for check in test_first_check):
                return {"decision": "insufficient_evidence",
                        "reason": "Configured test runner cannot execute the new regression test in isolation",
                        "checks": []}
        reuse_full = (changed_files is not None and full_checks == list(checks) and not overlay and
                      not changed_harness)
        baseline_key = None if test_first or overlay else self._cache_key(
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
                "candidate_full", candidate, baseline, candidate, full_checks, directory, execution_config, cancel,
                overlay, seeds, hidden, added_tests if test_first else (), run_checks=additions)
        if acceptance:
            stages["acceptance_candidate"] = lambda: self._acceptance_stage(
                "acceptance_candidate", candidate, baseline, acceptance, directory, execution_config, cancel,
                seeds, hidden)
        # Benchmarks have exclusive process-wide access; ordinary checks share
        # the global execution capacity while retaining stage concurrency.
        outcomes = self._run_stages(stages, 1 if acceptance and acceptance.get("change_kind") == "performance"
                                    else int(config.get("limits", {}).get("concurrency", 1)), stop=stage_stop)
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
            if "test_first_rows" in outcome:
                results["test_first_" + label] = outcome["test_first_rows"]
        if reuse_full:
            results["candidate_full"] = list(results.get("candidate", []))
        elif "candidate_full" in results:
            # Keep full_checks order: reused checks come from the candidate
            # stage, supplement checks from this stage. Names repeat across
            # nested modules, so pair them with argv and cwd.
            def identity(row):
                return (row["name"], tuple(row["argv"]), row.get("cwd", "."))
            own = {identity(row): row for row in results["candidate_full"]}
            previous = {identity(row): row for row in results.get("candidate", [])}
            merged = [own.get(identity(check)) or previous.get(identity(check)) for check in full_checks]
            results["candidate_full"] = [row for row in merged if row]
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
                "execution_environment": execution_identity, "host_tools": host_tool_versions(checks)}
        if seeds:
            from .process import scrub_tree
            scrub_tree(seeds)
        if (acceptance and acceptance.get("contract", {}).get("check_dependencies")):
            data["check_dependency_analysis"] = {
                "affected": [row["name"] for row in affected_checks(
                    changed_files, checks, acceptance["contract"]["check_dependencies"])],
                "execution": "full_checks_retained_until_graph_is_independently_validated"}
        if acceptance:
            from .workflow import improvement
            data["improvement"] = improvement(results, acceptance)
            if passing and data["improvement"]["decision"] != "pass":
                data.update(decision="insufficient_evidence", reason="Checks passed but task-specific improvement is unproven")
            data["acceptance_hash"] = acceptance["hash"]
            data["criteria"] = acceptance["criteria"]
            data["coverage"] = [{"name": check["name"], "criteria": check["criteria"]} for check in acceptance["spec"]["checks"]]
            data["contract"] = acceptance.get("contract")
            required = (acceptance.get("contract") or {}).get("required_test_ids")
            if required:
                data["test_identity_verification"] = verify_test_identities(results.get("acceptance_candidate", []), required)
                if not data["test_identity_verification"]["valid"]:
                    data.update(decision="insufficient_evidence", reason="Required regression test IDs were not executed")
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
