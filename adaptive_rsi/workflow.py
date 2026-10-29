"""Task evidence contracts shared by production and experiment runs."""
import json
import math
import re
import shlex
from pathlib import Path


ENVIRONMENT_FAILURE = re.compile(
    r"ModuleNotFoundError|ImportError|LoadError|GemNotFound|ERR_MODULE_NOT_FOUND|Cannot find module|"
    r"No module named|SyntaxError|command not found|No such file or directory|compilation failed|"
    r"failed to compile|cannot find symbol|unresolved import|linker command failed|failed to resolve package",
    re.I,
)

UNITTEST_DISCOVERY_VALUE_FLAGS = {"-s", "--start-directory", "-t", "--top-level-directory", "-p", "--pattern"}
UNITTEST_DISCOVERY_BOOL_FLAGS = {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast", "--locals",
                                 "--buffer", "--catch"}
NODE_TEST_VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
                         "--conditions", "--test-reporter", "--test-reporter-destination",
                         "--test-concurrency", "--test-timeout"}
NODE_TEST_BOOL_FLAGS = {"--test", "--experimental-strip-types"}


def pytest_options(args):
    """Preserve supported pytest configuration while replacing collection paths."""
    values = {"-c", "--rootdir", "--confcutdir", "--import-mode", "-o", "--override-ini", "-p",
              "--basetemp", "--tb", "--maxfail", "--color", "--capture", "--assert", "--junitxml"}
    booleans = {"-q", "-qq", "-v", "-vv", "-vvv", "-s", "-x", "--disable-warnings", "--strict-markers",
                "--strict-config", "--continue-on-collection-errors", "--noconftest", "--showlocals"}
    result = []
    index = 0
    while index < len(args):
        item = args[index]
        key, separator, value = item.partition("=")
        if key in values:
            result.append(item)
            if not separator:
                index += 1
                if index >= len(args):
                    return None
                result.append(args[index])
        elif item in booleans:
            result.append(item)
        elif item.startswith("-"):
            # Filters (-k/-m), collect-only and plugin flags can hide the new test.
            return None
        index += 1
    return result


def unittest_discovery_options(argv):
    """Parse the unittest options we can safely retain while targeting one file."""
    module_index = next((index for index, item in enumerate(argv[:-1])
                         if item == "-m" and argv[index + 1] == "unittest"), None)
    if module_index is None:
        return None
    args = list(argv[module_index + 2:])
    if args[:1] == ["discover"]:
        args = args[1:]
    elif any(not item.startswith("-") for item in args):
        return None
    options = {"start": None, "top": None, "pattern": None, "flags": []}
    names = {"-s": "start", "--start-directory": "start", "-t": "top", "--top-level-directory": "top",
             "-p": "pattern", "--pattern": "pattern"}
    index = 0
    while index < len(args):
        item = args[index]
        key, separator, value = item.partition("=")
        if key in names:
            if not separator:
                index += 1
                if index >= len(args):
                    return None
                value = args[index]
            if not value:
                return None
            options[names[key]] = value
        elif item in UNITTEST_DISCOVERY_BOOL_FLAGS:
            options["flags"].append(item)
        else:
            return None
        index += 1
    return options


def node_test_options(argv):
    """Retain supported Node setup flags and reject options we cannot rewrite safely."""
    if not argv or Path(str(argv[0])).name.lower() != "node":
        return None
    options = []
    has_test = False
    index = 1
    while index < len(argv):
        item = str(argv[index])
        key, separator, value = item.partition("=")
        if key in NODE_TEST_VALUE_FLAGS:
            if separator:
                if not value:
                    return None
                options.append(item)
            else:
                index += 1
                if index >= len(argv) or not str(argv[index]):
                    return None
                options.extend([item, str(argv[index])])
        elif item in NODE_TEST_BOOL_FLAGS:
            options.append(item)
            has_test = has_test or item == "--test"
        elif item == "--" or item.startswith("-"):
            return None
        index += 1
    return options if has_test else None


def npm_test_runner(script):
    """Recognize only npm scripts whose test target can be replaced with one file."""
    try:
        tokens = shlex.split(str(script))
    except ValueError:
        return None
    if not tokens:
        return None
    runner = Path(tokens[0]).name.lower()
    if runner == "node":
        return ["node", "--test"] if tokens[1:] == ["--test"] else None
    if runner not in {"jest", "vitest", "mocha"}:
        return None
    args = tokens[1:]
    command = [runner]
    if runner == "vitest" and args[:1] == ["run"]:
        command.append("run")
        args = args[1:]
    if any(not arg.startswith("-") or arg == "--" for arg in args):
        return None
    return command + args


