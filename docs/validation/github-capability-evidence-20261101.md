# GitHub 能力诊断真实核验留证（audit-200 #178，2026-11-01 数据）

**English** | [简体中文](github-capability-evidence-20261101.md)

## 目的与边界

audit-200 台账 #178「区分 GitHub 读取与写入权限的准备度」此前状态为 partial：fixture 测试已覆盖
`readiness.github_capabilities`（`adaptive_rsi/readiness.py:14`）的读写区分与 unknown 兜底，但没有用真实
token 跑过一次能力诊断。本轮补齐这一步。**全程只读**：诊断本身只发一个 GET 请求；本轮额外用
`git ls-remote` 在诊断前后对远端引用做 A/B 快照比对，未创建、未修改、未删除任何远端引用，也未新增任何凭据。

本轮一个重要的事实边界必须如实记录：**本机现有 token 不是纯只读令牌**。`gh auth status` 显示其 scope 为
`gist`、`read:org`、`repo`（classic token），其中 `repo` scope 包含 push 权限（本循环日常就用它推分支开 PR）。
因此本轮无法在真实远端上复现 `write: unavailable` 的只读分支——该分支由 fixture
`test_read_only_repository_permissions_never_imply_write` 覆盖；本轮没有为复现它而创建新的受限凭据。

## 函数的只读设计（代码走读）

`github_capabilities`（`adaptive_rsi/readiness.py:14`）只发起一个请求：

- `gh api repos/<repo> --method GET`（经 `run_process`，`adaptive_rsi/process.py:112`）

函数不提供任何写端点：结果固定携带 `test_write_performed: false`；`pull_requests_write`、`actions_write`
固定为 `unknown`（`adaptive_rsi/readiness.py:33-34` 明确注释：仓库 push 权限不能证明细粒度 token scope、
Actions dispatch 权限或修改 workflow 文件的权限）。诊断在整装 `inspect` 流程（readiness.py:217-222）中也
只读调用本函数，不执行测试写入。

## 真实诊断运行方法与结果

运行方法（本轮使用的探针，只读包装）：

```sh
python3 -B probe_capability_178.py   # 包装 subprocess.Popen：记录诊断期间派生的每个进程，遇写参数在检查中暴露；不 mock、不拦截真实调用
```

两次运行（20:50:35Z、20:51:23Z）结果一致。诊断期间派生的全部进程恰好一条：

```text
gh api repos/Haoog/Adaptive-RSI --method GET
```

`github_capabilities('Haoog/Adaptive-RSI')` 返回：

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `read` | `available` | 仓库可读 |
| `write` | `available` | 服务端 `permissions.push=true`，与 classic `repo` scope 一致（真实正报，非误报） |
| `actions_write` | `unknown` | 未声称 |
| `pull_requests_write` | `unknown` | 未声称 |
| `test_write_performed` | `false` | 全程零测试写入 |

### 服务端交叉印证

另发一次独立只读 GET（`gh api repos/Haoog/Adaptive-RSI --jq .permissions`，20:50Z）：
`{"admin":true,"maintain":true,"pull":true,"push":true,"triage":true}`。`push=true` 证实诊断的
`write: available` 与服务端授权一致，是真实正报。

## 验收两条的核验结论

1. **「只读令牌不会被误报为所有交付可用」**——已核验：
   即使 push 权限真实可用，诊断仍将 `pull_requests_write`、`actions_write` 保持为 `unknown`，
   「所有交付可用」从未被声称（fixture 覆盖 `write: unavailable` 的只读分支）。
2. **「诊断不执行测试写入」**——已核验：Popen 包装层记录到诊断恰好派生一条进程，argv 为
   `gh api repos/<repo> --method GET`，无 `-f`/`-F`/`--input`/`-X`/POST/PUT/PATCH/DELETE 任一写标志；
   远端引用快照 sha256 前后完全一致（`fc10b687…473727`，335 行）。

## 只读证明

- 诊断前后 `git ls-remote` 快照 sha256 完全一致
  （`fc10b687a6ab18a9608cfb2f48d5ef01d69447c783316b9908b9637719473727`，335 行）。
- Popen 包装层未观察到任何非 GET 请求；诊断函数代码中不存在写端点。
- token 值从未被读取或记录；探针只引用 `gh auth status` 输出的 scope 名称。

## 结论与状态建议

- 验收原文两条都已核验：诊断不执行测试写入（固定 `test_write_performed: false`、无写端点、真实运行零写请求）；
  即使在 push 可用的令牌上，「所有交付可用」也不会被误报（细粒度能力保持 `unknown`）。只读令牌
  `write: unavailable` 的分支由 fixture 覆盖，本轮未创建新凭据去真实复现。
- 建议台账 #178 从 partial 升级为 done：函数已实现并有离线契约测试，本轮补上真实受限 token 的
  诊断证据；残余边界（无纯只读令牌的真实 `unavailable` 分支）如实保留在证据 JSON 的
  `boundary_notes` 中。
