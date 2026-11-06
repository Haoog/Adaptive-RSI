"""Evidence integrity and dataset lifecycle acceptance without external models."""
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.config import fingerprint
from adaptive_rsi.evolution import Evolution, case_spec, paired_summary
from adaptive_rsi.store import Store, encode


class AuditEvolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.config = {'evolution': {'monitor_tasks': 1}}
        self.evolution = Evolution(SimpleNamespace(store=self.store, config=lambda project: self.config))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def candidate(self, evidence=None):
        self.store.execute('INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)',
                           ('c', 'p', 'strategy', 'stable', encode({'guidance': 'new', 'triggers': []}), encode(evidence or {}), 1))

    def setup_proposal(self, holdout_uses=0):
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git
        import threading
        repo = fixture(self.root / 'repo')
        self.config = merge(DEFAULTS, {'evolution': {'min_cases_per_split': 1, 'repeats': 2}})
        self.calls = {'proposal': 0, 'trial': 0}
        self.prompts = []
        def invoke(stage, prompt, workspace, run):
            self.calls['proposal'] += 1
            self.prompts.append(prompt)
            return {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': []}
        def execute(*args, **kwargs):
            self.calls['trial'] += 1
            assert kwargs.get('experiment') is True
            return {'decision': 'pass' if args[7] else 'fail'}
        runtime = self.evolution.runtime
        runtime.invoke = invoke
        runtime.active_strategies = lambda project: []
        runtime.execute_candidate = execute
        runtime.is_cancelled = lambda run: False
        runtime.dispatch_lock = threading.RLock()
        for split in ('validation', 'holdout'):
            spec = {'task': split + ' secret task', 'base': git(repo, 'rev-parse', 'HEAD'),
                    'checks': CHECKS, 'source': 'real', 'signature': split,
                    'repository': str(repo), 'overlay': None}
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               (split, 'p', split, split, encode(spec), holdout_uses if split == 'holdout' else 0, 1))
        return {'id': 'p'}, repo, self.root / 'run', {'id': 'run', 'task': 'task', 'config': encode(self.config)}

    def test_malformed_case_specs_are_skipped_without_blocking_the_proposal(self):
        args = self.setup_proposal()
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('broken-case', 'p', 'broken', 'validation', '{broken', 0, 5))
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('partial-case', 'p', 'partial', 'validation', encode({'task': 'x'}), 0, 6))
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('broken-development', 'p', 'broken-dev', 'development', '{broken', 0, 7))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertIn('status', result)
        self.assertEqual(self.calls['proposal'], 1)

    def test_manifest_failure_leaves_no_holdout_reservation(self):
        args = self.setup_proposal()
        with patch('adaptive_rsi.evolution.git', side_effect=RuntimeError('repository unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'repository unavailable'):
                self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.store.query("SELECT * FROM candidates WHERE status='reserved'"), [])

    def test_no_fresh_holdout_stops_before_any_paid_call(self):
        args = self.setup_proposal(holdout_uses=1)
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(self.calls, {'proposal': 0, 'trial': 0})
        self.assertEqual(self.store.query('SELECT * FROM calls'), [])
        self.assertIn('fresh', result['reason'].lower())

    def test_identical_failed_experiment_is_not_repeated_without_force(self):
        args = self.setup_proposal()
        first = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(first['status'], 'insufficient_evidence')
        self.assertEqual(self.calls, {'proposal': 1, 'trial': 8})
        candidates = self.store.query('SELECT id FROM candidates')
        second = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(second['status'], 'insufficient_evidence')
        self.assertIn('force', second['reason'].lower())
        self.assertEqual(self.calls, {'proposal': 1, 'trial': 8})
        self.assertEqual(len(self.store.query('SELECT id FROM candidates')), len(candidates))
        forced = self.evolution.propose_and_evaluate(args[0], args[1], self.root / 'run-forced', args[3],
                                                     force=True)
        self.assertEqual(self.calls, {'proposal': 2, 'trial': 16})
        self.assertNotEqual(forced['candidate'], first['candidate'])
        self.assertEqual(len(self.store.query('SELECT id FROM candidates')), len(candidates) + 1)
        first_trials = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?',
                                                 (first['candidate'],))['evidence'])['trials']
        forced_trials = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?',
                                                  (forced['candidate'],))['evidence'])['trials']
        # 每个 trial 覆盖一个独立案例家族的一次重复中的 stable/candidate 对照。
        self.assertEqual(len(first_trials), 4)
        self.assertEqual(len(forced_trials), 4)
        self.assertIsNot(first_trials, forced_trials)
        self.assertEqual(self.store.query('SELECT * FROM calls'), [])

    def test_invalid_proposal_keeps_failure_evidence_and_releases_reservation(self):
        args = self.setup_proposal()
        self.evolution.runtime.invoke = lambda *unused, **kwargs: {'no_kind': True}
        with self.assertRaisesRegex(RuntimeError, 'kind'):
            self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.store.query("SELECT * FROM candidates WHERE status='reserved'"), [])
        failed = self.store.query("SELECT evidence FROM candidates WHERE status='insufficient_evidence'")
        self.assertEqual(len(failed), 1)
        self.assertIn('kind', json.loads(failed[0]['evidence'])['reason'])

    def test_proposal_missing_guidance_fails_fast_without_running_trials(self):
        args = self.setup_proposal()
        for bad_proposal in (
            {'kind': 'strategy', 'triggers': []},
            {'kind': 'strategy', 'guidance': '', 'triggers': []},
            {'kind': 'strategy', 'guidance': '   ', 'triggers': []},
            {'kind': 'strategy', 'guidance': 123, 'triggers': []},
        ):
            self.calls['trial'] = 0
            self.evolution.runtime.invoke = lambda *unused, **kwargs: bad_proposal
            with self.assertRaisesRegex(RuntimeError, 'guidance'):
                self.evolution.propose_and_evaluate(*args, force=True)
            self.assertEqual(self.calls['trial'], 0)
            self.assertEqual(self.store.query("SELECT * FROM candidates WHERE status='reserved'"), [])
            failed = self.store.query("SELECT evidence FROM candidates WHERE status='insufficient_evidence'")
            self.assertTrue(failed)
            self.assertIn('guidance', json.loads(failed[-1]['evidence'])['reason'])

    def test_proposal_missing_triggers_fails_fast_without_running_trials(self):
        args = self.setup_proposal()
        for bad_proposal in (
            {'kind': 'strategy', 'guidance': 'fix'},
            {'kind': 'strategy', 'guidance': 'fix', 'triggers': 'not-a-list'},
            {'kind': 'strategy', 'guidance': 'fix', 'triggers': None},
        ):
            self.calls['trial'] = 0
            self.evolution.runtime.invoke = lambda *unused, **kwargs: bad_proposal
            with self.assertRaisesRegex(RuntimeError, 'triggers'):
                self.evolution.propose_and_evaluate(*args, force=True)
            self.assertEqual(self.calls['trial'], 0)
            self.assertEqual(self.store.query("SELECT * FROM candidates WHERE status='reserved'"), [])
            failed = self.store.query("SELECT evidence FROM candidates WHERE status='insufficient_evidence'")
            self.assertTrue(failed)
            self.assertIn('triggers', json.loads(failed[-1]['evidence'])['reason'])

    def test_skill_values_safe_against_missing_fields(self):
        values = Evolution.skill_values('p', 'c', {'kind': 'strategy', 'guidance': 'fix'}, {}, 0)
        self.assertEqual(json.loads(values[4]), [])
        self.assertEqual(values[5], 'fix')

        values_no_guidance = Evolution.skill_values('p', 'c', {'kind': 'strategy', 'triggers': ['t']}, {}, 0)
        self.assertEqual(json.loads(values_no_guidance[4]), ['t'])
        self.assertEqual(values_no_guidance[5], '')

    def test_auto_promote_with_valid_proposal_promotes_to_stable_and_writes_skills(self):
        args, observed = self.setup_ablation(auto_promote=True)
        baseline = {'id': 'stable-kind', 'kind': 'strategy', 'content': {'guidance': 'old'}}
        self.evolution.runtime.active_strategies = lambda project: [baseline]
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'stable')
        self.assertEqual(self.store.one('SELECT status FROM candidates WHERE id=?',
                                        (result['candidate'],))['status'], 'stable')
        skills = self.store.query('SELECT * FROM skills') or []
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]['content'], 'small improvement')

    def test_stale_reservation_expires_without_blocking_a_new_proposal(self):
        args = self.setup_proposal()
        self.store.execute('INSERT INTO candidates(id,project,kind,status,content,evidence,created) '
                           'VALUES (?,?,?,?,?,?,?)',
                           ('stale', 'p', 'strategy', 'reserved', '{}', '{}', time.time() - 7200))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='stale'")['status'], 'interrupted')
        self.assertEqual(self.calls['proposal'], 1)

    def test_restart_recovery_releases_fresh_reservation_before_expiry(self):
        """崩溃重启后不能等满一小时：recover() 必须中断仍在保留窗口内的预留。"""
        args = self.setup_proposal()
        self.store.execute('INSERT INTO candidates(id,project,kind,status,content,evidence,created) '
                           'VALUES (?,?,?,?,?,?,?)',
                           ('crash-reservation', 'p', 'strategy', 'reserved', '{}', '{}', time.time() - 300))
        self.store.recover()
        recovered = self.store.one("SELECT status FROM candidates WHERE id='crash-reservation'")
        assert recovered is not None
        self.assertEqual(recovered['status'], 'interrupted')
        result = self.evolution.propose_and_evaluate(*args)
        self.assertNotEqual(result.get('code'), 'evolution_reservation_busy')
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(self.calls['proposal'], 1)

    def test_changed_policy_cases_or_strategies_rearm_the_failed_experiment(self):
        args = self.setup_proposal()
        self.evolution.propose_and_evaluate(*args)
        config = json.loads(args[3]['config'])
        self.assertFalse(self.evolution.attempt_due('p', config)[0])
        policy = json.loads(json.dumps(config))
        policy['triage']['policy'] = 'prefer different evidence'
        self.assertTrue(self.evolution.attempt_due('p', policy)[0])
        self.assertFalse(self.evolution.attempt_due('p', config)[0])
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('new-holdout', 'p', 'new-holdout', 'holdout',
                            encode({'task': 'new fresh task', 'base': 'x', 'checks': [], 'source': 'real',
                                    'repository': '/tmp', 'overlay': None}), 0, 50))
        self.assertTrue(self.evolution.attempt_due('p', config)[0])
        self.store.execute("DELETE FROM cases WHERE id='new-holdout'")
        self.assertFalse(self.evolution.attempt_due('p', config)[0])
        runtime = self.evolution.runtime
        original = runtime.active_strategies
        runtime.active_strategies = lambda project: [{'id': 'stable', 'kind': 'strategy',
                                                      'content': {'guidance': 'new guidance'}}]
        try:
            self.assertTrue(self.evolution.attempt_due('p', config)[0])
        finally:
            runtime.active_strategies = original
        self.assertFalse(self.evolution.attempt_due('p', config)[0])

    def test_unknown_cost_quality_pass_keeps_explicit_warning(self):
        trials = []
        for family in range(5):
            split = 'validation' if family % 2 else 'holdout'
            for repetition in range(2):
                trials.append({'case': 'case-' + str(family), 'split': split, 'repetition': repetition,
                               'stable': {'pass': False, 'tokens': 0, 'usage_known': False},
                               'candidate': {'pass': True, 'tokens': 0, 'usage_known': False}})
        result = Evolution.decide(trials, {'evolution': {'repeats': 2, 'min_cases_per_split': 1,
                                                         'max_cost_ratio': 1.25}})
        self.assertEqual(result['decision'], 'pass')
        self.assertFalse(result['cost_comparable'])
        self.assertEqual(result['cost_state'], 'unknown_usage')
        self.assertTrue(result['cost_warning'])

    def test_partial_usage_tokens_are_a_lower_bound_not_a_cost_ratio(self):
        """usage_known=False 但 tokens>0 的变体只覆盖了已知调用：cost_ratio 不得据此
        通过 max_cost_ratio，也不能标记为 cost_state='known'。"""
        def trials_with(stable, candidate):
            return [{'case': 'case-' + str(family),
                     'split': 'validation' if family % 2 else 'holdout',
                     'repetition': repetition,
                     'stable': dict(stable), 'candidate': dict(candidate)}
                    for family in range(5) for repetition in range(2)]
        config = {'evolution': {'repeats': 2, 'min_cases_per_split': 1, 'max_cost_ratio': 1.25}}

        # 记录到 tokens>0 但显式 usage_known=False：已知部分远低于门槛也不能当全量。
        result = Evolution.decide(trials_with(
            {'pass': False, 'tokens': 100, 'usage_known': False},
            {'pass': True, 'tokens': 10, 'usage_known': False}), config)
        self.assertEqual(result['decision'], 'pass')
        self.assertIsNone(result['cost_ratio'])
        self.assertFalse(result['cost_comparable'])
        self.assertNotEqual(result['cost_state'], 'known')
        self.assertIn('lower bound', result['cost_warning'])

        # 缺失 usage_known 键的历史证据同样不得按 tokens>0 兜底成已知成本。
        result = Evolution.decide(trials_with(
            {'pass': False, 'tokens': 100},
            {'pass': True, 'tokens': 10}), config)
        self.assertEqual(result['decision'], 'pass')
        self.assertIsNone(result['cost_ratio'])
        self.assertFalse(result['cost_comparable'])
        self.assertNotEqual(result['cost_state'], 'known')

        # 部分已知也不能借超支拒绝之外的通道通过：candidate 下界已超 stable 全量
        # 时仍只按 unknown_usage 处理，不产生 cost_ratio 硬判定。
        result = Evolution.decide(trials_with(
            {'pass': False, 'tokens': 10, 'usage_known': True},
            {'pass': True, 'tokens': 100, 'usage_known': False}), config)
        self.assertEqual(result['decision'], 'pass')
        self.assertIsNone(result['cost_ratio'])
        self.assertFalse(result['cost_comparable'])
        self.assertNotEqual(result['cost_state'], 'known')

        # 全部 usage_known=True 时行为不变：cost_ratio 正常计算并参与门禁。
        result = Evolution.decide(trials_with(
            {'pass': False, 'tokens': 100, 'usage_known': True},
            {'pass': True, 'tokens': 50, 'usage_known': True}), config)
        self.assertEqual(result['decision'], 'pass')
        self.assertEqual(result['cost_state'], 'known')
        self.assertEqual(result['cost_ratio'], 0.5)
        self.assertTrue(result['cost_comparable'])
        self.assertEqual(result['cost_warning'], '')

        # 已知用量下超支仍按原门槛拒绝。
        result = Evolution.decide(trials_with(
            {'pass': False, 'tokens': 100, 'usage_known': True},
            {'pass': True, 'tokens': 200, 'usage_known': True}), config)
        self.assertEqual(result['decision'], 'insufficient_evidence')
        self.assertEqual(result['cost_ratio'], 2.0)

    def test_repeat_identity_tracks_overlay_contents_and_execution_limits(self):
        args = self.setup_proposal()
        overlay = self.root / 'overlay'
        overlay.mkdir()
        (overlay / 'case.txt').write_text('first')
        row = self.store.one("SELECT spec FROM cases WHERE id='validation'")
        spec = json.loads(row['spec'])
        spec['overlay'] = str(overlay)
        self.store.execute("UPDATE cases SET spec=? WHERE id='validation'", (encode(spec),))
        first = self.evolution.input_signature('p', self.config)
        (overlay / 'case.txt').write_text('second')
        self.assertNotEqual(first, self.evolution.input_signature('p', self.config))
        changed = json.loads(json.dumps(self.config))
        changed['limits']['command_seconds'] += 1
        self.assertNotEqual(self.evolution.input_signature('p', self.config),
                            self.evolution.input_signature('p', changed))

    def test_reserved_holdout_blocks_concurrent_proposal_and_survives_failure(self):
        args = self.setup_proposal()
        def interrupted(*unused):
            other = Evolution(self.evolution.runtime)
            second = other.propose_and_evaluate(*args[:-1], {**args[-1], 'id': 'second'})
            self.assertEqual(second['status'], 'insufficient_evidence')
            raise RuntimeError('proposal transport failed')
        self.evolution.runtime.invoke = interrupted
        with self.assertRaisesRegex(RuntimeError, 'transport failed'):
            self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.store.one("SELECT uses FROM cases WHERE id='holdout'")['uses'], 0)
        records = self.store.query("SELECT * FROM candidates WHERE status='insufficient_evidence'")
        reservation = [json.loads(row['evidence']) for row in records if 'reservation' in json.loads(row['evidence'])]
        self.assertEqual(len(reservation), 1)
        self.assertEqual(reservation[0]['reservation']['holdout'], ['holdout'])
        self.assertIn('transport failed', reservation[0]['reason'])

    def test_inactive_development_is_filtered_before_limit_and_holdout_never_proposed(self):
        args = self.setup_proposal()
        for index in range(11):
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               ('dev' + str(index), 'p', 'dev' + str(index), 'development',
                                encode({'task': 'retired secret', 'active': False}), 0, index))
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('active', 'p', 'active', 'development', encode({'task': 'active example'}), 0, 20))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.calls, {'proposal': 1, 'trial': 8})
        self.assertNotIn('retired secret', self.prompts[0])
        self.assertNotIn('holdout secret task', self.prompts[0])
        self.assertIn('active example', self.prompts[0])
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_eligible_case_filter_matches_row_by_row_reference(self):
        def reference(project, limit):
            # 旧实现的逐行过滤，作为新单查询的等价性参照保留。
            available = self.store.query("SELECT * FROM cases WHERE project=? AND split IN ('validation','holdout') "
                                         "ORDER BY created,id", (project,))
            cases = []
            for case in available:
                spec = case_spec(case)
                if (case['uses'] < limit and spec is not None and
                        spec.get('available', True) and spec.get('active', True) and
                        isinstance(spec.get('task'), str) and
                        isinstance(spec.get('repository'), str) and isinstance(spec.get('base'), str)):
                    cases.append(case['id'])
            return cases

        rows = [
            ('ok', 'p', 'validation', '{"task":"t","base":"b","repository":"r"}', 0, 1),
            ('within-limit', 'p', 'validation', '{"task":"t","base":"b","repository":"r"}', 2, 2),
            ('at-limit', 'p', 'holdout', '{"task":"t","base":"b","repository":"r"}', 3, 3),
            ('retired', 'p', 'validation', '{"task":"t","base":"b","repository":"r","active":false}', 0, 4),
            ('retired-integer', 'p', 'validation', '{"task":"t","base":"b","repository":"r","active":0}', 0, 5),
            ('unavailable', 'p', 'validation', '{"task":"t","base":"b","repository":"r","available":false}', 0, 6),
            ('unavailable-text-zero', 'p', 'validation', '{"task":"t","base":"b","repository":"r","available":"0"}', 0, 7),
            ('unavailable-object', 'p', 'holdout', '{"task":"t","base":"b","repository":"r","available":{"x":1}}', 0, 8),
            ('unavailable-array', 'p', 'holdout', '{"task":"t","base":"b","repository":"r","available":[0]}', 0, 9),
            ('missing-task', 'p', 'validation', '{"base":"b","repository":"r"}', 0, 10),
            ('null-task', 'p', 'validation', '{"task":null,"base":"b","repository":"r"}', 0, 11),
            ('numeric-task', 'p', 'validation', '{"task":7,"base":"b","repository":"r"}', 0, 12),
            ('missing-repository', 'p', 'validation', '{"task":"t","base":"b"}', 0, 13),
            ('numeric-base', 'p', 'validation', '{"task":"t","base":2,"repository":"r"}', 0, 14),
            ('broken-json', 'p', 'validation', '{not json', 0, 15),
            ('json-array', 'p', 'validation', '["task"]', 0, 16),
            ('empty-object', 'p', 'validation', '{}', 0, 17),
            ('duplicate-spec-a', 'p', 'validation', '{"task":"t","base":"b","repository":"r"}', 0, 19),
            ('duplicate-spec-b', 'p', 'validation', '{"task":"t","base":"b","repository":"r"}', 0, 19),
            ('other-project', 'q', 'validation', '{"task":"t","base":"b","repository":"r"}', 0, 20),
            ('other-split', 'p', 'development', '{"task":"t","base":"b","repository":"r"}', 0, 21),
        ]
        for case_id, project, split, spec, uses, created in rows:
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               (case_id, project, case_id, split, spec, uses, created))
        for limit in (0, 1, 3, 4):
            self.assertEqual([case['id'] for case in self.evolution.eligible_cases('p', limit)],
                             reference('p', limit))
        self.assertEqual([case['id'] for case in self.evolution.eligible_cases('q', 4)],
                         ['other-project'])

    def test_exposed_holdout_short_circuits_before_paid_proposal(self):
        args = self.setup_proposal(holdout_uses=1)
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.calls, {'proposal': 0, 'trial': 0})
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['code'], 'evolution_fresh_holdout_required')

    def test_exhausted_validation_split_reports_validation_not_holdout(self):
        # validation 曝光已用满而 holdout 完全新鲜：原因必须指向 validation，
        # 并且不能给出“导入新 holdout”这类在该场景下无效的补救建议。
        args = self.setup_proposal()
        limit = self.config['evolution']['max_holdout_uses']
        self.store.execute("UPDATE cases SET uses=? WHERE id='validation'", (limit,))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.calls, {'proposal': 0, 'trial': 0})
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['code'], 'evolution_validation_cases_exhausted')
        self.assertIn('validation has 0 of 1 required cases', result['reason'])
        self.assertIn('short by 1', result['reason'])
        self.assertIn('import new, previously unused validation cases', result['reason'].lower())
        # 调高上限对本场景有效，因此不能被写成“无用”；holdout 一侧不得被判为不足。
        self.assertIn('raise evolution.max_holdout_uses', result['reason'])
        self.assertNotIn('fresh holdout', result['reason'].lower())
        self.assertEqual(self.store.one("SELECT uses FROM cases WHERE id='holdout'")['uses'], 0)

    def test_both_exhausted_splits_are_reported_together(self):
        args = self.setup_proposal()
        limit = self.config['evolution']['max_holdout_uses']
        self.store.execute("UPDATE cases SET uses=? WHERE id IN ('validation','holdout')", (limit,))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(self.calls, {'proposal': 0, 'trial': 0})
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['code'], 'evolution_both_splits_exhausted')
        self.assertIn('validation has 0 of 1 required cases', result['reason'])
        self.assertIn('holdout has 0 of 1 fresh cases', result['reason'])

    def test_long_term_failure_is_detected_after_initial_window(self):
        self.candidate({'monitor': {'passed': 1, 'failed': 0, 'runs': ['old']}})
        self.store.execute('INSERT INTO runs(id,project,strategy,config) VALUES (?,?,?,?)', ('late', 'p', encode([{'id': 'c'}]), encode({})))
        self.evolution.monitor('p', False, {'id': 'late'}, attributable=True)
        row = self.store.one('SELECT status,evidence FROM candidates WHERE id=?', ('c',))
        self.assertEqual(row['status'], 'rolled_back')
        self.assertEqual(json.loads(row['evidence'])['drift']['phase'], 'sentinel')

    def feed_passing_runs(self, ids):
        for run_id in ids:
            self.store.execute('INSERT INTO runs(id,project,strategy,config) VALUES (?,?,?,?)',
                               (run_id, 'p', encode([{'id': 'c'}]), encode({})))
            self.evolution.monitor('p', True, {'id': run_id})

    def test_sentinel_successes_alone_do_not_report_drift(self):
        # monitor_tasks=1：seeded passed=1 即进入 sentinel，此后成功按 1/10 抽样入账。
        self.candidate({'monitor': {'passed': 1, 'failed': 0, 'runs': []}})
        self.feed_passing_runs('post-%d' % index for index in range(120))
        row = self.store.one('SELECT status,evidence FROM candidates WHERE id=?', ('c',))
        evidence = json.loads(row['evidence'])
        self.assertEqual(row['status'], 'stable')
        self.assertNotIn('drift', evidence)
        monitor = evidence['monitor']
        self.assertEqual(monitor['phase'], 'sentinel')
        self.assertEqual(monitor['success_sample_rate'], 0.1)
        self.assertIn('biased_low', monitor['sampling'])
        # 120 次成功只抽中固定的 8 个（fingerprint 前 8 位 %10==0），与 1 条 seed 合计 9。
        self.assertEqual(monitor['passed'], 9)
        self.assertEqual(monitor['failed'], 0)
        self.assertEqual(monitor['sampled_passed'], 8)

    def test_sentinel_drift_threshold_uses_unbiased_ratio(self):
        # 记录口径 passed:failed = 3:3 看似 50%，但只有 1 条是抽样成功；
        # 校正后 (2 + 10):3 = 0.8 恰好不触发 drift。
        self.candidate({'monitor': {'passed': 2, 'failed': 3, 'runs': []}})
        self.feed_passing_runs(['post-18'])  # fingerprint %10==0，必然入账
        evidence = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])
        self.assertNotIn('drift', evidence)
        self.assertEqual((evidence['monitor']['passed'], evidence['monitor']['failed']), (3, 3))
        self.assertEqual(evidence['monitor']['sampled_passed'], 1)
        # 真实回归仍触发：校正后 (3 + 10):50 = 13/63 ≈ 0.21 < 0.8，且证据显式声明采样口径。
        self.store.execute("UPDATE candidates SET evidence=? WHERE id='c'",
                           (encode({'monitor': {'passed': 3, 'failed': 50, 'runs': []}}),))
        self.feed_passing_runs(['post-46'])
        drift = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])['drift']
        self.assertEqual(drift['phase'], 'sentinel')
        self.assertTrue(drift['sampled'])
        self.assertEqual(drift['success_sample_rate'], 0.1)
        self.assertEqual(drift['sampled_passed'], 1)
        self.assertAlmostEqual(drift['unbiased_pass_ratio'], 13 / 63, places=3)
        self.assertEqual((drift['passed'], drift['failed']), (4, 50))

    def test_sentinel_initial_successes_keep_unit_weight(self):
        # 初始窗口的 20 条成功未经抽样，若被一并按 1/10 放大，
        # 校正比率会虚高（210/220 ≈ 0.95）而漏报真实回归；正确估计 30:40 = 0.75 必须报 drift。
        self.candidate({'monitor': {'passed': 20, 'failed': 10, 'runs': []}})
        self.feed_passing_runs(['post-18'])
        evidence = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])
        drift = evidence['drift']
        self.assertEqual(drift['phase'], 'sentinel')
        self.assertEqual(drift['sampled_passed'], 1)
        self.assertAlmostEqual(drift['unbiased_pass_ratio'], 30 / 40, places=3)

    def test_retirement_and_reclassification_keep_exposure(self):
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)', ('x', 'p', 'family', 'validation', encode({'signature': 's'}), 2, 1))
        self.evolution.update_case('p', 'x', active=False, split='development', source='human:review')
        row = self.store.one('SELECT * FROM cases WHERE id=?', ('x',))
        self.assertEqual(row['uses'], 2)
        self.assertFalse(json.loads(row['spec'])['active'])
        with self.assertRaises(ValueError):
            self.evolution.update_case('p', 'x', active=True, split='holdout', source='human:review')
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='case_lifecycle'")), 1)

    def importable_manifest(self, repo, task='replacement task', family='family', split='validation'):
        from adaptive_rsi.gitops import git
        return {'family': family, 'split': split, 'repository': str(repo),
                'base': git(repo, 'rev-parse', 'HEAD'), 'task': task,
                'checks': [{'name': 'unit', 'cwd': '.', 'argv': ['python3', '-c', 'print(1)']}],
                'source': 'real'}

    def test_retired_case_releases_its_family_for_reimport(self):
        """family 唯一性只约束仍参与调度的用例：退休行释放 family，退休行自身原样保留。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {'task': 'original task', 'base': base, 'checks': CHECKS,
                'source': 'real', 'signature': 'sig-old', 'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('old', 'p', 'family', 'validation', encode(spec), 3, 1))
        self.evolution.update_case('p', 'old', active=False, source='maintenance:aged')
        self.evolution.runtime.project = lambda project: {'id': project}
        manifest = self.importable_manifest(repo)
        new_id = self.evolution.import_case('p', manifest)
        self.assertNotEqual(new_id, 'old')
        new_row = self.store.one('SELECT * FROM cases WHERE id=?', (new_id,))
        self.assertEqual(new_row['family'], 'family')
        # 退休行保持不变：active 仍为假、uses 与 lifecycle 历史完整。
        old_row = self.store.one('SELECT * FROM cases WHERE id=?', ('old',))
        old_spec = json.loads(old_row['spec'])
        self.assertFalse(old_spec['active'])
        self.assertEqual(old_row['uses'], 3)
        self.assertEqual(old_spec['lifecycle'][-1]['source'], 'maintenance:aged')
        # 同 family 的第三条：现在有一个活跃同 family，再次导入必须拒绝。
        with self.assertRaisesRegex(ValueError, 'family'):
            self.evolution.import_case('p', {**manifest, 'task': 'third task'})

    def test_active_same_family_case_still_blocks_import(self):
        """存在活跃同 family 用例时导入仍被拒绝；退休不改变 signature 互斥。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        # spec.signature 必须与 import_case 重新计算的签名一致，否则命中不了 signature 互斥。
        signature = fingerprint({'task': 'original task',
                                 'tree': git(repo, 'rev-parse', base + '^{tree}')})
        spec = {'task': 'original task', 'base': base, 'checks': CHECKS,
                'source': 'real', 'signature': signature, 'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('active', 'p', 'family', 'validation', encode(spec), 0, 1))
        self.evolution.runtime.project = lambda project: {'id': project}
        manifest = self.importable_manifest(repo)
        with self.assertRaisesRegex(ValueError, 'family'):
            self.evolution.import_case('p', manifest)
        # 退休后 signature 相同的同一任务快照仍不得借退休重入。
        same_task = self.importable_manifest(repo, task='original task', family='other')
        self.evolution.update_case('p', 'active', active=False, source='maintenance:aged')
        with self.assertRaisesRegex(ValueError, 'family'):
            self.evolution.import_case('p', same_task)

    def test_transaction_recheck_rejects_interleaved_same_family_import(self):
        """读侧预检与事务内复检之间落库的同 family 活跃行必须由复检拦截：
        两个导入都通过预检时，只有先提交的活跃行占住 family，后到者报错而不是插入第二行。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        signature = fingerprint({'task': 'winner task',
                                 'tree': git(repo, 'rev-parse', base + '^{tree}')})
        winner = {'task': 'winner task', 'base': base, 'checks': CHECKS, 'source': 'real',
                  'signature': signature, 'repository': str(repo), 'overlay': None}
        self.evolution.runtime.project = lambda project: {'id': project}
        manifest = self.importable_manifest(repo, task='challenger task')
        original = self.store.query
        injected = []

        def racing(sql, args=()):
            rows = original(sql, args)
            if not injected and 'SELECT family,spec FROM cases WHERE project=?' in sql:
                injected.append(True)
                self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                                   ('winner', 'p', 'family', 'validation', encode(winner), 0, 1))
            return rows

        with patch.object(self.store, 'query', side_effect=racing):
            with self.assertRaisesRegex(ValueError, 'family'):
                self.evolution.import_case('p', manifest)
        # 失败的导入没有留下第二行；先提交的活跃行仍是 family 的唯一占用者。
        rows = self.store.query("SELECT id,spec FROM cases WHERE project='p' AND family='family'")
        self.assertEqual([row['id'] for row in rows if json.loads(row['spec']).get('active', True)],
                         ['winner'])
        self.assertEqual(len(self.store.query("SELECT id FROM cases WHERE project='p'")), 1)

    def test_legacy_inactive_row_holding_family_is_released_during_import(self):
        """上线前退休或被外部直写 available:false 的遗留行没改过名：import_case 的事务内复检
        必须在同一事务里把它们释放，否则 INSERT 仍撞 UNIQUE(project,family)。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {'task': 'original task', 'base': base, 'checks': CHECKS, 'available': False,
                'source': 'real', 'signature': 'sig-legacy', 'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('legacy', 'p', 'family', 'validation', encode(spec), 2, 1))
        self.evolution.runtime.project = lambda project: {'id': project}
        new_id = self.evolution.import_case('p', self.importable_manifest(repo))
        self.assertEqual(self.store.one('SELECT family FROM cases WHERE id=?', (new_id,))['family'],
                         'family')
        legacy = self.store.one('SELECT family,spec,uses FROM cases WHERE id=?', ('legacy',))
        self.assertEqual(legacy['family'], 'family-retired-legacy')
        self.assertEqual(legacy['uses'], 2)
        self.assertFalse(json.loads(legacy['spec'])['available'])

    def test_reactivation_restores_family_or_rejects_live_replacement(self):
        """退休改名只释放 family 名，不释放家族身份：原 family 空闲时激活恢复原名；
        原名已被仍活跃的替代用例占住时拒绝激活，避免同一家族两条活跃行被当成
        相互独立的评估证据。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {'task': 'original task', 'base': base, 'checks': CHECKS,
                'source': 'real', 'signature': 'sig-old', 'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('old', 'p', 'family', 'validation', encode(spec), 3, 1))
        self.evolution.update_case('p', 'old', active=False, source='maintenance:aged')
        # 空闲原名：激活直接恢复 family，而不是带着 -retired-<id> 名继续参与调度。
        self.evolution.update_case('p', 'old', active=True, source='human:review')
        row = self.store.one('SELECT family,spec FROM cases WHERE id=?', ('old',))
        self.assertEqual(row['family'], 'family')
        self.assertNotIn('origin_family', json.loads(row['spec']))
        self.evolution.update_case('p', 'old', active=False, source='maintenance:aged')
        # 原名被替代用例占住：激活必须被拒绝，退休行保持退休。
        self.evolution.runtime.project = lambda project: {'id': project}
        self.evolution.import_case('p', self.importable_manifest(repo))
        with self.assertRaisesRegex(ValueError, 'occupied|family'):
            self.evolution.update_case('p', 'old', active=True, source='human:review')
        row = self.store.one('SELECT family,spec FROM cases WHERE id=?', ('old',))
        self.assertEqual(row['family'], 'family-retired-old')
        self.assertFalse(json.loads(row['spec'])['active'])

    def test_reactivation_releases_family_from_inactive_holder(self):
        """激活时原 family 被另一条非活跃行占住：占住行当场改名释放，激活行恢复原名。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {'task': 'original task', 'base': base, 'checks': CHECKS,
                'source': 'real', 'signature': 'sig-old', 'repository': str(repo), 'overlay': None}
        stale = dict(spec, available=False, signature='sig-stale')
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('old', 'p', 'family', 'validation', encode(spec), 3, 1))
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('stale', 'p', 'other', 'validation', encode(stale), 0, 1))
        self.evolution.update_case('p', 'old', active=False, source='maintenance:aged')
        # stale 行不可用却占着 old 的原名：激活时应被改名释放。
        self.store.execute('UPDATE cases SET family=? WHERE id=?', ('family', 'stale'))
        self.evolution.update_case('p', 'old', active=True, source='human:review')
        self.assertEqual(self.store.one('SELECT family FROM cases WHERE id=?', ('old',))['family'],
                         'family')
        self.assertEqual(self.store.one('SELECT family FROM cases WHERE id=?', ('stale',))['family'],
                         'family-retired-stale')

    def test_feedback_is_separate_from_sealed_results(self):
        self.candidate({'trials': [{'case': 'x', 'candidate': {'pass': False}}]})
        self.evolution.record_feedback('c', 'false_positive', 'human:alice', 'late', 'deployment unrelated')
        row = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])
        self.assertEqual(row['trials'][0]['candidate']['pass'], False)
        summary = self.evolution.feedback_summary('c')
        self.assertEqual(summary['samples'], 1)
        self.assertIn('bias', summary)

    def test_feedback_overview_reports_sources_and_bias_without_touching_sealed_results(self):
        self.candidate({'trials': [{'case': 'x', 'candidate': {'pass': False}}]})
        for candidate, project in [('d', 'p'), ('e', 'q')]:
            self.store.execute('INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)',
                               (candidate, project, 'strategy', 'stable', encode({'guidance': 'other', 'triggers': []}),
                                encode({}), 2))
        before = self.store.one('SELECT evidence,status FROM candidates WHERE id=?', ('c',))
        self.evolution.record_feedback('c', 'false_positive', 'human:alice', 'late', 'deployment unrelated')
        self.evolution.record_feedback('c', 'rejected', 'human:bob')
        self.evolution.record_feedback('e', 'confirmed_regression', 'human:carol')
        # 非人工来源、未支持标签和损坏数据都不得进入校准汇总。
        for data in ['{"candidate":"c","label":"false_positive","source":"model:auto"}',
                     '{"candidate":"c","label":"approved","source":"human:mallory"}', '{not json']:
            self.store.execute('INSERT INTO events(project,run,kind,message,data,created) VALUES (?,?,?,?,?,?)',
                               ('p', None, 'monitor_feedback', 'fixture', data, 1))
        overview = self.evolution.feedback_overview('p')
        self.assertEqual(overview['samples'], 2)
        self.assertEqual(overview['labels'], {'false_positive': 1, 'confirmed_regression': 0, 'rejected': 1})
        self.assertEqual(overview['sources'], {'human:alice': 1, 'human:bob': 1})
        self.assertEqual(overview['labeled_candidates'], 1)
        self.assertEqual(overview['unlabeled_candidates'], 1)
        self.assertIn('1 of 2 candidates', overview['bias'])
        self.assertTrue(overview['read_only'])
        self.assertFalse(overview['automatic_threshold_changes'])
        self.assertIsNone(self.evolution.feedback_overview()['project'])
        self.assertEqual(self.evolution.feedback_overview()['samples'], 3)
        self.assertEqual(self.evolution.feedback_overview('q')['samples'], 1)
        empty = self.evolution.feedback_overview('unknown')
        self.assertEqual((empty['samples'], empty['labels'], empty['sources']),
                         (0, {'false_positive': 0, 'confirmed_regression': 0, 'rejected': 0}, {}))
        self.assertIn('0 of 0 candidates', empty['bias'])
        summary = self.evolution.feedback_summary('c')
        self.assertEqual(summary['sources'], {'human:alice': 1, 'human:bob': 1})
        after = self.store.one('SELECT evidence,status FROM candidates WHERE id=?', ('c',))
        self.assertEqual(json.loads(after['evidence'])['trials'][0]['candidate']['pass'], False)
        self.assertNotIn('feedback', json.loads(after['evidence']))
        self.assertEqual(after['status'], before['status'])
        self.assertEqual(after['evidence'], before['evidence'])

    def test_manifest_verification_detects_changed_task_and_tree(self):
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        spec = {'task': 'fix add', 'base': git(repo, 'rev-parse', 'HEAD'), 'checks': CHECKS,
                'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)', ('case', 'p', 'f', 'holdout', encode(spec), 0, 1))
        item = {'case': 'case', 'spec': fingerprint(spec), 'task': fingerprint(spec['task']),
                'tree': git(repo, 'rev-parse', spec['base'] + '^{tree}'), 'checks': fingerprint(CHECKS),
                'overlay': None}
        self.assertTrue(self.evolution.verify_inputs('p', [item])['valid'])
        spec['task'] = 'different task'
        self.store.execute('UPDATE cases SET spec=? WHERE id=?', (encode(spec), 'case'))
        self.assertFalse(self.evolution.verify_inputs('p', [item])['valid'])

    def test_overlay_identity_includes_empty_directories_and_modes(self):
        overlay = self.root / 'overlay'
        overlay.mkdir()
        path = overlay / 'run.sh'
        path.write_text('true\n')
        original = Evolution.overlay_digest(overlay)
        path.chmod(0o755)
        self.assertNotEqual(original, Evolution.overlay_digest(overlay))
        executable = Evolution.overlay_digest(overlay)
        (overlay / 'empty').mkdir()
        self.assertNotEqual(executable, Evolution.overlay_digest(overlay))

    def test_missing_overlay_directory_is_unavailable_instead_of_fatal(self):
        """维护清理或工作树删除后，历史用例的 overlay 目录消失不是调度错误。"""
        gone = self.root / 'cleaned-overlay'
        self.assertIsNone(Evolution.overlay_digest(gone))
        self.assertIsNone(Evolution.overlay_digest(str(gone)))
        self.assertIsNone(Evolution.overlay_digest(''))
        overlay = self.root / 'overlay'
        overlay.mkdir()
        (overlay / 'case.txt').write_text('sealed\n')
        digest = str(Evolution.overlay_digest(overlay) or '')
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        shutil.rmtree(overlay)
        # 先算过摘要的目录被清掉后同样降级，不再抛 FileNotFoundError。
        self.assertIsNone(Evolution.overlay_digest(overlay))
        overlay.mkdir()
        (overlay / 'case.txt').write_text('sealed\n')
        self.assertEqual(digest, Evolution.overlay_digest(overlay))
        # overlay 必须是目录；路径指向普通文件时同样不可用。
        self.assertIsNone(Evolution.overlay_digest(overlay / 'case.txt'))

    def test_unreadable_overlay_is_unavailable_while_malformed_content_still_raises(self):
        import os
        overlay = self.root / 'overlay'
        overlay.mkdir()
        (overlay / 'case.txt').write_text('sealed\n')
        with patch('pathlib.Path.lstat', side_effect=PermissionError('overlay denied')):
            self.assertIsNone(Evolution.overlay_digest(overlay))
        # 非常规文件不是「不可用」而是「不可复现」，仍要显式报错，避免被当成有效输入。
        os.mkfifo(overlay / 'pipe', 0o600)
        with self.assertRaisesRegex(ValueError, 'regular files'):
            Evolution.overlay_digest(overlay)

    def test_missing_overlay_does_not_block_periodic_evolution_attempt(self):
        from adaptive_rsi.config import DEFAULTS, merge
        config = merge(DEFAULTS, {})
        runtime = SimpleNamespace(store=self.store, config=lambda project: config,
                                  active_strategies=lambda project: [])
        evolution = Evolution(runtime)
        missing = self.root / 'missing-overlay'
        base_spec = {'task': 'historical task', 'repository': str(self.root),
                     'base': 'HEAD', 'checks': []}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('with-overlay', 'p', 'f1', 'validation',
                            encode({**base_spec, 'overlay': str(missing)}), 0, 1.0))
        signature = str(evolution.input_signature('p', config) or '')
        self.assertRegex(signature, r'^[0-9a-f]{64}$')
        due, computed, attempt = evolution.attempt_due('p', config)
        self.assertIs(due, True)
        self.assertEqual(computed, signature)
        self.assertEqual(attempt, {})
        # 缺失的 overlay 不会让签名每次抖动：记录尝试后同一输入不再重复触发。
        evolution.record_attempt('p', signature, 'insufficient_evidence')
        due_again, again, recorded = evolution.attempt_due('p', config)
        self.assertFalse(due_again)
        self.assertEqual(again, signature)
        self.assertEqual(recorded['status'], 'insufficient_evidence')
        # overlay 缺失不等同于「没有 overlay」：spec 指纹仍区分两种用例。
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('without-overlay', 'p', 'f2', 'validation',
                            encode({**base_spec, 'overlay': None}), 0, 2.0))
        self.assertNotEqual(signature, evolution.input_signature('p', config))

    def test_failure_reasons_with_volatile_details_share_one_suppression_signature(self):
        # 同一根因的两次故障只在临时路径 / run id / provider_request_id 上不同：
        # 归一化后 input_signature 一致，attempt_due 不再为每次新实例反复付费提案。
        from adaptive_rsi.config import DEFAULTS, merge
        config = merge(DEFAULTS, {})
        runtime = SimpleNamespace(store=self.store, config=lambda project: config,
                                  active_strategies=lambda project: [])
        evolution = Evolution(runtime)
        self.store.execute("INSERT INTO projects(id,name,path,config,paused,created) "
                           "VALUES ('p','p','/tmp/p','{}',0,0)")
        task = self.store.add_task('p', 'goal', 'same-fault', 'Same fault', 'same fault')

        def failed_run(run_id, reason, code='execution', action='inspect_details'):
            self.store.execute('INSERT INTO runs(id,task,project,state,config,started,result) '
                               'VALUES (?,?,?,?,?,?,?)',
                               (run_id, task, 'p', 'failed', encode(config), time.time(),
                                encode({'reason': reason,
                                        'failure': {'code': code, 'action': action, 'detail': reason}})))

        failed_run('run-a', 'Controlled ablation did not complete: FileNotFoundError: '
                            '/tmp/bundle-check-Ab3xK9/workspace/case-9f8e7d6c5b4a3f21 missing',
                   code='environment', action='check_environment')
        baseline_inputs = evolution.proposal_inputs('p')
        signature = evolution.input_signature('p', config, baseline_inputs)
        evolution.record_attempt('p', signature, 'insufficient_evidence')

        # 第二次实例换了临时目录、请求 id 与 UUID；其余输入完全未动。
        failed_run('run-b', 'Controlled ablation did not complete: FileNotFoundError: '
                            '/tmp/bundle-check-Zz9yX8/workspace/case-0123456789abcdef missing',
                   code='environment', action='check_environment')
        due, again, attempt = evolution.attempt_due('p', config)
        self.assertFalse(due)
        self.assertEqual(again, evolution.input_signature('p', config))
        self.assertEqual(attempt['status'], 'insufficient_evidence')
        failures = evolution.proposal_inputs('p')['failures']
        self.assertEqual([item['reason'] for item in failures],
                         ['Controlled ablation did not complete: FileNotFoundError: '
                          '<path> missing'])
        self.assertEqual(failures[0]['code'], 'environment')
        self.assertNotIn('bundle-check', failures[0]['reason'])
        self.assertNotIn('9f8e7d6c5b4a3f21', failures[0]['reason'])

        # failure.code/action 真实变化仍要产生新签名：抑制不误伤不同类别故障。
        failed_run('run-c', 'Controlled ablation did not complete: provider timeout',
                   code='provider', action='retry_later')
        self.assertNotEqual(again, evolution.input_signature('p', config))
        due_changed, _, _ = evolution.attempt_due('p', config)
        self.assertTrue(due_changed)

    def test_stale_running_attempt_does_not_suppress_evolution_forever(self):
        # record_attempt(...,\"running\") 之后进程崩溃：1 小时后 in_flight 变假，但
        # attempt.signature==signature 恒成立（running 记录从不更新 signature 也不被清除），
        # 回归修复前 attempt_due/propose_and_evaluate 永远返回「已评估过相同输入」，自动
        # 演化永久停摆，且每次任务完成都写 evolution_not_repeated 噪音。
        from adaptive_rsi.config import DEFAULTS, merge
        config = merge(DEFAULTS, {})
        runtime = SimpleNamespace(store=self.store, config=lambda project: config,
                                  active_strategies=lambda project: [])
        evolution = Evolution(runtime)
        base_spec = {'task': 't', 'repository': str(self.root), 'base': 'HEAD', 'checks': []}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('x', 'p', 'f', 'validation', encode({**base_spec, 'overlay': None}), 0, 1))
        signature = evolution.input_signature('p', config)
        # 正常窗口内的 running：仍按「进行中」抑制，不得重复提案。
        evolution.record_attempt('p', signature, 'running', reason='Proposal started')
        due, _, attempt = evolution.attempt_due('p', config)
        self.assertFalse(due)
        self.assertEqual(attempt['status'], 'running')
        # 把同一条 running 记录直接老化到 3600s 之外（模拟写记录后进程崩溃、
        # 终态从未落盘）——此时必须视为 stale，允许重新评估。
        stale = dict(self.store.setting('evolution-attempt:p', {}))
        stale['at'] = stale['at'] - 3601
        self.store.put_setting('evolution-attempt:p', stale)
        due_after, again, _ = evolution.attempt_due('p', config)
        self.assertTrue(due_after)
        self.assertEqual(again, signature)
        # propose_and_evaluate 走同一 dedup gate：过期 running 不计为「已评估」，不再写
        # evolution_not_repeated，而是进入正常的证据不足/提案流程（本例因用例不足按
        # insufficient_evidence 收尾并覆盖掉 stale 记录）。
        run = {'id': 'run', 'config': encode(config)}
        result = evolution.propose_and_evaluate({'id': 'p'}, self.root / 'w', self.root / 'd', run)
        self.assertFalse(result.get('repeated'))
        self.assertIsNone(self.store.one(
            "SELECT 1 FROM events WHERE kind='evolution_not_repeated'"))
        self.assertNotEqual(self.store.setting('evolution-attempt:p', {}).get('status'), 'running')

    def test_vanished_overlay_is_reported_as_changed_input_not_a_valid_manifest(self):
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        overlay = self.root / 'overlay'
        overlay.mkdir()
        (overlay / 'marker.txt').write_text('sealed\n')
        spec = {'task': 'fix add', 'base': git(repo, 'rev-parse', 'HEAD'), 'checks': CHECKS,
                'repository': str(repo), 'overlay': str(overlay)}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case', 'p', 'f', 'holdout', encode(spec), 0, 1))
        item = {'case': 'case', 'spec': fingerprint(spec), 'task': fingerprint(spec['task']),
                'tree': git(repo, 'rev-parse', spec['base'] + '^{tree}'), 'checks': fingerprint(CHECKS),
                'overlay': Evolution.overlay_digest(overlay)}
        self.assertTrue(self.evolution.verify_inputs('p', [item])['valid'])
        shutil.rmtree(overlay)
        result = self.evolution.verify_inputs('p', [item])
        # 优雅降级只放宽调度，不放宽封存校验：已归档的 overlay 消失仍判为输入变化。
        self.assertFalse(result['valid'])
        self.assertEqual(result['mismatches'], [{'case': 'case', 'fields': ['overlay']}])

    def test_unreadable_overlay_is_rejected_before_sealing_and_verification(self):
        args = self.setup_proposal()
        overlay = self.root / 'unreadable-overlay'
        overlay.mkdir()
        (overlay / 'marker').write_text('protected')
        row = self.store.one("SELECT spec FROM cases WHERE id='validation'")
        spec = {**json.loads(row['spec']), 'overlay': str(overlay)}
        self.store.execute("UPDATE cases SET spec=? WHERE id='validation'", (encode(spec),))
        from adaptive_rsi.gitops import git
        manifest = [{'case': 'validation', 'spec': fingerprint(spec), 'task': fingerprint(spec['task']),
                     'tree': git(spec['repository'], 'rev-parse', spec['base'] + '^{tree}'),
                     'checks': fingerprint(spec['checks']), 'overlay': None}]
        real_open = Path.open
        def denied(path, *args, **kwargs):
            if path == overlay / 'marker':
                raise PermissionError('fixture unreadable overlay')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            self.assertFalse(self.evolution.verify_inputs('p', manifest)['valid'])
            result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(self.calls, {'proposal': 0, 'trial': 0})

    def test_case_with_unavailable_overlay_is_excluded_before_sealing(self):
        """overlay 被清理的用例不得进入实验：缺失目录只会被静默跳过，试验会缺受保护输入。"""
        args = self.setup_proposal()
        from tests.test_runtime import CHECKS
        from adaptive_rsi.gitops import git
        repo = args[1]
        base = git(repo, 'rev-parse', 'HEAD')
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('missing-overlay', 'p', 'overlay-family', 'validation',
                            encode({'task': 'overlay task', 'base': base, 'checks': CHECKS,
                                    'repository': str(repo), 'overlay': str(self.root / 'cleaned-overlay')}), 0, 0))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        events = self.store.query("SELECT data FROM events WHERE kind='evolution_overlay_unavailable'")
        self.assertEqual([json.loads(row['data'])['cases'] for row in events], [['missing-overlay']])
        sealed = self.store.one("SELECT evidence FROM candidates WHERE project='p' AND kind='strategy' "
                                "ORDER BY created DESC LIMIT 1")
        manifest = json.loads(sealed['evidence'])['dataset']['manifest']
        self.assertEqual(sorted(item['case'] for item in manifest), ['holdout', 'validation'])
        self.assertEqual(self.store.one("SELECT uses FROM cases WHERE id='missing-overlay'")['uses'], 0)

    def test_manifest_sealed_while_overlay_was_unavailable_is_not_valid(self):
        """封存时 overlay 已取不到摘要（None），之后仍是 None 也不等于输入未变。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        missing = self.root / 'cleaned-overlay'
        spec = {'task': 'fix add', 'base': git(repo, 'rev-parse', 'HEAD'), 'checks': CHECKS,
                'repository': str(repo), 'overlay': str(missing)}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case', 'p', 'f', 'holdout', encode(spec), 0, 1))
        item = {'case': 'case', 'spec': fingerprint(spec), 'task': fingerprint(spec['task']),
                'tree': git(repo, 'rev-parse', spec['base'] + '^{tree}'), 'checks': fingerprint(CHECKS),
                'overlay': None}
        result = self.evolution.verify_inputs('p', [item])
        self.assertFalse(result['valid'])
        self.assertEqual([row['case'] for row in result['mismatches']], ['case'])
        # 未配置 overlay 的用例不受影响：判为不可用只针对配置了却取不到的目录。
        plain = {**spec, 'overlay': None}
        self.store.execute('UPDATE cases SET spec=? WHERE id=?', (encode(plain), 'case'))
        self.assertTrue(self.evolution.verify_inputs('p', [{**item, 'spec': fingerprint(plain)}])['valid'])

    def two_case_experiment(self):
        """≥2 用例/集合且 min_cases_per_split=1：返回 (args, 抽中 id 列表, 未抽中 id 列表)。"""
        from adaptive_rsi.evolution import select_cases
        from adaptive_rsi.gitops import git
        from tests.test_runtime import CHECKS
        args = self.setup_proposal()
        repo = args[1]
        base = git(repo, 'rev-parse', 'HEAD')
        for case, split in (('validation-extra', 'validation'), ('holdout-extra', 'holdout')):
            spec = {'task': split + ' extra sealed task', 'base': base, 'checks': CHECKS,
                    'source': 'real', 'signature': case, 'repository': str(repo), 'overlay': None}
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               (case, 'p', case, split, encode(spec), 0, 2))
        limit = self.config['evolution']['max_holdout_uses']
        eligible = self.evolution.eligible_cases('p', limit)
        rows = {'validation': [c for c in eligible if c['split'] == 'validation'],
                'holdout': [c for c in eligible if c['split'] == 'holdout' and c['uses'] == 0]}
        sampled = sorted({c['id'] for split, cases in rows.items()
                          for c in select_cases(cases, 1, 'run')})
        unsampled = sorted(c['id'] for cases in rows.values() for c in cases
                           if c['id'] not in sampled)
        self.assertEqual(len(unsampled), 2)
        return args, sampled, unsampled

    def mutate_cases_during_trials(self, case_ids):
        """首个付费试验调用返回后对给定用例例行退休，模拟试验窗口期内的维护。"""
        execute = self.evolution.runtime.execute_candidate
        def wrapped(*call, **kwargs):
            result = execute(*call, **kwargs)
            if self.calls['trial'] == 1:
                for case_id in case_ids:
                    self.evolution.update_case('p', case_id, active=False, source='maintenance:test')
            return result
        self.evolution.runtime.execute_candidate = wrapped

    def test_unsampled_case_retirement_does_not_void_the_paid_experiment(self):
        args, _, unsampled = self.two_case_experiment()
        self.mutate_cases_during_trials(unsampled)
        result = self.evolution.propose_and_evaluate(*args)
        evidence = self.evidence_of(result['candidate'])
        verification = evidence['input_verification']
        self.assertTrue(verification['valid'])
        self.assertEqual(verification['mismatches'], [])
        self.assertEqual(sorted(row['case'] for row in verification['unsampled_mismatches']),
                         unsampled)
        self.assertNotIn('Sealed experiment inputs', evidence['reason'])
        self.assertTrue(any('not selected for execution' in item
                            for item in evidence['promotion_review']['limitations']))
        self.assertEqual(self.calls['trial'], 8)

    def test_sampled_case_change_still_voids_the_experiment(self):
        args, sampled, _ = self.two_case_experiment()
        self.mutate_cases_during_trials([sampled[0]])
        result = self.evolution.propose_and_evaluate(*args)
        evidence = self.evidence_of(result['candidate'])
        verification = evidence['input_verification']
        self.assertFalse(verification['valid'])
        self.assertEqual([row['case'] for row in verification['mismatches']], [sampled[0]])
        self.assertEqual(verification['unsampled_mismatches'], [])
        self.assertEqual(evidence['decision'], 'insufficient_evidence')
        self.assertIn('Sealed experiment inputs changed', evidence['reason'])
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_trial_with_vanished_overlay_cannot_be_recorded_as_pass(self):
        """封存后 overlay 才消失时，试验必须判为不可完成，不能沿用候选自身的 pass。"""
        from tests.test_runtime import fixture, CHECKS
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        self.evolution.runtime.execute_candidate = lambda *a, **k: {'decision': 'pass', 'reason': 'ok'}
        spec = {'task': 'overlay task', 'base': git(repo, 'rev-parse', 'HEAD'), 'checks': CHECKS,
                'repository': str(repo), 'overlay': str(self.root / 'cleaned-overlay')}
        result = self.evolution.run_configuration(
            {'id': 'run', 'task': 'task'}, {'id': 'p'}, {'id': 'case', 'family': 'f', 'split': 'validation'},
            spec, 'combined', 0, '', self.root / 'run', 'candidate', {'evolution': {'max_holdout_uses': 1}})
        self.assertFalse(result['pass'])
        self.assertEqual(result['review'], {})
        self.assertIn('overlay is unavailable', result['reason'])

    def test_plan_tamper_and_incomplete_execution_are_rejected(self):
        data = {'repeats': 2, 'samples': [{'id': 'x', 'split': 'validation'}]}
        sealed = {'data': data, 'sha256': fingerprint(data)}
        self.assertFalse(Evolution.verify_plan(sealed, [])['valid'])
        data['repeats'] = 1
        self.assertEqual(Evolution.verify_plan(sealed, [])['reason'], 'plan_hash_mismatch')

    def test_attribution_does_not_assign_combined_gain_to_strategy(self):
        rows = [{'input': 'x', 'variant': name, 'pass': passed} for name, passed in [('baseline', False), ('strategy_only', False), ('skill_only', False), ('combined', True)]]
        result = Evolution.attribution(rows)
        self.assertEqual(result['interaction_only'], ['x'])
        self.assertEqual(result['strategy_wins'], [])
        self.assertEqual(Evolution.attribution(rows[:-1])['state'], 'insufficient_evidence')

    def test_attribution_refuses_a_variant_without_code_changes(self):
        """空补丁变体与 baseline 同树，不能给任何因素记增益。"""
        rows = [{'input': 'x', 'variant': name, 'pass': passed} for name, passed in
                [('baseline', False), ('strategy_only', False), ('skill_only', True), ('combined', True)]]
        rows[2]['no_change'] = True
        result = Evolution.attribution(rows)
        self.assertEqual(result['state'], 'insufficient_evidence')
        self.assertTrue(result['no_change'])

    def test_empty_patch_trial_is_not_a_paired_win_or_loss(self):
        """试验记录保留 no_change 标记；空转既不算改进也不算回归。"""
        trials = [{'case': 'x', 'split': 'validation',
                   'stable': {'pass': True}, 'candidate': {'pass': True, 'no_change': True}}]
        decision = Evolution.split_decision(trials, self.config)
        self.assertEqual(decision['decision'], 'insufficient_evidence')
        summary = paired_summary(trials)
        self.assertEqual((summary['wins'], summary['losses'], summary['ties'], summary['no_change']), (0, 0, 0, 1))
        self.assertEqual(summary['cohorts']['x']['outcome'], 'no_change')

    def test_run_configuration_records_no_change_without_a_pass(self):
        from adaptive_rsi.gitops import git
        from tests.test_runtime import fixture
        repo = fixture(self.root / 'repo')
        config = {'evolution': {'max_holdout_uses': 1}, 'checks': []}
        self.evolution.runtime.execute_candidate = lambda *args, **kwargs: {
            'decision': 'insufficient_evidence', 'no_change': True,
            'reason': 'Experiment produced no code changes'}
        spec = {'task': 'trial task', 'base': git(repo, 'rev-parse', 'HEAD'), 'checks': [],
                'repository': str(repo), 'overlay': None}
        result = self.evolution.run_configuration(
            {'id': 'run', 'task': 'task', 'config': encode(config)}, {'id': 'p'},
            {'id': 'case'}, spec, 'candidate', 0, [], self.root / 'run', 'candidate', config)
        self.assertFalse(result['pass'])
        self.assertTrue(result['no_change'])
        self.assertEqual(result['reason'], 'Experiment produced no code changes')

    def test_insufficient_stable_arm_is_not_a_paired_win(self):
        """评估器不可用的 stable 臂没有结论：不能算作候选的 10 次「改进」。"""
        config = {'evolution': {'repeats': 2, 'min_cases_per_split': 1, 'max_cost_ratio': 1.25}}
        trials = [{'case': 'case-' + str(family), 'split': 'validation' if family % 2 else 'holdout',
                   'repetition': repetition,
                   'stable': {'pass': False, 'decision': 'insufficient_evidence', 'reason': 'evaluator unavailable'},
                   'candidate': {'pass': True, 'decision': 'pass'}}
                  for family in range(5) for repetition in range(2)]
        decision = Evolution.decide(trials, config)
        self.assertEqual(decision['decision'], 'insufficient_evidence')
        self.assertEqual(decision['inconclusive_families'], 5)
        self.assertIn('no conclusive result', decision['reason'])
        summary = paired_summary(trials)
        self.assertEqual((summary['wins'], summary['losses'], summary['ties'], summary['inconclusive']),
                         (0, 0, 0, 5))
        self.assertEqual(summary['cohorts']['case-0']['outcome'], 'inconclusive')
        self.assertEqual(summary['cohorts']['case-0']['arms'],
                         ['candidate:pass', 'stable:insufficient_evidence'])

    def test_insufficient_candidate_arm_is_not_a_regression(self):
        """瞬时评估器故障的 candidate 臂不能被记成「候选导致回归」。"""
        config = {'evolution': {'repeats': 2, 'min_cases_per_split': 1, 'max_cost_ratio': 1.25}}
        trials = [{'case': 'case-' + str(family), 'split': 'validation', 'repetition': repetition,
                   'stable': {'pass': True, 'decision': 'pass'},
                   'candidate': {'pass': False, 'decision': 'insufficient_evidence', 'reason': 'evaluator unavailable'}}
                  for family in range(3) for repetition in range(2)]
        decision = Evolution.decide(trials, config)
        self.assertEqual(decision['decision'], 'insufficient_evidence')
        self.assertNotIn('regressed', decision['reason'])
        # 真实回归（两臂都有结论）仍判 fail，不会被同一批里的无结论臂掩盖。
        trials[0]['candidate'] = {'pass': False, 'decision': 'fail'}
        self.assertEqual(Evolution.decide(trials, config)['decision'], 'fail')

    def test_confirmed_regression_survives_an_inconclusive_repetition(self):
        """同族另一轮没跑完时，已确认的回归不能被抹掉成 inconclusive。"""
        config = {'evolution': {'repeats': 2, 'min_cases_per_split': 1, 'max_cost_ratio': 1.25}}
        trials = [
            {'case': 'regressed', 'split': 'validation', 'repetition': 0,
             'stable': {'pass': True, 'decision': 'pass'},
             'candidate': {'pass': False, 'decision': 'fail'}},
            {'case': 'regressed', 'split': 'validation', 'repetition': 1,
             'stable': {'pass': True, 'decision': 'insufficient_evidence', 'reason': 'evaluator unavailable'},
             'candidate': {'pass': False, 'decision': 'fail'}},
        ]
        trials += [{'case': 'improved', 'split': 'holdout', 'repetition': repetition,
                    'stable': {'pass': False, 'decision': 'fail'},
                    'candidate': {'pass': True, 'decision': 'pass'}}
                   for repetition in range(2)]
        summary = paired_summary(trials)
        # 一轮无结论不能把同族的真实回归从分母里剔除：否则 losses=0、estimate=1.0。
        self.assertEqual((summary['wins'], summary['losses'], summary['ties'], summary['inconclusive']),
                         (1, 1, 0, 0))
        self.assertEqual(summary['estimate'], 0.5)
        self.assertEqual(summary['cohorts']['regressed']['outcome'], 'loss')
        self.assertEqual(Evolution.decide(trials, config)['decision'], 'fail')

    def test_erroring_stable_arm_blocks_promotion_and_skill_write(self):
        """端到端：stable 臂报错、candidate 臂通过时不得晋升，也不写 verified 技能。"""
        args = self.setup_proposal()

        def execute(*positional, **kwargs):
            # setup_proposal 里稳定臂的 guide 为空，候选臂带新策略。
            return ({'decision': 'insufficient_evidence', 'reason': 'evaluator unavailable'}
                    if not positional[7] else {'decision': 'pass'})

        self.evolution.runtime.execute_candidate = execute
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIn('no conclusive result', result['reason'])
        self.assertEqual(self.store.one('SELECT status FROM candidates WHERE id=?', (result['candidate'],))['status'],
                         'insufficient_evidence')
        self.assertEqual(self.store.query('SELECT * FROM skills') or [], [])
        evidence = self.evidence_of(result['candidate'])
        self.assertTrue(evidence['trials'])
        # 每个臂都带 decision，报告/导出才能说明「该臂为何没结论」。
        self.assertEqual({(t['stable']['decision'], t['candidate']['decision']) for t in evidence['trials']},
                         {('insufficient_evidence', 'pass')})
        self.assertEqual(evidence['win_rate']['inconclusive'], len({t['case'] for t in evidence['trials']}))

    def test_erroring_candidate_arm_is_not_recorded_as_an_attempt_failure(self):
        """端到端：candidate 臂报错时落 insufficient_evidence，不在去重状态里写 fail。"""
        args = self.setup_proposal()

        def execute(*positional, **kwargs):
            return ({'decision': 'pass'} if not positional[7]
                    else {'decision': 'insufficient_evidence', 'reason': 'evaluator unavailable'})

        self.evolution.runtime.execute_candidate = execute
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertNotIn('Candidate regressed', result['reason'])
        self.assertIn('no conclusive result', result['reason'])
        self.assertEqual(self.store.one('SELECT status FROM candidates WHERE id=?', (result['candidate'],))['status'],
                         'insufficient_evidence')
        self.assertEqual(self.store.setting('evolution-attempt:p', {}).get('status'), 'insufficient_evidence')

    def test_ablation_is_scheduled_only_when_the_skill_is_retrievable(self):
        proposal = {'kind': 'strategy', 'guidance': 'do x', 'triggers': ['checklist']}
        self.assertTrue(Evolution.ablation_required(proposal))
        self.assertFalse(Evolution.ablation_required({**proposal, 'triggers': []}))
        self.assertFalse(Evolution.ablation_required({**proposal, 'triggers': ['  ']}))
        self.assertFalse(Evolution.ablation_required({**proposal, 'guidance': '   '}))
        self.assertFalse(Evolution.ablation_required({**proposal, 'kind': 'context'}))
        self.assertFalse(Evolution.ablation_required('strategy'))
        self.assertTrue(Evolution.skill_relevant(['checklist'], 'Run the RELEASE CHECKLIST'))
        self.assertFalse(Evolution.skill_relevant(['checklist'], 'an unrelated task'))
        self.assertFalse(Evolution.skill_relevant(['  '], 'an unrelated task'))
        self.assertFalse(Evolution.skill_relevant(['checklist'], ''))
        # 与 runtime.memories 一致：触发词两侧空白不作裁剪，这类技能在真实检索里也命中不了。
        self.assertFalse(Evolution.skill_relevant([' checklist '], 'run the checklist'))

    def test_ablation_limit_validation_rejects_invalid_values(self):
        import copy
        from adaptive_rsi.config import DEFAULTS, validate
        config = copy.deepcopy(DEFAULTS)
        for accepted in (0, 1, 1000):
            config['evolution']['ablation_cases'] = accepted
            self.assertEqual(validate(config), config)
        # 0 表示关闭消融；负数、非整数和超过实验规模上限的值都必须拒绝。
        for invalid in (-1, 1001, True, '1', None):
            config['evolution']['ablation_cases'] = invalid
            with self.assertRaisesRegex(ValueError, 'ablation'):
                validate(config)

    def test_ablation_skill_is_injected_only_into_declared_experiment_contexts(self):
        from adaptive_rsi.pipeline import experiment_skill_context
        promoted = {'content': 'old skill', 'evidence': {'candidate': 'stable-candidate'}, 'created': 1,
                    'triggers': ['old']}
        foreign = {'content': 'other candidate skill', 'evidence': {'candidate': 'other'}, 'created': 2,
                   'triggers': ['other']}
        injected = {'content': 'small improvement', 'evidence': {'candidate': 'cand', 'ablation': True},
                    'created': 3, 'triggers': ['secret']}
        strategies = [{'id': 'stable-candidate', 'kind': 'strategy', 'content': {'guidance': 'old'}}]
        source = [promoted, foreign, injected]
        ids = {'stable-candidate'}
        result = experiment_skill_context(source, strategies, ids, injected={'cand'})
        # 实验路径丢弃不属于基线策略的技能；消融技能由生产检索带入，本函数不再追加。
        self.assertEqual([item['content'] for item in result], ['old skill', 'small improvement'])
        # 只有本变体显式注入的候选 id 能越出基线集合：写入端遗留的 ablation 字段不构成旁路。
        self.assertEqual([item['content'] for item in experiment_skill_context(source, strategies, ids)],
                         ['old skill'])
        self.assertEqual([item['content'] for item in experiment_skill_context(source[:2], strategies, ids)],
                         ['old skill'])
        # 没有基线集合时保留旧行为：按当次 guide 的策略 id 过滤，结果会随变体变化。
        new_guide = [{'kind': 'strategy', 'content': {'guidance': 'new', 'triggers': ['secret']}}]
        self.assertEqual([item['content'] for item in experiment_skill_context(source[:2], new_guide)], [])
        self.assertEqual([item['content'] for item in experiment_skill_context(source[:2], strategies)],
                         ['old skill'])
        # 注入返回新列表，调用方传入的技能集合保持原样。
        self.assertIsNot(result, source)
        self.assertEqual(source, [promoted, foreign, injected])

    def setup_ablation(self, triggers=('secret task',), ablation_cases=1, auto_promote=False,
                       validation_cases=5, holdout_cases=5):
        """把「策略+技能」候选接入受控消融；同一个桩模型按配置决定是否通过。"""
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git
        from tests.test_runtime import CHECKS
        args = self.setup_proposal()
        self.config = merge(DEFAULTS, {'evolution': {'min_cases_per_split': 5, 'repeats': 2,
                                                     'ablation_cases': ablation_cases,
                                                     'auto_promote': auto_promote}})
        args[3]['config'] = encode(self.config)
        proposal = {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': list(triggers)}
        self.evolution.runtime.invoke = lambda *unused, **kwargs: dict(proposal)
        repo = args[1]
        base = git(repo, 'rev-parse', 'HEAD')
        for split, count in (('validation', validation_cases), ('holdout', holdout_cases)):
            for index in range(1, count):
                case = split + '-' + str(index)
                spec = {'task': split + ' secret task ' + str(index), 'base': base, 'checks': CHECKS,
                        'source': 'real', 'signature': case, 'repository': str(repo), 'overlay': None}
                self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                                   (case, 'p', case, split, encode(spec), 0, 1))
        observed = []
        def execute(run, project, task, repository, base, directory, workspace, guide,
                    experiment=False, experiment_skills=None, baseline_ids=None):
            skills = experiment_skills or []
            strategy = any(isinstance(item.get('content'), dict) and
                           item['content'].get('guidance') == 'small improvement' for item in guide)
            from pathlib import Path
            stage = 'ablation' if 'ablation' in str(directory) else 'paired'
            variant = Path(directory).name
            observed.append({'strategy': strategy, 'skill': bool(skills), 'experiment': experiment,
                             'baseline_ids': baseline_ids, 'stage': stage, 'variant': variant})
            # 只有策略能提升；技能单独不生效，两者同时生效时仍通过。
            return {'decision': 'pass' if strategy else 'fail'}
        self.evolution.runtime.execute_candidate = execute
        # 消融的候选技能可见性走真实生产检索（runtime.memories → context.related）。
        from adaptive_rsi.runtime import Runtime
        runtime = self.evolution.runtime
        runtime.memories = Runtime.memories.__get__(runtime)
        return args, observed

    def evidence_of(self, candidate):
        return json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', (candidate,))['evidence'])

    def runtime_with(self, strategies):
        """轻量运行时：只保留真实 memories 检索，其余模型与执行路径由用例替换。"""
        from adaptive_rsi.runtime import Runtime
        runtime = SimpleNamespace(store=self.store, active_strategies=lambda project: list(strategies),
                                  is_cancelled=lambda run: False)
        runtime.memories = Runtime.memories.__get__(runtime)
        return runtime

    def test_joint_strategy_and_skill_change_runs_the_scheduled_ablation(self):
        args, observed = self.setup_ablation()
        baseline = {'id': 'stable-kind', 'kind': 'strategy', 'content': {'guidance': 'old'}}
        self.evolution.runtime.active_strategies = lambda project: [baseline]
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'pass')
        evidence = self.evidence_of(result['candidate'])
        self.assertEqual(self.store.one('SELECT status FROM candidates WHERE id=?',
                                        (result['candidate'],))['status'], 'ready')
        self.assertEqual(evidence['preregistered_plan']['data']['ablation']['configurations'],
                         ['baseline', 'strategy_only', 'skill_only', 'combined'])
        self.assertEqual(evidence['ablation']['state'], 'complete')
        self.assertEqual(evidence['ablation']['sampled'], 1)
        rows = evidence['ablation']['rows']
        self.assertEqual(sorted(row['variant'] for row in rows),
                         ['baseline', 'combined', 'skill_only', 'strategy_only'])
        self.assertEqual(len(evidence['ablation']['trials']), 1)
        # 报告分别列出各配置，归因由四行结果决定而不是直接记在策略上。
        attribution = evidence['ablation']['attribution']
        self.assertEqual(attribution['state'], 'complete')
        self.assertEqual(attribution['skill_wins'], [])
        self.assertEqual(attribution['strategy_wins'], [rows[0]['input']])
        self.assertEqual(evidence['promotion_review']['attribution'], attribution)
        self.assertIn('no automatic attribution', evidence['ablation']['limitation'])
        # 消融只补跑 strategy_only 与 skill_only，初筛候选按 combined 评估；都走实验路径，没有写技能行。
        ablation_runs = [item for item in observed if item['stage'] == 'ablation']
        self.assertEqual(len(ablation_runs), 2)
        self.assertEqual([item['variant'] for item in ablation_runs], ['strategy_only', 'skill_only'])
        self.assertEqual([item['strategy'] for item in ablation_runs], [True, False])
        self.assertEqual([item['skill'] for item in ablation_runs], [False, True])
        self.assertTrue(all(item['experiment'] for item in observed))
        candidate_runs = [item for item in observed if item['stage'] == 'paired' and item['variant'] == 'candidate']
        self.assertTrue(all(item['strategy'] and item['skill'] for item in candidate_runs))
        # 四配置的既有技能集合由同一批基线策略固定，不随各变体 guide 漂移。
        self.assertEqual({frozenset(item['baseline_ids']) for item in observed}, {frozenset({'stable-kind'})})
        self.assertEqual(self.store.query('SELECT * FROM skills'), [])

    def test_ablation_stays_insufficient_when_no_sampled_input_matches_the_skill(self):
        args, observed = self.setup_ablation(triggers=('never spoken trigger',))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'pass')
        evidence = self.evidence_of(result['candidate'])
        self.assertEqual(evidence['ablation']['state'], 'insufficient_evidence')
        self.assertEqual(evidence['ablation']['sampled'], 0)
        self.assertEqual(evidence['ablation']['attribution']['state'], 'insufficient_evidence')
        self.assertIn('Four matched configurations', evidence['ablation']['reason'])
        self.assertEqual(evidence['promotion_review']['attribution']['state'], 'insufficient_evidence')
        # 没有输入命中触发词时不补跑任何消融配置，也不写技能行。
        self.assertEqual([item for item in observed if item['stage'] == 'ablation'], [])
        self.assertEqual(self.store.query('SELECT * FROM skills'), [])

    def test_joint_gain_proposal_evaluates_combined_and_attributes_to_interaction_only(self):
        """真正组合增益候选（单策略、单技能均失败，同时生效才通过）在初筛评估 combined 并归因为 interaction_only。"""
        args, observed = self.setup_ablation()
        baseline = {'id': 'stable-kind', 'kind': 'strategy', 'content': {'guidance': 'old'}}
        self.evolution.runtime.active_strategies = lambda project: [baseline]

        def execute(run, project, task, repository, base, directory, workspace, guide,
                    experiment=False, experiment_skills=None, baseline_ids=None):
            skills = experiment_skills or []
            strategy = any(isinstance(item.get('content'), dict) and
                           item['content'].get('guidance') == 'small improvement' for item in guide)
            has_skill = bool(skills)
            from pathlib import Path
            stage = 'ablation' if 'ablation' in str(directory) else 'paired'
            variant = Path(directory).name
            observed.append({'strategy': strategy, 'skill': has_skill, 'experiment': experiment,
                             'baseline_ids': baseline_ids, 'stage': stage, 'variant': variant})
            # 真正组合增益：仅当策略与技能同时存在（combined）时才通过；单独策略或单独技能均失败。
            return {'decision': 'pass' if (strategy and has_skill) else 'fail'}
        self.evolution.runtime.execute_candidate = execute

        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'pass')
        evidence = self.evidence_of(result['candidate'])
        self.assertEqual(evidence['ablation']['state'], 'complete')
        self.assertEqual(evidence['ablation']['sampled'], 1)
        rows = evidence['ablation']['rows']
        self.assertEqual(sorted(row['variant'] for row in rows),
                         ['baseline', 'combined', 'skill_only', 'strategy_only'])

        attribution = evidence['ablation']['attribution']
        self.assertEqual(attribution['state'], 'complete')
        self.assertEqual(attribution['interaction_only'], [rows[0]['input']])
        self.assertEqual(attribution['strategy_wins'], [])
        self.assertEqual(attribution['skill_wins'], [])
        self.assertEqual(evidence['promotion_review']['attribution'], attribution)

        # 检查消融执行的两个剥离因子配置与初筛候选配置
        ablation_runs = [item for item in observed if item['stage'] == 'ablation']
        self.assertEqual([item['variant'] for item in ablation_runs], ['strategy_only', 'skill_only'])
        self.assertEqual([item['strategy'] for item in ablation_runs], [True, False])
        self.assertEqual([item['skill'] for item in ablation_runs], [False, True])
        candidate_runs = [item for item in observed if item['stage'] == 'paired' and item['variant'] == 'candidate']
        self.assertTrue(all(item['strategy'] and item['skill'] for item in candidate_runs))

    def test_ablation_attribution_distinguishes_strategy_wins_and_interaction_only(self):
        """消融归因在多个采样输入中分别识别 strategy_wins 与 interaction_only，解除 100% 锁定。"""
        spec_1 = {'task': 'secret task 1', 'base': 'HEAD', 'checks': [], 'source': 'real',
                  'repository': str(self.root), 'overlay': None}
        spec_2 = {'task': 'secret task 2', 'base': 'HEAD', 'checks': [], 'source': 'real',
                  'repository': str(self.root), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-strat', 'p', 'case-strat', 'validation', encode(spec_1), 0, 1))
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-inter', 'p', 'case-inter', 'validation', encode(spec_2), 0, 1))
        trials = [
            {'case': 'case-strat', 'split': 'validation', 'repetition': 0,
             'stable': {'pass': False}, 'candidate': {'pass': True}},
            {'case': 'case-inter', 'split': 'validation', 'repetition': 0,
             'stable': {'pass': False}, 'candidate': {'pass': True}},
        ]
        self.evolution.runtime = self.runtime_with([])
        def run_cfg(*call, **kwargs):
            case_id = call[2]['id']
            variant = call[4]
            # case-strat: strategy_only 单独成功
            # case-inter: strategy_only 与 skill_only 单独均失败（仅在初筛 combined 成功）
            passed = (case_id == 'case-strat' and variant == 'strategy_only')
            return {'pass': passed, 'reason': 'test'}
        self.evolution.run_configuration = run_cfg
        config = {'evolution': {'ablation_cases': 2, 'max_holdout_uses': 3}}
        proposal = {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': ['secret']}
        record = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'}, config, [], proposal, 'cand', trials)
        self.assertEqual(record['state'], 'complete')
        self.assertEqual(record['sampled'], 2)
        attribution = record['attribution']
        self.assertEqual(attribution['state'], 'complete')
        self.assertEqual(attribution['strategy_wins'], ['case-strat'])
        self.assertEqual(attribution['interaction_only'], ['case-inter'])
        self.assertEqual(attribution['skill_wins'], [])

    def test_four_configurations_differ_only_in_the_tested_factor(self):
        """baseline/strategy_only 技能相同；skill_only 恰多一个候选技能；combined 只换引导。"""
        from adaptive_rsi.pipeline import experiment_skill_context
        current = [{'id': 'stable-kind', 'kind': 'strategy', 'content': {'guidance': 'old'}}]
        guide = [{'kind': 'strategy', 'content': {'guidance': 'small improvement', 'triggers': ['secret']}}]
        self.store.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)',
                           ('s', 'p', None, 'verified', encode(['secret']), 'old skill',
                            encode({'candidate': 'stable-kind'}), time.time() + 60, 1))
        skill = {'content': 'small improvement', 'evidence': {'candidate': 'cand', 'ablation': True},
                 'triggers': ['secret'], 'created': 2}
        runtime = self.runtime_with(current)
        ids = {'stable-kind'}
        def context(selected, injected):
            retrieved = runtime.memories('p', 'secret task', extra=[skill] if injected else None)
            return experiment_skill_context(retrieved, selected, baseline_ids=ids,
                                            injected={'cand'} if injected else set())
        contexts = {'baseline': context(current, False), 'strategy_only': context(guide, False),
                    'skill_only': context(current, True), 'combined': context(guide, True)}
        self.assertEqual([item['content'] for item in contexts['baseline']], ['old skill'])
        self.assertEqual(contexts['strategy_only'], contexts['baseline'])
        # 技能集合按内容比较：顺序由 related() 的相关度与 created 决定，不在本用例断言范围内。
        self.assertEqual({item['content'] for item in contexts['skill_only']}, {'old skill', 'small improvement'})
        self.assertEqual({item['content'] for item in contexts['combined']},
                         {item['content'] for item in contexts['skill_only']})
        self.assertEqual(len(contexts['skill_only']), len(contexts['baseline']) + 1)
        # combined 与 skill_only 的技能相同、引导不同：唯一差异就是策略因素。
        self.assertEqual(contexts['combined'][0]['evidence'], contexts['skill_only'][0]['evidence'])
        self.assertNotEqual(guide, current)
        # 旧行为（按当次 guide 推导技能集合）会让 strategy_only 丢掉基线技能，
        # 于是 strategy_wins 可能只是在测量「删掉旧技能」。
        self.assertEqual(experiment_skill_context(runtime.memories('p', 'secret task'), guide), [])

    def test_candidate_skill_is_ranked_and_bounded_by_production_retrieval(self):
        """候选技能与已验证技能同走 related() 的相关度排序、limit=3 与 5000 字符预算。"""
        for index, created in ((1, 1.0), (2, 2.0), (3, 3.0)):
            self.store.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)',
                               ('s' + str(index), 'p', None, 'verified', encode(['secret']),
                                'old skill ' + str(index), encode({'candidate': 'stable-kind'}),
                                time.time() + 60, created))
        runtime = self.runtime_with([{'id': 'stable-kind', 'kind': 'strategy', 'content': {}}])
        candidate = {'content': 'small improvement', 'evidence': {'candidate': 'cand', 'ablation': True},
                     'triggers': ['secret'], 'created': time.time()}
        retrieved = runtime.memories('p', 'secret task', extra=[candidate])
        contents = [item['content'] for item in retrieved]
        self.assertEqual(len(retrieved), 3)
        self.assertEqual(contents, ['small improvement', 'old skill 3', 'old skill 2'])
        # 与文本无关的候选技能不会被选中。
        unrelated = dict(candidate, content='unrelated guidance', triggers=['absent'])
        self.assertNotIn('unrelated guidance',
                         [item['content'] for item in runtime.memories('p', 'secret task', extra=[unrelated])])
        # 字符预算 5000 同样生效：候选技能计入预算，超出预算的旧技能被 related() 丢弃。
        for name, content, created in (('b1', 'y' * 2000, 1.0), ('b2', 'x' * 2000, 2.0)):
            self.store.execute('INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)',
                               (name, 'p', None, 'verified', encode(['budget']), content,
                                encode({'candidate': 'stable-kind'}), time.time() + 60, created))
        budget_candidate = dict(candidate, content='z' * 2000, triggers=['budget'], created=time.time())
        budget = runtime.memories('p', 'budget review', extra=[budget_candidate])
        self.assertEqual([item['content'] for item in budget], ['z' * 2000, 'x' * 2000])
        self.assertLessEqual(sum(len(item['content']) for item in budget), 5000)

    def test_ablation_stays_insufficient_when_production_retrieval_hides_the_skill(self):
        """字面 trigger 命中但生产 related() 丢词（单字符）时，不补跑任何配置。"""
        self.evolution.runtime = self.runtime_with([])
        calls = []
        self.evolution.run_configuration = lambda *call, **kwargs: calls.append(call[4]) or {'pass': True}
        spec = {'task': 'x', 'base': 'HEAD', 'checks': [], 'source': 'real',
                'repository': str(self.root), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('x', 'p', 'x', 'validation', encode(spec), 0, 1))
        trials = [{'case': 'x', 'split': 'validation', 'repetition': 0,
                   'stable': {'pass': False}, 'candidate': {'pass': True}}]
        proposal = {'kind': 'strategy', 'guidance': 'x', 'triggers': ['x']}
        self.assertTrue(Evolution.skill_relevant(proposal['triggers'], 'x'))
        record = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'},
                                       {'evolution': {'ablation_cases': 1, 'max_holdout_uses': 3}}, [],
                                       proposal, 'cand', trials)
        # 不为了让 attribution() 凑齐四配置而绕过检索或放宽 limit。
        self.assertEqual(calls, [])
        self.assertEqual(record['sampled'], 0)
        self.assertEqual(record['state'], 'insufficient_evidence')
        self.assertEqual(record['attribution']['state'], 'insufficient_evidence')
        self.assertIn('not visible under production retrieval', record['reason'])

    def test_ablation_marks_partial_rows_insufficient_when_a_later_input_raises(self):
        """两个合格输入、第二个抛异常：已完成的行只是部分证据，不得报 complete。"""
        for case_id, task in (('x', 'secret task one'), ('y', 'secret task two')):
            spec = {'task': task, 'base': 'HEAD', 'checks': [], 'source': 'real',
                    'repository': str(self.root), 'overlay': None}
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               (case_id, 'p', case_id, 'validation', encode(spec), 0, 1))
        self.evolution.runtime = self.runtime_with([])
        def run(*call, **kwargs):
            if call[2]['id'] == 'y':
                raise RuntimeError('worker crashed')
            return {'pass': True, 'reason': 'ok'}
        self.evolution.run_configuration = run
        trials = [{'case': case_id, 'split': 'validation', 'repetition': 0,
                   'stable': {'pass': False}, 'candidate': {'pass': True}} for case_id in ('x', 'y')]
        proposal = {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': ['secret']}
        record = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'},
                                       {'evolution': {'ablation_cases': 2, 'max_holdout_uses': 3}}, [],
                                       proposal, 'cand', trials)
        # 已完成的输入本身能凑齐四配置（旧实现会据此报 complete）。
        self.assertEqual(Evolution.attribution(record['rows'])['state'], 'complete')
        self.assertEqual({row['input'] for row in record['rows']}, {'x'})
        self.assertEqual(len(record['rows']), 4)
        self.assertEqual(record['state'], 'insufficient_evidence')
        self.assertEqual(record['attribution']['state'], 'insufficient_evidence')
        self.assertIn('worker crashed', record['attribution']['reason'])
        self.assertIn('worker crashed', record['reason'])
        self.assertTrue(record['partial'])

    def test_disabled_ablation_keeps_the_insufficient_evidence_marker(self):
        args, _ = self.setup_ablation(ablation_cases=0)
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'pass')
        evidence = self.evidence_of(result['candidate'])
        self.assertEqual(evidence['ablation']['state'], 'insufficient_evidence')
        self.assertIn('disabled', evidence['ablation']['reason'])
        self.assertEqual(evidence['promotion_review']['attribution']['state'], 'insufficient_evidence')

    def test_strategy_only_proposal_is_not_scheduled_for_ablation(self):
        args, _ = self.setup_ablation(triggers=())
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'pass')
        evidence = self.evidence_of(result['candidate'])
        self.assertNotIn('ablation', evidence)
        self.assertNotIn('ablation', evidence['preregistered_plan']['data'])
        self.assertEqual(evidence['promotion_review']['attribution']['state'], 'insufficient_evidence')
        self.assertIn('not executed', evidence['promotion_review']['attribution']['reason'])

    def test_ablation_samples_one_repetition_per_input(self):
        spec = {'task': 'secret task', 'base': 'HEAD', 'checks': [], 'source': 'real',
                'repository': str(self.root), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('x', 'p', 'x', 'validation', encode(spec), 0, 1))
        trials = [{'case': 'x', 'split': 'validation', 'repetition': repetition,
                   'stable': {'pass': False}, 'candidate': {'pass': True}} for repetition in (0, 1)]
        calls = []
        self.evolution.runtime = self.runtime_with([])
        self.evolution.run_configuration = lambda *call, **kwargs: calls.append(call[4]) or {'pass': True}
        config = {'evolution': {'ablation_cases': 2, 'max_holdout_uses': 3}}
        proposal = {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': ['secret']}
        record = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'}, config, [], proposal, 'cand', trials)
        # 同一输入的另一次重复不再补跑，否则四配置里会出现重复变体行。
        self.assertEqual(calls, ['strategy_only', 'skill_only'])
        self.assertEqual(record['sampled'], 1)
        self.assertEqual(record['state'], 'complete')
        self.assertEqual(sorted(row['variant'] for row in record['rows']),
                         ['baseline', 'combined', 'skill_only', 'strategy_only'])

    def test_ablation_records_incomplete_runs_as_insufficient_evidence(self):
        spec = {'task': 'secret task', 'base': 'HEAD', 'checks': [], 'source': 'real',
                'repository': str(self.root), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('x', 'p', 'x', 'validation', encode(spec), 0, 1))
        trials = [{'case': 'x', 'split': 'validation', 'repetition': 0,
                   'stable': {'pass': False}, 'candidate': {'pass': True}}]
        self.evolution.runtime = self.runtime_with([])
        def broken(*call, **kwargs):
            raise RuntimeError('budget exhausted')
        self.evolution.run_configuration = broken
        config = {'evolution': {'ablation_cases': 1, 'max_holdout_uses': 3}}
        proposal = {'kind': 'strategy', 'guidance': 'small improvement', 'triggers': ['secret']}
        record = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'}, config, [], proposal, 'cand', trials)
        self.assertEqual(record['state'], 'insufficient_evidence')
        self.assertIn('budget exhausted', record['reason'])
        self.assertEqual(record['rows'], [])
        no_gain = self.evolution.ablate({'id': 'p'}, self.root, {'id': 'r'}, config, [], proposal, 'cand',
                                        [{'case': 'x', 'split': 'validation', 'repetition': 0,
                                          'stable': {'pass': False}, 'candidate': {'pass': False}}])
        self.assertIn('No joint validation gain', no_gain['reason'])

    def test_bundle_verification_detects_changed_inputs(self):
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})}})
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])
        (destination / 'evidence.json').write_text('{}')
        self.assertFalse(Evolution.verify_bundle(destination)['valid'])

    def test_bundle_evidence_drops_host_trial_report_paths(self):
        """试验的评估报告绝对路径带出用户名与状态目录布局，导出时不得进包。"""
        def report(variant):
            return str(self.root / 'experiments' / 'experiments' / 'case-1-0' / variant / 'evaluation' / 'report.json')
        trial = {'case': 'case-1', 'split': 'validation', 'repetition': 0,
                 'order': ['stable', 'candidate'],
                 'stable': {'pass': False, 'report': report('stable')},
                 'candidate': {'pass': True, 'report': report('candidate')}}
        ablation = {'state': 'complete', 'trials': [
            {'case': 'case-1', 'repetition': 0,
             'strategy_only': {'pass': True, 'report': report('strategy_only')},
             'skill_only': {'pass': False, 'report': report('skill_only')}}]}
        self.candidate({'trials': [trial], 'ablation': ablation})
        destination = self.root / 'bundle-trial-report'
        self.evolution.export_bundle('c', destination)

        text = (destination / 'evidence.json').read_text(encoding='utf-8')
        self.assertNotIn(str(self.root), text)
        exported = json.loads(text)
        # 判定字段保留，只有宿主路径被删除。
        self.assertEqual(exported['trials'][0]['case'], 'case-1')
        self.assertEqual(exported['trials'][0]['split'], 'validation')
        self.assertEqual(exported['trials'][0]['repetition'], 0)
        self.assertEqual(exported['trials'][0]['order'], ['stable', 'candidate'])
        self.assertEqual([exported['trials'][0]['stable']['pass'],
                          exported['trials'][0]['candidate']['pass']], [False, True])
        self.assertNotIn('report', exported['trials'][0]['stable'])
        self.assertNotIn('report', exported['trials'][0]['candidate'])
        self.assertNotIn('report', exported['ablation']['trials'][0]['strategy_only'])
        self.assertNotIn('report', exported['ablation']['trials'][0]['skill_only'])
        self.assertFalse(exported['ablation']['trials'][0]['skill_only']['pass'])
        # 导出只裁剪副本，本地候选证据仍保留完整路径。
        stored = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])
        self.assertEqual(stored['trials'][0]['stable']['report'], trial['stable']['report'])
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])

    def test_bundle_hashing_streams_instead_of_reading_whole_files(self):
        """封存与校验摘要必须分块读取，峰值内存不随单个文件大小增长。

        整文件 ``read_bytes()`` 会为每个文件临时分配等长内存；300 MiB 用例在 2 GB
        机器上足以把校验/导出进程顶到 OOM，并连带中断同进程里的运行时。这里用
        稀疏文件模拟大文件：磁盘占用近零，但读出的字节数与真实文件一致。
        """
        import tracemalloc
        bundle = self.root / 'stream-bundle'
        bundle.mkdir()
        big = bundle / 'big.bin'
        with big.open('wb') as out:
            out.write(b'x' * 1024)
            out.truncate(64 * 1024 * 1024)
        expected = hashlib.sha256()
        with big.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                expected.update(block)
        (bundle / 'bundle.json').write_text(json.dumps(
            {'version': 1, 'files': {'big.bin': expected.hexdigest()}, 'limitations': []}),
            encoding='utf-8')

        tracemalloc.start()
        try:
            result = Evolution.verify_bundle(bundle)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertTrue(result['valid'], result['mismatches'])
        self.assertLess(peak, 8 * 1024 * 1024, 'peak %d bytes' % peak)

    def test_bundle_export_and_verify_never_read_whole_files(self):
        """导出与校验都不能调用整文件读取；禁止 read_bytes 后仍应导出并校验成功。"""
        import adaptive_rsi.build  # noqa: F401  # 预热模块级 BUILD_ID，避免其自身的小文件读取被误伤
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole-file read')):
            destination = self.sealed_bundle(repo_name='repo-stream')
            self.assertTrue(Evolution.verify_bundle(destination)['valid'])

    def test_bundle_verification_rejects_unregistered_files(self):
        """封存后新增的未登记文件必须判失效，并阻止复现执行任何检查。

        逐项比对登记摘要只能证明"登记过的文件没变"；sitecustomize.py 这类文件
        不改动任何摘要，却会在复现时被 Python 自动导入并改变检查结果。
        """
        import subprocess
        import sys
        marker = self.root / 'unlisted-check-ran'
        checks = [{'name': 'marker', 'cwd': '.',
                   'argv': [sys.executable, '-c',
                            'open(%r, "w").write("1")' % str(marker)]}]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-unlisted')
        case = destination / 'cases' / 'case-unlisted'
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])

        injected = [case / 'repository' / 'sitecustomize.py',
                    case / 'UNLISTED.txt',
                    destination / 'UNLISTED.txt']
        for path in injected:
            path.write_text('print("unlisted")\n')

        result = Evolution.verify_bundle(destination)
        self.assertFalse(result['valid'])
        for path in injected:
            self.assertIn(path.relative_to(destination).as_posix(), result['mismatches'])

        # 完整性失效后不得执行任何用例检查：marker 只在检查真正运行时写出。
        repro = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertFalse(repro['integrity']['valid'])
        self.assertFalse(repro['valid'])
        self.assertFalse(repro['reproducible'])
        self.assertEqual(repro['executed_checks'], [])
        self.assertFalse(marker.exists())

        # CLI 以完整性失败退出 1，不再打印「All bundle files verified」。
        cli = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(destination),
             '--run-checks', '--strict'],
            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(cli.returncode, 1, cli.stdout + cli.stderr)
        self.assertNotIn('All bundle files verified', cli.stdout)
        self.assertIn('cases/case-unlisted/repository/sitecustomize.py', cli.stdout)
        self.assertFalse(marker.exists())

        # 未注入的正常导出包不回归：verify 通过，--strict 退出 0 且检查真实执行。
        pristine = self.sealed_bundle(checks=checks, repo_name='repo-unlisted-pristine')
        self.assertTrue(Evolution.verify_bundle(pristine)['valid'])
        passed = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(pristine),
             '--run-checks', '--strict'],
            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn('All bundle files verified', passed.stdout)
        self.assertTrue(marker.exists())

    def test_exported_bundle_excludes_git_metadata(self):
        """导出 bundle 只封存工作树：.git 不随包走（origin 指向的宿主机 suite
        路径不外泄），cases/ 下出现 .git 由 verify_bundle 判失效而不是静默豁免。"""
        destination = self.sealed_bundle(repo_name='repo-gitless')
        repository = destination / 'cases' / 'case-gitless' / 'repository'
        self.assertTrue(repository.is_dir())
        self.assertFalse((repository / '.git').exists())
        self.assertFalse(list((destination / 'cases').rglob('.git')))
        # 包内任何残留文件都不应包含源仓库的宿主机绝对路径。
        for path in destination.rglob('*'):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(str(self.root), path.read_bytes().decode('utf-8', errors='replace'))
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])
        # 篡改注入：向用例仓库塞回 .git/config（含本地路径）必须被判失效。
        injected = repository / '.git'
        injected.mkdir()
        (injected / 'config').write_text('[remote "origin"]\n\turl = ' + str(self.root) + '/repo-gitless\n')
        result = Evolution.verify_bundle(destination)
        self.assertFalse(result['valid'])
        self.assertIn('cases/case-gitless/repository/.git', result['mismatches'])
        # reproduce_bundle 的完整性判定同口径拒绝，run_checks 不再走到复制 .git。
        repro = Evolution.reproduce_bundle(destination)
        self.assertFalse(repro['integrity']['valid'])
        self.assertFalse(repro['valid'])

    def test_bundle_drops_git_metadata_from_overlay(self):
        """overlay 由宿主机目录整棵复制，其中的 .git 同样不能进包：留下会把宿主机
        路径带给接收方，并让 verify_bundle 把刚导出的包判为失效。"""
        overlay = self.root / 'overlay-with-git'
        (overlay / '.git').mkdir(parents=True)
        (overlay / '.git' / 'config').write_text('[remote "origin"]\n\turl = ' + str(self.root) + '/suite\n')
        (overlay / 'protected.txt').write_text('sealed input\n')
        destination = self.sealed_bundle(repo_name='repo-overlay-git', overlay=overlay)
        case = destination / 'cases' / 'case-overlay-git'
        self.assertEqual((case / 'overlay' / 'protected.txt').read_text(), 'sealed input\n')
        self.assertFalse(list((destination / 'cases').rglob('.git')))
        for path in destination.rglob('*'):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(str(self.root), path.read_bytes().decode('utf-8', errors='replace'))
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])

    def test_export_bundle_seals_only_sampled_case_trees(self):
        """未抽中用例只登记身份：eligible 全集不再各复制一份完整工作树。"""
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo-sampled')
        (repo / 'payload.bin').write_bytes(b'payload' * 8192)
        git(repo, 'add', '.')
        git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--quiet', '-m', 'payload')
        base = git(repo, 'rev-parse', 'HEAD')
        tree = git(repo, 'rev-parse', base + '^{tree}')
        manifest = []
        for name, sampled in (('a', True), ('b', True), ('c', False), ('d', False)):
            checks = [{'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']}]
            if not sampled:
                checks.append({'name': 'unexecuted_check', 'cwd': '.', 'argv': ['fake-unsampled-tool']})
            spec = {'task': 'task ' + name, 'base': base, 'checks': checks, 'source': 'real',
                    'signature': 'sig-' + name, 'repository': str(repo), 'overlay': None}
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               ('case-' + name, 'p', 'family-' + name, 'validation', encode(spec), 0, time.time()))
            manifest.append({'case': 'case-' + name, 'split': 'validation', 'tree': tree, 'sampled': sampled})
        self.candidate({'dataset': {'manifest': manifest}, 'providers': []})
        destination = self.root / 'bundle-sampled'
        self.evolution.export_bundle('c', destination)

        # 1. 只有抽中用例封存工作树；未抽中条目连 case.json 都不落盘。
        self.assertEqual(sorted(path.parent.name for path in (destination / 'cases').glob('*/repository')),
                         ['case-a', 'case-b'])
        self.assertEqual(sorted(path.name for path in (destination / 'cases').iterdir()), ['case-a', 'case-b'])

        # 2. bundle.json 保留全部条目的身份与导出标记，审计信息不丢。
        exported = json.loads((destination / 'bundle.json').read_text(encoding='utf-8'))
        entries = {item['case']: item for item in exported['cases']}
        self.assertEqual(sorted(entries), ['case-a', 'case-b', 'case-c', 'case-d'])
        self.assertEqual([entries['case-' + name]['exported'] for name in 'abcd'], [True, True, False, False])
        self.assertEqual(entries['case-c']['tree'], tree)
        self.assertEqual(entries['case-c']['split'], 'validation')
        self.assertTrue(any('unsampled' in item for item in exported['limitations']))

        # 3. 包体不随未抽中条目放大：两份工作树而不是四份。
        source_bytes = sum(path.stat().st_size for path in repo.rglob('*')
                           if path.is_file() and '.git' not in path.relative_to(repo).parts)
        bundle_bytes = sum(path.stat().st_size for path in destination.rglob('*') if path.is_file())
        self.assertLess(bundle_bytes, source_bytes * 2 + 16384)

        # 4. 抽中用例仍可校验与离线复现；未执行用例的检查命令不进入依赖清单。
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'},
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertNotIn('fake-unsampled-tool', [item['name'] for item in result['dependencies']['missing']])
        self.assertTrue(result['reproducible'])

    def sealed_bundle(self, checks=None, providers=None, evidence=None, repo_name='repo', overlay=None):
        """导出一个含单个用例的封存实验包，供 bundle 复现测试复用。"""
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        suffix = repo_name[5:] if repo_name.startswith('repo-') else repo_name
        repo = fixture(self.root / repo_name)
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {
            'task': 'offline reproduction task',
            'base': base,
            'checks': checks if checks is not None else [
                {'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']},
            ],
            'source': 'real',
            'signature': 'sig-' + suffix,
            'repository': str(repo),
            'overlay': str(overlay) if overlay else None,
        }
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-' + suffix, 'p', 'family-' + suffix, 'validation', encode(spec), 0, time.time()))
        evidence = {'dataset': {'manifest': [{'case': 'case-' + suffix}]},
                    'providers': providers if providers is not None else [],
                    **(evidence or {})}
        if self.store.one("SELECT id FROM candidates WHERE id='c'"):
            self.store.execute("UPDATE candidates SET evidence=? WHERE id='c'", (encode(evidence),))
        else:
            self.candidate(evidence)
        destination = self.root / ('bundle-' + suffix)
        self.evolution.export_bundle('c', destination)
        return destination

    def test_bundle_offline_reproduction_enumerates_dependencies_without_aborting(self):
        checks = [
            {'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']},
            {'name': 'missing_tool_1', 'cwd': '.', 'argv': ['fake-compiler-xyz']},
            {'name': 'missing_tool_2', 'cwd': '.', 'argv': ['nonexistent-linter-abc', '--check']},
        ]
        destination = self.sealed_bundle(checks=checks, providers=[{'id': 'openai', 'env_key': 'OPENAI_API_KEY'}],
                                        repo_name='repo-deps')
        result = Evolution.reproduce_bundle(
            destination,
            env={'PATH': '/usr/bin:/bin'},  # No model API credentials
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None
        )

        # 1. Input integrity is verified
        self.assertTrue(result['valid'])
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(result['integrity']['mismatches'], [])

        # 2. Reproduction cannot proceed due to missing tools and credentials
        self.assertFalse(result['reproducible'])

        # 3. Missing tools are enumerated item-by-item instead of failing on the first one
        missing_tool_names = [item['name'] for item in result['dependencies']['missing']]
        self.assertIn('fake-compiler-xyz', missing_tool_names)
        self.assertIn('nonexistent-linter-abc', missing_tool_names)
        available_tool_names = [item['name'] for item in result['dependencies']['available']]
        self.assertIn('git', available_tool_names)
        self.assertIn('python3', available_tool_names)

        # 4. Missing credentials are enumerated
        missing_cred_names = [item['name'] for item in result['credentials']['missing']]
        self.assertIn('OPENAI_API_KEY', missing_cred_names)

        # 5. Limitations explicitly declare credentials, tools, and offline environment
        limitations_text = ' '.join(result['limitations'])
        self.assertIn('Credentials are excluded', limitations_text)
        self.assertIn('Missing credentials', limitations_text)
        self.assertIn('Missing host tools', limitations_text)
        self.assertIn('Isolated offline environment', limitations_text)

    def test_bundle_checks_run_without_host_git_when_checks_do_not_use_it(self):
        """bundle 不含 .git；检查只用 python3 时，宿主缺 git 不得把结论变成不可复现。"""
        import sys
        destination = self.sealed_bundle(repo_name='repo-no-git')
        bindir = self.root / 'python-only-bin'
        bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': str(bindir)}, run_checks=True)

        self.assertTrue(result['integrity']['valid'])
        self.assertNotIn('git', [item['name'] for item in result['dependencies']['missing']])
        self.assertEqual([row['check'] for row in result['executed_checks']], ['unit_check'])
        self.assertTrue(result['executed_checks'][0]['passed'])
        self.assertTrue(result['reproducible'])
        # 缺 git 只作为说明记录，不构成阻断项。
        self.assertTrue(any('git executable not found' in item for item in result['limitations']))
        self.assertFalse(any('Missing host tools' in item for item in result['limitations']))

    def test_bundle_check_that_invokes_git_reports_missing_tool(self):
        """检查命令 argv[0] 为 git 且宿主无 git：报缺失并拒绝执行，不得静默跳过。"""
        import sys
        checks = [{'name': 'vcs_check', 'cwd': '.', 'argv': ['git', '--version']}]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-needs-git')
        bindir = self.root / 'python-only-bin-vcs'
        bindir.mkdir()
        (bindir / 'python3').symlink_to(sys.executable)

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': str(bindir)}, run_checks=True)

        self.assertTrue(result['integrity']['valid'])
        missing = {item['name']: item for item in result['dependencies']['missing']}
        self.assertIn('git', missing)
        self.assertEqual(missing['git']['cases'], ['case-needs-git'])
        self.assertIn('vcs_check', missing['git']['checks'])
        self.assertEqual(result['executed_checks'], [])
        self.assertFalse(result['reproducible'])

    def test_bundle_relative_check_commands_resolve_inside_case_bundle(self):
        # 用例自带的相对命令（./run.sh）是包内依赖：必须按检查 cwd 在用例仓库
        # 中定位并真实执行，不能被当成「宿主缺少工具」而跳过全部检查。
        destination = self.sealed_bundle(repo_name='repo-relative-argv')
        case = destination / 'cases' / 'case-relative-argv'
        script = case / 'repository' / 'run.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o755)
        spec = json.loads((case / 'case.json').read_text(encoding='utf-8'))
        spec['checks'] = [{'name': 'repo-script', 'cwd': '.', 'argv': ['./run.sh']}]
        (case / 'case.json').write_text(encode(spec), encoding='utf-8')
        self.reseal_bundle(destination)

        probed = []

        def which(cmd, **kwargs):
            probed.append(cmd)
            return '/usr/bin/' + cmd if cmd in ('git', 'python3') else None

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True, which=which)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(result['dependencies']['missing'], [])
        # 包内路径不进入宿主 which() 查询。
        self.assertNotIn('./run.sh', probed)
        available = {item['name']: item for item in result['dependencies']['available']}
        self.assertEqual(available['./run.sh']['type'], 'bundle')
        self.assertEqual([(row['case'], row['check'], row['passed'])
                          for row in result['executed_checks']],
                         [('case-relative-argv', 'repo-script', True)])
        self.assertEqual(result['checks_executed'], 1)
        self.assertTrue(result['reproducible'])

        # 真正缺失的宿主工具仍按宿主依赖记录；包内缺失的脚本单独归类，
        # 两者都继续阻止复现，且不会再执行任何检查。
        spec['checks'] = [
            {'name': 'repo-script', 'cwd': '.', 'argv': ['./run.sh']},
            {'name': 'missing-host-tool', 'cwd': '.', 'argv': ['definitely-missing-binary']},
            {'name': 'missing-bundle-script', 'cwd': '.', 'argv': ['tools/missing.sh']},
        ]
        (case / 'case.json').write_text(encode(spec), encoding='utf-8')
        self.reseal_bundle(destination)
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True, which=which)
        missing = {item['name']: item for item in result['dependencies']['missing']}
        self.assertEqual(missing['definitely-missing-binary']['type'], 'executable')
        self.assertEqual(missing['tools/missing.sh']['type'], 'bundle')
        self.assertNotIn('./run.sh', missing)
        self.assertEqual(result['executed_checks'], [])
        self.assertFalse(result['reproducible'])
        limitations = ' '.join(result['limitations'])
        self.assertIn('Missing host tools: 1', limitations)
        self.assertIn('Missing bundle executables: 1', limitations)

    def test_bundle_offline_reproduction_detects_tampered_bundle(self):
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})},
                        'providers': []})
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)
        self.assertTrue(Evolution.reproduce_bundle(destination)['integrity']['valid'])

        # Tampering with bundle file invalidates integrity
        (destination / 'candidate.json').write_text('{"tampered": true}')
        tampered_result = Evolution.reproduce_bundle(destination)
        self.assertFalse(tampered_result['valid'])
        self.assertFalse(tampered_result['integrity']['valid'])
        self.assertIn('candidate.json', tampered_result['integrity']['mismatches'])
        self.assertFalse(tampered_result['reproducible'])

    def test_bundle_offline_reproduction_cli_script(self):
        import subprocess
        import sys
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})},
                        'providers': [{'id': 'openai', 'env_key': 'OPENAI_API_KEY'}]})
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)

        # Test CLI invocation with --json in clean environment (no API keys)
        proc = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(destination), '--json'],
            capture_output=True,
            text=True,
            env={'PATH': '/usr/bin:/bin'},
        )
        self.assertEqual(proc.returncode, 0)
        cli_data = json.loads(proc.stdout)
        self.assertTrue(cli_data['valid'])
        self.assertFalse(cli_data['reproducible'])
        self.assertIn('OPENAI_API_KEY', [c['name'] for c in cli_data['credentials']['missing']])

        # Test CLI invocation with --strict exits non-zero (2) when dependencies/credentials missing
        strict_proc = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(destination), '--strict'],
            capture_output=True,
            text=True,
            env={'PATH': '/usr/bin:/bin'},
        )
        self.assertEqual(strict_proc.returncode, 2)
        self.assertIn('Sealed Bundle Reproduction Inspection', strict_proc.stdout)
        self.assertIn('OPENAI_API_KEY', strict_proc.stdout)

    def test_bundle_without_any_case_is_not_reproducible(self):
        # 没有 dataset.manifest 的候选（旧证据/手写 ready 行）导出的包只有三个
        # json 文件：integrity 有效，但没有可执行基线，不得判「具备离线复现条件」。
        import subprocess
        import sys
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})},
                        'providers': []})
        destination = self.root / 'bundle-empty'
        manifest = self.evolution.export_bundle('c', destination)
        self.assertEqual(sorted(manifest['files']), ['candidate.json', 'evidence.json'])

        result = Evolution.reproduce_bundle(destination)
        self.assertTrue(result['valid'])
        self.assertEqual(result['cases'], {'exported': [], 'registered': [], 'missing': []})
        self.assertFalse(result['reproducible'])
        self.assertTrue(any('no registered case.json' in item for item in result['limitations']))

        strict = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(destination), '--strict'],
            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'},
        )
        self.assertEqual(strict.returncode, 2)
        self.assertIn('不含任何已登记用例', strict.stdout)

    def test_bundle_missing_registered_case_is_not_reproducible(self):
        # manifest 登记两个用例、库里只有一个：导出只写出一个，缺的用例必须让
        # 复现结论为否，并可在 cases.missing 与限制说明中读出具体 id。
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo-partial')
        spec = {
            'task': 'partial bundle task', 'base': git(repo, 'rev-parse', 'HEAD'),
            'checks': [{'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']}],
            'source': 'real', 'signature': 'sig-partial',
            'repository': str(repo), 'overlay': None,
        }
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-present', 'p', 'family-present', 'validation', encode(spec), 0, time.time()))
        self.candidate({'dataset': {'manifest': [{'case': 'case-present'}, {'case': 'case-missing'}]},
                        'providers': []})
        destination = self.root / 'bundle-partial'
        manifest = self.evolution.export_bundle('c', destination)
        self.assertIn('cases/case-present/case.json', manifest['files'])
        self.assertNotIn('cases/case-missing/case.json', manifest['files'])

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'},
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(result['cases']['exported'], ['case-present'])
        self.assertEqual(result['cases']['missing'], ['case-missing'])
        self.assertFalse(result['reproducible'])
        self.assertIn('case-missing', ' '.join(result['limitations']))

    def test_bundle_reproduction_ignores_unmanifested_case_directories(self):
        # 线程 1：向 bundle 多塞一个未登记用例目录不得被执行，也不得保持 integrity 有效。
        pristine = self.sealed_bundle(repo_name='repo-pristine')
        mutated = self.root / 'mutated'
        shutil.copytree(pristine, mutated)

        (mutated / 'cases' / 'x').mkdir()
        (mutated / 'cases' / 'x' / 'case.json').write_text(encode({
            'task': 'smuggled', 'base': 'b', 'source': 'real', 'signature': 'sig-x',
            'repository': 'repository', 'overlay': None,
            'checks': [{'name': 'smuggled_check', 'cwd': '.', 'argv': ['python3', '-c',
                        'open("SMUGGLED_EXECUTED", "w").write("1")']}],
        }))
        result = Evolution.reproduce_bundle(
            mutated, env={'PATH': '/usr/bin:/bin'}, run_checks=True)
        self.assertFalse(result['integrity']['valid'])
        self.assertIn('cases/x/case.json', result['integrity']['mismatches'])
        self.assertFalse(result['valid'])
        self.assertFalse(result['reproducible'])
        self.assertEqual(result['executed_checks'], [])
        self.assertFalse((mutated / 'SMUGGLED_EXECUTED').exists())

        # bundle 内已登记文件未被篡改：与导出原包逐文件比对 SHA-256。
        original_files = json.loads((pristine / 'bundle.json').read_text(encoding='utf-8'))['files']
        for relative, digest in original_files.items():
            self.assertEqual(hashlib.sha256((mutated / relative).read_bytes()).hexdigest(), digest)

        # 符号链接用例目录同样按 verify_bundle 规则拒绝，登记者仍可正常执行。
        symlinked = self.root / 'symlinked'
        shutil.copytree(pristine, symlinked)
        outside = self.root / 'outside'
        (outside / 'x').mkdir(parents=True)
        (outside / 'x' / 'case.json').write_text(encode({
            'task': 'escape', 'base': 'b', 'source': 'real', 'signature': 'sig-y',
            'repository': 'repository', 'overlay': None,
            'checks': [{'name': 'escape_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("escape")']}],
        }))
        shutil.rmtree(symlinked / 'cases')
        (symlinked / 'cases').symlink_to(outside)
        self.assertFalse(Evolution.verify_bundle(symlinked)['valid'])
        result = Evolution.reproduce_bundle(
            symlinked, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertFalse(result['integrity']['valid'])
        self.assertFalse(result['valid'])
        self.assertEqual(result['executed_checks'], [])

        # verify_bundle 也会把 cases/ 下未登记的符号链接条目判失效：它既是清单外的
        # 内容，也可能指向包外；reproduce 必须同时不执行其中任何内容。
        scanned = self.root / 'scanned'
        shutil.copytree(pristine, scanned)
        (scanned / 'cases' / 'link').symlink_to(outside / 'x')
        self.assertFalse(Evolution.verify_bundle(scanned)['valid'])
        self.assertIn('cases/link', Evolution.verify_bundle(scanned)['mismatches'])
        result = Evolution.reproduce_bundle(scanned, env={'PATH': '/usr/bin:/bin'}, run_checks=True)
        self.assertFalse(result['integrity']['valid'])
        self.assertIn('cases/link', result['integrity']['mismatches'])
        self.assertFalse(result['valid'])
        self.assertEqual(result['executed_checks'], [])

        baseline = Evolution.reproduce_bundle(
            pristine, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(baseline['integrity']['valid'])
        self.assertEqual([row['case'] for row in baseline['executed_checks']], ['case-pristine'])
        self.assertTrue(baseline['executed_checks'][0]['passed'])

    def sealed_overlay_bundle(self):
        """导出含单个 overlay 用例的封存包，供 overlay 复现测试复用。"""
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        overlay = self.root / 'overlay'
        overlay.mkdir()
        (overlay / 'sealed_marker.txt').write_text('sealed\n')
        (overlay / 'tests').mkdir()
        (overlay / 'tests' / 'test_overlay.py').write_text(
            "import pathlib, unittest\n"
            "class Overlay(unittest.TestCase):\n"
            "    def test_marker(self):\n"
            "        marker = pathlib.Path('sealed_marker.txt').read_text()\n"
            "        self.assertIn('sealed', marker)\n"
            "        self.assertFalse(pathlib.Path('OVLAER_RAW').exists())\n")
        spec = {
            'task': 'overlay reproduction task',
            'base': git(repo, 'rev-parse', 'HEAD'),
            'checks': [
                {'name': 'sees_staging_copy', 'cwd': '.', 'argv': ['python3', '-c',
                 'import pathlib, sys; sys.exit(0 if (pathlib.Path("sealed_marker.txt").exists()'
                 ' and not pathlib.Path("OVLAER_RAW").exists()) else 1)']},
                {'name': 'overlay_only_test', 'cwd': '.', 'argv': ['python3', '-m', 'unittest',
                 'discover', '-s', 'tests', '-p', 'test_overlay.py', '-v']},
            ],
            'source': 'real', 'signature': 'sig1', 'repository': str(repo),
            'overlay': str(overlay),
        }
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-01', 'p', 'family-01', 'validation', encode(spec), 0, time.time()))
        self.candidate({'dataset': {'manifest': [{'case': 'case-01'}]}, 'providers': []})
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)
        return destination

    def reseal_bundle(self, destination):
        """按 export_bundle 的规则重算 manifest，但跳过符号链接。

        export_bundle 本身拒绝符号链接；这里显式构造被篡改的包，用来验证复现侧
        在 integrity 只覆盖登记文件时仍自行拒绝 staging 内的符号链接。
        """
        manifest = json.loads((destination / 'bundle.json').read_text(encoding='utf-8'))
        files = {}
        for path in sorted(destination.rglob('*')):
            relative = path.relative_to(destination)
            if relative.as_posix() == 'bundle.json' or '.git' in relative.parts:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest['files'] = files
        (destination / 'bundle.json').write_text(encode(manifest), encoding='utf-8')

    def test_bundle_reproduction_applies_sealed_overlay_in_staging_copy(self):
        # 线程 2：overlay-only 检查在一次性副本中可见并执行，bundle 内文件保持不变。
        destination = self.sealed_overlay_bundle()
        files_before = json.loads((destination / 'bundle.json').read_text(encoding='utf-8'))['files']

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(len(result['executed_checks']), 2)
        for row in result['executed_checks']:
            self.assertTrue(row['passed'], row)
            self.assertTrue(row['overlay_applied'])
        self.assertEqual([row['check'] for row in result['executed_checks']],
                         ['sees_staging_copy', 'overlay_only_test'])
        # 覆盖层落盘脚本不在导出目录中：命令确实运行在一次性副本里。
        self.assertFalse((destination / 'cases' / 'case-01' / 'repository' / 'sealed_marker.txt').exists())
        for relative, digest in files_before.items():
            self.assertEqual(hashlib.sha256((destination / relative).read_bytes()).hexdigest(), digest,
                             relative)
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])

    def test_bundle_reproduction_tolerates_symlinked_temp_dir_ancestor(self):
        # macOS 默认 TMPDIR=/var/folders/... 而 /var 是 private/var 的符号链接；
        # 这类祖先符号链接属于运行环境，不是 bundle 内容，不得据此判 overlay 逃逸。
        destination = self.sealed_overlay_bundle()
        real = self.root / 'real-temp'
        real.mkdir()
        link = self.root / 'link-temp'
        link.symlink_to(real)
        original = tempfile.tempdir
        tempfile.tempdir = str(link)
        try:
            result = Evolution.reproduce_bundle(
                destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
                which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        finally:
            tempfile.tempdir = original

        self.assertTrue(result['integrity']['valid'])
        self.assertEqual([row['check'] for row in result['executed_checks']],
                         ['sees_staging_copy', 'overlay_only_test'])
        for row in result['executed_checks']:
            self.assertTrue(row['passed'], row)
            self.assertTrue(row['overlay_applied'])
        # 一次性副本确实建在符号链接临时目录内，并在返回前清理干净。
        self.assertEqual(list(real.iterdir()), [])
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])

    def test_bundle_reproduction_rejects_overlay_escape_through_symlink(self):
        # 放行 staging 之上的环境符号链接后，staging 之内的符号链接仍必须拒绝：
        # overlay 不能借仓库里未登记的符号链接把文件写到一次性副本之外。
        destination = self.sealed_bundle(repo_name='repo-escape')
        case_dir = destination / 'cases' / 'case-escape'
        outside = self.root / 'outside'
        outside.mkdir()
        (case_dir / 'repository' / 'vendor').symlink_to(outside)
        overlay = self.root / 'overlay-escape'
        (overlay / 'vendor').mkdir(parents=True)
        (overlay / 'vendor' / 'escaped.txt').write_text('escaped\n')
        shutil.copytree(overlay, case_dir / 'overlay')
        spec = json.loads((case_dir / 'case.json').read_text(encoding='utf-8'))
        spec['overlay'] = 'overlay'
        (case_dir / 'case.json').write_text(encode(spec), encoding='utf-8')
        self.reseal_bundle(destination)
        # 未登记的符号链接条目在 verify_bundle 阶段就被拒绝，不再等到 staging。
        rejected = Evolution.verify_bundle(destination)
        self.assertFalse(rejected['valid'])
        self.assertIn('cases/case-escape/repository/vendor', rejected['mismatches'])

        # 即使完整性校验被绕过，复现侧仍要拒绝 overlay 经仓库内符号链接写到副本外。
        with patch.object(Evolution, 'verify_bundle',
                          return_value={'valid': True, 'mismatches': [], 'limitations': []}):
            result = Evolution.reproduce_bundle(
                destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
                which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(len(result['executed_checks']), 1)
        row = result['executed_checks'][0]
        self.assertEqual(row['case'], 'case-escape')
        self.assertFalse(row['passed'])
        self.assertIn('Symlink overlaps reproduction overlay', row['error'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_bundle_reproduction_fails_check_with_missing_cwd(self):
        # 声明的 cwd 不在用例树里时不得退回 staging 根目录执行：那样检查会在
        # 错误目录里通过，整包被判可复现，接收方也无从核对实际工作目录。
        overlay = self.root / 'overlay-cwd'
        (overlay / 'sub').mkdir(parents=True)
        (overlay / 'sub' / 'marker.txt').write_text('present\n')
        checks = [
            {'name': 'missing_cwd', 'cwd': 'nested', 'argv': ['python3', '-c', 'print("ran")']},
            {'name': 'root_cwd', 'cwd': '.', 'argv': ['python3', '-c', 'print("root")']},
            {'name': 'subdir_cwd', 'cwd': 'sub',
             'argv': ['python3', '-c',
                      'import pathlib, sys; sys.exit(0 if pathlib.Path("marker.txt").is_file() else 7)']},
        ]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-cwd-missing', overlay=overlay)
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)

        self.assertTrue(result['integrity']['valid'])
        rows = {row['check']: row for row in result['executed_checks']}
        self.assertEqual(sorted(rows), ['missing_cwd', 'root_cwd', 'subdir_cwd'])
        missing = rows['missing_cwd']
        self.assertFalse(missing['passed'])
        self.assertEqual(missing['exit_code'], -1)
        self.assertEqual(missing['cwd'], 'nested')
        self.assertIn('cwd', missing['error'])
        # 合法 cwd（根目录与 overlay 提供的既有子目录）照常执行并通过。
        self.assertTrue(rows['root_cwd']['passed'], rows['root_cwd'])
        self.assertTrue(rows['subdir_cwd']['passed'], rows['subdir_cwd'])
        self.assertFalse(result['reproducible'])
        self.assertFalse(result['checks_passed'])
        self.assertEqual(result['checks_failed'], 1)
        self.assertIn('Offline checks failed: 1 of 3', ' '.join(result['limitations']))

    def test_bundle_reproduction_rejects_cwd_symlink_escape(self):
        # 用例仓库内的符号链接把 cwd 指向副本之外时，同样不能静默改写工作目录
        # 后照常执行；该检查必须失败，且命令不得在副本外的目标目录留下副作用。
        destination = self.sealed_bundle(
            checks=[{'name': 'escaped_cwd', 'cwd': 'escape',
                     'argv': ['python3', '-c', 'open("ESCAPED", "w").write("1")']}],
            repo_name='repo-cwd-escape')
        case_dir = destination / 'cases' / 'case-cwd-escape'
        outside = self.root / 'outside-cwd'
        outside.mkdir()
        (case_dir / 'repository' / 'escape').symlink_to(outside)

        with patch.object(Evolution, 'verify_bundle',
                          return_value={'valid': True, 'mismatches': [], 'limitations': []}):
            result = Evolution.reproduce_bundle(
                destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
                which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)

        self.assertEqual(len(result['executed_checks']), 1)
        row = result['executed_checks'][0]
        self.assertEqual(row['cwd'], 'escape')
        self.assertFalse(row['passed'])
        self.assertEqual(row['exit_code'], -1)
        self.assertIn('escapes the case copy', row['error'])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(result['reproducible'])

    def test_bundle_credentials_follow_exported_provider_requirements(self):
        # 线程 3：凭据需求来自导出的非机密 provider 元数据，不再默认 OPENAI_API_KEY。
        destination = self.sealed_bundle(repo_name='repo-creds')
        exported = json.loads((destination / 'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['providers'], [])
        result = Evolution.reproduce_bundle(destination, env={'PATH': '/usr/bin:/bin'})
        self.assertNotIn('OPENAI_API_KEY', [c['name'] for c in result['credentials']['missing']])
        self.assertNotIn('OPENAI_API_KEY', [c['name'] for c in result['credentials']['available']])
        # 空列表是明确的「无凭据需求」（如本地模型），环境齐备即可复现。
        self.assertTrue(result['reproducible'])
        self.assertFalse(any('provider requirements' in item for item in result['limitations']))

        # 导出裁剪：evidence 里已有 providers 列表时逐项裁为 {id, env_key}，
        # 任何多余字段（可能夹带密钥值）不得进入 bundle。
        poisoned = self.sealed_bundle(
            evidence={'providers': [{'id': 'openai', 'env_key': 'OPENAI_API_KEY',
                                     'api_key': 'sk-LEAK-123', 'secret': 'hunter2'}]},
            repo_name='repo-poison')
        poisoned_evidence = (poisoned / 'evidence.json').read_text(encoding='utf-8')
        self.assertEqual(
            json.loads(poisoned_evidence)['providers'],
            [{'id': 'openai', 'env_key': 'OPENAI_API_KEY'}])
        self.assertNotIn('sk-LEAK-123', poisoned_evidence)
        self.assertNotIn('hunter2', poisoned_evidence)

        # 旧格式导出包没有 provider 元数据：凭据可用性无法判定，不得猜测 OPENAI_API_KEY。
        legacy = self.root / 'legacy'
        shutil.copytree(destination, legacy)
        legacy_evidence = json.loads((legacy / 'evidence.json').read_text(encoding='utf-8'))
        legacy_evidence.pop('providers')
        (legacy / 'evidence.json').write_text(encode(legacy_evidence), encoding='utf-8')
        legacy_manifest = json.loads((legacy / 'bundle.json').read_text(encoding='utf-8'))
        legacy_manifest['files']['evidence.json'] = hashlib.sha256(
            (legacy / 'evidence.json').read_bytes()).hexdigest()
        (legacy / 'bundle.json').write_text(encode(legacy_manifest), encoding='utf-8')
        result = Evolution.reproduce_bundle(legacy, env={'PATH': '/usr/bin:/bin'})
        self.assertTrue(result['integrity']['valid'])
        self.assertFalse(result['reproducible'])
        self.assertEqual(result['credentials']['missing'], [])
        self.assertTrue(any('provider requirements' in item for item in result['limitations']))

        openai_bundle = self.sealed_bundle(
            providers=[{'id': 'openai', 'env_key': 'OPENAI_API_KEY'}], repo_name='repo-openai')
        self.assertEqual(
            json.loads((openai_bundle / 'evidence.json').read_text(encoding='utf-8'))['providers'],
            [{'id': 'openai', 'env_key': 'OPENAI_API_KEY'}])
        result = Evolution.reproduce_bundle(openai_bundle, env={'PATH': '/usr/bin:/bin'})
        self.assertIn('OPENAI_API_KEY', [c['name'] for c in result['credentials']['missing']])

        custom_bundle = self.sealed_bundle(
            providers=[{'id': 'custom', 'env_key': 'CUSTOM_TOKEN'}], repo_name='repo-custom')
        result = Evolution.reproduce_bundle(custom_bundle, env={'PATH': '/usr/bin:/bin'})
        self.assertIn('CUSTOM_TOKEN', [c['name'] for c in result['credentials']['missing']])
        self.assertNotIn('OPENAI_API_KEY', [c['name'] for c in result['credentials']['missing']])

        # 显式 required_credentials 覆盖缺省推断：本地免凭据模型可判定为可复现。
        local = self.sealed_bundle(providers=[{'id': 'ollama', 'env_key': ''}], repo_name='repo-local')
        result = Evolution.reproduce_bundle(
            local, env={'PATH': '/usr/bin:/bin'}, required_credentials=[],
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None,
            run_checks=True)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(result['credentials']['missing'], [])
        self.assertTrue(result['reproducible'])
        self.assertTrue(result['executed_checks'][0]['passed'])

        # provider_requirements 内置与上游供应商映射
        from adaptive_rsi.config import DEFAULTS
        self.assertEqual(Evolution.provider_requirements(DEFAULTS),
                         [{'id': 'openai', 'env_key': ''}])

        multi_cfg = {
            'models': {
                'general': {'provider': 'openai'},
                'heavy': {'provider': 'anthropic'},
                'fast': {'provider': 'deepseek'},
            }
        }
        self.assertEqual(Evolution.provider_requirements(multi_cfg), [
            {'id': 'openai', 'env_key': ''},
            {'id': 'anthropic', 'env_key': 'ANTHROPIC_API_KEY'},
            {'id': 'deepseek', 'env_key': 'DEEPSEEK_API_KEY'},
        ])

        # 本地与显式 env_key='' 行为不受影响
        local_builtins_cfg = {
            'models': {
                'analysis': {'provider': 'ollama'},
                'planning': {'provider': 'lmstudio'},
                'implementation': {'provider': 'opencode'},
            }
        }
        self.assertEqual(Evolution.provider_requirements(local_builtins_cfg), [
            {'id': 'ollama', 'env_key': ''},
            {'id': 'lmstudio', 'env_key': ''},
            {'id': 'opencode', 'env_key': ''},
        ])

        explicit_local_cfg = {
            'models': {'analysis': {'provider': 'custom_local'}},
            'providers': [{'id': 'custom_local', 'name': 'local', 'base_url': 'http://localhost:8080', 'env_key': ''}],
        }
        self.assertEqual(Evolution.provider_requirements(explicit_local_cfg),
                         [{'id': 'custom_local', 'env_key': ''}])

        explicit_openai_local_cfg = {
            'models': {'analysis': {'provider': 'openai'}},
            'providers': [{'id': 'openai', 'name': 'mock-openai', 'base_url': 'http://localhost:8080', 'env_key': ''}],
        }
        self.assertEqual(Evolution.provider_requirements(explicit_openai_local_cfg),
                         [{'id': 'openai', 'env_key': ''}])

    def test_bundle_offline_reproduction_default_builtin_provider_preserves_login_requirements(self):
        # 默认 openai 使用 Codex 登录，不得凭供应商 id 推断 API key。
        from adaptive_rsi.config import DEFAULTS
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        self.config = DEFAULTS
        repo = fixture(self.root / 'repo-default-cred')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {
            'task': 'offline reproduction task',
            'base': base,
            'checks': [{'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']}],
            'source': 'real',
            'signature': 'sig-default-cred',
            'repository': str(repo),
            'overlay': None,
        }
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-default-cred', 'p', 'family-default-cred', 'validation', encode(spec), 0, time.time()))
        evidence = {'dataset': {'manifest': [{'case': 'case-default-cred'}]}}
        self.candidate(evidence)
        destination = self.root / 'bundle-default-cred'
        self.evolution.export_bundle('c', destination)

        # 导出的凭据需求遵循内置 Codex 登录配置。
        exported = json.loads((destination / 'evidence.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['providers'], [{'id': 'openai', 'env_key': ''}])

        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'},
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual(result['credentials']['missing'], [])
        self.assertEqual(result['credentials']['available'], [])
        self.assertTrue(result['reproducible'])

    def test_bundle_run_checks_failure_marks_unreproducible_and_strict_fails(self):
        import subprocess
        import sys
        checks = [
            {'name': 'passing', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']},
            {'name': 'failing', 'cwd': '.', 'argv': ['python3', '-c', 'raise SystemExit(3)']},
        ]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-check-fail')
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertEqual([row['passed'] for row in result['executed_checks']], [True, False])
        self.assertEqual(result['checks_executed'], 2)
        self.assertEqual(result['checks_failed'], 1)
        self.assertFalse(result['checks_passed'])
        self.assertFalse(result['reproducible'])
        self.assertTrue(any('Offline checks failed: 1 of 2' in item for item in result['limitations']))

        strict = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(destination),
             '--run-checks', '--strict'],
            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(strict.returncode, 2, strict.stdout + strict.stderr)
        self.assertIn('未通过，不具备离线复现条件', strict.stdout)
        self.assertIn('[FAIL] case-check-fail - failing', strict.stdout)

        # 正常路径不回归：全部检查通过时仍可复现，--strict 退出 0。
        passing = self.sealed_bundle(
            checks=[{'name': 'passing', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']}],
            repo_name='repo-check-pass')
        result = Evolution.reproduce_bundle(
            passing, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['checks_passed'])
        self.assertTrue(result['reproducible'])
        clean = subprocess.run(
            [sys.executable, '-B', 'scripts/reproduce_bundle.py', str(passing),
             '--run-checks', '--strict'],
            capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

    def test_bundle_malformed_check_specs_block_reproducibility(self):
        checks = [
            {'name': 'string-argv', 'cwd': '.', 'argv': "python3 -c 'print(1)'"},
            {'name': 'empty-argv', 'cwd': '.', 'argv': []},
            {'name': 'escaping-cwd', 'cwd': '../outside', 'argv': ['python3', '-c', 'print(1)']},
            {'name': 'python-in-argv', 'cwd': '.', 'argv': ['python3', 3]},
        ]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-malformed-check')
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        self.assertFalse(result['reproducible'])
        limitations = ' '.join(result['limitations'])
        self.assertIn('checks[0] argv is not a non-empty list of strings', limitations)
        self.assertIn('checks[1] argv is not a non-empty list of strings', limitations)
        self.assertIn('checks[2] cwd is not a relative path inside the case', limitations)
        self.assertIn('checks[3] argv is not a non-empty list of strings', limitations)
        # 畸形规格不执行，也不会按半截 argv 进入依赖清单。
        self.assertEqual(result['executed_checks'], [])
        self.assertIsNone(result['checks_passed'])
        self.assertEqual(result['dependencies']['missing'], [])
        # 未请求执行时同样只凭畸形规格判不可复现。
        passive = Evolution.reproduce_bundle(destination, env={'PATH': '/usr/bin:/bin'})
        self.assertFalse(passive['reproducible'])
        self.assertIsNone(passive['checks_passed'])

    def test_bundle_check_allows_empty_arguments_but_not_an_empty_executable(self):
        checks = [
            {'name': 'empty-arg', 'cwd': '.',
             'argv': ['python3', '-c', 'import sys; assert sys.argv[1] == ""; print("ok")', '']},
            {'name': 'empty-exec', 'cwd': '.', 'argv': ['', '--version']},
        ]
        destination = self.sealed_bundle(checks=checks, repo_name='repo-empty-arg')
        result = Evolution.reproduce_bundle(
            destination, env={'PATH': '/usr/bin:/bin'}, run_checks=True,
            which=lambda cmd, **kw: '/usr/bin/' + cmd if cmd in ('git', 'python3') else None)
        self.assertTrue(result['integrity']['valid'])
        limitations = ' '.join(result['limitations'])
        # 空字符串是合法的 subprocess 参数，只有空可执行名才算畸形规格。
        self.assertNotIn('checks[0]', limitations)
        self.assertIn('checks[1] argv[0] is an empty executable name', limitations)
        self.assertEqual([(row['check'], row['passed']) for row in result['executed_checks']],
                         [('empty-arg', True)])
        self.assertIs(result['checks_passed'], True)
        self.assertFalse(result['reproducible'])

    def test_save_refreshes_derived_evidence_when_plan_or_trial_count_changes(self):
        data = {'repeats': 2, 'samples': [{'id': 'x', 'split': 'validation'}]}
        plan = {'data': data, 'sha256': fingerprint(data)}
        evidence = {'preregistered_plan': plan, 'trials': []}
        self.candidate(evidence)

        with patch.object(self.evolution, 'verify_plan', wraps=self.evolution.verify_plan) as mock_verify, \
             patch('adaptive_rsi.evolution.paired_summary', wraps=paired_summary) as mock_summary:
            # 1. 初始 evaluating 态保存：首次验证计划
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 1)
            self.assertEqual(mock_summary.call_count, 1)
            self.assertEqual(evidence['plan_verification']['analysis'], 'planned_in_progress')

            # 2. 证据未变化时再次保存：沿用缓存，不重复计算
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 1)
            self.assertEqual(mock_summary.call_count, 1)

            # 3. 追加 trial 后再次以 evaluating 保存：试验数变化必须刷新派生证据，
            #    否则中断后落库的仍是 0 条试验时的胜场与计划执行进度。
            trial1 = {'case': 'x', 'split': 'validation', 'repetition': 0,
                      'stable': {'pass': False}, 'candidate': {'pass': True}}
            evidence['trials'].append(trial1)
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 2)
            self.assertEqual(mock_summary.call_count, 2)
            row1 = self.store.one("SELECT evidence FROM candidates WHERE id='c'")
            assert row1 is not None
            persisted = json.loads(row1['evidence'])
            self.assertEqual(len(persisted['trials']), 1)
            self.assertEqual(persisted['win_rate']['wins'], 1)
            self.assertEqual(persisted['plan_verification']['observed'], 1)
            self.assertFalse(persisted['plan_verification']['valid'])

            # 4. 计划变化时：触发重新验证
            modified_data = {'repeats': 1, 'samples': [{'id': 'x', 'split': 'validation'}]}
            evidence['preregistered_plan'] = {'data': modified_data, 'sha256': fingerprint(modified_data)}
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 3)
            self.assertEqual(mock_summary.call_count, 3)

            # 5. 终态保存：触发完整重验
            trial2 = {'case': 'x', 'split': 'validation', 'repetition': 1,
                      'stable': {'pass': False}, 'candidate': {'pass': True}}
            evidence['trials'].append(trial2)
            self.evolution.save('c', 'ready', evidence)
            self.assertEqual(mock_verify.call_count, 4)
            self.assertEqual(mock_summary.call_count, 4)
            row2 = self.store.one("SELECT evidence FROM candidates WHERE id='c'")
            assert row2 is not None
            persisted = json.loads(row2['evidence'])
            self.assertEqual(len(persisted['trials']), 2)
            self.assertIn('wilson_95', persisted['win_rate'])
            # 终态清理缓存
            self.assertNotIn('c', getattr(self.evolution, '_plan_keys', {}))

    def test_evaluating_save_refreshes_win_rate_and_plan_verification_after_each_trial(self):
        # 崩溃恢复只改状态、不重算派生证据：评估中的每次 save 都必须让落库的
        # win_rate / plan_verification 与当前试验数一致，不能停在 0 条试验。
        data = {'repeats': 2, 'samples': [{'id': 'v1', 'split': 'validation'},
                                          {'id': 'h1', 'split': 'holdout'}]}
        plan = {'data': data, 'sha256': fingerprint(data)}
        evidence = {'preregistered_plan': plan, 'trials': []}
        self.candidate(evidence)

        def persisted():
            row = self.store.one("SELECT evidence FROM candidates WHERE id='c'")
            assert row is not None
            return json.loads(row['evidence'])

        self.evolution.save('c', 'evaluating', evidence)
        stored = persisted()
        self.assertEqual(stored['win_rate']['wins'], 0)
        self.assertIsNone(stored['win_rate']['estimate'])
        self.assertEqual(stored['plan_verification']['observed'], 0)
        self.assertEqual(stored['plan_verification']['analysis'], 'planned_in_progress')

        all_trials = [
            {'case': case, 'split': split, 'repetition': repetition,
             'stable': {'pass': False}, 'candidate': {'pass': True}}
            for case, split in (('v1', 'validation'), ('h1', 'holdout'))
            for repetition in range(2)]
        evidence['trials'] = all_trials[:2]
        self.evolution.save('c', 'evaluating', evidence)
        stored = persisted()
        self.assertEqual(len(stored['trials']), 2)
        self.assertEqual(stored['win_rate']['wins'], 1)
        self.assertEqual(stored['plan_verification']['observed'], 2)
        self.assertEqual(stored['plan_verification']['expected'], 4)
        self.assertFalse(stored['plan_verification']['valid'])
        self.assertEqual(stored['plan_verification']['analysis'], 'planned_in_progress')

        evidence['trials'] = all_trials
        self.evolution.save('c', 'evaluating', evidence)
        stored = persisted()
        self.assertEqual(len(stored['trials']), 4)
        self.assertEqual(stored['win_rate']['wins'], 2)
        self.assertEqual(stored['win_rate']['estimate'], 1.0)
        self.assertEqual(stored['plan_verification']['observed'], 4)
        self.assertEqual(stored['plan_verification']['expected'], 4)
        self.assertTrue(stored['plan_verification']['valid'])
        self.assertEqual(stored['plan_verification']['analysis'], 'preregistered')

        # 终态结果不变：完整计划通过，分析口径仍为 preregistered。
        self.evolution.save('c', 'ready', evidence)
        stored = persisted()
        self.assertTrue(stored['plan_verification']['valid'])
        self.assertEqual(stored['plan_verification']['analysis'], 'preregistered')
        self.assertEqual(stored['plan_verification']['observed'], 4)
        self.assertEqual(stored['plan_verification']['expected'], 4)

    def test_case_selection_follows_population_share(self):
        from adaptive_rsi.evolution import case_stratum, select_cases
        cases = ([{'id': 'small-%03d' % index, 'uses': 0,
                   'spec': encode({'task': 'short task', 'source': 'real'})} for index in range(900)] +
                 [{'id': 'large-%03d' % index, 'uses': 0,
                   'spec': encode({'task': 'x' * 20000, 'source': 'real'})} for index in range(100)])
        for count, share in ((6, 1), (10, 1), (20, 2)):
            picked = select_cases(cases, count, 'run-seed')
            allocation = {size: sum(case_stratum(case)[1] == size for case in picked)
                          for size in ('small', 'large')}
            self.assertEqual(len(picked), count)
            # 稀有层占比 10%，样本分配应贴近总体比例而不是与主流量层等量。
            self.assertLessEqual(abs(allocation['large'] - share), 1)
            self.assertGreaterEqual(allocation['small'], 1)
            self.assertEqual(allocation['small'] + allocation['large'], count)

    def test_case_selection_keeps_exact_count_and_stratum_coverage(self):
        from adaptive_rsi.evolution import case_stratum, select_cases
        cases = [{'id': source + '-' + size, 'uses': 0,
                  'spec': encode({'task': 'x' * length, 'source': source})}
                 for source, length, size in (('real', 100, 'small'), ('real', 2000, 'medium'),
                                              ('synthetic', 100, 'small'), ('synthetic', 20000, 'large'))]
        picked = select_cases(cases, 4, 'seed')
        self.assertEqual(len(picked), 4)
        self.assertEqual(len({case['id'] for case in picked}), 4)
        self.assertEqual(len({case_stratum(case) for case in picked}), 4)
        # count 超过总体时不能凭空补样本。
        self.assertEqual(len(select_cases(cases, 9, 'seed')), 4)
        # 层数多于样本量时按固定 hash 顺序整层选取，同样的输入得到同样的结果。
        subset = select_cases(cases, 2, 'seed')
        self.assertEqual(len(subset), 2)
        self.assertEqual(len({case_stratum(case) for case in subset}), 2)
        self.assertEqual([case['id'] for case in subset], [case['id'] for case in select_cases(cases, 2, 'seed')])

    def test_case_selection_seed_only_reorders_cases_inside_a_stratum(self):
        from adaptive_rsi.evolution import case_stratum, select_cases
        cases = ([{'id': 'small-%02d' % index, 'uses': 0,
                   'spec': encode({'task': 'short', 'source': 'real'})} for index in range(30)] +
                 [{'id': 'large-%02d' % index, 'uses': 0,
                   'spec': encode({'task': 'x' * 20000, 'source': 'real'})} for index in range(6)])
        def selection(seed):
            picked = select_cases(cases, 9, seed)
            tally = {}
            for case in picked:
                tally[case_stratum(case)] = tally.get(case_stratum(case), 0) + 1
            return tally, [case['id'] for case in picked]
        first, first_ids = selection('seed')
        repeat, repeat_ids = selection('seed')
        other, other_ids = selection('other')
        self.assertEqual(first_ids, repeat_ids)
        self.assertEqual(first, repeat)
        # 配额只由各层规模决定；换 seed 只在同名层内部换成员。
        self.assertEqual(first, other)
        self.assertNotEqual(first_ids, other_ids)

    def test_plan_records_stratum_population_and_sample_counts(self):
        args = self.setup_proposal()
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git
        from tests.test_runtime import CHECKS
        self.config = merge(DEFAULTS, {'evolution': {'min_cases_per_split': 3, 'repeats': 2}})
        args[3]['config'] = encode(self.config)
        repo = args[1]
        base = git(repo, 'rev-parse', 'HEAD')
        for case, split, source, length in (('validation-2', 'validation', 'real', 100),
                                            ('validation-3', 'validation', 'synthetic', 20000),
                                            ('holdout-2', 'holdout', 'real', 2000),
                                            ('holdout-3', 'holdout', 'synthetic', 20000)):
            spec = {'task': 'x' * length, 'base': base, 'checks': CHECKS, 'source': source,
                    'signature': case, 'repository': str(repo), 'overlay': None}
            self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                               (case, 'p', case, split, encode(spec), 0, 1))
        result = self.evolution.propose_and_evaluate(*args)
        evidence = self.evidence_of(result['candidate'])
        plan = evidence['preregistered_plan']['data']
        validation = plan['allocation']['validation']
        self.assertEqual(validation['requested'], 3)
        self.assertEqual(validation['population'], 3)
        strata = {tuple(item['stratum']): (item['population'], item['sampled'])
                  for item in validation['strata']}
        self.assertEqual(strata, {('real', 'small'): (2, 2), ('synthetic', 'large'): (1, 1)})
        self.assertEqual(plan['allocation']['holdout']['population'], 3)
        self.assertEqual(evidence['promotion_review']['allocation'], plan['allocation'])
        self.assertEqual(plan['selection_policy'], 'traffic_weighted_min_one_per_stratum')
        self.assertEqual(len(plan['samples']), 6)
        self.assertIn('share of the population', ' '.join(evidence['promotion_review']['limitations']))

    def test_sealed_plan_replays_selection_from_recorded_population(self):
        """uses 是层内排序主键：记录 eligible 全集后审计方才能复算抽中的样本。"""
        args = self.setup_proposal()
        from adaptive_rsi.gitops import git
        from tests.test_runtime import CHECKS
        repo = args[1]
        base = git(repo, 'rev-parse', 'HEAD')
        # 找一个 seeded hash 排在既有 validation 用例之前的 id：真实抽样按 uses 选中
        # uses=0 的 'validation'；只有把未知 uses 当成 0 复算才会选中 alt。
        alt = next(name for name in ('validation-alt-%d' % index for index in range(500))
                   if fingerprint({'id': name, 'seed': 'run'}) < fingerprint({'id': 'validation', 'seed': 'run'}))
        spec = {'task': 'validation secret task', 'base': base, 'checks': CHECKS, 'source': 'real',
                'signature': alt, 'repository': str(repo), 'overlay': None}
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           (alt, 'p', alt, 'validation', encode(spec), 2, 1))
        result = self.evolution.propose_and_evaluate(*args)
        evidence = self.evidence_of(result['candidate'])
        # 以 JSON 往返模拟审计方从封存文件读回的计划。
        plan = json.loads(encode(evidence['preregistered_plan']['data']))
        self.assertEqual(plan['selection_seed'], 'run')
        self.assertEqual({row['id']: row['uses'] for row in plan['selection_population']['validation']},
                         {'validation': 0, alt: 2})
        self.assertEqual([row['id'] for row in plan['samples'] if row['split'] == 'validation'],
                         ['validation'])
        verification = Evolution.verify_selection(plan)
        self.assertTrue(verification['valid'])
        self.assertEqual(verification['expected'], verification['observed'])
        self.assertEqual(evidence['promotion_review']['selection_verification'], verification)

        # 篡改排序主键 uses：把未知 uses 当 0 的「最合理复算」会选中另一个样本。
        erased = json.loads(encode(plan))
        next(row for row in erased['selection_population']['validation']
             if row['id'] == alt)['uses'] = 0
        changed = Evolution.verify_selection(erased)
        self.assertFalse(changed['valid'])
        self.assertEqual(changed['reason'], 'sample_not_rederivable')
        self.assertIn('validation:' + alt, changed['expected'])
        self.assertNotIn('validation:validation', changed['expected'])
        # 替换抽中样本或改样本记录的 prior_uses 同样不可复算。
        for mutate, reason in (
                (lambda data: data['samples'].__setitem__(
                    0, {'id': alt, 'split': 'validation', 'stratum': ['real', 'small'], 'prior_uses': 2}),
                 'sample_not_rederivable'),
                (lambda data: data['samples'][0].update(prior_uses=9), 'sample_prior_uses_mismatch')):
            tampered = json.loads(encode(plan))
            mutate(tampered)
            self.assertEqual(Evolution.verify_selection(tampered)['reason'], reason)

    def test_unreproducible_selection_blocks_promotion_and_is_flagged(self):
        args = self.setup_proposal()
        # 让验证集产生一次真实改进，确保执行到终态评审而不是提前返回。
        self.evolution.runtime.execute_candidate = lambda *call, **kwargs: {
            'decision': 'pass' if str(call[5]).endswith('candidate') else 'fail'}
        invalid = {'valid': False, 'reason': 'sample_not_rederivable',
                   'expected': ['validation:other'], 'observed': ['validation:validation']}
        with patch.object(Evolution, 'verify_selection', return_value=invalid):
            result = self.evolution.propose_and_evaluate(*args)
        evidence = self.evidence_of(result['candidate'])
        self.assertEqual(evidence['decision'], 'insufficient_evidence')
        self.assertIn('cannot be reproduced', evidence['reason'])
        self.assertEqual(evidence['promotion_review']['selection_verification'], invalid)
        self.assertTrue(any('cannot be reproduced' in item
                            for item in evidence['promotion_review']['limitations']))
        self.assertEqual(result['status'], 'insufficient_evidence')


if __name__ == '__main__':
    unittest.main()
