# Agent 交接：审计实现现状与剩余范围

[English](agent-handoff-20261028.en.md)

用户最新要求：当前“台账核对和证据索引”做完后先停，整理目前成果及那 83 项，交给另一个 agent。**本交接完成后不继续实现、不推送、不合并、不发版、不更新本地安装。** 此前的发布授权已被这条最新指令收窄；接手后先等待用户安排。

用户提出交接时是 **94 项已实现、24 项部分完成、83 项待做**。之后只收尾了这 83 项中的当前索引任务 **#200**，因此最终为 **95 项已实现、24 项部分完成、82 项待做**，总计 201 项（原审计 200 项加验证提速 #201）。下文完整保留原 83 项，明确标出 #200 已在交接收尾完成。部分完成不计为完成，`release_ready=false`。

## 工作现场与权威记录

- 仓库：`/Users/neg-entropy/Desktop/Adaptive RSI`。
- 分支：`fix/audit-200-and-faster-validation`。
- 起点：`75aecba5ef63bd719eb025b74d5c3c409d3ecf71`（v0.5.124 / PR #87）。
- 交接前最后一笔功能提交：`71b6a6a`；本次索引和交接另存为后续本地提交，准确最终 SHA 用 `git log -1` 获取。
- 此分支工作均保留在本地，未推送、创建 PR、合并、发布或更新本地应用。既有 v0.5.124 发布资产不能被不同字节覆盖，后续需使用新版本。
- `docs/research/` 是原有未跟踪用户资料，不修改、不删除、不 stage。
- 仓库外存在 updater 签名私钥，不读取、不写入报告或提交。验证没有调用真实或付费模型。

[JSON 台账](audit-200-implementation-20261028.json) 是状态、验收要求、实现路径、验证入口和残余限制的唯一权威记录。[完整证据索引](audit-200-evidence-20261028.md) 覆盖全部 201 项；[实施进度](audit-200-progress-20261028.md) 保留各批实现说明。原始审计输入位于 `/Users/neg-entropy/Desktop/Adaptive-RSI-audit-20261028/entries.tsv`。

## 目前已实现什么

95 项完成项的完整编号、代码路径和验收入口见证据索引，不要只凭提交标题判断完成。主要涉及原生模型协议诊断和中断轨迹、任务生命周期/修订/任务组/依赖图、供应商健康恢复、数据库迁移与历史及可观测性、后台操作回执、归档/备份恢复、桌面更新缓存与进程身份准入、部署观察与回滚策略、资源优先级与压力准入，以及导航、手机差异、翻译检查和结构化错误。

验证提速方面：Python、浏览器、桌面发布验证拆为独立 job；记录选择器耗时并按有效历史耗时分配工作线程，损坏缓存回退完整发现；机械版本 PR 只有逐字匹配独立重建元数据补丁才走普通 CI 简化路径。最终精确提交的完整发布门禁保留。远端实际提速和发布排队行为未验证，#201 仍为部分完成。

自动更新方面已有补丁版本默认策略、可选版本与安装窗口、磁盘缓存、安装前 feed 重查、签名验证以及取消/准入恢复等本地实现与验证，但不能称为生产升级端到端完成。部分下载续传、真实启动失败恢复、放量策略及真实签名替换证据仍有待做项；IPC 桩不证明真实应用替换，也不证明其他用户已收到更新。

最近一批新增了配置历史和安全撤销、多端点连续健康门禁、自动回滚的数据兼容声明、资源优先级和 FIFO 防饥饿、可选磁盘/内存准入、按文件手机差异、大历史固定数据性能预算、AST 翻译检查、任务阻塞因果解释、双语结构化错误及请求 ID、导航历史与通知关闭竞态修复。

## 验证证据与明确边界

