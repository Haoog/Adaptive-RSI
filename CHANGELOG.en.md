# Changelog

[简体中文](CHANGELOG.md) | **English**

## Unreleased

## 0.5.121

<!-- adaptive-rsi-release:0.5.121;base:481d0238943045910ee4592fd2c6912529ad6e94 -->

- The self-improvement page refreshes candidates and statistics immediately after a rollback, evaluation-case import or manual promotion instead of reusing the 30-second cache; other polling keeps its cache interval.
- When automatic promotion is off, a verified candidate can be enabled explicitly from the interface. The server accepts only `ready` candidates, validates the project and kind in a transaction, supersedes the prior stable strategy of the same project and kind, and rebinds the candidate parent to the stable it just replaced so rollbacks walk back through the immediately preceding version. It writes the same verified skill as automatic promotion (preserving paired evidence, 30-day expiry). Repeated or concurrent clicks succeed only once; automatic promotion and evaluation algorithms are unchanged.
- Manual promotion checks evidence compatibility against the current configuration and retains the stable strategy with reevaluation guidance when incompatible; working-hours waits no longer show an invalid “Retry now” action.
- The self-improvement page shows used/limit exposure per split and separate independent-case and paired-run counts for candidates. Exhausted exposure explains how to import new cases or raise `evolution.max_holdout_uses`, fixed controller reasons are mapped in Chinese and English, and the English subtitle is corrected.
- Supplement validation failures no longer degrade to a generic error: empty or whitespace-only input, more than 10,000 characters, and an Issue requirement combination over 30,000 characters show actionable messages inside the form in both languages; whitespace-only input is blocked in the browser without a request.
- A queued task waiting on automatic backoff shows “Retry now” in the task list and detail, runs early and clears its infrastructure retry count; ordinary queued tasks do not show this entry. Polling removes the entry once the wait expires naturally while preserving supplement drafts in the detail.
- Long unbroken text and long URLs in task sources, evidence and revision history wrap inside the detail at 390px mobile and desktop widths instead of scrolling horizontally.

## 0.5.120

<!-- adaptive-rsi-release:0.5.120;base:1adb167bd200e942256e7ea197c928986256eaa4 -->

- A failed model catalog refresh after sign-in no longer retries every 1.5 seconds forever: input rejections stop immediately with an actionable message, transient failures stop after four automatic retries, and the catalog can be refreshed manually or the account signed in again.
- Reloading the interface or switching the runtime machine while the desktop app is downloading a new installer image now resumes progress polling until completion or failure; the completion and failure notices match the non-reloaded flow instead of freezing at a percentage.
- Desktop update checks (including an explicit "Check for updates") keep an in-flight download's progress instead of clearing its downloading state, so the button cannot be clicked into a duplicate download; downloads are guarded by a process-wide gate, and no path can run two download threads writing the same installer image.
- After a download completes and the installer image opens, the button switches to a completed "Installer image is open. Drag to install." state that cannot be clicked into a full re-download; "Check for updates" remains the explicit way to re-check.
- "Remove connection" no longer deletes on a single click: a confirmation now explains that the connection and credentials stored on this machine (keychain or private file) are deleted irreversibly, that re-adding creates a new connection without restoring enabled/favorite/verified/price records, and that the server rejects removal while a role or fallback model still references the connection. Cancelling sends no request.
- Step 2 ("Account connection") of the model-center setup checklist no longer stays highlighted forever for key-based or local connections: it shows as done when no key is required or credentials are configured, and only stays current while credentials are missing. Codex/OpenCode official sign-in connections behave as before.
- The workbench "current task" card now shows the task title instead of a status word when the running task is outside the current task-list page (deep queues) or excluded by the "Needs you" and "History" groups; the card falls back to waiting copy once the run ends, and switching projects no longer leaks another project's running title.

## 0.5.119

<!-- adaptive-rsi-release:0.5.119;base:0d722d9a6bdbae28ee8a9e44c8ac6c4523733206 -->

