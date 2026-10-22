# Local validation

[简体中文](VALIDATION.md) | **English**

## Browser account connections and automatic releases / 2026-10-22

- **26 authentication tests pass**, including read-only real Codex/OpenCode CLI and Docker checks and cleanup after the authentication parent's abrupt exit. Regressions cover cancellation, late writes after shutdown and credential-lock contention. No real account OAuth or paid model call is performed.
- **42 release, deployment and recovery tests pass**, including real successful service upgrades, code and SQLite restoration after failed startup, and first reads of static WAL backups. A read-only installed 0.3.0 supervisor also successfully handed off to a temporary copy of the current implementation marked 0.3.1, retaining its database. This is separate from upgrading official release assets. Controlled protocols exercise GitHub dispatch, health endpoints and rollback states; they do not demonstrate a real user deployment.
- **10 related-work and HTTP-route checks pass**, covering distinct defects at one code location, legacy fingerprints, project isolation, replanning when related results change and plan reuse when they do not.
- **26 frontend request tests pass**, with syntax checks for four JavaScript files. Real Chromium full flows and focused authentication checks cover retained authorization input, refresh recovery, provider switching, model-directory-only refresh, conditional deployment fields and English mobile layouts. Fixture projects remain paused.
- Plugin ZIP and wheel builds, isolated installation/reinstallation, actual supervised ZIP service startup, version checks and retained state pass. No older official Release existed during validation, so cross-release wheel upgrades are not claimed. Formal publication still requires exact-commit verification on GitHub.
- Separate agents implement and independently review the relevant backends. Fixed findings include authentication lock contention, late writes after shutdown, stale deployments overwriting newer ones, false backup-corruption detection and pending deployments appearing in history. See the [authentication](docs/validation/browser-login-review-20261022.json), [release](docs/validation/release-review-20261022.json) and [task-experience](docs/validation/experience-review-20261022.json) reports. These are automated reviews, not independent human reviews.
- Repository Actions may create version PRs; default workflow permissions remain read-only, with required main-branch checks, conversation resolution and administrator enforcement retained. Other projects still require separate PR, merge, release and deployment authorization.

## Server and OpenCode / 0.3.0 / 2026-10-22

- macOS / Python 3.9.6: **173 backend tests pass, with 4 optional tests skipped**. Subsequent targeted checks cover Docker acceptance and workspace isolation, real OpenCode/Codex CLIs, OAuth refresh, hourly scheduling, unrestricted call recording, pagination across 205 Issues and native protocols.
- **25 frontend request tests pass**, with JavaScript syntax checks and real Chromium forms, Chinese/English and mobile flows. The UI exposes only two schedules, includes OpenCode selection and removes call-quota fields.
- OpenCode 1.19.3 performs actual reads/edits and returns structured output with 45 recorded tokens. Codex 0.163.0 returns structured output and 15 tokens inside Linux Docker. Local HTTP/SSE fixtures replace paid model services; this does not demonstrate capability improvement.
- The Linux worker image is built and exercised, and Linux CI is added. Installer shell syntax, supervisor health checks and PID verification are tested. No user server is available, so remote systemd, DNS and TLS deployment remain untested.
- Version 0.3.0 ZIP, SHA-256 and wheel builds pass; the plugin contains bilingual server documentation and deployment files. No official Release is published.
- Author self-review and GitHub Codex automated review are performed. Automated review identified missing CLI container resource controls; memory, CPU, process-count and temporary-filesystem protections are added and checked through actual cgroups and filesystem capacity. No independent human review is performed. See the [review record](docs/validation/server-opencode-review-20261022.json).

## Runtime safety and delivery fixes / 0.2.2 / 2026-10-21

- macOS / Python 3.9.6: all **149 backend tests passed**, including the installed Codex sandbox check. It denies reads from unregistered directories, registered projects, and controller state; disables tool networking; allows implementation writes only in the snapshot and review output only to one result file.
- All **25 frontend request tests passed**, along with syntax checks for all three JavaScript files. Real Chromium verified failed GitHub checks under Needs your attention, Chinese/English switching, and project isolation. Fixture projects remained paused with zero model calls.
- Plugin ZIP, SHA-256 sidecar, and wheel built successfully. Version `0.2.2` matches plugin, runtime, and update manifests.
- GitHub `main` now requires a PR, the `verify` check against the latest main, resolved conversations, and applies the rules to administrators. There is one maintainer, so a second-person approval is not required.
- Automated PR review found a closed-while-active Issue watermark omission. The revision is now retained until the task can safely stop, and its regression test passes separately. The PR check runs the complete suite for the latest branch.
- Author self-review and automated review follow-up completed; no independent human review was available. Limits include in-flight remote requests cannot be recalled, and live provider calls were not tested. See the [self-review record](docs/validation/runtime-safety-review-20261021.json). No formal Release has been published; the updater only accepts published stable releases.

