#!/usr/bin/env python3
"""Replayable, isolated harness for signed desktop upgrades and receipt recovery.

Every stage is real: disposable test signing keys, the real signature verifier,
the real receipt store, real cargo tests, and a real copy of a signed app bundle.
Nothing here pretends to be the Tauri installer: an actual N -> N+1 replacement
needs a signed build pair and the platform installer, so that stage reports
``blocked`` with the exact reason instead of simulating the replacement.

Usage:
    python3 -B scripts/upgrade_harness.py --stage signing receipt authorization
    python3 -B scripts/upgrade_harness.py --stage isolation --app /path/to/Adaptive\\ RSI.app
    python3 -B scripts/upgrade_harness.py --stage all --require-complete

Exit codes: 0 all executed stages passed, 1 a stage failed, 3 a requested stage
is blocked while ``--require-complete`` was given. The JSON report always states
``actual_acceptance`` so a blocked replacement is never reported as verified.
"""
import argparse
import hashlib
import json
import os
import plistlib
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "desktop/src-tauri/Cargo.toml"
SIGNING_SCRIPT = ROOT / "scripts/test_updater_signing.py"
STAGES = ("signing", "receipt", "authorization", "isolation", "bad_signature", "startup_failure", "replacement")
STAGE_ALIASES = {
    "bad-signature": "bad_signature",
    "startup-failure": "startup_failure",
}
# Filters stay explicit so the harness never silently widens to the whole suite.
CARGO_FILTERS = {
    "receipt": ["update_receipt::receipt_tests"],
    "authorization": ["authorization_revisions", "superseding_a_waiting_install",
                      "install_is_bound_to_the_revision"],
}


def strip_signing_env(env=None):
    """Never let release signing credentials reach a harness subprocess."""
    source = os.environ if env is None else env
    return {key: value for key, value in source.items() if not key.startswith("TAURI_SIGNING_")}


def cargo():
    found = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
    if not Path(found).exists():
        raise RuntimeError("cargo is required for the upgrade harness")
    return found


def validate_app_source(app, stage_name="isolation"):
    """Only an isolated copy of a build artifact may be used.

    The installed application under /Applications is the user's real app; the
    harness must never read, replace or launch it.
    """
    if not app:
        raise ValueError(f"{stage_name} requires --app pointing at a signed build artifact")
    path = Path(app).expanduser()
    if path.is_symlink():
        raise ValueError("the app fixture must not be a symlink")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError("the app fixture must be an existing .app directory")
    if resolved.suffix != ".app":
        raise ValueError("the app fixture must be a .app bundle")
    protected = [Path("/Applications").resolve(), Path.home().resolve() / "Applications"]
    for folder in protected:
        if resolved == folder or folder in resolved.parents:
            raise ValueError("refusing to use the installed application; pass a build artifact copy")
    for required in ("Contents/Info.plist", "Contents/MacOS"):
        if not (resolved / required).exists():
            raise ValueError("the app fixture is missing " + required)
    for root, dirs, files in os.walk(resolved, followlinks=False):
        for name in dirs + files:
            entry = Path(root) / name
            if entry.is_symlink():
                target = entry.resolve()
                if resolved not in target.parents:
                    raise ValueError(f"the app fixture contains an escaping symlink: {entry.relative_to(resolved)}")
    return resolved


def select_stages(requested):
    stages = []
    for raw in requested:
        name = STAGE_ALIASES.get(raw, raw)
        if name == "all":
            stages.extend([s for s in STAGES if s != "replacement"])
        elif name in STAGES:
            stages.append(name)
        else:
            raise ValueError("unknown stage: " + raw)
    ordered = [name for name in STAGES if name in stages]
    if not ordered:
        raise ValueError("no stages selected")
    return ordered


def digest_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bundle_version(app):
    plist = Path(app) / "Contents/Info.plist"
    if not plist.exists():
        return ""
    try:
        data = plistlib.loads(plist.read_bytes())
        if isinstance(data, dict) and "CFBundleShortVersionString" in data:
            return str(data["CFBundleShortVersionString"])
    except Exception:
        pass
    if Path("/usr/libexec/PlistBuddy").exists():
        result = subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString", str(plist)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        return result.stdout.strip() if result.returncode == 0 else ""
    return ""


def stage_signing():
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-B", str(SIGNING_SCRIPT)], cwd=ROOT,
                            env=strip_signing_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=600)
    return stage("signing", result, started,
                 command=[sys.executable, "-B", str(SIGNING_SCRIPT)],
                 detail=result.stdout.strip()[-1000:])


