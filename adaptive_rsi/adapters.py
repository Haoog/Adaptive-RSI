"""Manifest detection is deliberately separate from verified executable checks."""
import json
import os
import shutil
from pathlib import Path

IGNORED = {".git", "node_modules", ".venv", "venv", "target", "build", "dist", ".dart_tool",
           ".rsi", "vendor", ".gradle", "Pods", ".build", "__pycache__"}
PROFILES = [
    ("Java / Kotlin", ["pom.xml"], "mvn", [["mvn", "-B", "-o", "test"]]),
    ("Java / Kotlin", ["build.gradle", "build.gradle.kts"], "gradle", [["gradle", "--offline", "test"]]),
    ("Python", ["pyproject.toml", "setup.py", "requirements.txt", "Pipfile"], "python3", []),
    ("Go", ["go.mod"], "go", [["go", "test", "./..."]]),
    ("Rust", ["Cargo.toml"], "cargo", [["cargo", "test", "--offline"], ["cargo", "clippy", "--offline", "--", "-D", "warnings"]]),
    ("C / C++", ["CMakeLists.txt"], "cmake", [["cmake", "-S", ".", "-B", ".rsi-build"],
                                               ["cmake", "--build", ".rsi-build"],
                                               ["ctest", "--test-dir", ".rsi-build", "--output-on-failure", "--no-tests=error"]]),
    ("C / C++", ["Makefile"], "make", []),
    ("C / C++", ["build.ninja"], "ninja", []),
    ("PHP", ["composer.json"], "php", [["php", "vendor/bin/phpunit"]]),
    ("Ruby", ["Gemfile"], "bundle", []),
    ("Swift / Objective-C", ["Package.swift"], "swift", [["swift", "test", "--disable-automatic-resolution"]]),
    ("Dart / Flutter", ["pubspec.yaml"], "dart", []),
]
SUFFIXES = {".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".html": "HTML / CSS",
            ".css": "HTML / CSS", ".m": "Swift / Objective-C", ".mm": "Swift / Objective-C"}


def detect(root):
    root = Path(root).resolve()
    modules = []
    auxiliary = set()
    visited = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED and not (Path(parent) / d).is_symlink()]
        relative = Path(parent).relative_to(root)
        if len(relative.parts) >= 5:
            dirs[:] = []
        visited += len(files)
        if visited > 30000:
            break
        names = set(files)
        folder = Path(parent)
        cwd = relative.as_posix()
        for language, manifests, tool, commands in PROFILES:
            matched = sorted(names.intersection(manifests))
            if not matched:
                continue
            checks = list(commands)
            if language == "Python":
                tests = folder / "tests"
                text = "\n".join((folder / f).read_text(errors="replace")[:30000] for f in matched if (folder / f).is_file())
                if tests.is_dir() or any(f.startswith("test_") for f in files):
                    # pytest 的存在不只写在 manifest 文本里：专有配置文件与 conftest.py 同样可靠。
                    uses_pytest = ("pytest" in text or "pytest.ini" in names or "conftest.py" in names
                                   or (tests / "conftest.py").is_file())
                    checks = [["python3", "-m", "pytest", "-q"]] if uses_pytest else [["python3", "-m", "unittest", "discover", "-s", "tests" if tests.is_dir() else ".", "-v"]]
                if "uv.lock" in names:
                    tool = "uv"
                    checks = [["uv", "run", "--offline", "--no-sync"] + c for c in checks]
                elif "poetry.lock" in names:
                    tool = "poetry"
                    checks = [["poetry", "run"] + c for c in checks]
            elif language == "Ruby":
                checks = [["bundle", "exec", "rspec"]] if (folder / "spec").is_dir() else ([["bundle", "exec", "rake", "test"]] if (folder / "test").is_dir() else [])
            elif language == "Dart / Flutter":
                flutter = "flutter:" in (folder / matched[0]).read_text(errors="replace")
                tool = "flutter" if flutter else "dart"
                checks = [[tool, "test", "--no-pub"]] if flutter else [["dart", "test", "--no-pub"]]
            modules.append(module(language, matched[0], cwd, tool, checks))
        if "package.json" in names:
            try:
                package = json.loads((folder / "package.json").read_text())
                tool = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
                checks = [[tool, "run", s] for s in ["test", "typecheck", "lint"] if s in package.get("scripts", {})
                          and "no test specified" not in package["scripts"][s].lower()]
                modules.append(module("JavaScript / TypeScript", "package.json", cwd, tool, checks))
            except (ValueError, OSError, TypeError):
                modules.append(module("JavaScript / TypeScript", "package.json", cwd, "npm", []))
        dotnet = sorted(f for f in files if f.endswith((".sln", ".csproj", ".fsproj")))
        if dotnet:
            test_projects = [f for f in dotnet if "test" in f.lower() or "Microsoft.NET.Test.Sdk" in (folder / f).read_text(errors="replace")]
            target = test_projects[0] if test_projects else dotnet[0]
            modules.append(module("C# / F# / .NET", target, cwd, "dotnet", [["dotnet", "test", target, "--no-restore"]] if test_projects else []))
        xcode = [d for d in dirs if d.endswith(".xcodeproj")]
        if xcode:
            modules.append(module("Swift / Objective-C", xcode[0], cwd, "xcodebuild", []))
        for name in files:
            if name == "Dockerfile":
                auxiliary.add("Docker")
            if ".github/workflows" in (relative / name).as_posix():
                auxiliary.add("CI")
            suffix = Path(name).suffix
            if suffix in SUFFIXES:
                auxiliary.add(SUFFIXES[suffix])
    # A parent tool owns its subtree unless a nested module needs different commands.
    return {"modules": modules, "auxiliary": sorted(auxiliary), "files_seen": visited,
            "checks": [c for m in modules for c in m["checks"]]}


def module(language, manifest, cwd, tool, commands):
    available = bool(shutil.which(tool))
    return {"language": language, "manifest": str(Path(cwd) / manifest), "cwd": cwd, "tool": tool,
            "status": "unverified" if available and commands else "analysis_only" if available else "environment_blocked",
            "reason": "Commands require an isolated verification run" if available and commands else
                      "Configure project-specific checks" if available else "Missing tool: " + tool,
            "checks": [{"name": language + ": " + " ".join(cmd), "cwd": cwd, "argv": cmd} for cmd in commands]}


def test_path(path):
    p = Path(path)
    name = p.name.lower()
    test_directories = {"test", "tests", "spec", "specs", "__tests__"}
    named_test = p.stem.lower() in {"test", "tests", "spec", "specs"} or \
        p.stem.startswith(("Test", "test_", "test-")) or p.stem.endswith(("Test", "Tests", "TestCase"))
    return any(part.lower() in test_directories for part in p.parts) or named_test or name.endswith((
        "_test.py", "_test.go", "_test.rs", "_test.rb", "_test.ts", "_test.tsx", "_test.js", "_test.jsx", "_test.cjs", "_test.mjs",
        "_test.exs", "_test.dart", ".test.py", ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.cjs", ".test.mjs",
        ".spec.py", ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".spec.cjs", ".spec.mjs"))