- 较早检查点 `de75578`：全量 Python 842 项、四工作组通过。这是该检查点证据，不是最终 HEAD 证据。
- 后来的全量 Python 四组均完成，其中一组有 3 个断言失败：server-hardening 测试桩没接受新增 `source` 参数。`77d4892` 修复后该模块 16 项通过。之后新增功能做了针对性验证，尚未重新跑最新 HEAD 的完整 Python；不能称最终全量通过。
- 最近一次完整浏览器：33 组中 32 组通过；delivery-feedback 因通用翻译错误盖住具体 SHA 指引失败。`71b6a6a` 已修正文案优先级，该失败组复跑通过。不能将原完整运行说成一次全绿。
- 最新 Node 全套 351 项通过；最近记录的 Rust 13 项通过来自此前检查，本次收尾未改 Rust。
- 本地合成 5 万任务/10 万运行基准：任务页 p95 113.92 ms、工作流指标 435.20 ms、质量指标 164.61 ms。见[报告](history-performance-20261028.json)，不代表生产或远端 CI 性能。
- wheel 已实际构建并安装到仓库外临时 venv，`-I` 启动隔离 HTTP 检查返回 200 / API v2，schema 资源及迁移 1–6 正常，模型调用数为零。见[报告](wheel-install-20261028.json)。其中版本 0.5.124 仅为本地 smoke，不上传。
- 本次收尾：台账 4 项测试及工作流配置 7 项通过；CI 选择器覆盖检查报告 975 项，文档链接/中英配对及台账生成核对通过。这不代表实际执行了全部 975 项。
- 两路只读复查指出 unittest 选择器检查会导入可信测试模块并执行可能的 `load_tests` hook，不能称纯静态；已在贡献指南明确。检查器不执行台账内验收命令或测试用例。另一复查无确定性缺陷。复查基点为 `71b6a6a`，不等于整个大分支的最终发布评审。
- 没有最终精确提交的远端 CI、真实签名应用替换、公证验证、发版或本地安装更新。`implementation_commit=null`、`ci_evidence=[]` 刻意保持未就绪。

`/tmp` 日志可能被清理：`/tmp/rsi-audit-current-full-python.log`、`/tmp/rsi-audit-current-full-browser.log`、`/tmp/rsi-navigation-delivery-feedback.log`、`/tmp/rsi-navigation-node.log`、`/tmp/rsi-wheel-install.log`。长期入口以仓库内 JSON 产物和台账重放命令为准。

## 原来的 83 项待做清单

仅 #200 在本次收尾完成，另外 82 项仍为 pending。保留原编号，避免新 agent 重排后无法对账。

### #34 原生更新缺少启动失败后的完整恢复证据

验收要求：用真实签名包注入启动和迁移失败；验证应用及状态数据的一致回退。

来源：`desktop/src-tauri/src/native_update.rs`

### #48 监控窗口结束后不再检测后续漂移

验收要求：模拟窗口后再出现回归；选择性长期哨兵应发现并解释变化。

来源：`adaptive_rsi/evolution.py`

### #62 稳定版增加可暂停的分批更新机制

验收要求：模拟暂停和恢复，已获授权的版本范围及签名校验始终有效。

来源：`desktop/src-tauri/src/native_update.rs`

### #63 下载策略可考虑网络和电源状态

验收要求：切换网络和电源状态，任务执行不受影响，用户可随时手动覆盖。

来源：`desktop/src-tauri/src/native_update.rs`

### #66 建立更新签名公钥轮换流程

验收要求：用测试密钥演练跨两版本轮换，未经授权的签名仍被拒绝。

来源：`desktop/src-tauri/src/native_update.rs`

### #67 更新前显示操作系统和架构兼容性

验收要求：以不兼容测试资产验证提前解释原因，当前安装保持可用。

来源：`desktop/src-tauri/src/native_update.rs`

### #68 建立明确的平台资产支持矩阵

验收要求：每个宣称支持的组合都有构建、安装及更新证据，未支持组合明确提示。

来源：`scripts/package_desktop.py`

### #69 为后台下载提供可选带宽限制

验收要求：限速开启后吞吐符合配置，取消及时，签名校验不变。

来源：`desktop/src-tauri/src/native_update.rs`

### #70 更新进度区分速度、校验及替换阶段

验收要求：未知总大小不显示虚假百分比；慢速及校验阶段均有准确状态。

来源：`adaptive_rsi/web/native-update.js`

### #71 空间需求按实际资产估算

验收要求：小包和大包的空间边界测试均通过，未知体积时保守拒绝并说明依据。

来源：`desktop/src-tauri/src/native_update.rs`

### #72 允许用户选择关窗、后台驻留和登录启动行为

验收要求：逐一验证关窗、托盘退出及系统重启，不误停非自有服务。

