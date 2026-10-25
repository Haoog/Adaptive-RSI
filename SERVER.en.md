# Server deployment

[简体中文](SERVER.md) | **English**

A Linux server runs code, model execution, checks and delivery. Your personal computer only opens the dashboard and can be turned off. macOS also supports local operation with `python3 scripts/start.py`.

For phones, append `/install` to the configured HTTPS console origin or choose Install on phone in the sidebar. See [Mobile installation and workflow guide](EXPERIENCE.en.md).

## Install and connect

Install Python 3.9+ with venv, Git, authenticated GitHub CLI, a working Docker Engine and systemd user services. The service account needs Docker access. Use a dedicated account; this is a single-user maintenance tool.

```sh
git clone https://github.com/Haoog/Adaptive-RSI.git
cd Adaptive-RSI
gh auth login
bash scripts/install-server.sh
sudo loginctl enable-linger "$(id -un)"
```

The installer builds `adaptive-rsi-worker:local`, creates an isolated Python environment and starts a user service without overwriting existing settings. Lingering keeps it running after SSH logout and across reboots.

Alternatively, install directly from the verified release bundle without cloning (SHA-256 checked; the layout stays compatible with built-in self-updates):

```sh
curl -fsSL https://raw.githubusercontent.com/Haoog/Adaptive-RSI/main/scripts/remote-install.sh | bash
```

By default the app listens only on the server's `127.0.0.1:6090`. On your personal computer:

```sh
ssh -N -L 6090:127.0.0.1:6090 user@server
```

Run `journalctl --user -u adaptive-rsi` on the server for the authenticated startup URL, then open it in your local browser. The URL contains session credentials; keep it private and note that it stays in the browser history—clear that entry on shared devices. A plain URL displays recovery instructions. If the link may have leaked, choose **Rotate access link** in the console's data and diagnostics dialog: the old link and sessions become invalid immediately, server.json is updated, and the rotation survives restarts.

Register a path **on the server**, or import a GitHub URL. Model connections also use the server's network and authentication; logins from your personal computer are not uploaded. New projects remain paused until models, executable checks and delivery settings are ready.

## Models and project environments

- APIs: add official or compatible connections in the UI. Linux defaults to private 0600 credential files that survive restarts; environment variables and session-only credentials remain available.
- OpenCode: the image includes 1.19.3. Select OpenCode and a provider in the dashboard, then use its supported official authorization or API-key method. Authentication and model discovery can use the worker image without a host CLI. CLI login and global `provider` configuration remain available.
- Codex: the image includes 0.163.0. Start device authorization in the dashboard, then open the official page and enter the one-time code in your personal browser. Credentials are saved to the server service account's `~/.codex/auth.json`. Responses API and compatible connections remain available. See [Account connections](docs/accounts.en.md).
- Dependencies: default checks use `python:3.12-slim`; pull it in advance with `docker pull python:3.12-slim`. For this project, set advanced JSON to `{"sandbox":{"image":"adaptive-rsi-worker:local"}}`. Build trusted images with required language tools and cached dependencies for other projects. Downloads belong in trusted environment preparation; check containers have no network.

CLI containers mount only the current workspace and isolated HOME. They do not mount controller data, other projects, evaluator cases, SSH/GitHub credentials or the Docker socket. Read-only roles cannot edit source and Git metadata remains read-only. CLI networking is available for model requests; independent checks and native command tools have no network. CLIs can access the selected model connection credentials they need; this is not confidential-computing protection against arbitrary hostile programs.

## HTTPS domain access

Configure a reverse proxy (see [Caddyfile](deploy/Caddyfile.example)) and add to `~/.config/adaptive-rsi/server.env`:

```sh
ADAPTIVE_RSI_PUBLIC_URL=https://rsi.example.com
```

Restart with `systemctl --user restart adaptive-rsi` and use the new authenticated startup URL. The public endpoint uses HTTPS while the app stays on loopback. Host, Origin, session-cookie and CSRF checks remain enforced; domain cookies use `Secure`. Do not expose model executor services or Docker APIs publicly.

## Operations and data

```sh
systemctl --user status adaptive-rsi
systemctl --user restart adaptive-rsi
systemctl --user stop adaptive-rsi
journalctl --user -u adaptive-rsi -f
```

State defaults to `~/.local/share/adaptive-rsi`; clones live beside it in `adaptive-rsi-projects`. Back up state, private credentials and projects without committing them to GitHub. The existing supervised updater handles runtime upgrades; the user service tracks replacement processes and failed starts attempt restoration. Rebuild execution images separately. Active tasks keep their frozen configuration.

Project inspection and Issue sync are the only schedules, both hourly by default. Findings, calls, task duration and changed-file counts have no quotas; pause and cancellation remain available. Bounded logs, network request timeouts and container boundaries protect implementation resources without limiting the number of findings or tasks.
