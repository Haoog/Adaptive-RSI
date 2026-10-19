# Releases and user updates

[简体中文](RELEASING.md) | **English**

Maintainers explicitly publish releases. Building a plugin does not push Git, create PRs or publish releases.

## Bundle contract

- Keep versions in `plugin.json`, `pyproject.toml`, `adaptive_rsi/__init__.py` and `update.json` aligned.
- Use stable `major.minor.patch` versions and matching `vX.Y.Z` tags. Drafts and prereleases are skipped.
- The current manifest accepts only `format: 1`, `python_min: [3, 9]` and `state_schema: 1`. Incompatible database changes require a designed migration/rollback path and a protocol change, rather than just a version bump.
- Each release needs `adaptive-rsi-X.Y.Z-plugin.zip` and its matching `.zip.sha256`. The archive root must be `adaptive-rsi/`.
- The update source is fixed to the latest stable GitHub Release in `Haoog/Adaptive-RSI`. Independent release signing is not implemented.

## Local preparation and verification

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
python3 scripts/package.py
uv build --wheel --out-dir dist
```

Update both documentation languages, validation records and release notes. Inspect the ZIP for credentials, sessions, local state, caches or project data. Use isolated state to rehearse upgrade, health checks and database recovery after failed startup.

Create a draft release on GitHub, upload the two matching assets, verify versions, checksums, notes and validation scope, then explicitly publish. Pushing main alone does not notify users: a stable release matching this contract is required. The project does not automatically publish releases.

## User behavior and limits

The default checks daily and notifies. Automatic mode installs only same-major/minor stable patches while idle, such as `0.2.0 → 0.2.1`. `0.2.x → 0.3.x` needs user confirmation. Work budgets never increase automatically.

Updates use version directories and an active pointer, without `git pull` in user source checkouts. Failed startup restores previous code/database; recovery from power loss or supervisor crashes remains unverified. Launch-environment model secrets pass to the new service and are never written into update plans.

Loaded Codex skills/plugin metadata need marketplace refresh and a Codex restart. Runtime updates cannot hot-replace them. After the first successful managed upgrade, `STATE/installations/current` can serve as a stable local marketplace directory. Wheel users must update their pip package or use plugin launch scripts that follow the active pointer.
