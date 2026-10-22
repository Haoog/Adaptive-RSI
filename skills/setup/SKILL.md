---
name: setup
description: Set up the Adaptive RSI local runtime and open its visual control panel after plugin installation.
---

Resolve the plugin root as two directories above this file's containing folder. Run
`python3 <plugin-root>/scripts/rsi doctor`, then
`python3 <plugin-root>/scripts/start.py` to start or reopen the local panel.
Use absolute paths and quote paths with spaces. The runtime requires Python 3.9+, Git,
and Codex, OpenCode or a tool-capable native API provider for model work.
macOS Seatbelt or Linux Docker isolates CLI workers; checks and native commands use Seatbelt or network-disabled Docker.
For Linux installation and persistent operation, follow SERVER.md; build deploy/Dockerfile.worker first.

The plugin supplies skills; it does not add a native Codex settings tab. Settings live in
the authenticated localhost panel. State lives in `~/.local/share/adaptive-rsi`, or
`ADAPTIVE_RSI_HOME`. Never display the server session file, auth files, or full logs.

If the user asks to connect a project, register its Git root through
`python3 <plugin-root>/scripts/rsi add <repository-root>`.
Add `--enable` only when the user asks to activate autonomous work. Registration alone
leaves it paused. Enabling permits local model calls and isolated local edits; the default
delivery is a retained local candidate. Projects can separately authorize automatic PRs and merging in project settings; do not enable them without user authorization. All independent gates and GitHub rules remain mandatory. Multiple GitHub URLs can be imported with
`python3 <plugin-root>/scripts/rsi import <url> ...`; each starts paused with separate state.
New project registration waits until active runs finish to preserve isolation boundaries.
Global update policy is available in the panel. Runtime updates do not hot-reload Codex skills;
marketplace refresh and a Codex restart are still needed for loaded plugin metadata.
Do not change the user's Codex global model settings or install a system service implicitly.
