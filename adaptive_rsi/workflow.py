"""Task evidence contracts shared by production and experiment runs."""
import json
import math


def change_kind(task, plan=None):
    # Only explicit behavior-preserving requests can use the preservation contract.
    title = (task["title"] + "\n" + task.get("body", "")).lower()
    if any(word in title for word in ["fix", "bug", "broken", "incorrect", "修复", "错误", "缺陷", "失效", "异常"]):
        return "behavior"
    declared = (plan or {}).get("change_kind")
    if declared == "refactor" and not any(word in title for word in ["refactor", "重构", "保持行为不变", "preserve behavior"]):
        return "behavior"
    if declared in ["behavior", "performance", "refactor", "documentation", "configuration", "ui"]:
        return declared
    if any(word in title for word in ["refactor", "重构", "保持行为不变"]):
        return "refactor"
    if any(word in title for word in ["performance", "benchmark", "性能", "提速", "降低", "延迟", "吞吐"]):
        return "performance"
    return "behavior"


def improvement(results, acceptance):
    if not acceptance:
        return {"decision": "not_requested", "kind": "regression_only"}
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
        def measurements(rows):
            values = []
            for row in rows:
                for line in row.get("stdout", "").splitlines():
                    try:
                        sample = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(sample, dict) and sample.get("metric") == threshold["metric"] and sample.get("unit") == threshold["unit"]:
                        value = sample.get("value")
                        if type(value) in [int, float] and math.isfinite(value):
                            values.append(value)
            return values
        old, new = measurements(before), measurements(after)
        if len(old) != 1 or len(new) != 1:
            return {"decision": "insufficient_evidence", "kind": kind, "basis": "one_matching_measurement_required_per_version"}
        target, tolerance = threshold["target"], threshold["tolerance"]
        passed = (new[0] <= target + tolerance and old[0] - new[0] > tolerance if threshold["direction"] == "lower"
                  else new[0] >= target - tolerance and new[0] - old[0] > tolerance)
        return {"decision": "pass" if passed else "insufficient_evidence", "kind": kind,
                "before": old[0], "after": new[0], "threshold": threshold, "basis": "independent_benchmark_measurements"}
    # An unrelated import/setup failure is not evidence of an observed improvement.
    import re
    changed = [i for i, (old, new) in enumerate(zip(before, after)) if not old["passed"] and new["passed"]
               and not old.get("error") and old.get("exit_code", 0) != 0
               and not re.search(r"ModuleNotFoundError|ImportError|command not found|No such file or directory",
                                 old.get("stderr", ""))]
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
    if kind == "performance":
        threshold = plan.get("performance_threshold")
        if not isinstance(threshold, dict) or not threshold.get("metric") or not threshold.get("unit") or threshold.get("direction") not in ["lower", "higher"]:
            raise ValueError("Performance plan requires a named metric, unit, target, tolerance and direction")
        if any(type(threshold.get(k)) not in [int, float] or not math.isfinite(threshold[k]) for k in ["target", "tolerance"]) or threshold["tolerance"] < 0:
            raise ValueError("Performance threshold must be finite and tolerance nonnegative")
        contract["threshold"] = threshold
    return contract
