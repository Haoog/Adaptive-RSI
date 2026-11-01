# 测试运行器递归事故与防回归规则

**简体中文** | [English](test-runner-recursion-20261028.en.md)

## 事故与影响

维护者报告 2026-10-28 同类故障发生两次。已定位的故障是测试运行器递归派发自身，持续创建独立会话的 Python 子进程，耗尽系统进程资源，随后 Hermes 后端失联。关闭桌面窗口不能保证这些进程随之退出。这里记录共同根因，不虚构两次事故各自独立的完整时间线。

维护者提供的原始诊断包含 `fork: Resource temporarily unavailable (35)` 和十多层嵌套的 `[group 2]`。这类证据说明进程递归；不能把恢复后的安装引导页当作 Hermes 安装丢失的证明。

## 根因与修复

`tests/test_validation_planning.py` 的测试同时选择整个测试类与其中一个方法，要求运行器拒绝重叠。旧运行器只检查选择器字符串是否重复，没有在创建进程前校验展开后的用例重叠。子进程因此重新执行调用运行器的测试，再创建下一代进程。`start_new_session=True` 又使后代脱离父会话。

修复已经随 [PR #92](https://github.com/Haoog/Adaptive-RSI/pull/92) 合并，位于提交 `fa09f3b`：

- `scripts/test_selected.py` 在派发前检查选择器包含关系及具体用例重复，拒绝空选择；完全重复的选择器按幂等处理，去重后执行一次。
- `scripts/test_parallel.py` 使用 `RSI_TEST_RUNNING` 拒绝重入 worker 正在执行的模块，最多派发四个 worker。为隔离夹具执行其他模块的受控路径仍存在，因此不能把该防护描述成禁止任何嵌套进程。
- 使用 `scripts.test_worker` 的运行路径在父进程消失后退出；取消时运行器终止并回收自己拥有的进程组。

## 后续执行规则

1. 修改运行器或包含运行器调用的测试前，先读这份记录和相关调用路径。
2. 使用拦截 `subprocess.Popen` 的方式验证错误选择或自身模块重入，断言创建进程次数为零。禁止通过实际放开递归来重现故障。
3. 运行器单测不得真实派发包含自身的测试模块。测试选择与分组时模拟 `run_groups`；测试启动边界时使用普通拦截函数，不能使用会触发 mock 特例的包装来代替防护断言。
4. 全量回归每个批次只安排一个负责人；先运行安全回归，再启动最多四个 worker。观察到重复嵌套的组前缀或进程增长异常时立即停止该运行，不继续启动第二套全量检查。
5. 不能为了让测试通过、缩短验证或允许嵌套而删除重叠校验、正在运行模块校验、进程组清理或父进程监测。

## 验证入口

在可信仓库中先执行无真实 worker 派发的关键回归：

```sh
python3 -B -m unittest \
  tests.test_validation_planning.ValidationPlanningTest.test_selected_rejects_empty_and_overlapping_expansions \
  tests.test_validation_planning.ValidationPlanningTest.test_worker_refuses_to_spawn_another_runner -q
```

随后可执行 `python3 -B -m unittest tests.test_validation_planning tests.test_release_parallel -q`。后者包含有界的真实子进程生命周期夹具，不会真实重现无限递归。通过回归是对这些已定义边界的验证，不能作为所有未来进程故障都不可能发生的保证。