- Deferred/rejected decisions from batch triage are recorded in the task decision store when the finding lands: tasks without any run now show the concrete reason in the task list and detail instead of a bare state badge, consistent with deferrals produced by full planning. Decision records persist with the task and are no longer affected by the 90-day derived-data cleanup; legacy internal triage-state keys from earlier upgrades are still migrated at the next deferred reassessment, and wake-up, deduplication and manual reconsideration behave unchanged.
- The task detail block "Deferral decision and wake conditions" renders structured content instead of raw JSON: the reason, the baseline (short SHA with the full commit on hover) and the wake condition (machine enums get Chinese/English copy; unknown enums are shown verbatim), while the raw decision record stays expandable. For rejected tasks the block title and guidance become "Rejection reason" instead of the deferral wording.
- Software update failure reasons are no longer shown as raw English in the Chinese interface: every fixed reason from interrupted checks, an unverifiable release feed, installation verification failures, rollback completion and recovery mode now comes with actionable Chinese guidance; `GitHub HTTP <code>` reasons render as a structured sentence, and unrecognized reasons keep the original text behind a "reason (verbatim)" prefix. The English interface still shows the original reason.
- The usage list disables the previous/next buttons while a page request is in flight and reclamps the offset to the current total, refetching the last page when the record count shrinks: fast paging or shrinking data no longer produces mismatched ranges such as "51–36" with an empty list. First/last page boundary disabling is unchanged, and searches, filters and time-range switches still return to the first page.
- A failed pagination request keeps the displayed page label consistent with the rendered rows; retrying requests the failed page again instead of skipping it.
- Role and phase names such as "Value screening", "Acceptance design" and "Strategy improvement" render in the interface language in usage details and the workflow quality dialog instead of raw identifiers; the "Workflow quality and timing" button updates immediately on language switches and stays correct after round trips.
- The task detail "Release & deploy" panel now explains five delivery-tracking server errors with actionable copy (post-merge verification wait, target already at this or a newer commit, deployment ownership changed during validation, health evidence discarded by a newer deployment, and a workflow run that does not match the authorized operation), translated in the English interface as well.
- Waiting delivery states (waiting for post-merge verification, for the previous deployment of the same service, and for the release to succeed) no longer render their notice in the red error style; a neutral style is used instead, while real failures (blocked, superseded, health check failed) stay red.
- When a deployment moves to "waiting for release" after post-merge verification passed, it no longer inherits the stale notice from the earlier verification wait, avoiding a message that contradicts the current state.

## 0.5.118

<!-- adaptive-rsi-release:0.5.118;base:947ad7b0cb1f3aa6c03c8a4e8de181adcae5a9ba -->

- System notifications now share one deduplication record across browser tabs: a state change alerted by one tab is no longer alerted again by the others, while the existing restore-after-reload semantics are preserved.
- A single failing system notification no longer swallows the rest of its batch: the failed item retries on the next poll with at most one hint per failure streak; when the constructor is unavailable (such as an iOS Home Screen app) the interface says so explicitly and resumes after recovery instead of failing silently forever.
- Notification deduplication entries are touched on each observation before trimming, so active attention items are no longer evicted unchanged past the 5000-entry bound and re-alerted; storage stays bounded.
- Failed saves of delivery authorization settings no longer show only a generic error: the seven server-side validation messages about delivery mode, workflow path, fixed tag, commit SHA, health URL and automatic rollback now come with actionable localized guidance in Chinese, with matching English translations.
- The full commit SHA inputs for release/deploy tags now validate exactly like the server (40 lowercase hex characters). Uppercase input is blocked before saving with an explanation and is never silently rewritten; the post-deploy health URL must be HTTPS without credentials, query or fragment already in the browser.
- When listing release/deploy workflows fails, the dropdown no longer stays on "Loading workflows…": it shows an error item and retries through the existing refresh button; a successful reload clears the stale failure message.
- When the project's GitHub repository does not match its local origin, the server returns the fixed validation message and the interface explains how to align the repository with the origin before retrying.
- When a historical run in the task detail fails to load, the section now shows an error message with a retry entry, and collapsing and reopening reloads it instead of staying blank forever; duplicate requests are not sent while one load is in flight.
- "Cancel task" in the task detail now covers every state the server allows cancelling: awaiting_approval, waiting_budget, blocked, deferred, insufficient_evidence, failed and interrupted tasks can all be cancelled. Cancelling a pending-approval candidate first confirms the consequences (the candidate is revoked, fresh verification is required; existing PRs are not affected).
- "Recheck with current inputs" inside the task detail is now also available for waiting_budget and obsolete tasks, matching the task list and the server's retryable states.

