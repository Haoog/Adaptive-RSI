# Local validation / 2026-10-20

[简体中文](VALIDATION.md) | **English**

Version: 0.2.0. Host: macOS / Python 3.9.6 and 3.12.13 / Node 20.20.2 / Git 2.54.0.


## Simplified workspace acceptance (latest result)

- Primary navigation is Workspace, Tasks and Settings. Model settings and learning history are secondary pages. The empty workspace fits a 390 × 844 viewport; everyday settings expose six configuration values plus two interval-unit selectors, retaining all advanced settings.
- The complete backend suite passed **117 tests**, and **14 frontend tests** passed. New regressions cover task grouping, hour/day conversion, custom delivery preservation, cancellation after out-of-order details, closed details staying closed, and independent draft baselines/units across projects. Syntax checks, wheel and plugin ZIP builds passed.
- Two separate paused fixture projects and a local model directory verified real form saves, project-specific delivery authorization, hidden setting retention and drafts across project/shared-default/language/model-page changes. Saving a planning model leaves the other four roles and other project unchanged. Projects were not enabled and no model inference was started.
- Browser checks include goal submission/cancellation, attention/history groups, learning history, model directory/favorites and revealing invalid collapsed fields. Primary pages in Chinese and English have no horizontal overflow at 1440, 390 and 320 pixels; screenshots are in `output/playwright/simple-*.png`.
- Independent review found and prompted a fix for cancellation targeting the wrong task after out-of-order detail responses. The final six-area review passed; see [review evidence](docs/validation/simple-workspace-review-20261020.json). Backend execution and project authorization rules are unchanged.

## Hot settings fix (previous result)

- The full backend suite passed **117 tests** on Python 3.12.13; **8 frontend regression tests** passed. JavaScript syntax, Python compilation, wheel and plugin ZIP builds passed.
- Ordinary session restoration no longer enters upgrade exclusion. A real upgrade unlocks from the durable installation result instead of a one-shot ready file; real subprocess upgrade and failed-start rollback both verify that settings can be saved afterward.
- Two separate paused projects were imported and inspection intervals, budgets and weights saved within the same server process. A new provider with session credentials immediately fetched a local fixture directory without restart.
- Browser and frontend regressions cover original-payload retry with fresh CSRF after explicit upgrade rejection, retained pages across version changes, newer drafts within/across projects, acknowledged settings baselines, saving a restored original value, edits during acknowledgement refresh, and no replay after an ambiguous write failure.
- Chinese/English switching and the English layout at 390 pixels passed without horizontal overflow; screenshots are in `output/playwright/hot-*.png`. Simulated upgrade rejection and connection failure produce expected network errors, rather than page-script exceptions.
- See [review and execution evidence](docs/validation/hot-settings-review-20261020.json). Browser verification used local Git and HTTP fixtures; this fix created no GitHub repositories and did not run real-project model tasks.

## Autonomous delivery acceptance (previous result)

- **112 tests passed** in the complete suite, including pause-control, pinned-target, stale-candidate and reverse-DNS-independent startup regressions; JavaScript syntax, Python compilation, plugin ZIP and wheel builds passed.
- Startup review also passed: the loopback listener skips unnecessary reverse DNS, and isolation tests use the current interpreter and verify that it actually runs.
- The final targeted independent review passed with no remaining blockers; [review evidence](docs/validation/code-review-20261020.json). Additional fixes seal delivery targets, reject external merges into other branches, normalize historical scheduling credits, persist long failure reports and pin post-merge monitoring to its original branch.
- Independent review found and prompted fixes for writable native Git metadata, lost merge metadata after feedback errors, local/remote deferred-baseline mismatch and PR retargeting. Regression checks cover real sandbox isolation, host Git filters, retryable feedback failures, remote baselines and changed PR targets.
- Frozen original regression, full candidate tests and sealed pre-implementation acceptance run together; newly failing tests cannot disappear under harness freezing. Acceptance design runs in a separate snapshot whose artifacts never enter the implementer workspace.
- PR tracking, post-merge checks and deferred reassessment share project locks and global fair scheduling through queued jobs, keeping expensive checks out of the scheduler thread.
- **Two real-model / GitHub CI / automatic PR / automatic merge / post-merge verification rounds completed.** Round two starts from round one's merge SHA. The independent private fixture repository is paused afterward; no projects were enabled in the user's service. See [live validation](LIVE_VALIDATION.en.md) and [sanitized evidence](docs/validation/autonomous-loop-20261020.json).
- Playwright verified saved project delivery modes, merge authorization and weights; English at 390 pixels has no horizontal overflow. Screenshot: `output/playwright/delivery-controls-mobile-en.png`. Earlier visual checks remain below.

The following sections preserve earlier-stage observations. Their old delivery boundaries and statements that no real models were called describe those earlier stages; this section and the live-validation report describe the current result.


## Executed checks

