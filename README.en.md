# Adaptive RSI

[简体中文](README.md) | **English**

Let AI inspect, fix, and improve your software projects continuously inside isolated workspaces.

Adaptive RSI turns project inspections, user goals, and GitHub Issues into maintenance tasks that must pass value assessment. Changes go through planning, isolated implementation, host checks, and code review before a project's own authorization allows a PR, merge, or delivery. Independent acceptance is reserved for high-risk and performance work.

[![Latest release](https://img.shields.io/github/v/release/Haoog/Adaptive-RSI?display_name=tag&label=release)](https://github.com/Haoog/Adaptive-RSI/releases/latest)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[Download desktop](https://github.com/Haoog/Adaptive-RSI/releases/latest) · [Quick start](#quick-start) · [Documentation](#documentation) · [Boundaries](#safety-and-boundaries)

[Mobile installation and workflow guide](EXPERIENCE.en.md) · [Batch hardening and configuration](docs/audit-hardening.en.md)

## Why Adaptive RSI

Autonomous coding tools can treat every suggestion as work and deliver changes with weak evidence. Adaptive RSI adds four constraints around implementation:

- Decide whether a problem is real, duplicated, and worth solving.
- Modify a fixed Git snapshot instead of the user's worktree.
- Evaluate behavior, configuration, and UI changes with isolated new regression tests that fail on the baseline and pass on the candidate, frozen regression checks, and code review; unsupported runners, high-risk, and performance tasks use independent acceptance.
- Create PRs, merge, release, or deploy only after project-level authorization.

Project inspection and Issue sync default to hourly. Both can be customized, paused, or inherited from shared settings. There are no call-count, run-duration, or finding quotas; tokens and available cost estimates are recorded for usage reporting.

## Workflow

```mermaid
flowchart LR
    A[Inspection / user goals / GitHub Issues] --> B[Task queue]
    B --> C{Worth doing?}
    C -->|Defer| Z[Save deferred decision]
    C -->|Reject| Y[Save rejection reason]
    Z -->|Baseline, evidence, or policy changes| B
    Y -->|User explicitly reconsiders| B
    C -->|Execute| D[Plan and assess value]
    D --> E[Isolated implementation]
    E --> F[Host frozen regression and full candidate checks]
    F --> G{High-risk or performance task?}
    G -->|Yes| V[Independent before/after acceptance]
    G -->|No| R[One independent code review]
    V --> R
    R -->|Failure or insufficient evidence| X[Keep evidence and resolve]
    R -->|Pass| H[Project-authorized PR, merge, or delivery]
    H --> I[Post-merge verification / release / deployment]
    I --> B
```

Models, tasks, memory, usage, and delivery authorization remain separate per project. Pausing a project or revoking authorization blocks subsequent operations that have not yet been issued.

The task list supports previewing bulk retries, cancellations and delivery approvals, and grouping compatible queued goals in one project for shared verification. The preview flags known file overlaps and unknown scopes. An unstarted group can be split to restore its original tasks.

Cancellation previews affected downstream tasks and defaults to cancelling only the current task; cascading requires an explicit selection. Cancelling revokes approval eligibility for undelivered candidates. Restore an archived project before retrying tasks or revising requirements. Requirement revisions expose exact before/after snapshots, diffs and paginated history. Task intervention history retains optional reasons, candidate identity and operation effects, and can export loaded records. See [workflow and mobile usage](EXPERIENCE.en.md) for scope and historical-evidence limits.

## Quick Start

### Desktop app (recommended)

1. Download the macOS ARM64 DMG from the [latest official release](https://github.com/Haoog/Adaptive-RSI/releases/latest).
2. Open the app, then add a GitHub repository or local Git project.
3. Connect a model and choose models for planning, implementation, and review.
4. Click **Start** for the project.

Custom model IDs allow up to 256 characters. Display names are optional and default to the full ID. Custom display names allow up to 160 characters and survive catalog refreshes; names supplied by the service continue to follow catalog updates.

Desktop builds configured with update signing can check and download updates in the background, then install manually or when local work is idle. Updates are signature-verified and wait for local work to finish. Without signing configuration, download from the official releases page. See the [desktop guide](DESKTOP.en.md).

Closing the window hides the app in the menu bar and active tasks continue. Choosing **Quit** from the menu bar stops the service started by the app. The current DMG uses a temporary signature and is not Apple-notarized.

### Run from source

Requires Python 3.9+ and Git; GitHub import and delivery also require authenticated `gh`:

```sh
python3 scripts/start.py
python3 scripts/rsi doctor
```

The launcher reuses an existing service. Task details offer paginated dependency selection; unsaved queue settings survive language changes and requirement supplements. Edits made while settings are saving survive project switches; usage searches invalidate old requests so late responses cannot replace newer queries. The dashboard persistently shows save status: saving scan timing reschedules scans, model assignments are saved separately for new tasks, active tasks retain their configuration, and delivery permissions are rechecked before execution. No restart is required. Edit fixed checks as structured fields and read check results and mobile approval summaries with expandable original evidence. Unconfirmed goals can restore their original text and reuse their request IDs. Pending-approval counts in Data and diagnostics exclude approved, delivering and cancelled candidates, and a failed restore leaves no half-created directory that blocks retry. Issue sync counts only newly added tasks or actual body and evidence changes; an unfinished scan says so and continues next cycle, and open task details update with the synced input while preserving unsaved drafts. See [workflow and mobile use](EXPERIENCE.en.md) for recovery controls and evidence boundaries after database restoration, and [server deployment](SERVER.en.md) for Linux and systemd setup.

## Core Capabilities

- **Continuous discovery:** hourly checks for bugs, feature gaps, performance, usability, and maintenance work, with rotating code modules. Sync GitHub Issues incrementally or add a goal at any time.
- **Value gate:** assess current evidence, duplicates, expected benefit, cost, and the impact of doing nothing before implementation.
- **Isolation and verification:** implementation and review use separate contexts. The host runs new behavior tests in isolation on the baseline and candidate, then runs frozen regression and full-candidate checks. Unsupported runners, high-risk, and performance tasks use independent acceptance; model claims do not establish success.
- **Code review:** check correctness, security, maintainability, architecture, tests, and scope. Blocking findings or insufficient evidence stop delivery.
- **Controlled delivery:** keep candidates local by default. Each project can opt into automatic PRs, merging after checks, and release or deployment through pinned workflows.
- **Multiple projects and providers:** clone and manage repositories separately. The project selector shows both name and path so same-name repositories remain distinguishable, with support for official providers, gateways, local models, Codex, and OpenCode.

## Model Support

| Route | Use cases |
| --- | --- |
| Codex CLI | OpenAI/Codex accounts and Responses-compatible gateways |
| OpenCode CLI | Providers configured in OpenCode 1.19.3 / 2.1.3 with API/OAuth login |
| Responses / Chat Completions | OpenAI API, DeepSeek, compatible Qwen, gateways, and LM Studio |
| Anthropic / Gemini | Official APIs and compatible gateways |
| Ollama | Local models with tool-calling support |

Models must support tool calls and structured results. Protocol support does not establish tested compatibility with every vendor, model version, or gateway. See [account and model setup](docs/accounts.en.md) for basic connections and [advanced usage and configuration](docs/advanced.en.md) for reasoning effort, pricing estimates, recovery, and other detailed settings.

## Safety and Boundaries

- The dashboard listens only on `127.0.0.1`. Its login URL contains a local session token and must not be shared.
- New projects start paused, and delivery stays local by default. PR creation, merge, release, and deployment require separate project authorization.
- Catalog requests and ordinary settings saves do not call paid models automatically. Explicit model tests may incur charges.
- Code context is sent to the selected model provider under that provider's data policy.
- Frozen checks, separate sessions, and operating-system sandboxes reduce risk but are not a formal security proof against arbitrary malicious code.

Long-term capability improvement still requires real tasks and sustained experiments. When fresh holdout cases are insufficient, proposal and trials are skipped before model calls and the stable strategy is retained.

Task details collect changes, checks, usage, reused stages and the delivery commit, with optional Helpful, Unnecessary or False positive feedback. Ratings are observational and do not automatically change ranking or strategies; original-merge verification and later-mainline regressions are counted separately. Model pricing supports input, output and cache components with blended-price fallback; usage details provide historical ranges only when enough comparable samples exist. Missing usage and prices remain unknown. Failed usage or detail refreshes identify retained data and the last successful update. See the [reliable improvement loop](RELIABILITY.en.md) for execution and recovery rules.

## Documentation

| Topic | Guide |
| --- | --- |
| Desktop installation, lifecycle, and limits | [Desktop app](DESKTOP.en.md) |
| Codex, OpenCode, APIs, and local models | [Accounts and models](docs/accounts.en.md) |
| Detailed settings, CLI, isolation, and advanced configuration | [Advanced usage and configuration](docs/advanced.en.md) |
| Reliability gates, strategy experiments, and recovery | [Reliable improvement loop](RELIABILITY.en.md) |
| Releases, deployment, health checks, and updates | [Releases and updates](RELEASING.en.md) |
| Linux/systemd and HTTPS deployment | [Server deployment](SERVER.en.md) |
| Automated checks and live evidence | [Validation](VALIDATION.en.md) · [Live validation](LIVE_VALIDATION.en.md) |
| Development, testing, and contribution rules | [Contributing](CONTRIBUTING.en.md) |

## Development

Start local changes with affected modules and browser scenarios; see the [layered validation commands](CONTRIBUTING.en.md#local-development). Run the full checks when a batch is ready:

```sh
python3 -B scripts/test_parallel.py --durations .ci/test-durations.json
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
npm run test:browser
```

Routine tests substitute the model interface and do not spend model sessions. See [CONTRIBUTING.en.md](CONTRIBUTING.en.md) for the complete local and browser test setup.

## License

This project is available under the [MIT License](LICENSE).

Failed or cancelled groups can be split after a preview: originals return to the queue for fresh independent verification while previous group evidence remains. A complete revised group specification becomes a separate task, preserving changes without guessing their attribution. Task details paginate group lineage and outcomes; a whole-group pass is not individual criterion evidence.

During desktop replacement, admission stays closed while the installer process is alive, regardless of elapsed time. If the owned installer exits before stopping the service, admission recovers with an audit record and tasks remain intact. Unknown process status keeps the barrier closed; legacy barriers recover through the owned desktop restart or explicit lease cancellation.

Queue settings support up to 100 predecessor tasks and a combined ancestry budget of 1000 nodes. All predecessors must merge before a task can start. Cycles, missing nodes and cross-project dependencies are rejected. A failed or cancelled predecessor blocks its dependent for manual review while retaining unrelated prerequisites. Multi-selection survives paging, language changes and live detail refreshes.

Experiment cards compare paired quality, token usage (not currency) and latency, with real/synthetic and task-size cohorts. Unknown usage and zero baselines remain incomparable; observed subgroup losses remain visible. Expand a card to inspect or export its recorded plan, input manifest and evidence. Native tool activity records invocation hashes, status and timing without arguments, source text or command output. Interrupted search retains bounded file/line locations. A started step without a terminal receipt needs investigation and is never automatically replayed. Source reads check cancellation between 64 KiB chunks and search checks every 256 lines; an individual blocked operating-system filesystem call cannot be interrupted by these cooperative checks.

Database diagnostics correlate slow query fingerprints with opaque request/task/run IDs, separating SQL execution from connection waiting and excluding SQL text and bound parameters. Pool wait quantiles include capacity timeouts and declare their process/window scope. Normal metrics do not perform full relationship scans; explicit diagnostics inspect ownership and evidence links, disclosing per-check truncation. Explicit orphan-stage quarantine is transactional and bounded, retains every original column, and does not automatically delete other kinds of evidence.

Settings include redacted change history and safe undo. Only the latest change with matching revision and content can be undone; later project or global changes reject stale undo.

Deployment supports consecutive-success gates and multiple health endpoints. A round counts only when every endpoint confirms the same commit; rollout and rollback use the same rules.

Automatic deployment rollback also requires an explicit backward-data-compatibility declaration; otherwise rollback is blocked and maintainer-provided manual recovery steps are shown.

Execution resources queue model calls, commands and exclusive benchmarks by priority. Requests waiting at least 30 seconds take FIFO precedence; diagnostics include bounded wait samples and cancellation receipts.

Optional disk and memory low-water admission keeps new tasks queued until resources recover. Both checks default to disabled; when configured, unavailable measurements also defer admission with an explicit reason.

Diff reading folds by file and loads content when expanded. Mobile defaults to wrapping; search opens the matching file, and large diffs locate matches using their actual wrapped layout.

Task details can explain why work has not started, showing prerequisite chains, pause, retry time, working hours, global capacity and resource pressure together. Clearing one condition preserves the others.

Configuration validation shows localized stable reasons and safe field paths. Unknown request errors retain a copyable diagnostic ID for log correlation.

Project and page navigation enter browser history. Back, forward and reload retain the selected project; notification task details can be restored through history, and closing details preserves project and page context.
