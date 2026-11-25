"""Local generation integration: real evaluator, Store, Git and runtime retry."""
import json
import subprocess
import sys
import threading
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


class GenerationFixture(unittest.TestCase):
    base_branch = 'main'
    initial_automatic = False
    reapprove_before_generation = False
    prepare_generation = True

    def setUp(self):
        replay_tests.ReplayIntegrationTest.setUp(self)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.runtime.close())
        git(self.repo, 'remote', 'add', 'origin', 'https://github.com/example/project.git')
        self.runtime.configure({'github': {'repository': 'example/project'},
                                'delivery': {'base_branch': self.base_branch,
                                             'auto_merge': getattr(self, 'automatic_merge', False),
                                             'mode': 'pr' if self.initial_automatic else 'local'}}, self.project_id)
        self.config = self.runtime.config(self.project_id)
        self.runtime.pause(self.project_id, False)
        task = self.runtime.goal(self.project_id, 'Verified replay')
        run = self.store.claim(self.project_id, self.config)
        managed = self.store.root / 'runs' / run['id']
        managed.parent.mkdir(exist_ok=True)
        self.run_dir = self.run_dir.rename(managed)
        self.candidate = self.candidate.rename(self.run_dir / 'candidate')
        self.outcome['review'] = {'decision': 'pass'}
        self.store.execute('UPDATE runs SET base=?,path=? WHERE id=?',
                           (self.base, str(self.run_dir), run['id']))
        self.store.transition(run['id'], 'awaiting_approval', self.outcome, True)
        self.run = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.task = self.store.one('SELECT * FROM tasks WHERE id=?', (task,))
        self.store.put_setting('run-baseline:' + run['id'],
                               {'repository': 'example/project', 'branch': 'main', 'sha': self.base})
        self.approval = self.runtime.deliver(run['id'], automatic=self.initial_automatic)
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (self.approval,))
        self.project = self.runtime.project(self.project_id)
        # Obtain the real, current version-1 publish binding, not the obsolete
        # {commit,result_sha256} fixture. Direct publish leaves the run intact.
        with patch.object(delivery, 'baseline', return_value=(self.repo, self.base, 'main')), \
                patch.object(delivery, 'checked', return_value='[]'), \
                patch.object(delivery, 'github_git', side_effect=RuntimeError('fixture offline')) as push:
            with self.assertRaisesRegex(RuntimeError, 'fixture offline'):
                self.publish()
        push.assert_called_once()
        # The stub above performed no remote write; remove only its fixture intent.
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-push:' + run['id'],))
        self.before = self.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
        self.binding = self.store.setting('delivery-candidate:' + run['id'])
        if self.reapprove_before_generation:
            self.reapprove()
        if self.prepare_generation:
            self.record = self.prepare(generations._publication_root(self.runtime, self.run) / 'fixture')

    def reapprove(self):
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (self.approval,))
        self.assertEqual(self.runtime.deliver(self.run['id'], automatic=False), self.approval)
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (self.approval,))

    def prepare(self, directory):
        return generations.create_generation(self.runtime, self.project, self.run['id'], self.approval,
                self.candidate, self.repo, self.current, self.config, directory)

    def publish(self):
        return delivery.publish(self.runtime, self.project, self.task, self.run, self.candidate, self.config)

    def recover(self):
        return generations.recover_generation(self.runtime, self.run['id'], self.record['generation'], self.config)

    def restart(self):
        self.runtime.close()
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        replay_tests.install_test_identity(self)

    def push_binding(self):
        with self.store.connect() as db:
            snap = generations._snapshot(db, self.run['id'], self.approval)
        return generations._push_binding(self.record, snap)