## 0.5.117

<!-- adaptive-rsi-release:0.5.117;base:366d91099b42a38e8c3c8c89bfe0372bcaa267a1 -->

- The project selector shows both name and full path, blank names fall back to the directory name, and same-name local repositories remain distinguishable. A newly added project is persisted immediately and stays selected after reload.
- Re-adding an existing path now reports the existing project name and paused state instead of silently ignoring the name or enabled option or claiming success.
- Pending-approval counts in Data and diagnostics exclude approved, delivering and cancelled candidates. In-progress and delivered states are reported separately without rewriting history.
- A failed restore removes only the new empty state directory it created and preserves the original error, so the same restore command can be retried.
- Restore-invalidated candidates record the restore reason; the interface explains that candidate files are not in the database backup instead of reporting retention cleanup.
- Open task details now update their title, body and evidence after Issue sync while preserving unsaved drafts; unchanged polls no longer rebuild the dialog.
- When no GitHub repository is configured, the Issue sync entry point is disabled, and the service rejects the request with a localized message instead of reporting success. Manually scheduling checks remains available regardless of source toggles.
- Incremental Issue sync counts only newly added tasks or actual body and evidence changes. Overlapping `since` scans are no longer recounted, and unfinished scans say so and continue next cycle.

## 0.5.116

<!-- adaptive-rsi-release:0.5.116;base:e7787fc46becf2b9649bab47bfd51b075e41b350 -->

- Catalog refreshes retain custom display names, manual model status and preferences such as pricing while updating service-provided names and capabilities, with compatibility for existing directory records.
- Blank custom model names use the full ID, fixing rejection of valid IDs with 161–256 characters. Connection forms no longer generate truncated default names, and Chinese and English hints explain the limits.

- Language changes, requirement supplements and task-detail rebuilds preserve unsaved predecessor and urgency choices, preventing later saves from unintentionally restoring old queue settings.
- The predecessor selector supports explicit pagination, retries and selection retention across pages, making same-project tasks beyond the first 200 selectable.
- Inspection and Issue sync read current settings under the schedule-write lock. Older scheduler ticks cannot overwrite deadlines set by interval saves or restored inheritance, or trigger disabled sources from stale snapshots.
- Next inspection times match working-hour eligibility, skipping entire windows missing during daylight-saving transitions and handling half-hour jumps, repeated windows and day changes.

## 0.5.115

<!-- adaptive-rsi-release:0.5.115;base:eeec1271316933db544cac66343b3484bb9c5497 -->

- Usage search manages debounce and automatic refresh separately and invalidates old requests on input, preventing earlier responses from cancelling new queries or replacing later results.
- Settings retain edits made while saving, including changes back to old values followed by a project switch. Acknowledgments preserve those drafts and compare them with the newly saved values.

## 0.5.114

<!-- adaptive-rsi-release:0.5.114;base:744220e7beba2eed0cecf32caa83deedf0c884ea -->

