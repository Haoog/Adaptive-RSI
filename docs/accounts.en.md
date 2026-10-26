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

OpenCode `1.19.3` and `2.1.3` are supported. The runtime detects the version and adapts its CLI arguments automatically; if a version is unsupported, the page reports it and suggests an upgrade. Methods requiring a local-browser callback that cannot work reliably on the server are explained rather than offered as working logins; choose device authorization or a key instead. Account connection does not load project plugins, MCP or project instructions.

### OpenCode Go

Verified against the [official OpenCode Go documentation](https://opencode.ai/v2/docs/console/go/): obtain a Go API key in the [OpenCode console](https://opencode.ai/console). In the OpenCode TUI, run `/connect`, select **OpenCode Go**, paste the key, then use `/models`. The provider ID is `opencode-go`; OpenCode routes use `opencode-go/<model-id>`. In this dashboard, select the matching provider under the OpenCode connection and use **Save provider key**, or add a native API service using the **OpenCode Go API Key** preset. Signing in to Codex through a ChatGPT subscription does not supply Go credentials. Do not send a Go key to the official OpenAI API endpoint.

Choose the native API protocol and endpoint for the model. These are base URLs; the selected protocol appends its request path:

| Dashboard protocol | Base URL | Request path |
| --- | --- | --- |
| Responses API | `https://opencode.ai/inference/go/openai/v1` | `/responses` |
| Chat Completions API | `https://opencode.ai/inference/go/openai/v1` | `/chat/completions` |
| Anthropic Messages | `https://opencode.ai/inference/go/anthropic/v1` | `/messages` |
| Gemini GenerateContent | `https://opencode.ai/inference/go/google/v1beta` | `/models/<model-id>:generateContent` |

For example, the current official list assigns `gpt-6-luna` to Responses, `kimi-k3` to Chat Completions and `minimax-m3` to Anthropic Messages. The Go preset defaults to Chat Completions; select the matching protocol under **Advanced connection settings** for other models. For the official Go base URLs above, this dashboard automatically uses the separate public directory `GET https://opencode.ai/inference/go/v1/models` without sending inference keys. The directory has no protocol metadata, so select each model's matching protocol using the official documentation; missing model IDs can still be added manually. Current models, endpoints, remaining allowance and terms come from the official console. Verifying the documentation does not establish successful inference. Fetching the catalog and saving a key do not automatically run paid model tests.

## Official APIs, gateways and local services

Choose the service, enter its endpoint and key, then save and fetch models. Local services without authentication can select **No key**. Services without OAuth use API-key setup instead of a nonfunctional login button.

## Storage and recovery

- Successful login refreshes the model directory without automatically running potentially paid model tests. A manually opened model test uses the project scope captured when it opened. Switching projects or settings scopes closes the old dialog, and late results cannot replace feedback in the new project.
- Credentials use the runtime machine's CLI authentication location with `0600` file permissions. Codes, authorization links and keys are not written to project tasks, events or browser local storage.
- macOS uses local CLIs. Linux servers without host CLIs use the cached trusted worker image. Authentication containers mount only a temporary HOME; the temporary OpenCode endpoint is loopback-only and password-protected.
- If a CLI or image is missing, install the corresponding CLI or rebuild `deploy/Dockerfile.worker`. Restarting is not a replacement for login. Ordinary saved settings take effect immediately.
- Pausing a project does not sign out. Signing out does not automatically change model assignments or delivery authorization.
- After choosing to follow shared model assignments for a project, save to remove its overrides. Inheritance resolves against shared settings at save time, so defaults changed while a reset draft is pending do not pin old values. Roles manually edited after reset retain those edits; active runs continue using their sealed configuration.

Official references: [Codex authentication](https://learn.chatgpt.com/docs/auth), [Codex App Server](https://learn.chatgpt.com/docs/app-server), [OpenCode Server](https://opencode.ai/docs/server/). Actual account authorization remains subject to the provider's official service.