## Reliable improvement loop / 0.2.1 / 2026-10-21

- macOS / Python 3.9.6: the first full run passed **137 backend tests**, along with **25 frontend request tests** and syntax checks for all three JavaScript files.
- Real Chrome exercised two isolated temporary projects: hot settings saves, retained drafts, task pagination/filtering, project switching, Chinese/English and mobile layouts. The five durable stages of an actual fixture task were checked. Both projects finished paused with zero paid model calls.
- Added behavioral coverage for readiness, budget waiting/resumption, recovery of checked patches with fresh evaluation, baseline invalidation, value-based yielding, extra high-risk review, Issue continuation and revisions across watermarks, server filters, project isolation, experiment preflight, withholding holdout after unsuccessful validation, monitoring attribution and context bounds.
- The paired-strategy integration test uses six distinct synthetic defect families and two repetitions, running planning, acceptance, implementation, checks and review through the production function. It verifies controller behavior, not real model capability gains. The full suite still includes real service recovery of previous code and database after a failed startup.
- ZIP and wheel builds passed, with all four version fields aligned and matching documentation/review evidence packaged. Playwright is a development dependency; the runtime still needs no third-party Python packages.
- This round used author self-review, without independent external review. The review fixed Issue revision watermark omissions, budget-wait filter parity and pagination refresh. See the [value assessment and review record](docs/validation/reliability-review-20261021.json). Live provider accounts, paid calls, long-term gains and publication of a formal Release were outside this validation.
- The first cloud run found that simulated executors were incorrectly blocked by the local Codex executable check. Connection availability now belongs to the actual executor, with a regression for environments without Codex. The production Codex executor still requires its binary; credentials and budgets are still checked. Final cloud results are available in the corresponding PR checks.

The following preserves historical 0.2.0 validation. Old experiment settings and test counts apply only to that version. Current behavior is documented in the [reliable improvement loop](RELIABILITY.en.md).

## Historical validation / 0.2.0 / 2026-10-20

Host: macOS / Python 3.9.6 and 3.12.13 / Node 20.20.2 / Git 2.54.0.


## Usability and reliability fixes (latest result)

- Removed repeated workspace project headings and descriptions. Settings defaults to the current project. Shared defaults offers a project delivery shortcut instead of a disabled selector. Authorization rules remain unchanged.
- Chinese wording uses everyday terms with actual model parameters retained; all delivery modes are explained. Missing sessions show bilingual recovery instructions. APIs still require authentication, Origin and CSRF; recovery pages contain no credentials.
- All **118 backend tests passed**, along with **25 frontend regressions**, covering stale state/toolchain responses, duplicate submission, expired sessions, non-JSON responses, retained input/expansion and project-default scope.
- Two paused fixture projects verified partial import retries, real delivery-setting saves/isolation, scope switching, draft-preserving home navigation and inline JSON errors. Chinese/English pages have no horizontal overflow at 1440/390/320 widths or page-script exceptions. Git cloning and model directories used local substitutes; no remote repository or real project/model inference was started.
- Screenshots: `output/playwright/usability-workspace.png`, `usability-workspace-mobile.png`, `usability-inline-error.png`, `session-recovery.png`. Expected unauthenticated 401 and invalid-import 400 responses are not page-script failures.
- Interaction references: [GOV.UK form errors](https://design-system.service.gov.uk/components/error-summary/) and [Open WebUI connections](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/). No extra framework or permission-model change was introduced.
- The independent review's import-selection and cross-scope feedback findings were fixed and verified with delayed browser requests and regressions. All six final assessment areas passed; remaining English fragments and the queued-action translation were also corrected. See [review and resolution evidence](docs/validation/usability-review-20261020.json).
- GitHub PR checks exposed timing noise in the macOS evolution test. Only that test fixture's temporary cost ratio is set to the supported maximum; runtime defaults are unchanged. The targeted test and independent six-area review pass; PR CI will re-run.

## Simplified workspace acceptance (previous result)

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
