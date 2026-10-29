# 审计修复与验证记录

**简体中文** | [English](audit-reliability-20261030.en.md)

本批基于 `20800c456deef03cdd3aae903ba3fec14f4cc802`，合并处理 11 项缺陷与 4 项优化。缺陷编号沿用本轮隔离审计；下表给出实现入口与固定回归，不把模型评审意见当作测试结果。

| 编号 | 行为与边界 | 回归入口 |
| --- | --- | --- |
| B01 | 自动清理与手动预览均遵守运行、项目 hold；损坏值按保留处理。每个工件在写事务内重查，删除与新 hold 可按提交顺序解释。 | `tests.test_scheduler_isolation`、`tests.test_storage_acceptance` |
| B02 | 模型目录与调用预留共用连接及凭据代次身份；每次调用预留时冻结价格，未知值保持 NULL。已封存运行配置不等于锁定未来调用的价格。 | `tests.test_model_center` |
| B03 | 取消同事务撤销待批准候选和未领取的交付任务；批准与发布入口重查源任务。已发布历史保留。 | `tests.test_task_lifecycle`、`tests.test_runtime_safety` |
| B04 | 归档项目拒绝重试和需求修订，未留下队列、修订或事件写入。 | `tests.test_task_lifecycle` |
| B05 | 安全清理运行自有只读依赖目录，不沿符号链接修改或删除外部目标。 | `tests.test_scheduler_isolation` |
| B06 | 诊断明细的 SQLite/IO 保存失败不覆盖成功模型结果；准入状态与核心用量写入仍保留失败语义。 | `tests.test_operability` |
| B07 | 无检查生产者返回空对象；历史空列表兼容，若同时声称 pass 则在读模型降为证据不足。坏字段被隔离，其余证据与数据库原文保留。 | `tests.test_task_value_feedback`、`tests.test_comprehensive_audit` |
| B08 | 原合并与后续主线结果共用归属函数；缺少 SHA 的历史观测不猜测归属。 | `tests.test_task_value_feedback` |
| B09 | 流程指标各查询共享一个只读快照，嵌套快照不重开事务。 | `tests.test_task_value_feedback` |
| B10 | 用量失败保留的数据标明原范围和成功时间；明细及其分页沿用可见报表的成功请求范围。 | `tests/web_requests.cjs`、`tests/browser_usage.cjs` |
| B11 | 详情轮询显示持久失败提示与重试，连续失败退避并保留未提交评价草稿。 | `tests/browser_task_detail.cjs` |
| O01 | 命中先完整扫描身份，不建临时副本；复制到运行目录时单遍校验。共享目录锁保护正在复制的条目，淘汰使用非阻塞独占锁。 | `tests.test_audit_environment`、`tests.test_reliability` |
| O02 | HTTP 备份清单从已打开的描述符按 64 KiB 分块哈希。 | `tests.test_server_hardening` |
| O03 | Anthropic 能力上限与请求预算分开；旧 Claude 3.0 命名型号维持 4096，更具体规则优先。截断修复整次调用累计最多一次，所有请求的用量保留。 | `tests.test_native_audit_contracts` |
| O04 | 价格表单只提交相对打开快照发生变化的字段，清空使用 null；两个窗口修改不同字段不会互相覆盖。 | `tests/browser_model_center.cjs` |

另修复当前版本桌面 feed 双语更新说明，以及手机宽度下项目选择器的文本溢出。

## 本地固定验证

最终代码检查点：Python 全量 1266 项，其中跳过 10 项，四组退出码均为 0；JavaScript 375 项通过；Rust 51 项通过；Chrome 浏览器全部 36 个场景通过。文档配对与链接、i18n/AST、API 契约、CI 选择器、审计台账、插件打包及仓库外 wheel 安装检查通过。审计台账的旧计数仍为 123/53/25，本批不据此修改历史状态。

本地签名、回执与安装授权 harness 通过。它报告 `acceptance_scope=staging_only`，尚未实际执行真实签名 N→N+1 应用替换，不能将该结果称为真实自动更新全链路验收。发布流水线与本地安装结果在最终交付时分别核验。

## 同负载测量与限制

缓存测量固定 32 个文件、总计 8 MiB、每条路径 3 次，使用同一主机热文件系统。直接 capture/restore 中位 51.19 ms，旧缓存命中 68.96 ms，新缓存命中 55.36 ms；命中整树副本从 3 份降为 2 份。新缓存仍需完整读取源身份并复制，测量没有证明它快于直接路径。

隔离真实 HTTP 清单测量中，17,072,128 字节备份的 Python tracemalloc 峰值为 536,020 字节，哈希一致。这是 Python 跟踪分配，不是 RSS 或生产内存保证。

万任务合成读取基准通过 5000 ms 与单响应 8 MiB 门槛。以上测量不覆盖生产并发、冷磁盘、网络、真实模型效果或账单，不承诺生产提速或费用节省。4.x Claude 请求预算较旧策略提高，仍受能力值约束；未通过真实供应商调用验证。未新增普遍分类模型、投票或反例生成调用。
