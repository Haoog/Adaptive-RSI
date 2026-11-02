# 清理预览真实核验留证（audit-200 #176，2026-11-01 数据）

**English** | [简体中文](cleanup-preview-evidence-20261101.md)

## 目的与边界

audit-200 台账 #176「远端临时分支提供只读清理预览」此前状态为 partial：fixture 测试已覆盖
`delivery.branch_cleanup_preview`（`adaptive_rsi/delivery.py:176`），但没有在授权仓库上跑过真实只读预览。
本轮补齐这一步。**全程只读**：预览本身只发两个 GET 请求；本轮额外用 `git ls-remote` 在预览前后对远端引用做
A/B 快照比对，未删除、未修改、未创建任何远端引用。

## 函数的只读设计（代码走读）

`branch_cleanup_preview` 通过 `release.api`（`adaptive_rsi/release.py:39`）只发起两个 GET：

- `repos/<repo>/branches?per_page=100`（分支清单首页）
- `repos/<repo>/pulls?state=all&per_page=100`（全状态 PR 清单首页）

函数不提供任何删除端点；返回值固定 `deletion_performed: false`，并返回 `candidates`、`protected`
（附保护原因）、`complete`（两页是否都不满 100 条）。清单不完整或归属未核实时一律归入 protected，不由调用方删除。

## 远端现状（核验时点 2026-11-01T20:45Z）

- 远端分支共 129 个（`git ls-remote --heads origin`）；分支 API 首页返回 100 条，页面不完整。
- PR（state=all）共 147 个；PR API 首页返回 100 条，页面不完整。
- 其中 open PR 17 个（#126、#132–#147）。
- 匹配受管模式 `feat|fix/rsi-*` 的分支：**0 个**。
- `main` 不在分支 API 首页 100 条之内（首页按字母序截断在 `chore/`、`fix/` 等前缀）。

## 真实预览运行方法与结果

运行方法（本轮使用的探针，只读包装）：

```sh
python3 -B probe_cleanup_preview.py   # 包装 release.api：仅放行 GET，记录每次调用，遇非 GET 直接抛错
```

两种参数各跑一次，结果一致：

| 参数 | complete | deletion_performed | candidates | protected |
| --- | --- | --- | --- | --- |
| `branch_cleanup_preview('Haoog/Adaptive-RSI')` | false | false | 0 | 100（全部 `not_managed`） |
| `branch_cleanup_preview('Haoog/Adaptive-RSI', recovery_refs=[])` | false | false | 0 | 100（全部 `not_managed`） |

每次运行恰好两次 API 调用（branches、pulls），全部为 GET，与 fixture 测试断言一致。
保护名单样本：`chore/batch-20261101-rate-limit-crash-matrix`、`chore/release-v0.4.0-bb1374a98664`、
`fix/health-check-https-only` 等，全部因「不是受管模式」受保护。

### 与本地 fixture 行为的一致性

- `test_full_cleanup_page_yields_no_candidates`（任一页面满 100 条 → `complete=false`、零候选）：真实运行
  中 branches 与 pulls 首页都返回 100 条（远端实际 129 分支 / 147 PR），结果同为 `complete=false`、零候选，一致。
- `test_cleanup_preview_protects_open_pr_recovery_and_unknown_branches`：真实远端没有匹配受管模式的分支，
  因此 `open_pr` / `protected_or_recovery` / `unverified_ownership` 路径本轮没有真实数据可触发；这与 fixture
  行为不矛盾——fixture 覆盖的分类逻辑在代码路径上与真实运行相同，只是本轮远端恰好没有落在这些分类里的分支。
- `main` 未出现在首页清单，即使按完整清单处理，也会因不匹配受管模式走 `not_managed` 保护，不会被列为候选。

## 只读证明

- 预览前 `git ls-remote --heads origin` 与预览后快照的 sha256 完全一致
  （`88de2296cd3e9c5918dcc45d3d697047f0230201129dabe6d5bc2d93c646dc2a`，129 行）。
- 探针包装层未观察到任何非 GET 请求；预览函数代码中不存在删除端点。

## 结论与状态建议

- 验收原文两条都已核验：预览不执行删除（固定返回 `deletion_performed: false`，且无删除端点）；被 open PR
  引用的分支得到保护（本轮远端无受管分支，open PR 保护路径由 fixture 覆盖，真实数据未触发该分类）。
- 建议台账 #176 从 partial 升级为 done：函数已实现并有离线契约测试，本轮补上真实只读预览证据。开放 PR、
  恢复清单与未知归属等保护分类的行为由 fixture 覆盖；真实远端当前没有会落入这些分类的受管分支。
