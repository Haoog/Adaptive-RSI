# Workflow and mobile use

[简体中文](EXPERIENCE.md) | **English**

## Install on your phone

Choose **Install on phone** in the console sidebar and open the actual installation link. Alternatively, append `/install` to your HTTPS console origin. The installation page contains no login credential and can be saved. Use login links containing `?token=` only on your own devices.

- Android: open the page in Chrome, tap Install, or use Install app in the browser menu.
- iPhone / iPad: open it in Safari, then Share → Add to Home Screen. Opening from the home screen may require logging in again.
- This is a browser-installed PWA without an APK or App Store package. Execution remains on the computer or server; the phone provides monitoring and controls.

Your phone cannot reach your computer's `127.0.0.1`. Configure a reachable HTTPS origin using [Server deployment](SERVER.en.md), sign in on your phone with the runtime-generated login link, then open installation. Neither the manifest nor shortcut stores a token. The service worker does not cache API responses, code or private pages. A network connection is required.

Minimum requirements follow the browser: the console needs a modern browser equivalent to iOS 15.4+ or Android 8+ (requires `structuredClone` and PWA support). Older systems render a blank page; upgrade the system or use the desktop app instead.

## Tasks and recovery

The submit button retains the original text, content digest and request ID for up to 32 unconfirmed goals per project in the current browser session. After changing text or reloading, select an unconfirmed goal in the form to restore its text, then submit explicitly using its original ID. Restoring replaces the current draft without sending automatically. Success removes only the matching record, freeing capacity for new goals. Legacy digest-only records require the original text to match. Invalid records or storage failures prevent sending without a retained deduplication ID. Retrying an ambiguous response returns the original task; submitting after success creates a new task. CLI users can specify `goal --request-id`. Reusing an ID with different content is rejected; project submissions remain isolated.

Shared and project settings remain editable while saving. Later drafts survive project switches, including edits back to values from before the save, and can be explicitly saved or undone on return. Success confirms only the submitted values and compares unsaved edits against the newly saved values.

Newly claimed tasks use the settings saved before their claim; active runs retain their sealed settings. Pausing retains a stop request for current runs, and quickly resuming does not withdraw it. Interrupted tasks can be explicitly retried. Editing or removing a model connection retains its old credential until referencing runs finish, after which periodic maintenance removes it.

The queue shows ordering, urgency, and reasons for waiting: pause, backoff, working hours, current work or a predecessor. Urgency is separate from value. A waiting task can depend on another task in the same project. Once that task merges, downstream work starts from a fresh baseline. Cycles are rejected, and independent work can proceed.

After cancellation, retrying or supplementing requirements waits until the active run has stopped completely. The dashboard explains this wait. Manual retries and requirement supplements invalidate undelivered candidates and their queued approvals, requiring fresh verification. Existing PRs and merge records remain available. Failed remote closes of stale PRs retain reconciliation intent; old tracking cannot terminate replanned work. Local Issue supplements are stored separately from upstream text and combined into new task input during synchronization. Historical supplements cannot be recovered automatically if both the overwritten text and its revision history have already been removed.

Deferral decisions persist independently of runs, including reason, input fingerprint, baseline and wake conditions. Identical inputs do not requeue repeatedly. Only an explicitly complete dependency-path record can suppress unrelated changes. Implementation scope or an issue location alone is insufficient; uncertain relationships trigger conservative reassessment. Supplement requirements on the same task to retain revisions and invalidate stages based on older inputs.

Ordinary code tasks schedule up to three retries with jittered backoff for timeouts, rate limits and transient network failures, separately from code repair attempts. Every retry creates a fresh isolated run. Background inspections and strategy experiments retain their existing schedules and manual retry path; they are outside this automatic backoff and connection-resume scope. Authentication, protocol, checks and review failures do not receive transient recovery. Resuming a connection in Model Center only resumes matching temporary failures for that project and connection revision. Cancellation, pause, old connections and uncertain remote deliveries cannot be bypassed.

Retry feedback retains specific review locations, consequences and suggestions, plus bounded failed regression summaries. Protected independent acceptance sources remain excluded. When protected samples exist, a feedback field rewritten by URL or credential redaction is hidden in full to avoid exposing sample remnants; other fields still provide bounded specific feedback. Rejected candidates still require implementation, checks and review again. Plans can declare behavior, performance, refactor, documentation, configuration or UI changes. Defect fixes still need behavioral evidence and cannot evade gates through reclassification.

Performance tasks freeze a metric, unit, target, tolerance and direction before implementation. Independent checks report measurements for each version. The host verifies the target and an improvement exceeding tolerance; missing thresholds or matching measurements yield insufficient evidence. Preservation contracts require an explicitly behavior-preserving request. Other types demonstrate the requested before/after change and still require fixed regressions and independent review.

## Results and attention

Task details load the latest run first and fetch historical runs on expansion. Current state updates periodically; task URLs support direct navigation. Saved checkpoints are distinguished from actual execution. No fabricated percentages or completion estimates are shown. Results include independent before/after acceptance, review findings and readable diffs; original evidence is preserved.

The **Attention inbox** covers every project. System notifications require opt-in and fire on new states or reasons. Unchanged polling stays quiet. Denied notification permission does not remove inbox items, and viewing a notification does not resolve work. Browser notifications navigate to tasks. When the desktop notification service reports a click, the window opens the corresponding task. Notifications from a previously connected runtime cannot navigate into another runtime. Web Push while the page or desktop client is closed is not provided.

Usage list searches update after typing stops. Earlier responses cannot cancel new searches or replace later results; switching projects or leaving the page invalidates old requests.

**Records and usage → Workflow quality and timing** reports logical tasks created in the last 30 days: first pass, acceptance, merges, infrastructure failures, repeated failures, revisions and reused stages. Accumulated phase durations include parallel runs and are not total elapsed time. Unknown usage remains explicit rather than becoming zero. These metrics do not establish model capability improvements.

Metrics also report average time to PR and merge with sample counts, repeated failure signatures, user interventions, tokens per accepted/delivered task, invalidation reasons and recorded CI/review waits. Missing historical wait durations are not fabricated. Preparation, model calls, checks and review are recorded separately.

## Backups and retention

**Data and diagnostics** provides storage totals, consistent online SQLite backups, downloads and diagnostic previews. Backups contain private task content and configuration; keep them private. Diagnostic summaries include only version, counts, failure codes and storage size, excluding credentials, paths, code, task text and full prompts.

Backups remain absent from the download list while being generated. They become downloadable only after copying, portability preparation and file closure, preventing incomplete snapshots from being exported as successful backups. Failures remove staging files without overwriting existing backups.

Restore into a new state directory:

```sh
adaptive-rsi --state-dir /NEW/STATE restore /PATH/BACKUP.sqlite3
```

Existing state is never overwritten. Restored projects remain paused, active runs become interrupted, old candidate approvals expire, and queued deliveries are blocked for reconciliation. Imported runs stop using source-state artifact paths and local verification caches expire; known PR receipts and remote delivery records remain. Missing acceptance or dependency artifacts produce insufficient evidence during post-merge verification. Supply trusted artifacts inside the current state directory or verify again; evidence from another directory cannot be reused. Database backups exclude candidate workspaces and machine credentials. On another machine, reconnect credentials, verify project paths and remote delivery state before enabling projects.

Candidate files expire under project retention settings and cannot be approved afterward. Retry expired tasks to verify the latest code again. Original summaries remain; cleanup does not imply successful delivery.