class GenerationIntegrationTest(GenerationFixture):
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
from tests.test_delivery_replay_checks import controlled_execution_environment
runtime = Runtime(Store(sys.argv[1]), FakeAgent())
runtime.evaluator._execution_environment = controlled_execution_environment
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

    def test_restart_with_unresolved_identity_refuses_recovery(self):
        self.restart()
        with patch.object(self.runtime.evaluator, 'execution_identity', return_value=None):
            with self.assertRaisesRegex(generations.GenerationError, 'execution or dependency'):
                self.recover()

    def test_unresolved_identity_never_creates_generation(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        outcome = json.loads(self.before['result'])
        for original, current in ((None, None), (None, replay_tests.TEST_EXECUTION_IDENTITY),
                                  (replay_tests.TEST_EXECUTION_IDENTITY, None)):
            with self.subTest(original=original, current=current):
                outcome['evaluation']['execution_environment'] = original
                self.store.execute('UPDATE runs SET result=? WHERE id=?', (encode(outcome), self.run['id']))
                run = self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))
                binding = {**self.binding, 'evidence': generations.fingerprint(
                    {key: run[key] for key in ('task', 'project', 'base', 'result', 'config', 'path')})}
                self.store.put_setting('delivery-candidate:' + self.run['id'], binding)
                with patch.object(self.runtime.evaluator, 'execution_identity', return_value=current), \
                        patch.object(self.runtime.evaluator, 'evaluate') as evaluate:
                    with self.assertRaisesRegex(generations.GenerationError, 'execution or dependency'):
                        generations.create_generation(self.runtime, self.project, self.run['id'], self.approval,
                            self.candidate, self.repo, self.current, self.config, self.root / 'none-generation')
                evaluate.assert_not_called()
                self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
                self.assertFalse((self.root / 'none-generation').exists())

    def test_staged_change_after_real_evaluation_before_registration_is_rejected(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        replay = generations.replay_on_advanced_baseline
        def stage_after_pass(*args, **kwargs):
            candidate, evidence = replay(*args, **kwargs)
            self.assertEqual(evidence['decision'], 'pass')
            self.assertEqual(evidence['replay_test_first_improvement']['decision'], 'pass')
            evaluated_tree = evidence['source']['tree']
            # Another process writes and stages T2 after the real evaluator
            # passed, before the creator can register any generation.
            child = subprocess.run([sys.executable, '-B', '-c',
                "from pathlib import Path; import subprocess, sys; "
                "p = Path(sys.argv[1]); "
                "(p / 'calculator.py').write_text('unevaluated concurrent change\\n'); "
                "subprocess.run(['git', '-C', str(p), 'add', 'calculator.py'], check=True)",
                str(candidate)], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertNotEqual(git(candidate, 'write-tree'), evaluated_tree)
            return candidate, evidence
        with patch.object(generations, 'replay_on_advanced_baseline', side_effect=stage_after_pass):
            with self.assertRaisesRegex(generations.GenerationError, 'evaluated tree changed'):
                generations.create_generation(self.runtime, self.project, self.run['id'], self.approval,
                    self.candidate, self.repo, self.current, self.config, self.root / 'raced-generation')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))
        self.assertEqual(self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],)), self.before)

    def test_commit_patch_must_match_evaluated_evidence_before_registration(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        replay = generations.replay_on_advanced_baseline
        def wrong_patch(*args, **kwargs):
            candidate, evidence = replay(*args, **kwargs)
            self.assertEqual(evidence['decision'], 'pass')
            evidence['source']['patch_hash'] = 'incorrect'
            return candidate, evidence
        with patch.object(generations, 'replay_on_advanced_baseline', side_effect=wrong_patch):
            with self.assertRaisesRegex(generations.GenerationError, 'commit patch differs'):
                generations.create_generation(self.runtime, self.project, self.run['id'], self.approval,
                    self.candidate, self.repo, self.current, self.config, self.root / 'wrong-patch-generation')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))

    def test_existing_generation_requires_explicit_matching_evaluated_tree(self):
        # Even a matching legacy patch hash cannot prove which tree was evaluated.
        # Missing evidence must be rebuilt by fresh preparation, never backfilled.
        self.restart()
        for tree, patch_hash, error in (
                (None, self.record['patch_hash'], 'evaluated tree is missing; prepare a new generation'),
                (None, 'unbound', 'evaluated tree is missing; prepare a new generation'),
                ('0' * 40, self.record['patch_hash'], 'evaluation evidence'),
                (self.record['tree'], self.record['patch_hash'], None)):
            with self.subTest(tree=tree, patch_hash=patch_hash):
                record = json.loads(encode(self.record))
                if tree is None:
                    record['evaluation']['source'].pop('tree')
                else:
                    record['evaluation']['source']['tree'] = tree
                record['evaluation']['source']['patch_hash'] = patch_hash
                record['evidence_hash'] = generations.fingerprint(record['evaluation'])
                path = Path(record['directory']) / 'replay-evidence.json'
                path.write_text(encode(record['evaluation']))
                self.store.put_setting('delivery-generation:' + self.run['id'], record)
                if error:
                    with self.assertRaisesRegex(generations.GenerationError, error):
                        self.recover()
                else:
                    self.assertEqual(self.recover(), record)

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

    def test_runtime_retry_refuses_changed_generation_baseline_and_keeps_original_evidence(self):
        self.reapprove()
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.approval,))
        self.restart()
        job = self.store.claim(self.project_id, self.config)
        self.assertEqual(job['task'], self.approval)
        with patch.object(delivery, 'checked', return_value='[]') as remote, \
                patch.object(delivery, 'baseline', return_value=(self.repo, self.base, 'main')), \
                patch.object(delivery, 'github_git') as push:
            self.runtime.process(job)
        self.assertEqual(remote.call_count, 1)  # Read-only PR inventory.
        push.assert_not_called()
        original = self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))
        self.assertEqual(original, self.before)
        failed = self.store.one('SELECT * FROM runs WHERE id=?', (job['id'],))
        self.assertEqual(failed['state'], 'blocked')
        self.assertIn('Generation target or baseline changed', failed['result'])
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


