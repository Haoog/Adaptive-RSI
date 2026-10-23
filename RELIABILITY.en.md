# Reliable improvement loop

[简体中文](RELIABILITY.md) | **English**

## Everyday use

The dashboard retains three primary destinations: Workspace, Tasks, and Settings. Imported projects remain paused. Workspace readiness lists missing requirements and links to relevant settings. Configuration checks do not invoke models; a ready state covers basic requirements, while actual tasks still require independent checks.

Task details lead with outcomes, stages, and recovery suggestions. Value decisions, checks, reviews, and original records expand on demand. Tasks are filtered and paginated per project on the server. Switching projects resets pagination. Details retrieve run history separately, and Workspace includes the currently active run.

The dashboard polls lightweight work state every five seconds. Strategy candidates load when Improvement records opens; quality, usage, and model statistics refresh only when needed by the page. Clients that omit section parameters still receive the complete legacy state response. A malformed project configuration is isolated, reported, and represented as paused in the response without changing its stored pause state; that project cannot be enabled until its configuration is fixed.

## Execution and recovery

The flow is readiness → verification, deduplication and value assessment → completion criteria → isolated implementation → execution and before/after checks → independent review → project-authorized delivery → post-merge checks. The Workspace shows the current stage, elapsed time and latest progress; queued work explains that it starts by value after the active task.

- Before paid calls, check configured commands, tools, isolation, dependency directories, credential settings. Freeze dependencies and run configured preparation commands before planning. Preflight cannot prove remote availability, account login, or support for every language environment; execution still records actual failures.
- Persist planning, acceptance design, implementation summaries, checks, and review. Retries can reuse valid plans and acceptance designs. A patch that passed execution checks before a review-service outage can be restored, but all checks and review must run again.
- Reuse depends on controller build, base commit, original task evidence, strategies, dependency snapshot, delivery feedback, and only the configuration used by that stage. Scan timing, another role's model, or unrelated UI settings do not invalidate planning and acceptance; dependency or implementation-route changes still invalidate their affected stage. Patches rejected by checks or review cannot be replayed directly into delivery.
- Authentication, provider, environment, baseline and code-check failures have separate recovery suggestions. Legacy budget-waiting tasks can requeue without a usage gate.

## Discovery and value ordering

Initial Issue import uses a durable cursor per project. Subsequent cycles send an update-time `since` filter to GitHub and page only new or revised entries. Each cycle fixes its starting watermark and creation ordering; the next cycle overlaps its start to capture older Issues newly matching the filter. Revisions held by active runs are persisted separately across restarts and watermark advancement. Each turn remains bounded; initial imports and large update batches may need multiple turns.

Skip PR entries; handle closing, reopening, label changes, and revisions. Active runs keep their original evidence. Delivery and auto-merge recheck Issue state, revision, and labels. If an Issue changes while a candidate awaits delivery or PR checks, the old candidate is stopped until synchronization refreshes its evidence and value is reassessed; stale requirements are not retried directly. Changing repositories, label policy, or page size restarts synchronization.

When planning an Issue task, the runtime loads comments on demand and adds at most the 20 most recently updated comments as supplemental context. It reads comments incrementally by update time and advances the cursor only after all pages are read; results are briefly cached locally. Permission, authentication, or paging failures explicitly mark comment context as missing, do not masquerade as an empty comment list, and do not advance the cursor. Omitted older comments and incomplete history are identified. Issue bodies and comments are untrusted task data; they grant no permissions and cannot override system constraints.

Inspection findings require real file paths and literal existing quotes. Matching problem anchors deduplicate; matching titles with different evidence are not automatically collapsed. Planning receives tasks from other sources and open PRs to assess semantic overlap. Model-based semantic deduplication cannot guarantee detection of every paraphrase.

Value assessment records impact, benefit, cost, confidence, and inaction. Ordering follows `40 + impact weight - effort - uncertainty - risk`, bounded to 1–99. This is an ordering policy, not a success probability. Unassessed tasks retain source priorities. Assessed tasks yield to higher-priority work and reuse their saved plans. Waiting adds up to 20 points.

## Evidence that work helped

Ordinary behavioral changes require independent acceptance to fail before the change and pass afterward, alongside regression, full-suite checks, and review. Missing dependencies and import failures do not establish reproduction. Performance tasks require reproducible threshold checks; a single faster pipeline duration does not prove a performance improvement.

High-risk plans require negative-path and permission-boundary acceptance and an additional focused code review in a separate session. Any failed review blocks delivery. Risk classification and check adequacy still involve model judgment.

Explicitly titled refactors use behavior-preservation checks, with maintainability benefits assessed in review. This does not automatically prove reduced complexity. Reverts and post-merge monitoring check preserved behavior rather than requiring another new improvement. Candidates without sufficient evidence remain undelivered.

