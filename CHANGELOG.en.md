# Changelog

[简体中文](CHANGELOG.md) | **English**

## Unreleased

- Tasks judged obsolete by planning are no longer requeued by inspections (avoiding repeated paid planning every scan cycle); manual retry and requirement supplements remain available for obsolete tasks.
- A failed desktop update check no longer displays "up to date"; check errors are now distinct from "no update".
- After an automatic-update rollback, the previous version must pass its startup health check before the system reports ready; startup waits for the new candidate and the rollback are unified at 60 seconds so large state directories are no longer misjudged.
- The English anti-flash bootstrap now has a 3-second safety net, so a broken translation script can no longer leave the page blank forever.
- Sandbox snapshots exclude `.rsi-tmp`, so setup leftovers no longer pollute candidate patches; publishing retries use `checkout -B` so an existing branch no longer fails.
- Authentication token comparison treats non-ASCII input as a mismatch instead of raising and resetting the connection; CSP now includes `form-action 'self'`.
- Manual retry resets the infrastructure backoff counter; recurrence requeue of merged tasks also resets attempt counters.
- Native APIs tolerate null usage fields, and empty Gemini candidates return a diagnosable error instead of crashing.
- OpenCode CLI timeouts, rate limits and connection failures can now trigger the configured fallback model.
- Dark mode covers the model center and dialog accent variables; desktop exit force-kills a stuck engine after 10 seconds; the update download no longer has a double-click race; the unused Tauri global API is disabled.
- Unknown UI errors now show a generic message (the original text stays in the browser console); the release smoke retries transient GitHub API network errors.

## 0.5.105

<!-- adaptive-rsi-release:0.5.105;base:9b3de09c0ec50af7a9557b509b14d662c79a60a8 -->

- Fixed sandboxed commands running without a timeout: the command timeout is a safety boundary, not a quota. It now defaults to 1 hour (configurable 1–24 hours), so a hung check can no longer hold a run slot forever.
- OpenCode executor: task and event files are always written as UTF-8, so Chinese tasks no longer fail under non-UTF-8 locales; call failures now surface timeout/rate-limit details (redacted first) so transient connection faults retry automatically and feed the provider circuit breaker; OAuth write-back tolerates a corrupt auth.json and no longer hides an already-completed model call.
- Fixed findings trapped by the fingerprint dedup: when a previous attempt failed or expired, rediscovering the same issue requeues it with reset counters; explicitly rejected and user-cancelled decisions stay suppressed.
- Failure backoff is capped at 6 hours and manual retry can pull a backing-off task forward; periodically scheduled system tasks back off by consecutive-failure count instead of producing a new failed run every cycle.
- When a dependency reaches a terminal state where it will never merge, the wait is released automatically, the task is blocked with a visible reason, and a manual retry runs it independently.
- Cancelling can no longer rewrite terminal states such as merged or PR-opened; before publishing, an approved candidate re-verifies that its source task was not cancelled or expired.
- Ordinary Issue edits no longer reset delivered tasks to the queue; only reopening a closed Issue requeues them. Issue comment totals now use the authoritative snapshot count so deleted comments no longer distort notices.
- New **Rotate access link** action (data and diagnostics): one click invalidates the old token and sessions, updates server.json, and survives restarts.
- The HTTP service now enforces a socket timeout and a concurrent-connection cap, so slow connections cannot exhaust threads; missing backups return a clear message instead of leaking server paths.
- Very short secret values are excluded from redaction to keep output parsing and usage accounting intact; rolling back a strategy twice is idempotent; a damaged trailing line in a model catalog is skipped instead of failing the whole list; OpenCode versions accept four segments; pytest detection covers conftest.py and pytest.ini; 0.0.0.0 loopback addresses are rewritten inside Docker workers; empty-run detection covers more framework outputs; scheduler config validation is cached by content; DST-boundary schedule display is more stable.
- Console: system dark mode; confirmation dialogs follow the interface language instead of native confirm; timestamps consistently use the project timezone; the page title tracks the selected project; the English interface no longer flashes Chinese first; the PWA icon supports maskable; the static shell opens offline (API and private data are still never cached); desktop update failures use structured error codes.

## 0.5.104

<!-- adaptive-rsi-release:0.5.104;base:4cea07180b463817c84d1322c2f84b098e7c36d8 -->

- Release verified mainline improvements from commit `4cea07180b463817c84d1322c2f84b098e7c36d8`.

## 0.5.103