class GenerationLockTest(GenerationFixture):
    def stop_during_dependency_validation(self, operation):
        entered, release = threading.Event(), threading.Event()
        errors = []
        calls = 0
        active = False
        environment = generations._environment
        digest = generations.sealed_dependency_digest
        target = 2 if operation == 'create' else 1
        key = 'delivery-generation:' + self.run['id']
        if operation == 'create':
            self.store.execute('DELETE FROM settings WHERE key=?', (key,))

        def environment_check(*args, **kwargs):
            nonlocal calls, active
            calls += 1
            active = calls == target
            try:
                return environment(*args, **kwargs)
            finally:
                active = False

        def dependency_check(*args, **kwargs):
            result = digest(*args, **kwargs)
            if active:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Stop thread did not release dependency validation')
            return result

        def stopper():
            try:
                if not entered.wait(5):
                    raise AssertionError('Dependency validation was not reached')
                # Purely in-memory cancellation: no persisted stop or DB writer.
                self.runtime.stop.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()

        thread = threading.Thread(target=stopper)
        try:
            with patch.object(generations, '_environment', side_effect=environment_check), \
                    patch.object(generations, 'sealed_dependency_digest', side_effect=dependency_check), \
                    patch.object(generations, 'replay_on_advanced_baseline', return_value=(
                        Path(self.record['directory']) / 'candidate', self.record['evaluation'])):
                thread.start()
                action = (lambda: self.prepare(Path(self.record['directory']))) if operation == 'create' else self.recover
                with self.assertRaisesRegex(generations.GenerationError, 'stopped before'):
                    action()
        finally:
            release.set()
            thread.join(6)
            self.runtime.stop.clear()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())
        self.assertEqual(calls, target)
        self.assertIsNone(self.store.setting('run-stop:' + self.run['id']))
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))
        if operation == 'create':
            self.assertIsNone(self.store.setting(key))
        else:
            self.assertEqual(self.store.setting(key), self.record)
            self.assertEqual(self.recover(), self.record)

    def test_create_memory_stop_during_dependency_validation_never_registers(self):
        self.stop_during_dependency_validation('create')

    def test_recover_memory_stop_during_dependency_validation_never_succeeds(self):
        self.stop_during_dependency_validation('recover')

    def concurrent_validation(self, operation, hook, change):
        """One writer must commit while the real slow check remains paused."""
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        errors = []
        calls = 0
        active = False
        paused = False
        environment = generations._environment
        owner = self.runtime.evaluator if hook == 'identity' else generations
        name = 'execution_identity' if hook == 'identity' else 'sealed_dependency_digest'
        slow = getattr(owner, name)
        target = 2 if operation == 'create' else 1
        key = 'delivery-generation:' + self.run['id']
        if operation == 'create':
            self.store.execute('DELETE FROM settings WHERE key=?', (key,))

        def environment_check(*args, **kwargs):
            nonlocal calls, active
            calls += 1
            active = calls == target
            try:
                return environment(*args, **kwargs)
            finally:
                active = False

        def slow_check(*args, **kwargs):
            nonlocal paused
            result = slow(*args, **kwargs)
            if active and not paused:
                paused = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Writer did not release the slow check')
                self.assertTrue(committed.is_set(), 'Writer must commit before slow check resumes')
            return result

        def writer():
            try:
                if not entered.wait(5):
                    raise AssertionError('Slow check was not reached')
                with self.store.connect() as db:
                    db.execute('PRAGMA busy_timeout=250')
                    db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                               ('unrelated-project-setting', encode({'saved': True})))
                    if change == 'authorization':
                        db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
                    elif change == 'global':
                        raw = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
                        # Same effective config, different raw global input. This
                        # must be rejected even though runtime.config still agrees.
                        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('config',?)",
                                   ((raw[0] if raw else '{}') + ' ',))
                    elif change == 'project':
                        db.execute("UPDATE projects SET config=config || ' ' WHERE id=?", (self.project_id,))
                    elif change == 'generation':
                        if operation == 'create':
                            db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (key, encode(self.record)))
                        else:
                            db.execute("UPDATE settings SET value=value || ' ' WHERE key=?", (key,))
                    elif change == 'push':
                        db.execute('INSERT INTO settings(key,value) VALUES (?,?)',
                                   ('delivery-push:' + self.run['id'], encode(self.push_binding())))
                committed.set()
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()

        thread = threading.Thread(target=writer)
        try:
            # Reuse an already genuinely evaluated replay to keep the lock
            # regression light; only the replay operation is stubbed here.
            with patch.object(generations, '_environment', side_effect=environment_check), \
                    patch.object(owner, name, side_effect=slow_check), \
                    patch.object(generations, 'replay_on_advanced_baseline', return_value=(
                        Path(self.record['directory']) / 'candidate', self.record['evaluation'])):
                thread.start()
                action = (lambda: self.prepare(Path(self.record['directory']))) if operation == 'create' else self.recover
                if change == 'unrelated':
                    self.assertEqual(action()['tree'], self.record['tree'])
                else:
                    with self.assertRaises(generations.GenerationError):
                        action()
        finally:
            release.set()
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())
        self.assertTrue(committed.is_set())
        self.assertEqual(self.store.setting('unrelated-project-setting'), {'saved': True})
        if operation == 'create' and change not in ('unrelated', 'generation'):
            self.assertIsNone(self.store.setting(key))

    def validation_cases(self, operation):
        with self.store.connect() as db:
            original_global = generations._raw_setting(db, 'config')
        for hook in ('identity', 'dependencies'):
            for change in ('unrelated', 'authorization', 'global', 'project', 'generation', 'push'):
                with self.subTest(operation=operation, hook=hook, change=change):
                    try:
                        self.concurrent_validation(operation, hook, change)
                    finally:
                        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (self.approval,))
                        self.store.execute('UPDATE projects SET config=? WHERE id=?',
                                           (self.project['config'], self.project_id))
                        if original_global is None:
                            self.store.execute("DELETE FROM settings WHERE key='config'")
                        else:
                            self.store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('config',?)",
                                               original_global)
                        self.store.put_setting('delivery-generation:' + self.run['id'], self.record)
                        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-push:' + self.run['id'],))

    def test_create_retains_final_original_and_replay_tree_checks(self):
        candidate = Path(self.record['directory']) / 'candidate'
        environment = generations._environment
        for target, expected in ((self.candidate, 'Original approved candidate changed'),
                                 (candidate, 'evaluated tree changed')):
            with self.subTest(target=target):
                self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
                calls = 0

                def change_after_final_environment(*args, **kwargs):
                    nonlocal calls
                    result = environment(*args, **kwargs)
                    calls += 1
                    if calls == 2:
                        (target / 'calculator.py').write_text('late unverified tree\n')
                        git(target, 'add', 'calculator.py')
                    return result

                try:
                    with patch.object(generations, '_environment', side_effect=change_after_final_environment), \
                            patch.object(generations, 'replay_on_advanced_baseline',
                                         return_value=(candidate, self.record['evaluation'])):
                        with self.assertRaisesRegex(generations.GenerationError, expected):
                            self.prepare(Path(self.record['directory']))
                    self.assertEqual(calls, 2)
                    self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
                finally:
                    tree = self.binding['tree'] if target == self.candidate else self.record['tree']
                    git(target, 'read-tree', tree)
                    git(target, 'checkout-index', '-a', '-f')

    def test_create_slow_validation_allows_writes_and_rejects_changed_inputs(self):
        self.validation_cases('create')

    def test_recover_slow_validation_allows_writes_and_rejects_changed_inputs(self):
        self.validation_cases('recover')


