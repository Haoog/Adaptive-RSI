# Connect model accounts in the dashboard

[简体中文](accounts.md) | **English**

Open **Model settings → Connections and models** and select Codex, OpenCode or an API service. Connections belong to the machine running Adaptive RSI. For a server installation, complete official authorization in your own browser; your personal computer can then be turned off.

## Codex

1. Select **Codex login** and click **Open official Codex login**.
2. Open the official authorization page, enter the displayed one-time code and sign in.
3. Return to the dashboard. When authorization completes, the model directory refreshes so you can assign planning, implementation and review models.

Device authorization must be enabled in ChatGPT account security settings or workspace permissions. If unavailable, use an API connection. Enter account passwords only on the official page. Start a new authorization after expiry, cancellation or a server restart that interrupts the pending flow.

**Sign out** removes the corresponding CLI's local credentials on the runtime machine. It cannot recall requests already sent and does not revoke every session at the provider.

## OpenCode

1. Select OpenCode, then a specific provider.
2. The page displays the provider's actual supported methods: official login, device codes, authorization codes or an API key. Some providers request an account type or other connection details.
3. After connecting, refresh the directory and assign `provider/model` entries to roles.

Methods requiring a local-browser callback that cannot work reliably on the server are explained rather than offered as working logins; choose device authorization or a key instead. Account connection does not load project plugins, MCP or project instructions.

## Official APIs, gateways and local services

Choose the service, enter its endpoint and key, then save and fetch models. Local services without authentication can select **No key**. Services without OAuth use API-key setup instead of a nonfunctional login button.

## Storage and recovery

- Successful login refreshes the model directory without automatically running potentially paid model tests.
- Credentials use the runtime machine's CLI authentication location with `0600` file permissions. Codes, authorization links and keys are not written to project tasks, events or browser local storage.
- macOS uses local CLIs. Linux servers without host CLIs use the cached trusted worker image. Authentication containers mount only a temporary HOME; the temporary OpenCode endpoint is loopback-only and password-protected.
- If a CLI or image is missing, install the corresponding CLI or rebuild `deploy/Dockerfile.worker`. Restarting is not a replacement for login. Ordinary saved settings take effect immediately.
- Pausing a project does not sign out. Signing out does not automatically change model assignments or delivery authorization.

Official references: [Codex authentication](https://learn.chatgpt.com/docs/auth), [Codex App Server](https://learn.chatgpt.com/docs/app-server), [OpenCode Server](https://opencode.ai/docs/server/). Actual account authorization remains subject to the provider's official service.