<!-- adaptive-rsi-release:0.5.103;base:0ec3e61d6e0d1603c73fbaacc379c851a6ec14ac -->

- Stop the scheduler from hot-retrying quota-waiting tasks while no quota exists: quota failures now wait with increasing backoff before resuming, and after the automatic retry cap the task becomes blocked until manually retried; context or output overflow is no longer misclassified as a quota failure.
- OpenCode and Codex model calls no longer serialize end to end; execution outside credential reads and writes can run concurrently, and remote baseline refreshes now run per project in parallel.
- Native API response timeouts are relaxed by role to support slower reasoning models; unparseable tool arguments or schema-invalid finish results are reported back to the model to self-correct instead of failing the run.
- Legacy deferred tasks stay deferred on their first reassessment after an upgrade and only re-enter the queue when the baseline or inputs change.
- Write requests from the loopback origin are no longer rejected when a public URL is configured; login-session polling backs off, the provider list is cached briefly, and the auth service retries on a fresh port if its port is stolen.
- Database connections are reused through a bounded pool and rebuilt after file replacement; maintenance cleanup moved off the scheduler thread; several queries and file reads were deduplicated.
- Added missing English UI translations and removed duplicate entries.

## 0.5.102

<!-- adaptive-rsi-release:0.5.102;base:d27e35c636459847c86df3a67b9e8221f671773e -->

- Reduce module-inspection context and duplicate candidate checks; directly run new regression tests in isolation on the baseline and candidate for behavior, configuration and UI changes, falling back to independent acceptance for unsupported runners; retain high-risk and performance acceptance.

## 0.5.101

<!-- adaptive-rsi-release:0.5.101;base:8bec79619d0167e22b6c26daeaa501bd1b16d189 -->

- Support OpenCode 1.19.3 and 2.1.3 with version-aware model/authentication adapters, guided connection steps and actionable compatibility diagnostics.
- Batch-triage inspection findings and run independent acceptance and evaluation in parallel; reuse baselines across tasks only with the same immutable Docker image and dependencies, never across host toolchain runs.
- Optimize Issue comment window retrieval, Issue sync time slicing, code context indexing and Docker probe caching.
- Rebalance CI matrices and add build caches; ordinary code PRs smoke-test the bundled desktop engine, while formal releases prefer verified artifacts from the same source tree.

## 0.5.1

<!-- adaptive-rsi-release:0.5.1;base:6b94286895361bf0273a8af49f0f56482dd98fa7 -->

- Add actionable retry feedback, independent connection backoff, deferral wake conditions, dependency/urgency queues and task-level workflow timing and quality metrics.
- Restore mobile navigation and provide a PWA installation page with the actual installation URL; add live details, lazy history, acceptance comparisons, attention notifications and idempotent submission.
- Add same-task requirement revisions, typed evidence contracts, backup/restore and redacted diagnostics while retaining independent acceptance and delivery authorization gates.
- Distinguish documentation from code checks; run grouped Python tests, browser, JavaScript, packaging and Linux checks in parallel, and cancel unfinished checks for an older commit when a newer push arrives.
- Reuse planning, acceptance, implementation and review caches by the settings each stage actually uses; skip model inspection for documentation-only changes and prioritize existing repair work over more findings.
- Use code content to locate files for Chinese goals, compare similar findings by evidence to avoid duplicates while preserving distinct defects.
- Show the current stage, elapsed time, latest progress and actionable recovery paths in the workspace; group usage by role and browser timezone, and distinguish configured, tool-tested and real-task-verified models.
- Expand automatic PR descriptions with the problem, change, verification and residual impact; ordinary releases advance only the patch component unless a new product stage is explicitly confirmed.


## 0.5.0

<!-- adaptive-rsi-release:0.5.0;base:37537b758a733b95ffe4a3db31deb512ebf185da -->

- Accumulate ordinary merges without version bumps; prepare version PRs only for explicitly started stable release batches.
- Add a Tauri desktop app with a bundled local engine, tray lifecycle, explicit project start, native server connection and folder picker.
- Add task-based usage reporting including failures, retries and unknown usage, with project/date filters and call details.
- Use one searchable model chooser per role, and add desktop build checks and bilingual documentation.
- Reap the desktop runtime on every failed startup path and quietly handle responses to browser-aborted requests.
- This release also includes a macOS ARM64 desktop DMG and SHA-256; the temporary signature is not Apple-notarized.
- Complete the usage page's English translations for refresh, search and custom date labels.
- Close stale usage details on project or filter changes so delayed responses cannot appear under another project.

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