- `python3 -m unittest discover -v`: **83 tests passed**. Raw output is in `output/test-results.txt` in the development checkout.
- `node --check adaptive_rsi/web/app.js`, `node --check adaptive_rsi/web/i18n.js`, `node --check adaptive_rsi/web/model-center.js`, and Python byte-compilation passed.
- Both bundled skills passed the system skill-creator validator. The validator's PyYAML dependency was supplied in an isolated uv environment; the runtime has no third-party Python dependencies.
- The runtime wheel and local plugin ZIP were built. The plugin archive excludes state, sessions, auth data, build caches and browser logs.
- Playwright checked a real local server at 1440 x 1100 and 390 x 844. Project registration, paused-by-default state, schedule edits and goal enqueueing persisted. The browser reported zero console errors/warnings on the final page. Screenshots are in `output/playwright/`.

- Bilingual checks confirmed that switching language preserves unsaved settings, saving persists changes, reloading remembers English, and the English task view retains Chinese goal text. At 390px width there was no horizontal overflow; the final page reported zero console errors or warnings.

The new provider and review controls were checked in the in-app browser: provider registration, role routing, provider-default effort and review thresholds saved correctly. Switching languages preserved unsaved provider/model values; saved values survived a reload. At 1440 x 1100 and 390 x 844 there was no horizontal overflow or console error/warning. Temporary provider settings were removed and original model/threshold defaults restored. No real project was registered or enabled. Screenshot: `output/playwright/models-review-settings.png`.

## Configuration, diagnostics and telemetry / 2026-10-20

- Eight new behavioral tests cover atomic consecutive-outage accounting, restart persistence and project isolation, no network/budget reservation while paused, explicit resume and fallback, no fallback after workspace edits, effort-specific probes, frozen prices and refresh preferences, retained planning evidence and quality statistics. The full 83-test suite passes; affected model-center and operability tests are repeated after final adjustments.
- Tests blocked by budgets, missing credentials or paused connections retain earlier tool proofs. Actual tool-format failures invalidate only the matching effort. Pricing rejects negatives, booleans, NaN, Infinity and strings.
- Playwright exercises favorites/filters/paging, provider filtering, visual fallbacks, discard/save/reload, bilingual draft preservation, pricing, tool tests, repeated rate limits and pause/resume, project usage/estimates, and task evidence through a real isolated HTTP fixture. Chinese and English overview/task/model pages have no horizontal overflow at 320px; desktop is 1440 x 1100. Expected HTTP 400 responses occur during rate-limit/pause tests, with no JavaScript exceptions. Screenshots: `output/playwright/operability-*.png`.
- English overview grid minimums are corrected for narrow screens. Cost estimates use user-supplied blended rates and cannot establish actual billing with missing rates or usage. Pass-rate denominators are evaluated runs; review-block denominators are reviewed runs. System inspections are excluded from code-task quality.
- New bilingual live-validation procedures are in `LIVE_VALIDATION.md` / `LIVE_VALIDATION.en.md`. No real paid model, GitHub push or PR is performed. The actual local service retains its original session; all test projects and settings use isolated state.

## Model center redesign validation

- Sixteen new tests cover secure credentials, persistent directories, connection invalidation, project isolation and per-role inheritance, explicit model tests and budgets, update exclusion, and authenticated APIs.
- A disposable macOS Keychain entry passes real write/read/delete operations. Session credentials reach local directory and model fixtures. Configuration, database events and API snapshots contain no raw keys; custom Codex credentials use process environments only and are excluded from command-tool environments and redacted from logs.
- Refresh preserves disabled, custom and missing models. Key rotation preserves preferences but clears capability evidence. Projects store only changed roles; other roles continue inheriting global defaults. Scope transitions clear old roles and disable saving until the new scope loads.
- Explicit tests require user confirmation and count against call budgets. Native routes validate structured tool results; the Codex replacement must read randomized content from an isolated directory rather than return fixed JSON. No real paid models are called.
- Playwright covers connection creation/editing, session/Keychain storage, directory refresh/error recovery, search/selection, role save/reload, project overrides/restored inheritance, and bilingual unsaved-value preservation. Chinese and English have no horizontal overflow at 1440 x 1100, 390 x 844 and 320 x 844. No JavaScript exceptions occur; simulated authentication failure produces an expected HTTP 400. Screenshots: `output/playwright/model-center-*.png`.
- The actual user service only queries the real Codex directory. Provider round trips and explicit tests use isolated local fixtures, not proof of real vendor account permissions or long-running agent capability. Temporary configuration and test keys are cleaned up.

## Model directories and selection (earlier validation)

- Eight new tests cover directory requests across six provider protocols, auth headers, pagination, deduplication and IDs, empty directories, the 1,000-entry cap, malformed responses, repeated cursors, auth failure, redirect refusal and redacted errors; built-in Ollama / LM Studio endpoints and no connection when a required key is missing.
- A replacement Codex process verifies initialize / initialized / model/list sequencing, pagination, no inference and timeout cleanup. The real installed Codex separately returned 14 directory entries without starting inference.
- Real local HTTP API checks cover authentication, Origin and CSRF. Unsaved providers can be queried without storing configuration, reserving model calls or holding the task dispatch lock.
- Playwright uses a local directory server to check preserving the current model on fetch, explicit selection, save/reload, bilingual value preservation and manual entry. Delayed response interception checks provider-switch isolation; empty lists, auth failures, retries and invalidation after a protocol edit pass. The simulated auth failure produces an expected HTTP 400 console record, with no JavaScript exception.
- Chinese and English layouts have no horizontal overflow at 1440 x 1100 / 390 x 844. Screenshots: `output/playwright/model-catalog-*.png`. Temporary settings are restored and no project is enabled. Replacement directories do not establish real vendor execution permission or model tool capability.

