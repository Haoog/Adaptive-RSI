# Project review and validation timing

[简体中文](project-review-20261028.md)

This review starts from `e78aea5cc892010b86987bac8fc54254728370a4` (0.5.125). Changes remain in the `chore/audit-validation-feedback-20261028` working tree. No commit, push, merge, release or installed-application update was performed. Existing `docs/research/` files were preserved.

The report contains 39 substantive findings: 3 P1, 30 P2 and 6 P3. 34 have deterministic probe or execution evidence; 5 are supported by call-chain inspection. 30 are fixed in the working tree and 9 remain open. Related legacy IDs identify overlapping areas or incomplete old acceptance, not a renewed certification.

The historical 201-item ledger records 114 implemented, 56 partial and 31 pending items, leaving 87 partial or pending. Those items are not added again or recertified here. See the [machine-readable report](project-review-20261028.json).

## Highest priorities

1. Backup retention can delete the only recoverable older snapshot after accepting an incompatible migration ledger.
2. Post-merge evaluation can replace changed current tests with the old harness and reuse it as complete validation.
3. Successful feedback for a later mainline SHA can authorize release or deployment of the original failing merge SHA.
4. Model diagnostics has an undefined helper and wrong project scope; credential error recovery, post-close writes and update draining also have reproducible gaps.

The parent independently reran isolated fixtures for all three P1 findings. They remain open and should precede expanded validation caching. Reproductions use temporary databases, Git repositories and external-service substitutes.

## Implemented validation improvements

| Check | Before | Current |
| --- | --- | --- |
| Full Python, same four groups | 1,075 tests, 310.8s | 1,078 tests, 131.7s; 1,068 executed, 10 skipped |
| Short Python command median, 12 samples | 87.81 ms | 39.25 ms |
| HTTP fixture shutdown median, 6 samples | 501.18 ms | 11.13 ms |
| Full Node suite | 354 tests, 1.46s | 356 tests, 1.19s |
| Default real Chromium suite | No old-version comparison rerun | 35 scenarios passed, 117.4s |
| Focused browser run | No selection arguments | Task details and hardening routes, about 5.3s |

Python wall time fell 57.6%, or about 2.36 times faster, with three regression tests added and no existing tests removed. This is one local before/after run, not a GitHub CI or cold-cache benchmark.

Changes replace fixed command sleep with bounded wait, shorten polling in eight HTTP fixture files, tolerate invalid selected-test timing caches, add browser listing/selection and empty-plan rejection, separate parallel log readers from writers, and document focused checks followed by complete batch validation in both languages. Existing timeout, cancellation, output limits, platform CI and release gates remain.

Final Python evidence matches complete discovery: 1,078 tests, 68 module summaries and four zero exit codes. The first optimized run lost eight summary counts; the log-offset race was diagnosed, fixed and followed by another complete run. The incomplete-count run is not the final validation evidence.

Node, real Chromium, API contract, localization, documentation, matrix coverage and git diff --check passed. Two independent reviews found no blocking issues: one covered the main improvement and another covered the later logging fix. Raw probes/logs live in user scratch; key totals, file hashes and review verdicts are retained in JSON. Remote CI, Linux Docker, desktop builds and signed upgrades were not run.

## Daily workflow and further opportunities