来源：`desktop/src-tauri/src/main.rs`

### #73 增加用户主动启用的崩溃诊断包

验收要求：导出前可预览内容，凭据和用户源码不进入默认诊断包。

来源：`adaptive_rsi/storage_tools.py`

### #74 按运行实例保存导航和视图偏好

验收要求：切换两个含不同项目的服务，分别恢复各自的有效视图。

来源：`adaptive_rsi/web/desktop.js`

### #75 增加桌面常用操作的原生菜单与快捷键

验收要求：在 macOS 菜单和键盘上完成常用流程，快捷键不干扰文本输入。

来源：`desktop/src-tauri/src/main.rs`

### #76 提供代理和企业证书连接诊断

验收要求：测试代理认证失败和自建 CA 环境，诊断不输出代理密码或令牌。

来源：`desktop/src-tauri/src/main.rs`

### #79 提供可导出的升级回执

验收要求：成功、取消及恢复均有明确记录，回执不含用户认证信息。

来源：`desktop/src-tauri/src/native_update.rs`

### #80 按功能能力协商和界面降级

验收要求：新客户端接旧服务时可用功能继续工作，不支持操作有明确提示。

来源：`adaptive_rsi/contracts.py`

### #83 合并任务的每条验收要求链接独立证据

验收要求：一个子要求失败时组不能宣称全部通过，每条要求可导航到实际证据。

来源：`adaptive_rsi/batching.py`

### #84 按预计验证成本建议拆分过大任务组

验收要求：用实际 CI 数据比较分组策略，用户确认拆分且每个组仍通过完整门禁。

来源：`adaptive_rsi/batching.py`

### #85 预览共享检查收益和失败重做代价

验收要求：估计与实际执行可对比，证据不足时标记未知而非保证加速。

来源：`adaptive_rsi/web/batch.js`

### #86 推荐任务簇但保留用户授权

验收要求：推荐不产生任务变更；确认后执行范围与预览完全一致。

来源：`adaptive_rsi/batching.py`

### #89 用真实运行阶段样本估计排队时间区间

验收要求：历史回放报告区间覆盖率及误差，冷启动或样本不足明确说明。

来源：`adaptive_rsi/telemetry.py`

### #93 用到期队列减少大项目数下的全量轮询

验收要求：百千项目负载对比 CPU、查询数和到期误差，不降低取消与配置响应。

来源：`adaptive_rsi/runtime.py`

### #94 展示正常关闭的阶段和剩余工作

验收要求：注入慢子进程和维护任务，关闭状态始终可解释且最终有明确结果。

来源：`adaptive_rsi/runtime.py`

### #95 按阶段配置时限并记录超时位置

验收要求：慢模型、慢准备和慢测试分别命中对应时限，取消保持及时。

来源：`adaptive_rsi/config.py`

### #96 安全复用已封存的只读阶段检查点

验收要求：任一输入变化都失效；写工作区或远端操作不因检查点自动重放。

来源：`adaptive_rsi/evaluator.py`

### #98 预约任务明确时区与夏令时规则

验收要求：夏令时跳过及重复小时测试给出确定行为，时区变化不静默移动授权时间。

来源：`adaptive_rsi/runtime.py`

### #101 为分页提供稳定游标或快照版本

验收要求：翻页同时插入及更新任务，不漏读既定快照记录，并提示快照过期。

来源：`adaptive_rsi/server.py`

### #108 完善项目及运行关系的外键覆盖

验收要求：导入既有孤儿数据时先审计隔离，合法历史升级后不丢失。

来源：`adaptive_rsi/store.py`

### #113 为 settings 命名空间声明类型、版本及 TTL

验收要求：损坏单一 namespace 时其他功能仍可用，迁移和保留规则可检查。

来源：`adaptive_rsi/store.py`

### #118 清理动作增加可预览的短期回收区

验收要求：预览与实际对象一致，活动及法律保留对象不清理，撤销可验证。

来源：`adaptive_rsi/maintenance.py`

### #120 定期隔离演练业务级恢复

验收要求：恢复演练不派发真实远端操作，发现路径及对象关系问题并生成报告。

来源：`adaptive_rsi/storage_tools.py`

### #121 模型能力元数据加入来源、日期和人工覆盖