GO_TEST_VALUE_FLAGS = {"-benchtime", "-blockprofile", "-blockprofilerate", "-covermode",
                       "-coverpkg", "-coverprofile", "-cpu", "-cpuprofile", "-gcflags", "-ldflags",
                       "-memprofile", "-memprofilerate", "-mod", "-modfile", "-mutexprofile",
                       "-mutexprofilefraction", "-outputdir",
                       "-p", "-parallel", "-run", "-tags", "-timeout", "-trace", "-vet",
                       "-shuffle"}
GO_TEST_BOOL_FLAGS = {"-json", "-race", "-v", "-cover", "-failfast",
                      "-benchmem", "-msan", "-asan"}
GO_TEST_REJECT_FLAGS = {"-exec", "-bench", "-fuzz", "-list", "-c", "-i", "-n", "-overlay"}
def _go_test_options(argv):
    lower = [str(item).lower() for item in argv]
    go_index = next((index for index, item in enumerate(lower) if Path(item).name == "go"), None)
    if go_index is None or "test" not in lower[go_index + 1:]:
        return None
    test_index = lower.index("test", go_index + 1)
    flags = []
    args = argv[test_index + 1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in GO_TEST_REJECT_FLAGS or any(arg.startswith(flag + "=") for flag in GO_TEST_REJECT_FLAGS):
            return None
        if arg == "--":
            return None
        if arg in GO_TEST_VALUE_FLAGS:
            if index + 1 == len(args):
                return None
            if arg != "-run":
                flags.extend([arg, args[index + 1]])
            index += 2
        elif arg.startswith("-run="):
            index += 1
        elif arg.startswith("-") and "=" in arg:
            flag = arg.split("=", 1)[0]
            if flag not in GO_TEST_VALUE_FLAGS | GO_TEST_BOOL_FLAGS:
                return None
            if flag not in {"-run", "-skip"}:
                flags.append(arg)
            index += 1
        elif arg in GO_TEST_BOOL_FLAGS:
            flags.append(arg)
            index += 1
        elif arg.startswith("-"):
            return None
        else:
            index += 1
    return flags


def supports_test_first(checks, root=None):
    for check in checks:
        argv = [str(item).lower() for item in check.get("argv", [])]
        if not argv:
            continue
        if "-m" in argv:
            module_index = argv.index("-m")
            if module_index + 1 < len(argv) and argv[module_index + 1] == "pytest":
                return True
            if module_index + 1 < len(argv) and argv[module_index + 1] == "unittest" and \
                    unittest_discovery_options(check.get("argv", [])) is not None:
                return True
        names = [Path(item).name for item in argv]
        if any(item in {"pytest", "phpunit", "rspec", "jest", "vitest"} for item in names):
            return True
        if any(names[index] == "go" and names[index + 1] == "test" and _go_test_options(argv) is not None
               for index in range(len(names) - 1)):
            return True
        if names[0] == "node" and node_test_options(check.get("argv", [])) is not None:
            return True
        if names[0] == "npm" and root is not None and argv[1:] in (["test"], ["run", "test"]):
            package = Path(root) / check.get("cwd", ".") / "package.json"
            try:
                script = str(json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")).lower()
            except (OSError, ValueError, TypeError):
                script = ""
            if npm_test_runner(script):
                return True
    return False


def environment_failure(row):
    """Keep environment/setup failures distinguishable after cache compaction."""
    if row.get("environment_error"):
        return True
    return bool(ENVIRONMENT_FAILURE.search(
        (row.get("stdout") or "") + "\n" + (row.get("stderr") or "")))



def change_kind(task, plan=None):
    # Only explicit behavior-preserving requests can use the preservation contract.
    title = (task["title"] + "\n" + task.get("body", "")).lower()
    declared = (plan or {}).get("change_kind")
    if declared == "performance" or any(word in title for word in
            ["benchmark", "latency", "throughput", "speedup", "性能", "提速", "耗时", "吞吐", "延迟"]):
        return "performance"
    if declared in ["documentation", "configuration", "ui"] and not any(
            word in title for word in ["bug", "broken", "incorrect", "修复", "缺陷", "失效", "异常"]):
        return declared
    if any(word in title for word in ["performance", "benchmark", "optimize", "optimized", "optimization",
                                      "optimise", "optimised", "latency", "throughput", "speedup", "faster",
                                      "性能", "优化", "提速", "耗时", "吞吐"]):
        return "performance"
    if any(word in title for word in ["fix", "bug", "broken", "incorrect", "修复", "错误", "缺陷", "失效", "异常"]):
        return "behavior"
    if declared == "refactor" and not any(word in title for word in ["refactor", "重构", "保持行为不变", "preserve behavior"]):
        return "behavior"
    if declared in ["behavior", "performance", "refactor", "documentation", "configuration", "ui"]:
        return declared
    if any(word in title for word in ["refactor", "重构", "保持行为不变"]):
        return "refactor"
    if any(word in title for word in ["performance", "benchmark", "性能", "提速", "降低", "延迟", "吞吐"]):
        return "performance"
    return "behavior"


DOCUMENTATION_DIRECTORIES = {"docs", "doc", "documentation"}
DOCUMENTATION_SUFFIXES = {".md", ".mdx", ".markdown", ".rst", ".adoc"}
DOCUMENTATION_NAMES = {"readme", "changelog", "changes", "contributing", "code_of_conduct", "security",
                       "support", "license", "notice", "authors"}


def documentation_path(path):
    """Documentation is decided by clear path rules, never by parsing comments.

    A source file whose only changed lines are comments remains a source file:
    mixed patches must keep the normal acceptance gate instead of claiming a
    documentation-only change.
    """
    from pathlib import PurePosixPath
    value = str(path)
    if "\\" in value:
        # Git paths use '/', so a backslash is literal path text, not a separator.
        return False
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return False
    parts = candidate.parts
    if parts[0].lower() in DOCUMENTATION_DIRECTORIES:
        return candidate.suffix.lower() in DOCUMENTATION_SUFFIXES | {".txt", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}
    if candidate.suffix.lower() in DOCUMENTATION_SUFFIXES:
        return True
    if len(parts) == 1 and (candidate.name.lower() in DOCUMENTATION_NAMES or
                            candidate.suffix.lower() == ".txt" and candidate.stem.lower() in DOCUMENTATION_NAMES):
        return True
    return False


def documentation_only(files):
    paths = list(files)
    return bool(paths) and all(documentation_path(path) for path in paths)


def improvement(results, acceptance):
    if not acceptance:
        return {"decision": "not_requested", "kind": "regression_only"}
    if acceptance.get("mode") == "test_first":
        before = results.get("test_first_baseline", [])
        after = results.get("test_first_candidate", [])
        test_files = acceptance.get("regression_test_files", [])
        if not 1 <= len(test_files) <= 8 or not before or len(before) != len(after):
            return {"decision": "insufficient_evidence", "kind": acceptance["change_kind"]}
        improved = []
        proven = len(before) == len(test_files)
        for index, (old, new) in enumerate(zip(before, after)):
            same = (str(old.get("name", "")).startswith("isolated regression:") and
                    old.get("name") == new.get("name") and old.get("argv") == new.get("argv") and
                    old.get("cwd", ".") == new.get("cwd", "."))
            valid_before = (old.get("exit_code") is not None and not old.get("error") and
                            not old.get("empty_tests") and not environment_failure(old))
            passed = new.get("passed") is True and new.get("exit_code") == 0 and not new.get("error") and not new.get("empty_tests")
            proven = proven and same and valid_before and passed
            if old.get("exit_code") not in (None, 0):
                improved.append(index)
        proven = proven and bool(improved)
        return {"decision": "pass" if proven else "insufficient_evidence",
                "kind": acceptance["change_kind"], "improved_checks": improved if proven else [],
                "regression_test_files": test_files,
                "basis": "isolated_new_test_fails_baseline_and_passes_candidate"}
    before = results.get("acceptance_baseline", [])
    after = results.get("acceptance_candidate", [])
    kind = acceptance.get("change_kind", "behavior")
    if not before or len(before) != len(after) or not all(row["passed"] for row in after):
        return {"decision": "insufficient_evidence", "kind": kind}
    if kind in ["refactor", "preservation"]:
        preserved = all(row["passed"] for row in before)
        return {"decision": "pass" if preserved else "insufficient_evidence", "kind": kind,
                "basis": "preserved_behavior_requires_maintainability_review"}
    if kind == "performance":
        threshold = acceptance.get("contract", {}).get("threshold")
        if not threshold:
            return {"decision": "insufficient_evidence", "kind": kind}
        import statistics
        contract = acceptance.get("contract", {})
        repeats = contract.get("repetitions", 1)
        loads = contract.get("load_conditions", [None])
        if type(repeats) is not int or not 1 <= repeats <= 100 or not isinstance(loads, list) or not loads or len(loads) > 20:
            return {"decision": "insufficient_evidence", "kind": kind, "basis": "invalid_measurement_plan"}
        def measurements(rows, rule, load):
            values = []
            for row in rows:
                for line in row.get("stdout", "").splitlines():
                    try:
                        sample = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(sample, dict) and sample.get("metric") == rule["metric"]
                            and sample.get("unit") == rule["unit"] and sample.get("load") == load):
                        value = sample.get("value")
                        if type(value) in [int, float] and math.isfinite(value):
                            values.append(value)
            return values
        guardrails, estimates, passed = [], [], True
        rules = [threshold] + contract.get("guardrails", [])
        for index, rule in enumerate(rules):
            if (not isinstance(rule, dict) or not rule.get("metric") or not rule.get("unit")
                    or rule.get("direction") not in ("lower", "higher")
                    or any(type(rule.get(k)) not in (int, float) or not math.isfinite(rule[k]) for k in ("target", "tolerance"))
                    or rule["tolerance"] < 0):
                return {"decision": "insufficient_evidence", "kind": kind, "basis": "invalid_guardrail"}
            for load in loads:
                old, new = measurements(before, rule, load), measurements(after, rule, load)
                if len(old) != repeats or len(new) != repeats:
                    return {"decision": "insufficient_evidence", "kind": kind,
                            "basis": "one_matching_measurement_required_per_version" if repeats == 1 else "planned_repeated_measurements_required"}
                target, tolerance = rule["target"], rule["tolerance"]
                # Every planned measurement must satisfy the target. Disjoint ranges
                # conservatively reject gains explainable by observed run noise.
                lower = rule["direction"] == "lower"
                within = max(new) <= target + tolerance if lower else min(new) >= target - tolerance
                gain = (min(old) - max(new) > tolerance if lower else min(new) - max(old) > tolerance)
                if index:
                    gain = max(new) <= max(old) + tolerance if lower else min(new) >= min(old) - tolerance
                good = within and gain
                passed = passed and good
                item = {"metric": rule["metric"], "load": load, "passed": good,
                        "before": statistics.mean(old), "after": statistics.mean(new),
                        "before_range": [min(old), max(old)], "after_range": [min(new), max(new)],
                        "before_stdev": statistics.stdev(old) if repeats > 1 else None,
                        "after_stdev": statistics.stdev(new) if repeats > 1 else None, "samples": repeats}
                (guardrails if index else estimates).append(item)
        return {"decision": "pass" if passed else "insufficient_evidence", "kind": kind,
                "before": estimates[0]["before"], "after": estimates[0]["after"], "threshold": threshold,
                "guardrails": guardrails, "uncertainty": estimates,
                "noise_warning": "One sample cannot characterize noise" if repeats == 1 else "Observed ranges only; no causal or population confidence claim",
                "basis": "independent_benchmark_measurements"}
    # An unrelated import/setup failure is not evidence of an observed improvement.
    import re
    changed = [i for i, (old, new) in enumerate(zip(before, after)) if not old["passed"] and new["passed"]
               and not old.get("error") and old.get("exit_code", 0) != 0
               and not environment_failure(old)]
    return {"decision": "pass" if changed else "insufficient_evidence", "kind": kind,
            "improved_checks": changed, "basis": "independent_behavior_before_after"}


def evidence_contract(task, plan):
    kind = "refactor" if task["source"] == "rollback" else change_kind(task, plan)
    requirements = {
        "behavior": "Reproduce the reported behavior before the change and demonstrate it works afterward.",
        "performance": "Emit one JSON measurement per version: metric, unit, value. Use the frozen target and tolerance.",
        "refactor": "Preserve passing behavior and verify concrete maintainability criteria in independent review.",
        "documentation": "Execute documentation examples, validate local links and demonstrate the requested information is present.",
        "configuration": "Check effective configuration, defaults, invalid inputs and compatibility through public behavior.",
        "ui": "Exercise the requested interaction, mobile navigation and accessibility through the public UI.",
    }
    contract = {"kind": kind, "requirements": requirements[kind], "fixed_regression": True,
                "all_criteria_covered": True, "before_after_required": kind != "refactor"}
    import copy
    for field in ("required_test_ids", "check_dependencies"):
        if field in plan:
            contract[field] = copy.deepcopy(plan[field])
    if kind == "performance":
        threshold = plan.get("performance_threshold")
        if not isinstance(threshold, dict) or not threshold.get("metric") or not threshold.get("unit") or threshold.get("direction") not in ["lower", "higher"]:
            raise ValueError("Performance plan requires a named metric, unit, target, tolerance and direction")
        if any(type(threshold.get(k)) not in [int, float] or not math.isfinite(threshold[k]) for k in ["target", "tolerance"]) or threshold["tolerance"] < 0:
            raise ValueError("Performance threshold must be finite and tolerance nonnegative")
        contract["threshold"] = threshold
        for name in ("guardrails", "repetitions", "load_conditions"):
            if name in plan:
                import copy
                contract[name] = copy.deepcopy(plan[name])
    return contract
