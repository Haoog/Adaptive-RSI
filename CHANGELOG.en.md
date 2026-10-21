# Changelog

[简体中文](CHANGELOG.md) | **English**

## 0.2.1

- Simplify task outcomes; add readiness, recovery suggestions, project pagination, and preserved forms.
- Persist stage records. Changed environments, baselines, configuration, or evidence invalidate reuse; restored patches are checked again.
- Add durable Issue paging, revision checks, value ordering, and waiting-time aging.
- Require before/after behavioral evidence for ordinary changes; use preservation contracts for refactors, reverts, and post-merge monitoring.
- Share production execution with strategy experiments; validate before exposing holdouts, and promote conservatively using independent families and repeated trials with scoped monitoring.
- Add code anchors, bounded context, call units, and separate real-task evidence.
- Add build identities, real-browser CI, version checks, and a manually triggered draft-release workflow.

Existing authorization defaults and model assignments remain. Experiments default to two repetitions. Insufficient cases or budget stop work without raising limits. See [Reliable improvement loop](RELIABILITY.en.md) for behavior and validation limits.