验收要求：未知模型保持保守，错误目录可回退，界面显示元数据来源及时间。

来源：`adaptive_rsi/agent.py`

### #122 为各类工具设置独立结果预算和分页

验收要求：各工具在边界处返回可继续读取的位置和准确截断原因。

来源：`adaptive_rsi/native.py`

### #123 输入 token 估计显示语言相关不确定范围

验收要求：多语言样本与真实计数比较误差，未知 tokenizer 时不宣称精确。

来源：`adaptive_rsi/native.py`

### #127 索引读取 Git 忽略规则及用户排除项

验收要求：嵌套忽略、显式包含和未跟踪源码规则可预测，结果仍说明部分性。

来源：`adaptive_rsi/native.py`

### #128 搜索增加路径、大小写及正则选项

验收要求：对无效正则、超长模式和大结果集保持有界执行及部分结果说明。

来源：`adaptive_rsi/native.py`

### #129 大工具输出保留为独立产物引用

验收要求：超限输出不会进入模型上下文，保留产物仍按容量和敏感信息规则管理。

来源：`adaptive_rsi/native.py`

### #130 为可重试传输失败明确付费请求语义

验收要求：分别注入连接失败、响应丢失和 429，回执准确说明是否可能产生费用。

来源：`adaptive_rsi/native.py`

### #132 原生模型支持流式进度与首字节指标

验收要求：碎片化数据、取消及中断均有契约测试，不提前执行未完整解析的工具。

来源：`adaptive_rsi/native.py`

### #135 建立多协议畸形响应的统一契约矩阵

验收要求：每个协议运行相同语义样本，异常不丢已完成工作也不重复执行工具。

来源：`adaptive_rsi/native.py`

### #136 显示 fallback 原因及共享故障域

验收要求：模拟共享账号故障，用户能理解跳过路由及最终失败原因。

来源：`adaptive_rsi/agent.py`

### #137 费用估计分开输入、输出及缓存价格

验收要求：用公开固定样例核对账单公式，历史调用使用当时价表而非当前价格。

来源：`adaptive_rsi/usage_report.py`

### #138 明确费用币种、单位和订阅估值含义

验收要求：混合 API 和订阅调用时总计准确标记未知部分，用户可导出依据。

来源：`adaptive_rsi/usage_report.py`

### #139 展示任务成本预测与实际偏差

验收要求：样本不足时标未知，不新增强制额度限制；历史预测可与实际核对。

来源：`adaptive_rsi/telemetry.py`

### #143 最终晋升使用未参与候选选择的独立复核集

验收要求：复核集未参与提案或排序，使用记录可查，样本不足时不宣称提升。

来源：`adaptive_rsi/evolution.py`

### #144 将人工驳回和误报反馈用于校准监控

验收要求：标签有来源且不直接篡改封存测试结果，展示样本量及偏差。

来源：`adaptive_rsi/evolution.py`

### #146 记录性能实验的环境噪声和重复测量

验收要求：同一改动跨负载条件复测，结论能解释噪声且不把一次波动当提升。

来源：`adaptive_rsi/evaluator.py`

### #147 用固定负例验证检查工具确实能抓到错误

验收要求：负例必须被现有门禁阻止；不以 mutation 分数替代真实需求验收。

来源：`adaptive_rsi/evaluator.py`

### #148 按验收要求设计反例而非只验证正常路径

验收要求：关键要求的正例通过、反例失败，来源和执行结果可以追溯。

来源：`adaptive_rsi/evaluator.py`

### #149 记录实际执行的测试 ID 和跳过原因

验收要求：只执行无关测试的样例不能冒充目标回归验证，跳过项可查。

来源：`adaptive_rsi/evaluator.py`

### #150 采集有界标准测试及覆盖率产物

验收要求：产物缺失有明确状态，路径及大小受限，不从任意宿主路径采集。

来源：`adaptive_rsi/evaluator.py`

### #151 缓存不可变的依赖准备环境

验收要求：工具、锁文件、种子或配置变化自动失效，跨任务无可写污染。

来源：`adaptive_rsi/environment.py`

### #152 性能提升同时约束关键副指标不退化

验收要求：构造主指标通过但内存或错误率退化的样例，结果按预先封存的规则阻止晋升。

