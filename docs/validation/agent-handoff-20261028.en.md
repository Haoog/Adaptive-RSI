# Agent handoff: audit implementation and remaining scope

**Current authorization supersedes this historical handoff:** the user has authorized continued implementation, verification, push, merge, release and installation update, subject to final gates. The stop instruction and counts below describe the old handoff, not the current working tree. Current ledger: **114 implemented, 56 partial, 31 pending**; `release_ready=false`. See the [current follow-up](audit-followup-20261028.en.md) and [ledger](audit-200-implementation-20261028.json). Historical text is retained unchanged below.

[中文](agent-handoff-20261028.md)

The user's latest instruction is to finish the current ledger/index task, summarize the work and stop. Do not resume implementation, push, merge, release or update the installed application without a new user instruction. Earlier release authorization has been narrowed by this instruction.

At the request there were **94 implemented, 24 partial and 83 pending** items. Only the current evidence-index item **#200** was finished afterward: the final ledger is **95 implemented, 24 partial and 82 pending**, across 201 items (200 audit entries plus validation-throughput #201). The original 83-item list is preserved below; #200 is marked as completed during handoff. Partial does not mean complete. `release_ready=false`.

## Checkout and authoritative records

- Checkout: `/Users/neg-entropy/Desktop/Adaptive RSI`.
- Branch: `fix/audit-200-and-faster-validation`.
- Base: `75aecba5ef63bd719eb025b74d5c3c409d3ecf71` (v0.5.124 / PR #87).
- Last implementation commit before handoff: `71b6a6a`. This handoff and index closure are saved in a later local commit; use `git log -1` for its SHA.
- All branch changes are local. This work performed no push, PR creation, merge, release or installed-app update. Existing v0.5.124 assets must not be overwritten. Any future release requires a new version.
- Preserve the pre-existing untracked `docs/research/`; do not stage, edit or delete it.
- The updater signing key exists outside the repository; do not read or include it. No paid/real model calls were made for these validations.

[JSON ledger](audit-200-implementation-20261028.json) is authoritative for status, acceptance, implementation, validation and residuals. [Full replay index](audit-200-evidence-20261028.en.md) covers all 201 entries. [Progress](audit-200-progress-20261028.en.md) contains the implementation narrative. Original audit input remains at `/Users/neg-entropy/Desktop/Adaptive-RSI-audit-20261028/entries.tsv`.

## What is implemented

The complete list of 95 completed entries and their code/replay references is in the index. Major areas include native-provider diagnostics and interruption evidence; task lifecycle/revisions/groups and dependency DAGs; provider-health recovery; database migration/history and observability; background-operation receipts; archive/backup recovery; desktop updater staging and process-identity admission; deployment observation and rollback policy; resource priority and pressure admission; navigation, mobile diff, localization and structured errors.

Validation throughput work separates Python, browser and desktop release jobs, records selector durations, balances workers using valid historical timing, falls back safely on invalid caches, and gives ordinary CI a strictly reconstructed metadata-only path for mechanical version PRs. Exact-commit full release gates remain required. Remote speedup and release-queue behavior have not been proven (#201 remains partial).

Updater work is not a completed production end-to-end upgrade proof: patch-version default policy, selectable version/install windows, disk staging, feed recheck, signature verification and cancellation/admission behavior have local coverage. Partial downloads, real launch-failure recovery, rollout policy and real signed upgrade evidence remain in the outstanding list. IPC stubs do not prove real app replacement or that other users have already received an update.

Recent commits added configuration revision history and guarded undo; consecutive multi-endpoint deployment health; explicit data-compatibility policy for automatic rollback; priority/FIFO resource admission; optional disk/memory admission; per-file mobile diff rendering; fixed-data history benchmarks; AST-based localization checks; causal blocker explanations; structured bilingual errors and request IDs; navigation/history race fixes.

## Validation: passed evidence and limits

- Earlier checkpoint `de75578`: 842 Python tests across four workers passed. This is historical checkpoint evidence, not final-head evidence.
- A later full Python run completed all four groups, with three assertions failing in one server-hardening mock because `source` kwargs were not accepted. Commit `77d4892` fixed the mock, and that 16-test module passed. No latest-head full Python rerun has been claimed; subsequent work has targeted validation.
- Latest complete browser run: 33 groups, 32 passed; delivery-feedback failed because a generic localized reason hid specific SHA guidance. That priority was fixed in `71b6a6a`; the failed browser group passed on rerun. Do not call the original complete run green.
- Latest Node suite: 351 passed. Latest recorded Rust suite: 13 passed, before this handoff (no Rust changes in the closure).
- Local 50,000-task / 100,000-run benchmark: task-page p95 113.92 ms, workflow metrics 435.20 ms, quality 164.61 ms; synthetic local data, not production or remote CI performance. [Report](history-performance-20261028.json).
- Wheel built and installed into a temporary venv outside the checkout; isolated `-I` HTTP smoke returned 200 / API v2, loaded schema resources and migrations 1–6, with zero model calls. [Report](wheel-install-20261028.json). Its 0.5.124 version is a local smoke artifact only, never uploaded.
- Handoff closure: four ledger tests and seven workflow-configuration tests passed; CI selector coverage reports 975 tests; documentation links/pairs and ledger generation checks passed. These checks do not execute all 975 tests.
- Dual read-only review found a discovery-side-effect documentation issue: unittest selector validation imports trusted test modules and may run `load_tests` hooks. Documentation now discloses this; acceptance commands and test cases are not executed by the ledger checker. Other review had no confirmed defect. Both reviewed relative to `71b6a6a`; this does not cover the whole branch as a final release review.
- No final exact-head remote CI, signed app replacement, notarization proof, release or local installation update was performed. `implementation_commit=null`, `ci_evidence=[]` intentionally remain.

Local logs in `/tmp` may be ephemeral: `/tmp/rsi-audit-current-full-python.log`, `/tmp/rsi-audit-current-full-browser.log`, `/tmp/rsi-navigation-delivery-feedback.log`, `/tmp/rsi-navigation-node.log`, `/tmp/rsi-wheel-install.log`. Repo JSON artifacts and ledger commands are the durable entry points.

## Original 83 pending entries

#200 alone was finished while closing the current task; the other 82 remain pending. Acceptance criteria below preserve the original Chinese audit text verbatim so requirements are not silently rewritten.

### #34 Native update recovery needs startup-failure evidence

Original acceptance: 用真实签名包注入启动和迁移失败；验证应用及状态数据的一致回退。

Source: `desktop/src-tauri/src/native_update.rs`

### #48 Monitoring stops after its initial window

Original acceptance: 模拟窗口后再出现回归；选择性长期哨兵应发现并解释变化。

Source: `adaptive_rsi/evolution.py`

### #62 Add pausable staged rollout

Original acceptance: 模拟暂停和恢复，已获授权的版本范围及签名校验始终有效。

Source: `desktop/src-tauri/src/native_update.rs`

### #63 Respect network and power preferences

Original acceptance: 切换网络和电源状态，任务执行不受影响，用户可随时手动覆盖。

Source: `desktop/src-tauri/src/native_update.rs`

### #66 Exercise updater key rotation

Original acceptance: 用测试密钥演练跨两版本轮换，未经授权的签名仍被拒绝。

Source: `desktop/src-tauri/src/native_update.rs`

### #67 Explain update compatibility requirements

Original acceptance: 以不兼容测试资产验证提前解释原因，当前安装保持可用。

Source: `desktop/src-tauri/src/native_update.rs`

### #68 Publish a tested platform asset matrix

Original acceptance: 每个宣称支持的组合都有构建、安装及更新证据，未支持组合明确提示。

Source: `scripts/package_desktop.py`

### #69 Offer download bandwidth limits

Original acceptance: 限速开启后吞吐符合配置，取消及时，签名校验不变。

Source: `desktop/src-tauri/src/native_update.rs`

### #70 Explain update progress by stage

Original acceptance: 未知总大小不显示虚假百分比；慢速及校验阶段均有准确状态。

Source: `adaptive_rsi/web/native-update.js`

### #71 Estimate space from actual assets

Original acceptance: 小包和大包的空间边界测试均通过，未知体积时保守拒绝并说明依据。

Source: `desktop/src-tauri/src/native_update.rs`

### #72 Make desktop lifecycle preferences explicit

Original acceptance: 逐一验证关窗、托盘退出及系统重启，不误停非自有服务。

Source: `desktop/src-tauri/src/main.rs`

### #73 Offer opt-in crash diagnostics

Original acceptance: 导出前可预览内容，凭据和用户源码不进入默认诊断包。

Source: `adaptive_rsi/storage_tools.py`

### #74 Scope navigation preferences to runtime instances

Original acceptance: 切换两个含不同项目的服务，分别恢复各自的有效视图。

Source: `adaptive_rsi/web/desktop.js`

### #75 Add native desktop commands

Original acceptance: 在 macOS 菜单和键盘上完成常用流程，快捷键不干扰文本输入。

Source: `desktop/src-tauri/src/main.rs`

### #76 Diagnose proxy and certificate configuration

Original acceptance: 测试代理认证失败和自建 CA 环境，诊断不输出代理密码或令牌。

Source: `desktop/src-tauri/src/main.rs`

### #79 Export upgrade receipts

Original acceptance: 成功、取消及恢复均有明确记录，回执不含用户认证信息。

Source: `desktop/src-tauri/src/native_update.rs`

### #80 Negotiate feature capabilities

Original acceptance: 新客户端接旧服务时可用功能继续工作，不支持操作有明确提示。

Source: `adaptive_rsi/contracts.py`

### #83 Map grouped acceptance to individual evidence

Original acceptance: 一个子要求失败时组不能宣称全部通过，每条要求可导航到实际证据。

Source: `adaptive_rsi/batching.py`

### #84 Size task groups by verification cost

Original acceptance: 用实际 CI 数据比较分组策略，用户确认拆分且每个组仍通过完整门禁。

Source: `adaptive_rsi/batching.py`

### #85 Preview grouping benefits and rework costs

Original acceptance: 估计与实际执行可对比，证据不足时标记未知而非保证加速。

Source: `adaptive_rsi/web/batch.js`

### #86 Recommend task groups without automatic mutation

Original acceptance: 推荐不产生任务变更；确认后执行范围与预览完全一致。

Source: `adaptive_rsi/batching.py`

### #89 Estimate queue time from actual execution

Original acceptance: 历史回放报告区间覆盖率及误差，冷启动或样本不足明确说明。

Source: `adaptive_rsi/telemetry.py`

### #93 Consider deadline-driven scheduling

Original acceptance: 百千项目负载对比 CPU、查询数和到期误差，不降低取消与配置响应。

Source: `adaptive_rsi/runtime.py`

### #94 Explain graceful shutdown progress

Original acceptance: 注入慢子进程和维护任务，关闭状态始终可解释且最终有明确结果。

Source: `adaptive_rsi/runtime.py`

### #95 Use stage-specific deadlines

Original acceptance: 慢模型、慢准备和慢测试分别命中对应时限，取消保持及时。

Source: `adaptive_rsi/config.py`

### #96 Reuse sealed read-only checkpoints

Original acceptance: 任一输入变化都失效；写工作区或远端操作不因检查点自动重放。

Source: `adaptive_rsi/evaluator.py`

### #98 Clarify timezone-aware task scheduling

Original acceptance: 夏令时跳过及重复小时测试给出确定行为，时区变化不静默移动授权时间。

Source: `adaptive_rsi/runtime.py`

### #101 Stabilize pagination under mutation

Original acceptance: 翻页同时插入及更新任务，不漏读既定快照记录，并提示快照过期。

Source: `adaptive_rsi/server.py`

### #108 Expand relational constraint coverage

Original acceptance: 导入既有孤儿数据时先审计隔离，合法历史升级后不丢失。

Source: `adaptive_rsi/store.py`

### #113 Type and version settings namespaces

Original acceptance: 损坏单一 namespace 时其他功能仍可用，迁移和保留规则可检查。

Source: `adaptive_rsi/store.py`

### #118 Offer previewable reversible cleanup

Original acceptance: 预览与实际对象一致，活动及法律保留对象不清理，撤销可验证。

Source: `adaptive_rsi/maintenance.py`

### #120 Rehearse business-level restoration

Original acceptance: 恢复演练不派发真实远端操作，发现路径及对象关系问题并生成报告。

Source: `adaptive_rsi/storage_tools.py`

### #121 Version capability metadata with provenance

Original acceptance: 未知模型保持保守，错误目录可回退，界面显示元数据来源及时间。

Source: `adaptive_rsi/agent.py`

### #122 Budget and paginate tool results independently

Original acceptance: 各工具在边界处返回可继续读取的位置和准确截断原因。

Source: `adaptive_rsi/native.py`

### #123 Estimate tokens with language-aware uncertainty

Original acceptance: 多语言样本与真实计数比较误差，未知 tokenizer 时不宣称精确。

Source: `adaptive_rsi/native.py`

### #127 Respect ignore rules during indexing

Original acceptance: 嵌套忽略、显式包含和未跟踪源码规则可预测，结果仍说明部分性。

Source: `adaptive_rsi/native.py`

### #128 Add scoped search options

Original acceptance: 对无效正则、超长模式和大结果集保持有界执行及部分结果说明。

Source: `adaptive_rsi/native.py`

### #129 Reference large tool output as artifacts

Original acceptance: 超限输出不会进入模型上下文，保留产物仍按容量和敏感信息规则管理。

Source: `adaptive_rsi/native.py`

### #130 Classify retry safety for paid requests

Original acceptance: 分别注入连接失败、响应丢失和 429，回执准确说明是否可能产生费用。

Source: `adaptive_rsi/native.py`

### #132 Add native streaming progress

Original acceptance: 碎片化数据、取消及中断均有契约测试，不提前执行未完整解析的工具。

Source: `adaptive_rsi/native.py`

### #135 Expand cross-protocol response contracts

Original acceptance: 每个协议运行相同语义样本，异常不丢已完成工作也不重复执行工具。

Source: `adaptive_rsi/native.py`

### #136 Explain fallback fault domains

Original acceptance: 模拟共享账号故障，用户能理解跳过路由及最终失败原因。

Source: `adaptive_rsi/agent.py`

### #137 Price input, output and cache usage separately

Original acceptance: 用公开固定样例核对账单公式，历史调用使用当时价表而非当前价格。

Source: `adaptive_rsi/usage_report.py`

### #138 Explain currency and estimation limits

Original acceptance: 混合 API 和订阅调用时总计准确标记未知部分，用户可导出依据。

Source: `adaptive_rsi/usage_report.py`

### #139 Compare estimated and actual task cost

Original acceptance: 样本不足时标未知，不新增强制额度限制；历史预测可与实际核对。

Source: `adaptive_rsi/telemetry.py`

### #143 Use fresh final promotion evidence

Original acceptance: 复核集未参与提案或排序，使用记录可查，样本不足时不宣称提升。

Source: `adaptive_rsi/evolution.py`

### #144 Calibrate monitoring with optional human feedback

Original acceptance: 标签有来源且不直接篡改封存测试结果，展示样本量及偏差。

Source: `adaptive_rsi/evolution.py`

### #146 Measure benchmark noise and uncertainty

Original acceptance: 同一改动跨负载条件复测，结论能解释噪声且不把一次波动当提升。

Source: `adaptive_rsi/evaluator.py`

### #147 Validate harness sensitivity with negative controls

Original acceptance: 负例必须被现有门禁阻止；不以 mutation 分数替代真实需求验收。

Source: `adaptive_rsi/evaluator.py`

### #148 Exercise criterion-specific counterexamples

Original acceptance: 关键要求的正例通过、反例失败，来源和执行结果可以追溯。

Source: `adaptive_rsi/evaluator.py`

### #149 Record executed test identities

Original acceptance: 只执行无关测试的样例不能冒充目标回归验证，跳过项可查。

Source: `adaptive_rsi/evaluator.py`

### #150 Collect standard test and coverage artifacts

Original acceptance: 产物缺失有明确状态，路径及大小受限，不从任意宿主路径采集。

Source: `adaptive_rsi/evaluator.py`

### #151 Cache immutable prepared environments

Original acceptance: 工具、锁文件、种子或配置变化自动失效，跨任务无可写污染。

Source: `adaptive_rsi/environment.py`

### #152 Gate performance gains on secondary guardrails

Original acceptance: 构造主指标通过但内存或错误率退化的样例，结果按预先封存的规则阻止晋升。

Source: `adaptive_rsi/workflow.py`

### #153 Audit dataset retirement and reclassification

Original acceptance: 历史实验仍可复核，已曝光样本不能通过改名变成新 holdout。

Source: `adaptive_rsi/evolution.py`

### #154 Explain strategy changes before promotion

Original acceptance: 用户能从界面追溯每个行为变更到封存实验，未覆盖场景明确说明。

Source: `adaptive_rsi/evolution.py`

### #156 Export sealed experiment reproduction bundles

Original acceptance: 在隔离机器验证输入完整性，缺凭据或外部依赖时明确列出限制。

Source: `adaptive_rsi/evolution.py`

### #157 Record exact host tool versions

Original acceptance: 更换关键工具时环境身份或证据变化，报告可定位具体版本。

Source: `adaptive_rsi/evaluator.py`

### #158 Model monorepo check dependencies

Original acceptance: 跨包依赖变更触发正确检查，未知影响回退完整集合。

Source: `adaptive_rsi/evaluator.py`

### #159 Separate strategy and skill attribution

Original acceptance: 同输入下比较各配置，报告不把组合收益全部归给单一模块。

Source: `adaptive_rsi/evolution.py`

### #161 Explain merge queue states

Original acceptance: 在启用队列的测试仓库验证入队、重排、失败和最终合并状态。

Source: `adaptive_rsi/delivery.py`

### #162 Inspect GitHub ruleset requirements

Original acceptance: 分别在两种保护配置测试，权限不足时显示未知而不假称可合并。

Source: `adaptive_rsi/delivery.py`

### #163 Share trustworthy CI setup work

Original acceptance: 比较冷暖缓存耗时，缓存投毒和过期输入不能绕过最终验证。

Source: `.github/workflows/release-verify.yml`

### #164 Combine affected checks with full final gates

Original acceptance: 注入跨模块回归仍在最终门禁被发现，时间收益用真实运行数据确认。

Source: `.github/workflows/checks.yml`

### #165 Attribute post-merge failures carefully

Original acceptance: 候选通过但后续提交破坏的样例不被直接记为候选回归。

Source: `adaptive_rsi/delivery.py`

### #166 Separate tracking cancellation from remote cancellation

Original acceptance: 两种取消分别验证，远端已完成或结果未知时不报告虚假的取消成功。

Source: `adaptive_rsi/delivery.py`

### #167 Coordinate GitHub rate-limit handling

Original acceptance: 注入 403 次级限流、429 及未知写响应，不产生重复 PR 或 dispatch。

Source: `adaptive_rsi/release.py`

### #168 Explain rewritten or diverged baselines

Original acceptance: 模拟 force-push 与分叉，旧证据失效且不会自动覆盖上游。

Source: `adaptive_rsi/delivery.py`

### #169 Persist retryable post-processing work

Original acceptance: 在每个后处理步骤前后崩溃，重启后恰当恢复且无重复副作用。

Source: `adaptive_rsi/runtime.py`

### #170 Offer optional authenticated event delivery

Original acceptance: 乱序、重复和漏事件测试后状态正确，不要求普通本机用户部署额外服务。

Source: `adaptive_rsi/delivery.py`

### #174 Bind target environments to authorization

Original acceptance: 修改目标环境后旧授权失效，回执显示实际部署目标及输入 hash。

Source: `adaptive_rsi/release.py`

### #175 Break down remote verification time

Original acceptance: 指标与 GitHub 时间戳可核对，缺失阶段显示未知。

Source: `adaptive_rsi/telemetry.py`

### #176 Preview stale remote branch cleanup

Original acceptance: 预览不执行删除，仍被 PR 或恢复点引用的分支得到保护。

Source: `adaptive_rsi/delivery.py`

### #177 Explain synchronized requirement changes

Original acceptance: 保留原文和来源时间，未获授权的评论不改变任务权限。

Source: `adaptive_rsi/runtime.py`

### #178 Check GitHub capabilities without test writes

Original acceptance: 只读令牌不会被误报为所有交付可用，诊断不执行测试写入。

Source: `adaptive_rsi/readiness.py`

### #179 Expose ambiguous dispatch reconciliation

Original acceptance: 延迟出现及多个匹配运行时不盲目重发，人工结论留审计记录。

Source: `adaptive_rsi/release.py`

### #180 Plan explicit GitHub Enterprise support

Original acceptance: 在测试 Enterprise 主机完成读写流程，公共站点令牌不发送到其他主机。

Source: `adaptive_rsi/delivery.py`

### #181 Add navigation within long logs

Original acceptance: 大日志下查找响应有界，命中上下文和原始内容一致。

Source: `adaptive_rsi/web/app.js`

### #187 Optionally synchronize saved views

Original acceptance: 在另一浏览器恢复同一视图，私有筛选不意外暴露给其他用户。

Source: `adaptive_rsi/web/app.js`

### #188 Preserve focus and limit live announcements

Original acceptance: 纯键盘和读屏测试覆盖自动刷新、删除及分页，焦点不丢到页面顶部。

Source: `adaptive_rsi/web/app.js`

### #192 Test real signed desktop upgrades end to end

Original acceptance: 正确包成功升级，坏签名、断电窗口和启动失败各有经过验证的安全结果。

Source: `scripts/release_smoke.py`

### #197 Inject crashes at side-effect receipt boundaries

Original acceptance: 每个窗口重启后既不重复外部副作用，也不把未知结果误报完成。

Source: `tests/test_audit_round5.py`

### #200 Link audit completion to verifiable evidence — completed in this handoff

Original acceptance: 随机抽取完成项能直接重放验收，未覆盖边界明确列为残余限制。

Source: `docs/validation/audit-20261027-next-100.json`

## 24 partial entries and remaining requirements

### #15 Remote connection rejects API v2

Original acceptance: 按受支持版本及能力协商；0.5.124 对接当前服务必须成功。

Remaining boundary (original ledger text): v1/v2 accepted; real remote compatibility and capability negotiation remain.

Implemented files: `desktop/src-tauri/src/main.rs`

No linked acceptance yet; do not assume complete.

### #18 Input context is capped by output capacity

Original acceptance: 分别计算输入上下文与响应限制；合法长输入可用，超上下文输入有明确诊断。

Remaining boundary (original ledger text): Context/output separated; metadata accuracy remains.

Implemented files: `adaptive_rsi/native.py`

No linked acceptance yet; do not assume complete.

### #25 Migration checksums ignore migration contents

Original acceptance: hash 不可变迁移正文并记录版本；迁移内容变化必须被检测。

Remaining boundary (original ledger text): SQL migration bytes and ordered history are checked before mutation; sealed bootstrap is not repeated; additive SQL and ledger roll back together. Legacy 1/2 hashes and transaction-external bootstrap DDL remain historical boundaries. Compatibility format stays v2. Installed wheel resource/startup verified.

Implemented files: `adaptive_rsi/store.py`, `adaptive_rsi/schema_migrations.py`, `adaptive_rsi/migrations/003_seal_bootstrap.sql`

Replay: `python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store`

### #32 Staged updates can create memory peaks

Original acceptance: 测接近上限包的 RSS；优先落盘并设置可观测内存边界。

Remaining boundary (original ledger text): Disk staging added; verify/install still load one bounded buffer; peak RSS measurement remains.

Implemented files: `desktop/src-tauri/src/native_update.rs`

No linked acceptance yet; do not assume complete.

### #33 Disk checks may inspect the wrong volume

Original acceptance: 把 app、cache、临时目录放不同卷测试；各必需卷均做空间检查。

Remaining boundary (original ledger text): Cache and executable volumes checked; extraction-volume proof remains.

Implemented files: `desktop/src-tauri/src/native_update.rs`

No linked acceptance yet; do not assume complete.

### #35 Staged packages bypass subsequent feed checks

Original acceptance: 验证撤回及连续发布场景；安装前重新确认版本可用性并允许替换缓存。

Remaining boundary (original ledger text): Feed refreshed before reuse/install; withdrawal regression remains.

Implemented files: `desktop/src-tauri/src/native_update.rs`

No linked acceptance yet; do not assume complete.

### #44 Existing closed PRs need explicit handling

Original acceptance: 模拟同 head 的 closed PR；不能报告新 PR 已打开，应明确重开或新建策略。

Remaining boundary (original ledger text): Closed PR reuse blocked; publication regression remains.

Implemented files: `adaptive_rsi/delivery.py`

No linked acceptance yet; do not assume complete.

### #56 Database initialization precedes runtime ownership

Original acceptance: 同时启动两实例并注入迁移；拒绝的实例不能先修改数据库。

Remaining boundary (original ledger text): Service entry points claim ownership before initialization; standalone Store utilities and library callers still require a migration ownership contract.

Implemented files: `adaptive_rsi/runtime.py`, `adaptive_rsi/cli.py`, `adaptive_rsi/desktop.py`

Replay: `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_service_ownership_is_acquired_before_database_initialization`

### #58 Restored cases retain old absolute paths

Original acceptance: 恢复到不同目录后执行只读案例检查；路径映射明确，不访问原副本。

Remaining boundary (original ledger text): Cases invalidated/remapped on restore; business restore drill remains.

Implemented files: `adaptive_rsi/storage_tools.py`

No linked acceptance yet; do not assume complete.

### #59 Expired auth cleanup may retain an outer lock

Original acceptance: 注入慢 worker.close 及并发取消；其他会话状态读取保持响应。

Remaining boundary (original ledger text): Slow close moved outside outer lock; concurrency regression remains.

Implemented files: `adaptive_rsi/auth_sessions.py`

No linked acceptance yet; do not assume complete.

### #64 Persist verified update downloads

Original acceptance: 在下载及就绪阶段重启应用，安全恢复下载或使用已验证缓存，损坏缓存必须重下。

Remaining boundary (original ledger text): Verified complete packages persist; partial resume not implemented.

Implemented files: `desktop/src-tauri/src/native_update.rs`

No linked acceptance yet; do not assume complete.

### #65 Separate update transport deadlines

Original acceptance: 注入 DNS、连接及慢响应故障，各阶段有可解释的时限且可取消。

Remaining boundary (original ledger text): Separate transport deadlines implemented; injected fault matrix remains.

Implemented files: `desktop/src-tauri/src/native_update.rs`

No linked acceptance yet; do not assume complete.

### #77 Explain which components an update affects

Original acceptance: 本机自有、已有本机服务和远端三种模式分别显示准确影响。

Remaining boundary (original ledger text): Scope text implemented; three-mode browser proof remains.

Implemented files: `adaptive_rsi/web/native-update.js`

No linked acceptance yet; do not assume complete.

### #90 Evaluate rolling resource-based fairness

Original acceptance: 长短任务混合模拟中报告等待分位数和权重兑现程度，再选择策略。

Remaining boundary (original ledger text): Rolling worker-time accounting and starvation priority implemented; synthetic mixed-duration workload covered, production waiting quantiles and resource-pressure admission remain separate.

Implemented files: `adaptive_rsi/runtime.py`

Replay: `python3 -B -m unittest tests.test_audit_round6.AuditResourceFairnessTest tests.test_audit_round6.AuditResourceWorkloadTest`

### #99 Add archive restoration and relocation

Original acceptance: 移动仓库并恢复归档后历史仍可访问，活动任务及远端身份重新校验。

Remaining boundary (original ledger text): Explicit paused project restoration implemented; relocation remains.

Implemented files: `adaptive_rsi/runtime.py`

No linked acceptance yet; do not assume complete.

### #110 Execute migrations once per version

Original acceptance: 从各历史 fixture 升级，第二次启动不重复回填，未来版本仍被拒绝。

Remaining boundary (original ledger text): SQL migration bytes and ordered history are checked before mutation; sealed bootstrap is not repeated; additive SQL and ledger roll back together. Legacy 1/2 hashes and transaction-external bootstrap DDL remain historical boundaries. Compatibility format stays v2. Installed wheel resource/startup verified.

Implemented files: `adaptive_rsi/store.py`, `adaptive_rsi/schema_migrations.py`, `adaptive_rsi/migrations/003_seal_bootstrap.sql`

Replay: `python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store`

### #117 Bound backup duration and space consumption

Original acceptance: 高写入与低空间实验中有明确终态，旧恢复点不受影响。

Remaining boundary (original ledger text): Backup timeout/reserve implemented; disk-pressure acceptance remains.

Implemented files: `adaptive_rsi/storage_tools.py`

No linked acceptance yet; do not assume complete.

### #142 Hash complete experiment inputs

Original acceptance: 改动任一实验输入都会改变对应摘要，离线核对能发现不一致。

Remaining boundary (original ledger text): Trees/overlay/environment sealed; reproducibility proof remains.

Implemented files: `adaptive_rsi/evolution.py`

No linked acceptance yet; do not assume complete.

### #145 Seal experiment plans before execution

Original acceptance: 修改实验规则产生新版本，报告明确区分预设和探索性分析。

Remaining boundary (original ledger text): Plan sealed before trials; tamper/early-stop regression remains.

Implemented files: `adaptive_rsi/evolution.py`

No linked acceptance yet; do not assume complete.

### #182 Lazily construct large evidence views

Original acceptance: 大证据样本下首屏时间和内存改善，展开内容完整且可导出。

Remaining boundary (original ledger text): Serialization deferred until expansion; pagination/memory measurement remains.

Implemented files: `adaptive_rsi/web/app.js`

No linked acceptance yet; do not assume complete.

### #191 Pin remaining build toolchains

Original acceptance: 同提交在相同输入下可重建，升级工具链通过独立可审查变更完成。

Remaining boundary (original ledger text): Build tools pinned; reproducibility proof remains.

Implemented files: `.github/workflows/release-verify.yml`

No linked acceptance yet; do not assume complete.

### #193 Expand supported-Python boundary coverage

Original acceptance: 两个边界版本都跑真实关键路径，跳过的环境依赖明确记录。

Remaining boundary (original ledger text): Boundary matrix expanded; remote results remain.

Implemented files: `.github/workflows/checks.yml`

No linked acceptance yet; do not assume complete.

### #199 Generate a dependency-complete SBOM

Original acceptance: 抽查打包中的依赖与 SBOM 对齐，发布产物和 provenance 指向同一清单。

Remaining boundary (original ledger text): Locked crate hashes and Python inventory implemented; Rust licenses/alignment remain.

Implemented files: `scripts/package_desktop.py`

No linked acceptance yet; do not assume complete.

### #201 Layered validation, slow-test diagnosis, parallel release verification and release queue coordination

Original acceptance: See ledger.

Remaining boundary (original ledger text): Independent release jobs, measured selector timing, malformed-cache fallback and strict mechanical-metadata ordinary-CI path implemented. Final exact-head full release verification remains mandatory. Remote timing comparison and final release queue behavior remain unverified.

Implemented files: `.github/workflows/release-verify.yml`, `.github/workflows/checks.yml`, `scripts/classify_changes.py`, `scripts/release.py`, `scripts/test_parallel.py`

Replay: `python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel tests.test_release`

## Local commit checkpoints

Original commit subjects are retained.

- `cd6e614 修复第三轮审计首批问题并并行化验证流程`
- `de75578 完善升级恢复边界与存储诊断并修复模型故障关联`
- `022d5ce 保留后台操作真实终态并修复双写故障恢复`
- `5a70142 封存历史数据库引导并校验增量迁移正文`
- `2354f4a 增加取消影响预览与可追溯的需求修订和干预记录`
- `ffdca68 完善任务组拆分重做、修订保留与批次断线核对`
- `7a391e4 修复桌面安装进程退出后的准入恢复并绑定原生进程身份`
- `7d9d587 支持多前置任务依赖图并保留失效分支的独立阻塞关系`
- `b70ef84 补齐实验权衡与分组证据展示并保存脱敏工具中断轨迹`
- `a107b9a 完善数据库等待与慢查询诊断及证据关系巡检和有界隔离`
- `64b6d66 减少机械版本补丁的重复验证并按可靠耗时分配完整测试`
- `c52d51e 增加脱敏配置变更记录与版本及内容双重校验的安全撤销`
- `d43d208 支持部署与回滚的多端点连续健康观察门禁`
- `d10eb42 为自动部署回滚增加数据兼容声明和人工恢复指引`
- `3f1bc12 让执行资源优先级生效并增加防饥饿排队与等待诊断`
- `013ab73 增加可选磁盘与内存压力准入并保留任务排队恢复`
- `45f765e 完善手机差异按文件折叠与长文本换行定位`
- `ad1bb48 建立可重放的大历史查询预算并在CI保留性能报告`
- `77d4892 修正配置错误回归测试桩对审计来源参数的兼容`
- `9bcd32d 增加带词法作用域的动态文案语法检查与缺译反例验证`
- `30ad622 增加同时阻塞原因与前置任务因果链的只读解释`
- `786ade8 统一配置错误原因码与双语字段指引并保留可复制请求诊断标识`
- `71b6a6a 修复跨项目历史回放与任务通知关闭竞态并补完整浏览器验收`

## Suggested next steps, after renewed authorization

1. Read this handoff, the JSON ledger and the full base-to-HEAD diff. Preserve user files. Confirm branch/status before acting.
2. Prioritize real updater/recovery proof, migration/ownership boundaries and missing partial acceptances; batch related pending work by module, without marking unsupported cases complete.
3. Use targeted checks during development; once the scope is complete, fix the commit and run full Python, Node, browser, Rust, packaging, docs and contracts. Investigate failures rather than repeatedly rerunning everything after tiny edits.
4. Review the full branch, then use normal protected PR and exact-commit remote release gates if the user authorizes continuation. Publish a new version; never replace existing v0.5.124 assets.
5. Only after release evidence exists should a real signed local upgrade and recovery drill be claimed. IPC/HTTP/FakeAgent tests prove infrastructure behavior, not real model capability gains.

Ledger maintenance: `python3 -B scripts/check_audit_ledger.py --write`, then `python3 -B scripts/check_audit_ledger.py`. Commands are replay entry points, not certificates of success. Respect Chinese commits, Chinese-default UI plus English i18n, and paired user documentation.
