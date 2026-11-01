# unclassified flaky 记录分类与结论（2026-10-31 数据）

**简体中文** | [English](flaky-unclassified-triage-20261031.en.md)

## 数据来源与核对

[audit-20261031 的 PROC-02]（`~/rsi-ops/audit-20261031.md`，明细台账在用户 Mac 的 `/Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-audit-20261031/`）称 `.ci/test-durations.failures.json` 有 21 条 unclassified 记录、「全 macOS、随机种子、无惯犯」，且与已知 `worker exit -11` 未决问题吻合。本轮对这三个说法逐一核对，结论是：**21 条数据源未定位，「全 macOS」与「吻合 exit -11」均不成立**。

核对过程：

- 服务器上能找到的 failures 记录只有两批，均为 Linux 平台：7 条（`wt-bug06` worktree 的 `.ci/test-durations.failures.json`，2026-10-31 09:37–09:39 UTC）与 36 条（`TMPDIR` 下 4 个 `tmp*.failures.json` 孤立回执，11:57:54–11:58:35 UTC）。两批合计 43 条，没有任何一份文件恰好 21 条。
- GitHub 全量扫描了 `release-verify.yml` 与 `release.yml` 的 190 个 run，并在本轮（2026-10-31）下载了当时存在的全部 45 份 `timing-*` artifact（上传步骤定义于 `release-verify.yml:62-68`、`release.yml:136-142`）：artifact 内只有 `test-durations.json`，没有任何 failures 文件。通过的选择器组不产生记录；判定前因段错误终止的 suite 同样不产生记录（见下节机制，已写出的回执不受 worker 后续崩溃影响）。数据源仍在用户 Mac 侧的可能性无法排除，但审计引用的是相对路径，本轮无法证实。
- 「21 条」的一种可能解释是孤立回执之和（4 个文件分别含 15/10/6/5 条，存在多种和为 21 的组合），但没有证据表明审计读取过这些文件，不作定论。

## 记录产生机制（按 suite 粒度对应，不是整体互斥）

失败记录由 `scripts/test_worker.py:278-292` 在**一个类级 suite 跑完且 unittest 判定失败**时写入临时回执：worker 的 `suites()`（`scripts/test_worker.py:50-71`）先把模块选择器拆成类级组，失败回执的 `selector` 字段保留原模块选择器；回执写在同一份文件里，父进程收集时不看 worker 退出码（`scripts/test_parallel.py:152-159,180-188`）。因此：

- 判定之前因段错误（SIGSEGV/exit -11）终止的 suite，自身不会为所在的组写出记录；
- 但 worker 在**后续** suite 或收尾阶段崩溃，不会删除它早先写出的回执——这些回执照常被汇总。所以「有失败记录」不能证明「该 worker 全程健康」，「某 worker exit -11」也不能推出「它跑过的组都没有失败记录」；
- 父进程先于 worker 结束时，回执成为 `TMPDIR` 下的孤立文件，不进入 `.ci/`。本轮找到的 4 个 `tmp*.failures.json` 正是这种残留。

结论：记录与崩溃的关系必须对应到具体 suite 与时间窗来判断；下文对 exit -11 的排除建立在平台、时间与「CI 的 exit -11 组无对应记录」三个证据上，而不是「记录与崩溃机制互斥」。

## 分类统计

### 批次一：203 轮全量（7 条，2026-10-31 09:37–09:39 UTC）

| 选择器 | 记录数 | 失败用例数 | 时间 (UTC) |
|---|---|---|---|
| tests.test_audit_environment | 1 | 1 | 09:37:27 |
| tests.test_controls | 1 | 5 | 09:37:38 |
| tests.test_experience | 1 | 1 | 09:38:00 |
| tests.test_reliability | 1 | 3 | 09:38:25 |
| tests.test_delivery_loop | 1 | 1 | 09:38:26 |
| tests.test_runtime | 1 | 2 | 09:38:32 |
| tests.test_workflow_experience | 1 | 1 | 09:39:04 |

合计 7 条记录、7 个模块、14 个失败用例（含 1 个参数化重复，去重 13 个）。

### 批次二：11:58 级联（36 条孤立回执，11:57:54–11:58:35 UTC，约 42 秒）

| 选择器 | 记录数 | 失败用例数 |
|---|---|---|
| tests.test_backend_hardening | 5 | 17 |
| tests.test_delivery_readiness_audit | 4 | 19 |
| tests.test_model_center | 3 | 59 |
| tests.test_audit_round6 | 2 | 26 |
| tests.test_credential_lock_scope | 2 | 6 |
| tests.test_data_hardening | 2 | 11 |
| tests.test_discovery_hardening | 2 | 29 |
| tests.test_controls | 1 | 8 |
| tests.test_operability | 1 | 19 |
| tests.test_cleanup_recovery | 1 | 11 |
| tests.test_delivery_loop | 1 | 33 |
| 其余 12 个模块（各 1 条） | 12 | 49 |

