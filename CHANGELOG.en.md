# Changelog

[简体中文](CHANGELOG.md) | **English**

## 0.2.2

- Default-deny reads outside Codex snapshots while allowing system toolchains.
- Recheck pause, cancellation and current authorization before pushes and PR creation; allow fresh manual approval of failed deliveries.
- Route built-in Ollama and LM Studio through native APIs, aligning execution, readiness, model probes and accounting.
- Surface failed GitHub checks under Needs your attention while retaining recovery and PR tracking.
- Filter Issues server-side by update time while preserving pagination, overlapping watermarks and active revisions.
- Alternate and record paired experiment order; poll post-merge checks on schedule and stop after retention.
- Protect main with PRs, successful up-to-date checks and resolved conversations, including administrators.

## 0.2.1

- Simplify task outcomes; add readiness, recovery suggestions, project pagination, and preserved forms.
- Persist stage records. Changed environments, baselines, configuration, or evidence invalidate reuse; restored patches are checked again.
- Add durable Issue paging, revision checks, value ordering, and waiting-time aging.
- Require before/after behavioral evidence for ordinary changes; use preservation contracts for refactors, reverts, and post-merge monitoring.
- Share production execution with strategy experiments; validate before exposing holdouts, and promote conservatively using independent families and repeated trials with scoped monitoring.
- Add code anchors, bounded context, call units, and separate real-task evidence.
- Add build identities, real-browser CI, version checks, and a manually triggered draft-release workflow.

Existing authorization defaults and model assignments remain. Experiments default to two repetitions. Insufficient cases or budget stop work without raising limits. See [Reliable improvement loop](RELIABILITY.en.md) for behavior and validation limits.
