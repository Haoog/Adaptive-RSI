"""Evidence integrity and dataset lifecycle acceptance without external models."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adaptive_rsi.config import fingerprint
from adaptive_rsi.evolution import Evolution, case_spec
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

    def test_bundle_offline_reproduction_enumerates_dependencies_without_aborting(self):
        from tests.test_runtime import fixture
        from adaptive_rsi.gitops import git
        repo = fixture(self.root / 'repo')
        base = git(repo, 'rev-parse', 'HEAD')
        spec = {
            'task': 'offline reproduction task',
            'base': base,
            'checks': [
                {'name': 'unit_check', 'cwd': '.', 'argv': ['python3', '-c', 'print("ok")']},
                {'name': 'missing_tool_1', 'cwd': '.', 'argv': ['fake-compiler-xyz']},
                {'name': 'missing_tool_2', 'cwd': '.', 'argv': ['nonexistent-linter-abc', '--check']},
            ],
            'source': 'real',
            'signature': 'sig1',
            'repository': str(repo),
        }
        self.store.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?)',
                           ('case-01', 'p', 'family-01', 'validation', encode(spec), 0, time.time()))
        evidence = {
            'dataset': {'manifest': [{'case': 'case-01'}]},
            'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})},
        }
        self.candidate(evidence)
        destination = self.root / 'bundle'
        self.evolution.export_bundle('c', destination)

        # Reproduce in credential-free environment with simulated missing tools
        # git is found, python3 is found, but fake-compiler-xyz and nonexistent-linter-abc are missing
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
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})}})
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
        self.candidate({'preregistered_plan': {'data': {'x': 1}, 'sha256': fingerprint({'x': 1})}})
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


if __name__ == '__main__':
    unittest.main()