## Strategy experiments

1. Check independent cases before proposing. Defaults are three tasks per split and two repetitions. Calls, tokens and cost have no quota; actual usage remains recorded.
2. Stable and candidate variants share the production execution function: planning, independent acceptance, implementation, checks, and review. Experiments cannot create PRs. Stop before exposing holdouts if validation lacks improvement or regresses.
3. Stable and candidate executions alternate their first position by actual trial index and record that order as evidence to reduce fixed-order bias. Both splits require improvements, no observed regressions, and sufficient independent quality evidence. Repeated executions are not independent samples. Group by task family and count families winning consistently across repetitions. A one-sided sign test uses `p = 0.5^winning families`, requires `p ≤ 0.05`, and at least two repetitions.
4. This conservative finite-sample gate depends on family independence, check quality, and measurement conditions. It does not establish generalization. Synthetic tests verify controller behavior, not improved real-model capability.
5. Operational monitoring uses only strategy IDs recorded in each actual run. Infrastructure and insufficient-evidence failures do not count; each run counts once. Regressions that cannot be attributed to a unique used strategy retain diagnostics without rolling every strategy back.

Evolution improves working strategies, prompts, and project guidance. It does not train foundation-model weights.

## Models, context, and cost

Retain Codex, OpenCode, Responses, Chat Completions, Anthropic, Gemini, Ollama, and compatible gateways. Catalog visibility, basic tool checks, and successful real project tasks are separate evidence levels. Untested providers have no compatibility guarantee. Fallback follows existing availability and workspace-integrity restrictions; every implementation protocol refuses fallback after workspace edits to avoid mixing partial work. Built-in Ollama and LM Studio use native APIs without Codex; readiness, basic tool probes and billing units match execution.

Context adds task-relevant code paths and declaration anchors with bounded file counts and declaration lengths. Recent history includes concise failure causes. Verified memories are retrieved by relevance under a character budget while preserving original evidence. Expired, rolled-back, or configuration-incompatible guidance is excluded.

Each call freezes its counting unit: Codex/OpenCode session or native HTTP request. Historical records with no unit remain unknown. Tool round trips record actual usage. User-supplied blended prices are estimates, not bills.

## Authorization, delivery and monitoring

Before each push and PR creation, recheck pause, cancellation, shutdown, automatic PR authorization and delivery target. Before auto-merge, recheck that the checks/review, baseline, and Issue still match; cancelled, timed-out, or stopped delivery tracking does not continue to merge or close PRs. Requests already issued cannot be recalled; pausing blocks subsequent unissued operations. Approving a failed delivery requeues its job and records fresh per-candidate authorization instead of retaining a revoked automatic policy. Active deliveries are not duplicated.

Review receives the host-sealed full patch that matches the evaluated candidate, as untrusted evidence. Common test filenames across languages, test directories, manifests, and configured check entrypoints are restored from the baseline into candidate copies so candidates cannot weaken existing regression gates; candidate-added tests can still run in the full candidate check.

Failed GitHub checks appear directly under Needs your attention; successful reruns return to In progress while PR tracking continues. Post-merge checks respect the configured interval and record poll times even when the head is unchanged. Completed observation windows stop scheduling; pending regression investigations still retry.

Codex tools use a single default-deny filesystem permission profile, allowing the snapshot and system toolchains. Other user files, controller data, evaluation cases and other projects are unreadable. Implementation writes only its snapshot; planning and review are read-only, Git metadata is not writable, and tool networking is disabled. See [Codex permissions](https://developers.openai.com/codex/permissions). Unlisted toolchains must fail explicitly rather than granting the whole home directory.

## Engineering and updates

Candidate execution, Issue synchronization, dashboard queries, readiness, recovery, context, and evidence rules have separate modules. Keep the Python standard library and native frontend without adding a runtime framework. SQLite changes are additive tables and columns; the existing supervised updater retains startup-failure recovery.

Backend and frontend share a content build identity. Each server holds its startup assets; changed backend builds prompt users to save input before reloading. Forms are not cleared automatically. Packaging checks all four version declarations. CI covers backend, frontend, real Chromium form flows, ZIP, and wheel builds.

After each mainline merge, GitHub Actions automatically prepares a version PR. A trusted mainline workflow reconstructs and checks the exact version patch, runs the full tests, browser checks, and ZIP/wheel installation and upgrade checks, then writes the required `verify` check to that exact PR commit. The version PR merges only after normal branch-protection checks, reviews, and conflict checks pass; automation does not bypass protection. The bot then explicitly runs formal publication, verifies the merged commit again, and publishes the immutable assets produced by that verification. Updaters ignore drafts, and local builds do not push or publish. See [Releasing](RELEASING.en.md).
