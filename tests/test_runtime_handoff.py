"""Offline regression evidence for runtime handoff boundaries."""
import datetime
import json
import sqlite3
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

    def test_shutdown_rejects_claim_once_the_admission_boundary_closes(self):
        self.runtime.pause(self.project, False)
        self.runtime.goal(self.project, 'Work that must not be claimed while closing')
        self.runtime._closing = True
        self.runtime.dispatch([(self.runtime.project(self.project), self.runtime.config(self.project))])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)

    def test_shutdown_cancels_unstarted_worker_and_requeues_claimed_task(self):
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
            limit = time.monotonic() + 2
            while self.runtime.shutdown_status()['phase'] != 'workers' and time.monotonic() < limit:
                time.sleep(.01)
            self.assertEqual(self.runtime.shutdown_status()['phase'], 'workers')
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            saved = self.store.one('SELECT state,ended FROM runs WHERE id=?', (run['id'],))
            self.assertEqual(saved['state'], 'interrupted')
            self.assertIsNotNone(saved['ended'])
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')
            self.assertEqual(self.runtime._future_runs, {})
        finally:
            gate.set()

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

    def test_experiment_acceptance_baseline_runs_on_the_evaluation_clock(self):
        """后台验收基线线程在提交前就必须处于评估相位，不能继承 preparation 预算。"""
        from adaptive_rsi import pipeline
        class HighRisk(FakeAgent):
            def invoke(self, role, prompt, workspace, *args, **kwargs):
                result = super().invoke(role, prompt, workspace, *args, **kwargs)
                if role == 'planning' and isinstance(result, dict):
                    return {**result, 'risk': 'high'}
                return result
        self.runtime.agent = HighRisk()
        run, task, project, config, base, directory, workspace, trial = self.experiment_inputs()
        observed = {}
        def baseline(*args, **kwargs):
            observed['phase'] = trial.get('_explicit_phase')
            return {'decision': 'pass', 'checks': {}}
        with patch('adaptive_rsi.readiness.inspect', return_value={'ready': True, 'items': []}), \
             patch.object(self.runtime.evaluator, 'evaluate_acceptance_baseline', side_effect=baseline), \
             patch.object(self.runtime.evaluator, 'evaluate',
                          return_value={'decision': 'pass', 'checks': {}, 'improvement': {'decision': 'pass'}}):
            outcome = pipeline.execute_candidate(
                self.runtime, trial, project, task, project['path'], base, directory, workspace,
                self.runtime.active_strategies(self.project, config), experiment=True)
        self.assertEqual(outcome['decision'], 'pass')
        self.assertEqual(observed, {'phase': 'evaluation'})

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
