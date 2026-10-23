"""Task evidence contracts shared by production and experiment runs."""


def change_kind(task, plan=None):
    # Only explicit behavior-preserving requests can use the preservation contract.
    title = task["title"].lower()
    if any(word in title for word in ["fix", "bug", "修复", "错误", "缺陷"]):
        return "behavior"
    declared = (plan or {}).get("change_kind")
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
    # An unrelated import/setup failure is not evidence of an observed improvement.
    import re
    changed = [i for i, (old, new) in enumerate(zip(before, after)) if not old["passed"] and new["passed"]
               and not old.get("error") and old.get("exit_code", 0) != 0
               and not re.search(r"ModuleNotFoundError|ImportError|command not found|No such file or directory",
                                 old.get("stderr", ""))]
    return {"decision": "pass" if changed else "insufficient_evidence", "kind": kind,
            "improved_checks": changed, "basis": "independent_behavior_before_after"}
