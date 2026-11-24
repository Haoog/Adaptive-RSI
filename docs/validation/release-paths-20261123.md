# 发布复用路径实测

**简体中文** | [English](release-paths-20261123.en.md)

只读查询 `Haoog/Adaptive-RSI` 在 `2026-10-23T13:33:57Z` 至 `2026-11-23T16:01:00Z` 创建、采集时已完成的 `release.yml` 和 `release-verify.yml` runs。窗口起点为复用作业引入时刻。共读到 **479 个 run、488 个 attempt**，其中正式发布流程 425 个 run / 425 个 attempt，精确提交验包流程 54 个 run / 63 个 attempt。376 个发布 attempt 未进入可识别的发布路径，包含准备失败和无需发布的执行。

## 发布路径

以下时间单位均为秒。区间给出最小值和最大值。

| 路径 | Run / attempt | 关键作业排队中位数（区间） | 验包或重建中位数（区间） | `prepare` 启动至 `publish` 结束中位数（区间） |
| --- | ---: | --- | --- | --- |
| 复用成功 | 45 / 45，42 个不同的工作流 SHA | `reuse` 3（1–603），n=45 | `reuse` 49（36–92），n=45 | 80（53–691），n=45，44 个发布作业成功，1 个失败 |
| 无可复用产物，重建 | 3 / 3，2 个不同的工作流 SHA | `build` 2.5（2–3），n=2，缺 1 | `prepare` 完成至 `build` 完成 834（774–894），n=2，缺 1 | 857（798–916），n=2，缺 1 |
| 复验失败后重建 | 0 / 0 | 缺样 | 缺样 | 缺样 |
| 复用取消后重建 | 1 / 1 | `reuse` 缺 runner 时间，`build` 3 | `reuse` 作业时间戳跨 480 秒；`reuse` 结束至 `build` 完成 1284 | 1794，发布作业成功、attempt 结论为 `cancelled` |