来源：`adaptive_rsi/workflow.py`

### #153 数据集删除和重分类保留曝光历史

验收要求：历史实验仍可复核，已曝光样本不能通过改名变成新 holdout。

来源：`adaptive_rsi/evolution.py`

### #154 晋升前展示策略差异和适用边界

验收要求：用户能从界面追溯每个行为变更到封存实验，未覆盖场景明确说明。

来源：`adaptive_rsi/evolution.py`

### #156 导出可离线复现的封存实验包

验收要求：在隔离机器验证输入完整性，缺凭据或外部依赖时明确列出限制。

来源：`adaptive_rsi/evolution.py`

### #157 证据记录宿主工具精确版本

验收要求：更换关键工具时环境身份或证据变化，报告可定位具体版本。

来源：`adaptive_rsi/evaluator.py`

### #158 建立 monorepo 检查依赖图

验收要求：跨包依赖变更触发正确检查，未知影响回退完整集合。

来源：`adaptive_rsi/evaluator.py`

### #159 策略和技能共同变化时提供归因分析

验收要求：同输入下比较各配置，报告不把组合收益全部归给单一模块。

来源：`adaptive_rsi/evolution.py`

### #161 区分合并队列入队与实际合并状态

验收要求：在启用队列的测试仓库验证入队、重排、失败和最终合并状态。

来源：`adaptive_rsi/delivery.py`

### #162 准备度检查兼容 GitHub rulesets

验收要求：分别在两种保护配置测试，权限不足时显示未知而不假称可合并。

来源：`adaptive_rsi/delivery.py`

### #163 共享可信输入的 CI 环境准备

验收要求：比较冷暖缓存耗时，缓存投毒和过期输入不能绕过最终验证。

来源：`.github/workflows/release-verify.yml`

### #164 早期受影响检查与最终完整门禁衔接

验收要求：注入跨模块回归仍在最终门禁被发现，时间收益用真实运行数据确认。

来源：`.github/workflows/checks.yml`

### #165 合并后失败区分候选与其他主线提交影响

验收要求：候选通过但后续提交破坏的样例不被直接记为候选回归。

来源：`adaptive_rsi/delivery.py`

### #166 区分停止本地跟踪和取消远端工作流

验收要求：两种取消分别验证，远端已完成或结果未知时不报告虚假的取消成功。

来源：`adaptive_rsi/delivery.py`

### #167 GitHub 主限流与次级限流统一退避

验收要求：注入 403 次级限流、429 及未知写响应，不产生重复 PR 或 dispatch。

来源：`adaptive_rsi/release.py`

### #168 上游强制推送和分叉基线提供恢复指引

验收要求：模拟 force-push 与分叉，旧证据失效且不会自动覆盖上游。

来源：`adaptive_rsi/delivery.py`

### #169 终态后处理使用可核对的持久化待办

验收要求：在每个后处理步骤前后崩溃，重启后恰当恢复且无重复副作用。

来源：`adaptive_rsi/runtime.py`

### #170 可选签名 webhook 降低远端轮询成本

验收要求：乱序、重复和漏事件测试后状态正确，不要求普通本机用户部署额外服务。

来源：`adaptive_rsi/delivery.py`

### #174 将工作流目标环境绑定到授权证据

验收要求：修改目标环境后旧授权失效，回执显示实际部署目标及输入 hash。

来源：`adaptive_rsi/release.py`

### #175 拆分远端排队、执行、构建和复验耗时

验收要求：指标与 GitHub 时间戳可核对，缺失阶段显示未知。

来源：`adaptive_rsi/telemetry.py`

### #176 远端临时分支提供只读清理预览

验收要求：预览不执行删除，仍被 PR 或恢复点引用的分支得到保护。

来源：`adaptive_rsi/delivery.py`

### #177 Issue 更新显示需求差异和来源链

验收要求：保留原文和来源时间，未获授权的评论不改变任务权限。

来源：`adaptive_rsi/runtime.py`

### #178 区分 GitHub 读取与写入权限的准备度

验收要求：只读令牌不会被误报为所有交付可用，诊断不执行测试写入。

来源：`adaptive_rsi/readiness.py`

### #179 结果未知的远端派发提供人工核对界面

验收要求：延迟出现及多个匹配运行时不盲目重发，人工结论留审计记录。

