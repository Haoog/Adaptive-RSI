# Live validation / 2026-10-20

[简体中文](LIVE_VALIDATION.md) | **English**

The autonomous delivery loop was exercised with the real Codex CLI 0.163.0 using local ChatGPT authentication and a separate private GitHub repository, `Haoog/adaptive-rsi-validation-20261020`. The requested models were preserved; substitute agents were not reported as real-model success.

## Completed live loop

- [Validation PR #1](https://github.com/Haoog/adaptive-rsi-validation-20261020/pull/1) fixes addition implemented as subtraction and adds positive, negative, zero and fractional regression cases.
- [Validation PR #2](https://github.com/Haoog/adaptive-rsi-validation-20261020/pull/2) implements subtraction from PR #1's merged SHA while preserving addition. The production scheduler queue automatically merged and verified it, again yielding `verified`. The user checkout stayed at the initial revision throughout.
- Planning, independent acceptance design before implementation, and review used `gpt-6.1-sol / high`; implementation used `gpt-6-luna / max`. Value assessment, frozen original regression, full candidate suite, independent task acceptance and six-area code review all passed.
- After real GitHub Actions success, the runtime automatically created and squash-merged the PR, without a manual merge confirmation or admin bypass. It then fetched remote mainline again and ran isolated post-merge verification, yielding `verified`.
- Merge SHAs, role-specific calls, usage and check groups appear in the [sanitized validation record](docs/validation/autonomous-loop-20261020.json). Raw evidence remains in isolated local state; authentication, session values and secrets are not published.

## Problems found through real execution

Live calls exposed private HTTPS Git authentication, Codex parent-metadata checks, unsupported nested macOS Seatbelt isolation, a read-only planner incorrectly trying to implement, descriptions mixed into scope paths, and generated checks assuming a `python` alias. Generated Python checks now normalize that command to `python3`. Failed candidates stopped without creating or merging PRs. Current Codex named permissions provide a single sandbox layer: command tools cannot read controller/other-project/authentication data, write `.git`, or access the network. The CLI installation binary remains readable.

Acceptance design runs in its own snapshot, so its model outputs never enter the implementer's workspace. The host seals acceptance files and gives the immutable specification to independent review only after implementation; review checks for trivial always-passing tests.

## Fault-validation boundaries

Local real Git, real sandboxing and deterministic checks cover erased failing tests, weak-test false positives, post-merge regressions, restart recovery and dirty-file preservation. PR conflicts, empty/failed CI, required-review waiting, changed heads and revoked authorization use controlled status injection; these are not additional real-provider model tests.

Success on a small controlled Python repository establishes this scenario and delivery path. It does not establish every provider/language, deployment success, or sustained recursive improvement. APIs and gateways still require validation with their actual accounts/models. Models and budgets were not silently substituted or expanded.

## Validating another project

Start with a paused independent project, select provider/model/effort, and configure executable checks and sufficient call budgets. After explicit activation, inspect value evidence, sealed acceptance, all checks, review, exact commits and CI. Delivery defaults to local candidates; automatic PRs and merging require explicit authorization for that project. Strict required checks or a merge queue are recommended on mainline; GitHub review requirements always remain enforced. Inspect post-merge outcomes rather than treating local success as deployment proof.
