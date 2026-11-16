"""Issue #480: preserve test-first evidence after candidate commits."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import evaluator
from adaptive_rsi.gitops import git


class CommittedTestsDiscoveryTest(unittest.TestCase):
    def test_finds_test_added_in_committed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'base'
            candidate = root / 'candidate'
            baseline.mkdir()
            git(baseline, 'init', '-q')
            (baseline / 'app.py').write_text('pass\n')
            git(baseline, 'add', '.')
            git(baseline, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                'commit', '-qm', 'base')
            git(root, 'clone', '-q', str(baseline), str(candidate))
            (candidate / 'tests').mkdir()
            (candidate / 'tests' / 'test_new.py').write_text('def test_new(): pass\n')
            git(candidate, 'add', '.')
            git(candidate, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                'commit', '-qm', 'candidate')
            self.assertEqual(evaluator._added_test_files(candidate, baseline), ['tests/test_new.py'])


class CommittedTestFirstGateTest(unittest.TestCase):
    def test_committed_regression_runs_overlay_and_improvement_gate(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, candidate = root / 'baseline', root / 'candidate'
            baseline.mkdir()
            git(baseline, 'init', '-q')
            (baseline / 'app.py').write_text('VALUE = 1\n')
            (baseline / 'tests').mkdir()
            (baseline / 'tests/__init__.py').write_text('')
            (baseline / 'tests/test_old.py').write_text(
                'import unittest\nclass Old(unittest.TestCase):\n'
                '    def test_old(self): self.assertTrue(True)\n')
            git(baseline, 'add', '.')
            git(baseline, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                'commit', '-qm', 'base')
            git(root, 'clone', '-q', str(baseline), str(candidate))
            (candidate / 'app.py').write_text('VALUE = 2\n')
            (candidate / 'tests/test_new.py').write_text(
                'import unittest\nfrom app import VALUE\nclass New(unittest.TestCase):\n'
                '    def test_fixed(self): self.assertEqual(VALUE, 2)\n')
            git(candidate, 'add', '.')
            git(candidate, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                'commit', '-qm', 'candidate')
            self.assertEqual(evaluator._changed_files(candidate), [])
            check = {'name': 'tests', 'argv': [sys.executable, '-B', '-m', 'unittest', 'discover']}
            config = merge(DEFAULTS, {'limits': {'concurrency': 1}})
            store = Store(root / 'state')
            self.addCleanup(store.close)

            # Execute only this tiny fixture's Python tests. Sandbox transport
            # is replaced; this does not attest macOS/Docker isolation.
            def execute(_sandbox, check, workspace, *args, **kwargs):
                result = subprocess.run(check['argv'], cwd=Path(workspace) / check.get('cwd', '.'),
                                        capture_output=True, text=True, timeout=10)
                return {'name': check['name'], 'argv': check['argv'], 'exit_code': result.returncode,
                        'stdout': result.stdout, 'stderr': result.stderr, 'error': None}

            with patch.object(evaluator, 'detect', return_value={'checks': [check]}), \
                    patch.object(evaluator.Sandbox, 'check', autospec=True, side_effect=execute) as sandbox:
                result = evaluator.Evaluator(store).evaluate(
                    baseline, candidate, [check], root / 'evaluation', config,
                    test_first=True, test_first_kind='behavior', changed_files=[])
            self.assertEqual(result['decision'], 'pass', result)
            self.assertEqual(result['improvement']['decision'], 'pass', result)
            self.assertFalse(result['checks']['test_first_baseline'][0]['passed'])
            self.assertTrue(result['checks']['test_first_candidate'][0]['passed'])
            self.assertGreaterEqual(sandbox.call_count, 4)


class MixedCandidateEvidenceTest(unittest.TestCase):
    def setUp(self):
        from adaptive_rsi.config import DEFAULTS, merge
        from adaptive_rsi.store import Store

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / 'baseline'
        self.candidate = self.root / 'candidate'
        self.baseline.mkdir()
        git(self.baseline, 'init', '-q')
        (self.baseline / 'app.py').write_text('VALUE = 1\n')
        (self.baseline / 'tests').mkdir()
        (self.baseline / 'tests' / 'test_old.py').write_text('assert True\n')
        git(self.baseline, 'add', '.')
        git(self.baseline, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
            'commit', '-qm', 'base')
        git(self.root, 'clone', '-q', str(self.baseline), str(self.candidate))
        (self.candidate / 'tests' / 'test_old.py').write_text('assert False\n')
        (self.candidate / 'tests' / 'test_new.py').write_text('def test_new(): pass\n')
        git(self.candidate, 'add', 'tests')
        git(self.candidate, '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
            'commit', '-qm', 'candidate tests')
        (self.candidate / 'app.py').write_text('VALUE = 2\n')
        store = Store(self.root / 'state')
        self.addCleanup(store.close)
        self.evaluator = evaluator.Evaluator(store)
        self.config = merge(DEFAULTS, {})
        self.check = {'name': 'tests', 'argv': ['python3', '-m', 'unittest', 'discover']}

    def evaluate(self, changed_files, name):
        with patch.object(evaluator, 'detect', return_value={'checks': [self.check]}), \
                patch.object(evaluator, '_isolated_test_check', return_value=None), \
                patch.object(evaluator.Sandbox, 'check') as sandbox:
            result = self.evaluator.evaluate(
                self.baseline, self.candidate, [self.check], self.root / name, self.config,
                test_first=True, changed_files=changed_files)
        sandbox.assert_not_called()
        return result

    def test_unreadable_added_test_diff_blocks_checks(self):
        with patch.object(evaluator, '_added_test_files', return_value=None):
            result = self.evaluate(['app.py'], 'missing-added-tests')
        self.assertEqual(result['decision'], 'insufficient_evidence')
        self.assertEqual(result['reason'], 'Added-test evidence is unavailable')

    def test_unreadable_worktree_diff_blocks_checks_despite_committed_history(self):
        with patch.object(evaluator, '_changed_files', return_value=None):
            result = self.evaluate(None, 'missing-worktree')
        self.assertEqual(result['decision'], 'insufficient_evidence')
        self.assertIn('Changed-file evidence is unavailable', result['reason'])

    def test_committed_harness_edit_is_seen_with_uncommitted_changes(self):
        for name, changed in [('discovered', None), ('provided', ['app.py', 'tests/test_new.py'])]:
            with self.subTest(name=name):
                result = self.evaluate(changed, name)
                self.assertEqual(result['decision'], 'insufficient_evidence')
                self.assertEqual(result['reason'], 'Test-first proof cannot modify the existing test harness')

    def test_missing_committed_history_refuses_nonempty_worktree_delta(self):
        for name, changed in [('discovered', None), ('provided', ['app.py'])]:
            with self.subTest(name=name), patch.object(evaluator, '_committed_changes', return_value=False):
                result = self.evaluate(changed, name)
                self.assertEqual(result['decision'], 'insufficient_evidence')
                self.assertIn('Changed-file evidence is unavailable', result['reason'])