## New validation in 0.2.0

- Execute/defer/reject value decisions; no implementation after defer/reject, host overrides for low confidence, blank evidence or low-impact/high-effort suggestions. Reassess deferred tasks only after baseline/evidence/policy changes; closed deferred Issues become obsolete.
- Five native protocols complete real local HTTP read/replace/finish tool round trips, checking authentication headers, conversation history, Gemini thoughtSignature, usage, read-only roles, path safety and failure behavior. These fixtures need no Codex and invoke no real vendor models.
- Two GitHub URLs register as separate paused projects through a replacement clone connector. Cursors, tasks, configuration and memory stay project-scoped. Real Seatbelt enforcement blocks model/native-command reads of another repository; command tools do not inherit secrets. Registration is blocked during active runs to keep isolation profiles consistent.
- Automatic PR/merge configuration is rejected and old settings migrated. Publishing without explicit approval for that candidate is refused.
- Updates validate stable versions, official assets, SHA-256, paths and state contracts; automatic mode installs only same-major/minor patches, waits while busy and never restarts on invalid bundles.
- Real background services in temporary state successfully upgrade while preserving data. A failed candidate deletes a database record before exiting; rollback restores old code and database. Bootstrap launchers respect the explicit state directory and active managed version.
- Playwright exercises the real 0.2.0 service: Anthropic preset/native protocol, role route, value policy and update interval save and survive reload. Language switches preserve unsaved changes. At 1440 x 1100 / 390 x 844 there is no horizontal overflow; the multiple-URL dialog renders correctly. No remote project is actually imported. Temporary settings are cleared and no project is enabled. New screenshots: `output/playwright/native-providers-*.png`, `updates-mobile.png`, `multiple-projects-mobile.png`.

## Behaviors exercised

- Real Git snapshots, a failing baseline and a passing candidate, with the original dirty working directory preserved.
- Exact-commit shallow fetches exclude future fix commits and their Git objects.
- Host-sealed patches, independent baseline/candidate checks, frozen original tests and build entrypoints.
- Replacing tests with a fake success cannot pass the host's original regression test.
- macOS checks cannot read controller state, write frozen tests, or access the network.
- Read-only model processes cannot modify sealed candidate code.
- Selected untracked dependencies are snapshotted; evaluation restores the same dependency bytes even after the executor changes its own copy.
- Atomic project locking and task claiming, frozen configuration precedence, reserved call budgets, restart recovery, bounded retries and missed-schedule coalescing.
- GitHub incremental arguments, duplicate handling and closed-Issue handling through a stubbed connector.
- Model/effort CLI arguments and usage accounting through a deterministic model substitute.
- Responses-provider and Ollama / LM Studio arguments, explicit cross-provider fallback, and omission of explicit effort with provider defaults; no key values in arguments. No live third-party or local model calls.
- Existing call history migrates to include providers without losing usage. Sandbox setup or structured-output failures are not counted as completed sessions.
- Green tests cannot bypass a medium maintainability finding even if the reviewer reports an overall pass. Six-area coverage, insufficient evidence and low-severity thresholds are enforced by the host.
- Both paired variants receive code review with costs recorded; a candidate that passes checks but fails review cannot be promoted. Provider endpoints, models and review rules affect strategy compatibility.
- Candidate proposal, paired replay, promotion, verified-skill retrieval, model compatibility invalidation and rollback. This test lowers the evaluation split size to one case per split and uses a deterministic provider. It verifies the machinery; it is not evidence of improved model intelligence. Product defaults require at least three cases per split.

## Remaining live validation

No paid model calls or autonomous GitHub branch pushes, PR creation, or merges were performed. No public upgrade package was published/downloaded; user updates still require a maintainer to publish contract-compliant release assets. Publishing this source repository does not validate autonomous delivery. Codex authentication was checked without exposing credentials. Model availability is verified on the first real invocation; routes have no silent fallback. A real project is still needed for provider, toolchain, dependency, GitHub and long-running integration checks.

Docker was installed but its daemon was stopped, so Docker execution and cleanup were not exercised. The working local backend was macOS Seatbelt. Linux Codex workers remain blocked; native Linux/Docker execution has not been validated on a real host; Windows hosting is not supported by this POSIX runtime. Language adapters were checked against manifest fixtures; those tests do not certify builds for every language/toolchain.

Long-term recursive improvement, transfer across projects, protection against arbitrary adversarial code, and strict monetary budgeting have not been established. Codex limits count CLI sessions; native limits count HTTP requests. Neither is a strict monetary budget. Recovery from power loss or supervisor crashes is unverified. Promotion requires a separately prepared evaluation corpus; absent evidence leaves the stable strategy active.