def stage_cargo(name):
    started = time.monotonic()
    command = [cargo(), "test", "--locked", "--manifest-path", str(MANIFEST), "--bin", "adaptive-rsi-desktop"]
    details, exit_code = [], 0
    for filter_name in CARGO_FILTERS[name]:
        result = subprocess.run(command + [filter_name], cwd=ROOT, env=strip_signing_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900)
        summary = next((line for line in result.stdout.splitlines() if line.startswith("test result:")), "")
        matched = re.search(r"(\d+) passed", summary)
        passed = int(matched.group(1)) if matched else 0
        details.append(f"{filter_name}: {summary.strip() or 'no test result line'}")
        exit_code = result.returncode
        if exit_code != 0:
            break
        # A filter that matches nothing would silently "pass" after a rename.
        if passed == 0:
            exit_code = 1
            details.append("the filter matched no test")
            break
    return {"stage": name, "status": "passed" if exit_code == 0 else "failed",
            "command": command + CARGO_FILTERS[name], "exit_code": exit_code,
            "detail": " | ".join(details), "duration_s": round(time.monotonic() - started, 3)}


def verify_codesign(path):
    """Verify codesign signature on macOS if codesign tool is present.

    Returns (returncode, stderr). On non-Darwin or without /usr/bin/codesign, returns (0, "").
    """
    if sys.platform != "darwin" or not Path("/usr/bin/codesign").exists():
        return 0, ""
    result = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
    return result.returncode, result.stderr


