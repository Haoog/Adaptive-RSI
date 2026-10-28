# 项目复查与验证耗时报告

[English](project-review-20261028.en.md)

本次审查基于 `e78aea5cc892010b86987bac8fc54254728370a4`（0.5.125），实现位于 `chore/audit-validation-feedback-20261028` 工作树。未提交、推送、合并、发布或更新本地应用；原有 `docs/research/` 保留。

共 39 条实质发现：P1 3 条、P2 30 条、P3 6 条；34 条有确定性探针或实际执行证据，5 条为调用链证据。工作树已处理 6 条，另外 33 条仍待修复。相关历史编号只表示重叠领域或旧验收未覆盖，不表示该项已由历史测试证明。

已有201项历史台账为114项已实现、56项部分完成、31项待做，尚余87项部分完成或待做；本次没有将它们重复加到新清单，也没有替旧记录重新认证。机器清单见 [JSON](project-review-20261028.json)。

## 优先处理

1. 备份验证未核对迁移账本，轮换可删除唯一可恢复旧快照。
2. 合并后主线改变已有测试时，旧harness覆盖新测试并被复用为完整检查。
3. 后续主线通过的反馈可解锁原失败合并SHA的发布或部署。
4. 模型只读诊断存在作用域错误和未定义dialog；认证损坏、关闭后凭据写入及更新排空也有可复现缺口。

前3项为P1，均由主agent重新运行隔离fixture复核。它们尚未修复，应先于进一步扩大验证缓存实施；复现仅使用临时数据库、临时Git仓库和外部服务替身。

## 已完成的验证提速

| 检查 | 原结果 | 当前结果 |
| --- | --- | --- |
| 全量Python，原4组不变 | 1075项，310.8秒 | 1078项，131.7秒；执行1068、跳过10 |
| 短Python命令中位数，12次 | 87.81 ms | 39.25 ms |
| HTTP夹具关闭中位数，6次 | 501.18 ms | 11.13 ms |
| Node全套 | 354项，1.46秒 | 356项，1.19秒 |
| 真实Chromium默认全套 | 本次未重跑旧版本对照 | 35场景通过，117.4秒 |
| 定向浏览器 | 原入口没有选择参数 | 任务详情＋hardening routes约5.3秒 |

Python同分组墙钟减少 57.6%，约 2.36 倍；增加3项回归，未删除现有测试。这是单机单次前后比较，不能当作GitHub CI提速比例或冷缓存基准。

实施包括：命令固定sleep改成有界wait；八个HTTP测试夹具缩短仅供测试的服务轮询；选测耗时缓存损坏时回退；浏览器支持--list与明确场景选择并拒绝空发现；并行日志使用独立reader防止覆盖；中英文贡献指南改为“局部反馈→批次全量”。超时、取消、输出上限、平台CI和正式发布门禁均保留。

最终Python日志已核对1078项、68个模块摘要、4组exit=0与完整discovery一致。首次优化后的131秒运行丢失8项摘要，查明并修复日志采集竞态后重新全量验证；不把缺计数那轮作为最终证明。

Node、真实浏览器、API契约、翻译、文档、矩阵覆盖及git diff --check通过。两次独立代码评审无阻断：第一次覆盖提速主体，补充评审覆盖日志修复。原始日志与探针位于用户scratch，关键计数、文件hash和审查结论保留在JSON。未执行远端CI、Linux Docker、桌面构建或签名升级。

## 日常验证与后续优化

