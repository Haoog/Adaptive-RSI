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

    def test_seed_clear_removes_regular_file_and_symlink_without_crashing(self):
        # 候选把 seed 路径建成普通文件时，clear=True 不得抛 NotADirectoryError。
        from adaptive_rsi.config import DEFAULTS, merge
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeds = root / 'seeds'
            (seeds / 'deps').mkdir(parents=True)
            (seeds / 'deps/value').write_text('seeded\n')
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']}})
            target = root / 'target'
            target.mkdir()
            (target / 'deps').write_text('candidate-file\n')
            evaluator.Evaluator._seed_target(target, seeds, config, clear=True)
            self.assertTrue((target / 'deps').is_dir())
            self.assertEqual((target / 'deps/value').read_text(), 'seeded\n')
            # 断开的符号链接也按同一清除路径处理，不留给 restore() 的 overlap 检查。
            import shutil as _shutil
            _shutil.rmtree(target / 'deps')
            (target / 'deps').symlink_to('missing')
            evaluator.Evaluator._seed_target(target, seeds, config, clear=True)
            self.assertTrue((target / 'deps').is_dir())
            self.assertEqual((target / 'deps/value').read_text(), 'seeded\n')

    def test_acceptance_stage_clears_candidate_seed_overlap(self):
        # 候选把文件写在 seed 路径时，独立验收阶段必须清除后还原种子，
        # 而不是让 restore() 的 overlap RuntimeError 逃出评估。
        from adaptive_rsi.config import DEFAULTS, merge
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeds = root / 'seeds'
            (seeds / 'deps').mkdir(parents=True)
            (seeds / 'deps/value').write_text('seeded\n')
            source = root / 'source'
            source.mkdir()
            (source / 'deps').write_text('candidate-file\n')
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']}})
            acceptance = {'hash': 'h', 'checks': [], 'spec': {'files': []}, 'criteria': []}
            outcome = evaluator.Evaluator(SimpleNamespace(root=root / 'state'))._acceptance_stage(
                'acceptance_candidate', source, source, acceptance, root / 'evaluation',
                config, None, seeds, [])
            self.assertEqual(outcome, {'rows': []})
            target = root / 'evaluation' / 'acceptance_candidate'
            self.assertTrue((target / 'deps').is_dir())
            self.assertEqual((target / 'deps/value').read_text(), 'seeded\n')

    def test_controls_clear_candidate_seed_overlap(self):
        # 对照例目标同样复用 seed 路径，候选写入该路径时不得让阶段异常退出。
        from adaptive_rsi.config import DEFAULTS, merge
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeds = root / 'seeds'
            (seeds / 'deps').mkdir(parents=True)
            (seeds / 'deps/value').write_text('seeded\n')
            source = root / 'source'
            source.mkdir()
            (source / 'deps').write_text('candidate-file\n')
            (source / 'value.py').write_text('VALUE = 1\n')
            checks = [{'name': 'unit', 'argv': ['python3', 'value.py']}]
            controls = [{'id': 'c1', 'source': 'host:fixture', 'criterion': 'x',
                         'files': [{'path': 'value.py', 'content': 'VALUE = 2\n'}]}]
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']}})

            def execute(_sandbox, check, workspace, *args, **kwargs):
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}

            with patch('adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                outcome = evaluator.Evaluator(SimpleNamespace(root=root / 'state')).validate_controls(
                    source, checks, controls, root / 'controls', config, seeds=seeds)
            # 空夹具下检查通过但未检出反例：仍返回结构化结论而非抛异常。
            self.assertEqual(outcome['decision'], 'insufficient_evidence')
            self.assertFalse(outcome['controls'][0]['detected'])
            for target in [root / 'controls/positive', root / 'controls/0']:
                self.assertTrue((target / 'deps').is_dir())
                self.assertEqual((target / 'deps/value').read_text(), 'seeded\n')

    def test_seed_overlap_writes_evaluation_report(self):
        # 端到端：候选写入 seed 路径时评估仍落盘 report.json。
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seeds = root / 'seeds'
            (seeds / 'deps').mkdir(parents=True)
            (seeds / 'deps/value').write_text('seeded\n')
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            candidate = root / 'candidate'
            candidate.mkdir()
            (candidate / 'value.py').write_text('VALUE = 1\n')
            (candidate / 'deps').write_text('candidate-file\n')
            checks = [{'name': 'unit', 'argv': ['python3', 'value.py']}]
            acceptance = {'hash': 'h', 'change_kind': 'refactor', 'criteria': ['behavior preserved'],
                          'checks': [{'name': 'ac', 'argv': ['python3', 'value.py']}],
                          'spec': {'files': [], 'checks': [{'name': 'ac', 'criteria': [0]}]}}
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']}})

            restored = []

            def execute(_sandbox, check, workspace, *args, **kwargs):
                marker = Path(workspace) / 'deps' / 'value'
                restored.append(marker.read_text() if marker.is_file() else None)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}

            store = Store(root / 'state')
            self.addCleanup(store.close)
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                # 必须真的把快照交给 evaluate()：不传 seeds 时 _seed_target() 直接
                # 返回，验收阶段不会触发 overlap，用例就测不到本次修复的行为。
                result = evaluator.Evaluator(store).evaluate(
                    source, candidate, checks, root / 'evaluation', config,
                    seeds=seeds, changed_files=[], acceptance=acceptance)
            # 每个阶段（含验收阶段）执行检查时都应看到已还原的种子内容。
            self.assertTrue(restored)
            self.assertEqual(set(restored), {'seeded\n'})
            report = json.loads((root / 'evaluation' / 'report.json').read_text())
            self.assertEqual(report['decision'], result['decision'])
            self.assertEqual(result['decision'], 'pass')

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

    def test_rejected_sealed_review_reruns_stage_without_resealing(self):
        store, runner = _stage_runner()
        calls, reused, rejected = [], [], []
        inputs = {'review_key': 'k', 'patch_hash': 'h'}
        # 上一轮把一条证据不足的评审结果封存了；审批谓词只接受通过的记录。
        runner.seal_checkpoint('review', inputs, {'decision': 'insufficient_evidence', 'generation': 1},
                               effect='read_only')
        approved = lambda value: value.get('decision') == 'pass'

        def unclear():
            calls.append(1)
            return {'decision': 'insufficient_evidence', 'generation': 2}

        result = runner.execute_stage('review', inputs, unclear, enabled=True, accept=approved,
                                      on_reuse=reused.append, on_reject=rejected.append)
        # 被拒的封存结果不能顶替真实执行，也不能报告为复用。
        self.assertEqual(result['generation'], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reused, [])
        self.assertEqual(rejected, ['review'])
        # 仍未通过的复查结果不会覆盖封存记录，重试时依旧重新评审。
        self.assertEqual(runner.reuse_checkpoint('review', inputs),
                         {'decision': 'insufficient_evidence', 'generation': 1})

        def passing():
            calls.append(1)
            return {'decision': 'pass', 'generation': 3}

        runner.execute_stage('review', inputs, passing, enabled=True, accept=approved, on_reuse=reused.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(reused, [])
        # 通过的复查结果照常封存，之后的重试直接复用而不再执行评审。
        final = runner.execute_stage('review', inputs, passing, enabled=True, accept=approved,
                                     on_reuse=reused.append)
        self.assertEqual(final['generation'], 3)
        self.assertEqual(reused, ['review'])
        self.assertEqual(len(calls), 2)

    def test_accept_predicate_error_blocks_reuse_and_sealing(self):
        store, runner = _stage_runner()
        calls, reused = [], []
        inputs = {'review_key': 'k'}
        runner.seal_checkpoint('review', inputs, {'decision': 'insufficient_evidence'}, effect='read_only')

        def broken(_value):
            raise ValueError('predicate bug')

        def action():
            calls.append(1)
            return {'decision': 'pass', 'generation': 2}

        result = runner.execute_stage('review', inputs, action, enabled=True, accept=broken,
                                      on_reuse=reused.append)
        # 谓词自身异常视同拒绝：真实执行、不报告复用，也不把新结果写进封存记录。
        self.assertEqual(result['generation'], 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(reused, [])
        self.assertEqual(store.data['sealed-read-checkpoint:review']['result'],
                         {'decision': 'insufficient_evidence'})

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
        from unittest.mock import patch
        tools = evaluator.host_tool_versions([{'argv': ['python3', '-m', 'unittest']}])
        self.assertIn('python3', tools)
        self.assertEqual(tools['python3']['state'], 'available')
        self.assertTrue(tools['python3']['version'])
        self.assertTrue(tools['python3']['binary_sha256'])
        # 未提供执行身份时不写 scope：依赖准备缓存键保持稳定。
        self.assertNotIn('_scope', tools)
        scoped = evaluator.host_tool_versions([{'argv': ['python3', '-m', 'unittest']}],
                                              {'backend': 'docker', 'image_id': 'sha256:' + 'a' * 64})
        self.assertEqual(scoped['_scope'], {'probe': 'host', 'checks_run_in': 'docker'})
        self.assertNotIn('image_id', scoped)
        unsupported = evaluator.host_tool_versions([{'argv': ['custom-runner', '--check']}])
        self.assertEqual(unsupported['custom-runner'],
                         {'state': 'unavailable', 'reason': 'not_an_allowlisted_host_probe'})
        with patch('adaptive_rsi.evaluator.shutil.which', return_value=None):
            missing = evaluator.host_tool_versions([{'argv': ['node', '--version']}])
        self.assertEqual(missing['node'], {'state': 'missing'})

    def test_large_host_binary_is_hashed_in_bounded_chunks_and_digest_is_cached(self):
        import builtins
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'node'
            payload = b'n' * (evaluator.HOST_TOOL_DIGEST_CHUNK * 2 + 17)
            binary.write_bytes(payload)
            expected = hashlib.sha256(payload).hexdigest()
            reads = []
            real_open = builtins.open

            class Tracked:
                def __init__(self, handle): self.handle = handle
                def __enter__(self): return self
                def __exit__(self, *args): self.handle.close()
                def fileno(self): return self.handle.fileno()
                def read(self, size=-1):
                    reads.append(size)
                    return self.handle.read(size)

            def tracked_open(file, *args, **kwargs):
                handle = real_open(file, *args, **kwargs)
                return Tracked(handle) if str(file) == str(binary) else handle

            probe = {'exit_code': 0, 'stdout': 'v20.0.0\n', 'stderr': '', 'error': None}
            with patch('adaptive_rsi.evaluator.shutil.which', return_value=str(binary)), \
                    patch('adaptive_rsi.process.run_process', return_value=probe), \
                    patch('pathlib.Path.read_bytes', side_effect=AssertionError('whole-file read')), \
                    patch('builtins.open', side_effect=tracked_open):
                first = evaluator.host_tool_versions([{'argv': ['node', '--version']}])
                self.assertEqual(first['node']['binary_sha256'], expected)
                self.assertLessEqual(max(reads), evaluator.HOST_TOOL_DIGEST_CHUNK)
                self.assertGreaterEqual(len(reads), 4)
                cached_reads = list(reads)
                second = evaluator.host_tool_versions([{'argv': ['node', '--version']}])
                self.assertEqual(reads, cached_reads)
                self.assertEqual(second['node']['binary_sha256'], expected)
                binary.write_bytes(payload + b'!')
                reads.clear()
                replaced = evaluator.host_tool_versions([{'argv': ['node', '--version']}])
                self.assertNotEqual(replaced['node']['binary_sha256'], expected)
                self.assertTrue(reads)

    def test_docker_report_marks_host_probes_next_to_execution_image_identity(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        identity = {'build': 'build', 'backend': 'docker', 'image_id': 'sha256:' + 'a' * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            checks = [{'name': 'module', 'argv': ['python3', '-c', 'print(1)'], 'timeout_seconds': 1}]

            def execute(_sandbox, check, workspace, *args, **kwargs):
                return {'name': check['name'], 'argv': check['argv'], 'cwd': check.get('cwd', '.'),
                        'exit_code': 0, 'stdout': '', 'stderr': '', 'error': None}

            runner = evaluator.Evaluator(Store(root / 'state'))
            with patch.object(runner, '_execution_environment', return_value=(merge(DEFAULTS, {}), identity)), \
                    patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), \
                    patch('adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = runner.evaluate(source, source, checks, root / 'evaluation', merge(DEFAULTS, {}),
                                         cache_scope='project', cache_base='base', changed_files=[])
        self.assertEqual(result['execution_environment'], identity)
        self.assertEqual(result['host_tools']['_scope'], {'probe': 'host', 'checks_run_in': 'docker'})
        self.assertNotIn('image_id', result['host_tools'])

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

    def test_controls_strip_candidate_added_tests_and_protect_fixtures(self):
        """Candidate-added test files cannot decide whether a control fixture was caught."""
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            # Host-owned harness asserts the real behavior; the counterexample
            # fixture flips VALUE so detection must come from this file alone.
            (source / 'test_value.py').write_text(
                'from value import VALUE\nassert VALUE == 1\n')
            check = {'name': 'criterion', 'argv': ['python3', 'test_value.py']}
            control = {'id': 'wrong_value', 'criterion': 'value remains one',
                       'source': 'host:fixture',
                       'files': [{'path': 'value.py', 'content': 'VALUE = 2\n'}]}
            # Candidate adds a hostile test that fails whenever the fixture is
            # applied; under the unfrozen harness it would make ``detected``
            # unconditionally true.
            candidate = root / 'candidate'
            candidate.mkdir()
            (candidate / 'value.py').write_text('VALUE = 1\n')
            (candidate / 'test_value.py').write_text(
                'from value import VALUE\nassert VALUE == 1\n')
            (candidate / 'test_forged.py').write_text(
                'from pathlib import Path\nassert "VALUE = 1" in Path("value.py").read_text()\n'
                'assert "VALUE = 1" in Path("test_forged.py").read_text() or True\n'
                'import sys; sys.exit(0 if "VALUE = 1" in Path("value.py").read_text() else 1)\n')
            workspaces = []
            def execute(_sandbox, check, workspace, *args, **kwargs):
                workspaces.append(Path(workspace))
                forged = Path(workspace, 'test_forged.py').exists()
                value_one = 'VALUE = 1' in Path(workspace, 'value.py').read_text()
                # A forged run would always "detect" because the hostile file
                # fails on the fixture; the frozen harness detects only via the
                # baseline test asserting VALUE == 1.
                return {'name': check['name'], 'argv': check['argv'],
                        'exit_code': 0 if value_one else 1,
                        'stdout': 'Ran 1 test\n' if value_one else '',
                        'stderr': '' if value_one else 'AssertionError',
                        'error': None, 'seconds': 0.1,
                        'forged_present': forged}
            store = Store(root / 'state')
            self.addCleanup(store.close)
            runner = evaluator.Evaluator(store)
            config = merge(DEFAULTS, {'validation': {'controls': [control]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = runner.evaluate(source, candidate, [check], root / 'evaluation',
                                         config, changed_files=[])
            self.assertEqual(result['decision'], 'pass')
            self.assertEqual(result['controls']['decision'], 'pass')
            self.assertTrue(result['controls']['controls'][0]['detected'])
            control_targets = [p for p in workspaces
                               if p.parent.name.startswith('controls')]
            self.assertTrue(control_targets)
            for target in control_targets:
                self.assertFalse((target / 'test_forged.py').exists(),
                                 'candidate-added test must not run inside a control')
                self.assertTrue((target / 'test_value.py').is_file())

    def test_controls_reject_benign_fixture_when_candidate_rewrites_harness(self):
        """A hostile candidate harness must not make every control look detected."""
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            (source / 'test_value.py').write_text('from value import VALUE\nassert VALUE == 1\n')
            check = {'name': 'criterion', 'argv': ['python3', 'test_value.py']}
            # The fixture restates the baseline value, so it is not a
            # counterexample: a frozen baseline harness must not flag it.
            control = {'id': 'benign', 'criterion': 'value remains one', 'source': 'host:fixture',
                       'files': [{'path': 'value.py', 'content': 'VALUE = 1\n'}]}
            candidate = root / 'candidate'
            candidate.mkdir()
            (candidate / 'value.py').write_text('VALUE = 1\n')
            # Both forgery routes: rewrite the existing harness and add a test
            # that fails whenever the fixture touches the asserted file.
            (candidate / 'test_value.py').write_text('assert False\n')
            (candidate / 'test_forged.py').write_text(
                'from pathlib import Path\nassert "VALUE = 1" in Path("value.py").read_text()\n')
            workspaces = []

            def execute(_sandbox, check, workspace, *args, **kwargs):
                workspaces.append(Path(workspace))
                texts = [path.read_text() for path in Path(workspace).glob('test_*.py')]
                hostile = any('assert False' in text for text in texts)
                value_one = 'VALUE = 1' in Path(workspace, 'value.py').read_text()
                failed = hostile or not value_one
                return {'name': check['name'], 'argv': check['argv'],
                        'exit_code': 1 if failed else 0,
                        'stdout': '' if failed else 'Ran 1 test\n',
                        'stderr': 'AssertionError' if failed else '', 'error': None}

            store = Store(root / 'state')
            self.addCleanup(store.close)
            config = merge(DEFAULTS, {'validation': {'controls': [control]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                result = evaluator.Evaluator(store).evaluate(
                    source, candidate, [check], root / 'evaluation', config, changed_files=[])
            # Under the candidate's own harness this benign fixture is always
            # "detected"; the frozen baseline harness must report it as missed
            # and therefore refuse to claim control sensitivity.
            self.assertFalse(result['controls']['controls'][0]['detected'])
            self.assertEqual(result['controls']['decision'], 'insufficient_evidence')
            self.assertEqual(result['decision'], 'insufficient_evidence')
            control_targets = [path for path in workspaces if path.parent.name.startswith('controls')]
            self.assertTrue(control_targets)
            for target in control_targets:
                self.assertFalse((target / 'test_forged.py').exists())
                self.assertEqual((target / 'test_value.py').read_text(),
                                 'from value import VALUE\nassert VALUE == 1\n')

    def test_controls_pass_protected_and_hidden_to_sandbox(self):
        """sandbox.check must receive non-empty protected/hidden in control runs."""
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            (source / 'test_value.py').write_text('assert True\n')
            check = {'name': 'criterion', 'argv': ['python3', 'test_value.py']}
            control = {'id': 'c', 'criterion': 'x', 'source': 'host:fixture',
                       'files': [{'path': 'value.py', 'content': 'VALUE = 2\n'}]}
            seen = []
            def execute(_sandbox, check, workspace, cancel=None, protected=(), hidden=(), **kwargs):
                seen.append((Path(workspace).name, list(protected), list(hidden)))
                value_one = 'VALUE = 1' in Path(workspace, 'value.py').read_text()
                return {'name': check['name'], 'argv': check['argv'],
                        'exit_code': 0 if value_one else 1,
                        'stdout': 'Ran 1 test\n' if value_one else '',
                        'stderr': '' if value_one else 'AssertionError',
                        'error': None}
            store = Store(root / 'state')
            self.addCleanup(store.close)
            store.execute("INSERT INTO projects(id, path, created) VALUES (?,?,?)",
                          ('p1', str(root / 'other-project'), 0.0))
            config = merge(DEFAULTS, {'validation': {'controls': [control]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                evaluator.Evaluator(store).evaluate(
                    source, source, [check], root / 'evaluation', config, changed_files=[])
            control_calls = [row for row in seen if row[0].isdigit() or row[0] == 'positive']
            self.assertTrue(control_calls)
            for _name, protected, hidden in control_calls:
                self.assertTrue(protected, 'protected must cover harness/fixture paths')
                self.assertTrue(hidden, 'hidden must cover other project paths')

    def test_controls_fixture_over_protected_path_mounts_once(self):
        """A fixture replacing a frozen harness file must not mount the path twice."""
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'value.py').write_text('VALUE = 1\n')
            # test_value.py is a harness file the freeze protects; the fixture
            # overwrites it, so a naive append would mount the destination twice
            # and Docker would refuse the control container outright.
            (source / 'test_value.py').write_text('assert True\n')
            check = {'name': 'criterion', 'argv': ['python3', 'test_value.py']}
            control = {'id': 'c', 'criterion': 'x', 'source': 'host:fixture',
                       'files': [{'path': 'test_value.py', 'content': 'assert False\n'}]}
            seen = []
            def execute(_sandbox, check, workspace, cancel=None, protected=(), hidden=(), **kwargs):
                resolved = [str(Path(item).resolve()) for item in protected]
                seen.append(resolved)
                return {'name': check['name'], 'argv': check['argv'],
                        'exit_code': 0, 'stdout': 'Ran 1 test\n', 'stderr': '',
                        'error': None}
            store = Store(root / 'state')
            self.addCleanup(store.close)
            config = merge(DEFAULTS, {'validation': {'controls': [control]}})
            with patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), patch(
                    'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                evaluator.Evaluator(store).evaluate(
                    source, source, [check], root / 'evaluation', config, changed_files=[])
            self.assertTrue(seen)
            for resolved in seen:
                self.assertEqual(len(resolved), len(set(resolved)),
                                 'protected must not mount the same destination twice')

    def test_required_test_identity_cannot_be_satisfied_by_unrelated_tests(self):
        required = ['target.test_regression']
        unrelated = [{'test_identities': {'state': 'reported', 'executed': ['other.test_pass'], 'skipped': []}}]
        self.assertFalse(evaluator.verify_test_identities(unrelated, required)['valid'])
        matching = [{'test_identities': {'state': 'reported', 'executed': required, 'skipped': []}}]
        self.assertTrue(evaluator.verify_test_identities(matching, required)['valid'])

    def test_unittest_verbose_identities_match_selector_form(self):
        text = 'test_ok (test_ok.T.test_ok) ... ok\n'
        rows = [{'test_identities': evaluator.test_identities(text)}]
        self.assertIn('test_ok.T.test_ok', rows[0]['test_identities']['executed'])
        self.assertTrue(evaluator.verify_test_identities(rows, ['test_ok.T.test_ok'])['valid'])
        # The old display form stays accepted for contracts written before the fix.
        self.assertTrue(evaluator.verify_test_identities(rows, ['test_ok (test_ok.T.test_ok)'])['valid'])
        # Abbreviations are not fuzzy-matched, and unrelated IDs stay missing.
        mixed = evaluator.verify_test_identities(rows, ['test_ok.T.test_ok', 'other.T.test_missing'])
        self.assertFalse(evaluator.verify_test_identities(rows, ['T.test_ok'])['valid'])
        self.assertEqual(['other.T.test_missing'], mixed['missing'])
        pytest_rows = [{'test_identities': evaluator.test_identities('tests/test_x.py::test_a PASSED\n')}]
        self.assertTrue(evaluator.verify_test_identities(pytest_rows, ['tests/test_x.py::test_a'])['valid'])

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

    def test_stage_artifacts_belong_only_to_the_check_that_wrote_them(self):
        """Checks share a workspace; a stale report must not be credited to a later check."""
        import hashlib
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        junit = '<testsuite tests="1"><testcase classname="m" name="t"/></testsuite>'
        lcov = 'TN:\nSF:app.py\n'
        checks = [{'name': 'unit', 'argv': ['python3', '-m', 'unittest']},
                  {'name': 'lint', 'argv': ['python3', '-m', 'compileall']}]

        def execute(_sandbox, check, workspace, *args, **kwargs):
            if check['name'] == 'unit':
                (Path(workspace) / 'junit.xml').write_text(junit)
                (Path(workspace) / 'coverage').mkdir()
                (Path(workspace) / 'coverage' / 'lcov.info').write_text(lcov)
            return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                    'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            store = Store(root / 'state')
            self.addCleanup(store.close)
            runner = evaluator.Evaluator(store)
            config = merge(DEFAULTS, {})
            with patch('adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
                standard = runner._standard_stage(
                    'candidate', source, source, source, checks, root / 'evaluation', config,
                    None, None, None, [])['rows']
                accepted = runner._acceptance_stage(
                    'acceptance_candidate', source, source, {'checks': checks, 'spec': {'files': []}},
                    root / 'acceptance', config, None, None, [])['rows']
            for rows in (standard, accepted):
                self.assertEqual([row['name'] for row in rows], ['unit', 'lint'])
                first = rows[0]['artifacts']
                self.assertEqual(first['junit.xml']['state'], 'collected')
                self.assertEqual(first['junit.xml']['sha256'], hashlib.sha256(junit.encode()).hexdigest())
                self.assertEqual(first['junit.xml']['bytes'], len(junit))
                self.assertEqual(first['coverage/lcov.info']['state'], 'collected')
                self.assertEqual(first['coverage/lcov.info']['content'], lcov)
                self.assertEqual(rows[1]['artifacts']['junit.xml']['state'], 'missing')
                self.assertEqual(rows[1]['artifacts']['coverage/lcov.info']['state'], 'missing')

    def test_dependency_graph_closes_over_dependents_and_unknown_falls_back(self):
        checks = [{'name': 'a'}, {'name': 'b'}, {'name': 'all'}]
        graph = {'a': {'paths': ['packages/a/'], 'depends_on': [], 'checks': ['a']},
                 'b': {'paths': ['packages/b/'], 'depends_on': ['a'], 'checks': ['b']}}
        self.assertEqual([c['name'] for c in evaluator.affected_checks(['packages/a/x'], checks, graph)], ['a', 'b'])
        self.assertEqual([c['name'] for c in evaluator.affected_checks(['packages/b/x'], checks, graph)], ['a', 'b'])
        self.assertEqual(evaluator.affected_checks(['unknown/x'], checks, graph), checks)
        self.assertEqual(evaluator.affected_checks(['packages/a/x'], checks, {'b': graph['b']}), checks)

    def test_check_identity_normalization(self):
        self.assertEqual(evaluator.check_identity({'name': 'a', 'argv': ['cmd', 'arg']}), ('a', ('cmd', 'arg'), '.'))
        self.assertEqual(evaluator.check_identity({'name': 'b', 'argv': ('cmd',), 'cwd': 'pkg'}), ('b', ('cmd',), 'pkg'))
        self.assertEqual(evaluator.check_identity({'name': 'c'}), ('c', (), '.'))
        self.assertIsNone(evaluator.check_identity(None))
        self.assertIsNone(evaluator.check_identity('string'))

    def scheduling_fixtures(self, root, graph):
        source = root / 'source'
        source.mkdir()
        for package in ('pkg_a', 'pkg_b', 'pkg_c'):
            (source / package).mkdir()
            (source / package / 'value.py').write_text('VALUE = 1\n')
            (source / package / 'check.py').write_text('print("ok")\n')
        checks = [{'name': name, 'argv': ['python3', package + '/check.py']}
                  for name, package in [('check_a', 'pkg_a'), ('check_b', 'pkg_b'), ('check_c', 'pkg_c')]]
        acceptance = {'hash': 'h', 'change_kind': 'refactor', 'criteria': ['behavior remains preserved'],
                      'checks': [{'name': 'ac', 'argv': ['python3', '-c', 'print(9)']}],
                      'spec': {'files': [], 'checks': [{'name': 'ac', 'criteria': [0]}]},
                      'contract': {'check_dependencies': graph}}
        return source, checks, acceptance

    def run_scheduled_evaluation(self, root, source, checks, acceptance, changed_files, scheduling=True,
                                 detected=True, evaluation='evaluation'):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        detected_check = {'name': 'detected', 'argv': ['python3', '-c', 'print(3)']} if detected else None
        config = merge(DEFAULTS, {'validation': {'affected_check_scheduling': scheduling}})
        acceptance_baseline = {'decision': 'pass', 'execution_environment': None,
                               'rows': [{'name': 'ac', 'argv': ['python3', '-c', 'print(9)'], 'passed': True,
                                         'exit_code': 0, 'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}]}
        calls = []
        def execute(_sandbox, check, workspace, *args, **kwargs):
            calls.append((Path(workspace).name, check['name']))
            return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                    'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}
        store = Store(root / ('state-' + evaluation))
        self.addCleanup(store.close)
        with patch('adaptive_rsi.evaluator.detect', return_value={'checks': [detected_check] if detected_check else []}), patch(
                'adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=execute):
            result = evaluator.Evaluator(store).evaluate(
                source, source, checks, root / evaluation, config, changed_files=changed_files,
                acceptance=acceptance, acceptance_baseline=acceptance_baseline)
        return result, calls

    def test_affected_check_scheduling_runs_graph_closure_and_keeps_full_surface(self):
        graph = {'pa': {'paths': ['pkg_a/'], 'depends_on': [], 'checks': ['check_a']},
                 'pb': {'paths': ['pkg_b/'], 'depends_on': ['pa'], 'checks': ['check_b']},
                 'pc': {'paths': ['pkg_c/'], 'depends_on': [], 'checks': ['check_c']}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, checks, acceptance = self.scheduling_fixtures(root, graph)
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, ['pkg_a/value.py'])
        # 跨包闭包：pkg_a 变更触发自身与依赖方 pkg_b 的检查，无关 pkg_c 留给全量阶段。
        self.assertEqual([name for stage, name in calls if stage == 'candidate'], ['check_a', 'check_b'])
        self.assertEqual([name for stage, name in calls if stage == 'candidate_full'], ['check_c', 'detected'])
        self.assertEqual([row['name'] for row in result['checks']['candidate']], ['check_a', 'check_b'])
        self.assertEqual([row['name'] for row in result['checks']['candidate_full']],
                         ['check_a', 'check_b', 'check_c', 'detected'])
        self.assertFalse(result['candidate_full_reused'])
        self.assertEqual(result['check_dependency_analysis']['scheduled'], ['check_a', 'check_b'])
        self.assertEqual(result['decision'], 'pass')

    def test_affected_check_scheduling_runs_prerequisite_closure_in_candidate_stage(self):
        graph = {'pa': {'paths': ['pkg_a/'], 'depends_on': [], 'checks': ['check_a']},
                 'pb': {'paths': ['pkg_b/'], 'depends_on': ['pa'], 'checks': ['check_b']},
                 'pc': {'paths': ['pkg_c/'], 'depends_on': [], 'checks': ['check_c']}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, checks, acceptance = self.scheduling_fixtures(root, graph)
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, ['pkg_b/value.py'])
        # 前置闭包：只改 pkg_b 时，前置包 pkg_a 也在同一 candidate workspace 里先执行。
        self.assertEqual([name for stage, name in calls if stage == 'candidate'], ['check_a', 'check_b'])
        self.assertEqual([name for stage, name in calls if stage == 'candidate_full'], ['check_c', 'detected'])
        self.assertEqual([row['name'] for row in result['checks']['candidate']], ['check_a', 'check_b'])
        self.assertEqual([row['name'] for row in result['checks']['candidate_full']],
                         ['check_a', 'check_b', 'check_c', 'detected'])
        self.assertFalse(result['candidate_full_reused'])
        self.assertEqual(result['check_dependency_analysis']['scheduled'], ['check_a', 'check_b'])
        self.assertEqual(result['decision'], 'pass')

    def test_scheduling_fallback_runs_complete_configured_surface(self):
        graph = {'pa': {'paths': ['pkg_a/'], 'depends_on': [], 'checks': ['check_a']},
                 'pb': {'paths': ['pkg_b/'], 'depends_on': ['pa'], 'checks': ['check_b']},
                 'pc': {'paths': ['pkg_c/'], 'depends_on': [], 'checks': ['check_c']}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, checks, acceptance = self.scheduling_fixtures(root, graph)
            # 未知路径：回退完整集合，candidate 阶段执行全部配置检查。
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, ['docs/readme.md'], detected=False)
            self.assertEqual([name for stage, name in calls if stage == 'candidate'],
                             ['check_a', 'check_b', 'check_c'])
            self.assertEqual([stage for stage, _ in calls if stage == 'candidate_full'], [])
            self.assertTrue(result['candidate_full_reused'])
            self.assertIsNone(result['check_dependency_analysis']['scheduled'])
            self.assertEqual(result['decision'], 'pass')
            calls.clear()
            # 空 diff 同样回退。
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, [], detected=False, evaluation='evaluation-empty')
            self.assertEqual([name for stage, name in calls if stage == 'candidate'],
                             ['check_a', 'check_b', 'check_c'])
            self.assertTrue(result['candidate_full_reused'])
            calls.clear()
            # 无效 graph（未知依赖）回退。
            invalid = {'pb': {'paths': ['pkg_b/'], 'depends_on': ['missing'], 'checks': ['check_b']}}
            acceptance = {**acceptance, 'contract': {'check_dependencies': invalid}}
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, ['pkg_b/value.py'], detected=False,
                evaluation='evaluation-invalid')
            self.assertEqual([name for stage, name in calls if stage == 'candidate'],
                             ['check_a', 'check_b', 'check_c'])
            self.assertTrue(result['candidate_full_reused'])
            self.assertIsNone(result['check_dependency_analysis']['scheduled'])

    def test_scheduling_requires_opt_in_and_type_checked_config(self):
        graph = {'pa': {'paths': ['pkg_a/'], 'depends_on': [], 'checks': ['check_a']},
                 'pb': {'paths': ['pkg_b/'], 'depends_on': ['pa'], 'checks': ['check_b']},
                 'pc': {'paths': ['pkg_c/'], 'depends_on': [], 'checks': ['check_c']}}
        from adaptive_rsi.config import DEFAULTS, merge, validate
        self.assertIs(validate(merge(DEFAULTS, {}))['validation']['affected_check_scheduling'], False)
        with self.assertRaises(ValueError):
            validate(merge(DEFAULTS, {'validation': {'affected_check_scheduling': 'yes'}}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, checks, acceptance = self.scheduling_fixtures(root, graph)
            result, calls = self.run_scheduled_evaluation(
                root, source, checks, acceptance, ['pkg_a/value.py'], scheduling=False, detected=False)
            # 默认关闭：调度不改变既有行为，配置检查全部照常执行。
            self.assertEqual([name for stage, name in calls if stage == 'candidate'],
                             ['check_a', 'check_b', 'check_c'])
            self.assertTrue(result['candidate_full_reused'])
            self.assertIsNone(result['check_dependency_analysis']['scheduled'])

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

    def test_baseline_cache_write_failure_keeps_evaluation_result(self):
        # 缓存写失败（满盘、连接容量耗尽、只读状态目录）不能让已跑完检查的
        # 评估整体作废；失败必须写进报告，不能静默丢掉加速记录。
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        identity = {'build': 'build', 'backend': 'docker', 'image_id': 'sha256:' + 'b' * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            checks = [{'name': 'criterion', 'argv': ['python3', 'app.py']}]

            def execute(_sandbox, check, workspace, *args, **kwargs):
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n', 'stderr': '', 'error': None}

            def failing(_sandbox, check, workspace, *args, **kwargs):
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 1,
                        'stdout': '', 'stderr': 'AssertionError', 'error': None}

            def evaluate(store, name, side_effect):
                runner = evaluator.Evaluator(store)
                config = merge(DEFAULTS, {})
                with patch.object(runner, '_execution_environment', return_value=(config, identity)), \
                        patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), \
                        patch('adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=side_effect):
                    result = runner.evaluate(source, source, checks, root / name, config,
                                             cache_scope='project', cache_base='base', changed_files=[])
                return result, json.loads((root / name / 'report.json').read_text())

            healthy = Store(root / 'state-healthy')
            self.addCleanup(healthy.close)
            baseline_result, _report = evaluate(healthy, 'evaluation-healthy', execute)
            self.assertEqual(baseline_result['decision'], 'pass')
            self.assertNotIn('cache_write', baseline_result)

            broken = Store(root / 'state-broken')
            self.addCleanup(broken.close)
            broken.execute(
                "CREATE TRIGGER block_baseline_cache_write BEFORE INSERT ON settings "
                "WHEN NEW.key LIKE 'evaluation-baseline-entry:%' "
                "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END")
            result, report = evaluate(broken, 'evaluation-broken', execute)
            # 写失败不改变结论：检查结果与无故障时一致，报告照常落盘。
            self.assertEqual(result['decision'], 'pass')
            self.assertEqual([row['name'] for row in result['checks']['candidate_full']],
                             [row['name'] for row in baseline_result['checks']['candidate_full']])
            self.assertEqual(result['cache_write']['state'], 'failed')
            self.assertIn('database or disk is full', result['cache_write']['reason'])
            self.assertEqual(report['cache_write'], result['cache_write'])
            self.assertEqual(report['decision'], 'pass')

            # 门禁不放宽：缓存写失败时，失败的检查仍然得到 fail 并留下报告。
            failed_result, failed_report = evaluate(broken, 'evaluation-broken-fail', failing)
            self.assertEqual(failed_result['decision'], 'fail')
            self.assertEqual(failed_result['cache_write']['state'], 'failed')
            self.assertEqual(failed_report['decision'], 'fail')

    def test_passing_check_stderr_mentioning_missing_executable_keeps_baseline_cache(self):
        # Go 的 os/exec 在断言缺少某工具时会把 "executable file not found"
        # 打进 stderr，但检查本身 exit 0 通过。通过行不能一票否决整段
        # baseline 缓存，否则每次评估都要重跑一遍基线。
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store
        from unittest.mock import patch
        identity = {'build': 'build', 'backend': 'docker', 'image_id': 'sha256:' + 'b' * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'app.py').write_text('VALUE = 1\n')
            checks = [{'name': 'criterion', 'argv': ['go', 'test', './...']}]
            calls = []

            def execute(_sandbox, check, workspace, *args, **kwargs):
                calls.append(Path(workspace).name)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 0,
                        'stdout': 'Ran 1 test\n',
                        'stderr': 'exec: "missing-tool": executable file not found in $PATH\n',
                        'error': None}

            def failing(_sandbox, check, workspace, *args, **kwargs):
                calls.append(Path(workspace).name)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': 1, 'stdout': '',
                        'stderr': 'exec: "missing-tool": executable file not found in $PATH\n',
                        'error': None}

            def evaluate(store, name, side_effect):
                runner = evaluator.Evaluator(store)
                config = merge(DEFAULTS, {})
                with patch.object(runner, '_execution_environment', return_value=(config, identity)), \
                        patch('adaptive_rsi.evaluator.detect', return_value={'checks': []}), \
                        patch('adaptive_rsi.evaluator.Sandbox.check', autospec=True, side_effect=side_effect):
                    return runner.evaluate(source, source, checks, root / name, config,
                                           cache_scope='project', cache_base='base', changed_files=[])

            store = Store(root / 'state')
            self.addCleanup(store.close)
            first = evaluate(store, 'first', execute)
            self.assertEqual(first['decision'], 'pass')
            self.assertEqual(calls, ['baseline', 'candidate'])
            second = evaluate(store, 'second', execute)
            self.assertEqual(second['decision'], 'pass')
            # 基线已复用：第二轮只跑候选阶段。
            self.assertEqual(calls, ['baseline', 'candidate', 'candidate'])

            # 反例：未通过的行命中不可用模式时仍不写缓存，避免把坏环境当基线复用。
            failed_store = Store(root / 'state-failed')
            self.addCleanup(failed_store.close)
            calls.clear()
            failed = evaluate(failed_store, 'failed', failing)
            # 候选检查不可用仍按证据不足处理，不放宽判定。
            self.assertEqual(failed['decision'], 'insufficient_evidence')
            self.assertEqual(calls, ['baseline', 'candidate'])
            self.assertEqual(failed_store.query(
                "SELECT key FROM settings WHERE key LIKE 'evaluation-baseline-entry:%'"), [])

    def test_go_checks_require_test_as_immediate_subcommand(self):
        # 形如 go build test / go vet ./test 的 argv 把 test 当包参数，不得生成
        # go build test -run ... 这类畸形隔离命令（_run 只适用于 go test 子命令）。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for argv in [['go', 'build', 'test'], ['go', 'vet', './test']]:
                check = {'name': 'go', 'cwd': '.', 'argv': argv}
                self.assertIsNone(workflow._go_test_options(argv))
                self.assertIsNone(
                    evaluator._isolated_test_check([check], root, 'pkg/x_test.go', ('TestX',)),
                    argv)
            argv = ['go', 'test', '-tags', 'ci', './...']
            self.assertIsNotNone(workflow._go_test_options(argv))
            isolated = evaluator._isolated_test_check(
                [{'name': 'go', 'cwd': '.', 'argv': argv}], root, 'pkg/x_test.go', ('TestX',))
            self.assertIsNotNone(isolated)
            self.assertEqual(isolated['argv'], ['go', 'test', '-tags', 'ci', '-run', '^(TestX)$', './pkg'])


if __name__ == '__main__':
    unittest.main()