class DefaultBranchGenerationTest(GenerationFixture):
    base_branch = ''

    def test_default_empty_branch_prepares_and_recovers_sealed_main(self):
        self.assertEqual(self.config['delivery']['base_branch'], '')
        self.assertEqual(self.store.setting('run-baseline:' + self.run['id'])['branch'], 'main')
        self.restart()
        self.assertEqual(self.recover(), self.record)
        with patch.object(delivery, 'checked') as remote, patch.object(delivery, 'github_git') as push:
            with self.assertRaisesRegex(RuntimeError, 'Candidate version or verification changed'):
                self.publish()
        remote.assert_not_called()
        push.assert_not_called()
        self.reapprove()
        with patch.object(delivery, 'checked', return_value='[]') as remote, \
                patch.object(delivery, 'baseline', return_value=(self.repo, self.current, 'release')), \
                patch.object(delivery, 'github_git') as push:
            with self.assertRaisesRegex(RuntimeError, 'Generation target or baseline changed'):
                self.publish()
        self.assertEqual(remote.call_count, 1)  # Reading is allowed; writing is refused.
        push.assert_not_called()

    def test_explicit_different_branch_refused_before_evaluation(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        self.runtime.configure({'delivery': {'base_branch': 'release'}}, self.project_id)
        self.config = self.runtime.config(self.project_id)
        self.store.execute('UPDATE runs SET config=? WHERE id=?', (encode(self.config), self.run['id']))
        run = self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))
        self.store.put_setting('delivery-candidate:' + self.run['id'],
            {**self.binding, 'evidence': generations.fingerprint(
                {key: run[key] for key in ('task', 'project', 'base', 'result', 'config', 'path')})})
        with patch.object(self.runtime.evaluator, 'evaluate') as evaluate:
            with self.assertRaisesRegex(generations.GenerationError, 'publication binding'):
                self.prepare(self.root / 'different-branch')
        evaluate.assert_not_called()
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertFalse((self.root / 'different-branch').exists())

    def test_empty_or_changed_sealed_target_is_rejected(self):
        key = 'run-baseline:' + self.run['id']
        baseline = self.store.setting(key)
        invalid = [{**baseline, field: ''} for field in baseline]
        invalid += [{**baseline, 'repository': 'other/repo'}, {**baseline, 'sha': 'a' * 40},
                    {**baseline, 'branch': 'release'}]
        for value in invalid:
            with self.subTest(value=value):
                self.store.put_setting(key, value)
                with self.assertRaises(generations.GenerationError):
                    self.recover()
        self.store.put_setting(key, baseline)
        self.assertEqual(self.recover(), self.record)


