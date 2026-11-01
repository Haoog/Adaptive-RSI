"""Offline acceptance for audit evaluation changes; no model calls."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from adaptive_rsi import evaluator, workflow


def _config(**recovery):
    from adaptive_rsi.config import DEFAULTS, merge
    return merge(DEFAULTS, {'recovery': recovery} if recovery else {})


def _stage_runner():
    """Minimal settings store: the sealed checkpoint layer never needs SQLite."""
    from adaptive_rsi.evaluator import Evaluator

    class MemoryStore:
        def __init__(self): self.data = {}
        def setting(self, key, default=None): return self.data.get(key, default)
        def put_setting(self, key, value): self.data[key] = value
    store = MemoryStore()
    return store, Evaluator(store)


class AuditEvaluationTest(unittest.TestCase):
    def test_sealed_read_only_checkpoint_rejects_mutation_or_changed_input(self):
        from adaptive_rsi.evaluator import Evaluator
        class MemoryStore:
            def __init__(self): self.data = {}
            def setting(self, key, default=None): return self.data.get(key, default)
            def put_setting(self, key, value): self.data[key] = value
        store = MemoryStore()
        runner = Evaluator(store)
        inputs = {'tree': 'a', 'tool': 'python3.14', 'checks': ['x']}
        runner.seal_checkpoint('read', inputs, {'pass': True}, effect='read_only')
        self.assertEqual(runner.reuse_checkpoint('read', inputs), {'pass': True})
        self.assertIsNone(runner.reuse_checkpoint('read', {**inputs, 'tree': 'b'}))
        with self.assertRaises(ValueError):
            runner.seal_checkpoint('write', inputs, {}, effect='workspace_write')
        store.data['sealed-read-checkpoint:read']['result']['pass'] = False
        self.assertIsNone(runner.reuse_checkpoint('read', inputs))

    def test_stage_reuse_requires_explicit_opt_in(self):
        from adaptive_rsi.evaluator import checkpoint_reuse_enabled
        self.assertFalse(checkpoint_reuse_enabled(_config()))
        self.assertTrue(checkpoint_reuse_enabled(_config(reuse_sealed_checkpoints=True)))
        # 只有显式 true 才开启；字符串或数字不能悄悄打开复用。
        self.assertFalse(checkpoint_reuse_enabled(_config(reuse_sealed_checkpoints='yes')))
        self.assertFalse(checkpoint_reuse_enabled(_config(reuse_sealed_checkpoints=1)))
        self.assertFalse(checkpoint_reuse_enabled({'recovery': None}))
        store, runner = _stage_runner()
        calls = []

        def action():
            calls.append(1)
            return {'decision': 'ready', 'generation': len(calls)}

        # 默认关闭：既不复用也不写入检查点。
        runner.execute_stage('planning', {'tree': 'a'}, action, enabled=False)
        runner.execute_stage('planning', {'tree': 'a'}, action, enabled=False)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('sealed-read-checkpoint:planning', store.data)

    def test_stage_reuse_returns_sealed_result_and_reports_reuse(self):
        store, runner = _stage_runner()
        calls, reused = [], []
        inputs = {'tree': 'a', 'checks': ['x']}

        def action():
            calls.append(1)
            return {'decision': 'ready', 'value': {'impact': 'high'}}

        first = runner.execute_stage('planning', inputs, action, enabled=True, on_reuse=reused.append)
        second = runner.execute_stage('planning', inputs, action, enabled=True, on_reuse=reused.append)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reused, ['planning'])
        self.assertIsNotNone(store.data.get('sealed-read-checkpoint:planning'))

    def test_stage_reuse_invalidated_by_changed_input(self):
        _store, runner = _stage_runner()
        calls = []

        def action():
            calls.append(1)
            return {'decision': 'ready', 'generation': len(calls)}

        first = runner.execute_stage('planning', {'tree': 'a'}, action, enabled=True)
        second = runner.execute_stage('planning', {'tree': 'b'}, action, enabled=True)
        self.assertEqual(first['generation'], 1)
        self.assertEqual(second['generation'], 2)
        self.assertEqual(len(calls), 2)

    def test_stage_reuse_invalidated_by_tampered_checkpoint(self):
        store, runner = _stage_runner()
        calls = []

        def action():
            calls.append(1)
            return {'decision': 'pass', 'generation': len(calls)}

        runner.execute_stage('review', {'review_key': 'k'}, action, enabled=True)
        store.data['sealed-read-checkpoint:review']['result']['decision'] = 'fail'
        result = runner.execute_stage('review', {'review_key': 'k'}, action, enabled=True)
        self.assertEqual(result['decision'], 'pass')
        self.assertEqual(result['generation'], 2)
        self.assertEqual(len(calls), 2)

    def test_write_and_remote_stages_never_reuse_or_seal_checkpoints(self):
        store, runner = _stage_runner()
        for effect in ('workspace_write', 'remote_action'):
            calls = []

            def action():
                calls.append(1)
                return {'effect': effect, 'generation': len(calls)}

            # 即便显式要求复用，写工作区与远端动作也始终真实执行。
            runner.execute_stage('implementation', {'patch': 'p'}, action, effect=effect, enabled=True)
            result = runner.execute_stage('implementation', {'patch': 'p'}, action, effect=effect, enabled=True)
            self.assertEqual(result['generation'], 2)
            self.assertEqual(len(calls), 2)
            self.assertNotIn('sealed-read-checkpoint:implementation', store.data)
        # 写阶段即便被显式要求封存也只允许 read_only，直接拒绝。
        with self.assertRaises(ValueError):
            runner.seal_checkpoint('implementation', {'patch': 'p'}, {'ok': True}, effect='workspace_write')

    def test_clean_committed_harness_change_cannot_reuse_frozen_regression(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git, snapshot
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            git(source, 'init', '-q')
            (source / 'test_value.py').write_text('assert True\n')
            git(source, 'add', '.')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            candidate = snapshot(source, root / 'candidate')
            (candidate / 'test_value.py').write_text('assert False\n')
            git(candidate, 'add', '.')
            git(candidate, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'candidate')
            check = {'name': 'criterion', 'argv': ['python3', 'test_value.py']}
            def execute(_sandbox, check, workspace, *args, **kwargs):
                failed = 'False' in (Path(workspace) / 'test_value.py').read_text()
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': int(failed),
                        'stdout': '', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': [check]}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state')).evaluate(
                    source, candidate, [check], root / 'evaluation', merge(DEFAULTS, {}), changed_files=[])
            self.assertFalse(result['candidate_full_reused'])
            self.assertTrue(result['checks']['candidate'][0]['passed'])
            self.assertFalse(result['checks']['candidate_full'][0]['passed'])
            self.assertEqual(result['decision'], 'fail')

    def test_unavailable_diff_blocks_pass_and_freeze_reuse(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            check = {'name': 'criterion', 'argv': ['python3', 'app.py']}
            calls = []
            def execute(_sandbox, check, workspace, *args, **kwargs):
                calls.append(Path(workspace).name)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator._changed_files', return_value=False), patch(
                    'adaptive_rsi.evaluator._committed_changes', return_value=False), patch(
                    'adaptive_rsi.evaluator.detect', return_value={'checks': [check]}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state')).evaluate(
                    source, source, [check], root / 'evaluation', merge(DEFAULTS, {}))
            self.assertEqual(result['decision'], 'insufficient_evidence')
            self.assertEqual(result['checks'], {})
            self.assertEqual(calls, [])

    def test_committed_history_unavailable_blocks_pass_between_snapshots(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git, snapshot
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            git(source, 'init', '-q')
            git(source, 'add', '.')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            candidate = snapshot(source, root / 'candidate')
            check = {'name': 'criterion', 'argv': ['python3', 'app.py']}
            calls = []
            def execute(_sandbox, check, workspace, *args, **kwargs):
                calls.append(Path(workspace).name)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator._committed_changes', return_value=False), patch(
                    'adaptive_rsi.evaluator.detect', return_value={'checks': [check]}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state2')).evaluate(
                    source, candidate, [check], root / 'evaluation', merge(DEFAULTS, {}))
            self.assertEqual(result['decision'], 'insufficient_evidence')
            self.assertEqual(result['checks'], {})
            self.assertEqual(calls, [])

    def test_committed_test_edit_blocks_freeze_reuse_between_snapshots(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git, snapshot
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            git(source, 'init', '-q')
            (source / 'app.py').write_text('VALUE = 1\n')
            (source / 'test_value.py').write_text('assert True\n')
            git(source, 'add', '.')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            base_sha = git(source, 'rev-parse', 'HEAD')
            baseline = snapshot(source, root / 'baseline', base_sha)
            (source / 'test_value.py').write_text('assert False\n')
            git(source, 'add', '.')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'break')
            head_sha = git(source, 'rev-parse', 'HEAD')
            candidate = snapshot(source, root / 'candidate', head_sha)
            self.assertEqual(evaluator._changed_files(candidate), [])
            check = {'name': 'criterion', 'argv': ['python3', 'app.py']}
            def execute(_sandbox, check, workspace, *args, **kwargs):
                broken = 'False' in (Path(workspace) / 'test_value.py').read_text()
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None} if not broken else {
                        'name': check['name'], 'argv': check['argv'], 'exit_code': 1,
                        'stdout': '', 'stderr': 'AssertionError', 'error': None}
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': [check]}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state3')).evaluate(
                    baseline, candidate, [check], root / 'evaluation', merge(DEFAULTS, {}))
            self.assertFalse(result['candidate_full_reused'])
            self.assertFalse(result['checks']['candidate_full'][0]['passed'])
            self.assertEqual(result['decision'], 'fail')

    def test_failed_worktree_inspection_blocks_frozen_reuse_despite_committed_edits(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.gitops import git, snapshot
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            git(source, 'init', '-q')
            (source / 'app.py').write_text('VALUE = 1\n')
            (source / 'test_value.py').write_text('assert True\n')
            git(source, 'add', '.')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            base_sha = git(source, 'rev-parse', 'HEAD')
            baseline = snapshot(source, root / 'baseline', base_sha)
            (source / 'app.py').write_text('VALUE = 2\n')
            git(source, 'add', 'app.py')
            git(source, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'candidate')
            head_sha = git(source, 'rev-parse', 'HEAD')
            candidate = snapshot(source, root / 'candidate', head_sha)
            self.assertEqual(evaluator._changed_files(candidate), [])
            (candidate / 'test_value.py').write_text('assert False\n')
            check = {'name': 'criterion', 'argv': ['python3', 'app.py']}
            calls = []
            def execute(_sandbox, check, workspace, *args, **kwargs):
                calls.append(Path(workspace).name)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator._changed_files', return_value=None), patch(
                    'adaptive_rsi.evaluator.detect', return_value={'checks': [check]}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state')).evaluate(
                    baseline, candidate, [check], root / 'evaluation', merge(DEFAULTS, {}))
            self.assertEqual(result['decision'], 'insufficient_evidence')
            self.assertEqual(result['checks'], {})
            self.assertEqual(calls, [])

    def test_detected_additions_run_once_with_unchanged_harness(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            checks = [{'name': name, 'argv': ['python3', '-c', 'print(1)'], 'timeout_seconds': i + 1}
                      for i, name in enumerate(['fixed', 'detected'])]
            # Different argv are distinct commands; timeout is also part of the identity.
            checks[1]['argv'][-1] = 'print(2)'
            calls = []
            def execute(_sandbox, check, workspace, *args, **kwargs):
                calls.append((Path(workspace).name, check['name']))
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': '', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': checks}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state')).evaluate(
                    source, source, checks[:1], root / 'evaluation', merge(DEFAULTS, {}))
            self.assertEqual([name for stage, name in calls if stage != 'baseline'], ['fixed', 'detected'])
            self.assertEqual(len(result['checks']['candidate_full']), 2)

    def test_same_check_name_in_two_modules_keeps_both_results(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            configured = [{'name': 'module', 'argv': ['python3', '-c', 'print(1)'], 'timeout_seconds': 1}]
            detected = [{'name': 'module', 'argv': ['python3', '-c', 'print(2)'], 'timeout_seconds': 2, 'cwd': 'nested'}]
            def execute(_sandbox, check, workspace, *args, **kwargs):
                return {'name': check['name'], 'argv': check['argv'], 'cwd': check.get('cwd', '.'),
                        'exit_code': 1 if check['argv'][-1] == 'print(2)' else 0,
                        'stdout': '', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': detected}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(Store(root / 'state')).evaluate(
                    source, source, configured, root / 'evaluation', merge(DEFAULTS, {}), changed_files=[])
            rows = result['checks']['candidate_full']
            self.assertEqual(len(rows), 2)
            self.assertEqual({(row['argv'][-1], row['passed']) for row in rows},
                             {('print(1)', True), ('print(2)', False)})

    def test_exact_host_tool_versions_are_recorded_without_cache_authorization(self):
        tools = evaluator.host_tool_versions([{'argv': ['python3', '-m', 'unittest']}])
        self.assertIn('python3', tools)
        self.assertEqual(tools['python3']['state'], 'available')
        self.assertTrue(tools['python3']['version'])
        self.assertTrue(tools['python3']['binary_sha256'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Requires macOS Seatbelt; ordinary Linux jobs have no cached sandbox image')
    def test_fixed_controls_must_be_detected_by_the_real_gate(self):
        from adaptive_rsi.evaluator import Evaluator
        from adaptive_rsi.config import merge, DEFAULTS
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            check = {'name': 'criterion', 'argv': ['python3', '-c', 'from value import VALUE; assert VALUE == 1'], 'timeout': 10}
            controls = [{'id': 'wrong_value', 'criterion': 'value remains one', 'source': 'host:fixture',
                         'files': [{'path': 'value.py', 'content': 'VALUE = 2\n'}]}]
            from types import SimpleNamespace
            result = Evaluator(SimpleNamespace(root=root)).validate_controls(source, [check], controls, root / 'controls', merge(DEFAULTS, {}))
            self.assertEqual(result['decision'], 'pass')
            self.assertEqual(result['controls'][0]['id'], 'wrong_value')

    def test_configured_controls_run_in_production_evaluation_and_can_block(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            check = {'name': 'criterion', 'argv': ['python3', '-c', 'from value import VALUE; assert VALUE == 1']}
            def execute(_sandbox, check, workspace, *args, **kwargs):
                passed = 'VALUE = 1' in (Path(workspace) / 'value.py').read_text()
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0 if passed else 1,
                        'stdout': 'Ran 1 test\n' if passed else '', 'stderr': '' if passed else 'AssertionError',
                        'error': None, 'seconds': 0.1}
            control = {'id': 'wrong_value', 'criterion': 'value remains one', 'source': 'host:fixture',
                       'files': [{'path': 'value.py', 'content': 'VALUE = 2\n'}]}
            store = Store(root / 'state')
            self.addCleanup(store.close)
            runner = evaluator.Evaluator(store)
            config = merge(DEFAULTS, {'validation': {'controls': [control]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = runner.evaluate(source, source, [check], root / 'evaluation', config, changed_files=[])
            self.assertEqual(result['decision'], 'pass')
            self.assertEqual(result['controls']['decision'], 'pass')
            self.assertTrue(result['controls']['controls'][0]['detected'])

            undetected = merge(DEFAULTS, {'validation': {'controls': [
                {**control, 'files': [{'path': 'value.py', 'content': 'VALUE = 1\n'}]}]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                blocked = runner.evaluate(source, source, [check], root / 'blocked', undetected, changed_files=[])
            self.assertEqual(blocked['decision'], 'insufficient_evidence')
            self.assertIn('control', blocked['reason'].lower())
            self.assertFalse(blocked['controls']['controls'][0]['detected'])
            # A maintainer regression fixture can describe the behavior being
            # repaired. Its positive control must execute on the fixed candidate.
            candidate = root / 'fixed'
            candidate.mkdir()
            (source / 'value.py').write_text('VALUE = 2\n')
            (candidate / 'value.py').write_text('VALUE = 1\n')
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                repaired = runner.evaluate(source, candidate, [check], root / 'repaired', config, changed_files=[])
            self.assertEqual(repaired['decision'], 'pass')
            self.assertTrue(repaired['controls']['controls'][0]['detected'])

            with patch.object(evaluator.Evaluator, 'validate_controls') as validate, patch(
                    'adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                runner.evaluate(source, source, [check], root / 'no-controls', merge(DEFAULTS, {}), changed_files=[])
            validate.assert_not_called()

    def test_required_test_identity_cannot_be_satisfied_by_unrelated_tests(self):
        required = ['target.test_regression']
        unrelated = [{'test_identities': {'state': 'reported', 'executed': ['other.test_pass'], 'skipped': []}}]
        self.assertFalse(evaluator.verify_test_identities(unrelated, required)['valid'])
        matching = [{'test_identities': {'state': 'reported', 'executed': required, 'skipped': []}}]
        self.assertTrue(evaluator.verify_test_identities(matching, required)['valid'])

    def test_executed_identities_and_skip_reasons(self):
        xml = '<testsuite tests="2" skipped="1"><testcase classname="X" name="yes"/><testcase classname="X" name="no"><skipped message="unsupported platform"/></testcase></testsuite>'
        result = evaluator.test_identities(xml)
        self.assertEqual(result['executed'], ['X.yes'])
        self.assertEqual(result['skipped'], [{'id': 'X.no', 'reason': 'unsupported platform'}])
        self.assertEqual(evaluator.test_identities('all passed')['state'], 'unavailable')

    def test_bounded_artifacts_reject_links_and_host_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'junit.xml').write_text('<testsuite tests="1"><testcase name="x"/></testsuite>')
            (root / 'coverage.xml').symlink_to('/etc/hosts')
            rows = evaluator.collect_test_artifacts(root)
            self.assertEqual(rows['junit.xml']['state'], 'collected')
            self.assertEqual(rows['coverage.xml']['state'], 'rejected')
            self.assertEqual(rows['lcov.info']['state'], 'missing')

    def test_dependency_graph_closes_over_dependents_and_unknown_falls_back(self):
        checks = [{'name': 'a'}, {'name': 'b'}, {'name': 'all'}]
        graph = {'a': {'paths': ['packages/a/'], 'depends_on': [], 'checks': ['a']},
                 'b': {'paths': ['packages/b/'], 'depends_on': ['a'], 'checks': ['b']}}
        self.assertEqual([c['name'] for c in evaluator.affected_checks(['packages/a/x'], checks, graph)], ['a', 'b'])
        self.assertEqual(evaluator.affected_checks(['unknown/x'], checks, graph), checks)
        self.assertEqual(evaluator.affected_checks(['packages/a/x'], checks, {'b': graph['b']}), checks)

    def test_performance_guardrail_blocks_primary_gain(self):
        threshold = {'metric': 'latency', 'unit': 'ms', 'direction': 'lower', 'target': 8, 'tolerance': 0}
        guard = {'metric': 'memory', 'unit': 'MiB', 'direction': 'lower', 'target': 50, 'tolerance': 0}
        def row(latency, memory):
            return {'passed': True, 'stdout': '\n'.join(json.dumps({'metric': m, 'unit': u, 'value': v}) for m, u, v in [('latency', 'ms', latency), ('memory', 'MiB', memory)])}
        acceptance = {'change_kind': 'performance', 'contract': {'threshold': threshold, 'guardrails': [guard]}}
        result = workflow.improvement({'acceptance_baseline': [row(10, 40)], 'acceptance_candidate': [row(5, 90)]}, acceptance)
        self.assertEqual(result['decision'], 'insufficient_evidence')
        self.assertFalse(result['guardrails'][0]['passed'])

    def test_repeated_benchmarks_require_consistent_loads_and_report_noise(self):
        threshold = {'metric': 'latency', 'unit': 'ms', 'direction': 'lower', 'target': 9, 'tolerance': 0}
        contract = {'threshold': threshold, 'repetitions': 3, 'load_conditions': ['idle', 'busy']}
        def row(values):
            return {'passed': True, 'stdout': '\n'.join(json.dumps({'metric': 'latency', 'unit': 'ms', 'value': value, 'load': load}) for load in ['idle', 'busy'] for value in values)}
        result = workflow.improvement({'acceptance_baseline': [row([10, 11, 12])], 'acceptance_candidate': [row([5, 6, 7])]}, {'change_kind': 'performance', 'contract': contract})
        self.assertEqual(result['decision'], 'pass')
        self.assertIn('uncertainty', result)
        result = workflow.improvement({'acceptance_baseline': [row([10])], 'acceptance_candidate': [row([5])]}, {'change_kind': 'performance', 'contract': contract})
        self.assertEqual(result['decision'], 'insufficient_evidence')


if __name__ == '__main__':
    unittest.main()