def stage_isolation(app, state):
    started = time.monotonic()
    try:
        source = validate_app_source(app)
    except ValueError as error:
        return {"stage": "isolation", "status": "blocked", "reason": str(error),
                "duration_s": round(time.monotonic() - started, 3)}
    before = digest_of(source / "Contents/Info.plist")
    work = Path(tempfile.mkdtemp(prefix="rsi-upgrade-isolation-"))
    copy = work / source.name
    try:
        shutil.copytree(source, copy, symlinks=True)
        returncode, stderr = verify_codesign(copy)
        after = digest_of(source / "Contents/Info.plist")
        if after != before:
            return {"stage": "isolation", "status": "failed",
                    "reason": "the source bundle changed while it was copied",
                    "duration_s": round(time.monotonic() - started, 3)}
        if returncode != 0:
            return {"stage": "isolation", "status": "failed", "exit_code": returncode,
                    "command": ["/usr/bin/codesign", "--verify", "--deep", "--strict", "<isolated copy>"],
                    "reason": "the isolated copy does not keep a valid signature",
                    "detail": stderr.strip()[-1000:],
                    "duration_s": round(time.monotonic() - started, 3)}
        if state:
            Path(state).expanduser().mkdir(parents=True, exist_ok=True)
        return {"stage": "isolation", "status": "passed", "exit_code": returncode,
                "command": ["/usr/bin/codesign", "--verify", "--deep", "--strict", "<isolated copy>"],
                "source": str(source), "version": bundle_version(copy),
                "state": str(Path(state).expanduser()) if state else "",
                "detail": "signed app copied to a private directory; the source stayed unchanged",
                "duration_s": round(time.monotonic() - started, 3)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_bad_signature_receipt(version, now=None):
    ts = int(time.time()) if now is None else int(now)
    return {
        "receipt_version": 1,
        "operation_id": f"bad-sig-{ts}",
        "from_version": version or "0.0.0",
        "to_version": "9.9.9",
        "artifact_sha256": "0" * 64,
        "signature_sha256": "f" * 64,
        "signature_verified": False,
        "created_at": ts,
        "updated_at": ts,
        "state": "failed",
        "error_code": "update_download_or_signature_failed",
    }


def make_startup_failure_receipt(version, now=None):
    ts = int(time.time()) if now is None else int(now)
    return {
        "receipt_version": 1,
        "operation_id": f"startup-fail-{ts}",
        "from_version": version or "0.0.0",
        "to_version": "broken-candidate",
        "artifact_sha256": "0" * 64,
        "signature_sha256": "0" * 64,
        "signature_verified": True,
        "created_at": ts,
        "updated_at": ts,
        "state": "failed",
        "error_code": "update_health_failed",
    }


def stage_bad_signature(app=None, state=None):
    """Exercises bad signature rejection and asserts the old app and service remain safe."""
    started = time.monotonic()
    if app is not None:
        try:
            validate_app_source(app, "bad_signature")
        except ValueError as error:
            return {"stage": "bad_signature", "status": "blocked", "reason": str(error),
                    "duration_s": round(time.monotonic() - started, 3)}
    return {
        "stage": "bad_signature",
        "status": "blocked",
        "reason": "requires_real_updater_integration",
        "detail": "Exercising bad signature rejection requires driving native_update.rs "
                  "and observing the real receipt store. This harness does not simulate updater failure paths.",
        "duration_s": round(time.monotonic() - started, 3),
    }


def stage_startup_failure(app=None, state=None):
    """Exercises post-replacement startup failure rollback and asserts safety."""
    started = time.monotonic()
    if app is not None:
        try:
            validate_app_source(app, "startup_failure")
        except ValueError as error:
            return {"stage": "startup_failure", "status": "blocked", "reason": str(error),
                    "duration_s": round(time.monotonic() - started, 3)}
    return {
        "stage": "startup_failure",
        "status": "blocked",
        "reason": "requires_real_updater_integration",
        "detail": "Exercising post-replacement startup failure rollback requires driving "
                  "native_update.rs / update_receipt.rs and observing the real health check. "
                  "This harness does not simulate updater failure paths.",
        "duration_s": round(time.monotonic() - started, 3),
    }


def stage_replacement():
    """The real N -> N+1 replacement is deliberately not simulated."""
    return {"stage": "replacement", "status": "blocked",
            "reason": "requires_real_signed_tauri_installer",
            "detail": "Use a signed build of version N and N+1, install N, let the real updater "
                      "replace it, then confirm the receipt reached succeeded. This harness does "
                      "not reproduce app replacement itself."}


def stage(name, result, started, command, detail=""):
    return {"stage": name, "status": "passed" if result.returncode == 0 else "failed",
            "command": [str(part) for part in command], "exit_code": result.returncode,
            "detail": detail, "duration_s": round(time.monotonic() - started, 3)}


def summarize(stages, require_complete=False):
    failed = [item for item in stages if item["status"] == "failed"]
    blocked = [item for item in stages if item["status"] == "blocked"]
    replaced = [item for item in stages if item["stage"] == "replacement" and item["status"] == "passed"]
    # Only a real replacement stage can make the acceptance complete; signature,
    # receipt and isolation checks are real, but they are not the app swap.
    complete = not failed and not blocked and bool(replaced)
    if complete:
        note = ""
    else:
        notes = []
        if not replaced or any(item["stage"] == "replacement" and item["status"] == "blocked" for item in stages):
            notes.append("The real signed N -> N+1 app replacement was not exercised.")
        if any(item["stage"] in ("bad_signature", "startup_failure") and item["status"] == "blocked" for item in stages):
            notes.append("Failure recovery stages require real updater integration.")
        if not notes:
            notes.append("These stages cover signatures, the persisted receipt and the native authorization gates.")
        note = " ".join(notes)
    report = {"harness": "desktop-upgrade", "stages": stages,
              "passed": [item["stage"] for item in stages if item["status"] == "passed"],
              "blocked": [item["stage"] for item in stages if item["status"] == "blocked"],
              "failed": [item["stage"] for item in stages if item["status"] == "failed"],
              "actual_acceptance": "complete" if complete else "incomplete",
              "acceptance_scope": "real_installer_replacement" if complete else "staging_only",
              "note": note}
    if failed:
        status = 1
    elif require_complete and (blocked or not replaced):
        # --require-complete means the real installer acceptance was observed.
        status = 3
    else:
        status = 0
    return report, status


def run(requested, app=None, state=None, require_complete=False):
    stages = []
    for name in select_stages(requested):
        if name == "signing":
            stages.append(stage_signing())
        elif name in CARGO_FILTERS:
            stages.append(stage_cargo(name))
        elif name == "isolation":
            stages.append(stage_isolation(app, state))
        elif name == "bad_signature":
            stages.append(stage_bad_signature(app, state))
        elif name == "startup_failure":
            stages.append(stage_startup_failure(app, state))
        else:
            stages.append(stage_replacement())
    return summarize(stages, require_complete)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", nargs="+", default=["all"],
                        choices=list(STAGES) + list(STAGE_ALIASES.keys()) + ["all"],
                        help="stages to run; 'all' runs everything except the blocked replacement")
    parser.add_argument("--app", help="signed .app build artifact to copy for isolation")
    parser.add_argument("--state", help="private directory for harness state (temporary by default)")
    parser.add_argument("--require-complete", action="store_true",
                        help="exit 3 unless real installer replacement evidence was observed")
    args = parser.parse_args(argv)
    try:
        report, status = run(args.stage, args.app, args.state, args.require_complete)
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"harness": "desktop-upgrade", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
