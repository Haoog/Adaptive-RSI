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
    def modes(revision):
        result = {}
        for row in run("git", "ls-tree", "-z", revision, "--", *RELEASE_FILES).split("\0"):
            if row:
                attributes, path = row.split("\t", 1)
                mode, kind, _ = attributes.split()
                result[path] = (mode, kind)
        return result
    before, after = modes(base), modes(head)
    if (set(before) != set(RELEASE_FILES) or before != after or
            any(kind != "blob" or mode not in ("100644", "100755") for mode, kind in before.values())):
        raise ValueError("Release PR changes a file type or executable mode")
    target = get_version(read_at(head, "adaptive_rsi/__init__.py"))
    expected = bump_files(base, target)
    changed = set(run("git", "diff", "--name-only", base, head).splitlines())
    if changed != set(RELEASE_FILES) or any(read_at(head, name) != content for name, content in expected.items()):
        raise ValueError("Release PR differs from the independently reconstructed version-only patch")
    return target


def desktop_mode(base, head, branch, event):
    """Only an exact generated release identity can omit a redundant build."""
    if event != 'pull_request' or not re.fullmatch(r'chore/release-v\d+\.\d+\.\d+-[a-f0-9]{12}', branch):
        return 'full'
    try:
        target = verify_generated(base, head)
    except (ValueError, subprocess.CalledProcessError):
        return 'full'
    return 'skip' if branch == f'chore/release-v{target}-{base[:12]}' else 'full'


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
    updater = ["adaptive-rsi-" + target + "-macos-arm64.app.tar.gz",
               "adaptive-rsi-" + target + "-macos-arm64.app.tar.gz.sig", "desktop-latest.json"]
    if any(name in files for name in updater):
        if not all(name in files for name in updater):
            raise ValueError("Incomplete desktop updater assets")
        try:
            feed = json.loads(files[updater[2]].read_text())
            platform = feed["platforms"]["darwin-aarch64"]
            signature = files[updater[1]].read_text().strip()
            expected_url = "https://github.com/Haoog/Adaptive-RSI/releases/download/v" + target + "/" + updater[0]
            if feed["version"] != target or platform["url"] != expected_url or not signature or platform["signature"] != signature:
                raise ValueError("Desktop updater manifest does not match release assets")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid desktop updater manifest") from exc
        expected += updater
    if "desktop-latest.json" in files and "sbom.cdx.json" not in files:
        raise ValueError("Signed desktop update is missing its SBOM")
    if "sbom.cdx.json" in files:
        expected.append("sbom.cdx.json")
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


def find_verified_artifact(tree, branch, details=None):
    """Find an unexpired artifact from a successful trusted verification run."""
    details = {} if details is None else details
    details['artifact_reuse_reason'] = 'missing'
    if not re.fullmatch(r"[a-f0-9]{40}", tree or ""):
        raise ValueError("An exact source tree is required")
    name = "release-verified-" + tree
    listing = gh("actions/artifacts?name=" + quote(name, safe="") + "&per_page=100")
    artifacts = sorted(listing.get("artifacts", []), key=lambda item: item.get("id", 0), reverse=True)
    for artifact in artifacts:
        run_id = (artifact.get("workflow_run") or {}).get("id")
        if artifact.get('name') != name:
            continue
        if artifact.get('expired'):
            if details['artifact_reuse_reason'] == 'missing':
                details['artifact_reuse_reason'] = 'expired'
            continue
        details['artifact_reuse_reason'] = 'untrusted'
        if not run_id:
            continue
        run_info = gh("actions/runs/" + str(run_id))
        # The caller may continue publication from the merge job of the same
        # release-verify run. The artifact already exists at that point, even
        # though the run is still finishing its merge job; a missing artifact
        # never reaches this branch.
        if (run_info.get("path") != ".github/workflows/release-verify.yml" or
                run_info.get("event") != "workflow_dispatch" or
                run_info.get("status") not in {"in_progress", "completed"} or
                run_info.get("conclusion") in {"failure", "cancelled"} or
                run_info.get("head_branch") != branch):
            continue
        details['artifact_reuse_reason'] = 'available'
        return {"run_id": run_id, "name": name}
    return None


def output(**fields):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            for key, value in fields.items():
                stream.write(key + "=" + str(value) + "\n")
    print(json.dumps(fields, ensure_ascii=False))


def release_verification_title(pr, sha):
    return f"Release verification PR #{int(pr)} @{sha}"


