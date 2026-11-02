# GitHub ruleset 只读核验证据 / 2026-11-01

**简体中文** | [English](ruleset-verification-evidence-20261101.en.md)

本轮针对审计台账 #162「准备度检查兼容 GitHub rulesets」做一次真实只读核验：用与 `adaptive_rsi.delivery.merge_protection` 相同的 `gh api` 命令读取分支保护与规则端点，把响应原文留成测试夹具，并在真实响应上重放判定代码。整轮没有任何写操作，也没有修改判定代码。汇总结果见[证据 JSON](ruleset-verification-evidence-20261101.json)，响应原文位于 [tests/fixtures/github_protection](../../tests/fixtures/github_protection)。

## 方法

- 抓取时间 2026-11-01T20:32:26Z；账号 `Haoog`；token scope 为 `gist`、`read:org`、`repo`。仓库管理权限来自账户对所有者的身份，不使用额外的 admin scope。
- 只调用三个只读端点，均与产品代码一致：

```sh
gh api repos/<repository>
gh api repos/<repository>/branches/<branch>/protection
gh api repos/<repository>/rules/branches/<branch> --paginate --slurp
gh api repos/<repository>/rulesets
```

- 每个响应连同仓库、分支、端点、抓取时间和只读标记存成一个 JSON 夹具。测试用 `ProcessError` 重放失败响应的原始 `exit_code`、`stdout`、`stderr`，因此失败分支也来自真实命令输出，而不是手写错误字符串。
- `vercel/next.js` 与 `microsoft/TypeScript` 是公开参考仓库，只用于补齐本仓库不存在的 ruleset 响应形态；它们不是受管项目，也不参与任何交付判定。

## 三种观察

| 仓库 / 分支 | 配置 | `protection` 读取 | `rules` 读取 | 判定输出 | 来源 |
| --- | --- | --- | --- | --- | --- |
| `Haoog/Adaptive-RSI` / `main` | 仅 classic 保护 | observed：`strict=true`，必需检查 `verify`，`enforce_admins=true`，必需批准 0，要求解决讨论 | observed：0 条规则；仓库 rulesets 索引为空 | `state=strict`，`strict_checks=true`，`merge_queue=false`，`complete=true` | 实时 |
| `Haoog/Ours` / `master` | 未配置保护 | 失败：`Branch not protected`（HTTP 404） | observed：0 条规则 | `state=unknown`，`strict_checks=null`，`merge_queue=false`，`complete=false` | 实时 |
| `Haoog/adaptive-rsi-validation-20261020` / `main` | 私有仓库且计划受限 | 失败：`Upgrade to GitHub Pro or make this repository public to enable this feature.`（HTTP 403） | 失败：同一 403 | `state=unknown`，`strict_checks=null`，`merge_queue=null`，`complete=false` | 实时 |
| `vercel/next.js` / `canary` | 仅 ruleset | 失败：HTTP 404 | observed：17 条规则，含 `required_status_checks`，其 `strict_required_status_checks_policy=false` | `state=unknown`，`strict_checks=null`，`merge_queue=false`，`complete=false` | 实时（公开参考仓库） |
| `microsoft/TypeScript` / `main` | 仅 ruleset，含合并队列 | 失败：HTTP 404 | observed：6 条规则，含 `merge_queue` 与 `required_status_checks` | `state=unknown`，`strict_checks=null`，`merge_queue=true`，`complete=false` | 实时（公开参考仓库） |
| 回放：本仓库 classic + 参考 ruleset | classic 加 ruleset 组合 | observed | observed | `state=strict`，`strict_checks=true`，`merge_queue=true`，`complete=true` | 回放夹具，非实时 |

三点结论：

1. 两种保护形态都覆盖到了：classic-only 在受管仓库上实时观察到严格状态检查；ruleset-only 在参考仓库上实时观察到最后生效规则，其中 `required_status_checks` 不含严格策略。
2. 读取失败不会变成"可合并"：404 与 403 都只让对应来源保持 `unknown`，`complete=false`；只要有一侧未读到，判定就不声称完整。ruleset 存在但缺少 classic 读取时，`strict_checks` 仍是 `null`。
3. 组合配置（classic 加 ruleset）在受管仓库上不存在：仓库 rulesets 索引为空，而新建 ruleset 属于写操作，超出本轮只读边界。因此该行由两次真实抓取拼成的夹具回放得出，`merge_queue=true` 来自 `microsoft/TypeScript` 的真实响应。

## 判定代码

`adaptive_rsi/delivery.py` 的 `merge_protection` 未改动。真实响应没有暴露缺口：`sources`、`complete`、`state` 与端点实际返回一致；失败捕获后来源保持 `unknown`，与台账验收要求「权限不足时显示未知而不假称可合并」相符。现有 UI 文案也只陈述"已观察到严格状态检查，GitHub 分支保护仍决定是否允许合并"或"当前权限可能无法读取全部保护规则"。

## 残余边界

- 受管仓库只有 classic 保护，rulesets 为空；classic+ruleset 组合仍只由回放夹具覆盖，不是真实接受。
- 未实时观察到 active ruleset 内 `strict_required_status_checks_policy=true` 的样本，该分支继续由离线回归 `test_active_rules_endpoint_can_establish_strictness_without_classic_protection` 覆盖。
- 403/404 的具体文案取决于账户计划与仓库可见性；夹具是时点快照，不证明 GitHub 之后仍执行相同策略，也不证明真实合并是否被放行。
- 本轮只验证保护规则的读取与判定，不涉及评审、检查结果或合并队列的实际执行。

## 验证

```sh
python3 -B -m unittest tests.test_delivery_readiness_audit -q
python3 -B scripts/check_audit_ledger.py
```

本轮执行结果：`tests.test_delivery_readiness_audit` 34 项通过，`ProtectionRulesTest` 8 项通过；台账与重放索引检查通过。按仓库约定未在本机跑全量测试，全量交由 PR 的 CI 执行。
