# 在页面连接模型账号

**简体中文** | [English](accounts.en.md)

进入「模型设置 → 连接与模型」，选择 Codex、OpenCode 或 API 服务。连接使用运行 Adaptive RSI 的机器；在服务器部署时，你只需在自己的浏览器完成官方授权，个人电脑不需要一直开机。

## Codex

1. 选择「Codex 登录」，点击「前往 Codex 官方登录」。
2. 打开官方授权页面，输入页面显示的一次性验证码并完成登录。
3. 返回控制台等待完成；系统刷新模型目录，然后可为规划、实现和评审选择模型。

设备授权需要在 ChatGPT 账号安全设置或工作区权限中启用。如果官方不允许此方式，可改用 API 连接。账号密码只在官方页面填写。授权有效期结束、服务重启中断或点击取消后，重新发起登录即可。

「退出账号」移除运行机器上对应 CLI 的本地凭据，不会终止已发出的模型请求，也不等于撤销账号在供应商处的所有会话。

## OpenCode

1. 选择 OpenCode，再选择具体供应商。
2. 页面按供应商实际提供的方法显示官方登录、设备验证码、授权码或 API 密钥输入；部分供应商还需选择账号类型等信息。
3. 完成连接后刷新模型目录，在工作分工中选择 `provider/model`。

兼容 OpenCode `1.19.3` 和 `2.1.3`。版本差异由运行器自动识别，页面不会要求你手动改写 CLI 参数；版本不支持时会显示检测结果和升级建议。只支持本地浏览器回调、无法在当前服务器环境可靠完成的方法会显示说明；可选择设备授权或密钥。服务不加载项目插件、MCP 或项目指令来完成登录。

### OpenCode Go

已核验 [OpenCode Go 官方文档](https://opencode.ai/v2/docs/console/go/)：在 [OpenCode 控制台](https://opencode.ai/console) 获取 Go API Key；OpenCode TUI 中运行 `/connect`、选择 **OpenCode Go** 并粘贴密钥，再用 `/models` 选择模型。供应商 ID 是 `opencode-go`，OpenCode 路由写作 `opencode-go/<model-id>`。在本控制台的 OpenCode 连接中选择对应供应商并使用「保存供应商密钥」，或使用「OpenCode Go API Key」预设添加原生 API 服务。ChatGPT 订阅的 Codex 登录不会提供 Go 凭据，不要把 Go 密钥填到 OpenAI 官方 API 地址。

原生 API 的地址与协议须按模型选择；下列是基础地址，调用时由对应协议追加路径：

| 界面协议 | 基础地址 | 调用路径 |
| --- | --- | --- |
| Responses API | `https://opencode.ai/inference/go/openai/v1` | `/responses` |
| Chat Completions API | `https://opencode.ai/inference/go/openai/v1` | `/chat/completions` |
| Anthropic Messages | `https://opencode.ai/inference/go/anthropic/v1` | `/messages` |
| Gemini GenerateContent | `https://opencode.ai/inference/go/google/v1beta` | `/models/<model-id>:generateContent` |

例如，官方当前将 `gpt-6-luna` 列为 Responses，`kimi-k3` 列为 Chat Completions，`minimax-m3` 列为 Anthropic Messages。Go 预设默认 Chat Completions，其他模型须在「高级连接设置」选择匹配协议。本控制台对上述官方 Go 基础地址自动使用独立公开目录 `GET https://opencode.ai/inference/go/v1/models`，目录请求不发送推理密钥。目录不提供协议元信息，选择模型后仍须按官方说明选择匹配协议；目录遗漏的模型可手动添加 ID。实际模型、地址、剩余额度与服务条款以官方控制台为准。官方文档核验不等于真实推理已验证；获取目录与保存密钥不会自动执行付费模型测试。

## 官方 API、中转站和本地服务

选择相应服务，填写地址和密钥后保存并获取模型。没有密钥的本地服务可选「无需密钥」。不支持 OAuth 的服务使用密钥连接，不会展示没有作用的登录按钮。

## 保存与故障恢复

- 登录后自动获取模型目录，不自动运行可能收费的模型能力测试。手动打开的模型测试固定使用打开时的项目范围；切换项目或设置范围会关闭旧弹窗，旧测试的迟到结果不会写入新项目反馈。
- 凭据保存在运行机器的 CLI 认证位置，文件权限为 `0600`；验证码、授权链接和密钥不写入项目任务、运行事件或浏览器本地存储。
- macOS 使用本机 CLI；没有宿主 CLI 的 Linux 服务器使用已缓存的可信执行镜像，认证容器只挂载临时 HOME。临时 OpenCode 服务只允许本机访问，并要求随机密码。
- 页面提示连接工具或镜像缺失时，安装对应 CLI 或重新构建 `deploy/Dockerfile.worker`。重启不代替登录；普通配置保存后立即生效。
- 暂停项目不会退出模型账号；退出账号也不会自动改变项目的模型分工或交付授权。
- 项目模型分工选择跟随通用设置后，需保存才能清除对应覆盖。保存按当时的通用设置解析继承，即使重置草稿期间默认值变化也不会固定旧值；重置后手动编辑的角色按编辑值保存，运行中的任务继续使用其封存配置。

官方依据：[Codex 认证](https://learn.chatgpt.com/docs/auth)、[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[OpenCode Server](https://opencode.ai/docs/server/)。真实供应商是否允许你的账号登录由其官方服务决定。