来源：`adaptive_rsi/release.py`

### #180 将 GitHub Enterprise 支持作为明确扩展项

验收要求：在测试 Enterprise 主机完成读写流程，公共站点令牌不发送到其他主机。

来源：`adaptive_rsi/delivery.py`

### #181 长日志增加搜索、时间戳和阶段锚点

验收要求：大日志下查找响应有界，命中上下文和原始内容一致。

来源：`adaptive_rsi/web/app.js`

### #187 保存的视图可选择跨浏览器同步

验收要求：在另一浏览器恢复同一视图，私有筛选不意外暴露给其他用户。

来源：`adaptive_rsi/web/app.js`

### #188 刷新列表保持焦点并限制读屏重复播报

验收要求：纯键盘和读屏测试覆盖自动刷新、删除及分页，焦点不丢到页面顶部。

来源：`adaptive_rsi/web/app.js`

### #192 增加真实签名桌面 N 到 N+1 升级端到端测试

验收要求：正确包成功升级，坏签名、断电窗口和启动失败各有经过验证的安全结果。

来源：`scripts/release_smoke.py`

### #197 在副作用与回执间的精确窗口注入崩溃

验收要求：每个窗口重启后既不重复外部副作用，也不把未知结果误报完成。

来源：`tests/test_audit_round5.py`

### #200 审计完成项链接具体实现和验证证据——本次交接收尾已完成

验收要求：随机抽取完成项能直接重放验收，未覆盖边界明确列为残余限制。

来源：`docs/validation/audit-20261027-next-100.json`

## 24 项部分完成及剩余要求

### #15 桌面远程入口拒绝当前 API v2 服务

完整验收要求：按受支持版本及能力协商；0.5.124 对接当前服务必须成功。

残余边界（保留台账原文）：v1/v2 accepted; real remote compatibility and capability negotiation remain.

已有实现：`desktop/src-tauri/src/main.rs`

尚无已链接验收入口，不可据此视为完成。

### #18 输入上下文预算错误绑定输出上限

完整验收要求：分别计算输入上下文与响应限制；合法长输入可用，超上下文输入有明确诊断。

残余边界（保留台账原文）：Context/output separated; metadata accuracy remains.

已有实现：`adaptive_rsi/native.py`

尚无已链接验收入口，不可据此视为完成。

### #25 迁移校验和与迁移内容无关

完整验收要求：hash 不可变迁移正文并记录版本；迁移内容变化必须被检测。

残余边界（保留台账原文）：SQL migration bytes and ordered history are checked before mutation; sealed bootstrap is not repeated; additive SQL and ledger roll back together. Legacy 1/2 hashes and transaction-external bootstrap DDL remain historical boundaries. Compatibility format stays v2. Installed wheel resource/startup verified.

已有实现：`adaptive_rsi/store.py`, `adaptive_rsi/schema_migrations.py`, `adaptive_rsi/migrations/003_seal_bootstrap.sql`

已有验证入口：`python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store`

### #32 更新包驻留内存可能形成显著峰值

完整验收要求：测接近上限包的 RSS；优先落盘并设置可观测内存边界。

残余边界（保留台账原文）：Disk staging added; verify/install still load one bounded buffer; peak RSS measurement remains.

已有实现：`desktop/src-tauri/src/native_update.rs`

尚无已链接验收入口，不可据此视为完成。

### #33 空间预检可能检查了不同的磁盘卷

完整验收要求：把 app、cache、临时目录放不同卷测试；各必需卷均做空间检查。

残余边界（保留台账原文）：Cache and executable volumes checked; extraction-volume proof remains.

已有实现：`desktop/src-tauri/src/native_update.rs`

尚无已链接验收入口，不可据此视为完成。

### #35 已有下载包时检查直接返回旧状态

完整验收要求：验证撤回及连续发布场景；安装前重新确认版本可用性并允许替换缓存。

残余边界（保留台账原文）：Feed refreshed before reuse/install; withdrawal regression remains.

已有实现：`desktop/src-tauri/src/native_update.rs`

尚无已链接验收入口，不可据此视为完成。

### #44 重用既有 PR 时需要区分已关闭状态

完整验收要求：模拟同 head 的 closed PR；不能报告新 PR 已打开，应明确重开或新建策略。

