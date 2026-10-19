# Local validation / 2026-10-19

[简体中文](VALIDATION.md) | **English**

Version: 0.1.0. Host: macOS / Python 3.9.6 / Node 20.20.2 / Git 2.54.0.

## Executed checks

- `python3 -m unittest discover -v`: **32 tests passed**. Raw output is in `output/test-results.txt` in the development checkout.
- `node --check adaptive_rsi/web/app.js`, `node --check adaptive_rsi/web/i18n.js`, and Python byte-compilation passed.
- Both bundled skills passed the system skill-creator validator. The validator's PyYAML dependency was supplied in an isolated uv environment; the runtime has no third-party Python dependencies.
- The runtime wheel and local plugin ZIP were built. The plugin archive excludes state, sessions, auth data, build caches and browser logs.
- Playwright checked a real local server at 1440 x 1100 and 390 x 844. Project registration, paused-by-default state, schedule edits and goal enqueueing persisted. The browser reported zero console errors/warnings on the final page. Screenshots are in `output/playwright/`.

- Bilingual checks confirmed that switching language preserves unsaved settings, saving persists changes, reloading remembers English, and the English task view retains Chinese goal text. At 390px width there was no horizontal overflow; the final page reported zero console errors or warnings.

The new provider and review controls were checked in the in-app browser: provider registration, role routing, provider-default effort and review thresholds saved correctly. Switching languages preserved unsaved provider/model values; saved values survived a reload. At 1440 x 1100 and 390 x 844 there was no horizontal overflow or console error/warning. Temporary provider settings were removed and original model/threshold defaults restored. No real project was registered or enabled. Screenshot: `output/playwright/models-review-settings.png`.

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

No paid model calls or autonomous GitHub branch pushes, PR creation, or merges were performed. Publishing this source repository does not validate autonomous delivery. Codex authentication was checked without exposing credentials. Model availability is verified on the first real invocation; routes have no silent fallback. A real project is still needed for provider, toolchain, dependency, GitHub and long-running integration checks.

Docker was installed but its daemon was stopped, so Docker execution and cleanup were not exercised. The working local backend was macOS Seatbelt. Linux model workers deliberately remain blocked; Windows hosting is not supported by this POSIX runtime. Language adapters were checked against manifest fixtures; those tests do not certify builds for every language/toolchain.

Long-term recursive improvement, transfer across projects, protection against arbitrary adversarial code, and strict monetary budgeting have not been established. Invocation limits count Codex CLI sessions, not every underlying model API request. Promotion requires a separately prepared evaluation corpus; absent evidence leaves the stable strategy active.