- Manually retrying a task atomically invalidates undelivered candidate approvals and queued publication, preventing an old candidate from publishing after fresh verification fails. Existing PR receipts remain.
- Stale PR replanning persists supersession and close intent before remote cleanup, allowing failed closes to reconcile. Old tracking cannot mark tasks closed after a new round begins.
- Issue source text and local requirement supplements are stored separately and recomposed for task input on synchronization, preserving repeated supplements and recoverable legacy revisions so saved requirements reach planning.

## 0.5.113

<!-- adaptive-rsi-release:0.5.113;base:87ebf190302c60b062f7db308a3f1f4a4bd0e9b1 -->

- Restoring a database disconnects source-state artifact paths and local verification caches while preserving history and remote delivery receipts. Artifact downloads and post-merge verification stay inside the current state directory, and missing required artifacts produce insufficient evidence.
- Each project retains the original text, digest and request ID for up to 32 unconfirmed goals in the browser session. The form can restore their text for explicit retries, preventing duplicate tasks after text changes or reloads. Success clears only its matching record and frees capacity; invalid records block sending with a dedicated error.
- Queue settings block overlapping saves so older requests cannot overwrite newer dependency or urgency choices, while retaining edits made after submission.
- Project model settings can restore inheritance even when effective values equal global defaults. The server resolves inheritance under the save lock using current shared settings, preventing old-value overrides when defaults change before saving. Acknowledgments clear only the submitted reset intent and retain later edits and project round-trip drafts.
- Switching projects or model scopes closes and invalidates old probe dialogs. Requests retain their original scope, and late results cannot overwrite another project's feedback.

## 0.5.112

<!-- adaptive-rsi-release:0.5.112;base:e4eaead279ee7cd823883168a7777c891a7613f1 -->

- The built-in updater recognizes exhausted GitHub anonymous API primary quotas, shows the retry time, and blocks checks and installation requests until reset. It retains installation consent for the original version without spending ordinary installation retries, then becomes eligible to retry at the deadline.
- SQLite backups become visible atomically only after copying, portability preparation and file closure. Incomplete files are neither listed nor successfully downloaded as empty backups; failures remove staging files and retain existing backups. Storage totals tolerate disappearing temporary files, preventing data and diagnostics refresh failures.
- New tasks reread saved configuration, working hours and scheduling parameters under the claim lock, preventing stale settings after a completed save. Active runs retain their sealed settings.
- Pause requests persist for unfinished runs. Quickly resuming a project no longer withdraws the old worker's stop signal; it finishes as interrupted, while new runs can proceed after resumption.
- Editing or removing model connections retains old credentials referenced by unfinished runs, preventing missing-key errors on later calls. Periodic maintenance safely removes retired credentials after their runs finish and retains failed deletions for retry. Fresh credentials receive durable cleanup intent before storage, exceptions after committed configuration do not delete live credentials, and update recovery suspends cleanup.

## 0.5.111

<!-- adaptive-rsi-release:0.5.111;base:81e24d5f40f864de545b06d2461f1d8f5634b933 -->

- Historical run cleanup verifies expected and resolved paths and rejects symlinked ancestors or out-of-bounds paths, preventing deletion in other directories while retaining the existing cleanup scope and run evidence.
- Derived-setting cleanup batches only records with remaining settings, allowing backlogs beyond 200 historical runs to drain.
- Issue task identities include repository and number, preventing same-number Issues from overwriting each other or inheriting delivery state after repository changes. Legacy identities migrate only with verifiable source evidence, retaining history.
- Relaxing Issue label policy restores previously excluded task states without waiting for remote content changes. Existing planning decisions and backoff remain intact, without repeatedly requeuing unchanged obsolete work.
- Task details synchronize state and actions even before any run exists, preserving unsaved requirements and applicable dependency and urgency drafts. Shrinking last pages trigger a bounded valid-page fetch, avoiding empty pages and inverted ranges while discarding results after navigation changes.

## 0.5.110

<!-- adaptive-rsi-release:0.5.110;base:9433d4de98aa3ac6a4b5139b4a26f0d64da07d80 -->