残余边界（保留台账原文）：Closed PR reuse blocked; publication regression remains.

已有实现：`adaptive_rsi/delivery.py`

尚无已链接验收入口，不可据此视为完成。

### #56 数据库初始化早于运行实例目录锁

完整验收要求：同时启动两实例并注入迁移；拒绝的实例不能先修改数据库。

残余边界（保留台账原文）：Service entry points claim ownership before initialization; standalone Store utilities and library callers still require a migration ownership contract.

已有实现：`adaptive_rsi/runtime.py`, `adaptive_rsi/cli.py`, `adaptive_rsi/desktop.py`

已有验证入口：`python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_service_ownership_is_acquired_before_database_initialization`

### #58 数据库恢复仍含案例的旧绝对路径

完整验收要求：恢复到不同目录后执行只读案例检查；路径映射明确，不访问原副本。

残余边界（保留台账原文）：Cases invalidated/remapped on restore; business restore drill remains.

已有实现：`adaptive_rsi/storage_tools.py`

尚无已链接验收入口，不可据此视为完成。

### #59 认证过期清理可能在外层锁内执行慢关闭

完整验收要求：注入慢 worker.close 及并发取消；其他会话状态读取保持响应。

残余边界（保留台账原文）：Slow close moved outside outer lock; concurrency regression remains.

已有实现：`adaptive_rsi/auth_sessions.py`

尚无已链接验收入口，不可据此视为完成。

### #64 已签名更新包持久化缓存与断点续传

完整验收要求：在下载及就绪阶段重启应用，安全恢复下载或使用已验证缓存，损坏缓存必须重下。

残余边界（保留台账原文）：Verified complete packages persist; partial resume not implemented.

已有实现：`desktop/src-tauri/src/native_update.rs`

尚无已链接验收入口，不可据此视为完成。

### #65 拆分更新请求连接及数据传输时限

完整验收要求：注入 DNS、连接及慢响应故障，各阶段有可解释的时限且可取消。

残余边界（保留台账原文）：Separate transport deadlines implemented; injected fault matrix remains.

已有实现：`desktop/src-tauri/src/native_update.rs`

尚无已链接验收入口，不可据此视为完成。

### #77 升级确认说明本机客户端与运行服务的作用范围

完整验收要求：本机自有、已有本机服务和远端三种模式分别显示准确影响。

残余边界（保留台账原文）：Scope text implemented; three-mode browser proof remains.

已有实现：`adaptive_rsi/web/native-update.js`

尚无已链接验收入口，不可据此视为完成。

### #90 调度公平性采用滚动窗口与实际资源消耗

完整验收要求：长短任务混合模拟中报告等待分位数和权重兑现程度，再选择策略。

残余边界（保留台账原文）：Rolling worker-time accounting and starvation priority implemented; synthetic mixed-duration workload covered, production waiting quantiles and resource-pressure admission remain separate.

已有实现：`adaptive_rsi/runtime.py`

已有验证入口：`python3 -B -m unittest tests.test_audit_round6.AuditResourceFairnessTest tests.test_audit_round6.AuditResourceWorkloadTest`

### #99 提供归档恢复与项目重定位流程

完整验收要求：移动仓库并恢复归档后历史仍可访问，活动任务及远端身份重新校验。

残余边界（保留台账原文）：Explicit paused project restoration implemented; relocation remains.

已有实现：`adaptive_rsi/runtime.py`

尚无已链接验收入口，不可据此视为完成。

### #110 将建表和回填迁移按版本分段执行

完整验收要求：从各历史 fixture 升级，第二次启动不重复回填，未来版本仍被拒绝。

残余边界（保留台账原文）：SQL migration bytes and ordered history are checked before mutation; sealed bootstrap is not repeated; additive SQL and ledger roll back together. Legacy 1/2 hashes and transaction-external bootstrap DDL remain historical boundaries. Compatibility format stays v2. Installed wheel resource/startup verified.

已有实现：`adaptive_rsi/store.py`, `adaptive_rsi/schema_migrations.py`, `adaptive_rsi/migrations/003_seal_bootstrap.sql`

已有验证入口：`python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store`

### #117 在线备份增加整体时限和空间余量控制

完整验收要求：高写入与低空间实验中有明确终态，旧恢复点不受影响。