def paged_items(endpoint, key):
    items, page = [], 1
    while True:
        response = gh(endpoint + ('&' if '?' in endpoint else '?') + f'per_page=100&page={page}')
        batch = response.get(key)
        if not isinstance(batch, list):
            raise ValueError('Incomplete GitHub listing; refusing an uncertain dispatch')
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def ensure_release_verification(pr, sha):
    """Reserve dispatch durably before POST; ambiguity requires reconciliation."""
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Exact PR head SHA is required')
    title = release_verification_title(pr, sha)
    runs = paged_items('actions/workflows/release-verify.yml/runs?event=workflow_dispatch&branch=main', 'workflow_runs')
    matching = [item for item in runs
                if item.get('display_title') == title and item.get('event') == 'workflow_dispatch'
                and item.get('head_branch') == 'main'
                and item.get('path') == '.github/workflows/release-verify.yml']
    reusable = [item for item in matching if item.get('status') != 'completed' or item.get('conclusion') == 'success']
    if reusable:
        item = max(reusable, key=lambda item: item['id'])
        return {'verification_state': 'success' if item['status'] == 'completed' else 'active',
                'verification_run_id': item['id']}
    if matching:
        item = max(matching, key=lambda item: item['id'])
        # Retry the exact known run explicitly, never create a second uncertain dispatch.
        return {'verification_state': 'failed', 'verification_run_id': item['id'],
                'verification_retry': f"gh run rerun {item['id']} --repo {os.environ['GITHUB_REPOSITORY']}"}
    receipts = paged_items(f'commits/{sha}/check-runs?filter=all', 'check_runs')
    name = 'release-verification-dispatch-' + str(int(pr))
    if any(item.get('name') == name and item.get('head_sha') == sha for item in receipts):
        return {'verification_state': 'dispatch_unknown', 'verification_run_id': ''}
    # This receipt is deliberately separate from verify: dispatch is not verification.
    receipt = gh('check-runs', 'POST', name=name, head_sha=sha, status='completed', conclusion='neutral',
                 external_id=title, **{'output[title]': 'Release verification dispatch reserved',
                                     'output[summary]': 'Await exact-head trusted release verification; do not redispatch an uncertain request.'})
    saved = gh('check-runs/' + str(receipt['id']))
    if saved.get('name') != name or saved.get('head_sha') != sha or saved.get('external_id') != title:
        raise ValueError('Dispatch receipt identity could not be verified')
    try:
        gh('actions/workflows/release-verify.yml/dispatches', 'POST', ref='main',
           **{'inputs[pr]': str(int(pr)), 'inputs[sha]': sha})
    except subprocess.CalledProcessError:
        return {'verification_state': 'dispatch_unknown', 'verification_run_id': ''}
    return {'verification_state': 'dispatched', 'verification_run_id': ''}


def release_selection():
    level = os.environ.get("RELEASE_LEVEL", "patch")
    stage = os.environ.get("NEW_PRODUCT_STAGE", "false").lower()
    if level not in ["major", "minor", "patch"]:
        raise ValueError("Release level must be major, minor or patch")
    if stage not in ("true", "false"):
        raise ValueError("New product stage must be true or false")
    if level != "patch" and stage != "true":
        raise ValueError("Minor and major releases require an explicit new product stage")
    return level, stage


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
            details = {}
            try:
                artifact = find_verified_artifact(tree, branch, details)
            except (subprocess.CalledProcessError, ValueError) as exc:
                print("Verified artifact lookup failed; formal release will rebuild: " + str(exc), file=sys.stderr)
                artifact = None
                details['artifact_reuse_reason'] = 'lookup_error'
            reason = details.get('artifact_reuse_reason', 'available' if artifact else 'missing')
            release_path = 'reuse_candidate' if artifact else 'rebuild'
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                    stream.write(f'\nRelease path: `{release_path}`; artifact reuse reason: `{reason}`.\n')
            if not artifact:
                print("No reusable attested artifact found for this source tree; formal release will rebuild", file=sys.stderr)
            output(mode="publish", sha=head, version=current, tree=tree,
                   release_path=release_path, artifact_reuse_reason=reason,
                   artifact_run_id=(artifact or {}).get("run_id", ""),
                   artifact_name=(artifact or {}).get("name", ""))
            return
    # 合并不等于发版；只有明确启动稳定批次，才创建新的版本 PR。
    if os.environ.get("PREPARE_RELEASE") != "true":
        output(mode="idle", reason="Changes accumulated; start a stable release batch explicitly")
        return
    level, _ = release_selection()
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
            output(mode="verify", pr=item["number"], sha=item["headRefOid"],
                   **ensure_release_verification(item["number"], item["headRefOid"]))
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
    output(mode="verify", pr=item["number"], sha=item["headRefOid"],
                   **ensure_release_verification(item["number"], item["headRefOid"]))


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
    elif (args.action == "prepare" and os.environ.get("PREPARE_RELEASE") == "true"
          and os.environ.get("GITHUB_ACTIONS") != "true"):
        level, stage = release_selection()
        # Only the canonical workflow owns PR/check writes under its shared lock.
        try:
            run("gh", "workflow", "run", "release.yml", "--repo", os.environ["GITHUB_REPOSITORY"],
                "--ref", "main", "-f", "prepare_release=true", "-f", "release_level=" + level,
                "-f", "new_product_stage=" + stage)
        except (subprocess.CalledProcessError, OSError):
            output(mode="dispatch_unknown", reason="Inspect release.yml runs before requesting another batch")
        else:
            output(mode="requested")
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
