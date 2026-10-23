# 发布、部署与用户更新

**简体中文** | [English](RELEASING.en.md)

本仓库的日常主线合并只累积改动，不自动增加版本号或推送用户更新。明确启动稳定批次发布后才准备版本 PR；只有版本补丁检查、完整验证和 GitHub 必需检查及评审全部满足，才按正常分支保护合并并发布正式 GitHub Release。构建插件本身没有推送或发布副作用。发布产品不代表已经部署任何用户服务器。

## 本仓库的版本与发布

`.github/workflows/release.yml` 的主线触发仅识别已经核验的机械版本提交并继续正式发布，普通代码合并不创建版本 PR。准备好一批稳定改动后，在 Actions 手动运行该流程，勾选 `prepare_release`；默认版本级别固定为 `patch`，因此 `0.5.1 → 0.5.2 → 0.5.10` 都只改变末位。只有明确进入新的产品阶段时，才同时选择 `minor` 或 `major` 并勾选 `new_product_stage`；缺少这项确认会被脚本拒绝。缺省不勾选 `prepare_release` 时不准备新版本。此前的 `release:*` PR 标签不再触发发版或决定级别。自动测试、版本 PR 门禁、打包和正式发布保持自动化；这个节奏只约束本仓库，不修改受管项目的交付授权。

四处版本为 `plugin.json`、`pyproject.toml`、`adaptive_rsi/__init__.py`、`update.json`，中英文 `CHANGELOG` 的 `Unreleased` 内容移入相应版本；提交标题使用中文。

版本 PR 的分支包含目标版本和主线 SHA。主线更新后，系统仅在确认旧 PR 完全等于可重建的机械版本补丁时关闭旧 PR，并针对新基线创建一个替代 PR；发现人工改动会阻断。重复触发会复用同一 PR；关闭的相同 PR 不会自动重开。

默认 `GITHUB_TOKEN` 创建的 PR 不会自动触发 `pull_request` 检查。因此准备流程明确触发可信主线的 `release-verify.yml`：独立核对 PR 的仓库、目标分支、精确 SHA，并从基线重新生成六个文件的预期补丁；之后运行完整测试、浏览器检查和 ZIP/wheel 安装升级 smoke。单独的报告作业才有 `checks:write`，在该精确 PR SHA 写入正常 `verify` 检查；执行候选的检查作业只有读取权限，不接收写入 token。合并使用 `--match-head-commit`，没有管理员绕过；必需人工批准、检查失败或冲突会阻断。

机器人合并后再次明确触发正式发布流程，避免 `GITHUB_TOKEN` 合并不触发 push 的限制。最终合并提交会重新构建、验证插件 ZIP 和 wheel，并在隔离状态目录实际启动 ZIP 服务、检查版本与状态保留；存在旧版正式 wheel 时，还实际安装旧 wheel 再升级新 wheel。首次发布没有旧 wheel 时明确记录只执行安装及重复安装检查，不将其称为跨版本升级。正式发布作业只使用同一次检查上传的资产，先创建固定标签和草稿、完整上传后转为正式 Release。已有标签指向其他提交或已有正式资产不一致时停止，绝不移动标签或替换正式资产。ZIP 时间与权限固定，wheel 设置 `SOURCE_DATE_EPOCH`，支持同提交重试对账。

smoke 可使用可选的 `GH_TOKEN` 读取本仓库固定的官方 latest Release API；工作流为这一步提供仅读权限的 token。该请求拒绝所有重定向，资产下载不携带 token；只有 API 的 404 表示暂无正式版本，403 限流及其他错误仍阻断验证。

GitHub 必须允许 Actions 创建 PR，并允许既定工作流申请对应仓库权限；禁用这些能力时流程会明确失败，不假设已配置额外 PAT。仓库管理员仍应维护必需检查与评审规则。标签和工作流文件应纳入规则保护。

## 受管项目的发布与部署授权

新项目默认只保留本地候选，不授予 PR、合并、发布或部署权限。本仓库维护授权也不传递给其他项目。项目面板先列出该项目 GitHub 上已存在、满足契约的工作流；用户选择并授权后，系统把默认分支当时的提交固定为 `rsi-workflow/<完整SHA>` 标签。此操作只保存所选发布或部署授权，不改变 PR 模式和自动合并开关。

`delivery.release` 与 `delivery.deploy` 都有 `enabled`、`workflow`、`ref`、`sha`。工作流路径固定为 `.github/workflows/*.yml` 或 `.yaml`；ref 必须是指向预先信任 commit 的轻量标签。高级用户可手动配置同样的固定目标。工作流必须使用以下契约，现成模板见 [rsi-delivery-workflow.example.yml](scripts/rsi-delivery-workflow.example.yml)：

- 顶层 `run-name: ${{ inputs.rsi_operation_id }}`。
- `workflow_dispatch.inputs` 中必须有 `rsi_operation_id` 和 `rsi_sha`，均为 `required: true`、`type: string`。
- 只发布或部署输入的完整 `rsi_sha`，目标端持久化 `rsi_operation_id`，对同一操作的重试保持幂等。
- 目标、命令和 secrets 由维护者事先配置在既定工作流及 GitHub environment；模型不能生成任意命令、选择 secrets 或自行授予发布权限。模板默认失败，配置真实目标后才可使用。

