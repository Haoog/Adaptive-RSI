#!/usr/bin/env python3
"""Offline reproduction and verification entry point for sealed experiment bundles."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adaptive_rsi.evolution import Evolution


def format_report(result):
    lines = []
    lines.append("=== 封存实验包离线复现检查 / Sealed Bundle Reproduction Inspection ===")

    integ = result.get("integrity", {})
    if integ.get("valid"):
        lines.append("[OK] 输入完整性校验 / Input Integrity: 通过 (All bundle files verified)")
    else:
        mismatches = integ.get("mismatches", [])
        lines.append(f"[FAIL] 输入完整性校验 / Input Integrity: 失败 ({len(mismatches)} mismatches)")
        for m in mismatches:
            lines.append(f"     - 损坏或缺失文件: {m}")

    deps = result.get("dependencies", {})
    avail_deps = deps.get("available", [])
    missing_deps = deps.get("missing", [])
    lines.append(f"\n[-] 宿主工具与执行依赖 / Host Dependencies ({len(avail_deps)} 可用, {len(missing_deps)} 缺失):")
    for d in avail_deps:
        lines.append(f"     [+] {d['name']}: {d.get('path', 'available')}")
    for d in missing_deps:
        reason = d.get("reason", "not found")
        cases = f" (cases: {', '.join(d.get('cases', []))})" if d.get("cases") else ""
        lines.append(f"     [!] {d['name']}{cases}: 缺失 ({reason})")

    creds = result.get("credentials", {})
    avail_creds = creds.get("available", [])
    missing_creds = creds.get("missing", [])
    lines.append(f"\n[-] 模型凭据与外部服务 / Credentials & Services ({len(avail_creds)} 已配置, {len(missing_creds)} 缺失):")
    for c in avail_creds:
        lines.append(f"     [+] {c['name']}: 已配置")
    for c in missing_creds:
        lines.append(f"     [!] {c['name']}: 缺失 ({c.get('reason', 'not set')})")

    lines.append("\n[-] 限制说明 / Limitations:")
    for i, lim in enumerate(result.get("limitations", []), 1):
        lines.append(f"     {i}. {lim}")

    executed = result.get("executed_checks", [])
    if executed:
        lines.append(f"\n[-] 离线用例执行 / Executed Checks ({len(executed)}):")
        for chk in executed:
            status = "PASS" if chk.get("passed") else "FAIL"
            lines.append(f"     [{status}] {chk.get('case')} - {chk.get('check')}: exit {chk.get('exit_code')}")

    lines.append("\n=== 结论 / Summary ===")
    if result.get("reproducible"):
        lines.append("完整性通过且所有依赖与凭据齐备，具备离线复现条件。")
    elif not integ.get("valid"):
        lines.append("输入完整性校验失败，实验包文件损坏或被篡改，无法安全复现。")
    else:
        lines.append(f"输入完整性校验通过。缺凭据 ({len(missing_creds)} 项) 或外部依赖 ({len(missing_deps)} 项)，已逐项列出上述限制。")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", help="Path to sealed reproduction bundle directory")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON result")
    parser.add_argument("--run-checks", action="store_true", help="Execute evaluation checks if dependencies are satisfied")
    parser.add_argument("--strict", action="store_true", help="Exit with non-zero code if any dependency/credential is missing")
    args = parser.parse_args(argv)

    bundle_path = Path(args.bundle).expanduser().resolve()
    if not bundle_path.is_dir():
        print(f"Error: Bundle directory not found: {args.bundle}", file=sys.stderr)
        return 1

    result = Evolution.reproduce_bundle(bundle_path, run_checks=args.run_checks)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))

    if not result.get("valid", False):
        return 1
    if args.strict and not result.get("reproducible", False):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