See [Contributing](../../CONTRIBUTING.en.md#local-development) for commands. Run a single case/module directly, independent modules with test_selected, all Node checks plus relevant real-browser scenarios for shared frontend changes, and complete Python validation for a finished batch or shared backend-boundary changes. Install dependencies/browsers for initial setup or changed lockfiles.

The 562 extra Python executions in the CI matrix come from the 3.9 and 3.14 compatibility suites, 281 each. Ordinary 3.12 entries contain no duplicates; compatibility coverage should remain. Product-level repetition comes from candidate_full rerunning fixed checks, identical-SHA feedback using two stages, and validated-candidate recovery repeating evaluation. Safe reuse must bind exact source, harness, dependencies and execution identity.

## Detailed findings

Fixed means implemented and tested in this working tree. Open findings remain. Deterministic fixtures do not establish production-load or real-remote behavior.

### R4-001 · P1 · Fixed · Backup validation ignores migration history and retention can delete the only recoverable snapshot

Location: [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py), lines 215–232; related legacy #4. Evidence: confirmed.

SQLite integrity and user_version checks accept an unknown migration ledger that Store refuses to open. Retention then deletes the older usable backup. Validate migration history and recovery compatibility before selecting a protected snapshot.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe_extra.py；另运行 data_probe.py 查看验证通过、恢复失败的配对结果。两个脚本仅创建 scratch 临时 Store。

### R4-002 · P1 · Fixed · Later mainline verification authorizes release of a failing merge commit

Location: [adaptive_rsi/release.py](../../adaptive_rsi/release.py), lines 578–637. Evidence: confirmed.

Feedback verifies later head H but release.advance dispatches merged SHA M based only on state=verified. The fixture demonstrates failing M, passing H, then dispatch of M. Bind authorization to successful evidence for the exact dispatched SHA.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 delivery-probe-results.json 的 failed_merge_fixed_later 和 release_uses_failed_merge。只运行本地 fixture 的 Python assert；GitHub API、授权和存储为隔离替身，没有远程写。

### R4-003 · P1 · Fixed · Post-merge snapshots hide changed failing tests behind the old harness

Location: [adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py), lines 667–676. Evidence: confirmed.

Clean post-merge snapshots yield an empty working-tree diff. The evaluator freezes the old harness and reuses it as candidate_full, hiding a changed failing test. Compute the committed baseline-to-head diff and run the current real harness when it changed.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；changed_mainline_test_hidden: direct_current_exit=1, decision=pass, candidate_full_reused=true。

### R4-004 · P2 · Fixed · Malformed historical run result containers abort the entire restore

Location: [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py), lines 391–394. Evidence: confirmed.

A merged run with result=[] passes backup checks but crashes restore with AttributeError. Validate historical result containers per record, preserve malformed source evidence, and continue restoring healthy data.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；查看 malformed_result_verification 与 malformed_result_restore。

### R4-005 · P2 · Fixed · Usage report reads lack a shared snapshot and can contradict their own totals

Location: [adaptive_rsi/usage_report.py](../../adaptive_rsi/usage_report.py), lines 59–130. Evidence: confirmed.

A write between summary and detail queries yields summary_calls=1 and daily_calls=2 in one response. Read the report through one SQLite snapshot, keeping the transaction as short as practical.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；脚本通过 one 包装器在两次读取之间确定性插入一条本地调用记录，不执行模型。

### R4-006 · P2 · Fixed · Storage overview refresh rehashes unchanged backup contents

Location: [adaptive_rsi/storage_tools.py](../../adaptive_rsi/storage_tools.py), lines 557–567; related legacy #104. Evidence: confirmed.

Two unchanged backups are fully hashed on each forced overview refresh. Cache published digests by file identity, invalidate changed files, and distinguish cached metadata from an explicit integrity verification.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe.py；overview_hashes 通过包装真实 file_digest 计数，不使用大文件或长时间压测。

### R4-007 · P2 · Fixed · One malformed superseded receipt aborts delivery scanning and recovery

Location: [adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py), lines 387–389. Evidence: confirmed.

json_extract in a batch WHERE clause fails on one malformed superseded result before per-item error handling. Guard JSON extraction or move parsing inside each item boundary in both schedule and tick.

**Reproduction or validation:** sqlite3 :memory: 中 CREATE TABLE runs(state TEXT,result TEXT); INSERT INTO runs VALUES('merged','{}'),('superseded','broken-json'); SELECT * FROM runs WHERE (state IN ('pr_open','merged') OR (state='superseded' AND json_extract(result,'$.close_pending')=1)); 返回 malformed JSON。实际已用 Python sqlite3 执行，无仓库或远程状态修改。

### R4-008 · P2 · Fixed · Additional detected checks make candidate_full repeat configured checks

Location: [adaptive_rsi/evaluator.py](../../adaptive_rsi/evaluator.py), lines 831–903; related legacy #201. Evidence: confirmed.

When detection adds one command, candidate_full starts with every configured check again. Reuse completed checks only when exact source, dependencies, harness and execution identities match, and run the added commands separately.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 candidate_full_duplicate.calls 和 candidate_full_reused=false。

### R4-009 · P2 · Open · Identical post-merge SHAs still run uncached baseline and candidate stages

Location: [adaptive_rsi/delivery.py](../../adaptive_rsi/delivery.py), lines 667–676; related legacy #201. Evidence: confirmed.

Feedback always creates two snapshots and runs baseline and candidate even for identical SHAs, without baseline-cache inputs. Design a single-execution preservation path for identical immutable inputs; retain conservative host-environment restrictions.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/delivery_probe.py；查看 identical_postmerge_trees.physical_stages。

### R4-010 · P2 · Fixed · RuntimeError handler shadows credential recovery in auth snapshots

Location: [adaptive_rsi/auth_sessions.py](../../adaptive_rsi/auth_sessions.py), lines 869–877. Evidence: confirmed.

AuthError inherits RuntimeError, so the earlier RuntimeError handler rethrows it and makes the intended invalid-credential fallback unreachable. Catch AuthError first and preserve the healthy backend status.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/desktop_probes.py；见 desktop-probe-results.json 的 corrupt_credentials_snapshot。注入 _read_json 异常，不读取任何用户凭据。

### R4-011 · P2 · Open · API key workers can install credentials after session service closes

Location: [adaptive_rsi/auth_sessions.py](../../adaptive_rsi/auth_sessions.py), lines 1161–1173. Evidence: confirmed.

An API-key worker is not tracked by close and does not recheck closed before credential installation. A pure in-memory worker installs after close returns. Track ownership and cancellation and serialize the final commit with shutdown.

**Reproduction or validation:** 运行 desktop_probes.py；key_install_after_close.installed[0].after_closed 为 true。Worker 为纯内存替身，FAKE_TEST_VALUE 未发送到网络或真实凭据存储。

### R4-012 · P2 · Fixed · Update admission ignores in-flight model catalog requests

Location: [adaptive_rsi/model_center.py](../../adaptive_rsi/model_center.py), lines 319–343. Evidence: confirmed.

Catalog fetch records catalog_inflight but update readiness only checks model_center.busy. A blocked fake catalog request still allows readiness commit to installing. Register it atomically as cancellable active work and drain it during shutdown.

**Reproduction or validation:** 运行 desktop_probes.py；见 desktop-probe-results.json 的 catalog_drain。list_models 使用事件阻塞替身，不访问网络或模型。

### R4-013 · P2 · Open · Desktop discards session rotation messages from reused services

Location: [desktop/src-tauri/src/main.rs](../../desktop/src-tauri/src/main.rs), lines 654–661. Evidence: code_supported.

The desktop bootstrap reads one handshake then drops stdout, while the reused Python service emits later rotated handshakes. Other-client session rotation can leave stale desktop credentials. This is code-supported; real desktop integration was not run.

**Reproduction or validation:** 代码支持，未启动真实应用：检查 main.rs:654-661 的一次性 reader、desktop.py:63-69 的持续 rotated 输出以及 transport.rs:126-133 的凭据发布条件。集成复现需隔离状态目录下先 serve，再 desktop-runtime，随后从另一客户端 rotate。

### R4-014 · P2 · Fixed · Runtime shutdown timeout leaves an unrecoverable stale connection

Location: [desktop/src-tauri/src/native_update.rs](../../desktop/src-tauri/src/native_update.rs), lines 376–383. Evidence: code_supported.

After stdin EOF requests shutdown, a 30-second timeout restores a child whose control pipe is already gone. engine_stopped remains false, so later exit has no bootstrap recovery. Track shutdown-requested ownership and recover when the old process actually exits. The long experiment was not run.

**Reproduction or validation:** 代码支持，未更新实际应用：用隔离假运行器在读到 stdin EOF 后延迟超过30秒退出，执行安装停止阶段，观察失败回包后进程退出而 local/target 未重新 bootstrap。当前未运行该超过30秒实验，符合本次单项验证<=30秒约束。

### R4-015 · P2 · Fixed · Model diagnostics calls an out-of-scope dialog helper

Location: [adaptive_rsi/web/model-center.js](../../adaptive_rsi/web/model-center.js), lines 167–168; related legacy #1. Evidence: confirmed.

A successful diagnostics request calls dialog, which exists only inside a different IIFE. The actual callback raises ReferenceError. Use a locally defined or explicitly exported modal helper and test visible results.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/diagnostics-repro.cjs；输出 error=ReferenceError: dialog is not defined

### R4-016 · P2 · Fixed · Project diagnostics always sends the global scope

Location: [adaptive_rsi/web/model-center.js](../../adaptive_rsi/web/model-center.js), lines 166–166; related legacy #1. Evidence: confirmed.

Project settings store an ID in scope, but the callback compares it with the literal project, sending project:null. Send the actual scope and cover both global and project requests.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/diagnostics-repro.cjs；request.payload.project 为 null，而 fixture scope 为 project-123

### R4-017 · P2 · Fixed · Recommended groups bypass batch idempotency recovery

Location: [adaptive_rsi/web/batch.js](../../adaptive_rsi/web/batch.js), lines 44–48; related legacy #37. Evidence: confirmed.

Recommended groups generate a fresh request ID on every confirmation and bypass normal batch receipt recovery. A lost response followed by retry produces two IDs. Persist a stable ID and expose unknown-result reconciliation; duplicate grouping is not asserted.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/frontend-repro.cjs；batch.request_ids=[request-1,request-2]

### R4-018 · P2 · Fixed · Auth rerenders discard provider search focus and selection focus

Location: [adaptive_rsi/web/auth.js](../../adaptive_rsi/web/auth.js), lines 96–104; related legacy #188. Evidence: confirmed.

Auth rendering replaces the provider search/select without focus restoration. A Chromium fixture loses search focus during translation; polling uses the same render path. Preserve these controls or restore focus and selection.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/frontend-repro.cjs；focus={before:mc-auth-provider-search,after:"",oldConnected:false}

### R4-019 · P2 · Fixed · Background download stalls behind the check interval after reload

Location: [adaptive_rsi/web/native-update.js](../../adaptive_rsi/web/native-update.js), lines 98–104. Evidence: confirmed.

Downloading an already available update is nested inside the six-hour check interval. Reloading with download policy and a recent check only calls status. Move download policy evaluation outside the check-due branch while retaining failure backoff.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/native-resume-repro.cjs；calls=[status,status]，policy=download，界面发现新版本。

### R4-020 · P2 · Fixed · Experiment evaluation uses the parent's planning state and loses deadline receipts

Location: [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py), lines 387–398; related legacy #95. Evidence: confirmed.

Experiment mode skips the parent database transition to evaluating. Runtime.is_cancelled therefore uses the preparation clock, and the shallow trial context does not propagate its timeout marker to the parent. Use an explicit per-trial phase clock and propagate structured deadline evidence.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/runtime-probes.py；experiment_deadline 使用真实 Store.claim 和 Runtime.is_cancelled， preparation_seconds=1、evaluation_seconds=100、准备时钟过去5秒。当前测试 test_evaluation_deadline_has_its_own_clock 先显式 transition('evaluating')，未覆盖 experiment=True 的不落库分支。

### R4-021 · P2 · Fixed · Retired development cases remain in strategy proposal prompts

Location: [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py), lines 315–327; related legacy #153. Evidence: confirmed.

The development-case query ignores spec.active although validation and holdout selection honor it. A retired task still appears in the actual proposal prompt. Apply lifecycle filtering before the query limit and assert prompt exclusion.

**Reproduction or validation:** 运行同目录 runtime-probes.py，查看 retired_development。现有 test_retirement_and_reclassification_keep_exposure 只核对数据库中的 active/uses/lifecycle，不断言提案提示或样本选择排除 inactive 数据。

### R4-022 · P2 · Fixed · Postprocessing rescans completed history while holding the dispatch lock

Location: [adaptive_rsi/runtime.py](../../adaptive_rsi/runtime.py), lines 1137–1151; related legacy #169. Evidence: confirmed.

Every scheduler pass loads and parses all postprocess settings, including completed receipts, while holding dispatch_lock. Separate indexed pending work from historical receipts and keep critical sections short. No large-load timing is claimed.

**Reproduction or validation:** 运行同目录 runtime-probes.py 查看 postprocess_query.loaded_completed_records=3；可补充一个无 pending、上千 completed 的测试，断言读取/解析记录数与 pending 数关联，而不是只测耗时。现有恢复测试只用少量记录，不覆盖历史放大。

### R4-023 · P2 · Fixed · Evolution runs paid trials despite insufficient fresh holdout cases

Location: [adaptive_rsi/evolution.py](../../adaptive_rsi/evolution.py), lines 279–282; related legacy #143. Evidence: code_supported.

Freshness is known to be insufficient before paired trials start, but proposal and full trials still run before promotion is rejected. Check promotion-eligible fresh holdout availability before model work or explicitly mark an exploratory run.

**Reproduction or validation:** 最小用例：min_cases_per_split=3，提供3个validation和3个uses=1的holdout，max_holdout_uses=3；用计数fake runtime.invoke/execute_candidate观察仍触发提案和试验，而最终fresh恒为false。现有测试仅检查元数据完整性，无不满足fresh前置条件时模型调用数必须为0的测试。

### R4-024 · P2 · Fixed · Fixed command polling sleep delays short Git and validation commands

Location: [adaptive_rsi/process.py](../../adaptive_rsi/process.py), lines 142–147. Evidence: confirmed.

Twelve short-command samples show median latency falling from 87.81 ms to 39.25 ms after replacing a fixed sleep with wait(timeout=0.08). Existing cancellation, timeout and output-limit checks remain in place.

**Reproduction or validation:** tests.test_audit_boundaries.AuditProcessBoundariesTest.test_completed_commands_do_not_wait_for_a_fixed_polling_sleep; process-before.json / process-after.json

### R4-025 · P2 · Fixed · Parallel log readers rewind the worker write offset and overwrite results

Location: [scripts/test_parallel.py](../../scripts/test_parallel.py), lines 98–110. Evidence: confirmed.

A shared file description let parent seeks rewind child output and overwrite result lines. A deterministic regression fails on the old implementation. Independent writer and reader opens restore all 1,078 test counts, 68 summaries and four exit results.

**Reproduction or validation:** tests.test_release_parallel.ParallelReleaseTest.test_log_reader_cannot_rewind_a_running_workers_output

### R4-026 · P2 · Fixed · Selected-test SIGTERM leaves detached workers running

Location: [scripts/test_selected.py](../../scripts/test_selected.py), lines 31–35. Evidence: confirmed.

test_selected lacks the SIGTERM-to-KeyboardInterrupt handler used by the full runner. A real isolated parent exits -15 while its detached worker survives. Share bounded signal cleanup and verify actual process termination.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-027 · P2 · Fixed · Empty selected suites pass as successful validation

Location: [scripts/test_worker.py](../../scripts/test_worker.py), lines 18–26. Evidence: confirmed.

test_worker.run considers an empty suite successful. The direct standard-unittest path is version-dependent: local Python 3.14 rejects empty suites, while the custom worker still returns 0. Require nonempty expansion for each selector and report loader errors.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-028 · P2 · Fixed · Overlapping selectors duplicate and parallelize the same tests

Location: [scripts/test_selected.py](../../scripts/test_selected.py), lines 18–30. Evidence: confirmed.

Distinct selector strings can overlap: a class plus one of its methods executes three tests instead of two across groups. Expand IDs first and reject overlap or normalize it while preserving fixture boundaries.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-029 · P2 · Fixed · Selected-test timing cache bypasses validation and fallback

Location: [scripts/test_selected.py](../../scripts/test_selected.py), lines 22–30; related legacy #201. Evidence: confirmed.

Malformed JSON, non-object data and nonnumeric timings crashed selected-test planning. The working-tree fix reuses timing_cache and read-error fallback; every requested selector remains scheduled.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-030 · P2 · Fixed · Timing mode repeats class fixtures for separate method selectors

Location: [scripts/test_worker.py](../../scripts/test_worker.py), lines 16–21; related legacy #194. Evidence: confirmed.

Timing mode creates one runner per selector, repeating setUpClass for methods from the same class. Preserve suite fixture semantics while collecting result timing or group selectors by fixture ownership.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

### R4-031 · P2 · Fixed · HTTP fixture shutdown polling dominates short test cases

Location: [tests/test_server_hardening.py](../../tests/test_server_hardening.py), lines 29–39; related legacy #201. Evidence: confirmed.

Default serve_forever polling adds about half a second to each short HTTP fixture shutdown. Eight fixture files now use a test-only 0.01-second interval. Existing real HTTP assertions are retained.

**Reproduction or validation:** 见 http-fixture-probe-results.json；在 scratch 临时目录实例化 ServerHardeningTest，分别以默认值和 serve_forever(poll_interval=0.01) 执行同一用例。

### R4-032 · P2 · Open · Unreferenced browser kill escalation may never run

Location: [scripts/test_browser.cjs](../../scripts/test_browser.cjs), lines 11–15. Evidence: code_supported.

The browser termination escalation timer is unreferenced. If the leader exits and descendants close stdio while ignoring SIGTERM, the parent can exit before SIGKILL. Track cleanup completion; a real descendant test remains necessary.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/browser_probe.cjs

### R4-033 · P2 · Fixed · Empty browser test discovery reports success

Location: [scripts/test_browser.cjs](../../scripts/test_browser.cjs), lines 6–9. Evidence: confirmed.

An empty browser discovery previously wrote an empty timing object and returned success. The working-tree fix now rejects an empty scenario plan and includes regression coverage.

**Reproduction or validation:** node /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/browser_probe.cjs

### R4-034 · P3 · Open · Intervention history scans unrelated project events despite a page limit

Location: [adaptive_rsi/task_lifecycle.py](../../adaptive_rsi/task_lifecycle.py), lines 24–27. Evidence: confirmed.

The intervention query scans unrelated project events. A 10,000-event fixture with zero matches performs about 100,064 VM instructions. Index the validated task identifier and fixed intervention kinds, then verify sparse-history query work.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/data_probe_extra.py；输出 intervention_plan、events=10000、matching_interventions=0、vm_instructions=100064。

### R4-035 · P3 · Open · Validated candidate recovery reuses the patch but always reevaluates it

Location: [adaptive_rsi/pipeline.py](../../adaptive_rsi/pipeline.py), lines 355–415; related legacy #201. Evidence: code_supported.

validated_candidate recovery restores the patch but unconditionally reevaluates it. This is a conservative contract, not a correctness defect. Consider a separately sealed evaluation checkpoint only for demonstrably identical execution inputs.

**Reproduction or validation:** 代码支持：对已有 validated_candidate 的 execute_candidate 入口将 evaluator.evaluate 替换为计数器，会在补丁恢复后调用一次。未运行模型或完整 pipeline；执行位置和无条件调用可直接由 pipeline.py:355-415 复核。

### R4-036 · P3 · Fixed · Browser runner lacks scenario selection for focused validation

Location: [scripts/test_browser.cjs](../../scripts/test_browser.cjs), lines 9–14. Evidence: confirmed.

The runner now supports --list, scenario files and individual hardening groups while retaining full default discovery. Two selected real-browser scenarios completed in about 5.3 seconds; all 35 took 117.4 seconds. Bilingual development guidance now describes the focused loop.

**Reproduction or validation:** node --test tests/web_browser_runner.cjs; npm run test:browser -- browser_task_detail.cjs browser_hardening.cjs:routes

### R4-037 · P3 · Open · Every Python matrix job repeats full-suite coverage discovery

Location: [.github/workflows/checks.yml](../../.github/workflows/checks.yml), lines 140–141; related legacy #201. Evidence: confirmed.

All 12 Python matrix entries repeat complete coverage discovery. Local cost is small; centralize only after preserving interpreter-dependent discovery coverage. This is not a primary wall-clock bottleneck.

**Reproduction or validation:** python3 -B -c "import time; from scripts.check_test_matrix import check; s=time.monotonic(); print(check(),time.monotonic()-s)"

### R4-038 · P3 · Open · Overescaped sed expression records zero quick tests

Location: [.github/workflows/checks.yml](../../.github/workflows/checks.yml), lines 170–172. Evidence: confirmed.

The literal shell block overescapes sed grouping and records zero quick tests. The parent independently reproduced 32 executed tests yielding an empty count. Use structured counts or correct the expression and retain failing logs.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/metrics_probe.py

### R4-039 · P3 · Open · Matrix coverage check misses duplicate and extra test IDs

Location: [scripts/check_test_matrix.py](../../scripts/check_test_matrix.py), lines 13–25. Evidence: confirmed.

The matrix checker reduces a Counter to a set and checks only missing IDs, accepting duplicates and extra IDs. Validate each entry and explicitly account for intended platform/version overlap.

**Reproduction or validation:** python3 -B /Users/neg-entropy/.hermes/cache/scratch/adaptive-rsi-review-20261028/validation_probe.py

## Closeout record (2026-10-28)

This batch was completed and verified in the same working tree:

- Test runner: selectors are expanded to concrete tests before any process starts; a class together with one of its methods, selectors that contain each other, and empty selections are rejected. A worker may not start a runner for the module it is already running, and it exits when its parent disappears. A regression test plus a probe that intercepts `Popen` and proves zero spawns guard against the recursive subprocess exhaustion.
- P1: the release gate requires verification of the exact merged SHA; post-merge committed diffs decide `changed_files`; backup restore checks the migration ledger; malformed `result`/`feedback` rows keep their original bytes and stay recoverable.
- Evaluator: `candidate_full` uses the complete check set, detected additions run once, and an explicit empty `changed_files` still compares committed history instead of reusing frozen evidence.
- Also: the usage report shares one read snapshot, model catalog requests join the update barrier, automatic downloads resume after a restart, auth re-renders keep focus, the diagnostics dialog exists, and a malformed delivery row no longer blocks the scheduler.
- Verification: full Python run passed across 134 grouped selectors; Node 357 passed; Rust 22 passed; documentation links and bilingual pairs passed. Items that need real signing, key rotation, cross-volume startup, hosted CI and manual screen-reader evidence remain open; see the 201-item ledger.
