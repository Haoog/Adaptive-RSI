# Contributing

[简体中文](CONTRIBUTING.md) | **English**

Report bugs and propose improvements through Issues, or contribute through a Pull Request. Describe expected behavior, actual behavior, and reproduction steps. Screenshots help with UI problems.

## Local development

Requires Python 3.9+. Execution uses macOS Seatbelt or Linux Docker. Models can use Codex, OpenCode CLI or tool-capable native APIs. GitHub import, sync and approved delivery use authenticated `gh`. Routine tests substitute the model interface and require no paid model calls.

You do not need the full suite (~8 minutes) for every local change. Run only the affected group the same way CI splits them, for example `python3 -m unittest tests.test_runtime.RuntimeTest` or `python3 -m unittest tests.test_store`; CI runs the full set in parallel after you push.

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
node --check adaptive_rsi/web/model-center.js
node --check adaptive_rsi/web/auth.js
node --test tests/web_requests.cjs
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
python3 scripts/start.py
```

Keep temporary UI-check projects paused to avoid unintended model calls. Store runtime state outside managed repositories.

`test:browser` creates two paused temporary projects and uses real Chromium to exercise forms, pagination, project switching, languages, and mobile layouts without invoking models. Set `RSI_BROWSER_CHANNEL=chrome` to use installed Chrome. Playwright is a development dependency only.

GitHub main-branch protection requires a PR, successful `verify` checks against the latest main, and resolved conversations, including administrators. With only one maintainer, a second human approval is not required; this does not establish independent human review.

CI first distinguishes documentation changes from code changes. Documentation-only changes check local links, heading anchors and bilingual pairs. Code changes run grouped Python tests, browser checks, JavaScript checks, packaging and Linux checks in parallel; `verify` only aggregates the required results. A newer push cancels unfinished checks for an older commit. Formal releases still run complete installation and desktop-build verification.

## Commits and documentation

- Start every change on a new working branch. Do not commit or push directly to `main`. Automated improvements to this repository must also pass checks, code review, and a PR before merging.
- Use branch prefixes such as `feat/`, `fix/`, `refactor/`, `docs/`, or `chore/` according to the change type. Do not use `codex/`.
- The maintainer has granted standing authorization for this repository: assess value before implementation, then require relevant tests, runtime checks, and code review without blocking findings before pushing the branch, creating a PR, and merging without per-change confirmation. GitHub required checks and reviews must still pass. Stop merging on failures, conflicts, or insufficient evidence.
- This maintenance authorization applies only to this repository. The product defaults to local candidates. Other projects require their own explicit automatic PR/merge authorization; repository development instructions do not grant it.
- These instructions do not configure GitHub branch protection. Maintainers should also configure rules for `main` requiring PRs, checks, and reviews on GitHub.
- Use Chinese commit messages describing the behavior change, for example: `修复巡检任务重复入队的问题` (fix duplicate inspection tasks).
- PR descriptions can be Chinese or English. Explain the problem, behavior change, verification, and known limitations.
- Update both READMEs, relevant documentation, and UI translations when changing user-facing features. Chinese is the default UI language; English translations live in `adaptive_rsi/web/i18n.js`.
- Do not translate user goals, Issue bodies, model output, configuration keys, or raw evidence when switching UI language. Check both static and dynamically rendered text.
- Verify meaningful behavior and risks. Model-substitute tests do not establish improvements in real model capability.
- Run model review after the change has been organized. Review the complete diff and affected behavior first; after a narrow fix, focus on the original finding and adjacent code, and repeat a complete review for scheduling, authentication, upgrade, permission or data-boundary changes. Review and tests are both retained; model judgment does not replace fixed checks.

## Recovering local branches after a merge

Merge PRs with `gh pr merge <number> --squash --match-head-commit <full-reviewed-SHA>` and handle local branch cleanup separately. `--delete-branch` removes both local and remote branches and may switch to an existing local `main` and try to update it. If that old branch has diverged from the squashed mainline, the local update can fail even though the remote PR was merged successfully.

Check the PR's `state` and `mergeCommit`, then inspect `git status`, `git reflog`, and branch differences. Run `git fetch origin main` to refresh the remote-tracking ref, then use `git merge-base --is-ancestor <mergeCommit> origin/main` to confirm that the latest mainline includes the merge. A nonzero exit requires checking the repository, target branch, and remote state before rebuilding a local branch from a stale ref. Preserve existing commits and uncommitted files. Do not repeat the merge, cherry-pick commits already included by a squash merge, or force-reset the local branch. Treat it as a file merge conflict only when Git reports one.

For a clean but divergent local `main`, rename it to a unique `archive/local-main-<date>` backup, then create a new local `main` from the latest `origin/main`. If there are uncommitted files, preserve them in place and continue in a separate worktree. If `node_modules/` or build directories appear only after switching to an old branch, compare `.gitignore` and the files first; a change in ignore rules does not establish that the user added new files. Recover known command side effects without data loss under existing authorization, then verify the result instead of unnecessarily blocking on another confirmation.

## Security and evaluation boundaries

Do not commit credentials, authenticated session URLs, local state, databases, browser logs, or private project content. Do not include live credentials or private data in public security reports.

For scheduler, isolation, budget, acceptance, or promotion changes, explain their effect on existing boundaries. Model success claims cannot replace fixed checks. Candidates must not modify protected evaluators. Do not claim recursive capability gains without independent evaluation.

For bundles and update assets, see [RELEASING.en.md](RELEASING.en.md). Automatic PRs and merging require explicit project authorization and all delivery gates.

Contributions are provided under the repository's MIT license.

Mechanical version PRs take a metadata-only path in ordinary CI only when their six changed files exactly match an independently reconstructed release patch. Any source, workflow, classifier or generator change requires the full ordinary CI path. This removes duplicate source checks; automatic release merge still requires the exact-head full Python, browser, desktop build, smoke and provenance gate in `release-verify.yml`. Timing caches affect worker distribution only. Invalid entries fall back to complete discovery, and cached timings never skip tests.

Large-history benchmark: `python3 -B scripts/benchmark_history.py --tasks 10000 --samples 3 --enforce --output /tmp/rsi-history-performance.json`. The fixture fixes four projects, two runs per task, a 2023 anchor and stable ordinal distribution. Each read plus JSON serialization has a p95 budget of 5000 ms and an 8 MiB response limit. Only the metrics module clock is replaced to replay the fixed window; no models are called. Use `--tasks 50000` for local stress; ordinary CI runs one 10000-task benchmark. Reports include Python/SQLite/platform and dataset identity; budget violations fail explicitly and retain an artifact. This does not measure HTTP networks, cold-disk caches or concurrent production traffic.

Dynamic localization structure check: run `npm ci --ignore-scripts`, then `node scripts/check_i18n_ast.cjs`. Pinned Acorn parses JavaScript syntax and checks literals, conditional expressions, statically traceable aliases and dynamic dictionaries passed to `t()`, respecting lexical shadowing and decoded Unicode escapes. The existing Python check still covers duplicate translation keys and nonempty English content. Reassigned variables, destructuring and for-of value propagation, arbitrary user input, server-returned text and runtime composition that cannot be statically resolved are not covered; bilingual browser scenarios remain necessary. Paired document content equivalence requires diff review; link validation does not establish semantic equivalence.
