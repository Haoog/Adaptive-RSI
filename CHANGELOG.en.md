# Changelog

[简体中文](CHANGELOG.md) | **English**

## Unreleased

## 0.4.1

<!-- adaptive-rsi-release:0.4.1;base:76a70822c28febf882a4b049c2da848ae972b817 -->

- Release verified mainline improvements from commit `76a70822c28febf882a4b049c2da848ae972b817`.

## 0.4.0

<!-- adaptive-rsi-release:0.4.0;base:bb1374a9866466db3b6a3853d61c1679b39a26a7 -->

- Start official Codex device authorization or select an OpenCode provider for authorization and API-key setup in model settings; cancel, sign out, reconnect and refresh models after connecting.
- Explicitly authorize pinned release and deployment workflows per project, track outcomes, reconcile interrupted requests and retry confirmed failures while retaining each project's existing delivery settings.
- Prepare version PRs automatically, independently validate version changes, build and smoke-test installation/upgrades, publish official releases and configure the scope of stable automatic updates.
- Simplify onboarding and show value, verification and delivery outcomes; preserve distinct findings at the same source location and consider current related-work results during planning.

## 0.3.0

- Add an OpenCode CLI executor using the runtime machine's providers and API/OAuth authentication, with model discovery and variant selection.
- Support isolated Linux Docker execution, a persistent systemd installer, SSH access, HTTPS proxy configuration and private persistent credential files.
- Default project inspection and GitHub Issue sync to hourly; remove separate deep inspection, rotate source modules and paginate all matching Issues with durable continuation.
- Remove call, run-duration, command-duration, changed-file and experiment quotas; retain input/output/cache tokens and unknown-usage reporting.
- Preserve value assessment, independent acceptance, regression, code review and per-project delivery authorization, plus pause, cancellation, request timeouts and log protection.

## 0.2.3

- Protect common test filenames across more languages and configured check entrypoints from candidate weakening.
- Give read-only review the host-sealed patch; cancellation or timeout now stops subsequent auto-merge side effects.
- Recheck Issue state, revision and labels before auto-merge; stale candidates wait for synchronization before reassessment.
- Refuse Codex fallback after primary implementation edits, and keep goal drafts scoped to the displayed project.

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
