"""Local generation integration: real evaluator, Store, Git and runtime retry."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import delivery
from adaptive_rsi import delivery_generation as generations
from adaptive_rsi.gitops import git
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests import test_delivery_replay_checks as replay_tests
from tests.test_runtime import FakeAgent


class GenerationIntegrationTest(unittest.TestCase):
    def setUp(self):
        replay_tests.ReplayIntegrationTest.setUp(self)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.runtime.close())
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/example/project.git')
        self.runtime.configure({'github': {'repository': 'example/project'},
                                'delivery': {'base_branch': 'main', 'mode': 'local'}}, self.project_id)
        self.config = self.runtime.config(self.project_id)
        self.runtime.pause(self.project_id, False)
        task = self.runtime.goal(self.project_id, 'Verified replay')
        run = self.store.claim(self.project_id, self.config)
        self.candidate = self.candidate.rename(self.run_dir / 'candidate')
        self.outcome['review'] = {'decision': 'pass'}
        self.store.execute('UPDATE runs SET base=?,path=? WHERE id=?',
                           (self.base, str(self.run_dir), run['id']))
        self.store.transition(run['id'], 'awaiting_approval', self.outcome, True)
        self.run = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.task = self.store.one('SELECT * FROM tasks WHERE id=?', (task,))
        self.store.put_setting('run-baseline:' + run['id'],
                               {'repository': 'example/project', 'branch': 'main', 'sha': self.base})
        self.approval = self.runtime.deliver(run['id'], automatic=False)
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (self.approval,))
        self.project = self.runtime.project(self.project_id)
        # Obtain the real, current version-1 publish binding, not the obsolete
        # {commit,result_sha256} fixture. Direct publish leaves the run intact.
        with patch.object(delivery, 'baseline', return_value=(self.repo, self.current, 'main')), \
                patch.object(delivery, 'checked', return_value='[]'), \
                patch.object(delivery, 'github_git') as push:
            with self.assertRaises(delivery.StaleCandidate):
                self.publish()
        push.assert_not_called()
        self.before = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.binding = self.store.setting('delivery-candidate:' + run['id'])
        self.record = generations.create_generation(self.runtime, self.project, run['id'], self.approval,
                self.candidate, self.repo, self.current, self.config, self.root / 'generation')

    def publish(self):
        return delivery.publish(self.runtime, self.project, self.task, self.run, self.candidate, self.config)

    def recover(self):
        return generations.recover_generation(self.runtime, self.run['id'], self.record['generation'], self.config)

    def restart(self):
        self.runtime.close()
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())

    def push_binding(self):
        with self.store.connect() as db:
            snap = generations._snapshot(db, self.run['id'], self.approval)
        return generations._push_binding(self.record, snap)

    def test_real_gates_and_matching_push_survive_restart_without_mutating_original(self):
        self.assertEqual(self.record['evaluation']['replay_test_first_improvement']['decision'], 'pass')
        self.assertEqual(self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],)), self.before)
        self.assertEqual(self.store.setting('run-baseline:' + self.run['id'])['sha'], self.base)
        self.assertEqual(self.store.setting('delivery-candidate:' + self.run['id']), self.binding)
        self.store.put_setting('delivery-push:' + self.run['id'], {**self.push_binding(), 'created': 123})
        self.runtime.close()
        # A separate interpreter must recover solely from persisted DB/Git/files.
        script = '''import sys
from adaptive_rsi.delivery_generation import recover_generation
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent
runtime = Runtime(Store(sys.argv[1]), FakeAgent())
try:
    print(recover_generation(runtime, sys.argv[2], sys.argv[3], runtime.config(sys.argv[4]))['commit'])
finally:
    runtime.close()
'''
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.root / 'state'),
                                self.run['id'], self.record['generation'], self.project_id],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), self.record['commit'])
        self.restart()
        self.assertEqual(self.recover(), self.record)
        self.assertEqual(self.recover()['commit'], self.record['commit'])

    def test_legacy_and_mismatched_journals_never_rebind_to_generation(self):
        good = self.push_binding()
        invalid = [{key: self.binding[key] for key in ('branch', 'head', 'base', 'repository')},
                   self.binding, {**good, 'version': True}, {**good, 'unknown': 'field'}]
        invalid.extend({**good, key: 'replaced'} for key in good if key != 'version')
        for journal in invalid:
            with self.subTest(journal=journal):
                self.store.put_setting('delivery-push:' + self.run['id'], journal)
                with self.assertRaisesRegex(generations.GenerationError, 'Push journal'):
                    self.recover()
        self.store.put_setting('delivery-push:' + self.run['id'], good)
        self.restart()
        self.assertEqual(self.recover(), self.record)

    def test_ref_replacement_with_same_tree_is_rejected_after_restart(self):
        candidate = Path(self.record['directory']) / 'candidate'
        alternate = git(candidate, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                        'commit-tree', self.record['tree'], '-p', self.current, '-m', 'replacement')
        self.assertNotEqual(alternate, self.record['commit'])
        git(candidate, 'update-ref', self.record['ref'], alternate, self.record['commit'])
        self.restart()
        with self.assertRaisesRegex(generations.GenerationError, 'ref changed'):
            self.recover()

    def test_changed_authorization_head_tree_or_environment_is_rejected(self):
        for field in ('approval', 'authorization', 'head', 'tree', 'environment'):
            with self.subTest(field=field):
                key = 'delivery-candidate:' + self.run['id']
                if field == 'environment':
                    path = self.run_dir / 'dependencies/deps/data'
                    original = path.read_text()
                    path.write_text('changed')
                else:
                    changed = {**self.binding, field: 'changed'}
                    self.store.put_setting(key, changed)
                try:
                    with self.assertRaises(generations.GenerationError):
                        self.recover()
                finally:
                    if field == 'environment':
                        path.write_text(original)
                    else:
                        self.store.put_setting(key, self.binding)
        self.assertEqual(self.recover(), self.record)

    def test_old_candidate_schema_and_changed_generation_head_tree_are_rejected(self):
        key = 'delivery-candidate:' + self.run['id']
        self.store.put_setting(key, {'commit': self.binding['head'], 'result_sha256': 'obsolete'})
        with self.assertRaises(generations.GenerationError):
            self.recover()
        self.store.put_setting(key, self.binding)
        for field in ('commit', 'tree', 'environment'):
            with self.subTest(field=field):
                self.store.put_setting('delivery-generation:' + self.run['id'],
                                       {**self.record, field: 'a' * 40})
                with self.assertRaises(RuntimeError):
                    self.recover()
        self.store.put_setting('delivery-generation:' + self.run['id'], self.record)
        self.assertEqual(self.recover(), self.record)

    def test_live_authorization_config_and_original_head_changes_are_rejected(self):
        self.store.execute('UPDATE tasks SET evidence=? WHERE id=?',
                           (encode([{'authorization': 'project_policy'}]), self.approval))
        with self.assertRaises(generations.GenerationError):
            self.recover()
        self.store.execute('UPDATE tasks SET evidence=? WHERE id=?',
                           (encode([{'authorization': 'per_candidate'}]), self.approval))
        changed = json.loads(encode(self.config))
        changed['sandbox']['memory_mb'] += 1
        with self.assertRaises(generations.GenerationError):
            generations.recover_generation(self.runtime, self.run['id'], self.record['generation'], changed)
        git(self.candidate, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--amend', '-m', 'changed original identity')
        self.restart()
        with self.assertRaises(generations.GenerationError):
            self.recover()

    def test_approval_changed_during_real_replay_never_registers_generation(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        evaluate = self.runtime.evaluator.evaluate
        def revoke(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
            return result
        with patch.object(self.runtime.evaluator, 'evaluate', side_effect=revoke):
            with self.assertRaises(generations.GenerationError):
                generations.create_generation(self.runtime, self.project, self.run['id'], self.approval,
                    self.candidate, self.repo, self.current, self.config, self.root / 'revoked-generation')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertEqual(self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],)), self.before)

    def test_runtime_retry_refuses_generation_remote_write_and_keeps_original_evidence(self):
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.approval,))
        self.restart()
        job = self.store.claim(self.project_id, self.config)
        self.assertEqual(job['task'], self.approval)
        with patch.object(delivery, 'checked') as remote, patch.object(delivery, 'github_git') as push:
            self.runtime.process(job)
        remote.assert_not_called()
        push.assert_not_called()
        original = self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))
        self.assertEqual(original, self.before)
        failed = self.store.one('SELECT * FROM runs WHERE id=?', (job['id'],))
        self.assertEqual(failed['state'], 'blocked')
        self.assertIn('generation publication is not connected', failed['result'])
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))

    def test_replaced_generation_tree_and_evidence_are_rejected(self):
        candidate = Path(self.record['directory']) / 'candidate'
        (candidate / 'calculator.py').write_text('tampered\n')
        git(candidate, 'add', 'calculator.py')
        with self.assertRaisesRegex(generations.GenerationError, 'workspace changed'):
            self.recover()
        git(candidate, 'read-tree', self.record['tree'])
        git(candidate, 'checkout-index', '-a', '-f')
        evidence = Path(self.record['directory']) / 'replay-evidence.json'
        evidence.write_text('{}')
        with self.assertRaisesRegex(generations.GenerationError, 'evaluation evidence'):
            self.recover()


if __name__ == '__main__':
    unittest.main()
