import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from adaptive_rsi.config import fingerprint
from adaptive_rsi.delivery_replay_checks import ReplayRefused, replay_on_advanced_baseline
from adaptive_rsi.environment import capture
from adaptive_rsi.gitops import git, snapshot
from adaptive_rsi.pipeline import _evaluation_source
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import CHECKS, FakeAgent, fixture


class ReplayIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.repo = fixture(self.root / 'repo')
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        self.project_id = self.runtime.add_project(self.repo)
        self.runtime.configure({'checks': CHECKS, 'environment': {'seed_paths': ['deps']}}, self.project_id)
        self.config = self.runtime.config(self.project_id)
        (self.repo / 'deps').mkdir()
        (self.repo / 'deps' / 'data').write_text('sealed')
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.digest = capture(self.repo, self.run_dir / 'dependencies', self.config)['sha256']
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.baseline = snapshot(self.repo, self.root / 'original-base', self.base)
        self.candidate = snapshot(self.repo, self.root / 'original-candidate', self.base)
        (self.candidate / 'calculator.py').write_text('def add(a, b):\n    return a + b\n')
        (self.candidate / 'tests/test_added.py').write_text(
            'import unittest\nfrom calculator import add\nclass Added(unittest.TestCase):\n'
            '    def test_fixed(self):\n        self.assertEqual(add(2, 3), 5)\n')
        git(self.candidate, 'add', '-A')
        self.patch = git(self.candidate, 'diff', self.base, '--binary', '--no-ext-diff', '--no-textconv')
        self.files = ['calculator.py', 'tests/test_added.py']
        self.project = {'id': self.project_id}
        self.run = {'base': self.base, 'path': str(self.run_dir)}
        self.evaluator_runtime = SimpleNamespace(evaluator=self.runtime.evaluator, is_cancelled=lambda run: False)
        result = self.runtime.evaluator.evaluate(self.baseline, self.candidate, CHECKS, self.root / 'original-eval',
                    self.config, seeds=self.run_dir / 'dependencies', cache_scope=self.project_id,
                    cache_base=self.base, environment_digest=self.digest, test_first=True,
                    test_first_kind='behavior', changed_files=self.files)
        self.assertEqual(result['decision'], 'pass', result)
        result['source'] = _evaluation_source(self.config, self.base, CHECKS, self.patch, self.digest, None, True, 'behavior')
        self.outcome = {'base': self.base, 'files': self.files, 'patch_hash': fingerprint(self.patch), 'evaluation': result}
        (self.repo / 'advance.txt').write_text('advanced')
        git(self.repo, 'add', 'advance.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--quiet', '-m', 'advance')
        self.current = git(self.repo, 'rev-parse', 'HEAD')
        self.count = 0

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def replay(self):
        self.count += 1
        return replay_on_advanced_baseline(self.evaluator_runtime, self.project, self.run, self.candidate,
                self.outcome, self.repo, self.current, self.config, self.root / ('replay' + str(self.count)))

    def test_replay_executes_real_gates_and_preserves_base(self):
        tree, result = self.replay()
        self.assertEqual(result['decision'], 'pass')
        self.assertEqual(result['replay_test_first_improvement']['decision'], 'pass')
        self.assertFalse(result['checks']['test_first_baseline'][0]['passed'])
        self.assertTrue(result['checks']['test_first_candidate'][0]['passed'])
        self.assertEqual(self.run['base'], self.base)
        self.assertEqual(json.loads((tree.parent / 'replay-evidence.json').read_text())['source']['base'], self.current)

    def test_missing_tampered_or_different_seed_digest_refuses(self):
        seed = self.run_dir / 'dependencies/deps/data'
        seed.unlink()
        with self.assertRaises(ReplayRefused):
            self.replay()
        seed.write_text('tampered')
        with self.assertRaises(ReplayRefused):
            self.replay()
        seed.write_text('sealed')
        self.outcome['evaluation']['source']['environment'] = 'different'
        with self.assertRaises(ReplayRefused):
            self.replay()

    def test_tampered_acceptance_refuses(self):
        (self.run_dir / 'acceptance.json').write_text('{}')
        with self.assertRaises(ReplayRefused):
            self.replay()

    def test_acceptance_cannot_bypass_independent_test_first_gate(self):
        from adaptive_rsi.acceptance import seal
        spec = {'files': [{'path': '.rsi-acceptance/check.py', 'content':
                           'from calculator import add\nassert add(2, 3) == 5\n'}],
                'checks': [{'name': 'addition', 'argv': ['python3', '-c', "exec(open('.rsi-acceptance/check.py').read())"],
                            'criteria': [0]}]}
        acceptance = seal(spec, ['addition works'], self.root / 'sealed-acceptance')
        acceptance.update(change_kind='behavior', contract={'kind': 'behavior'})
        (self.run_dir / 'acceptance.json').write_text(json.dumps(acceptance))
        (self.candidate / 'tests/test_added.py').write_text(
            'import unittest\nfrom pathlib import Path\nfrom calculator import add\n'
            'class Added(unittest.TestCase):\n'
            '    def test_fixed(self):\n        self.assertTrue(Path("advance.txt").exists() or add(2, 3) == 5)\n')
        git(self.candidate, 'add', '-A')
        patch = git(self.candidate, 'diff', self.base, '--binary', '--no-ext-diff', '--no-textconv')
        original = self.runtime.evaluator.evaluate(
            self.baseline, self.candidate, CHECKS, self.root / 'with-acceptance', self.config,
            seeds=self.run_dir / 'dependencies', acceptance=acceptance, cache_scope=self.project_id,
            cache_base=self.base, environment_digest=self.digest, test_first=True,
            test_first_kind='behavior', changed_files=self.files)
        self.assertEqual(original['decision'], 'pass', original)
        original['source'] = _evaluation_source(self.config, self.base, CHECKS, patch, self.digest, acceptance, True, 'behavior')
        self.assertEqual(original['source']['contract_hash'], fingerprint(acceptance['contract']))
        self.assertEqual(original['source']['criteria_hash'], fingerprint(acceptance['criteria']))
        self.outcome.update(evaluation=original, patch_hash=fingerprint(patch))
        contract_hash = original['source'].pop('contract_hash')
        with self.assertRaisesRegex(ReplayRefused, 're-evaluation required'):
            self.replay()
        original['source']['contract_hash'] = contract_hash
        tampered = dict(acceptance, contract={'kind': 'refactor'})
        (self.run_dir / 'acceptance.json').write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ReplayRefused, 'acceptance'):
            self.replay()
        (self.run_dir / 'acceptance.json').write_text(json.dumps(acceptance))
        with self.assertRaisesRegex(ReplayRefused, 'independent test-first improvement'):
            self.replay()

    def test_pipeline_saved_acceptance_and_test_first_replay(self):
        git(self.repo, 'reset', '--hard', self.base)
        task = self.runtime.goal(self.project_id, 'Fix addition')
        self.store.execute("UPDATE tasks SET fingerprint=? WHERE id=?", ('batch:replay', task))
        self.runtime.pause(self.project_id, False)
        run = self.store.claim(self.project_id, self.runtime.config(self.project_id))
        self.runtime.process(run)
        row = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        outcome = json.loads(row['result'])
        self.assertEqual(row['state'], 'awaiting_approval', outcome)
        self.assertTrue(outcome['evaluation']['source']['test_first'])
        self.assertTrue(outcome['evaluation']['source']['contract_hash'])
        self.assertTrue(outcome['evaluation']['source']['criteria_hash'])
        self.assertTrue(self.store.one("SELECT * FROM stages WHERE run=? AND stage='evaluation'", (run['id'],)))
        self.assertTrue((Path(row['path']) / 'acceptance.json').is_file())
        git(self.repo, 'reset', '--hard', self.current)
        from adaptive_rsi.environment import frozen_dependencies
        self.assertEqual(frozen_dependencies(Path(row['path']) / 'dependencies', self.config)['sha256'], outcome['evaluation']['source']['environment'])
        tree, replay = replay_on_advanced_baseline(
            self.evaluator_runtime, self.project, row, Path(row['path']) / 'candidate', outcome,
            self.repo, self.current, self.config, self.root / 'pipeline-replay')
        self.assertEqual(replay['decision'], 'pass')
        self.assertEqual(replay['replay_test_first_improvement']['decision'], 'pass')
        self.assertTrue(tree.is_dir())

    def test_committed_candidate_new_test_is_discovered(self):
        git(self.candidate, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--quiet', '-m', 'candidate')
        committed = self.runtime.evaluator.evaluate(
            self.baseline, self.candidate, CHECKS, self.root / 'committed-eval', self.config,
            seeds=self.run_dir / 'dependencies', cache_scope=self.project_id, cache_base=self.base,
            environment_digest=self.digest, test_first=True, test_first_kind='behavior', changed_files=self.files)
        self.assertEqual(committed['decision'], 'pass', committed)
        self.assertEqual(committed['improvement']['regression_test_files'], ['tests/test_added.py'])
        _, result = self.replay()
        self.assertEqual(result['replay_test_first_improvement']['regression_test_files'], ['tests/test_added.py'])


if __name__ == '__main__':
    unittest.main()
