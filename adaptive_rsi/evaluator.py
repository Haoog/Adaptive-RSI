import json
import os
import re
import shutil
from pathlib import Path

from .adapters import test_path
from .config import fingerprint
from .gitops import copy_source
from .process import Sandbox

MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py",
             "pom.xml", "build.gradle", "build.gradle.kts", "Makefile", "CMakeLists.txt", "Cargo.toml",
             "composer.json", "phpunit.xml", "Gemfile", "Rakefile", "Package.swift", "pubspec.yaml"}


def empty_test_run(text):
    empty = re.search(r"Ran 0 tests|no tests ran|No tests found|No tests were found|no test files|running 0 tests|"
                      r"No examples found|No tests executed|No test is available|Total tests: 0|Tests run: 0|# tests 0|0 runs, 0 assertions", text, re.I)
    positive = re.search(r"(?:Ran|running) [1-9]\d* tests?|[1-9]\d* passed|[1-9]\d* examples?|"
                         r"Tests run: [1-9]\d*|Total tests: [1-9]\d*|# tests [1-9]\d*", text, re.I)
    return bool(empty and not positive)


def freeze_harness(baseline, target):
    """Host-owned baseline tests/build entrypoints overwrite candidate-authored gate changes."""
    protected = set()
    for parent, dirs, files in os.walk(baseline, followlinks=False):
        dirs[:] = [d for d in dirs if d not in {".git", ".rsi-agent", ".rsi-home"}]
        for name in files:
            src = Path(parent) / name
            relative = src.relative_to(baseline)
            if not (test_path(relative) or name in MANIFESTS or name.endswith((".csproj", ".fsproj", ".sln"))):
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

    def evaluate(self, baseline, candidate, checks, directory, config, cancel=None, overlay=None, seeds=None):
        if not checks:
            return {"decision": "insufficient_evidence", "reason": "No executable acceptance checks. Configure checks before delivery.", "checks": []}
        results = {}
        for label, source in [("baseline", baseline), ("candidate", candidate)]:
            target = copy_source(source, Path(directory) / label)
            if seeds:
                from .environment import restore
                for relative in config["environment"]["seed_paths"]:
                    seeded = target / relative
                    if seeded.exists():
                        if seeded.is_symlink():
                            seeded.unlink()
                        else:
                            shutil.rmtree(seeded)
                restore(seeds, target, config)
            protected = freeze_harness(Path(baseline), target)
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
            hidden = [row["path"] for row in self.store.query("SELECT path FROM projects")]
            rows = []
            for setup in config["environment"]["setup"]:
                result = sandbox.check(setup, target, cancel, protected, hidden)
                if result["exit_code"] or result["error"]:
                    return {"decision": "insufficient_evidence", "reason": "Isolated dependency preparation failed", "setup": result}
            for check in checks:
                result = sandbox.check(check, target, cancel, protected, hidden)
                text = result["stdout"] + result["stderr"]
                empty = empty_test_run(text)
                result["passed"] = result["exit_code"] == 0 and not result["error"] and not empty
                result["empty_tests"] = empty
                rows.append(result)
            results[label] = rows
        passing = all(row["passed"] for row in results["candidate"])
        data = {"decision": "pass" if passing else "fail",
                "reason": "Frozen checks passed" if passing else "Candidate failed independent checks",
                "harness_hash": fingerprint(checks), "checks": results}
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