具体命令见 [贡献指南](../../CONTRIBUTING.md#本地开发)。单个用例/模块先直接unittest；多个独立模块用test_selected；共享前端改动运行Node全套及相关真实场景；批次完成或共享后端边界变化时再跑Python全量。依赖和浏览器安装仅在环境初次准备或锁文件变化后进行。

CI的562次额外Python测试执行来自3.9和3.14两个兼容套，各281项；普通3.12分组本身没有重复，不应删除兼容覆盖。可节省的产品内重复主要是candidate_full重复固定检查、相同SHA反馈跑两轮、validated_candidate恢复仍完整重验。它们必须绑定精确源码、harness、依赖与执行身份后再复用。

## 逐项清单

“已修”仅表示当前工作树有实现和本次回归；“待修”表示保留的问题。确定性fixture不等于真实远程/生产负载能力。

### R4-001 · P1 · 待修 · 备份验证未检查迁移账本，轮换可删除唯一可恢复备份

位置：[adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py)，行 215–232；关联旧#4。

**触发与影响：** 备份保持 SQLite 完整性和 user_version=2，但 schema_migrations 包含未知版本、校验和不匹配或缺口；随后执行 retain_backups(max_count=1)。 verify_backup 返回 valid=true，轮换把无法由当前运行时打开的最新快照当作保护点，删除真正可恢复的旧快照。归档验证与恢复演练的前置校验也会接受该文件。

**证据：** verify_backup 仅检查 integrity_check、user_version、外键及四张表；实际 Store.__init__ 在 store.py:94-98 调用 schema_migrations.validate_history，拒绝同一快照。retain_backups 在 storage_tools.py:249-265 据前者决定保护与删除。实测在快照中插入 schema_migrations(999,'future',0)，验证 valid=true；restore 抛 RuntimeError: Database migration history is newer than this runtime；轮换结果 good_backup_exists=false、bad_backup_exists=true。

**建议：** 在只读验证连接上设置 sqlite3.Row 并调用与 Store 一致的迁移账本校验，检查必要 schema；轮换仅保护当前运行时可打开的快照。增加未知版本、坏 checksum、账本缺口和唯一旧恢复点的 fixture。可选将隔离恢复演练成功作为更强的可恢复标记。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe_extra.py；另运行 data_probe.py 查看验证通过、恢复失败的配对结果。两个脚本仅创建 scratch 临时 Store。

### R4-002 · P1 · 待修 · 合并后通过的是后续主线，却发布尚未通过检查的原合并提交

位置：[adaptive_rsi/release.py](../../adaptive_rsi/release.py)，行 578–637。

**触发与影响：** 合并提交 M 的固定检查失败，第一次反馈轮询前主线 H 已修复它；该运行启用了 release 或 deploy。 自动化会把失败的 M 作为 rsi_sha 派发，即使实际通过的是 H；部署可能把已经修好的线上版本回退为坏版本。

**证据：** 调用链 Runtime._process(system_delivery) runtime.py:1317-1319 → delivery.tick:563 feedback → feedback:667-676 以 merged_sha 为 baseline、当前 head 为 candidate → Evaluator._evaluate:934-938 只要求 candidate/candidate_full/acceptance_candidate 通过，不要求 baseline 通过 → feedback:678-681 标记 verified，head=H → tick:566 release.advance → advance:578 仅检查 feedback.state，不验证 feedback.head 或 M 的检查 → advance:621,636-637 派发 merged_sha=M。真实隔离 Git fixture 中 M 的 baseline_passed=[false]、H 的 candidate_passed=[true]，decision=pass；进一步执行真实 advance 控制流并替换外部服务后，state=submitted、dispatch_sha=M、verified_head=H。

**建议：** 将发布准入证据绑定实际派发 SHA；单独验证 merged_sha 并持久化其通过回执，或要求该 SHA 对应的所有发布门禁通过。主线漂移监控应保留独立状态，不能直接成为原合并提交的发布凭证。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 delivery-probe-results.json 的 failed_merge_fixed_later 和 release_uses_failed_merge。只运行本地 fixture 的 Python assert；GitHub API、授权和存储为隔离替身，没有远程写。

### R4-003 · P1 · 待修 · 合并后快照比较把修改过的失败测试恢复为旧测试并复用为完整检查

位置：[adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py)，行 667–676。

**触发与影响：** merged_sha 到当前主线之间修改了已有测试、配置或检查入口；当前工作树是已提交且干净的 snapshot。 主线真实检查失败仍可显示 verified，并解锁 release/deploy。合并后候选完整检查实际上没有覆盖当前提交的真实测试入口。

**证据：** feedback:667-676 → Evaluator._evaluate:822-823 → _changed_files:466-472 用 git diff HEAD 获取变更，干净 snapshot 始终返回 []，没有比较 baseline SHA 与 current SHA → _evaluate:836,850 判定 harness 未变化且 reuse_full=True → _standard_stage:671-672 对 candidate 使用 merged baseline 的 freeze_harness → freeze_harness:317 覆盖 current 修改后的测试 → _evaluate:928-929 把这一轮直接当成 candidate_full。fixture 中 current 的 test_app.py 直接执行 exit=1，evaluate 返回 pass、candidate_full_reused=true。

**建议：** 反馈比较显式计算 merged_sha..head 的提交差异并传 changed_files；当测试或入口发生变化时，保留旧 harness 回归检查，同时强制在当前快照的真实 harness 下执行 candidate_full。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；changed_mainline_test_hidden: direct_current_exit=1, decision=pass, candidate_full_reused=true。

### R4-004 · P2 · 待修 · 历史运行结果容器异常会中止整库恢复

位置：[adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py)，行 391–394。

**触发与影响：** 数据库中有 pr_open 或 merged 运行，其 result 为合法 JSON 数组、null，或非法 JSON；其余结构和关系有效。 备份验证报告成功，但恢复在写反馈不可用标记时崩溃，随后删除新建的恢复目录；单条历史证据阻止全部项目和任务恢复。

**证据：** 同一 restore 函数在 365-371 行对 awaiting_approval 结果执行 JSON 异常隔离和 dict 类型校验，391-394 行对 merged/pr_open 未做保护。fixture 写入 merged result='[]'，verify_backup 返回 valid=true，restore 抛 AttributeError: 'list' object has no attribute 'get'。

**建议：** 统一使用容器校验解码；对异常原始结果保留原文或可追溯隔离记录，只附加 restore_invalidation，不让历史证据中断恢复。覆盖数组、null、标量和坏 JSON，并确认健康运行证据不变。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；查看 malformed_result_verification 与 malformed_result_restore。

### R4-005 · P2 · 待修 · 用量报告多次读取缺少共同快照，总计与明细可能自相矛盾

位置：[adaptive_rsi/usage_report.py](../../adaptive_rsi/usage_report.py)，行 59–130。

**触发与影响：** 模型调用在 /api/usage 计算 summary 后、读取 daily、roles、tasks 或 calls 前完成写入或更新 token。 同一响应显示不同调用数和成本；导出与任务明细无法核对，分页 total 也可与返回行不同。

**证据：** report 顺序调用 store.one、store.stream 和多个 store.query，server.py:378-380 直接调用 report，未包 store.snapshot；每次独立借连接。fixture 在 summary 读完后插入第二个调用，实测同一响应 summary_calls=1、daily_calls=2、task_calls=2。

**建议：** 将报告所有关系读取放在同一 store.snapshot 中，使 summary、趋势、分组、分页和调用详情引用同一 SQLite 读事务；尽量缩短 Python 处理持有读事务的时长，避免长读拖延 WAL 检查点。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；脚本通过 one 包装器在两次读取之间确定性插入一条本地调用记录，不执行模型。

### R4-006 · P2 · 待修 · 存储概览刷新重复读取全部备份内容计算 hash

位置：[adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py)，行 557–567；关联旧#104。

**触发与影响：** 访问 /api/storage?refresh=true，或十秒概览缓存到期后再次读取；backups 中包含大型且未变的 SQLite 快照。 每次扫描都完整读取最多二十份备份，延迟和磁盘 I/O 随备份总字节线性增长；流式 hash 限制了内存，却没有限制常规概览的工作量。diagnostics 调用 overview 也继承该成本。

**证据：** 560-562 行清单直接对 backups[:20] 无条件调用 file_digest；529-539 行读取完整文件，缓存只保存整体结果十秒。fixture 连续两次强制刷新，在没有任何文件变化时每次均计算两份备份 hash（first_scan=2、second_scan=2）。

**建议：** 备份发布时计算并保存 digest；概览按 inode/size/mtime_ns 缓存未变文件结果，外部改动失效。将完整内容重新校验留给明确的验证动作，并展示最近校验时间，避免把缓存 hash 宣称为新完整性验证。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；overview_hashes 通过包装真实 file_digest 计数，不使用大文件或长时间压测。

### R4-007 · P2 · 待修 · 单条损坏的待关闭 PR 记录阻断整个交付扫描与恢复

位置：[adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py)，行 387–389。

**触发与影响：** runs 中存在 state=superseded 且 result 不是合法 JSON 的历史、恢复或损坏记录，同时其他项目仍有 merged/pr_open 工作。 schedule 在进入 scheduler_scope 的逐项隔离前抛 malformed JSON，无法调度健康项目的交付监控；tick:431-434 的同类批量查询也在 try:440 之前失败。

**证据：** schedule:387-389 和 tick:431-434 都在 SQL WHERE 中直接 json_extract(result,'$.close_pending')；单行 JSON 解析失败会中止整个 SELECT，不会进入每条运行的错误处理。SQLite 内存小用例插入 merged/'{}' 和 superseded/'broken-json'，执行同型条件得到 OperationalError: malformed JSON。

**建议：** 将解析与校验移至每项异常边界，或使用 CASE WHEN json_valid(result) THEN json_extract(...) END；损坏记录应保留原文并记录独立诊断，不阻塞其他运行。schedule 和 tick 同时修复。

**复现或验证：** sqlite3 :memory: 中 CREATE TABLE runs(state TEXT,result TEXT); INSERT INTO runs VALUES('merged','{}'),('superseded','broken-json'); SELECT * FROM runs WHERE (state IN ('pr_open','merged') OR (state='superseded' AND json_extract(result,'$.close_pending')=1)); 返回 malformed JSON。实际已用 Python sqlite3 执行，无仓库或远程状态修改。

### R4-008 · P2 · 待修 · candidate_full 追加探测检查时再次执行全部已跑固定检查

位置：[adaptive_rsi/evaluator.py](../../adaptive_rsi/evaluator.py)，行 831–903；关联旧#201。

**触发与影响：** 配置的 checks 不是 detect(candidate).checks 的完整超集，例如用户自定义 Python 检查同时仓库存在 pyproject.toml 与测试文件。 candidate_full 重跑已在 candidate 运行的全部固定命令，只为增加一个探测命令；若固定命令是完整测试套件，每次候选和每次反馈都会多跑一轮。

**证据：** execute_candidate:410 → _evaluate:831-835 构建 full_checks；只要新增命令，850 的全量相等判定失败 → 897-903 同时创建 candidate 和 candidate_full → _standard_stage:698-707 candidate_full 从 list(checks) 开始，再追加 detect 检查。fixture 实际记录 baseline/python test_app.py、candidate/python test_app.py、candidate_full/python test_app.py、candidate_full/python3 -m unittest discover，证明同一固定命令在两个候选阶段重复执行。

**建议：** 区分 harness 真正改变与仅追加命令：在树、依赖和 harness 相同的情况下复用已完成的固定检查，仅运行探测增加的检查；harness 变化时继续执行独立完整检查。不要按名称复用，绑定 argv/cwd 和执行输入。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 candidate_full_duplicate.calls 和 candidate_full_reused=false。

### R4-009 · P2 · 待修 · 相同 SHA 的合并后验证仍无缓存地执行基线和候选两轮

位置：[adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py)，行 667–676；关联旧#201。

**触发与影响：** 首次合并后反馈时 head==merged_sha，或多个历史 merged 运行在同一主线 SHA 上同时需要反馈。 同一代码、依赖和固定命令被重复执行；多个历史运行的反馈没有共享 baseline cache，长期监控会放大执行和磁盘复制成本。

**证据：** feedback:667-668 无条件创建 merged_sha 与 head 两份快照；675-676 调 evaluate 未传 cache_scope/cache_base/environment_digest，Evaluator:820-821 因而没有执行身份，_cache_key:580-581 返回 None，885-899 必定执行 baseline/candidate。首次反馈 previous.head 不存在，653 的跳过条件不适用。隔离相同提交 fixture 实际 physical_stages=['baseline','candidate']。非 Docker 主机即使生产候选传缓存参数，也被 _execution_environment:591-599 明确禁止跨运行复用；这部分是保守策略，不能把缓存失效全部归咎于配置变化。

**建议：** 为 head==merged_sha 的 preservation 验证提供绑定不可变输入的单次执行路径，复用为同版本证据；其他情况传递明确缓存作用域与依赖摘要，并保留对不稳定宿主环境的保守禁用。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 identical_postmerge_trees.physical_stages。

### R4-010 · P2 · 待修 · 认证快照的 RuntimeError 分支遮蔽 AuthError 恢复逻辑

位置：[adaptive_rsi/auth_sessions.py](../../adaptive_rsi/auth_sessions.py)，行 869–877。

**触发与影响：** Codex 或 OpenCode 的凭据 JSON 损坏、超限或不是对象，_read_json 抛出 AuthError('invalid_credentials')。 整个 /api/auth 状态请求失败，不能返回原本设计的 signed_out/invalid_credentials，也丢失另一后端的正常状态；用户难以从认证面板诊断和恢复。

**证据：** AuthError 在第58行继承 RuntimeError；snapshot 第869行先捕获 RuntimeError，仅处理特定 OpenCode credential listing 错误，其余重新抛出，因此第876行的 except AuthError 永远接不到 AuthError。server.py:630 直接调用此快照。纯替身执行结果为 raised=AuthError, code=invalid_credentials。

**建议：** 将 except AuthError 放到 except RuntimeError 之前；分别测试 Codex/OpenCode 凭据读取错误，并断言另一个后端状态仍返回。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/desktop_probes.py；见 desktop-probe-results.json 的 corrupt_credentials_snapshot。注入 _read_json 异常，不读取任何用户凭据。

### R4-011 · P2 · 待修 · API key 写入未加入关闭跟踪且关闭后仍可安装凭据

位置：[adaptive_rsi/auth_sessions.py](../../adaptive_rsi/auth_sessions.py)，行 1161–1173。

**触发与影响：** set_api_key 通过 closed 检查后阻塞在临时 worker 的网络请求；并发调用 AuthSessions.close，再让请求返回。 关闭函数返回后仍可能写入宿主认证存储，临时认证服务也未及时关闭；关闭、退出或错误恢复边界不能保证停止凭据变更。

**证据：** set_api_key 仅在创建 worker 前检查 closed，worker 不登记 workers/discoveries/threads，安装前不再检查 closed 或 shutdown。close 第1192-1205行只关闭这些登记集合。fixture 在 worker.set_api_key 阻塞时调用 close，随后得到 install_credential(after_closed=true)，无异常。server.py:816 为调用入口，runtime.py:788 在退出时调用 close。

**建议：** 把密钥写入 worker 纳入关闭跟踪，使用取消事件，并在提交宿主凭据前在会话锁下重新检查 closed；关闭与提交之间建立明确的所有权交接。

**复现或验证：** 运行 desktop_probes.py；key_install_after_close.installed[0].after_closed 为 true。Worker 为纯内存替身，FAKE_TEST_VALUE 未发送到网络或真实凭据存储。

### R4-012 · P2 · 待修 · 更新空闲屏障遗漏正在运行的模型目录请求

位置：[adaptive_rsi/model_center.py](../../adaptive_rsi/model_center.py)，行 319–343。

**触发与影响：** 模型目录 fetch 已通过 updates.is_busy 检查并阻塞在 list_models 时，桌面更新执行 prepare/commit 或 Python 更新检查 work_busy。 服务宣称空闲并进入替换时仍有外部 HTTP 或 CLI 目录任务；任务可被退出打断，或在新版本启动/状态备份期间继续执行旧版本操作，破坏更新排空承诺。

**证据：** fetch 只登记 catalog_inflight，不增加 busy。lifecycle.py:71 只读取 model_center.busy，updates.py:359 同样如此；Runtime._close 也不等待 catalog_inflight。fixture 阻塞 list_models 后调用真实 readiness prepare/commit，输出 catalog_inflight=1、ready=true、phase=installing、reasons=[]。

**建议：** 在 dispatch_lock 内把目录请求加入统一的可取消活动操作登记，并在 finally 释放；更新屏障和关闭逻辑都应等待或取消该操作，避免检查与登记之间再次竞态。

**复现或验证：** 运行 desktop_probes.py；见 desktop-probe-results.json 的 catalog_drain。list_models 使用事件阻塞替身，不访问网络或模型。

### R4-013 · P2 · 待修 · 桌面复用已有服务时丢弃后续会话轮换消息

位置：[desktop/src-tauri/src/main.rs](../../desktop/src-tauri/src/main.rs)，行 654–661。

**触发与影响：** 先独立启动服务，再启动桌面复用该服务；其他浏览器窗口调用 session/rotate。 桌面 Connection 保留旧 cookie/csrf，后续请求持续认证失败；Python 原本发送 rotated 消息的复用监视进程也会因 stdout 管道已关闭而在 print 时退出。桌面无法自动恢复到仍正常运行的已有服务。

**证据：** bootstrap 启动的线程仅 BufReader::new(stdout).read_line 一次，发送第一条握手后销毁 reader，后续没有 stdout 消费者。desktop.py:63-69 明确设计成在 marker token 改变后持续打印 rotated 握手。transport.rs:126-133 仅对本桌面亲自发起的 rotate 回包更新凭据，不能处理其他客户端轮换。

**建议：** 保留并持续读取私有握手管道，校验轮换消息对应的服务实例后更新 local/target；对监视进程意外退出给出重连或明确恢复入口。

**复现或验证：** 代码支持，未启动真实应用：检查 main.rs:654-661 的一次性 reader、desktop.py:63-69 的持续 rotated 输出以及 transport.rs:126-133 的凭据发布条件。集成复现需隔离状态目录下先 serve，再 desktop-runtime，随后从另一客户端 rotate。

### R4-014 · P2 · 待修 · 更新停止运行器超时后保留已失去控制管道的旧连接

位置：[desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs)，行 376–383。

**触发与影响：** 更新关闭 stdin 请求运行器退出，但 Python 清理超过30秒；原生 worker 判定 update_runtime_shutdown_failed，运行器随后自行退出。 child 被放回状态时 stdin 已被取走并关闭，无法撤销退出。engine_stopped 仍为 false，错误恢复不会 bootstrap，local/target 继续指向即将死亡的服务。应用返回 ready 可重试外观，但普通 API 连接随后不可用，且取消更新屏障也可能触发恢复中的任务准入。

**证据：** 第378行 child.stdin.take 的局部变量在 if 块结束即 drop；第379行仅等待30秒，超时把无 stdin 的 child 放回并返回错误；engine_stopped 直到第383行才置 true。第402行只在 engine_stopped=true 时 bootstrap。Python desktop.py:94-98 在 stdin EOF 后停止服务，runtime.py:783-806 有 join、worker shutdown、维护线程等可超过30秒的清理。

**建议：** 把已发送停止请求作为独立不可撤销状态管理；超时后继续监视并在退出后重启旧运行器，或提供明确恢复流程；不要把仍在退出中的 child 视作可用连接或重新开放其更新屏障。

**复现或验证：** 代码支持，未更新实际应用：用隔离假运行器在读到 stdin EOF 后延迟超过30秒退出，执行安装停止阶段，观察失败回包后进程退出而 local/target 未重新 bootstrap。当前未运行该超过30秒实验，符合本次单项验证<=30秒约束。

### R4-015 · P2 · 待修 · 模型诊断成功返回后调用不可见的 dialog 函数

位置：[adaptive_rsi/web/model-center.js](../../adaptive_rsi/web/model-center.js)，行 167–168；关联旧#1。

**触发与影响：** 在模型服务中选择包含模型的连接，点击「只读诊断」且接口返回成功。 所有诊断结果都无法展示；safe 捕获 ReferenceError 后仅显示错误提示，用户无法查看诊断。

**证据：** experience.js 的 dialog 仅定义在 RSIExperience IIFE 内，也未导出；model-center.js 自身没有 dialog 定义。直接提取原始诊断回调执行，成功取得结果后抛 ReferenceError: dialog is not defined。浏览器模型中心测试未覆盖该按钮。

**建议：** 在模型中心定义或复用已明确导出的模态框工厂；增加点击诊断并确认结果可见的浏览器用例。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/diagnostics-repro.cjs；输出 error=ReferenceError: dialog is not defined

### R4-016 · P2 · 待修 · 项目模型诊断始终使用全局配置

位置：[adaptive_rsi/web/model-center.js](../../adaptive_rsi/web/model-center.js)，行 166–166；关联旧#1。

**触发与影响：** 切到某个项目的模型设置，在项目专有连接或覆盖的模型上点击只读诊断。 发送 project:null，导致项目专有连接报不存在，或诊断全局同名连接的凭据、目录和健康状态。

**证据：** scope 的取值为项目 ID 或 null（show/changeScope/load 调用链），但此处比较 scope===project 后才发送 scope。Node fixture 设置 scope=project-123，真实回调发送 project:null；服务端 diagnose 使用 runtime.config(project) 和 provider(provider_id,project)。

**建议：** 发送实际 scope；浏览器分别断言全局和项目诊断的请求体及结果来源。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/diagnostics-repro.cjs；request.payload.project 为 null，而 fixture scope 为 project-123

### R4-017 · P2 · 待修 · 推荐任务簇路径遗漏批量幂等恢复

位置：[adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js)，行 44–48；关联旧#37。

**触发与影响：** 确认推荐合并任务簇后请求回执丢失，再次点击推荐并确认，或同一操作返回 complete:false。 此路径没有保存 request_id 或提示待核对回执，重试使用新 ID，不能像普通批量操作一样恢复原操作；已执行任务可能从推荐中消失，用户只能另行查找批次记录。服务端状态校验存在，未据此声称一定重复创建任务组。

**证据：** 普通 perform 在 92–110 行读取/保存 RSIDraftStore 并重用 ID；recommendations 每次直接 crypto.randomUUID()，也不处理 complete:false 的反馈。Chromium fixture 模拟受理后丢响应，连续两次提交产生 request-1/request-2。

**建议：** 让推荐簇复用普通批量提交/回执恢复机制，逐簇持久化稳定 ID，并在未知结果时展示核对入口；覆盖受理后断线场景。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/frontend-repro.cjs；batch.request_ids=[request-1,request-2]

### R4-018 · P2 · 待修 · 授权轮询重绘打断供应商搜索和键盘导航

位置：[adaptive_rsi/web/auth.js](../../adaptive_rsi/web/auth.js)，行 96–104；关联旧#188。

**触发与影响：** 有进行中的授权会话时，在 OpenCode 供应商搜索框输入或用键盘操作供应商下拉；轮询每 1.5 秒结束会 render，切语言也触发相同路径。 搜索框/下拉被 replaceChildren 移除，焦点落到 body，键盘输入被中断；搜索值 providerQuery 被保留，但控件焦点没有恢复。

**证据：** render 仅保存 [data-auth-field]，而 mc-auth-provider-search 和 mc-auth-provider 没有该标记。poll finally 总是 render；Chromium fixture 将搜索框 focus 后调用真实 RSIAuth.translate，document.activeElement.id 从 mc-auth-provider-search 变空，旧节点 isConnected=false。

**建议：** 保持供应商控件 DOM，或为搜索框和下拉保存/恢复焦点和选择；用一次确定性 poll 而非真实等待验证键盘焦点。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/frontend-repro.cjs；focus={before:mc-auth-provider-search,after:"",oldConnected:false}

### R4-019 · P2 · 待修 · 桌面后台下载在重新打开页面后被六小时检查间隔阻塞

位置：[adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js)，行 98–104。

**触发与影响：** 最近一次成功检查发现 available，下载前重载/重开页面，或在六小时内从 manual 切到 download；原生状态仍为 available。 设置为后台下载后 tick 仅获取 status，直到六小时检查过期才下载；automatic 策略也无法走到 ready/install。用户可手动下载，因此非完全阻断。

**证据：** 下载只在「需要重新检查」的 if 内执行；持久化 checked 在 check 成功时已写入，刷新后 failureCounts 清空。真实 native-update.js Chromium fixture 预置 checked=Date.now、policy=download、IPC available，连续 tick 两次调用只有 status/status，没有 download。

**建议：** 将已知 available 且满足策略/范围时的下载决策移出检查间隔分支，并保留独立下载失败退避；测试重载后 available 和策略切换。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/native-resume-repro.cjs；calls=[status,status]，policy=download，界面发现新版本。

### R4-020 · P2 · 待修 · 实验评估沿用父运行 planning 状态，命中准备时限且丢失阶段超时回执

位置：[adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py)，行 387–398；关联旧#95。

**触发与影响：** system_evolve 的配对试验进入 evaluator；准备和评估时限不同，或前一试验准备时间已接近上限。 慢测试按 preparation_seconds 提前停止，或超过 evaluation_seconds 仍继续；trial_run 的 _timed_out_stage 没有同步父 run，最终被记作 interrupted 而非带 evaluation 阶段的 deadline 故障，误导恢复判断。

**证据：** pipeline.py:112-115 在 experiment=True 时完全跳过 transition，因此父 run 在 DB 保持 planning。Runtime.is_cancelled:1210-1217 仅凭 DB state=='evaluating' 切换 evaluation 时钟。evolution.py:387 浅拷贝 run 后让 pipeline 使用 trial_run，而 overlay 的 cancel 又改用父 run；runtime.py:1439-1445 仅查父字典的 _timed_out_stage。隔离探针实际返回 database_state=planning,cancelled=true,timed_out_stage=preparation,parent_receives_timeout=null。

**建议：** 为运行上下文显式记录当前阶段和阶段开始时间，实验路径也更新此上下文；独立 trial 阶段时钟不要从父 run 复制旧时间。将阶段超时作为结构化异常传回父运行，并让 overlay 和普通实验检查使用同一上下文。补充 experiment=True 的 evaluation 超时及回执测试。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/runtime-probes.py；experiment_deadline 使用真实 Store.claim 和 Runtime.is_cancelled， preparation_seconds=1、evaluation_seconds=100、准备时钟过去5秒。当前测试 test_evaluation_deadline_has_its_own_clock 先显式 transition('evaluating')，未覆盖 experiment=True 的不落库分支。

### R4-021 · P2 · 待修 · 停用的 development 样本仍进入策略提案上下文

位置：[adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py)，行 315–327；关联旧#153。

**触发与影响：** 通过 update_case(active=False) 停用 development 样本，或先把样本重分类为 development 并停用，随后执行策略进化。 用户已经停用的问题、低质量或失效任务仍被送入付费提案并影响新策略；停用 development 样本对实际学习上下文没有效果。

**证据：** update_case:118-123 把 active 写入 spec。validation/holdout 选择在279-280检查 active，但 development 查询315-316仅按 split 筛选。隔离探针插入 active=false 的 development 样本后截获实际 propose_and_evaluate 的提案提示，retired_task_in_prompt=true；截获点主动抛异常，未调用任何模型。

**建议：** 查询 development 样本时使用与 validation/holdout 一致的 active/available 条件；过滤后再应用数量上限，避免前10个停用样本占满窗口。新增停用样本不出现在提案提示的测试。

**复现或验证：** 运行同目录 runtime-probes.py，查看 retired_development。现有 test_retirement_and_reclassification_keep_exposure 只核对数据库中的 active/uses/lifecycle，不断言提案提示或样本选择排除 inactive 数据。

### R4-022 · P2 · 待修 · 后处理每轮扫描并解析全部历史完成记录且持有全局调度锁

位置：[adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py)，行 1137–1151；关联旧#169。

**触发与影响：** 长期运行累积大量已完成或 obsolete 的 postprocess:* settings；调度器每5秒调用 drain_postprocess()。 每轮时间和分配内存随成功历史线性增长，即使没有待办也 SELECT、传回并 json.loads 全部记录；这段工作持有 dispatch_lock，延迟项目暂停、配置保存、交付批准和新任务领取。

**证据：** loop:1010-1011 每轮无条件 drain_postprocess。1138 获取 dispatch_lock 后，1142-1143查询所有 postprocess:*，1147先解析JSON，1150才跳过 completed。1191-1192完成记录永久留在同一命名空间。极小真实Store探针插入3个completed记录，生产查询仍读回全部3个。未进行大型性能测试，因此不声称具体毫秒收益。

**建议：** 使用有状态索引的专用待办表，或把 pending 与归档回执分开存储；仅分页领取到期 pending。把历史回执解析移出 dispatch_lock，持锁范围限于实际需序列化的副作用和状态提交。

**复现或验证：** 运行同目录 runtime-probes.py 查看 postprocess_query.loaded_completed_records=3；可补充一个无 pending、上千 completed 的测试，断言读取/解析记录数与 pending 数关联，而不是只测耗时。现有恢复测试只用少量记录，不覆盖历史放大。

### R4-023 · P2 · 待修 · 明知新鲜 holdout 数不足仍执行整套付费试验后才拒绝晋升

位置：[adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py)，行 279–282；关联旧#143。

**触发与影响：** holdout 记录仍在 max_holdout_uses 范围内，但 uses==0 的数量少于 min_cases_per_split；常见于第一次进化已经消耗样本、默认允许同一样本使用3次的后续进化。 预检认为样本足够，于是调用提案、完整 validation 和 holdout 两个变体重复试验；即使质量全部提升，也会在最后因 fresh=false 必定转 insufficient_evidence。新增样本前的后续自动进化持续花费模型和全套检查时间，却不可能晋升。

**证据：** 279-282只校验 uses<limit 的总数；344按该集合选择holdout。363-366在开始实验前已经算出fresh=false，但369-408仍执行全部实验，直到422-423才依据该已知值拒绝通过。select_cases虽然按uses排序，但不能补足不存在的新鲜样本。此为静态调用链证据，未运行付费实验。

**建议：** 在首次模型调用前，按晋升用途校验并选择足够的 uses==0 holdout；不足时直接返回需要新复核集的可操作原因。如确实允许探索性复用，应显式区分探索运行并提前说明该次无晋升资格，避免自动调度浪费。

**复现或验证：** 最小用例：min_cases_per_split=3，提供3个validation和3个uses=1的holdout，max_holdout_uses=3；用计数fake runtime.invoke/execute_candidate观察仍触发提案和试验，而最终fresh恒为false。现有测试仅检查元数据完整性，无不满足fresh前置条件时模型调用数必须为0的测试。

### R4-024 · P2 · 已修 · 命令执行固定睡眠放大 Git 和检查的完成等待

位置：[adaptive_rsi/process.py](../../adaptive_rsi/process.py)，行 142–147。

**触发与影响：** 短命令在第一次 poll 后很快退出。 每次命令仍等待80毫秒轮询间隔，真实任务和测试中反复执行Git时放大耗时。

**证据：** 同机12次短Python命令，中位耗时87.81ms；改为有界wait后39.25ms。超时、取消和输出上限检查继续保留。

**建议：** 使用能随进程结束返回的wait(timeout=0.08)，继续保持相同检查间隔上界。

**复现或验证：** tests.test_audit_boundaries.AuditProcessBoundariesTest.test_completed_commands_do_not_wait_for_a_fixed_polling_sleep; process-before.json / process-after.json

### R4-025 · P2 · 已修 · 并行日志读写共用文件位置导致结果行被覆盖

位置：[scripts/test_parallel.py](../../scripts/test_parallel.py)，行 98–110。

**触发与影响：** 父进程seek读取日志时子进程仍在写入同一open file description。 通过/失败行、测试摘要可丢失，不能可靠核对数量，排查需要重跑；退出码仍由worker返回。

**证据：** 首次优化后日志缺8项测试摘要；os.dup加seek期间并发write确定性重现覆盖。独立reader修复后1078项、68个模块摘要和4组退出结果全部对齐。

**建议：** 对子进程保留二进制writer，父进程通过独立open只读reader采集。

**复现或验证：** tests.test_release_parallel.ParallelReleaseTest.test_log_reader_cannot_rewind_a_running_workers_output

### R4-026 · P2 · 待修 · 选测入口收到 SIGTERM 后遗留独立 worker

位置：[scripts/test_selected.py](../../scripts/test_selected.py)，行 31–35。

**触发与影响：** CI 取消作业或外部向 test_selected 父进程发送 SIGTERM。 父进程被默认信号处理直接终止，绕过 run_groups 的 finally；独立 session 的 worker 继续执行，消耗资源并可能继续修改 fixture。

**证据：** 真实 scratch 子进程树：worker 写出 PID 后 SIGTERM 父进程，父退出 -15，os.kill(worker,0) 仍成功；probe finally 已显式杀死自己的 worker。全量 main 在 test_parallel.py:182-185 安装了将 SIGTERM 转为 KeyboardInterrupt 的 handler，选测入口没有。

**建议：** 复用全量入口的信号转换与退出码约定，覆盖真实 SIGTERM、SIGINT、启动一半失败及第二次中断的有界清理；不要只 mock KeyboardInterrupt。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-027 · P2 · 待修 · 空 suite 被选测 worker 标记通过

位置：[scripts/test_worker.py](../../scripts/test_worker.py)，行 18–26。

**触发与影响：** selector 指向没有 test 方法的有效 TestCase，或 load_tests 返回空 suite。 带--durations的自定义worker执行零项仍返回0。无durations的标准unittest行为随Python版本变化；本机3.14已拒绝零项，不能一概而论。

**证据：** 最小 Empty TestCase 的 test_worker.run 返回 0；worker 日志显示 Ran 0 tests/OK。全量 discover_groups 已拒绝空 discovery，但选测路径没有对应检查。

**建议：** 执行前要求每个 selector 展开至少一个真实测试，检查 loader.errors，并在父进程对有/无 --durations 两条路径统一执行；添加空 module/class/load_tests 回归。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-028 · P2 · 待修 · 选测仅按 selector 字符串去重，允许同一测试重复并发执行

位置：[scripts/test_selected.py](../../scripts/test_selected.py)，行 18–30。

**触发与影响：** 同时传入 tests.module.Case 和 tests.module.Case.test_a，或 module 与其 class。 相同测试在不同 worker 重复执行，增加耗时；具有共享外部 fixture 的测试还可能争用资源。

**证据：** scratch 两方法类加其中一个方法：输入字符串唯一但选测规划到两个 group；worker 实际执行计数为 3，退出 0。

**建议：** 先展开 selector 得到测试 ID Counter，拒绝重叠或合并成不重叠的选择，再按 fixture 边界分组；保留明确 requested/expanded/executed 清单。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-029 · P2 · 已修 · 选测耗时缓存未复用已有安全过滤器

位置：[scripts/test_selected.py](../../scripts/test_selected.py)，行 22–30；关联旧#201。

**触发与影响：** 传入损坏 JSON、JSON 数组，或记录值为字符串的 --durations 文件。 辅助缓存可使整次选测在启动任何测试前崩溃，违背缓存只影响分配、不阻止验证的约定。

**证据：** 实测损坏 JSON 抛 JSONDecodeError，[] 抛 AttributeError，字符串耗时抛 TypeError；test_parallel.timing_cache 已实现类型、范围、finite 检查和 main 的读取容错。

**建议：** 复用 timing_cache 和 OSError/ValueError 回退逻辑；无效缓存回退默认权重并继续全部 selector。补充直接覆盖 test_selected 的畸形值及不可读路径测试。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-030 · P2 · 待修 · 按 selector 单独启动 runner 重复执行同类 fixture

位置：[scripts/test_worker.py](../../scripts/test_worker.py)，行 16–21；关联旧#194。

**触发与影响：** 使用 --durations 运行同一 TestCase 的多个方法 selector。 每个方法重新执行 setUpClass/tearDownClass；无 --durations 时 unittest 一次加载所有 selector，可共享相邻同类 fixture，导致记录耗时模式改变实际测试语义及成本。

**证据：** scratch 同类两个方法经 test_worker.run 执行后 setUpClass 计数为 2。macos_sandbox_flows 当前对 DeliveryLoopTest、ReliabilityTest 传入多个方法且始终启用 --durations。

**建议：** 在一个 suite 中运行并用 TestResult 记录各方法时间，或先按 class/module 合并 fixture 分组；在保留 fixture 一次性语义的条件下记录调度权重。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-031 · P2 · 已修 · HTTP 测试 fixture 默认半秒轮询耗时远大于测试主体

位置：[tests/test_server_hardening.py](../../tests/test_server_hardening.py)，行 29–39；关联旧#201。

**触发与影响：** 每个 HTTP 用例新建 serve_forever 默认 poll_interval=0.5 的 server，并在 tearDown 调用 shutdown。 每个短用例固定等待接近半秒；该文件有 16 个用例，且 Python 3.9/3.14/3.12 都运行。tests/test_runtime.py:1243-1247 等 fixture 有相同模式。

**证据：** 真实 test_audit_is_bounded_and_never_records_request_secrets：默认 setup 0.0155s、body 0.0114s、teardown 0.5014s；仅在 scratch harness 给测试线程注入 poll_interval=0.01 后分别为 0.0150s、0.0083s、0.0115s。这是单用例实测，未跑整套，不将其外推为总提速。

**建议：** 抽取仅供测试的 HTTP fixture，使用较短 poll interval 和明确线程 join；逐个确认 shutdown/close/Store 资源释放，不修改生产轮询默认值。

**复现或验证：** 见 http-fixture-probe-results.json；在 scratch 临时目录实例化 ServerHardeningTest，分别以默认值和 serve_forever(poll_interval=0.01) 执行同一用例。

### R4-032 · P2 · 待修 · 浏览器取消升级定时器 unref 可能导致漏杀顽固后代

位置：[scripts/test_browser.cjs](../../scripts/test_browser.cjs)，行 11–15。

**触发与影响：** 取消或超时后直属浏览器测试进程先退出，但同 session 后代忽略 SIGTERM 且关闭继承的 stdio。 child close 后 active 清空，父进程可在 1.5 秒 SIGKILL 定时器前退出；残留后代没有最后的强制清理。

**证据：** VM seam 执行真实调度脚本：SIGTERM 注册一个 1500ms 且 unref 的升级 timer，直属 child close 后立即写结果并置 exitCode；实现没有等待升级完成。未运行真实 Chromium/后代信号实验，结论基于 Node timer 生命周期和代码路径。

**建议：** 取消/超时维护独立的 cleanup pending 状态，直到保有进程组的安全升级和回收完成才结束；为忽略 SIGTERM、关闭 stdio 的后代增加真实无浏览器回归。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/browser_probe.cjs

### R4-033 · P2 · 已修 · 浏览器测试发现为空仍返回成功

位置：[scripts/test_browser.cjs](../../scripts/test_browser.cjs)，行 6–9。

**触发与影响：** tests 目录缺失所有 browser_*.cjs，或文件命名/发现过滤变更导致结果为空。 test:browser 执行零个场景却退出 0，并写空 durations 对象。

**证据：** VM seam 令 readdirSync 返回 []，执行原始脚本：spawned=0、exitCode=0、写入 {}。

**建议：** 对 files/pending 非空作硬检查；记录计划与完成数量并在完成时核对，不把取消清空 pending 当正常零测试。

**复现或验证：** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/browser_probe.cjs

### R4-034 · P3 · 待修 · 人工干预历史按项目扫描普通事件，分页限制不能约束查询工作量

位置：[adaptive_rsi/task_lifecycle.py](../../adaptive_rsi/task_lifecycle.py)，行 24–27。

**触发与影响：** 项目有大量普通运行事件，但指定任务很少或没有 manual_retry、requirements_revised 等干预记录。 打开单任务干预历史会扫描项目的大量事件；LIMIT 101 仅限制匹配结果，无法使空结果或稀疏历史读取有界。随着运行历史增长，该界面查询持续变慢。

**证据：** EXPLAIN QUERY PLAN 实际使用 project_events(project,seq)，逐行过滤 kind 与 json_extract(data,'$.task')。一万条同项目非干预事件且零匹配的 fixture 执行约 100064 条 SQLite VM 指令，返回仍为空；项目索引无法跳过这些普通事件。

**建议：** 为固定干预 kind 集合建立部分索引，进一步将经过验证的 task 标识提取到结构化列或使用受 json_valid 保护的表达式索引，以 (project,task,seq DESC) 提供游标分页；用 EXPLAIN 和稀疏历史 fixture 确认读取规模随匹配记录而非项目事件总数增长。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe_extra.py；输出 intervention_plan、events=10000、matching_interventions=0、vm_instructions=100064。

### R4-035 · P3 · 待修 · 恢复 validated_candidate 仅复用补丁，仍无条件重跑完整评价

位置：[adaptive_rsi/pipeline.py](../../adaptive_rsi/pipeline.py)，行 355–415；关联旧#201。

**触发与影响：** 同输入运行已通过 evaluation 并保存 validated_candidate，但在 review 或后续阶段中断后重试。 重试会显示复用 validated_candidate，但只跳过实现；固定候选、完整候选和独立候选验收仍全部再次执行。macOS Seatbelt 没有跨运行 baseline cache，基线也会重跑。

**证据：** recovery.cached:68-75 按任务与签名取阶段；pipeline:329 取得 validated_candidate → 355-361 校验 patch_hash 并 git apply → 410 无条件 evaluate，不读取421保存的 evaluation 阶段 → 425-429 再保存 validated_candidate。334 的提前 acceptance_baseline 被跳过，但 evaluator 仍负责补跑。候选签名324-328还包含 planning_outcomes、open_prs 和 related_work；这些变动会让仅交付背景变化也失去候选复用。这里没有把安全必需的重验定性为错误，而是记录重复验证的真实来源。

**建议：** 先明确恢复契约：区分复用补丁与复用验证。对精确补丁、固定依赖、不可变执行镜像、harness、build 和检查契约一致的情况，可设计独立 evaluation checkpoint；宿主环境无法证明一致时继续重验，并在界面解释具体原因。

**复现或验证：** 代码支持：对已有 validated_candidate 的 execute_candidate 入口将 evaluator.evaluate 替换为计数器，会在补丁恢复后调用一次。未运行模型或完整 pipeline；执行位置和无条件调用可直接由 pipeline.py:355-415 复核。

### R4-036 · P3 · 已修 · 浏览器入口无场景选择，局部修改容易触发全套验证

位置：[scripts/test_browser.cjs](../../scripts/test_browser.cjs)，行 9–14。

**触发与影响：** 按npm默认验证命令只想检查一个页面或hardening分组。 原入口忽略参数并发现全部35个场景；贡献文档首个命令仍为串行全量，快速反馈路径不明显。

**证据：** 新增--list、文件及hardening分组selector，未知/重复/空发现拒绝。实跑两个定向场景约5.3秒；全套35场景117.4秒。

**建议：** 保留默认全套发现，局部验证显式选择，并同步中英文分层验证命令。

**复现或验证：** node --test tests/web_browser_runner.cjs; npm run test:browser -- browser_task_detail.cjs browser_hardening.cjs:routes

### R4-037 · P3 · 待修 · 每个 Python 分组重复执行全库矩阵发现检查

位置：[.github/workflows/checks.yml](../../.github/workflows/checks.yml)，行 140–141；关联旧#201。

**触发与影响：** 普通 full CI 启动 12 个 Python matrix entry。 同一覆盖检查重复导入全库并 loadTestsFromNames；它不是分组特有验证，还会在仅需少量模块的 job 执行所有测试导入。

**证据：** 静态统计为 12 个 modules entry，所有共用 line 140；本机单次 check() 发现 1075 个 ID，耗时 0.179s。未实测 GitHub 时间，预计收益较小，不应夸大为主要瓶颈。

**建议：** 将静态矩阵覆盖检查集中在单个 quick job；如 discovery 有版本条件，仅在必要解释器上各跑一次，分组 job 保留自身 unittest 执行。

**复现或验证：** python3 -B -c "import time; from scripts.check_test_matrix import check; s=time.monotonic(); print(check(),time.monotonic()-s)"

### R4-038 · P3 · 待修 · 快速测试数量的 sed 转义错误使指标总为零

位置：[.github/workflows/checks.yml](../../.github/workflows/checks.yml)，行 170–172。

**触发与影响：** quick_python 完成 unittest 并尝试从 Ran N tests 提取数量。 成功执行的快速测试被上报为 0，误导吞吐和测试数量判断；当前 stderr 重定向还会使失败信息难以从 step 日志直接看到。

**证据：** 直接取工作流的原始 count 行（未 YAML 二次解码，run 为 literal block），输入 Ran 42 tests in 0.123s，bash 返回 0 且 count=0。表达式用双反斜杠代替 sed BRE 分组单反斜杠。

**建议：** 修正 literal shell 中的 BRE 转义，或由结构化 unittest runner 写真实 testsRun；同时 tee/失败时输出完整测试日志。为原始 workflow shell 的已知样本增加小契约检查。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/metrics_probe.py

### R4-039 · P3 · 待修 · 矩阵覆盖检查只做集合包含，漏报组内重复和额外 selector

位置：[scripts/check_test_matrix.py](../../scripts/check_test_matrix.py)，行 13–25。

**触发与影响：** 同一 matrix.modules 内添加 module+class 或重复 module，或 selector 额外引入非 discovery 测试。 检查仍显示覆盖通过，不能阻止重复执行成本或发现范围偏移。

**证据：** mock loader discovery 为 test_a、selection 为 test_a 两次及额外 test_b，check() 仍返回 1；Counter 建立后只用于 set 差集，丢弃重复和 extra。当前跨 Python 版本重复是有意兼容验证，应与组内重复区分。

**建议：** 解析每条矩阵 entry，分别验证组内 Counter 唯一，并对受支持环境的预期测试集合做显式覆盖；允许有声明的跨版本/平台重复，不做全局一刀切去重。

**复现或验证：** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

## 收尾记录（2026-10-28）

本批改动在同一工作树完成，并已逐项验证：

- 测试运行器：选择器在启动任何进程前展开为具体用例；整类与其方法同时选择、选择器互相包含、选择不到用例都会直接拒绝。worker 不得为自身正在运行的模块再启动 runner，父进程消失后 worker 自行退出。回归测试与“拦截 Popen、零 spawn”的探针共同验证，避免再次出现递归子进程拖垮系统。
- P1：发布闸门要求验证覆盖实际合并 SHA；合并后使用 committed diff 决定 changed_files；备份恢复核对迁移账本；损坏的 result/feedback 保留原文并可恢复。
- 评估器：candidate_full 使用完整检查集，检测到的追加检查只执行一次；显式空 changed_files 仍会比较已提交历史，不能复用冻结证据。
- 其他：用量报告共享同一读快照、模型目录请求纳入更新屏障、更新恢复后自动下载、认证重绘保留焦点、诊断弹窗补齐、损坏 delivery 记录不阻塞调度。
- 验证：全量 Python 134 个分组选择器通过；Node 357 项通过；Rust 22 项通过；文档链接与中英配套检查通过。需要真实签名、密钥轮换、跨卷启动、远端 CI 和人工读屏的条目仍未完成，详见 201 项历史台账。