class ReapprovedGenerationTest(GenerationFixture):
    initial_automatic = True
    reapprove_before_generation = True

    def test_failed_policy_attempt_then_manual_reapproval_keeps_historical_binding(self):
        self.assertEqual(self.binding['authorization'], generations.fingerprint(
            [{'authorization': 'project_policy', 'version_bound': True}]))
        approval = self.store.one('SELECT * FROM tasks WHERE id=?', (self.approval,))
        self.assertEqual(json.loads(approval['evidence']), [{'authorization': 'per_candidate', 'version_bound': True}])
        self.assertEqual(self.store.setting('delivery-candidate:' + self.run['id']), self.binding)
        self.restart()
        self.assertEqual(self.recover(), self.record)

    def test_reapproval_seals_current_generation_and_new_baseline_requires_another_approval(self):
        self.reapprove()
        self.restart()
        approval = self.store.setting('delivery-approval:' + self.run['id'])
        self.assertEqual(approval['version']['candidate']['generation'], self.record['generation'])
        self.assertEqual(self.recover(), self.record)
        (self.repo / 'advance.txt').write_text('advanced a second time')
        git(self.repo, 'add', 'advance.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'advance again')
        current = git(self.repo, 'rev-parse', 'HEAD')
        with patch.object(delivery, 'checked', return_value='[]'), \
                patch.object(delivery, 'baseline', return_value=(self.repo, current, 'main')), \
                patch.object(delivery, 'github_git') as push:
            with self.assertRaisesRegex(RuntimeError, 'approve the new candidate'):
                self.publish()
            push.assert_not_called()
        replacement = self.store.setting('delivery-generation:' + self.run['id'])
        self.assertNotEqual(replacement['generation'], self.record['generation'])
        self.assertEqual(replacement['parent'], current)
        history = self.store.query('SELECT value FROM settings WHERE key LIKE ?',
                                   ('delivery-generation-history:' + self.run['id'] + ':%',))
        self.assertEqual([json.loads(row['value']) for row in history], [self.record])
        self.assertTrue(Path(self.record['directory']).is_dir())
        self.assertEqual(self.store.setting('delivery-approval:' + self.run['id']), approval)

    def test_new_approval_after_push_intent_requires_reconciliation(self):
        self.store.put_setting('delivery-push:' + self.run['id'], self.push_binding())
        self.store.execute("UPDATE tasks SET state='blocked',fingerprint='retired-publish' WHERE id=?", (self.approval,))
        new_approval = self.runtime.deliver(self.run['id'], automatic=False)
        self.assertNotEqual(new_approval, self.approval)
        self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (new_approval,))
        self.restart()
        with self.assertRaisesRegex(generations.GenerationError, 'authorization changed'):
            self.recover()
        with patch.object(delivery, 'checked') as remote, patch.object(delivery, 'github_git') as push:
            with self.assertRaises(generations.GenerationError):
                self.publish()
        remote.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.store.setting('delivery-generation:' + self.run['id']), self.record)

    def test_reapproval_does_not_allow_revocation_or_changed_candidate(self):
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation:' + self.run['id'],))
        key = 'delivery-candidate:' + self.run['id']
        invalid = [{**self.binding, field: 'changed'} for field in
                   ('branch', 'head', 'tree', 'base', 'repository', 'evidence', 'authorization', 'approval')]
        for binding in invalid:
            with self.subTest(binding=binding):
                # Nonempty historical approval IDs are valid audit snapshots;
                # malformed IDs and digests are still rejected.
                if binding['approval'] == 'changed':
                    binding['approval'] = ''
                self.store.put_setting(key, binding)
                with patch.object(self.runtime.evaluator, 'evaluate') as evaluate:
                    with self.assertRaises(RuntimeError):
                        self.prepare(self.root / 'changed-candidate')
                evaluate.assert_not_called()
        self.store.put_setting(key, self.binding)
        for state, evidence in [('cancelled', [{'authorization': 'per_candidate'}]),
                                ('planning', [])]:
            with self.subTest(state=state, evidence=evidence):
                self.store.execute('UPDATE tasks SET state=?,evidence=? WHERE id=?',
                                   (state, encode(evidence), self.approval))
                with patch.object(self.runtime.evaluator, 'evaluate') as evaluate:
                    with self.assertRaises(generations.GenerationError):
                        self.prepare(self.root / 'revoked-candidate')
                evaluate.assert_not_called()
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))


if __name__ == '__main__':
    unittest.main()
