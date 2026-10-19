---
name: setup
description: Set up the Adaptive RSI local runtime and open its visual control panel after plugin installation.
---

Resolve the plugin root as two directories above this file's containing folder. Run
`python3 <plugin-root>/scripts/rsi doctor`, then
`python3 <plugin-root>/scripts/start.py` to start or reopen the local panel.
Use absolute paths and quote paths with spaces. The runtime requires Python 3.9+, Git,
and an authenticated Codex CLI for model work. macOS Seatbelt protects model workers;
Docker or Seatbelt isolates checks. Other operating systems currently block model work.

The plugin supplies skills; it does not add a native Codex settings tab. Settings live in
the authenticated localhost panel. State lives in `~/.local/share/adaptive-rsi`, or
`ADAPTIVE_RSI_HOME`. Never display the server session file, auth files, or full logs.

If the user asks to connect a project, register its Git root through
`python3 <plugin-root>/scripts/rsi add <repository-root>`.
Add `--enable` only when the user asks to activate autonomous work. Registration alone
leaves it paused. Enabling permits local model calls and isolated local edits; the default
delivery is a retained local candidate. GitHub publishing is separately configured by the user.
Do not change the user's Codex global model settings or install a system service implicitly.
