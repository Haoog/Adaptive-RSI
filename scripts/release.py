#!/usr/bin/env python3
"""Deterministic release-only PRs and immutable releases; never push main."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = ("plugin.json", "pyproject.toml", "adaptive_rsi/__init__.py", "update.json")
RELEASE_FILES = VERSION_FILES + ("CHANGELOG.md", "CHANGELOG.en.md")


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd or ROOT, text=True).strip()


def gh(endpoint, method="GET", **fields):
    path = "repos/" + os.environ["GITHUB_REPOSITORY"] + ("/" + endpoint if endpoint else "")
    args = ["gh", "api", path, "--method", method]
    for key, value in fields.items():
        args += ["-f", key + "=" + str(value)]
    output = run(*args)
    return json.loads(output) if output else {}


def read_at(sha, name):
    return subprocess.check_output(["git", "show", sha + ":" + name], cwd=ROOT, text=True)


def tree_at(sha):
    return run("git", "rev-parse", sha + "^{tree}")


def get_version(text):
    match = re.search(r'(?m)^__version__ = "(\d+\.\d+\.\d+)"$', text)
    if not match:
        raise ValueError("A stable version is required")
    return match[1]


def bump_files(base, target):
    previous = get_version(read_at(base, "adaptive_rsi/__init__.py"))
    if tuple(map(int, target.split("."))) <= tuple(map(int, previous.split("."))):
        raise ValueError("Release version must increase")
    result = {}
    for name in VERSION_FILES:
        original = read_at(base, name)
        if name.endswith(".json"):
            if json.loads(original)["version"] != previous:
                raise ValueError("Version files disagree")
            updated, count = re.subn(r'("version"\s*:\s*")' + re.escape(previous) + '(")',
                                     lambda m: m[1] + target + m[2], original)
        else:
            prefix = "__version__" if name.endswith(".py") else "version"
            updated, count = re.subn(r'(?m)^' + prefix + ' = "' + re.escape(previous) + '"$',
                                     prefix + ' = "' + target + '"', original)
        if count != 1:
            raise ValueError("Version file did not contain one expected version: " + name)
        result[name] = updated
    marker = "<!-- adaptive-rsi-release:" + target + ";base:" + base + " -->"
    for name, note in [("CHANGELOG.md", "- 发布经过检查的主线改进；本版本对应提交 `" + base + "`。"),
                       ("CHANGELOG.en.md", "- Release verified mainline improvements from commit `" + base + "`.")]:
        original = read_at(base, name)
        unreleased = re.search(r"(?m)^## Unreleased[ \t]*$", original)
        position = original.find("\n## ")
        if position < 0:
            raise ValueError("Changelog must contain version sections")
        if unreleased:
            end = original.find("\n## ", unreleased.end())
            end = len(original) if end < 0 else end
            notes = original[unreleased.end():end].strip() or note
            result[name] = (original[:unreleased.end()] + "\n\n## " + target + "\n\n" + marker + "\n\n" +
                            notes + "\n" + original[end:])
        else:
            result[name] = original[:position] + "\n## " + target + "\n\n" + marker + "\n\n" + note + "\n" + original[position:]
    return result


def verify_generated(base, head):
    target = get_version(read_at(head, "adaptive_rsi/__init__.py"))
    expected = bump_files(base, target)
    changed = set(run("git", "diff", "--name-only", base, head).splitlines())
    if changed != set(RELEASE_FILES) or any(read_at(head, name) != content for name, content in expected.items()):
        raise ValueError("Release PR differs from the independently reconstructed version-only patch")
    return target


def release_assets(directory, target):
    files = {path.name: path for path in Path(directory).glob("*")
             if path.is_file() and not path.is_symlink()}
    wheels = [path for name, path in files.items()
              if name.startswith("adaptive_rsi-" + target + "-") and name.endswith(".whl")]
    expected = ["adaptive-rsi-" + target + "-plugin.zip",
                "adaptive-rsi-" + target + "-plugin.zip.sha256",
                "adaptive-rsi-" + target + "-macos-arm64-adhoc.dmg",
                "adaptive-rsi-" + target + "-macos-arm64-adhoc.dmg.sha256"]
    if len(wheels) != 1 or any(name not in files for name in expected):
        raise ValueError("Expected plugin ZIP, checksums, one wheel and one macOS ARM64 desktop DMG")
    return sorted([files[name] for name in expected] + wheels)


def workflow_path(reference):
    source = reference.split("@", 1)[0]
    parts = source.split("/", 2)
    return parts[2] if len(parts) == 3 else source


def read_proof(directory):
    path = Path(directory) / "release-proof.json"
    try:
        proof = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Release proof is missing or invalid") from exc
    if (proof.get("format") != 1 or not re.fullmatch(r"[a-f0-9]{40}", proof.get("source_sha", "")) or
            not re.fullmatch(r"[a-f0-9]{40}", proof.get("source_tree", "")) or
            not isinstance(proof.get("artifacts"), dict)):
        raise ValueError("Release proof has an unsupported format")
    workflow_source_sha = proof.get("workflow_source_sha")
    if workflow_source_sha is not None and not re.fullmatch(r"[a-f0-9]{40}", workflow_source_sha):
        raise ValueError("Release proof has an invalid workflow source SHA")
    return proof


def validate_proof(directory, sha, allowed_workflows):
    proof = read_proof(directory)
    target = get_version(read_at(sha, "adaptive_rsi/__init__.py"))
    if proof.get("version") != target or proof["source_tree"] != tree_at(sha):
        raise ValueError("Release proof does not describe the exact source tree")
    producer = workflow_path(proof.get("workflow_ref", ""))
    if producer not in allowed_workflows:
        raise ValueError("Release proof was not produced by a trusted release workflow")
    if producer == ".github/workflows/release.yml" and proof["source_sha"] != sha:
        raise ValueError("Direct release build proof does not match the release commit")
    if not re.fullmatch(r"[a-f0-9]{40}", proof.get("workflow_source_sha", "")):
        raise ValueError("Release proof is missing the signing workflow source SHA")
    assets = release_assets(directory, target)
    expected = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in assets}
    if proof["artifacts"] != expected:
        raise ValueError("Release proof does not match the built asset bytes")
    for asset in assets:
        if not asset.name.endswith(".sha256"):
            continue
        subject = asset.with_name(asset.name[:-7])
        fields = asset.read_text().split()
        digest = hashlib.sha256(subject.read_bytes()).hexdigest()
        if fields != [digest, subject.name]:
            raise ValueError("Release checksum does not match its asset")
    return proof


def find_verified_artifact(tree, branch):
    """Find an unexpired artifact from a successful trusted verification run."""
    if not re.fullmatch(r"[a-f0-9]{40}", tree or ""):
        raise ValueError("An exact source tree is required")
    name = "release-verified-" + tree
    listing = gh("actions/artifacts?name=" + quote(name, safe="") + "&per_page=100")
    artifacts = sorted(listing.get("artifacts", []), key=lambda item: item.get("id", 0), reverse=True)
    for artifact in artifacts:
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if artifact.get("name") != name or artifact.get("expired") or not run_id:
            continue
        run_info = gh("actions/runs/" + str(run_id))
        if (run_info.get("path") != ".github/workflows/release-verify.yml" or
                run_info.get("event") != "workflow_dispatch" or
                run_info.get("status") != "completed" or run_info.get("conclusion") != "success" or
                run_info.get("head_branch") != branch):
            continue
        return {"run_id": run_id, "name": name}
    return None


def output(**fields):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key, value in fields.items():
                stream.write(key + "=" + str(value) + "\n")
    print(json.dumps(fields, ensure_ascii=False))


def prepare():
    head = run("git", "rev-parse", "HEAD")
    repository = gh("")
    branch = repository["default_branch"]
    if gh("commits/" + branch)["sha"] != head:
        raise ValueError("Mainline advanced; the newer workflow must prepare the release")
    current = get_version(read_at(head, "adaptive_rsi/__init__.py"))
    marker = re.search(r"<!-- adaptive-rsi-release:" + re.escape(current) + r";base:([a-f0-9]{40}) -->", read_at(head, "CHANGELOG.md"))
    if marker:
        try:
            verify_generated(marker[1], head)
        except ValueError:
            pass
        else:
            tree = tree_at(head)
            try:
                artifact = find_verified_artifact(tree, branch)
            except (subprocess.CalledProcessError, ValueError) as exc:
                print("Verified artifact lookup failed; formal release will rebuild: " + str(exc), file=sys.stderr)
                artifact = None
            if not artifact:
                print("No reusable attested artifact found for this source tree; formal release will rebuild", file=sys.stderr)
            output(mode="publish", sha=head, version=current, tree=tree,
                   artifact_run_id=(artifact or {}).get("run_id", ""),
                   artifact_name=(artifact or {}).get("name", ""))
            return
    # 合并不等于发版；只有明确启动稳定批次，才创建新的版本 PR。
    if os.environ.get("PREPARE_RELEASE") != "true":
        output(mode="idle", reason="Changes accumulated; start a stable release batch explicitly")
        return
    level = os.environ.get("RELEASE_LEVEL", "patch")
    if level not in ["major", "minor", "patch"]:
        raise ValueError("Release level must be major, minor or patch")
    if level != "patch" and os.environ.get("NEW_PRODUCT_STAGE", "false").lower() != "true":
        raise ValueError("Minor and major releases require an explicit new product stage")
    # A single stable branch and all-state PR lookup prevent duplicate version PRs.
    major, minor, patch = map(int, current.split("."))
    target = f"{major + 1}.0.0" if level == "major" else f"{major}.{minor + 1}.0" if level == "minor" else f"{major}.{minor}.{patch + 1}"
    release_branch = "chore/release-v" + target + "-" + head[:12]
    open_prs = json.loads(run("gh", "pr", "list", "--repo", os.environ["GITHUB_REPOSITORY"], "--state", "open",
                              "--json", "number,headRefName,headRefOid"))
    for item in open_prs:
        if not item["headRefName"].startswith("chore/release-v") or item["headRefName"] == release_branch:
            continue
        old_notes = read_at(item["headRefOid"], "CHANGELOG.md")
        old_marker = re.search(r"<!-- adaptive-rsi-release:[0-9.]+;base:([a-f0-9]{40}) -->", old_notes)
        if not old_marker:
            raise ValueError("Existing release branch has unrecognized manual changes; inspect it before replacing")
        verify_generated(old_marker[1], item["headRefOid"])
        run("gh", "pr", "close", str(item["number"]), "--repo", os.environ["GITHUB_REPOSITORY"])
    existing = json.loads(run("gh", "pr", "list", "--repo", os.environ["GITHUB_REPOSITORY"], "--state", "all",
                              "--head", release_branch, "--json", "number,state,headRefOid,baseRefOid"))
    if existing:
        item = existing[0]
        if item["state"] == "OPEN":
            output(mode="verify", pr=item["number"], sha=item["headRefOid"])
        else:
            output(mode="blocked", reason="Existing version PR is closed; inspect before preparing another version")
        return
    files = bump_files(head, target)
    run("git", "checkout", "-b", release_branch)
    for name, contents in files.items():
        (ROOT / name).write_text(contents)
    run("git", "add", "--", *RELEASE_FILES)
    run("git", "-c", "user.name=Adaptive RSI", "-c", "user.email=adaptive-rsi@localhost", "commit", "-m", "准备正式版本 " + target)
    run("git", "push", "origin", "HEAD:refs/heads/" + release_branch)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as body:
        body.write("同步四处版本及中英文版本记录。\n\n价值：将已合入主线并通过检查的改进交付给用户。发布前独立重建版本补丁、完整检查、ZIP/wheel/macOS ARM64 桌面 DMG 构建及安装/升级 smoke；正式发布优先复用同一源树且 provenance 有效的产物并再次 smoke，任何不一致都会回退完整重建。DMG 使用临时 ad-hoc 签名，尚未 Apple 公证。\n\n基线：`" + head + "`。必须遵守所有必需检查与评审规则；不得绕过审批。")
        body.flush()
        url = run("gh", "pr", "create", "--repo", os.environ["GITHUB_REPOSITORY"], "--base", branch, "--head", release_branch,
                  "--title", "准备正式版本 " + target, "--body-file", body.name)
    item = json.loads(run("gh", "pr", "view", url, "--json", "number,headRefOid"))
    output(mode="verify", pr=item["number"], sha=item["headRefOid"])


def gate(pr_number, expected_sha):
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise ValueError("Exact PR head SHA is required")
    pr = gh("pulls/" + str(int(pr_number)))
    info = gh("")
    if (pr["state"] != "open" or pr["draft"] or pr["head"]["sha"] != expected_sha or
            pr["base"]["ref"] != info["default_branch"] or pr["head"]["repo"]["full_name"] != os.environ["GITHUB_REPOSITORY"] or
            not re.fullmatch(r"chore/release-v\d+\.\d+\.\d+-[a-f0-9]{12}", pr["head"]["ref"])):
        raise ValueError("PR identity changed or is not a generated release PR")
    verify_generated(pr["base"]["sha"], expected_sha)
    output(sha=expected_sha, base=pr["base"]["sha"])


def publish():
    head = run("git", "rev-parse", "HEAD")
    target = get_version(read_at(head, "adaptive_rsi/__init__.py"))
    marker = re.search(r"<!-- adaptive-rsi-release:" + re.escape(target) + r";base:([a-f0-9]{40}) -->", read_at(head, "CHANGELOG.md"))
    if not marker:
        raise ValueError("Formal releases require the generated and reviewed version PR")
    verify_generated(marker[1], head)
    validate_proof(ROOT / "dist", head, {
        ".github/workflows/release-verify.yml",
        ".github/workflows/release.yml",
    })
    tag = "v" + target
    refs = gh("git/matching-refs/tags/" + tag)
    existing = [item for item in refs if item["ref"] == "refs/tags/" + tag]
    if existing:
        if existing[0]["object"]["type"] != "commit" or existing[0]["object"]["sha"] != head:
            raise ValueError("Version tag already points to another commit; never move release tags")
    else:
        gh("git/refs", "POST", ref="refs/tags/" + tag, sha=head)
    releases = gh("releases?per_page=100")
    previous = next((item for item in releases if item["tag_name"] == tag), None)
    assets = release_assets(ROOT / "dist", target)
    if previous and not previous["draft"]:
        if {item["name"] for item in previous.get("assets", [])} != {path.name for path in assets}:
            raise ValueError("Existing release asset list differs; refusing to claim a complete immutable release")
        with tempfile.TemporaryDirectory() as temp:
            for path in assets:
                run("gh", "release", "download", tag, "--repo", os.environ["GITHUB_REPOSITORY"], "--pattern", path.name, "--dir", temp)
                if hashlib.sha256((Path(temp) / path.name).read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
                    raise ValueError("Existing release asset differs; refusing to replace immutable assets")
        output(mode="already_published", sha=head, version=target)
        return
    if not previous:
        run("gh", "release", "create", tag, "--repo", os.environ["GITHUB_REPOSITORY"], "--verify-tag", "--draft",
            "--title", "Adaptive RSI " + target, "--notes-file", str(ROOT / "CHANGELOG.md"))
    # Upload while draft. A crash can safely resume without exposing incomplete assets.
    run("gh", "release", "upload", tag, "--repo", os.environ["GITHUB_REPOSITORY"], "--clobber", *map(str, assets))
    run("gh", "release", "edit", tag, "--repo", os.environ["GITHUB_REPOSITORY"], "--draft=false")
    output(mode="published", sha=head, version=target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "gate", "publish", "validate-verified"])
    parser.add_argument("--pr")
    parser.add_argument("--sha")
    parser.add_argument("--directory", default=str(ROOT / "dist"))
    args = parser.parse_args()
    if args.action == "gate":
        gate(args.pr, args.sha)
    elif args.action == "validate-verified":
        if not args.sha:
            parser.error("validate-verified requires --sha")
        proof = validate_proof(Path(args.directory), args.sha, {".github/workflows/release-verify.yml"})
        output(source_sha=proof["source_sha"], source_tree=proof["source_tree"],
               workflow_source_sha=proof["workflow_source_sha"])
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