- Edits made while creating a model connection are subsequently saved to the same connection. Reopened editors do not inherit IDs from old responses, and saves retain their originating project scope.
- Manually re-adding a model missing from the catalog makes it selectable again, preserves existing preferences, and keeps it available after later catalog refreshes.
- Auto-merge rechecks the current repository, branch and sealed target after fetching the baseline, preventing merges to an old target after authorization changes.
- Workflow authorization acknowledgments update the saved baseline while preserving newer disable choices and authorization field edits made while waiting.
- Saving login credentials immediately invalidates account-status caches. Cancelled or replaced sessions ignore late poll results and errors while keeping the new session visible and polled. Temporary post-login status/catalog failures retry their unfinished stage without repeating authorization or overwriting feedback for a newly selected provider.

## 0.5.109

<!-- adaptive-rsi-release:0.5.109;base:87cbab342c238c6b8e58808ae6dd2b57ec7b74a3 -->

- Inspection requests preserve cancellation until the active run stops. Cancellation no longer overwrites concurrent completion, preventing delivered tasks from being executed again.
- Preserve prerequisite links during automatic retries and recheck current state when releasing dead dependencies, so temporary failures do not discard execution ordering.
- Successful cross-project submissions clear their submitted goal draft while retaining later edits. Expired candidates remain visible in attention, and delayed notification navigation cannot replace a newer project or detail selection.
- Show update checks in progress instead of stale errors, and prevent installation clicks during checks. Transient check failures retain confirmed installation requests bound to their original version.
- Persist previous installation pointers before committing an update, fixing crash recovery that could report rollback while leaving the failed candidate selected for the next launch.

## 0.5.108

<!-- adaptive-rsi-release:0.5.108;base:2cc8dcbd17190de8d9b2d55c35deff4d507ec316 -->

- Reject retries and requirement supplements until a cancelled active run stops, preventing old workers from resuming or overwriting new queued work; show an actionable wait message.
- Atomically invalidate undelivered candidates and queued approvals when requirements change, and recheck candidate validity before publishing. Existing PRs and merge records remain available.
- Periodic inspections preserve quota-backoff deadlines instead of requeuing model calls early.
- Prevent background reconciliation from overwriting manual workflow retry state and causing duplicate rerun requests. Recover existing verified PRs before treating an advanced baseline as a stale unpublished candidate.
- Settings and model-route acknowledgments use the server-confirmed baseline while retaining edits made during saving and project round trips. Block overlapping route saves within the same scope.
- Preserve prerequisites absent from the current option page when changing urgency. Delayed retry/supplement responses no longer reopen dismissed task details or jump across projects.
- Formal releases run the complete Python suite in parallel groups while retaining platform coverage, failure gates and final installation/upgrade verification.

## 0.5.107

<!-- adaptive-rsi-release:0.5.107;base:bc434853392eb827d64942e7b787804d495cc0cd -->

- Native macOS Codex calls resolve executable symlinks before launch, allowing a CLI installed under `~/.codex` to start its sandbox helpers; Docker continues to use the container's `codex`.
- Usage details reuse the list's friendly background-check title. When calls have unknown usage and no tokens are recorded, show “Unknown usage” instead of the misleading “0 + partly unknown”.

## 0.5.106

<!-- adaptive-rsi-release:0.5.106;base:e1ae12f1b643fb0ad6573ee144712f1c64df0f1d -->

