# Contributing

[简体中文](CONTRIBUTING.md) | **English**

Report bugs and propose improvements through Issues, or contribute through a Pull Request. Describe expected behavior, actual behavior, and reproduction steps. Screenshots help with UI problems.

## Local development

Requires Python 3.9+. The fully tested execution environment is macOS. Models can use authenticated Codex CLI or a tool-capable native API. GitHub import, sync and approved delivery use authenticated `gh`. Routine tests substitute the model interface and require no paid model calls.

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
node --check adaptive_rsi/web/model-center.js
node --test tests/web_requests.cjs
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
python3 scripts/start.py
```

Keep temporary UI-check projects paused to avoid unintended model calls. Store runtime state outside managed repositories.

`test:browser` creates two paused temporary projects and uses real Chromium to exercise forms, pagination, project switching, languages, and mobile layouts without invoking models. Set `RSI_BROWSER_CHANNEL=chrome` to use installed Chrome. Playwright is a development dependency only.

GitHub main-branch protection requires a PR, successful `verify` checks against the latest main, and resolved conversations, including administrators. With only one maintainer, a second human approval is not required; this does not establish independent human review.

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

## Security and evaluation boundaries

Do not commit credentials, authenticated session URLs, local state, databases, browser logs, or private project content. Do not include live credentials or private data in public security reports.

For scheduler, isolation, budget, acceptance, or promotion changes, explain their effect on existing boundaries. Model success claims cannot replace fixed checks. Candidates must not modify protected evaluators. Do not claim recursive capability gains without independent evaluation.

For bundles and update assets, see [RELEASING.en.md](RELEASING.en.md). Automatic PRs and merging require explicit project authorization and all delivery gates.

Contributions are provided under the repository's MIT license.