残余边界（保留台账原文）：Backup timeout/reserve implemented; disk-pressure acceptance remains.

已有实现：`adaptive_rsi/storage_tools.py`

尚无已链接验收入口，不可据此视为完成。

### #142 实验清单包含真实源码树及完整输入摘要

完整验收要求：改动任一实验输入都会改变对应摘要，离线核对能发现不一致。

残余边界（保留台账原文）：Trees/overlay/environment sealed; reproducibility proof remains.

已有实现：`adaptive_rsi/evolution.py`

尚无已链接验收入口，不可据此视为完成。

### #145 实验运行前封存指标、样本量及停止规则

完整验收要求：修改实验规则产生新版本，报告明确区分预设和探索性分析。

残余边界（保留台账原文）：Plan sealed before trials; tamper/early-stop regression remains.

已有实现：`adaptive_rsi/evolution.py`

尚无已链接验收入口，不可据此视为完成。

### #182 折叠日志真正按展开加载或虚拟渲染

完整验收要求：大证据样本下首屏时间和内存改善，展开内容完整且可导出。

残余边界（保留台账原文）：Serialization deferred until expansion; pagination/memory measurement remains.

已有实现：`adaptive_rsi/web/app.js`

尚无已链接验收入口，不可据此视为完成。

### #191 精确固定 Rust 和 Python 构建工具版本

完整验收要求：同提交在相同输入下可重建，升级工具链通过独立可审查变更完成。

残余边界（保留台账原文）：Build tools pinned; reproducibility proof remains.

已有实现：`.github/workflows/release-verify.yml`

尚无已链接验收入口，不可据此视为完成。

### #193 扩展 Python 边界版本的关键路径覆盖

完整验收要求：两个边界版本都跑真实关键路径，跳过的环境依赖明确记录。

残余边界（保留台账原文）：Boundary matrix expanded; remote results remain.

已有实现：`.github/workflows/checks.yml`

尚无已链接验收入口，不可据此视为完成。

### #199 SBOM 从真实锁定依赖生成精确清单

完整验收要求：抽查打包中的依赖与 SBOM 对齐，发布产物和 provenance 指向同一清单。

残余边界（保留台账原文）：Locked crate hashes and Python inventory implemented; Rust licenses/alignment remain.

已有实现：`scripts/package_desktop.py`

尚无已链接验收入口，不可据此视为完成。

### #201 验证分层、慢测试定位、并行发版及发布排队治理

完整验收要求：See ledger.

残余边界（保留台账原文）：Independent release jobs, measured selector timing, malformed-cache fallback and strict mechanical-metadata ordinary-CI path implemented. Final exact-head full release verification remains mandatory. Remote timing comparison and final release queue behavior remain unverified.

已有实现：`.github/workflows/release-verify.yml`, `.github/workflows/checks.yml`, `scripts/classify_changes.py`, `scripts/release.py`, `scripts/test_parallel.py`

已有验证入口：`python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel tests.test_release`

## 本地提交检查点

以下提交均未推送；当前索引与交接提交在这些之后。

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

## 接手后建议顺序（须先获得用户继续安排）

1. 先读交接、JSON 台账和 base 到 HEAD 的完整差异，核对分支与状态，保护用户文件。
2. 优先核实真实更新/恢复、迁移与所有权等高风险边界和部分完成项缺少的验收；按模块批量处理剩余项，不把缺少证据的情况改成完成。
3. 开发过程中按影响范围验证；范围完成后固定提交，统一跑 Python、Node、浏览器、Rust、打包、文档和契约，再处理确实失败项，不为每处微小改动反复跑所有检查。
4. 完整审查整个分支，再在用户允许继续后走受保护 PR 和精确提交的远端发布门禁，使用新版本，不能覆盖 v0.5.124 既有资产。
5. 发版证据具备后才能验证并宣称真实签名本地升级和恢复。IPC/HTTP/FakeAgent 测试只验证基础设施行为，不证明真实模型能力提升。

维护台账：`python3 -B scripts/check_audit_ledger.py --write`，然后 `python3 -B scripts/check_audit_ledger.py`。重放入口不等于通过证明。继续遵守中文提交、中文默认界面并同步英文 i18n、面向用户文档中英配对。