- Tasks judged obsolete by planning are no longer requeued by inspections (avoiding repeated paid planning every scan cycle); manual retry and requirement supplements remain available for obsolete tasks.
- Only authorized publishing tasks are requeued automatically on startup; ordinary interrupted tasks retain their evidence and await manual retry. Budget-backoff tasks return to the queue as soon as their wait expires instead of waiting for the next working-hours window.
- Local commands and model adapters carry separate structured failure codes, which take precedence in retry, provider circuit-breaker and fallback decisions. Timeout, connection or quota words in local setup/check output no longer imply provider failures. Older CLIs still support their own stderr diagnostics while excluding project tool output in stdout.
- Native model responses record known usage before content parsing can fail. Codex/OpenCode cancellation preserves usage and redacted events already produced, then propagates cancellation without triggering fallback.
- OpenCode parsing only accepts the final message's structured output, so valid JSON in an intermediate step is no longer adopted; only duplicate steps with ids are deduplicated, while all usage from steps without ids is retained to avoid merging distinct steps with equal consumption; string status codes from gateways now trigger the circuit breaker.
- Update and rollback startup health checks share a 60-second window. A restored service remains guarded until its version and process pass health checks; a compatible acknowledgment then releases the guard and records rollback completion. Interrupted supervisors retain phase evidence and verify the existing rollback process without restoring the database again.
- Update downloads have a 15-minute total budget; the launcher serializes double-starts with a file lock; delivery baseline fetches get a 180-second timeout.
- Auth containers inherit environment variable values with only variable names in argv, keeping authentication passwords out of command arguments; shutdown flows wait at most 10 seconds for stuck processes; check, auth and CLI worker containers encode complete CSV mount fields to preserve commas, quotes and newlines in paths.
- Native API implementation/review timeouts are relaxed to 900/600 seconds; model probe failures are no longer masked by catalog update errors; workspace comparison reports a diagnosable error for escaping symlinks; the runtime releases data-layer connections on close.
- The model center includes an OpenCode Go API Key preset and official Console link. Protocol endpoints are verified, and official Go endpoints discover models through the independent public directory without sending inference keys. Existing OpenCode configuration, API keys and other login methods remain distinct, and model usage is not presented as remaining subscription allowance.
- Checks and acceptance results use readable cards with expandable original commands and output. Candidate approval summarizes repository, branch, baseline, gates and current authorization on desktop and mobile.
- Settings use a structured check editor, collapse internal policy controls by default, and no longer require arbitrary JSON editing. Unsaved, saving and saved states explain when schedules change, new runs capture configuration and delivery authorization is rechecked.
- The workspace overview includes blocked work, recent failures and model verification evidence. Missing connection or task evidence remains explicitly unknown; it does not claim real model testing.
- A failed desktop update check no longer displays "up to date"; check errors are now distinct from "no update".
- The English anti-flash bootstrap now has a 3-second safety net, so a broken translation script can no longer leave the page blank forever.
- Sandbox snapshots exclude `.rsi-tmp`, so setup leftovers no longer pollute candidate patches; publishing retries only reuse an existing branch at the candidate HEAD and refuse to reset branches pointing to other commits.
- Authentication token comparison treats non-ASCII input as a mismatch instead of raising and resetting the connection; CSP now includes `form-action 'self'`.
- Manual retry resets the infrastructure backoff counter; recurrence requeue of merged tasks also resets attempt counters.
- Native APIs tolerate null usage fields, and empty Gemini candidates return a diagnosable error instead of crashing.
- OpenCode CLI timeouts, rate limits and connection failures can now trigger the configured fallback model.
- Dark mode covers the model center and dialog accent variables; desktop exit force-kills a stuck engine after 10 seconds; the update download no longer has a double-click race; the unused Tauri global API is disabled.
- Access sessions and restart journals are atomically written through randomly named private temporary files. Failed rotation preserves existing credentials; the desktop proxy updates its Cookie and CSRF together so requests continue after rotation.
- Backup downloads stream with size, concurrency and duration limits. Failed authentication is rate limited with bounded credential-free auditing. API failures return fixed messages and allowlisted codes while preserving actionable configuration diagnostics.
- The Caddy example includes HSTS. The worker image defaults to a non-root user and pins a verified official Docker Node multi-platform OCI index digest, with a build-argument override. Managed containers still use the host UID/GID.
- The release smoke retries transient GitHub API network errors.

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
