# Contributing

[简体中文](CONTRIBUTING.md) | **English**

Report bugs and propose improvements through Issues, or contribute through a Pull Request. Describe expected behavior, actual behavior, and reproduction steps. Screenshots help with UI problems.

## Local development

Requires Python 3.9+. The fully tested execution environment is macOS. Models can use authenticated Codex CLI or a tool-capable native API. GitHub import, sync and approved delivery use authenticated `gh`. Routine tests substitute the model interface and require no paid model calls.

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
python3 scripts/start.py
```

Keep temporary UI-check projects paused to avoid unintended model calls. Store runtime state outside managed repositories.

## Commits and documentation

- Use Chinese commit messages describing the behavior change, for example: `修复巡检任务重复入队的问题` (fix duplicate inspection tasks).
- PR descriptions can be Chinese or English. Explain the problem, behavior change, verification, and known limitations.
- Update both READMEs, relevant documentation, and UI translations when changing user-facing features. Chinese is the default UI language; English translations live in `adaptive_rsi/web/i18n.js`.
- Do not translate user goals, Issue bodies, model output, configuration keys, or raw evidence when switching UI language. Check both static and dynamically rendered text.
- Verify meaningful behavior and risks. Model-substitute tests do not establish improvements in real model capability.

## Security and evaluation boundaries

Do not commit credentials, authenticated session URLs, local state, databases, browser logs, or private project content. Do not include live credentials or private data in public security reports.

For scheduler, isolation, budget, acceptance, or promotion changes, explain their effect on existing boundaries. Model success claims cannot replace fixed checks. Candidates must not modify protected evaluators. Do not claim recursive capability gains without independent evaluation.

For bundles and update assets, see [RELEASING.en.md](RELEASING.en.md). Each candidate requires explicit user approval before PR creation.

Contributions are provided under the repository's MIT license.