合计 36 条记录、23 个模块、287 个失败用例（去重 286 个）。同一模块的多条记录不必是重复执行：worker 按测试类拆组，每个失败的类各写一条回执并保留模块选择器，test_backend_hardening 恰有 5 个测试类、test_model_center 恰有 3 个，与回执条数一一对应。因此这批记录与「23 个模块各产生 1–5 条失败类回执」的单次执行形态一致（修订说明：初稿曾据「同模块多记录且失败用例不同」推断为重复执行，经评审指出并核对源码后更正；依赖时间窗与模块覆盖面的 213 轮并行归因不受影响）。

### 维度汇总

| 维度 | 批次一（7 条） | 批次二（36 条） |
|---|---|---|
| 平台 | Linux-7.0.0-1014-aws-aarch64-with-glibc2.39（本机，2 vCPU / 2 GB） | 同左 |
| Python | 3.14.9 | 同左 |
| hash_seed | 全部 `random`（运行环境未设 `PYTHONHASHSEED`，非种子复现手段） | 同左 |
| quarantined | 全部 false | 同左 |
| 惯犯（同一用例出现在多条记录） | 无 | 无 |

两批合并后，以完整用例 ID 计出现在超过一条记录中的用例只有 7 条，对应 6 个测试方法（其中 controls 的 `test_deferred_and_rejected_never_execute_and_keep_value_evidence` 带 `decision='deferred'/'rejected'` 两个参数化），且全部是两批交集——即 09:37 已在本机稳定失败、11:58 再次失败的同一批环境性用例，不是「平时绿、偶尔红」的随机 flaky。

## 与 worker exit -11 的关系：不吻合

已知 `worker exit -11` 的真实证据在 CI：release-verify run [41203204855](https://github.com/Haoog/Adaptive-RSI/actions/runs/41203204855)（PR #119 @f4b29a3，macOS runner）的 tests 作业，`[group 1] exit -11`，发生在 60 秒卡顿看门狗触发 faulthandler 输出的瞬间（栈帧在 `test_worker.py line 0 in _frames` 被截断），日志中没有任何测试断言失败。

上述 43 条记录与它不吻合：

1. 平台不同：CI 的 exit -11 在 macOS runner；两批记录全部是 Linux 本机。
2. 记录性质不同：43 条记录全部由 unittest 结果判定产生，每条都有具体失败用例；CI 的 exit -11 组（group 1）没有留下任何带失败用例的记录（日志无断言失败）。按上节机制，段错误发生的 suite 在判定前终止、不写出记录，而同 worker 早先 suite 的回执不受影响——两组证据按 suite 对应后不重合。
3. 时间不同：本机两批发生在 2026-10-31 09:37 与 11:58（UTC）；CI run 41203204855 开始于 12:55 UTC。
4. 分布不同：批次一与测试运行器递归事故记录无关，与 adaptive-rsi-loop 运行经验里已记载的本机稳定失败集合一致（test_audit_environment / test_controls / test_experience / test_delivery_loop / test_runtime / test_reliability / test_workflow_experience）；批次二覆盖 23 个模块、秒级集中爆发，是并行资源踩踏的形态，与单 worker 段错误不同。

## 级联的归因

213 轮（2026-10-31 11:52 启动，任务 bug20-audit-size）在后台启动了全量 `python3 -B scripts/test_parallel.py --durations .ci/test-durations.json`（run.log 记录 session `proc_c1baa3de3660`），未等待结果即结束本轮。11:57:54–11:58:35 的 36 条记录正落在这次后台运行期间，与循环后续轮次（214 轮 11:58:27 启动）并行。在 2 vCPU / 2 GB 的机器上，全量四 worker 与其他轮次的进程叠加导致大面积非确定性失败；213 轮工作树被回收后，父进程无法收集回执，留下 4 个孤立文件。这一归因与时间窗口及运行日志一致，但未做受控复现。

## 结论

1. 服务器上可核实的 43 条 unclassified 记录中没有任何真 flaky：批次一是本机已知环境性失败的复现，批次二是全量运行与循环轮次并行导致的资源踩踏。
2. 审计 PROC-02 的三个表述（21 条、全 macOS、与 exit -11 吻合）均与可核实数据不符；21 条的原始数据源本轮未能定位，需要用户侧（Mac 工作副本）确认。
3. `worker exit -11` 的排查对象应保持为 CI macOS runner 上看门狗 faulthandler dump 路径（run 41203204855 的既有结论）；可核实的 failures.json 记录中没有与它对应的失败用例证据。
4. 流程教训：全量 `test_parallel.py` 不应与循环轮次并行（本机仅 2 vCPU / 2 GB）；轮次结束前应等待或终止自己启动的后台全量，并把最终状态写入 SUMMARY。

本轮为只读分析 + 新增文档，未改动测试运行器行为，未改动既有事故记录正文（`test-runner-recursion-20261028.md`），未放宽任何护栏。
