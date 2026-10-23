# Workflow and mobile use

[简体中文](EXPERIENCE.md) | **English**

## Install on your phone

Choose **Install on phone** in the console sidebar and open the actual installation link. Alternatively, append `/install` to your HTTPS console origin. The installation page contains no login credential and can be saved. Use login links containing `?token=` only on your own devices.

- Android: open the page in Chrome, tap Install, or use Install app in the browser menu.
- iPhone / iPad: open it in Safari, then Share → Add to Home Screen. Opening from the home screen may require logging in again.
- This is a browser-installed PWA without an APK or App Store package. Execution remains on the computer or server; the phone provides monitoring and controls.

Your phone cannot reach your computer's `127.0.0.1`. Configure a reachable HTTPS origin using [Server deployment](SERVER.en.md), sign in on your phone with the runtime-generated login link, then open installation. Neither the manifest nor shortcut stores a token. The service worker does not cache API responses, code or private pages. A network connection is required.

## Tasks and recovery

The submit button retains a request ID for the same unconfirmed submission. Retrying an ambiguous response returns the original task; submitting after success creates a new task. CLI users can specify `goal --request-id`. Reusing an ID with different content is rejected; project submissions remain isolated.

The queue shows ordering, urgency, and reasons for waiting: pause, backoff, working hours, current work or a predecessor. Urgency is separate from value. A waiting task can depend on another task in the same project. Once that task merges, downstream work starts from a fresh baseline. Cycles are rejected, and independent work can proceed.

Deferral decisions persist independently of runs, including reason, input fingerprint, baseline and wake conditions. Identical inputs do not requeue repeatedly. When specific paths are known, unrelated changes do not wake a task; uncertain relationships trigger conservative reassessment. Supplement requirements on the same task to retain revisions and invalidate stages based on older inputs.

Timeouts, rate limits and transient network failures schedule up to three retries with jittered backoff, separately from code repair attempts. Every retry creates a fresh isolated run. Authentication, protocol, checks and review failures do not receive transient recovery. Resuming a connection in Model Center only resumes matching temporary failures for that project and connection revision. Cancellation, pause, old connections and uncertain remote deliveries cannot be bypassed.

Retry feedback retains specific review locations, consequences and suggestions, plus bounded failed regression summaries. Protected independent acceptance sources remain excluded. Rejected candidates still require implementation, checks and review again. Plans can declare behavior, performance, refactor, documentation, configuration or UI changes. Defect fixes still need behavioral evidence and cannot evade gates through reclassification.

## Results and attention

Task details load the latest run first and fetch historical runs on expansion. Current state updates periodically; task URLs support direct navigation. Saved checkpoints are distinguished from actual execution. No fabricated percentages or completion estimates are shown. Results include independent before/after acceptance, review findings and readable diffs; original evidence is preserved.

The **Attention inbox** covers every project. System notifications require opt-in and fire on new states or reasons. Unchanged polling stays quiet. Denied notification permission does not remove inbox items, and viewing a notification does not resolve work. Browser notifications navigate to tasks; desktop notifications provide alerts, with details in the inbox. Web Push while the page or desktop client is closed is not provided.

**Records and usage → Workflow quality and timing** reports logical tasks created in the last 30 days: first pass, acceptance, merges, infrastructure failures, repeated failures, revisions and reused stages. Accumulated phase durations include parallel runs and are not total elapsed time. Unknown usage remains explicit rather than becoming zero. These metrics do not establish model capability improvements.

## Backups and retention

**Data and diagnostics** provides storage totals, consistent online SQLite backups, downloads and diagnostic previews. Backups contain private task content and configuration; keep them private. Diagnostic summaries include only version, counts, failure codes and storage size, excluding credentials, paths, code, task text and full prompts.

Restore into a new state directory:

```sh
adaptive-rsi --state-dir /NEW/STATE restore /PATH/BACKUP.sqlite3
```

Existing state is never overwritten. Restored projects remain paused, active runs become interrupted, old candidate approvals expire, and queued deliveries are blocked for reconciliation. Database backups exclude candidate workspaces and machine credentials. On another machine, reconnect credentials, verify project paths and remote delivery state before enabling projects.

Candidate files expire under project retention settings and cannot be approved afterward. Retry expired tasks to verify the latest code again. Original summaries remain; cleanup does not imply successful delivery.