- 复用成功样本的 provenance 校验中位数为 23 秒、DMG 与校验和复验 4 秒、ZIP/wheel smoke 7 秒，各 n=45。`45209940650/1` 的 `reuse` 排队为 309 秒，作业执行 92 秒。 [对应 attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45209940650/attempts/1)
- 两次无可复用产物的成功重建均由 `build` 决定关键路径。桌面构建与检查分别为 505、415 秒，安装升级 smoke 均为 8 秒。第三次 `build` 取消且未观测到发布作业执行。跳过 `reuse` 且重建实际启动可确认这一分支，详细产物不可用原因无法从 jobs 推断。 [重建样本一](https://github.com/Haoog/Adaptive-RSI/actions/runs/40712540811/attempts/1) · [重建样本二](https://github.com/Haoog/Adaptive-RSI/actions/runs/40801795824/attempts/1) · [未走完的样本](https://github.com/Haoog/Adaptive-RSI/actions/runs/40709279448/attempts/1)
- 复用取消样本无独立复验步骤时间，`build` 执行 1280 秒，桌面构建与检查 453 秒，安装升级 smoke 7 秒。窗口内未观察到 `reuse` 失败或超时后启动重建，取消样本单列。 [取消后重建的 attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/40534641447/attempts/1)

## 精确提交验包

复核覆盖本窗口内全部 54 个验包 run、63 个 attempt。9 次重跑中，2 次启动了新 `gate`，7 次继承了先前的 `gate`，继承记录的两种入口耗时均为空。同期 425 个正式发布 attempt 均为首跑。

| 验包 attempt 范围 | 总数 | 有值 / 缺值 | 本次起点至 `gate` 启动中位数（区间） | run 首次创建至 `gate` 启动中位数（区间） |
| --- | ---: | ---: | ---: | ---: |
| 全部 | 63 | 56 / 7 | 4（3–410） | 4（3–1311） |
| 首跑 | 54 | 54 / 0 | 3.5（3–410） | 3.5（3–410） |
| 重跑 | 9 | 2 / 7 | 4.5（4–5） | 847（383–1311） |

`40934405014/2` 本次等待为 5 秒，run 累计为 1311 秒。`41005242867/2` 本次等待为 4 秒，run 累计为 383 秒。旧脚本直接使用精确 attempt 端点的 `created_at`，这两例均得到 3 秒，全体有值样本的旧中位数也是 3 秒。两条重跑记录的原始 `created_at` 分别比 `run_started_at` 晚 2 秒和 1 秒。 [重跑样本一](https://github.com/Haoog/Adaptive-RSI/actions/runs/40934405014/attempts/2) · [重跑样本二](https://github.com/Haoog/Adaptive-RSI/actions/runs/41005242867/attempts/2)

63 个 attempt 中有 58 个新 `verify` 作业，另 5 个继承之前的作业。新作业排队中位数 3 秒（2–304），执行中位数 612.5 秒（209–1353），各 n=56、缺 2。桌面构建与检查中位数 194 秒（59–659），安装升级 smoke 8 秒（5–33），各 n=53、缺 5。`44228968296/2` 的 `verify` 在本次 attempt 启动前已结束，其计时只归入 attempt 1。 [首次 attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/44228968296/attempts/1) · [部分重跑](https://github.com/Haoog/Adaptive-RSI/actions/runs/44228968296/attempts/2)

在 `45206714950/1`，验包候选为 `e37e550807fae08d7acffd5024729ce955b2e2c4`，版本 PR 为 #523。工作流 SHA 为 `39c1229f33eeda27435fd8718364097d7d6e2b5b`。`verify` 排队 2 秒、执行 359 秒，桌面构建与检查 265 秒、安装升级 smoke 10 秒。合并后的发布样本 `45209940650/1` 对应工作流 SHA `77b10646a2fac8069a411e4f1c938e6636b9e66b`。 [验包 attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45206714950/attempts/1) · [版本 PR](https://github.com/Haoog/Adaptive-RSI/pull/523) · [发布 attempt](https://github.com/Haoog/Adaptive-RSI/actions/runs/45209940650/attempts/1)

## 复测与口径

```sh
python3 -B scripts/release_path_metrics.py --created 2026-10-23T13:33:57Z..2026-11-23T16:01:00Z
python3 -B scripts/release_path_metrics.py --run 40934405014/2 --run 41005242867/2 --run 44228968296/2 --run 45209940650/1
```

命令仅用 `gh api --method GET` 读取已完成 runs、attempts 和 jobs，向标准输出写 JSON。时间窗按 run 创建时间筛选，`--run` 可锁定精确 attempt。JSON 保留每个 attempt 的工作流 SHA、job URL、结论和时间。若运行标题含显式候选 SHA，则以独立字段记录。个别旧验包 run 的标题没有候选 SHA，此字段留空。

`attempt_to_entry_seconds` 从本次起点计至新 `gate` 或 `prepare` 启动，可能包含工作流并发和调度等待。起点与 #16 的精确 attempt 口径一致。首跑用 run 首次创建时间，重跑用精确 attempt 的 `run_started_at`，且须晚于首次创建时间及已知的先前 attempt 起点。起点缺失、无法验证或晚于入口启动时，本次耗时为 `null`。`run_to_entry_seconds` 从首跑的 `run_created_at` 累计至本次新入口启动。`attempt_created_at` 和 `attempt_started_at` 保留端点原始值，`attempt_origin_at` 记录选定的起点。

排队采用已分配 runner 的 job 创建至启动的时间差。作业时间戳跨度可含未分配 runner 的等待。执行中位数仅计算有 runner 的作业，跳过步骤和缺少时间戳保留为 `null`。部分重跑可返回新 job ID 配旧 `started_at`/`completed_at`。结束时间早于本次 attempt 启动的记录标为继承，保留结论，排除其计时。回退关键路径取重建并行门禁中最后完成的作业，起点为导致重建的 `prepare` 或 `reuse` 完成时间。没有发布作业执行证据时，无法确定到达发布的完整重建路径。统计是历史观测，工作流版本、runner 及提交不同，复用和重建的样本差异不能归因于复用本身。
