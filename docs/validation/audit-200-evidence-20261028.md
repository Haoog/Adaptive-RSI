# 审计验收重放索引

[English](audit-200-evidence-20261028.en.md)

由 JSON 台账生成。命令是验收重放入口，不代表其在当前提交均已通过；发版前须核对残余边界及最终精确提交 CI。

| ID | 状态 | 项目 | 实现 | 重放 / 产物 |
| --- | --- | --- | --- | --- |
| 1 | 已实现 | 免费模型诊断调用了不存在的方法 | [adaptive_rsi/model_center.py](../../adaptive_rsi/model_center.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_diagnostics_never_calls_a_model` |
| 2 | 已实现 | 连接创建失败泄漏数据库容量名额 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_failed_connection_open_returns_capacity` |
| 3 | 已实现 | 备份轮换可能删除最后一个恢复点 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_retention_keeps_last_expired_valid_backup` |
| 4 | 已实现 | 损坏的新备份可以淘汰正常旧备份 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_corrupt_new_backup_cannot_displace_valid_backup` |
| 5 | 已实现 | 半开恢复允许多个调用者同时进入 | [adaptive_rsi/provider_health.py](../../adaptive_rsi/provider_health.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_half_open_lease_admits_one_attempt` |
| 6 | 已实现 | 旧成功仍可清除新的 Retry-After | [adaptive_rsi/provider_health.py](../../adaptive_rsi/provider_health.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_late_success_cannot_clear_newer_failure` |
| 7 | 已实现 | 归档项目仍能被重新启用并接收任务 | [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_archived_project_rejects_resume_and_new_goals` |
| 8 | 已实现 | 项目配置版本未涵盖继承的全局配置 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_effective_revision_changes_with_inherited_globals` |
| 9 | 已实现 | 任务合并丢失预约时间、紧急度及优先级 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_group_preserves_scheduling_constraints` |
| 10 | 已实现 | 普通界面优化被强制判为性能改进 | [adaptive_rsi/workflow.py](../../adaptive_rsi/workflow.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_explicit_ui_kind_does_not_require_performance_proof` |
| 11 | 已实现 | 契约检查只验证数组第一项 | [scripts/check_api_contract.py](../../scripts/check_api_contract.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_contract_validates_tail_and_numeric_bounds` |
| 12 | 已实现 | 契约检查忽略数值范围约束 | [scripts/check_api_contract.py](../../scripts/check_api_contract.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_contract_validates_tail_and_numeric_bounds` |
| 13 | 已实现 | wheel 缺少契约接口依赖的 schema | [pyproject.toml](../../pyproject.toml) | `python3 -B scripts/check_wheel_install.py --output docs/validation/wheel-install-20261028.json` <br> [docs/validation/wheel-install-20261028.json](../../docs/validation/wheel-install-20261028.json) |
| 14 | 已实现 | 更新状态刷新会反复销毁策略控件 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | `node --test tests/web_native_update.cjs` |
| 15 | 部分完成 | 桌面远程入口拒绝当前 API v2 服务 | [desktop/src-tauri/src/main.rs](../../desktop/src-tauri/src/main.rs) | — |
| 16 | 已实现 | 契约 CI 验证的是手写样本 | [scripts/check_api_contract.py](../../scripts/check_api_contract.py) | `python3 -B scripts/check_api_contract.py` |
| 17 | 已实现 | 全局坏 JSON 的错误回包再次读取坏 JSON | [adaptive_rsi/server.py](../../adaptive_rsi/server.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_malformed_global_configuration_still_has_a_repair_response` |
| 18 | 部分完成 | 输入上下文预算错误绑定输出上限 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) | — |
| 19 | 已实现 | 名为任务耗时的模型实际采集 SQL 耗时 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_task_duration_model_uses_actual_runs` |
| 20 | 已实现 | 桌面请求取消只结束网页等待 | [adaptive_rsi/web/desktop.js](../../adaptive_rsi/web/desktop.js) <br> [desktop/src-tauri/src/transport.rs](../../desktop/src-tauri/src/transport.rs) | `node --test tests/web_desktop_transport.cjs` <br> `cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml --bin adaptive-rsi-desktop` |
| 21 | 已实现 | 检查失败也会推迟下一次自动更新六小时 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | `node --test tests/web_native_update.cjs` |
| 22 | 已实现 | 备份验证的符号链接拒绝检查失效 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_backup_validation_rejects_input_symlink` |
| 23 | 已实现 | POST 请求日志丢失错误码 | [adaptive_rsi/server.py](../../adaptive_rsi/server.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_post_failure_log_preserves_safe_error_code` |
| 24 | 已实现 | JUnit 摘要无法处理常规多行 XML | [adaptive_rsi/evaluator.py](../../adaptive_rsi/evaluator.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_multiline_multisuite_junit_summary` |
| 25 | 部分完成 | 迁移校验和与迁移内容无关 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/schema_migrations.py](../../adaptive_rsi/schema_migrations.py) <br> [adaptive_rsi/migrations/003_seal_bootstrap.sql](../../adaptive_rsi/migrations/003_seal_bootstrap.sql) | `python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store` |
| 26 | 已实现 | 请求日志存在重复同列索引 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/migrations/003_seal_bootstrap.sql](../../adaptive_rsi/migrations/003_seal_bootstrap.sql) | `python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest.test_request_history_has_one_time_index_and_query_uses_it` |
| 27 | 已实现 | 状态快照只包住部分返回数据 | [adaptive_rsi/server.py](../../adaptive_rsi/server.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_state_configuration_and_revision_share_one_read_snapshot` |
| 28 | 已实现 | 后台异常回执本身可能再次写库失败 | [adaptive_rsi/operations.py](../../adaptive_rsi/operations.py) | `python3 -B -m unittest tests.test_audit_round6.AuditOperationPersistenceTest` |
| 29 | 已实现 | 重新接入已归档路径返回隐藏旧项目 | [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_readding_archived_project_is_explicit_and_restore_stays_paused` |
| 30 | 已实现 | 排空更新租约可能在长任务结束前失效 | [adaptive_rsi/lifecycle.py](../../adaptive_rsi/lifecycle.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_update_drain_renews_only_with_owner_lease` |
| 31 | 已实现 | 安装阶段崩溃可能留下不自动过期的屏障 | [adaptive_rsi/lifecycle.py](../../adaptive_rsi/lifecycle.py) <br> [adaptive_rsi/desktop.py](../../adaptive_rsi/desktop.py) <br> [desktop/src-tauri/src/main.rs](../../desktop/src-tauri/src/main.rs) | `python3 -B -m unittest tests.test_update_admission tests.test_desktop_runtime tests.test_audit_round5 tests.test_audit_round6` <br> `cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml --bin adaptive-rsi-desktop` |
| 32 | 部分完成 | 更新包驻留内存可能形成显著峰值 | [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs) | — |
| 33 | 部分完成 | 空间预检可能检查了不同的磁盘卷 | [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs) | — |
| 34 | 待做 | 原生更新缺少启动失败后的完整恢复证据 | — | — |
| 35 | 部分完成 | 已有下载包时检查直接返回旧状态 | [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs) | — |
| 36 | 已实现 | 更新策略保存失败没有反馈 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | `node --test tests/web_native_update.cjs` |
| 37 | 已实现 | 批量请求超时后重试会更换幂等键 | [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) <br> [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 38 | 已实现 | 批量按钮缺少执行期间的防重入状态 | [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) <br> [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 39 | 已实现 | 批量未知回执没有明确的核对终结流程 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) <br> [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 40 | 已实现 | 冲突预览只比较完全相同的路径字符串 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) <br> [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 41 | 已实现 | 分组完成后原任务状态与成果关联需核实 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) <br> [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 42 | 已实现 | 部署意图和目标所有权分两次事务保存 | [adaptive_rsi/release.py](../../adaptive_rsi/release.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_dispatch_intent_and_owner_rollback_together` |
| 43 | 已实现 | 健康检查总时限仍可能被阻塞读取超出 | [adaptive_rsi/release.py](../../adaptive_rsi/release.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_health_transport_bounds_stalled_body_and_cancels_real_connection` |
| 44 | 部分完成 | 重用既有 PR 时需要区分已关闭状态 | [adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py) | — |
| 45 | 已实现 | 本地冷却拒绝可能被计为新的供应商故障 | [adaptive_rsi/agent.py](../../adaptive_rsi/agent.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_account_retry_after_is_shared_without_inflating_failures` |
| 46 | 已实现 | 同账号跨项目仍可能分别触发限流 | [adaptive_rsi/provider_health.py](../../adaptive_rsi/provider_health.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_account_retry_after_is_shared_without_inflating_failures` |
| 47 | 已实现 | 畸形用量容器可能中断有效模型响应 | [adaptive_rsi/usage.py](../../adaptive_rsi/usage.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_malformed_usage_does_not_discard_valid_tool_response` |
| 48 | 待做 | 监控窗口结束后不再检测后续漂移 | — | — |
| 49 | 已实现 | 环境失败前消耗样本曝光额度的口径需核实 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_case_exposure_reservation_is_atomic_and_idempotent` |
| 50 | 已实现 | 胜率诊断混用缺陷家族和重复试验计数 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_paired_statistics_do_not_change_with_correlated_repeats` |
| 51 | 已实现 | 并发导入相同样本的跨 split 去重窗口 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_concurrent_imports_cannot_put_one_case_in_two_splits` |
| 52 | 已实现 | Go 测试摘要可能同时计算父测试和子测试 | [adaptive_rsi/evaluator.py](../../adaptive_rsi/evaluator.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_go_summary_counts_top_level_tests_separately_from_subtests` |
| 53 | 已实现 | 未知 token 的实验可能绕过成本比判断 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 54 | 已实现 | 宿主文件工具存在路径检查与打开的时间窗口 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 55 | 已实现 | 长搜索单次工具内没有细粒度取消检查 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 56 | 部分完成 | 数据库初始化早于运行实例目录锁 | [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/cli.py](../../adaptive_rsi/cli.py) <br> [adaptive_rsi/desktop.py](../../adaptive_rsi/desktop.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_service_ownership_is_acquired_before_database_initialization` |
| 57 | 已实现 | 轮询请求日志可能造成持续写放大 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRequestSamplingTest` |
| 58 | 部分完成 | 数据库恢复仍含案例的旧绝对路径 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | — |
| 59 | 部分完成 | 认证过期清理可能在外层锁内执行慢关闭 | [adaptive_rsi/auth_sessions.py](../../adaptive_rsi/auth_sessions.py) | — |
| 60 | 已实现 | 两个项目部署同一目标的所有权不互斥 | [adaptive_rsi/release.py](../../adaptive_rsi/release.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_target_ownership_is_shared_and_migrates_legacy_receipts` |
| 61 | 已实现 | 桌面和引擎更新策略采用同一套版本范围语义 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | `node --test tests/web_native_update.cjs` |
| 62 | 待做 | 稳定版增加可暂停的分批更新机制 | — | — |
| 63 | 待做 | 下载策略可考虑网络和电源状态 | — | — |
| 64 | 部分完成 | 已签名更新包持久化缓存与断点续传 | [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs) | — |
| 65 | 部分完成 | 拆分更新请求连接及数据传输时限 | [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs) | — |
| 66 | 待做 | 建立更新签名公钥轮换流程 | — | — |
| 67 | 待做 | 更新前显示操作系统和架构兼容性 | — | — |
| 68 | 待做 | 建立明确的平台资产支持矩阵 | — | — |
| 69 | 待做 | 为后台下载提供可选带宽限制 | — | — |
| 70 | 待做 | 更新进度区分速度、校验及替换阶段 | — | — |
| 71 | 待做 | 空间需求按实际资产估算 | — | — |
| 72 | 待做 | 允许用户选择关窗、后台驻留和登录启动行为 | — | — |
| 73 | 待做 | 增加用户主动启用的崩溃诊断包 | — | — |
| 74 | 待做 | 按运行实例保存导航和视图偏好 | — | — |
| 75 | 待做 | 增加桌面常用操作的原生菜单与快捷键 | — | — |
| 76 | 待做 | 提供代理和企业证书连接诊断 | — | — |
| 77 | 部分完成 | 升级确认说明本机客户端与运行服务的作用范围 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | — |
| 78 | 已实现 | 自动安装增加维护时间窗口 | [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js) | `node --test tests/web_native_update.cjs` |
| 79 | 待做 | 提供可导出的升级回执 | — | — |
| 80 | 待做 | 按功能能力协商和界面降级 | — | — |
| 81 | 已实现 | 任务依赖从单前置扩展为受控 DAG | [adaptive_rsi/task_dependencies.py](../../adaptive_rsi/task_dependencies.py) <br> [adaptive_rsi/migrations/006_dependency_dag.sql](../../adaptive_rsi/migrations/006_dependency_dag.sql) <br> [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `python3 -B -m unittest tests.test_dependency_dag` <br> `node tests/browser_dependency_dag.cjs` <br> `node --test tests/web_*.cjs` |
| 82 | 已实现 | 取消前置任务时预览受影响的后继 | [adaptive_rsi/task_lifecycle.py](../../adaptive_rsi/task_lifecycle.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_task_lifecycle` <br> `node tests/browser_task_detail.cjs` <br> `node tests/browser_revision_flow.cjs` |
| 83 | 待做 | 合并任务的每条验收要求链接独立证据 | — | — |
| 84 | 待做 | 按预计验证成本建议拆分过大任务组 | — | — |
| 85 | 待做 | 预览共享检查收益和失败重做代价 | — | — |
| 86 | 待做 | 推荐任务簇但保留用户授权 | — | — |
| 87 | 已实现 | 批量回执可检索、导出和重新打开 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) <br> [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 88 | 已实现 | 为合并任务失败提供拆分重做流程 | [adaptive_rsi/batching.py](../../adaptive_rsi/batching.py) <br> [adaptive_rsi/task_groups.py](../../adaptive_rsi/task_groups.py) <br> [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js) | `python3 -B -m unittest tests.test_task_groups` <br> `node tests/browser_group_retry.cjs` <br> `node tests/browser_batch.cjs` |
| 89 | 待做 | 用真实运行阶段样本估计排队时间区间 | — | — |
| 90 | 部分完成 | 调度公平性采用滚动窗口与实际资源消耗 | [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) | `python3 -B -m unittest tests.test_audit_round6.AuditResourceFairnessTest tests.test_audit_round6.AuditResourceWorkloadTest` |
| 91 | 已实现 | 让资源优先级和等待指标发挥作用 | [adaptive_rsi/resources.py](../../adaptive_rsi/resources.py) <br> [adaptive_rsi/agent.py](../../adaptive_rsi/agent.py) <br> [adaptive_rsi/evaluator.py](../../adaptive_rsi/evaluator.py) <br> [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_resource_priority tests.test_comprehensive_audit tests.test_audit_round6` |
| 92 | 已实现 | 加入磁盘和内存压力准入判断 | [adaptive_rsi/resource_pressure.py](../../adaptive_rsi/resource_pressure.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/config.py](../../adaptive_rsi/config.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `python3 -B -m unittest tests.test_resource_pressure tests.test_scheduler_isolation tests.test_schedule_consistency tests.test_comprehensive_audit tests.test_audit_round6` <br> `node tests/browser_resource_pressure.cjs` <br> `node --test tests/web_*.cjs` |
| 93 | 待做 | 用到期队列减少大项目数下的全量轮询 | — | — |
| 94 | 待做 | 展示正常关闭的阶段和剩余工作 | — | — |
| 95 | 待做 | 按阶段配置时限并记录超时位置 | — | — |
| 96 | 待做 | 安全复用已封存的只读阶段检查点 | — | — |
| 97 | 已实现 | 记录人工干预的结构化原因和效果 | [adaptive_rsi/task_lifecycle.py](../../adaptive_rsi/task_lifecycle.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `python3 -B -m unittest tests.test_task_lifecycle` <br> `node tests/browser_task_detail.cjs` <br> `node tests/browser_revision_flow.cjs` |
| 98 | 待做 | 预约任务明确时区与夏令时规则 | — | — |
| 99 | 部分完成 | 提供归档恢复与项目重定位流程 | [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) | — |
| 100 | 已实现 | 需求修订显示差异及重跑影响 | [adaptive_rsi/task_revisions.py](../../adaptive_rsi/task_revisions.py) <br> [adaptive_rsi/migrations/004_revision_snapshots.sql](../../adaptive_rsi/migrations/004_revision_snapshots.sql) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `python3 -B -m unittest tests.test_task_lifecycle` <br> `node tests/browser_task_detail.cjs` <br> `node tests/browser_revision_flow.cjs` |
| 101 | 待做 | 为分页提供稳定游标或快照版本 | — | — |
| 102 | 已实现 | 后台操作及批量回执增加保留策略 | [adaptive_rsi/maintenance.py](../../adaptive_rsi/maintenance.py) | `python3 -B -m unittest tests.test_audit_round6.AuditMaintenanceRetentionTest` |
| 103 | 已实现 | 备份清单计算 hash 改为有界流式读取 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_storage_manifest_is_streamed_and_cached_with_explicit_refresh` |
| 104 | 已实现 | 存储概览采用可失效缓存或增量清单 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_storage_manifest_is_streamed_and_cached_with_explicit_refresh` |
| 105 | 已实现 | 归档压缩级别与资源预算可选 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) <br> [adaptive_rsi/cli.py](../../adaptive_rsi/cli.py) | `python3 -B -m unittest tests.test_audit_round6.AuditStorageBudgetsTest` |
| 106 | 已实现 | 增加 WAL 检查点状态与长读事务诊断 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/maintenance.py](../../adaptive_rsi/maintenance.py) | `python3 -B -m unittest tests.test_audit_round6.AuditStorageBudgetsTest.test_passive_checkpoint_reports_reader_pin_without_waiting` |
| 107 | 已实现 | 为大历史规模设查询和响应性能预算 | [scripts/benchmark_history.py](../../scripts/benchmark_history.py) <br> [.github/workflows/checks.yml](../../.github/workflows/checks.yml) | `python3 -B -m unittest tests.test_history_budget` <br> `python3 -B scripts/benchmark_history.py --tasks 50000 --samples 3 --enforce --output docs/validation/history-performance-20261028.json` <br> [docs/validation/history-performance-20261028.json](../../docs/validation/history-performance-20261028.json) |
| 108 | 待做 | 完善项目及运行关系的外键覆盖 | — | — |
| 109 | 已实现 | 关系巡检覆盖更多证据表 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_store_observability tests.test_store tests.test_audit_round6 tests.test_server_execution` <br> `python3 -B scripts/soak.py --iterations 30 --runtime-cycles 10` <br> [docs/validation/store-metrics-20261028.json](../../docs/validation/store-metrics-20261028.json) |
| 110 | 部分完成 | 将建表和回填迁移按版本分段执行 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/schema_migrations.py](../../adaptive_rsi/schema_migrations.py) <br> [adaptive_rsi/migrations/003_seal_bootstrap.sql](../../adaptive_rsi/migrations/003_seal_bootstrap.sql) | `python3 -B -m unittest tests.test_audit_round6.AuditMigrationHistoryTest tests.test_audit_round5 tests.test_store` |
| 111 | 已实现 | 提供数据库不可写或损坏时的只读恢复模式 | [adaptive_rsi/recovery_diagnostics.py](../../adaptive_rsi/recovery_diagnostics.py) <br> [adaptive_rsi/cli.py](../../adaptive_rsi/cli.py) | `python3 -B -m unittest tests.test_audit_round6.AuditOfflineRecoveryTest` |
| 112 | 已实现 | 配置审计增加来源、脱敏差异及安全撤销 | [adaptive_rsi/config_history.py](../../adaptive_rsi/config_history.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/dashboard.py](../../adaptive_rsi/dashboard.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_config_history tests.test_model_center tests.test_store tests.test_audit_round6` <br> `node tests/browser_config_history.cjs` <br> `node --test tests/web_*.cjs` |
| 113 | 待做 | 为 settings 命名空间声明类型、版本及 TTL | — | — |
| 114 | 已实现 | 增加不含敏感参数的慢 SQL 诊断 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_store_observability tests.test_store tests.test_audit_round6 tests.test_server_execution` <br> `python3 -B scripts/soak.py --iterations 30 --runtime-cycles 10` <br> [docs/validation/store-metrics-20261028.json](../../docs/validation/store-metrics-20261028.json) |
| 115 | 已实现 | 连接池等待增加分位数与超时指标 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_store_observability tests.test_store tests.test_audit_round6 tests.test_server_execution` <br> `python3 -B scripts/soak.py --iterations 30 --runtime-cycles 10` <br> [docs/validation/store-metrics-20261028.json](../../docs/validation/store-metrics-20261028.json) |
| 116 | 已实现 | 将完整关系检查与轻量状态读取分开 | [adaptive_rsi/store.py](../../adaptive_rsi/store.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py) <br> [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | `python3 -B -m unittest tests.test_store_observability tests.test_store tests.test_audit_round6 tests.test_server_execution` <br> `python3 -B scripts/soak.py --iterations 30 --runtime-cycles 10` <br> [docs/validation/store-metrics-20261028.json](../../docs/validation/store-metrics-20261028.json) |
| 117 | 部分完成 | 在线备份增加整体时限和空间余量控制 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) | — |
| 118 | 待做 | 清理动作增加可预览的短期回收区 | — | — |
| 119 | 已实现 | 归档增加内容清单及定期完整性验证 | [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py) <br> [adaptive_rsi/maintenance.py](../../adaptive_rsi/maintenance.py) | `python3 -B -m unittest tests.test_audit_round6.AuditStorageBudgetsTest tests.test_audit_round6.AuditMaintenanceRetentionTest` |
| 120 | 待做 | 定期隔离演练业务级恢复 | — | — |
| 121 | 待做 | 模型能力元数据加入来源、日期和人工覆盖 | — | — |
| 122 | 待做 | 为各类工具设置独立结果预算和分页 | — | — |
| 123 | 待做 | 输入 token 估计显示语言相关不确定范围 | — | — |
| 124 | 已实现 | 有界并行执行相互独立的只读工具 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_readonly_tools_overlap_but_return_in_call_order` |
| 125 | 已实现 | 文件写入采用原子替换并保留必要属性 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_atomic_write_preserves_original_after_disk_failure` |
| 126 | 已实现 | 按行流式读取大源文件 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_large_source_ranges_are_streamed` |
| 127 | 待做 | 索引读取 Git 忽略规则及用户排除项 | — | — |
| 128 | 待做 | 搜索增加路径、大小写及正则选项 | — | — |
| 129 | 待做 | 大工具输出保留为独立产物引用 | — | — |
| 130 | 待做 | 为可重试传输失败明确付费请求语义 | — | — |
| 131 | 已实现 | 记录供应商请求 ID 和脱敏调用关联 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/native_transport.py](../../adaptive_rsi/native_transport.py) <br> [adaptive_rsi/recovery.py](../../adaptive_rsi/recovery.py) | `python3 -B -m unittest tests.test_audit_round6.AuditModelDiagnosticsTest` |
| 132 | 待做 | 原生模型支持流式进度与首字节指标 | — | — |
| 133 | 已实现 | 持久化脱敏工具轨迹用于崩溃调查 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 134 | 已实现 | 把停止原因传入最终诊断和恢复建议 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/native_transport.py](../../adaptive_rsi/native_transport.py) <br> [adaptive_rsi/recovery.py](../../adaptive_rsi/recovery.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/i18n.js](../../adaptive_rsi/web/i18n.js) | `python3 -B -m unittest tests.test_audit_round6.AuditModelDiagnosticsTest` <br> `python3 -B scripts/check_i18n.py` |
| 135 | 待做 | 建立多协议畸形响应的统一契约矩阵 | — | — |
| 136 | 待做 | 显示 fallback 原因及共享故障域 | — | — |
| 137 | 待做 | 费用估计分开输入、输出及缓存价格 | — | — |
| 138 | 待做 | 明确费用币种、单位和订阅估值含义 | — | — |
| 139 | 待做 | 展示任务成本预测与实际偏差 | — | — |
| 140 | 已实现 | 凭据轮换后失效旧健康及探测证据 | [adaptive_rsi/provider_health.py](../../adaptive_rsi/provider_health.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_rotating_same_credential_reference_invalidates_probe_identity` |
| 141 | 已实现 | 评估样本按来源和任务规模分层选择 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_stratified_selection_covers_source_and_task_size` |
| 142 | 部分完成 | 实验清单包含真实源码树及完整输入摘要 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | — |
| 143 | 待做 | 最终晋升使用未参与候选选择的独立复核集 | — | — |
| 144 | 待做 | 将人工驳回和误报反馈用于校准监控 | — | — |
| 145 | 部分完成 | 实验运行前封存指标、样本量及停止规则 | [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py) | — |
| 146 | 待做 | 记录性能实验的环境噪声和重复测量 | — | — |
| 147 | 待做 | 用固定负例验证检查工具确实能抓到错误 | — | — |
| 148 | 待做 | 按验收要求设计反例而非只验证正常路径 | — | — |
| 149 | 待做 | 记录实际执行的测试 ID 和跳过原因 | — | — |
| 150 | 待做 | 采集有界标准测试及覆盖率产物 | — | — |
| 151 | 待做 | 缓存不可变的依赖准备环境 | — | — |
| 152 | 待做 | 性能提升同时约束关键副指标不退化 | — | — |
| 153 | 待做 | 数据集删除和重分类保留曝光历史 | — | — |
| 154 | 待做 | 晋升前展示策略差异和适用边界 | — | — |
| 155 | 已实现 | 按任务家族展示收益异质性 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 156 | 待做 | 导出可离线复现的封存实验包 | — | — |
| 157 | 待做 | 证据记录宿主工具精确版本 | — | — |
| 158 | 待做 | 建立 monorepo 检查依赖图 | — | — |
| 159 | 待做 | 策略和技能共同变化时提供归因分析 | — | — |
| 160 | 已实现 | 同时展示质量、成本和延迟权衡 | [adaptive_rsi/native.py](../../adaptive_rsi/native.py) <br> [adaptive_rsi/experiment_report.py](../../adaptive_rsi/experiment_report.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `python3 -B -m unittest tests.test_native_path_races tests.test_experiment_report tests.test_protocols tests.test_audit_round5 tests.test_audit_round6` <br> `node --test tests/web_*.cjs` <br> `node tests/browser_evolution.cjs` |
| 161 | 待做 | 区分合并队列入队与实际合并状态 | — | — |
| 162 | 待做 | 准备度检查兼容 GitHub rulesets | — | — |
| 163 | 待做 | 共享可信输入的 CI 环境准备 | — | — |
| 164 | 待做 | 早期受影响检查与最终完整门禁衔接 | — | — |
| 165 | 待做 | 合并后失败区分候选与其他主线提交影响 | — | — |
| 166 | 待做 | 区分停止本地跟踪和取消远端工作流 | — | — |
| 167 | 待做 | GitHub 主限流与次级限流统一退避 | — | — |
| 168 | 待做 | 上游强制推送和分叉基线提供恢复指引 | — | — |
| 169 | 待做 | 终态后处理使用可核对的持久化待办 | — | — |
| 170 | 待做 | 可选签名 webhook 降低远端轮询成本 | — | — |
| 171 | 已实现 | 部署健康要求连续成功或稳定观察窗口 | [adaptive_rsi/config.py](../../adaptive_rsi/config.py) <br> [adaptive_rsi/release.py](../../adaptive_rsi/release.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/index.html](../../adaptive_rsi/web/index.html) | `python3 -B -m unittest tests.test_deployment_observation tests.test_release tests.test_audit_round6` <br> `node tests/browser_deployment_health.cjs` <br> `node --test tests/web_*.cjs` |
| 172 | 已实现 | 可选多个健康端点和区域观察 | [adaptive_rsi/config.py](../../adaptive_rsi/config.py) <br> [adaptive_rsi/release.py](../../adaptive_rsi/release.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/index.html](../../adaptive_rsi/web/index.html) | `python3 -B -m unittest tests.test_deployment_observation tests.test_release tests.test_audit_round6` <br> `node tests/browser_deployment_health.cjs` <br> `node --test tests/web_*.cjs` |
| 173 | 已实现 | 部署回滚显式声明数据迁移兼容条件 | [adaptive_rsi/config.py](../../adaptive_rsi/config.py) <br> [adaptive_rsi/release.py](../../adaptive_rsi/release.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/index.html](../../adaptive_rsi/web/index.html) | `python3 -B -m unittest tests.test_deployment_observation` <br> `python3 -B -m unittest tests.test_release tests.test_backend_hardening` <br> `node tests/browser_deployment_health.cjs` <br> `node --test tests/web_*.cjs` |
| 174 | 待做 | 将工作流目标环境绑定到授权证据 | — | — |
| 175 | 待做 | 拆分远端排队、执行、构建和复验耗时 | — | — |
| 176 | 待做 | 远端临时分支提供只读清理预览 | — | — |
| 177 | 待做 | Issue 更新显示需求差异和来源链 | — | — |
| 178 | 待做 | 区分 GitHub 读取与写入权限的准备度 | — | — |
| 179 | 待做 | 结果未知的远端派发提供人工核对界面 | — | — |
| 180 | 待做 | 将 GitHub Enterprise 支持作为明确扩展项 | — | — |
| 181 | 待做 | 长日志增加搜索、时间戳和阶段锚点 | — | — |
| 182 | 部分完成 | 折叠日志真正按展开加载或虚拟渲染 | [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | — |
| 183 | 已实现 | 差异阅读增加文件内搜索及 hunk 定位 | [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) <br> [adaptive_rsi/web/style.css](../../adaptive_rsi/web/style.css) | `node tests/browser_experience.cjs` <br> `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_quoted_git_diff_paths_preserve_utf8_and_control_characters` <br> `node tests/browser_diff_mobile.cjs` |
| 184 | 已实现 | 展示阻塞原因的因果链 | [adaptive_rsi/blocking.py](../../adaptive_rsi/blocking.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `python3 -B -m unittest tests.test_blocking_causes tests.test_dependency_dag tests.test_store tests.test_experience tests.test_server_hardening` <br> `node tests/browser_blocking_causes.cjs` <br> `node --test tests/web_*.cjs` |
| 185 | 已实现 | 设置冲突保留本地与服务端两份并展示差异 | [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) | `node tests/browser_settings_conflicts.cjs` |
| 186 | 已实现 | 导航变化进入浏览器历史栈 | [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) | `node tests/browser_navigation_history.cjs` <br> `node tests/browser_experience.cjs` <br> `node tests/browser_notifications.cjs` <br> `node --test tests/web_*.cjs` |
| 187 | 待做 | 保存的视图可选择跨浏览器同步 | — | — |
| 188 | 待做 | 刷新列表保持焦点并限制读屏重复播报 | — | — |
| 189 | 已实现 | 手机差异阅读提供折叠和换行模式 | [adaptive_rsi/web/experience.js](../../adaptive_rsi/web/experience.js) <br> [adaptive_rsi/web/style.css](../../adaptive_rsi/web/style.css) | `node tests/browser_diff_mobile.cjs` <br> `node tests/browser_experience.cjs` <br> `node --test tests/web_*.cjs` |
| 190 | 已实现 | 动态错误码及字段路径统一本地化 | [adaptive_rsi/error_details.py](../../adaptive_rsi/error_details.py) <br> [adaptive_rsi/server.py](../../adaptive_rsi/server.py) <br> [adaptive_rsi/web/app.js](../../adaptive_rsi/web/app.js) <br> [adaptive_rsi/web/i18n.js](../../adaptive_rsi/web/i18n.js) | `python3 -B -m unittest tests.test_error_details tests.test_server_hardening tests.test_backend_hardening tests.test_audit_round4` <br> `node tests/browser_error_details.cjs` <br> `node --test tests/web_*.cjs` |
| 191 | 部分完成 | 精确固定 Rust 和 Python 构建工具版本 | [.github/workflows/release-verify.yml](../../.github/workflows/release-verify.yml) | — |
| 192 | 待做 | 增加真实签名桌面 N 到 N+1 升级端到端测试 | — | — |
| 193 | 部分完成 | 扩展 Python 边界版本的关键路径覆盖 | [.github/workflows/checks.yml](../../.github/workflows/checks.yml) | — |
| 194 | 已实现 | 按模块真实耗时分配并行测试 | [scripts/test_parallel.py](../../scripts/test_parallel.py) <br> [scripts/test_worker.py](../../scripts/test_worker.py) | `python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel tests.test_release` |
| 195 | 已实现 | 建立可追踪且有期限的偶发失败记录 | [scripts/test_parallel.py](../../scripts/test_parallel.py) | `python3 -B -m unittest tests.test_audit_round6.AuditRound6Test.test_failure_history_keeps_original_attempts_and_review_deadlines` |
| 196 | 已实现 | 资源 soak 扩展到调度、HTTP 与关闭交互 | [scripts/soak.py](../../scripts/soak.py) | `python3 -B scripts/soak.py --iterations 30 --runtime-cycles 10` |
| 197 | 待做 | 在副作用与回执间的精确窗口注入崩溃 | — | — |
| 198 | 已实现 | 文案检查扩展为语法级和动态错误覆盖 | [scripts/check_i18n_ast.cjs](../../scripts/check_i18n_ast.cjs) <br> [package.json](../../package.json) <br> [package-lock.json](../../package-lock.json) <br> [.github/workflows/checks.yml](../../.github/workflows/checks.yml) <br> [.github/workflows/release-verify.yml](../../.github/workflows/release-verify.yml) | `node scripts/check_i18n_ast.cjs` <br> `node --test tests/web_i18n_ast.cjs` <br> `node --test tests/web_*.cjs` <br> `python3 -B -m unittest tests.test_release.WorkflowConfigurationTest tests.test_validation_planning` |
| 199 | 部分完成 | SBOM 从真实锁定依赖生成精确清单 | [scripts/package_desktop.py](../../scripts/package_desktop.py) | — |
| 200 | 已实现 | 审计完成项链接具体实现和验证证据 | [scripts/check_audit_ledger.py](../../scripts/check_audit_ledger.py) <br> [scripts/check_wheel_install.py](../../scripts/check_wheel_install.py) <br> [tests/test_audit_ledger.py](../../tests/test_audit_ledger.py) <br> [docs/validation/audit-200-evidence-20261028.md](../../docs/validation/audit-200-evidence-20261028.md) <br> [docs/validation/audit-200-evidence-20261028.en.md](../../docs/validation/audit-200-evidence-20261028.en.md) | `python3 -B -m unittest tests.test_audit_ledger` <br> `python3 -B scripts/check_audit_ledger.py` <br> [docs/validation/wheel-install-20261028.json](../../docs/validation/wheel-install-20261028.json) |
| 201 | 部分完成 | 验证分层、慢测试定位、并行发版及发布排队治理 | [.github/workflows/release-verify.yml](../../.github/workflows/release-verify.yml) <br> [.github/workflows/checks.yml](../../.github/workflows/checks.yml) <br> [scripts/classify_changes.py](../../scripts/classify_changes.py) <br> [scripts/release.py](../../scripts/release.py) <br> [scripts/test_parallel.py](../../scripts/test_parallel.py) | `python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel tests.test_release` |

每项残余边界保留在 [JSON 台账](audit-200-implementation-20261028.json)。
