# Operability and real evidence / 2026-10-30

[简体中文](operability-evidence-20261030.md) | **English**

This batch starts from 0.5.131. Original inputs, model output and run evidence retain their original language. The [evidence JSON](operability-evidence-20261030.json) contains publishable sanitized results. Raw logs, isolated state and application copies remain outside the repository; credentials and real user databases are not published.

## Behavior changes

- Native-tool text above 16 KiB is stored as a run-owned artifact with a summary, byte count, SHA-256 and opaque ID. `read_artifact` paginates; `search_artifact` performs literal search. Storage is outside the workspace and checks run, invocation and role ownership, rejecting traversal, symlinks, hardlinks, corruption and expiry. Limits of 16 MiB per artifact, 64 MiB total and 128 artifacts fail explicitly without another model judgment call. Expiry does not automatically delete artifacts; run retention handles cleanup, and budgets count the run's artifact directory.
- Model capabilities distinguish documented rules, conservative inference and manual overrides. Documented rules carry sources and local review dates, which are not vendor change dates. Older models without current model-specific evidence retain inference labels. Catalog, role snapshots, diagnostics and actual native requests use the same override values and budget policy. Corrupt overrides fall back, with an error marker in the catalog; large catalogs cannot silently discard saved overrides.
- `dispatch_unknown` / `rerun_unknown` can re-query GitHub candidates read-only. An operator must explicitly select a server-returned run. The backend rechecks repository, workflow, ref, SHA, operation ID, existing run ID and a new attempt, without another dispatch or rerun. Audit retains raw/display candidates, prior state, selection and a source label; that label is not an authenticated person identity.
- CLI execution uses a private 0700 temporary directory inside the isolated HOME and rejects existing links. macOS adds only an exact `/dev/null` write rule for child processes using null standard streams, preserving other write boundaries.
- Test workers emit phase, selector, thread-stack and bounded child diagnostics while preserving reentry rejection, parent monitoring and process-group cleanup. The historical stall is localized after a class printed OK and before the next class began. A truncated faulthandler dump supports the timer-cancellation-window hypothesis, but 300 bounded probes did not reproduce it; the exact cause remains unknown. Diagnostics use one process-wide timer and do not claim to eliminate every hang.

## Real updates

An independent copy of the official 0.5.130 application used an isolated HOME and controller state, with the real application UI installing the official signed 0.5.131 package.

1. Normal update: replacement, restart, a `succeeded` receipt, verified signature and HTTP 200 health passed.
2. Download interruption: after observing a real segment, termination left 37,054 bytes and download identity. Restart produced the complete 15,773,688-byte signed cache, followed by successful in-app installation. HTTP Range transfer bytes were not captured, so byte savings are not claimed.
3. Cache-tampering probe: a one-byte disk-cache mutation did not reach the installation; the receipt retained the official original digest and startup was healthy. No failure transition was observed, so this is not signature-failure recovery acceptance.
4. Installation failure: replacing the isolated installer temporary path with a regular file caused a real `failed / update_install_failed` receipt. The old 0.5.130 application remained, its engine restarted and HTTP health returned 200. After preserving the failure receipt and restoring the temporary path, a second in-app installation succeeded with a 0.5.131 `succeeded` receipt and HTTP 200 health.

Live acceptance does not yet cover startup failure after replacement, power loss, disk corruption or the full fault matrix; those remain partial. Fixed Rust regressions cover controlled signature, receipt, startup-health and recovery behavior, without substituting for live fault exercises.

## Context measurement

For the same 80,000-byte, 8,000-line tool body, compare serialized request sizes after adding its tool result. No model was called. Separate regressions round-trip all pages and literal search.

| Protocol | Inline request bytes | Artifact-reference request bytes |
| --- | ---: | ---: |
| responses | 96,237 | 1,167 |
| chat_completions | 96,214 | 1,144 |
| ollama | 96,233 | 1,163 |
| anthropic | 96,271 | 1,201 |
| gemini | 88,281 | 1,137 |

This is a fixed synthetic request-byte comparison. It excludes subsequent on-demand reads and does not measure provider tokens, billing or production speed.

## Bounded real task

A disposable Python addition project used OpenCode CLI with `opencode-go/deepseek-v4.1-flash / max` for real planning, implementation and review. The first run added regressions that failed on the old code and passed on the candidate, leaving it awaiting approval. The agent then cancelled that candidate and requested fresh verification. The second run also left an awaiting-approval candidate and a reviewable patch, without changing the original checkout.

Each run used three role calls, recording 87,661 and 128,475 tokens, totaling 216,136. One cancellation and one retry were agent-operated rehearsals. Independent human ratings and human rejections are both zero; no human feedback was fabricated. Prices were not configured, so cost remains unknown. This disposable project had no remote delivery authorization, so no PR or merge was created. Earlier failures exposed private-temp and `/dev/null` CLI compatibility issues; their logs remain available. One small task and one retry do not establish general capability, long-term recursive improvement or lower production costs.

## Validation and ledger

Final-source Python discovery ran 1,321 tests with 10 environment skips; all four workers exited normally, with no ResourceWarning. JavaScript 376 tests and Rust 51 tests passed. The complete browser batch passed, followed by both affected scenarios after the last capability-page edit. API, translation completeness, AST, CI-selector, packaging and isolated wheel-install checks passed; bilingual docs and the ledger receive a final checker pass. Findings from the specified independent review model were addressed with fixed regressions; model review does not replace execution.

The 201-entry historical ledger now has 128 implemented, 53 partial and 20 pending entries. Stale residuals for component pricing, controls, holdout and dependency caching were updated. Gaps in the live fault matrix, long-term effectiveness and independent human feedback remain explicit; all pending ledger entries are not added to this batch's implementation scope.
