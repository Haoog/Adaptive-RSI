# 发布与用户更新

**简体中文** | [English](RELEASING.en.md)

发布由维护者明确执行；构建插件不会推送 Git、创建 PR 或发布 Release。

## 发布包契约

- 同步 `plugin.json`、`pyproject.toml`、`adaptive_rsi/__init__.py` 和 `update.json` 的版本号。
- 正式版本使用 `major.minor.patch`，对应标签 `vX.Y.Z`。更新器跳过草稿和预发布。
- `update.json` 当前只接受 `format: 1`、`python_min: [3, 9]`、`state_schema: 1`。不兼容的数据库变更需要先设计迁移/回退方案并更新协议，不能只改版本号。
- 每个 Release 必须包含 `adaptive-rsi-X.Y.Z-plugin.zip` 以及同名 `.zip.sha256`。ZIP 内根目录必须为 `adaptive-rsi/`。
- 更新源固定为 `Haoog/Adaptive-RSI` 的最新正式 GitHub Release。尚未提供独立发布签名。

## 本地准备与验证

```sh
python3 -m unittest discover -v
node --check adaptive_rsi/web/app.js
node --check adaptive_rsi/web/i18n.js
python3 scripts/package.py
uv build --wheel --out-dir dist
```

更新中英文文档、验证记录和版本说明。检查 ZIP 不包含认证、会话、本地状态、构建缓存或项目数据。在隔离状态目录演练升级、健康检查及新版本启动失败的数据库恢复。

由维护者在 GitHub 创建草稿 Release，上传这两个对应资产，确认版本、校验和、说明及验证范围后再发布。仓库本身推送到 main 不会通知用户更新；必须有符合上述契约的正式 Release。本项目不自动执行此发布步骤。

## 用户端行为与边界

默认每日检查并提醒。选择自动模式后，仅空闲时自动安装相同主次版本的补丁；例如 `0.2.0 → 0.2.1`。`0.2.x → 0.3.x` 需要用户确认，任务处理预算不会自动调整。

运行器采用版本目录和活动指针，不在用户源码仓库执行 `git pull`。启动失败恢复旧代码和数据库；断电/监督进程崩溃恢复尚未验证。服务启动环境中的模型密钥继承到新服务，不会写进更新计划。

已加载的 Codex 技能/插件元数据需要刷新 marketplace 并重启 Codex；运行器更新不能热替换它们。首次托管升级成功后，`STATE/installations/current` 可作为本地 marketplace 的稳定目录入口。wheel 用户需更新 pip 包，或改用随插件提供、会跟随活动指针的启动脚本。
