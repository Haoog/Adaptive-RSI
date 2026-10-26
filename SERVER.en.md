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
- Dependencies: default checks use `python:3.12-slim`; pull it in advance with `docker pull python:3.12-slim`. For this project, configure the check image with `adaptive-rsi config --project <PROJECT_ID> --json '{"sandbox":{"image":"adaptive-rsi-worker:local"}}'`. Build trusted images with required language tools and cached dependencies for other projects. Downloads belong in trusted environment preparation; check containers have no network.

CLI containers mount only the current workspace and isolated HOME. They do not mount controller data, other projects, evaluator cases, SSH/GitHub credentials or the Docker socket. Read-only roles cannot edit source and Git metadata remains read-only. CLI networking is available for model requests; independent checks and native command tools have no network. CLIs can access the selected model connection credentials they need; this is not confidential-computing protection against arbitrary hostile programs.

The image defaults to the `node` user. Executors already pass explicit `--user` with the service account's host UID/GID and retain a read-only root filesystem, `--cap-drop=ALL` and `no-new-privileges`; the image default does not override these boundaries. Avoid running the service as root. Host Docker administration remains a privileged capability.

The base image defaults to `node:22-bookworm@sha256:63538dfe497cc29dfafdda25ada09b46db68a3f4cc205abf1f5d6bf0913f3384`, pinning a multi-platform OCI index so Docker selects the matching platform. Verified on 2026-10-25 against the official Docker registry's [`library/node:22-bookworm` manifest](https://registry-1.docker.io/v2/library/node/manifests/22-bookworm): SHA-256 hashes of the raw JSON fetched by tag and digest both matched `Docker-Content-Digest`. The index includes `linux/amd64`, `linux/arm/v7`, `linux/arm64/v8` and `linux/ppc64le`; the amd64 and arm64 child manifest hashes and lengths were also verified. `docker buildx imagetools inspect node:22-bookworm` returned the same index digest.

To upgrade the base, run that inspect command and verify the official registry, then override it with `docker build --build-arg NODE_IMAGE='node:22-bookworm@sha256:ACTUALLY_VERIFIED_DIGEST' -f deploy/Dockerfile.worker -t adaptive-rsi-worker:local .`. Replace the digest placeholder. A pinned digest does not follow tag updates: security updates require explicitly changing the digest and rebuilding. This verification did not require a Docker daemon; no image build or container run was performed. Pinning the base does not lock every transitive npm dependency. Custom check images can use verified digest references in `sandbox.image`.

## HTTPS domain access

Configure a reverse proxy (see [Caddyfile](deploy/Caddyfile.example)) and add to `~/.config/adaptive-rsi/server.env`:

```sh
ADAPTIVE_RSI_PUBLIC_URL=https://rsi.example.com
```

Restart with `systemctl --user restart adaptive-rsi` and use the new authenticated startup URL. The public endpoint uses HTTPS while the app stays on loopback. Host, Origin, session-cookie and CSRF checks remain enforced; domain cookies use `Secure`. Do not expose model executor services or Docker APIs publicly.

The Caddy example sends `Strict-Transport-Security: max-age=31536000` at the HTTPS edge, instructing browsers to prefer HTTPS for the following year. Consider `includeSubDomains` or preload only after every subdomain supports HTTPS. The application's local HTTP listener does not send HSTS.

Authentication failures share a 60-second window per listener: the first 30 retain the existing 401 response; subsequent failures receive 429 with `Retry-After`. Valid cookies or startup links continue to permit login, API access and health checks. Visitors behind a proxy share the failure counter; forgeable forwarded-IP headers do not establish identity. Host/Origin/CSRF rejections and session failures share at most one security audit per minute, with fixed reasons and a suppressed count, excluding URLs, tokens, cookies and request bodies. `/api/health` retains Host and cookie authentication.

API exceptions return fixed messages or allowlisted machine error codes, keeping filesystem paths, credentials and upstream response bodies out of exception responses. Session rotation atomically saves a private file before updating memory; failed persistence keeps the existing session valid.

## Operations and data

```sh
systemctl --user status adaptive-rsi
systemctl --user restart adaptive-rsi
systemctl --user stop adaptive-rsi
journalctl --user -u adaptive-rsi -f
```

State defaults to `~/.local/share/adaptive-rsi`; clones live beside it in `adaptive-rsi-projects`. Back up state, private credentials and projects without committing them to GitHub. The existing supervised updater handles runtime upgrades; the user service tracks replacement processes and failed starts attempt restoration. Rebuild execution images separately. Active tasks keep their frozen configuration.

Backup downloads retain cookie authentication, accept files up to 1 GiB and allow at most two concurrent transfers. Binary responses stream in 64 KiB blocks, with a 30-second write timeout and a 120-second total transfer budget; JSON chunks retain their 512 KiB maximum. Symlinks and non-regular files are rejected. Interrupted downloads close the connection; use the service account to back up local state when these limits are exceeded.

Project inspection and Issue sync are the only schedules, both hourly by default. Findings, calls, task duration and changed-file counts have no quotas; pause and cancellation remain available. Bounded logs, network request timeouts and container boundaries protect implementation resources without limiting the number of findings or tasks.
