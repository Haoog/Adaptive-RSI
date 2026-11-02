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

    def test_stale_reservation_expires_without_blocking_a_new_proposal(self):
        args = self.setup_proposal()
        self.store.execute('INSERT INTO candidates(id,project,kind,status,content,evidence,created) '
                           'VALUES (?,?,?,?,?,?,?)',
                           ('stale', 'p', 'strategy', 'reserved', '{}', '{}', time.time() - 7200))
        result = self.evolution.propose_and_evaluate(*args)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(self.store.one("SELECT status FROM candidates WHERE id='stale'")['status'], 'interrupted')
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

    def test_long_term_failure_is_detected_after_initial_window(self):
        self.candidate({'monitor': {'passed': 1, 'failed': 0, 'runs': ['old']}})
        self.store.execute('INSERT INTO runs(id,project,strategy,config) VALUES (?,?,?,?)', ('late', 'p', encode([{'id': 'c'}]), encode({})))
        self.evolution.monitor('p', False, {'id': 'late'}, attributable=True)
        row = self.store.one('SELECT status,evidence FROM candidates WHERE id=?', ('c',))
        self.assertEqual(row['status'], 'rolled_back')
        self.assertEqual(json.loads(row['evidence'])['drift']['phase'], 'sentinel')

    def test_retirement_and_reclassification_keep_exposure(self):
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)', ('x', 'p', 'family', 'validation', encode({'signature': 's'}), 2, 1))
        self.evolution.update_case('p', 'x', active=False, split='development', source='human:review')
        row = self.store.one('SELECT * FROM cases WHERE id=?', ('x',))
        self.assertEqual(row['uses'], 2)
        self.assertFalse(json.loads(row['spec'])['active'])
        with self.assertRaises(ValueError):
            self.evolution.update_case('p', 'x', active=True, split='holdout', source='human:review')
        self.assertEqual(len(self.store.query("SELECT * FROM events WHERE kind='case_lifecycle'")), 1)

    def test_feedback_is_separate_from_sealed_results(self):
        self.candidate({'trials': [{'case': 'x', 'candidate': {'pass': False}}]})
        self.evolution.record_feedback('c', 'false_positive', 'human:alice', 'late', 'deployment unrelated')
        row = json.loads(self.store.one('SELECT evidence FROM candidates WHERE id=?', ('c',))['evidence'])
        self.assertEqual(row['trials'][0]['candidate']['pass'], False)
        summary = self.evolution.feedback_summary('c')
        self.assertEqual(summary['samples'], 1)
        self.assertIn('bias', summary)

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

    def test_bundle_verification_detects_changed_inputs(self):
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})}})
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)
        self.assertTrue(Evolution.verify_bundle(destination)['valid'])
        (destination / 'evidence.json').write_text('{}')
        self.assertFalse(Evolution.verify_bundle(destination)['valid'])

    def sealed_bundle(self, checks=None, providers=None, evidence=None, repo_name='repo'):
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
            'overlay': None,
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

        # 复现扫描自身的符号链接检测：verify_bundle 只查登记文件，会放过 cases/ 内
        # 未登记的符号链接条目；reproduce 必须主动判失效且不执行其中任何内容。
        scanned = self.root / 'scanned'
        shutil.copytree(pristine, scanned)
        (scanned / 'cases' / 'link').symlink_to(outside / 'x')
        self.assertTrue(Evolution.verify_bundle(scanned)['valid'])
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

    def test_bundle_reproduction_applies_sealed_overlay_in_staging_copy(self):
        # 线程 2：overlay-only 检查在一次性副本中可见并执行，bundle 内文件保持不变。
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

    def test_save_hotpath_skips_reverification_in_evaluating_state(self):
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

            # 2. 追加 trial 后再次以 evaluating 保存：跳过完整重验，但 trials 正常持久化
            trial1 = {'case': 'x', 'split': 'validation', 'repetition': 0,
                      'stable': {'pass': False}, 'candidate': {'pass': True}}
            evidence['trials'].append(trial1)
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 1)
            self.assertEqual(mock_summary.call_count, 1)
            row1 = self.store.one("SELECT evidence FROM candidates WHERE id='c'")
            assert row1 is not None
            persisted = json.loads(row1['evidence'])
            self.assertEqual(len(persisted['trials']), 1)

            # 3. 计划变化时：触发重新验证
            modified_data = {'repeats': 1, 'samples': [{'id': 'x', 'split': 'validation'}]}
            evidence['preregistered_plan'] = {'data': modified_data, 'sha256': fingerprint(modified_data)}
            self.evolution.save('c', 'evaluating', evidence)
            self.assertEqual(mock_verify.call_count, 2)
            self.assertEqual(mock_summary.call_count, 2)

            # 4. 终态保存：触发完整重验
            trial2 = {'case': 'x', 'split': 'validation', 'repetition': 1,
                      'stable': {'pass': False}, 'candidate': {'pass': True}}
            evidence['trials'].append(trial2)
            self.evolution.save('c', 'ready', evidence)
            self.assertEqual(mock_verify.call_count, 3)
            self.assertEqual(mock_summary.call_count, 3)
            row2 = self.store.one("SELECT evidence FROM candidates WHERE id='c'")
            assert row2 is not None
            persisted = json.loads(row2['evidence'])
            self.assertEqual(len(persisted['trials']), 2)
            self.assertIn('wilson_95', persisted['win_rate'])
            # 终态清理缓存
            self.assertNotIn('c', getattr(self.evolution, '_plan_keys', {}))


if __name__ == '__main__':
    unittest.main()