工作流文件必须事先位于该仓库默认分支；控制器不会将模型候选自动变为受信任的工作流。主线正常门禁及合并后验证通过后，控制器才使用封存授权触发它，传入实际合并 SHA。每次发出外部操作前都重查项目、仓库、工作流标签、暂停、取消和当前授权；撤销或切换目标会阻止尚未发出的动作。已在 GitHub 发出的工作流可能仍继续执行，需要维护者按 GitHub environment 规则处理。

操作状态、精确目标 SHA、工作流版本、稳定操作 ID、GitHub run ID/attempt、URL 和错误持久保存。服务重启后按稳定 ID 对账。超时不能证明请求未到达 GitHub，因此 `dispatch_unknown` 会继续查询，绝不盲目重复触发；无法对账时应检查原工作流，不能用“重试”强行创建第二次发布。确认失败的工作流通过同一个 run ID 的 GitHub rerun 重试；重复点击不会重复发出尚未确认的 rerun。

GitHub 明确以 4xx 拒绝 rerun（408 超时除外）时，恢复为失败并保留 HTTP 状态，可修正原因后再重试。网络中断、408 或 5xx 不能证明请求未被接受，系统只核对同一 run 的 attempt；五分钟仍无新 attempt 则进入 `rerun_unknown`，停止自动轮询并提示到 GitHub 核对。此时“对账”只读取结果，绝不重新发送不确定的请求；回退工作流同样适用。

## 部署健康与回退

没有 `delivery.deploy.health_url` 时，成功状态仅为 `workflow_succeeded`，健康证据为 `unverified`。用户可明确配置固定 HTTPS 健康地址，不接受 URL 凭据、query、fragment 或跨 origin 重定向。端点必须返回 HTTP 200 和 JSON `{"sha":"完整部署SHA","healthy":true}`（也接受 `status: "ok"`），只有精确版本匹配才记为 `healthy`。网络超时、无效响应或旧 SHA 都记为健康失败。

`delivery.deploy.automatic_rollback` 默认关闭，且开启时必须配置健康地址。系统只记住同一项目、仓库及健康目标上实际检查通过的 SHA。健康失败后，仅在这项授权仍有效且存在先前健康 SHA 时，才使用同一个固定部署工作流重新部署那个 SHA，并再次检查健康。回退工作流成功仍不足以宣布恢复，必须第二次健康检查通过才为 `rolled_back`；否则为 `rollback_failed`。没有历史健康版本时保留 `health_failed` 和 `rollback_unavailable`，不宣称已回退。

同一项目的同一部署目标按持久化所有者串行交付；较新的部署接管后，旧操作不会再次触发过期回退。健康失败可重试健康检查，回退工作流失败可重试原 GitHub run。这里的回退是受信任工作流重新部署已知 SHA，不是数据库回退保证；应用的数据迁移与兼容策略必须由该工作流的维护者实现。

## 用户端稳定更新

默认每日检查正式 GitHub Release 并提醒。`updates.mode` 可设为 `manual`、`notify`、`automatic`；自动模式还须选择 `updates.auto_policy`：`patch`（默认，仅同 major/minor）、`minor`（同 major）、`stable`（任何更高稳定版本）。草稿、预发布、同版本和降级均不安装。自动更新等待项目运行、模型测试和浏览器授权全部空闲；调用与 tokens 只记录，不设额度。

更新源固定为 `Haoog/Adaptive-RSI`。每个正式版本必须有 `adaptive-rsi-X.Y.Z-plugin.zip` 和同名 `.zip.sha256`；ZIP 根目录为 `adaptive-rsi/`。目前没有独立发布签名，校验和仍由同一 GitHub Release 提供。`update.json` 只接受 `format: 1`、`python_min: [3, 9]`、`state_schema: 1`；不兼容的数据迁移会被拒绝，不能仅改版本号绕过协议。

桌面版同批随正式版本提供 `adaptive-rsi-X.Y.Z-macos-arm64-adhoc.dmg` 和 `.dmg.sha256`。桌面包从同一经过验证的版本提交构建，并检查 Rust 桥接测试、引擎版本、应用签名完整性及 DMG 校验；目前只有 macOS ARM64 构建。临时 ad-hoc 签名不是 Apple Developer ID 签名或公证，macOS 可能显示安全警告，不能宣称为已公证安装包。桌面 app 需用户手动下载替换；服务器自动更新不替换桌面应用。

更新器使用独立版本目录和活动指针，重新读取官方 feed、校验 SHA-256 并检查归档路径。已经暂存且内容相同的版本可恢复继续；内容不同则停止。下载中断后可重试；自动失败采用退避并限制三次，后续可再次检查或手动重试。监督进程使用独占锁、持久化阶段和原始数据库备份；重启发现监督进程中断时，在禁止新任务及写入的状态下恢复计划。启动健康通过后才允许工作，启动失败仅在没有运行器占用数据库时恢复受保护的启动前快照。缺失备份、状态协议不兼容或磁盘损坏须人工处理；不能保证任意迁移、断电或已发生外部写入都能安全倒退。

旧 0.3.0 监督协议没有阶段日志或监督锁，因此先给予原监督器 90 秒完成窗口，不因缺少新协议的锁接管或终止候选；届时仍无法确认则保留当前数据库并转为人工恢复。已使用实际已安装的 0.3.0 监督代码，在临时状态目录向当前候选代码执行升级并连续轮询状态，验证正常交接和数据库保留；这不替代正式发布资产的验证。

Codex 已加载的插件元数据仍需刷新 marketplace 并重启 Codex。成功托管升级后，`STATE/installations/current` 是稳定目录入口；wheel 用户使用 pip 更新，或改用随插件提供的启动器。
