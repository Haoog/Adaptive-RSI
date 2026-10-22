# Desktop App

[简体中文](DESKTOP.md) | **English**

The desktop app uses Tauri 2 and a bundled Python runtime. No separate Python installation is needed. UI assets ship locally; authenticated API requests use a native bridge rather than loading a remote dashboard.

## Lifecycle

- Opening the app starts its local service with projects paused. Connect a model, then explicitly click Start for a project.
- Closing the window hides it to the menu bar. Already-started projects continue.
- Choosing Quit stops only the app-owned service, preserving projects, settings and records. Reopening requires Start again.
- An existing independent local service is attached without pausing projects or stopping it on app exit.
- Use the runtime-machine button to paste an authenticated HTTPS server URL. Connections last for this app process only. Server tasks do not depend on the desktop window. The server must support API version 1.

State remains in `~/.local/share/adaptive-rsi`; `ADAPTIVE_RSI_HOME` selects a separate directory. Do not run different versions against the same directory simultaneously. Upgrade an older existing service first.

## Records and Usage

Real call records are grouped by task, including failures and retries. Filter by project, dates, model/task search, and inspect details. Background checks and connection tests are identified separately. Missing usage is unknown, not zero; missing historical input/output/cache fields cannot be reconstructed. Model time is summed call duration, not task wall time. Daily buckets use UTC. Cost estimates include only priced calls, do not represent provider bills, and impose no call quota.

## Build

Future formal releases will include a macOS ARM64 installer named `adaptive-rsi-version-macos-arm64-adhoc.dmg` and its `.dmg.sha256` checksum on GitHub Releases. Installation on other platforms has not been verified; the local runtime is not supported on Windows yet.

```sh
# Requires Node.js, Rust, uv and platform-specific Tauri build dependencies
npm ci --prefix desktop
npm run --prefix desktop build
# Development
npm run --prefix desktop dev
```

The build downloads Python 3.12, installs PyInstaller and packages a standalone engine. The app version comes from the Python package rather than a second release-version source. Bundles are under `desktop/src-tauri/target/release/bundle/`.

Builds use local ad-hoc signatures only. Apple Developer ID signing and notarization are not configured; macOS may show a security warning, and the package is not notarized. To update, download the new asset from GitHub Releases, quit, then replace the app manually. Server source hot-updates are disabled for an app-owned engine to prevent UI/engine version mismatch. Replacing the app preserves its data directory.
