# 贡献指南

**简体中文** | [English](CONTRIBUTING.en.md)

欢迎通过 Issue 描述问题、提出改进，或通过 Pull Request 贡献代码。请先说明预期行为、实际行为及复现方式；界面问题可附截图。

## 本地开发

需要 Python 3.9+。支持 macOS Seatbelt 和 Linux Docker；模型可走 Codex、OpenCode CLI 或支持工具调用的原生 API。GitHub 导入、同步及确认后的交付使用已登录的 `gh`。日常测试通过替代模型接口运行，不需要付费调用。

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
node --check adaptive_rsi/web/model-center.js
node --check adaptive_rsi/web/auth.js
node --test tests/web_requests.cjs
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
python3 scripts/start.py
```

使用临时项目检查界面时，保持项目暂停，避免意外调用模型。状态目录应放在受管仓库之外。

`test:browser` 自动创建两个暂停的临时项目，使用真实 Chromium 操作表单、分页、项目切换和中英文手机布局，不调用模型。已安装 Chrome 时可设置 `RSI_BROWSER_CHANNEL=chrome`。开发用 Playwright 不属于运行器依赖。

本仓库已启用 GitHub 主分支保护：必须经 PR，`verify` 检查基于最新主线通过、所有讨论解决后才能合并，管理员同样受限。当前只有一位维护者，未强制第二位人员批准；这不等于已获得独立人工评审。

CI 会先区分文档改动和代码改动。纯文档改动检查本地链接、标题锚点及中英文配套文件；代码改动并行运行 Python 分组测试、浏览器、JavaScript、打包和 Linux 检查，`verify` 只负责汇总必需结果。同一 PR 推送新提交会取消旧提交尚未完成的运行。正式发版仍执行完整安装与桌面构建验证。

## 提交与文档

- 所有改动先新建工作分支，不得直接在 `main` 提交或推送。本仓库自身的自动改进也必须经过检查、代码评审和 PR 后才能合入。
- 分支按改动类型使用 `feat/`、`fix/`、`refactor/`、`docs/`、`chore/` 等前缀，不使用 `codex/`。
- 本仓库维护者已授权自动开发流程：先评估改动价值，通过后实施；相关测试与运行检查通过、代码评审无阻断问题后，可推送分支、创建 PR 并合并，无需逐次确认。GitHub 必需检查及评审仍须满足；失败、冲突或证据不足时停止合并。
- 这份维护授权仅适用于本仓库。产品默认保留本地候选；其他受管项目必须分别明确开启自动 PR 和合并，不继承本仓库维护授权。
- 这些约定不等于 GitHub 分支保护；仓库维护者还应在 GitHub 为 `main` 配置必须通过 PR、检查和评审的规则。
- 本仓库的提交说明使用中文，简洁描述行为变化，例如：`修复巡检任务重复入队的问题`。
- PR 说明写清解决的问题、行为变化、验证方式与已知限制。中文或英文均可。
- 修改用户功能时，同时更新中英文 README、相关说明及界面翻译。中文是默认界面语言；英文文案位于 `adaptive_rsi/web/i18n.js`。
- 用户目标、Issue 正文、模型输出、配置键及原始证据不随界面语言改写。添加界面文案时，分别检查静态页面和动态渲染。
- 为实际行为和风险添加必要验证。模型替代测试不能作为真实模型能力提高的证据。
- 模型评审安排在改动整理完成之后，优先审完整差异和受影响行为；小范围修复后重点复查原问题及相邻代码，核心调度、认证、升级、权限或数据边界变化重新完整评审。评审和测试都保留，模型判断不能替代固定检查。

## 合并后的本地分支恢复

合并 PR 时使用 `gh pr merge <编号> --squash --match-head-commit <已检查的完整SHA>`，本地分支清理另行处理。`--delete-branch` 同时清理本地和远端分支，可能切回已有的本地 `main` 并尝试更新它；旧本地分支与 squash 后的主线分叉时，这一步可能失败，但不代表远端 PR 合并失败。

先检查 PR 的 `state`、`mergeCommit`，再检查 `git status`、`git reflog` 和分支差异。执行 `git fetch origin main` 更新远端跟踪引用，并用 `git merge-base --is-ancestor <mergeCommit> origin/main` 确认最新主线已包含该合并；退出码非零时先核对仓库、目标分支与远端状态，不能用旧引用重建本地分支。保留原有提交和未提交文件；不能据此重复合并、把已经 squash 的旧提交再次 cherry-pick，或强制重置本地分支。只有 Git 确认存在文件合并冲突时才按冲突处理。

本地 `main` 干净但已分叉时，可将它重命名为唯一的 `archive/local-main-<日期>` 备份，再从最新 `origin/main` 创建新的本地 `main`；有未提交文件时先原样保留，并在独立工作树继续。切换到旧分支后才显示的 `node_modules/` 或构建目录，应先比较 `.gitignore` 和文件内容；忽略规则的变化本身不表示用户新增了文件。已知命令副作用且可无损恢复时，在现有授权内处理并核验，不将其误报为必须再次确认的阻断。

## 安全与评估边界

不要提交认证信息、会话链接、本地状态、数据库、浏览器日志或私有项目内容。报告安全问题时，不要把可用凭据或私人数据写入公开 Issue。

修改调度、隔离、预算、验收或策略晋升逻辑时，请说明对现有边界的影响。不能以模型自报成功替代固定检查，不能让候选修改受保护评估器，也不能用缺少独立评估的结果宣称递归能力提升。

发布插件与维护更新资产见 [RELEASING.md](RELEASING.md)。自动 PR 与合并只适用于明确授权的项目，并须通过完整交付门禁。

提交代码即表示同意按仓库的 MIT 许可证提供该贡献。
