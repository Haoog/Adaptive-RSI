# 2026-10 project hardening

[简体中文](audit-hardening.md) | **English**

This batch covers process execution, isolation, storage, scheduling, providers, delivery and UI audit findings. The [100-item tracker](validation/audit-100-implementation-20261027.json) separates implementation, validation and remaining limitations. Temporary projects and substitute protocol servers do not establish real model capability improvements.

## Configuration and compatibility

- `schedule.working_hours` accepts overnight windows such as `[22, 6]`; `[0, 24]` means all day. Equal endpoints are invalid.
- `recovery.provider_retry_minutes` defaults to `0`, preserving manual recovery. Values `1..1440` permit one half-open attempt after a pause. Provider `Retry-After` still constrains retries. This is not a call quota.
- `delivery.deploy.health_attempts` defaults to `1`, range `1..10`; `health_interval_seconds` defaults to `10`, range `1..300`; `health_warmup_seconds` defaults to `0`, range `0..900`. Tracking waits during warmup and retry intervals, declaring failure or starting an authorized rollback only after the threshold. Manual retries reset the counters. Old snapshots receive compatible defaults for missing fields.
- `updates.require_provenance` defaults to `false` for clients without `gh`. Enabling it additionally runs `gh attestation verify` before installation, requiring the official repository, allowed release workflows, main-branch source and a non-self-hosted build. Missing verification evidence blocks installation. SHA-256 checks remain. This option does not imply that every historical release has attestations.
- Legacy quota keys `limits.daily_calls/run_calls/run_seconds/max_changed_files` and `evolution.max_calls` are accepted for compatibility and normalized to zero; they do not limit execution. `limits.command_seconds` is a safety timeout of `60..86400` seconds, shared across a native multi-turn conversation. Tokens and estimated costs are recorded only.

The database now uses `PRAGMA user_version=2` with a checksummed migration ledger and still rejects newer versions. Added tables, indexes and duration columns are compatible migrations. Restore checks SQLite integrity, foreign keys and run/task project ownership. Old duration records retain wall-clock fallback; new records prefer monotonic elapsed durations.

Settings saves use revision checks so two windows cannot silently overwrite each other; `/api/state` reads related data from one database snapshot. Invalid global or project configuration can be reset while retaining its raw audit copy, and projects remain paused. Diagnostics include read-only database metrics, relationship checks, the migration ledger and a backup manifest without credentials or task bodies.

## Retention and capacity

Run artifacts follow each project's `limits.retention_days`. Candidates being published, merged runs still being monitored and the complete stable-strategy ancestor chain remain protected. Retired model directories are retained while any run is active. Credential cleanup limits actual deletion attempts per batch; referenced entries do not starve later unreferenced entries.

Run `python3 -m adaptive_rsi --state-dir /absolute/path archive --max-mb 1024` to export a private gzip snapshot of a consistent database. The combined archive directory capacity is bounded: exceeding the limit refuses another archive, preserving existing archives and online history. A cross-process exclusive lock serializes exports; files use mode `0600`. Decompress an archive to SQLite and use the existing `restore` command with a new state directory. Archives contain private project evidence and must not be uploaded publicly.

This capacity applies to the archive directory, not the complete state directory. Raw run, task and call history can still grow; this batch preserves audit and usage records rather than deleting them automatically. Offline archive management and disk monitoring remain necessary. Compressed snapshots do not provide automatic historical storage tiering.

## Execution, delivery and UI

Native HTTP runs in a disposable process so cancellation terminates a genuinely blocked request; the frozen engine has a dedicated internal entry point. Source reads reject links and special files, and search caches have a total limit. Native file tools and commands restrict writes to the host-sealed plan scope while protecting metadata. Docker command tools mount only existing writable scopes; create missing files with file tools first. Docker may reject atomic replacement of a single-file mount.

Ordinary model/check execution shares a process-wide capacity limit. Performance comparisons hold exclusive capacity and run serially, reducing interference from managed work. External machine load can still affect measurements. New regressions may contain up to eight files; isolated pytest retains supported safe options. Go JSON output can supply structured execution summaries.

Authorization, dispatch intent and receipts are durable separately. Slow GitHub requests run outside the global scheduler lock; a short final lock checks authorization and ownership before recording intent. Revoking authorization after intent is recorded cannot guarantee withdrawal of a remote action. Ambiguous network results are reconciled without blind reposting. Merge-protection reads are advisory; GitHub enforces actual rules.

Activity history uses cursor pagination, and historical timestamps include year and timezone. Pages remain usable when browser storage is denied, with an explicit warning when drafts cannot persist. Confirmation dialogs manage focus, and partial model saves show the remaining state. Desktop backups stream through Rust to private temporary files with progress and cancellation. The service worker caches only public icons without query parameters; executable assets and navigation always use the network. The console does not provide offline operation.

## Round 3 deep defect and cross-platform hardening

- **Indexes and batched purging**: Added covering indexes on `events(kind, created)`, `phase_times(run)`, `remote_waits(run)`, and `task_revisions(task)`. Batched cascading deletes for `phase_times`, `remote_waits`, `task_revisions`, and `submissions` with a limit of 500 rows per transaction to prevent long WAL locks. Replaced unindexed subquery in `events` purge with a single indexed seek via `LIMIT 1 OFFSET 1000`.
- **Sandbox isolation and environment resilience**: `scrub_tree` allows benign external symlinks (e.g. package manager caches or system compilers) while strictly blocking symlinks into controller state or credential paths. The sandbox falls back to the system temporary directory when the project workspace is read-only. Test evaluators support Unicode checkmarks and Vitest duration formats; baseline freezing skips dangling symlinks without crashing.
- **Model conversations and context trimming**: Native model conversations automatically trim bloated tool output payloads when approaching context token limits while preserving diagnostics. The Gemini transport preserves `tool_call_id`. Claude 2 / Opus `max_tokens` is bounded to 4096. Concurrent finish and tool calls convert into feedback guidance.
- **Release and desktop multi-architecture**: Release workflow parsing handles inline comments and quoted YAML values. Desktop update downloads detect `x86_64` vs `arm64` dynamically and fallback to system temp if `HOME` is unset. The PR delivery button is protected against double-click reentrancy, and CSP specifies `object-src 'none'`.


## Remaining limitations

- Ordinary process-group cleanup under macOS Seatbelt cannot guarantee termination of descendants that actively detach with `setsid()` and quickly reparent. Docker checks use container cleanup. These changes do not provide complete macOS process-tree supervision.
- Remote baselines are still fetched at execution start, after evaluation and before publication to reject stale candidates. These critical checks are not cached merely to reduce requests.
- A Developer ID signing, hardened-runtime and notarization configuration path is prepared. Actual signing, notarization and Gatekeeper verification require Apple credentials and a real release build. Builds without those credentials remain ad-hoc and must not be labeled notarized. See [Tauri's signing documentation](https://v2.tauri.app/distribute/sign/macos/).
- Randomized state transitions, short resource soaks, bilingual accessibility checks and translation checks cover only their defined boundaries. They do not replace long-term production observation, a comprehensive accessibility audit or real model evaluations.
