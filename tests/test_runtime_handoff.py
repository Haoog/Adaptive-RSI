"""Offline regression evidence for runtime handoff boundaries."""
import concurrent.futures
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.config import validate
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_runtime import FakeAgent, fixture


class RuntimeHandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        self.project = self.runtime.add_project(fixture(self.root / 'project'))
        self.runtime.configure({'sources': {'inspection': False, 'issues': False},
                                'evolution': {'enabled': False}}, self.project)

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def claim(self):
        task = self.runtime.goal(self.project, 'Check boundary')
        self.runtime.pause(self.project, False)
        return task, self.store.claim(self.project, self.runtime.config(self.project))

    def test_shutdown_progress_is_readable_while_worker_drains(self):
        gate, began = threading.Event(), threading.Event()
        self.runtime.futures.add(self.runtime.pool.submit(lambda: (began.set(), gate.wait(5))))
        began.wait(1)
        closer = threading.Thread(target=self.runtime.close)
        closer.start()
        try:
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            status = self.runtime.shutdown_status()
            self.assertEqual(status['phase'], 'workers')
            self.assertEqual(status['remaining_workers'], 1)
        finally:
            gate.set()
            closer.join(5)
        self.assertEqual(self.runtime.shutdown_status()['phase'], 'completed')
        self.assertFalse(self.runtime.shutdown_status()['workers_timed_out'])

    def test_shutdown_bounds_non_cancellable_worker(self):
        gate, began = threading.Event(), threading.Event()
        _, run = self.claim()

        def wedged():
            began.set()
            gate.wait(10)

        future = self.runtime.pool.submit(wedged)
        with self.runtime._futures_lock:
            self.runtime.futures.add(future)
            self.runtime._future_runs[future] = run['id']
        self.assertTrue(began.wait(1))
        try:
            with patch('adaptive_rsi.runtime.WORKER_DRAIN_TIMEOUT', .2):
                started = time.monotonic()
                self.runtime.close()
                elapsed = time.monotonic() - started
            status = self.runtime.shutdown_status()
            self.assertLess(elapsed, 1)
            self.assertEqual(status['phase'], 'completed')
            self.assertTrue(status['workers_timed_out'])
            self.assertFalse(status['operations_timed_out'])
            events = self.store.query("SELECT data FROM events WHERE kind='shutdown_workers_timed_out'")
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]['data'])['runs'], [run['id']])
        finally:
            gate.set()

    def test_operations_close_bounds_a_wedged_operation(self):
        gate, began = threading.Event(), threading.Event()

        def wedged(operation, kind, parameters, stop):
            began.set()
            gate.wait(10)
            return {}

        with patch.object(self.runtime.operations, '_execute', wedged):
            operation = self.runtime.operations.start('diagnostics', {})['id']
        self.assertTrue(began.wait(1))
        try:
            with patch('adaptive_rsi.operations.OPERATION_DRAIN_TIMEOUT', .2):
                started = time.monotonic()
                self.runtime.operations.close()
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1)
            self.assertTrue(self.runtime.operations.timed_out)
            events = self.store.query("SELECT data FROM events WHERE kind='shutdown_operations_timed_out'")
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0]['data'])['operations'], [operation])
        finally:
            gate.set()
            limit = time.monotonic() + 5
            while (self.runtime.operations.active or self.runtime.operations.pending) and time.monotonic() < limit:
                time.sleep(.01)

    def test_shutdown_rejects_claim_once_the_admission_boundary_closes(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, 'Work that must not be claimed while closing')
        self.runtime._closing = True
        self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)

    def test_shutdown_cancels_unstarted_worker_and_requeues_claimed_task(self):
        # 被取消的 future 从未运行，workers 阶段没有可等待对象，close() 会毫秒级
        # 穿过 workers → maintenance → storage → completed；轮询 phase 只能采样到
        # 瞬时值，CPU 争用时必然错过。这里等待关闭线程结束后断言稳定终态。
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Queued across shutdown')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            # 断言关闭结束后的稳定终态，而不是轮询期间的瞬时 phase。
            self.assertEqual(self.runtime.shutdown_status()['phase'], 'completed')
            saved = self.store.one('SELECT state,ended FROM runs WHERE id=?', (run['id'],))
            self.assertEqual(saved['state'], 'interrupted')
            self.assertIsNotNone(saved['ended'])
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')
            self.assertEqual(self.runtime._future_runs, {})
            self.assertEqual(self.runtime.shutdown_status()['reconcile_failed'], 0)
        finally:
            gate.set()

    def test_shutdown_reconcile_failure_is_reported(self):
        # 已取消、从未启动的领取在关闭时必须落 interrupted；写入失败不能只留日志，
        # 否则 shutdown-status 会报告 completed，而数据库里仍有 claimed run。
        _, run = self.claim()
        self.assertIsNotNone(run)
        future = concurrent.futures.Future()
        self.assertTrue(future.cancel())
        with self.runtime._futures_lock:
            self.runtime._future_runs[future] = run['id']
        self.runtime.futures.add(future)
        with self.assertLogs('adaptive_rsi.runtime', level='ERROR') as logged:
            with patch.object(self.store, 'transition', side_effect=RuntimeError('database or disk is full')):
                self.runtime.close()
        self.assertIn('Failed to reconcile unstarted worker', logged.output[0])
        status = self.runtime.shutdown_status()
        self.assertEqual(status['phase'], 'completed')
        self.assertEqual(status['reconcile_failed'], 1)
        event = self.store.one("SELECT * FROM events WHERE kind='shutdown_reconcile_failed'")
        self.assertIsNotNone(event)
        self.assertEqual(event['run'], run['id'])
        data = json.loads(event['data'])
        self.assertEqual(data['run'], run['id'])
        self.assertIn('database or disk is full', data['error'])
        # 失败后 run 仍是残留，只能由下一次启动的 store.recover() 修复。
        saved = self.store.one('SELECT state,ended FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(saved['state'], 'planning')
        self.assertIsNone(saved['ended'])

    def test_shutdown_cancel_before_start_does_not_requeue_cancelled_task(self):
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Cancelled while shutting down')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            self.runtime.cancel_task(task)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'cancelled')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'cancelled')
        finally:
            gate.set()

    def test_shutdown_pause_before_start_keeps_task_interrupted(self):
        gate = threading.Event()
        blockers = [self.runtime.pool.submit(gate.wait, 5) for _ in range(4)]
        try:
            self.runtime.pause(self.project, False)
            task = self.runtime.goal(self.project, 'Paused while shutting down')
            self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
            run = self.store.one('SELECT * FROM runs WHERE task=?', (task,))
            self.assertIsNotNone(run)
            self.runtime.pause(self.project, True)
            closer = threading.Thread(target=self.runtime.close)
            closer.start()
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'interrupted')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'interrupted')
        finally:
            gate.set()

    def test_preparation_deadline_stops_real_subprocess_with_stage_receipt(self):
        import sys
        from adaptive_rsi.process import checked
        self.runtime.configure({'deadlines': {'preparation_seconds': 1}}, self.project)
        task, run = self.claim()
        def slow(*args):
            checked([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=20)
        started = time.monotonic()
        with patch('adaptive_rsi.delivery.baseline', side_effect=slow):
            self.runtime.process(run)
        self.assertLess(time.monotonic() - started, 4)
        row = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(row['state'], 'blocked')
        self.assertEqual(json.loads(row['result'])['failure']['stage'], 'preparation')

    def test_stage_budget_survives_interruption_and_bounds_the_retry(self):
        """中断后重启的同一任务只拿到剩余阶段预算，不再从零重计。"""
        from adaptive_rsi.process import Cancelled
        from adaptive_rsi.gitops import git
        self.runtime.configure({'deadlines': {'preparation_seconds': 100}}, self.project)
        task, run = self.claim()
        project = self.runtime.project(self.project)
        base = git(project['path'], 'rev-parse', 'HEAD')
        clock = [1000.0]
        def interrupted(*args, **kwargs):
            clock[0] += 20.0
            self.assertFalse(self.runtime.is_cancelled(run))
            # 检查点按 15 秒间隔落盘，硬崩溃最多损失一个间隔。
            self.assertEqual(self.store.setting('stage-budget:' + task),
                             {'preparation': 20.0, 'evaluation': 0.0, 'run': run['id']})
            clock[0] += 40.0
            raise Cancelled('Runtime restarted')
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]), \
             patch('adaptive_rsi.delivery.baseline', return_value=(project['path'], base, 'main')), \
             patch.object(self.runtime, 'execute_candidate', side_effect=interrupted):
            self.runtime.process(run)
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'interrupted')
        self.assertEqual(self.store.setting('stage-budget:' + task),
                         {'preparation': 60.0, 'evaluation': 0.0, 'run': run['id']})

        # 模拟进程重启：新 Store/Runtime、recover，然后按用户重试路径领取同一任务。
        self.runtime.close()
        store = Store(self.root / 'state')
        runtime = Runtime(store, FakeAgent())
        self.addCleanup(runtime.close)
        self.assertEqual(store.recover(), 0)
        runtime.retry(task)
        retried = store.claim(self.project, runtime.config(self.project))
        self.assertNotEqual(retried['id'], run['id'])
        observed = []
        def retry(*args, **kwargs):
            clock[0] += 39.0
            observed.append(runtime.is_cancelled(retried))
            clock[0] += 2.0
            observed.append(runtime.is_cancelled(retried))
            raise Cancelled('Model invocation cancelled or stage deadline exceeded')
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]), \
             patch('adaptive_rsi.delivery.baseline', return_value=(project['path'], base, 'main')), \
             patch.object(runtime, 'execute_candidate', side_effect=retry):
            runtime.process(retried)
        # 已耗 60 秒：剩 39 秒可用，第 41 秒触发 preparation 超时。
        self.assertEqual(observed, [False, True])
        row = store.one('SELECT state,result FROM runs WHERE id=?', (retried['id'],))
        self.assertEqual(row['state'], 'blocked')
        failure = json.loads(row['result'])['failure']
        self.assertEqual((failure['code'], failure['stage']), ('deadline', 'preparation'))
        # 已按超时收尾的尝试不再把额度带给下一次人工重试。
        self.assertIsNone(store.setting('stage-budget:' + task))

    def test_stage_budget_checkpoint_charges_preparation_during_model_call(self):
        """模型调用在途时的检查点仍计入准备阶段，硬崩溃不会丢掉整段准备耗时。"""
        from adaptive_rsi.process import Cancelled
        task, run = self.claim()
        clock = [1000.0]
        run['_stage_used'] = {'preparation': 0.0, 'evaluation': 0.0}
        run['_deadline_stage'], run['_deadline_started'] = 'preparation', clock[0]
        run['_deadline_checkpoint'] = clock[0]
        observed = []

        def model(role, text, workspace, project, run_id, config, cancelled, protected):
            clock[0] += 20.0
            self.assertFalse(cancelled())
            observed.append(self.store.setting('stage-budget:' + task))
            raise Cancelled('Process stopped')

        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.runtime.agent, 'invoke', side_effect=model):
            with self.assertRaises(Cancelled):
                self.runtime.invoke('planning', 'prompt', self.root / 'workspace', run)
        # 模型时钟独立计时，但它所属的准备阶段墙钟仍按同一个检查点落盘。
        self.assertEqual(observed, [{'preparation': 20.0, 'evaluation': 0.0, 'run': run['id']}])

    def test_trial_poll_checkpoints_the_owner_budget_on_its_own_schedule(self):
        """trial 轮询继续给所属 run 落盘：trial 时钟独立，但不能让父账丢掉整段耗时。"""
        task, parent = self.claim()
        clock = [1000.0]
        parent['_stage_used'] = {'preparation': 0.0, 'evaluation': 0.0}
        parent['_deadline_stage'], parent['_deadline_started'] = 'preparation', clock[0]
        parent['_deadline_checkpoint'] = clock[0]
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]):
            # trial 的时钟锚点也必须在同一时钟下建立：新开机机器的真实 monotonic
            # 只有几十秒，否则会被当成已耗时间而直接判定超时。
            trial = self.runtime.new_trial_context(parent)
            self.assertNotIn('_stage_used', trial)
            clock[0] += 20.0
            self.assertFalse(self.runtime.is_cancelled(trial))
            self.assertEqual(self.store.setting('stage-budget:' + task),
                             {'preparation': 20.0, 'evaluation': 0.0, 'run': parent['id']})
            # 父 run 的内存账本不被 trial 改写：落盘只做 used + 本次区间，重复调用幂等。
            self.assertEqual(parent['_stage_used'], {'preparation': 0.0, 'evaluation': 0.0})
            clock[0] += 10.0
            self.assertFalse(self.runtime.is_cancelled(trial))
            # 按 owner 的检查点节流，不因 trial 自身没有检查点而每次轮询都写盘。
            self.assertEqual(self.store.setting('stage-budget:' + task)['preparation'], 20.0)
            clock[0] += 5.0
            self.assertFalse(self.runtime.is_cancelled(trial))
            self.assertEqual(self.store.setting('stage-budget:' + task)['preparation'], 35.0)

    def test_terminal_run_budget_residue_is_discarded_before_loading(self):
        """终态提交后、finally 之前崩溃留下的键不能把旧消耗带进下一次尝试。"""
        from adaptive_rsi.gitops import git
        from adaptive_rsi.process import Cancelled
        self.runtime.configure({'deadlines': {'preparation_seconds': 100}}, self.project)
        task, stale = self.claim()
        project = self.runtime.project(self.project)
        base = git(project['path'], 'rev-parse', 'HEAD')
        # 上一次尝试已把终态写进 runs，但进程死在 process() 的 finally 之前。
        self.store.execute("UPDATE runs SET state='failed',ended=? WHERE id=?", (time.time(), stale['id']))
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        self.store.put_setting('stage-budget:' + task,
                               {'preparation': 60.0, 'evaluation': 5.0, 'run': stale['id']})
        self.runtime.retry(task)
        retried = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertNotEqual(retried['id'], stale['id'])
        # 中断交接仍要保留额度，所以清理只能发生在载入时按归属尝试判定，而不是 retry()。
        self.assertIsNotNone(self.store.setting('stage-budget:' + task))
        observed = []
        def restarted(*args, **kwargs):
            observed.append(dict(retried['_stage_used']))
            observed.append(self.store.setting('stage-budget:' + task))
            raise Cancelled('Runtime restarted')
        with patch('adaptive_rsi.delivery.baseline', return_value=(project['path'], base, 'main')), \
             patch.object(self.runtime, 'execute_candidate', side_effect=restarted):
            self.runtime.process(retried)
        # 失败后的新尝试从满额度起算，残留键在载入时已被丢弃。
        self.assertEqual(observed, [{'preparation': 0.0, 'evaluation': 0.0}, None])

    def test_unreadable_stage_budget_closes_run_and_releases_the_project(self):
        """预算读失败不能把 run 钉在 planning：该项目的普通任务否则全被冻到重启。"""
        task, run = self.claim()
        original = self.store.setting

        def unreadable(key, default=None):
            if str(key).startswith('stage-budget:'):
                raise sqlite3.OperationalError('database is locked')
            return original(key, default)

        with patch.object(self.store, 'setting', side_effect=unreadable), \
             patch('adaptive_rsi.runtime.STAGE_BUDGET_BACKOFF_SECONDS', 0):
            self.runtime.process(run)
        row = self.store.one('SELECT state,ended,result FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(row['state'], 'interrupted')
        self.assertIsNotNone(row['ended'])
        self.assertEqual(json.loads(row['result'])['failure']['code'], 'environment')
        claimed_task = self.store.one('SELECT state,attempts FROM tasks WHERE id=?', (task,))
        self.assertEqual(claimed_task['state'], 'queued')
        # 读预算失败属于基础设施故障而不是任务尝试：claim 自增的额度要归还。
        self.assertEqual(claimed_task['attempts'], 0)
        event = self.store.one("SELECT * FROM events WHERE run=? AND kind='stage_budget_unavailable'", (run['id'],))
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event['data'])['error'], 'OperationalError')
        # 未结 run 消失后来源互斥解除：同项目下一条普通任务能重新领取。
        self.runtime.goal(self.project, 'Second ordinary task')
        self.assertIsNotNone(self.store.claim(self.project, self.runtime.config(self.project)))

    def test_trial_phase_clock_ignores_parent_planning_and_propagates_timeout(self):
        _, parent = self.claim()
        parent['_deadline_started'] = time.monotonic() - 5000
        parent['_deadline_stage'] = 'preparation'
        trial = self.runtime.new_trial_context(parent)
        self.runtime.begin_phase(trial, 'evaluation')
        self.assertFalse(self.runtime.is_cancelled(trial))
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (parent['id'],))['state'], 'planning')
        trial['_deadline_started'] -= 4000
        self.assertTrue(self.runtime.is_cancelled(trial))
        self.runtime.propagate_deadline(parent, trial)
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')
        self.assertNotIn('_timed_out_stage', self.runtime.new_trial_context(parent))

    def test_known_phase_skips_runs_state_lookup_in_cancel_check(self):
        """阶段已由 _explicit_phase/_model_started 确定时，不再为推导 stage 查 runs.state。"""
        _, run = self.claim()
        self.runtime.begin_phase(run, 'evaluation')
        queries = []
        original = self.store.one

        def counting(sql, args=()):
            queries.append(sql)
            return original(sql, args)

        def stage_lookups():
            return [sql for sql in queries if sql.startswith('SELECT state FROM runs')]

        with patch.object(self.store, 'one', side_effect=counting):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual(stage_lookups(), [])
        # 省掉的只是阶段推断那一条；真正的取消判定仍要查任务与项目状态。
        self.assertTrue(any('FROM runs r JOIN tasks t' in sql for sql in queries))
        # _model_started 优先级高于 _explicit_phase，同样不需要该查询。
        run['_model_started'] = time.monotonic()
        queries.clear()
        with patch.object(self.store, 'one', side_effect=counting):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual(stage_lookups(), [])
        # 两者都缺席时仍回退到 runs.state：状态为 evaluating 才进入 evaluation 阶段。
        del run['_model_started'], run['_explicit_phase']
        self.store.execute("UPDATE runs SET state='evaluating' WHERE id=?", (run['id'],))
        queries.clear()
        with patch.object(self.store, 'one', side_effect=counting):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual(len(stage_lookups()), 1)
        self.assertEqual(run['_deadline_stage'], 'evaluation')

    def test_propagated_trial_stage_survives_parent_timeout_recheck(self):
        """propagate_deadline 写入的阶段是首个判定结论；父 run 自身时钟超时不覆盖它。"""
        from adaptive_rsi.gitops import git
        from adaptive_rsi.process import Cancelled
        _, parent = self.claim()
        config = self.runtime.config(self.project)
        # 父 run 的 preparation 时钟从 process() 入口起算，必然已超预算。
        parent['_deadline_stage'] = 'preparation'
        parent['_deadline_started'] = time.monotonic() - config['deadlines']['preparation_seconds'] - 1
        trial = self.runtime.new_trial_context(parent)
        trial['_timed_out_stage'] = 'evaluation'
        self.runtime.propagate_deadline(parent, trial)
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')
        # 变体间/交付前的父级 is_cancelled 命中自身超时分支，但不得改写阶段回执。
        self.assertTrue(self.runtime.is_cancelled(parent))
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')
        # _process 的 except Cancelled 路径必须按 evaluation 记 failure.stage。
        # stub 模拟真实顺序：trial 已把 evaluation 结论 propagate 到父 run、
        # 父 run 自身时钟仍超 preparation 预算，变体间取消复查不得改写它。
        project = self.runtime.project(self.project)
        base = git(project['path'], 'rev-parse', 'HEAD')
        def timed_out(*args):
            parent['_timed_out_stage'] = 'evaluation'
            parent['_deadline_started'] = time.monotonic() - config['deadlines']['preparation_seconds'] - 1
            self.assertTrue(self.runtime.is_cancelled(parent))
            self.assertEqual(parent['_timed_out_stage'], 'evaluation')
            raise Cancelled('Model invocation cancelled or stage deadline exceeded')
        with patch('adaptive_rsi.delivery.baseline', return_value=(project['path'], base, 'main')), \
             patch.object(self.runtime, 'execute_candidate', side_effect=timed_out):
            self.runtime.process(parent)
        row = self.store.one('SELECT state,result FROM runs WHERE id=?', (parent['id'],))
        self.assertEqual(row['state'], 'blocked')
        failure = json.loads(row['result'])['failure']
        self.assertEqual((failure['code'], failure['stage']), ('deadline', 'evaluation'))

    def test_later_trial_cannot_rewrite_the_first_propagated_timeout(self):
        """两个 trial 先后上报超时：父 run 保留首个结论，不被后一个 trial 改写。"""
        _, parent = self.claim()
        first = self.runtime.new_trial_context(parent)
        first['_timed_out_stage'] = 'evaluation'
        self.runtime.propagate_deadline(parent, first)
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')
        second = self.runtime.new_trial_context(parent)
        second['_timed_out_stage'] = 'preparation'
        self.runtime.propagate_deadline(parent, second)
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')

    def test_recorded_timeout_stops_variant_dispatch_on_a_fresh_clock(self):
        """变体循环的取消守卫读 is_cancelled；已有超时结论时不得因时钟重置而继续派发试验。"""
        _, parent = self.claim()
        trial = self.runtime.new_trial_context(parent)
        trial['_timed_out_stage'] = 'evaluation'
        self.runtime.propagate_deadline(parent, trial)
        # 父 run 重新切相，时钟是新的、预算充足：结论本身必须让取消判定成立，
        # 否则 evolution 的下一个 case/repetition/variant 会照常开跑并消耗模型预算。
        self.runtime.begin_phase(parent, 'preparation')
        self.assertTrue(self.runtime.is_cancelled(parent))
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')

    def test_evaluation_deadline_remains_active_during_model_call(self):
        _, run = self.claim()
        clock = [10.0]
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]):
            self.runtime.begin_phase(run, 'evaluation')
            clock[0] = 20.0
            def model(*args, **kwargs):
                clock[0] = 3610.0
                self.assertTrue(self.runtime.is_cancelled(run))
                self.assertEqual(run['_timed_out_stage'], 'evaluation')
                return {}
            from adaptive_rsi.process import Cancelled
            with patch.object(self.runtime.agent, 'invoke', side_effect=model), self.assertRaises(Cancelled):
                self.runtime.invoke('implementation', 'fixture', self.root, run)
        trial = self.runtime.new_trial_context(run)
        self.assertNotIn('_outer_deadline_stage', trial)
        self.assertNotIn('_outer_deadline_started', trial)

    def test_model_call_preserves_the_background_evaluation_deadline(self):
        _, run = self.claim()
        clock = [10.0]
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]):
            self.runtime.begin_phase(run, 'evaluation')
            clock[0] = 20.0
            with patch.object(self.runtime.agent, 'invoke', return_value={}):
                self.runtime.invoke('implementation', 'fixture', self.root, run)
            self.assertEqual(run['_deadline_stage'], 'evaluation')
            self.assertEqual(run['_deadline_started'], 10.0)
            clock[0] = 3610.0
            self.assertTrue(self.runtime.is_cancelled(run))
            self.assertEqual(run['_timed_out_stage'], 'evaluation')

    def test_model_call_preserves_the_background_preparation_deadline(self):
        """preparation 时钟在模型调用后按原起点恢复：T+M 与预算比较，不退回模型耗时。"""
        from adaptive_rsi.process import Cancelled
        _, run = self.claim()
        clock = [10.0]
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]):
            self.runtime.begin_phase(run, 'preparation')
            clock[0] = 810.0
            observed = {}
            def model(*args, **kwargs):
                observed['during'] = self.runtime.is_cancelled(run)
                clock[0] = 1010.0
                return {}
            with patch.object(self.runtime.agent, 'invoke', side_effect=model):
                # prep 已耗 800s、调用再耗 200s：1000s >= 900s，调用内即按 preparation 超时取消。
                with self.assertRaises(Cancelled):
                    self.runtime.invoke('planning', 'work', self.root, run)
            self.assertFalse(observed['during'])
            self.assertEqual(run['_deadline_stage'], 'preparation')
            self.assertEqual(run['_deadline_started'], 10.0)
            self.assertEqual(run['_timed_out_stage'], 'preparation')
            self.assertNotIn('_outer_deadline_stage', run)
            self.assertNotIn('_outer_deadline_started', run)

    def test_model_call_within_preparation_budget_does_not_restart_the_clock(self):
        """T+M 未超预算时调用正常返回，时钟保持调用前起点而非重置到返回时刻。"""
        _, run = self.claim()
        clock = [10.0]
        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]):
            self.runtime.begin_phase(run, 'preparation')
            clock[0] = 310.0
            with patch.object(self.runtime.agent, 'invoke', side_effect=lambda *a, **k: clock.__setitem__(0, 610.0) or {}):
                self.runtime.invoke('planning', 'work', self.root, run)
            self.assertEqual(run['_deadline_stage'], 'preparation')
            self.assertEqual(run['_deadline_started'], 10.0)
            clock[0] = 920.0
            self.assertTrue(self.runtime.is_cancelled(run))
            self.assertEqual(run['_timed_out_stage'], 'preparation')

    def test_concurrent_cancel_poll_cannot_rewrite_the_stage_clock(self):
        """invoke finally 与另一线程的 is_cancelled 交错后，时钟三元组仍等于顺序执行结果。"""
        _, claimed = self.claim()
        in_finally, poller_inside, worker_done = threading.Event(), threading.Event(), threading.Event()
        threads = {}

        class InterleavingRun(dict):
            """在摘除模型标记、轮询线程写阶段字段处插入确定性的交错点。"""

            def pop(self, key, *default):
                value = super().pop(key, *default)
                if key == '_model_started' and threading.current_thread() is threads.get('worker'):
                    # 真实竞态窗口：_model_started 已摘除、阶段时钟尚未重写的间隙。
                    in_finally.set()
                    poller_inside.wait(1.0)
                return value

            def __setitem__(self, key, value):
                if key == '_deadline_stage' and threading.current_thread() is threads.get('poller'):
                    # 让轮询线程的旧时钟写落在 worker 恢复时钟之后。
                    poller_inside.set()
                    worker_done.wait(5)
                super().__setitem__(key, value)

        run = InterleavingRun(claimed)
        clock = [100.0]
        observed = {}

        def model(*args, **kwargs):
            clock[0] += 10
            return {}

        def invoke():
            try:
                self.runtime.invoke('planning', 'work', self.root, run)
            finally:
                worker_done.set()

        def poll():
            observed['cancelled'] = self.runtime.is_cancelled(run)

        with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.runtime.agent, 'invoke', side_effect=model):
            self.runtime.begin_phase(run, 'preparation')
            started = run['_deadline_started']
            threads['worker'] = threading.Thread(target=invoke)
            threads['poller'] = threading.Thread(target=poll)
            threads['worker'].start()
            self.assertTrue(in_finally.wait(5), 'worker never reached the clock rewrite')
            threads['poller'].start()
            threads['worker'].join(5)
            threads['poller'].join(5)
        self.assertFalse(threads['worker'].is_alive())
        self.assertFalse(threads['poller'].is_alive())
        self.assertFalse(observed['cancelled'])
        # 与单线程 invoke 结束后的状态一致：恢复调用前阶段与起点，不产生超时标记。
        self.assertEqual(run['_deadline_stage'], 'preparation')
        self.assertEqual(run['_deadline_started'], started)
        self.assertNotIn('_timed_out_stage', run)

    def experiment_inputs(self):
        """准备一条试验运行的隔离工作区，返回 execute_candidate 需要的全部输入。"""
        from adaptive_rsi.gitops import git, snapshot
        task_id, run = self.claim()
        task = self.store.one('SELECT * FROM tasks WHERE id=?', (task_id,))
        project, config = self.runtime.project(self.project), self.runtime.config(self.project)
        base = git(project['path'], 'rev-parse', 'HEAD')
        directory = self.store.root / 'runs' / run['id']
        directory.mkdir(parents=True, exist_ok=True)
        workspace = snapshot(project['path'], directory / 'workspace', base)
        return run, task, project, config, base, directory, workspace, self.runtime.new_trial_context(run)

    def test_experiment_evaluation_runs_on_the_evaluation_clock(self):
        """实验路径不写 runs.state，评估阶段仍必须切到 evaluation 预算。"""
        from adaptive_rsi import pipeline
        run, task, project, config, base, directory, workspace, trial = self.experiment_inputs()
        observed = {}
        def evaluation(_baseline, _candidate, _checks, _target, _config, _cancel, **kwargs):
            # 901 秒：超过 preparation 的 900 秒，仍在 evaluation 的 3600 秒内。
            trial['_deadline_started'] -= 901
            observed['phase'] = trial.get('_explicit_phase')
            observed['cancelled'] = self.runtime.is_cancelled(trial)
            observed['timed_out_stage'] = trial.get('_timed_out_stage')
            return {'decision': 'pass', 'checks': {}, 'improvement': {'decision': 'pass'}}
        with patch('adaptive_rsi.readiness.inspect', return_value={'ready': True, 'items': []}), \
             patch.object(self.runtime.evaluator, 'evaluate', side_effect=evaluation):
            outcome = pipeline.execute_candidate(
                self.runtime, trial, project, task, project['path'], base, directory, workspace,
                self.runtime.active_strategies(self.project, config), experiment=True)
        self.assertEqual(outcome['decision'], 'pass')
        # 试验从不写 runs.state；若评估未切相位，这里会是 preparation 超时。
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'], 'planning')
        self.assertEqual(observed, {'phase': 'evaluation', 'cancelled': False, 'timed_out_stage': None})

    def test_experiment_acceptance_baseline_is_charged_to_preparation(self):
        """验收基线与实现并发执行，按 preparation 计时；评估相位实现结束后才起算。"""
        from adaptive_rsi import pipeline
        baseline_observed = threading.Event()
        class HighRisk(FakeAgent):
            def invoke(self, role, prompt, workspace, *args, **kwargs):
                if role == 'implementation':
                    # 先让基线线程读到相位，排除线程调度造成的另一个观察窗口。
                    assert baseline_observed.wait(5), 'acceptance baseline never observed its phase'
                result = super().invoke(role, prompt, workspace, *args, **kwargs)
                if role == 'planning' and isinstance(result, dict):
                    return {**result, 'risk': 'high'}
                return result
        self.runtime.agent = HighRisk()
        run, task, project, config, base, directory, workspace, trial = self.experiment_inputs()
        observed = {}
        def baseline(*args, **kwargs):
            observed['phase'] = trial.get('_explicit_phase')
            baseline_observed.set()
            return {'decision': 'pass', 'checks': {}}
        with patch('adaptive_rsi.readiness.inspect', return_value={'ready': True, 'items': []}), \
             patch.object(self.runtime.evaluator, 'evaluate_acceptance_baseline', side_effect=baseline), \
             patch.object(self.runtime.evaluator, 'evaluate',
                          return_value={'decision': 'pass', 'checks': {}, 'improvement': {'decision': 'pass'}}):
            outcome = pipeline.execute_candidate(
                self.runtime, trial, project, task, project['path'], base, directory, workspace,
                self.runtime.active_strategies(self.project, config), experiment=True)
        self.assertEqual(outcome['decision'], 'pass')
        # 实现期间不能提前切到评估相位：生产路径同样把实现与基线计入 preparation。
        self.assertEqual(observed, {'phase': 'preparation'})
        # 候选封存后才显式起算评估相位，评估仍拿到完整预算。
        self.assertEqual(trial['_explicit_phase'], 'evaluation')

    def test_experiment_implementation_time_is_charged_to_preparation(self):
        """实现耗时计入 preparation，不挤占评估预算；否则长实现会被误报评估超时。"""
        from adaptive_rsi import pipeline
        clock = [0.0]
        class Clock:
            def __getattr__(self, name):
                return getattr(time, name)
            def monotonic(self):
                return clock[0]
        class SlowImplementation(FakeAgent):
            def invoke(self, role, *args, **kwargs):
                result = super().invoke(role, *args, **kwargs)
                if role == 'planning' and isinstance(result, dict):
                    # 独立验收会产生与实现并发的基线线程，旧代码正是在这里提前切段。
                    return {**result, 'risk': 'high'}
                if role == 'implementation':
                    # 600 秒实现耗时：超过本用例 300 秒评估预算，仍在 1200 秒准备预算内。
                    clock[0] += 600
                return result
        self.runtime.agent = SlowImplementation()
        self.runtime.configure({'deadlines': {'preparation_seconds': 1200, 'evaluation_seconds': 300}},
                               self.project)
        run, task, project, config, base, directory, workspace, trial = self.experiment_inputs()
        clock[0] = trial['_deadline_started']
        observed = {}
        def evaluation(*args, **kwargs):
            observed['phase'] = trial.get('_explicit_phase')
            observed['cancelled'] = self.runtime.is_cancelled(trial)
            observed['timed_out_stage'] = trial.get('_timed_out_stage')
            return {'decision': 'pass', 'checks': {}, 'improvement': {'decision': 'pass'}}
        with patch('adaptive_rsi.runtime.time', Clock()), \
             patch('adaptive_rsi.readiness.inspect', return_value={'ready': True, 'items': []}), \
             patch.object(self.runtime.evaluator, 'evaluate_acceptance_baseline',
                          return_value={'decision': 'pass', 'checks': {}}), \
             patch.object(self.runtime.evaluator, 'evaluate', side_effect=evaluation):
            outcome = pipeline.execute_candidate(
                self.runtime, trial, project, task, project['path'], base, directory, workspace,
                self.runtime.active_strategies(self.project, config), experiment=True)
        self.assertEqual(outcome['decision'], 'pass')
        # 评估从实现结束后的封存点重新起算；实现耗时不得让试验按评估超时取消。
        self.assertEqual(observed, {'phase': 'evaluation', 'cancelled': False, 'timed_out_stage': None})

    def test_trial_evaluation_timeout_reaches_parent_when_pipeline_raises(self):
        from adaptive_rsi.evolution import Evolution
        from adaptive_rsi.gitops import git
        from adaptive_rsi.process import Cancelled
        _, parent = self.claim()
        project, config = self.runtime.project(self.project), self.runtime.config(self.project)
        spec = {'task': 'trial task', 'base': git(project['path'], 'rev-parse', 'HEAD'),
                'checks': [], 'repository': project['path'], 'overlay': None}
        def cancel(trial, *args, **kwargs):
            self.runtime.begin_phase(trial, 'evaluation')
            trial['_deadline_started'] -= config['deadlines']['evaluation_seconds'] + 1
            self.assertTrue(self.runtime.is_cancelled(trial))
            raise Cancelled('Model invocation cancelled or stage deadline exceeded')
        self.runtime.execute_candidate = cancel
        with self.assertRaises(Cancelled):
            Evolution(self.runtime).run_configuration(
                parent, project, {'id': 'case'}, spec, 'stable', 0, [],
                self.root / 'run', 'candidate', config)
        # 异常路径同样要把试验的 _timed_out_stage 交给父 run；否则只能记为 interrupted。
        self.assertEqual(parent['_timed_out_stage'], 'evaluation')

    def test_protected_overlay_evaluation_obeys_trial_deadline_and_propagates_it(self):
        from adaptive_rsi.evolution import Evolution
        from adaptive_rsi.gitops import git
        from adaptive_rsi.process import Cancelled
        _, parent = self.claim()
        project, config = self.runtime.project(self.project), self.runtime.config(self.project)
        overlay = self.root / 'protected-inputs'
        overlay.mkdir()
        (overlay / 'test.py').write_text('protected input')
        spec = {'task': 'trial task', 'base': git(project['path'], 'rev-parse', 'HEAD'),
                'checks': [], 'repository': project['path'], 'overlay': str(overlay)}
        clock = [10.0]
        observed = {}
        def execute(trial, _project, _task, _repository, _base, directory, *args, **kwargs):
            self.runtime.begin_phase(trial, 'evaluation')
            (directory / 'candidate').mkdir()
            clock[0] += 1
            return {'decision': 'pass'}
        def evaluate(_baseline, _candidate, _checks, _directory, _config, cancel, **kwargs):
            clock[0] = 10.0 + config['deadlines']['evaluation_seconds']
            observed['cancelled'] = cancel()
            if observed['cancelled'] and raises:
                raise Cancelled('Protected evaluation deadline exceeded')
            return {'decision': 'interrupted' if observed['cancelled'] else 'pass'}
        for raises in (False, True):
            with self.subTest(raises=raises):
                clock[0] = 10.0
                parent.pop('_timed_out_stage', None)
                with patch('adaptive_rsi.runtime.time.monotonic', side_effect=lambda: clock[0]), \
                     patch.object(self.runtime, 'execute_candidate', side_effect=execute), \
                     patch.object(self.runtime.evaluator, 'evaluate', side_effect=evaluate):
                    if raises:
                        with self.assertRaises(Cancelled):
                            Evolution(self.runtime).run_configuration(
                                parent, project, {'id': 'case'}, spec, str(raises), 0, [],
                                self.root / 'run', 'candidate', config)
                    else:
                        result = Evolution(self.runtime).run_configuration(
                            parent, project, {'id': 'case'}, spec, str(raises), 0, [],
                            self.root / 'run', 'candidate', config)
                        self.assertFalse(result['pass'])
                self.assertTrue(observed['cancelled'])
                self.assertEqual(parent.get('_timed_out_stage'), 'evaluation')

    def test_evaluation_deadline_marks_run_blocked_with_evaluation_stage(self):
        from adaptive_rsi.gitops import git
        from adaptive_rsi.process import Cancelled
        _, run = self.claim()
        project, config = self.runtime.project(self.project), self.runtime.config(self.project)
        base = git(project['path'], 'rev-parse', 'HEAD')
        def cancel(run, *args):
            run['_timed_out_stage'] = 'evaluation'
            raise Cancelled('Model invocation cancelled or stage deadline exceeded')
        with patch('adaptive_rsi.delivery.baseline', return_value=(project['path'], base, 'main')), \
             patch.object(self.runtime, 'execute_candidate', side_effect=cancel):
            self.runtime.process(run)
        row = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.assertEqual(row['state'], 'blocked')
        failure = json.loads(row['result'])['failure']
        self.assertEqual((failure['code'], failure['stage']), ('deadline', 'evaluation'))

    def test_postprocess_does_not_reload_completed_history_after_migration(self):
        for index in range(1000):
            self.store.put_setting('postprocess:history-' + str(index), {'state': 'completed', 'steps': []})
        self.runtime.drain_postprocess()
        loaded = []
        original = self.store.query
        def observe(sql, args=()):
            rows = original(sql, args)
            if 'settings' in sql:
                loaded.extend(rows)
            return rows
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime.drain_postprocess()
        self.assertEqual(loaded, [])

    def test_postprocess_recovers_without_duplicate_observation(self):
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified outcome'}, True)
        with patch.object(self.runtime, 'maybe_evolve', side_effect=RuntimeError('crash')):
            self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['last_error'], 'crash')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 1)
        self.runtime.drain_postprocess()
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 1)
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')

    def test_postprocess_delivery_crash_before_and_after_recovers_without_duplicates(self):
        for boundary in ('before', 'after'):
            with self.subTest(boundary=boundary):
                project = self.runtime.add_project(fixture(self.root / f'delivery-{boundary}'))
                self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
                self.runtime.pause(project, False)
                task = self.runtime.goal(project, f'Delivery crash {boundary}')
                run = self.store.claim(project, self.runtime.config(project))
                self.runtime.queue_postprocess(run)
                self.store.transition(run['id'], 'awaiting_approval', {
                    'summary': 'Verified outcome',
                    'evaluation': {'decision': 'pass'},
                    'review': {'decision': 'pass'},
                }, True)

                orig_deliver = self.runtime.deliver

                if boundary == 'before':
                    with patch.object(self.runtime, 'deliver', side_effect=RuntimeError('crash before delivery')):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash before delivery')
                    self.assertNotIn('delivery', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 0)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 0)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('delivery', record['steps'])
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                else:
                    def deliver_and_crash(run_id, automatic=False):
                        orig_deliver(run_id, automatic=automatic)
                        raise RuntimeError('crash after delivery')

                    with patch.object(self.runtime, 'deliver', side_effect=deliver_and_crash):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash after delivery')
                    self.assertNotIn('delivery', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 0)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('delivery', record['steps'])
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    publish_tasks = self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                                     (project, 'publish:' + run['id']))
                    self.assertEqual(len(publish_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

    def test_postprocess_delivery_effect_committed_marker_lost_is_not_reexecuted(self):
        # 副作用已落库、步骤标记未提交的崩溃窗口内，下一轮 drain 按持久效果
        # （publish:<run_id> 任务存在）跳过 deliver 调用，而不是再次执行副作用。
        project = self.runtime.add_project(fixture(self.root / 'delivery-effect'))
        self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
        self.runtime.pause(project, False)
        self.runtime.goal(project, 'Delivery durable effect')
        run = self.store.claim(project, self.runtime.config(project))
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {
            'summary': 'Verified outcome',
            'evaluation': {'decision': 'pass'},
            'review': {'decision': 'pass'},
        }, True)

        calls = []
        original = self.runtime.deliver
        def deliver_then_crash(run_id, automatic=False):
            calls.append(run_id)
            original(run_id, automatic=automatic)
            raise RuntimeError('crash after effect committed')
        with patch.object(self.runtime, 'deliver', side_effect=deliver_then_crash):
            self.runtime.drain_postprocess()
        self.assertEqual(calls, [run['id']])
        self.assertNotIn('delivery', self.store.setting('postprocess:' + run['id'])['steps'])

        # 第二轮 drain 仍挂着崩溃 mock：若再次调用 deliver 会追加调用并抛错；
        # 持久效果已存在时只能走 durable-guard 跳过，回执补齐标记并完成。
        with patch.object(self.runtime, 'deliver', side_effect=deliver_then_crash):
            self.runtime.drain_postprocess()
        self.assertEqual(calls, [run['id']])
        record = self.store.setting('postprocess:' + run['id'])
        self.assertEqual(record['state'], 'completed')
        self.assertIn('delivery', record['steps'])
        self.assertEqual(len(self.store.query('SELECT id FROM tasks WHERE project=? AND fingerprint=?',
                                              (project, 'publish:' + run['id']))), 1)

    def test_postprocess_evolution_enqueued_task_survives_watermark_loss(self):
        # maybe_evolve 先入队 system_evolve 任务再写 last-evolution: 水位；崩溃落在
        # 两者之间时任务已持久化而水位丢失。下一轮 drain 重放必须归还既有任务并补写
        # 水位，不能因水位已写而永久丢失这次进化机会（旧顺序的失败模式）。
        project = self.runtime.add_project(fixture(self.root / 'evolve-watermark'))
        self.runtime.configure({'evolution': {'enabled': True, 'every_completed': 1}}, project)
        self.runtime.pause(project, False)
        self.runtime.goal(project, 'Evolution watermark loss')
        run = self.store.claim(project, self.runtime.config(project))
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified outcome'}, True)

        stamp_key = 'last-evolution:' + project
        original = self.store.put_setting
        def lose_watermark(key, value):
            if key == stamp_key:
                raise OSError('crash before watermark write')
            return original(key, value)
        with patch.object(self.store, 'put_setting', side_effect=lose_watermark):
            self.runtime.drain_postprocess()
        # 任务已入队但水位未写：恰好是 kill -9 落在崩溃窗口留下的中间态。
        # （scheduler_scope 记录错误并吞掉异常，drain 本身不向上传播。）
        evolve = self.store.query("SELECT id,state FROM tasks WHERE project=? AND source='system_evolve'",
                                  (project,))
        self.assertEqual(len(evolve), 1)
        self.assertIsNone(self.store.setting(stamp_key))

        # 重放：任务指纹去重不产生第二条，水位补写，回执完成。
        self.runtime.drain_postprocess()
        evolve = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                  (project,))
        self.assertEqual(len(evolve), 1)
        self.assertEqual(self.store.setting(stamp_key), 1)
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        self.runtime.drain_postprocess()
        self.assertEqual(len(self.store.query(
            "SELECT id FROM tasks WHERE project=? AND source='system_evolve'", (project,))), 1)

    def test_postprocess_monitor_crash_before_and_after_recovers_without_duplicates(self):
        for boundary in ('before', 'after'):
            with self.subTest(boundary=boundary):
                project = self.runtime.add_project(fixture(self.root / f'monitor-{boundary}'))
                self.runtime.configure({'evolution': {'enabled': True, 'monitor_tasks': 5}}, project)
                self.runtime.pause(project, False)

                candidate_id = f'cand-monitor-{boundary}'
                self.store.execute(
                    "INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,'stable','{}',?,?)",
                    (candidate_id, project, 'instruction',
                     json.dumps({'monitor': {'passed': 0, 'failed': 0, 'runs': []}}), time.time())
                )

                task = self.runtime.goal(project, f'Monitor crash {boundary}')
                run = self.store.claim(project, self.runtime.config(project))
                self.store.execute('UPDATE runs SET strategy=? WHERE id=?',
                                   (json.dumps([{'id': candidate_id}]), run['id']))
                self.runtime.queue_postprocess(run)
                self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified outcome'}, True)

                orig_monitor = self.runtime.monitor_strategy

                if boundary == 'before':
                    with patch.object(self.runtime, 'monitor_strategy', side_effect=RuntimeError('crash before monitor')):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash before monitor')
                    self.assertNotIn('monitor', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 0)
                    self.assertEqual(ev['monitor']['runs'], [])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('monitor', record['steps'])
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 1)
                    self.assertEqual(ev['monitor']['runs'], [run['id']])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 1)
                    self.assertEqual(ev['monitor']['runs'], [run['id']])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                else:
                    def monitor_and_crash(proj, passed, run_obj, attributable=False):
                        orig_monitor(proj, passed, run_obj, attributable)
                        raise RuntimeError('crash after monitor')

                    with patch.object(self.runtime, 'monitor_strategy', side_effect=monitor_and_crash):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash after monitor')
                    self.assertNotIn('monitor', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 1)
                    self.assertEqual(ev['monitor']['runs'], [run['id']])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('monitor', record['steps'])
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 1)
                    self.assertEqual(ev['monitor']['runs'], [run['id']])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    row = self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate_id,))
                    ev = json.loads(row['evidence'])
                    self.assertEqual(ev['monitor']['passed'], 1)
                    self.assertEqual(ev['monitor']['runs'], [run['id']])
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

    def test_postprocess_evolution_crash_before_and_after_recovers_without_duplicates(self):
        for boundary in ('before', 'after'):
            with self.subTest(boundary=boundary):
                project = self.runtime.add_project(fixture(self.root / f'evolution-{boundary}'))
                self.runtime.configure({'evolution': {'enabled': True, 'every_completed': 1}}, project)
                self.runtime.pause(project, False)

                task = self.runtime.goal(project, f'Evolution crash {boundary}')
                run = self.store.claim(project, self.runtime.config(project))
                self.runtime.queue_postprocess(run)
                self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified outcome'}, True)

                orig_evolve = self.runtime.maybe_evolve

                if boundary == 'before':
                    with patch.object(self.runtime, 'maybe_evolve', side_effect=RuntimeError('crash before evolution')):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash before evolution')
                    self.assertNotIn('evolution', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 0)
                    self.assertIsNone(self.store.setting('last-evolution:' + str(project)))
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('evolution', record['steps'])
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 1)
                    self.assertEqual(self.store.setting('last-evolution:' + str(project)), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                else:
                    def evolve_and_crash(proj, cfg):
                        orig_evolve(proj, cfg)
                        raise RuntimeError('crash after evolution')

                    with patch.object(self.runtime, 'maybe_evolve', side_effect=evolve_and_crash):
                        self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'pending')
                    self.assertEqual(record['last_error'], 'crash after evolution')
                    self.assertNotIn('evolution', record['steps'])
                    self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 1)
                    self.assertEqual(self.store.setting('last-evolution:' + str(project)), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    record = self.store.setting('postprocess:' + run['id'])
                    self.assertEqual(record['state'], 'completed')
                    self.assertIn('evolution', record['steps'])
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 1)
                    self.assertEqual(self.store.setting('last-evolution:' + str(project)), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

                    self.runtime.drain_postprocess()
                    evolve_tasks = self.store.query("SELECT id FROM tasks WHERE project=? AND source='system_evolve'",
                                                    (project,))
                    self.assertEqual(len(evolve_tasks), 1)
                    self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills WHERE task=?', (task,))['n'], 1)

    def test_postprocess_rotates_past_a_full_batch_of_transient_failures(self):
        from adaptive_rsi.runtime import POSTPROCESS_BATCH
        for index in range(POSTPROCESS_BATCH + 1):
            self.runtime.queue_postprocess({'id': f'{index:04d}'})
        seen = []
        def process(key, identifier, record):
            seen.append(identifier)
            if identifier != f'{POSTPROCESS_BATCH:04d}':
                raise RuntimeError('temporary failure')
            self.store.put_setting(key, {**record, 'state': 'completed'})
        with patch.object(self.runtime, '_postprocess_run', side_effect=process):
            self.runtime.drain_postprocess()
            self.runtime.drain_postprocess()
        self.assertIn(f'{POSTPROCESS_BATCH:04d}', seen)

    def test_corrupt_postprocess_records_do_not_block_healthy_work(self):
        for identifier, value in [('corrupt', '{'), ('array', '[]'),
                                  ('bad-steps', '{"state":"pending","steps":null}')]:
            self.store.execute('INSERT INTO settings VALUES (?,?)', ('postprocess:' + identifier, value))
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        self.assertEqual(self.store.one("SELECT value FROM settings WHERE key='postprocess:corrupt'")['value'], '{')

    def test_postprocess_archived_project_does_not_block_another_project(self):
        other = self.runtime.add_project(fixture(self.root / 'other'))
        runs = []
        for project in (self.project, other):
            self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
            self.runtime.goal(project, 'Pending delivery')
            self.runtime.pause(project, False)
            run = self.store.claim(project, self.runtime.config(project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
            runs.append(run)
        self.runtime.archive_project(self.project, 'archive-project')
        with patch.object(self.runtime, 'deliver') as deliver:
            self.runtime.drain_postprocess()
        deliver.assert_called_once_with(runs[1]['id'], automatic=True)
        retired = self.store.setting('postprocess:' + runs[0]['id'])
        self.assertEqual(retired['state'], 'obsolete')
        self.assertIn('archived', retired['reason'])
        self.assertEqual(self.store.setting('postprocess:' + runs[1]['id'])['state'], 'completed')

    def test_postprocess_transient_failure_is_isolated_and_retryable(self):
        other = self.runtime.add_project(fixture(self.root / 'other'))
        runs = []
        for project in (self.project, other):
            self.runtime.configure({'delivery': {'mode': 'pr'}, 'evolution': {'enabled': False}}, project)
            self.runtime.goal(project, 'Pending delivery')
            self.runtime.pause(project, False)
            run = self.store.claim(project, self.runtime.config(project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
            runs.append(run)
        def transient(run_id, automatic=False):
            if run_id == runs[0]['id']:
                raise RuntimeError('temporary failure')
        with patch.object(self.runtime, 'deliver', side_effect=transient):
            self.runtime.drain_postprocess()
        pending = self.store.setting('postprocess:' + runs[0]['id'])
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['steps'], [])
        self.assertEqual(pending['last_error'], 'temporary failure')
        self.assertEqual(self.store.setting('postprocess:' + runs[1]['id'])['state'], 'completed')
        with patch.object(self.runtime, 'deliver') as deliver:
            self.runtime.drain_postprocess()
        deliver.assert_called_once_with(runs[0]['id'], automatic=True)
        self.assertEqual(self.store.setting('postprocess:' + runs[0]['id'])['state'], 'completed')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM skills')['n'], 2)

    def test_postprocess_pending_index_is_bounded_and_range_scanned(self):
        from adaptive_rsi import runtime as runtime_module
        batch = getattr(runtime_module, 'POSTPROCESS_BATCH', 20)
        self.runtime.pause(self.project, False)
        total = batch + 3
        for index in range(total):
            self.runtime.goal(self.project, 'Postprocess item ' + str(index))
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.queue_postprocess(run)
            self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        statements = []
        original = self.store.query
        def observe(sql, args=()):
            statements.append(sql)
            return original(sql, args)
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime.drain_postprocess()
        pending_sql = [sql for sql in statements if 'FROM settings' in sql and 'ORDER BY key LIMIT' in sql]
        self.assertTrue(pending_sql)
        self.assertTrue(all('LIKE' not in sql for sql in pending_sql))
        self.assertTrue(any('LIMIT' in sql and 'key>' in sql for sql in pending_sql))
        completed = self.store.one("SELECT count(*) AS n FROM settings WHERE key LIKE 'postprocess:%' "
                                   "AND json_valid(value) AND json_extract(value,'$.state')='completed'")['n']
        self.assertEqual(completed, batch)
        markers = self.store.one("SELECT count(*) AS n FROM settings WHERE key>=? AND key<?",
                                 ('postprocess-pending:', 'postprocess-pending;'))['n']
        self.assertEqual(markers, total - batch)
        plan = self.store.query("EXPLAIN QUERY PLAN SELECT key FROM settings WHERE key>=? AND key<? "
                                "ORDER BY key LIMIT ?", ('postprocess-pending:', 'postprocess-pending;', batch))
        detail = ' '.join(str(row.get('detail', '')) for row in plan).upper()
        self.assertTrue('PRIMARY KEY' in detail or 'INDEX' in detail)
        for _ in range(total):
            self.runtime.drain_postprocess()
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM settings WHERE key>=? AND key<?",
                                        ('postprocess-pending:', 'postprocess-pending;'))['n'], 0)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM settings WHERE key LIKE 'postprocess:%' "
                                        "AND json_valid(value) AND json_extract(value,'$.state')='completed'")['n'], total)

    def test_legacy_pending_postprocess_is_indexed_for_upgrade(self):
        task, run = self.claim()
        self.store.put_setting('postprocess:' + run['id'], {'state': 'pending', 'steps': [], 'created': time.time()})
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime._index_postprocess_pending()
        self.assertEqual(self.store.setting('postprocess-pending-index'), 2)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        statements = []
        original = self.store.query
        def observe(sql, args=()):
            statements.append(sql)
            return original(sql, args)
        with patch.object(self.store, 'query', side_effect=observe):
            self.runtime._index_postprocess_pending()
        self.assertEqual([sql for sql in statements if 'key>=?' in sql], [])

    def test_indexed_corrupt_postprocess_is_retired_without_blocking_healthy_work(self):
        self.store.execute("INSERT INTO settings VALUES ('postprocess:0000000000000000', '{')")
        self.store.execute("INSERT INTO settings VALUES ('postprocess-pending:0000000000000000', ?)",
                           (encode({}),))
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)
        self.runtime.drain_postprocess()
        self.assertEqual(self.store.setting('postprocess:' + run['id'])['state'], 'completed')
        self.assertEqual(self.store.one("SELECT value FROM settings WHERE key='postprocess:0000000000000000'")['value'], '{')
        self.assertIsNone(self.store.setting('postprocess-pending:0000000000000000'))

    def test_postprocess_deterministic_failure_escapes_to_failed_after_cap(self):
        # _postprocess_run 恒抛（确定性失败，如损坏 config/永久 ValueError）时：
        # 无尝试计数/终态会让 record 保持 pending+marker 每 ~5s 热循环（settings 写事务 +
        # WAL 增长 + CPU）。回归修复后：每次失败计入 attempts，达到
        # POSTPROCESS_MAX_ATTEMPTS 置 failed 终态、摘 marker、记 postprocess_abandoned，
        # 之后 drain 不再调用 _postprocess_run。
        from adaptive_rsi import runtime as runtime_module
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)

        calls = []
        def always_fails(key, identifier, record):
            calls.append(identifier)
            raise ValueError('permanent failure: corrupted config')
        self.runtime._postprocess_run = always_fails

        marker = 'postprocess-pending:' + run['id']
        key = 'postprocess:' + run['id']
        # 每个调度节拍重试一次：未达上限保持 pending+marker，达上限置 failed 终态。
        for _ in range(runtime_module.POSTPROCESS_MAX_ATTEMPTS):
            self.runtime.drain_postprocess()
        record = self.store.setting(key)
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['attempts'], runtime_module.POSTPROCESS_MAX_ATTEMPTS)
        self.assertIn('corrupted config', record['last_error'])
        self.assertIn('abandoned', record['reason'])
        # 终态逃逸：marker 摘除、写放弃事件、不再热循环。
        self.assertIsNone(self.store.setting(marker))
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='postprocess_abandoned' AND run=?", (run['id'],)))
        # 后续 drain 不再调用 _postprocess_run，也不再改写这条 record。
        before = len(calls)
        self.runtime.drain_postprocess()
        self.runtime.drain_postprocess()
        self.assertEqual(len(calls), before)
        self.assertEqual(self.store.setting(key)['state'], 'failed')

    def test_postprocess_failure_retries_immediately_until_cap(self):
        # 未达上限的失败保持 pending+marker 供下一节拍即时重试：重试节奏由调度循环
        # 自身的 ~5s 间隔承担，瞬态失败不人为延迟。成功后 marker 摘除、state completed。
        task, run = self.claim()
        self.runtime.queue_postprocess(run)
        self.store.transition(run['id'], 'awaiting_approval', {'summary': 'Verified'}, True)

        calls = []
        def fails_once_then_succeeds(key, identifier, record):
            calls.append(identifier)
            if len(calls) == 1:
                raise ValueError('transient failure')
            self.store.put_setting(key, {**record, 'state': 'completed'})
        self.runtime._postprocess_run = fails_once_then_succeeds

        key = 'postprocess:' + run['id']
        self.runtime.drain_postprocess()
        self.assertEqual(len(calls), 1)
        record = self.store.setting(key)
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(record['attempts'], 1)
        self.assertIsNotNone(self.store.setting('postprocess-pending:' + run['id']))
        # 下一节拍立刻重试（不附加墙钟退避），成功后摘除 marker。
        self.runtime.drain_postprocess()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.setting(key)['state'], 'completed')
        self.assertIsNone(self.store.setting('postprocess-pending:' + run['id']))

    def test_operation_crash_after_effect_is_unknown_and_never_reexecuted(self):
        operation = '0123456789abcdef'
        self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                           (operation, 'import', 'running', time.time()))
        self.runtime.operations.recover()
        row = self.runtime.operations.get(operation)
        self.assertEqual(row['state'], 'interrupted')
        self.assertEqual(row['result']['outcome'], 'unknown')

    def test_issue_sync_retains_original_diff_and_source_without_permission_change(self):
        task = self.store.add_task(self.project, 'issue', 'issue:1', 'Original', 'Original body',
                                   [{'number': 1, 'url': 'https://github.com/a/b/issues/1', 'updated': 'old'}])
        issue = {'number': 1, 'title': 'Original', 'body': 'New body', 'labels': [], 'state': 'open',
                 'updated_at': '2026-11-01T00:00:00Z', 'html_url': 'https://github.com/a/b/issues/1'}
        self.runtime.configure({'github': {'repository': 'a/b'}}, self.project)
        with patch('adaptive_rsi.runtime.checked', side_effect=lambda args, **kw: encode([issue]) if args[1] == 'api' else '[]'):
            self.runtime.sync_issues(self.runtime.project(self.project), self.runtime.config(self.project))
        row = self.store.one("SELECT run,data FROM events WHERE kind='issue_requirements_diff'")
        record = json.loads(row['data'])
        self.assertEqual(record['before']['body'], 'Original body')
        self.assertEqual(record['after']['body'], 'New body')
        self.assertEqual(record['source']['updated'], issue['updated_at'])
        self.assertIn('-Original body', record['diff'])
        self.assertIsNone(row['run'])
        self.assertEqual(self.runtime.config(self.project)['delivery']['mode'], 'local')

    def test_issue_sync_audit_rolls_back_with_task_write(self):
        task = self.store.add_task(self.project, 'issue', 'issue:1', 'Original', 'Original body',
                                   [{'number': 1, 'url': 'https://github.com/a/b/issues/1', 'updated': 'old'}])
        issue = {'number': 1, 'title': 'Original', 'body': 'New body', 'labels': [], 'state': 'open',
                 'updated_at': '2026-11-01T00:00:00Z', 'html_url': 'https://github.com/a/b/issues/1'}
        self.runtime.configure({'github': {'repository': 'a/b'}}, self.project)
        self.store.execute("CREATE TRIGGER reject_issue_audit BEFORE INSERT ON events "
                           "WHEN NEW.kind='issue_requirements_diff' "
                           "BEGIN SELECT RAISE(ABORT, 'Injected audit failure'); END")
        try:
            with patch('adaptive_rsi.runtime.checked',
                       side_effect=lambda args, **kw: encode([issue]) if args[1] == 'api' else '[]'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'Injected audit failure'):
                    self.runtime.sync_issues(self.runtime.project(self.project), self.runtime.config(self.project))
        finally:
            self.store.execute("DROP TRIGGER reject_issue_audit")
        row = self.store.one("SELECT title,body,evidence FROM tasks WHERE id=?", (task,))
        self.assertEqual((row['title'], row['body'], row['evidence']),
                         ('Original', 'Original body', encode([{'number': 1, 'url': 'https://github.com/a/b/issues/1', 'updated': 'old'}])))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE kind='issue_requirements_diff'"))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE kind='issues'"))

    def test_idle_projects_use_deadline_cache_and_wake_invalidates_it(self):
        self.runtime.pause(self.project, False)
        self.assertEqual(len(self.runtime.scheduler_projects(now=100)), 1)
        self.runtime.defer_project_tick(self.project, self.runtime.config(self.project), now=100)
        self.runtime.wake.clear()
        self.assertEqual(self.runtime.scheduler_projects(now=104), [])
        self.runtime.goal(self.project, 'New user work')
        self.assertEqual(len(self.runtime.scheduler_projects(now=104)), 1)

    def test_model_deadline_cancels_actual_command(self):
        import sys
        from adaptive_rsi.process import Cancelled, checked
        self.runtime.configure({'deadlines': {'model_seconds': 1}}, self.project)
        task, run = self.claim()
        def slow(role, text, workspace, project, run_id, config, cancel, protected):
            checked([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=20, cancel=cancel)
        with patch.object(self.runtime.agent, 'invoke', side_effect=slow):
            with self.assertRaises(Cancelled):
                self.runtime.invoke('planning', 'work', self.root, run)
        self.assertEqual(run['_timed_out_stage'], 'model')

    def test_evaluation_deadline_has_its_own_clock(self):
        task, run = self.claim()
        run['_deadline_started'], run['_deadline_stage'] = time.monotonic() - 5000, 'preparation'
        self.store.transition(run['id'], 'evaluating')
        self.assertFalse(self.runtime.is_cancelled(run))
        run['_deadline_started'] -= 4000
        self.assertTrue(self.runtime.is_cancelled(run))
        self.assertEqual(run['_timed_out_stage'], 'evaluation')

    def test_explicit_phase_skips_the_stage_inference_query(self):
        """已显式切相时 is_cancelled 不再为推断 stage 查询 runs 表。"""
        _, run = self.claim()
        self.runtime.begin_phase(run, 'evaluation')
        original = self.store.one
        statements = []
        def observe(sql, *args, **kwargs):
            statements.append(sql)
            return original(sql, *args, **kwargs)
        with patch.object(self.store, 'one', side_effect=observe):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual([sql for sql in statements if sql == 'SELECT state FROM runs WHERE id=?'], [])

    def test_model_call_skips_the_stage_inference_query(self):
        """模型调用进行中（_model_started）同样不触发 stage 推断查询。"""
        _, run = self.claim()
        run['_deadline_started'], run['_deadline_stage'] = time.monotonic(), 'model'
        run['_model_started'] = time.monotonic()
        original = self.store.one
        statements = []
        def observe(sql, *args, **kwargs):
            statements.append(sql)
            return original(sql, *args, **kwargs)
        with patch.object(self.store, 'one', side_effect=observe):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual([sql for sql in statements if sql == 'SELECT state FROM runs WHERE id=?'], [])

    def test_stage_inference_query_runs_only_without_explicit_phase(self):
        """未切相也未进入模型调用时，仍按 runs.state 推断 evaluation 相位。"""
        _, run = self.claim()
        run['_deadline_started'], run['_deadline_stage'] = time.monotonic(), 'preparation'
        self.store.transition(run['id'], 'evaluating')
        original = self.store.one
        statements = []
        def observe(sql, *args, **kwargs):
            statements.append(sql)
            return original(sql, *args, **kwargs)
        with patch.object(self.store, 'one', side_effect=observe):
            self.assertFalse(self.runtime.is_cancelled(run))
        self.assertEqual(len([sql for sql in statements if sql == 'SELECT state FROM runs WHERE id=?']), 1)
        self.assertEqual(run['_deadline_stage'], 'evaluation')

    def test_idle_deadline_selection_scales_to_thousand_projects(self):
        import time as clock
        now = clock.time()
        for count in (100, 1000):
            with self.store.connect() as db:
                db.executemany('INSERT OR IGNORE INTO projects VALUES (?,?,?,?,?,?)',
                    [('load' + str(i), 'Load', '/offline/load/' + str(i), '{}', 0, now) for i in range(count)])
            self.runtime._project_deadlines = {'load' + str(i): now + 3600 for i in range(count)}
            self.runtime._project_deadlines[self.project] = now + 3600
            self.runtime.wake.clear()
            began = clock.process_time()
            for _ in range(10):
                self.assertEqual(self.runtime.scheduler_projects(now), [])
            print('deadline_selection', {'projects': count, 'ticks': 10, 'queries': 10,
                                         'cpu_seconds': clock.process_time() - began})
            self.runtime._project_deadlines['load0'] = now
            self.assertEqual([row['id'] for row in self.runtime.scheduler_projects(now)], ['load0'])

    def test_operation_effect_receipt_crash_recovers_receipt_without_repeating_effect(self):
        operation = 'fedcba9876543210'
        self.store.execute("INSERT INTO operations VALUES (?,?,?,'{}',?,NULL)",
                           (operation, 'import', 'queued', time.time()))
        effect = self.root / 'effect'
        def execute(*args):
            effect.write_text('one effect')
            return {'id': 'local-evidence'}
        original = self.store.execute
        def crash(sql, *args, **kwargs):
            if sql.startswith('UPDATE operations SET state=?,result='):
                raise OSError('database unavailable after effect')
            return original(sql, *args, **kwargs)
        with patch.object(self.runtime.operations, '_execute', side_effect=execute) as call, patch.object(self.store, 'execute', side_effect=crash):
            self.runtime.operations._run(operation, 'import', {}, threading.Event())
            self.assertEqual(call.call_count, 1)
        self.runtime.operations.recover()
        self.assertEqual(self.runtime.operations.get(operation)['state'], 'completed')
        self.assertEqual(effect.read_text(), 'one effect')

    def test_shutdown_rejects_new_worker_admission(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, 'Do not start after stop')
        self.runtime.stop.set()
        self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)

    def test_worker_admission_failure_does_not_consume_retries(self):
        from adaptive_rsi.runtime import ADMISSION_BACKOFF_SECONDS
        self.runtime.pause(self.project, False)
        task = self.runtime.goal(self.project, 'Admission failure keeps the retry quota')
        config = self.runtime.config(self.project)
        before = self.store.one('SELECT attempts FROM tasks WHERE id=?', (task,))
        with patch.object(self.runtime.pool, 'submit', side_effect=RuntimeError("can't start new thread")):
            with self.assertRaises(RuntimeError):
                self.runtime.dispatch([(self.runtime.project(self.project), config)])
            # 退避窗口内不重新提交，也不新建第二条 run。
            self.runtime.dispatch([(self.runtime.project(self.project), config)])
        after = self.store.one('SELECT state,attempts,available FROM tasks WHERE id=?', (task,))
        self.assertEqual(after['state'], 'queued')
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertGreater(after['available'], time.time())
        self.assertLessEqual(after['available'], time.time() + ADMISSION_BACKOFF_SECONDS)
        runs = self.store.query('SELECT * FROM runs WHERE task=?', (task,))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['state'], 'interrupted')
        self.assertIsNotNone(runs[0]['ended'])
        event = self.store.one("SELECT * FROM events WHERE run=? AND kind='infrastructure_retry'",
                               (runs[0]['id'],))
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event['data'])['code'], 'admission')
        self.assertEqual(json.loads(event['data'])['retry_at'], after['available'])
        # 退避到期后恢复提交：同一任务重新领取并在线程池里执行，attempts 只按真实尝试自增一次。
        self.store.execute('UPDATE tasks SET available=0 WHERE id=?', (task,))
        admitted = []
        with patch.object(self.runtime, 'process', lambda run: admitted.append(run['id'])):
            self.runtime.dispatch([(self.runtime.project(self.project), config)])
            limit = time.monotonic() + 5
            while not admitted and time.monotonic() < limit:
                time.sleep(.01)
        second = self.store.one('SELECT * FROM runs WHERE task=? AND id<>?', (task, runs[0]['id']))
        self.assertIsNotNone(second)
        self.assertEqual(admitted, [second['id']])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs WHERE task=?', (task,))['n'], 2)
        self.assertEqual(self.store.one('SELECT state,attempts FROM tasks WHERE id=?', (task,)),
                         {'state': 'planning', 'attempts': before['attempts'] + 1})

    def test_auto_evolution_skips_unchanged_failed_inputs_until_inputs_change(self):
        from adaptive_rsi.evolution import Evolution
        self.runtime.configure({'evolution': {'enabled': True, 'every_completed': 1}}, self.project)
        done = self.store.add_task(self.project, 'goal', 'done-1', 'Done once', 'done once')
        self.store.execute('INSERT OR IGNORE INTO task_completions VALUES (?,?,?)',
                           (done, self.project, time.time()))
        config = self.runtime.config(self.project)
        signature = Evolution(self.runtime).attempt_due(self.project, config)[1]
        self.store.put_setting('evolution-attempt:' + self.project,
                               {'signature': signature, 'status': 'insufficient_evidence', 'at': time.time()})
        self.runtime.maybe_evolve(self.project, config)
        self.assertFalse(self.store.query("SELECT 1 FROM tasks WHERE source='system_evolve'"))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='evolution_not_repeated'"))
        self.assertEqual(self.store.setting('last-evolution:' + self.project), 1)
        again = self.store.add_task(self.project, 'goal', 'done-2', 'Done twice', 'done twice')
        self.store.execute('INSERT OR IGNORE INTO task_completions VALUES (?,?,?)',
                           (again, self.project, time.time()))
        self.runtime.configure({'triage': {'policy': 'prefer verifiable evidence'}}, self.project)
        self.runtime.maybe_evolve(self.project, self.runtime.config(self.project))
        self.assertTrue(self.store.one("SELECT 1 FROM tasks WHERE source='system_evolve' AND state='queued'"))

    def test_evolution_force_retry_marker_is_consumed_and_task_is_retryable(self):
        from adaptive_rsi.gitops import git
        self.runtime.configure({'evolution': {'enabled': True}}, self.project)
        task = self.runtime.request(self.project, 'evolve')
        self.runtime.pause(self.project, False)
        calls = []
        def fake_propose(evolution, project, workspace, directory, run, force=False):
            calls.append(force)
            return {'status': 'insufficient_evidence', 'reason': 'No fresh holdout'}
        path = self.runtime.project(self.project)['path']
        base = git(path, 'rev-parse', 'HEAD')
        with patch('adaptive_rsi.delivery.baseline', return_value=(path, base, 'main')), \
                patch('adaptive_rsi.evolution.Evolution.propose_and_evaluate',
                      autospec=True, side_effect=fake_propose):
            run = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(run)
            self.assertEqual(calls, [False])
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (run['id'],))['state'],
                             'insufficient_evidence')
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'],
                             'insufficient_evidence')
            self.runtime.retry(task, force=True)
            self.assertIsNotNone(self.store.setting('evolution-force:' + task))
            fresh = self.store.claim(self.project, self.runtime.config(self.project))
            self.runtime.process(fresh)
            self.assertEqual(calls, [False, True])
            self.assertIsNone(self.store.setting('evolution-force:' + task))
            self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (fresh['id'],))['state'],
                             'insufficient_evidence')

    def test_forced_retry_is_rejected_for_non_evolution_tasks(self):
        task = self.store.add_task(self.project, 'goal', 'forced-goal', 'Goal', 'Goal')
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        with self.assertRaisesRegex(ValueError, 'evolution'):
            self.runtime.retry(task, force=True)

    def test_stage_limits_reject_invalid_values(self):
        self.assertEqual(validate({'deadlines': {'preparation_seconds': 2}})['deadlines']['preparation_seconds'], 2)
        for value in [0, -1, True, 86401]:
            with self.assertRaises(ValueError):
                validate({'deadlines': {'preparation_seconds': value}})

    def test_scheduled_wall_time_rejects_gap_and_requires_fold(self):
        task, _ = self.claim()
        # Use a separate waiting task; active work cannot be rescheduled.
        task = self.runtime.goal(self.project, 'Later work')
        with self.assertRaisesRegex(ValueError, 'nonexistent'):
            self.runtime.schedule_task(task, '2027-03-14T02:30:00', 'America/New_York')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York')
        first = self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York', fold=0)
        second = self.runtime.schedule_task(task, '2027-11-07T01:30:00', 'America/New_York', fold=1)
        self.assertEqual(second['at'] - first['at'], 3600)
        self.runtime.configure({'schedule': {'timezone': 'UTC'}}, self.project)
        self.assertEqual(self.store.one('SELECT available FROM tasks WHERE id=?', (task,))['available'], second['at'])
        self.assertEqual(self.store.setting('task-schedule:' + task)['timezone'], 'America/New_York')

    def test_commit_rejects_a_desktop_update_without_an_observable_owner(self):
        # desktop_owner_pid 缺失时写入 installing 会让屏障两条清除路径都不可达，
        # 准入被永久楔死；commit 必须在落盘前拒绝并释放 draining 屏障。
        from adaptive_rsi.lifecycle import KEY, barrier, readiness
        self.runtime.updates.desktop_managed = True
        for owner in (None, 0, 1, True, '123'):
            with self.subTest(owner=owner):
                prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
                self.runtime.updates.desktop_owner_pid = owner
                with self.assertRaisesRegex(ValueError, 'owner'):
                    readiness(self.runtime, 'commit', lease=prepared['lease'])
                self.assertIsNone(self.store.setting(KEY))
                self.assertIsNone(barrier(self.runtime))

    @unittest.skipUnless(os.name == 'posix', 'Process-zero liveness follows POSIX semantics')
    def test_installing_barrier_recovers_after_the_owner_process_exits(self):
        from adaptive_rsi.lifecycle import KEY, barrier
        owner = subprocess.Popen([sys.executable, '-c', 'pass'])
        owner.wait(timeout=10)
        self.store.put_setting(KEY, {'version': '9.0.0', 'lease': 'lease-token', 'phase': 'installing',
                                     'expires': time.time() + 3600, 'owner_pid': owner.pid,
                                     'committed': time.time()})
        self.assertIsNone(barrier(self.runtime))
        self.assertIsNone(self.store.setting(KEY))

    def test_stale_installing_barrier_recovers(self):
        # 存活但已不是安装器的 pid、或挂起的安装器，曾让 installing 屏障永不清理：
        # expires 不参与判定，os.kill 又始终成功。硬上界必须在窗口后放行准入。
        from adaptive_rsi.lifecycle import INSTALL_WINDOW, KEY, barrier, readiness
        self.assertGreaterEqual(INSTALL_WINDOW, 3600)
        self.runtime.updates.desktop_managed = True
        prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
        self.runtime.updates.desktop_owner_pid = os.getpid()
        state = readiness(self.runtime, 'commit', lease=prepared['lease'])['barrier']
        self.store.put_setting(KEY, {**state, 'committed': time.time() - INSTALL_WINDOW - 60})
        self.assertIsNone(barrier(self.runtime))
        self.assertIsNone(self.store.setting(KEY))
        event = json.loads(self.store.one(
            "SELECT data FROM events WHERE kind='desktop_update_recovered' ORDER BY rowid DESC")['data'])
        self.assertEqual(event['reason'], 'install_expired')
        self.assertFalse(self.runtime.updates.is_busy())
        fresh = readiness(self.runtime, 'prepare', '9.0.1')['barrier']
        self.assertEqual((fresh['phase'], fresh['version']), ('draining', '9.0.1'))

    def test_fresh_installing_barrier_is_kept(self):
        from adaptive_rsi.lifecycle import KEY, barrier, readiness
        self.runtime.updates.desktop_managed = True
        prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
        self.runtime.updates.desktop_owner_pid = os.getpid()
        state = readiness(self.runtime, 'commit', lease=prepared['lease'])['barrier']
        kept = barrier(self.runtime)
        self.assertEqual((kept['lease'], kept['owner_pid'], kept['phase']),
                         (state['lease'], os.getpid(), 'installing'))
        self.assertIsNotNone(self.store.setting(KEY))
        self.assertTrue(self.runtime.updates.is_busy())
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE kind='desktop_update_recovered'"))
        if state.get('owner_start') is not None:
            self.assertEqual(kept['owner_start'], state['owner_start'])

    @unittest.skipUnless(os.path.exists('/proc/self/stat'), 'PID identity requires procfs')
    def test_reused_installer_pid_recovers_as_owner_exited(self):
        from adaptive_rsi.lifecycle import KEY, barrier, readiness
        self.runtime.updates.desktop_managed = True
        prepared = readiness(self.runtime, 'prepare', '9.0.0')['barrier']
        self.runtime.updates.desktop_owner_pid = os.getpid()
        state = readiness(self.runtime, 'commit', lease=prepared['lease'])['barrier']
        self.assertIsInstance(state['owner_start'], str)
        self.store.put_setting(KEY, {**state, 'owner_start': state['owner_start'] + '0'})
        self.assertIsNone(barrier(self.runtime))
        event = json.loads(self.store.one(
            "SELECT data FROM events WHERE kind='desktop_update_recovered' ORDER BY rowid DESC")['data'])
        self.assertEqual(event['reason'], 'owner_exited')

    def test_prepare_recovers_a_barrier_record_without_a_lease(self):
        from adaptive_rsi.lifecycle import KEY, readiness
        self.runtime.updates.desktop_managed = True
        self.store.put_setting(KEY, {'version': '9.0.0', 'phase': 'draining',
                                     'expires': time.time() + 3600})
        prepared = readiness(self.runtime, 'prepare', '9.0.0')
        self.assertIsInstance(prepared['barrier']['lease'], str)
        self.assertEqual(self.store.setting(KEY)['lease'], prepared['barrier']['lease'])
        self.assertIsNotNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='desktop_update_recovered'"))

    def test_non_object_barrier_record_is_recovered_instead_of_crashing_admission(self):
        from adaptive_rsi.lifecycle import KEY, barrier
        self.store.put_setting(KEY, ['not', 'a', 'barrier'])
        self.assertIsNone(barrier(self.runtime))
        self.assertIsNone(self.store.setting(KEY))
