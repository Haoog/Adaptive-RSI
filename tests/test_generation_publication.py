"""Real runtime/Store/Git publication; only GitHub API and execution identity stubbed.

Push/fetch use a local bare repository. No GitHub or Docker validation is implied.
"""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import delivery
from adaptive_rsi import delivery_generation as generations
from adaptive_rsi.gitops import git
from adaptive_rsi.maintenance import retention_hold, _remove_run_artifacts
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_delivery_generation import GenerationFixture
from tests.test_delivery_replay_checks import controlled_execution_environment
from tests.test_runtime import FakeAgent


class LocalRemote:
    """Persist mock GitHub receipts across interpreters, with real Git transport."""
    def __init__(self, root):
        self.root = Path(root)
        self.bare = self.root / 'remote.git'
        self.api = self.root / 'github.json'
        self.lose_push = False
        self.lose_pr = False
        self.before_create = None
        self.before_push = None
        self.after_push = None
        self.pushes = 0

    def github_git(self, root, *args, **kwargs):
        args = [str(self.bare) if a.startswith('https://github.com/') else a for a in args]
        if args[0] == 'push':
            self.pushes += 1
            if self.before_push:
                self.before_push()
        result = git(root, *args, **kwargs)
        if args[0] == 'push' and self.after_push:
            self.after_push()
        if args[0] == 'push' and self.lose_push:
            self.lose_push = False
            raise RuntimeError('push response lost after remote accepted commit')
        return result

    def checked(self, args, **kwargs):
        if args[0] == 'git':
            from adaptive_rsi.process import checked
            return checked(args, **kwargs)
        if args[:3] == ['gh', 'repo', 'view']:
            return 'main'
        state = json.loads(self.api.read_text())
        if args[:3] == ['gh', 'pr', 'list']:
            return json.dumps([state['pr']] if state['pr'] else [])
        if args[:3] == ['gh', 'pr', 'create']:
            if self.before_create:
                self.before_create()
            if state['pr']:
                raise AssertionError('duplicate PR create')
            branch = args[args.index('--head') + 1]
            base = args[args.index('--base') + 1]
            state['creates'] += 1
            state['pr'] = {'url': 'https://github.com/example/project/pull/1', 'state': 'OPEN',
                'headRefName': branch, 'headRefOid': git(self.bare, 'rev-parse', 'refs/heads/' + branch),
                'baseRefName': base, 'baseRefOid': git(self.bare, 'rev-parse', 'refs/heads/' + base),
                'headRepository': {'nameWithOwner': 'example/project'},
                'headRepositoryOwner': {'login': 'example'}, 'mergeCommit': None,
                'statusCheckRollup': [], 'comments': [], 'reviews': []}
            self.api.write_text(json.dumps(state))
            if self.lose_pr:
                self.lose_pr = False
                raise RuntimeError('PR response lost after creation')
            return state['pr']['url']
        raise AssertionError('unexpected network call: ' + repr(args))

    def pr_view(self, repo, url):
        return json.loads(self.api.read_text())['pr']

    def install(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(delivery, 'github_git', side_effect=self.github_git))
        stack.enter_context(patch.object(delivery, 'checked', side_effect=self.checked))
        stack.enter_context(patch.object(delivery, 'pr_view', side_effect=self.pr_view))
        return stack


class PublicationFixture(GenerationFixture):
    prepare_generation = False

    def setUp(self):
        super().setUp()
        # Runtime must produce the first immutable binding itself on this path.
        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-candidate:' + self.run['id'],))
        self.remote = LocalRemote(self.root)
        subprocess.run(['git', 'clone', '--quiet', '--bare', str(self.repo), str(self.remote.bare)],
                       check=True, capture_output=True, timeout=10)
        git(self.remote.bare, 'update-ref', 'refs/heads/main', self.current)
        self.remote.api.write_text(json.dumps({'pr': None, 'creates': 0}))

    def process_approval(self):
        job = self.run_publication_job()
        if 'Replay prepared on a new baseline' in job['result']:
            self.assert_needs_reapproval(job)
            self.runtime.deliver(self.run['id'], automatic=False)
            job = self.run_publication_job()
        return job

    def run_publication_job(self):
        self.store.execute("UPDATE tasks SET state='queued',available=0 WHERE id=?", (self.approval,))
        job = self.store.claim(self.project_id, self.config)
        self.assertEqual(job['task'], self.approval)
        with self.remote.install():
            self.runtime.process(job)
        return self.store.one('SELECT * FROM runs WHERE id=?', (job['id'],))

    def assert_needs_reapproval(self, job):
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertIn('approve the new candidate before publishing', job['result'])
        self.assertEqual(self.original(), self.before)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.task['id'],))['state'],
                         'awaiting_approval')
        record = self.record()
        self.assertEqual(record['evaluation']['decision'], 'pass')
        receipt = self.store.setting('delivery-approval:' + self.run['id'])
        self.assertNotEqual(receipt['version']['candidate']['generation'], record['generation'])
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))
        self.assertEqual(self.remote.pushes, 0)
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 0)

    def original(self):
        return self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))

    def record(self):
        return self.store.setting('delivery-generation:' + self.run['id'])

    def assert_original_evidence(self):
        original = self.original()
        for key in ('base', 'config', 'path'):
            self.assertEqual(original[key], self.before[key])
        previous = json.loads(self.before['result'])
        actual = json.loads(original['result'])
        for key in previous:
            self.assertEqual(actual[key], previous[key], key)
        self.assertEqual(git(self.candidate, 'rev-parse', 'HEAD'), self.binding['head'])
        self.assertEqual(self.store.setting('run-baseline:' + self.run['id'])['sha'], self.base)

    def restart(self):
        self.runtime.close()
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, FakeAgent())
        self.runtime.evaluator._execution_environment = controlled_execution_environment


class RuntimePublicationTest(PublicationFixture):
    def test_first_advance_real_runtime_pushes_replayed_commit_and_keeps_original(self):
        self.assert_needs_reapproval(self.run_publication_job())
        self.runtime.deliver(self.run['id'], automatic=False)
        job = self.run_publication_job()
        self.assertEqual(job['state'], 'completed', job['result'])
        record = self.record()
        push = self.store.setting('delivery-push:' + self.run['id'])
        self.assertEqual(push['version'], 2)
        self.assertEqual(push['head'], record['commit'])
        self.assertEqual(git(self.remote.bare, 'rev-parse', 'refs/heads/' + push['branch']), record['commit'])
        self.assertNotEqual(record['commit'], self.binding['head'])
        self.assertEqual(record['parent'], self.current)
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.task['id'],))['state'], 'pr_open')
        self.assert_original_evidence()
        evidence = record['evaluation']
        self.assertFalse(evidence['checks']['test_first_baseline'][0]['passed'])
        self.assertTrue(evidence['checks']['test_first_candidate'][0]['passed'])
        self.assertEqual(evidence['replay_test_first_improvement']['decision'], 'pass')
        self.assertEqual((Path(record['directory']) / 'dependencies/deps/data').read_text(), 'sealed')
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.restart()
        job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)

    def test_unregistered_partial_workspace_is_never_adopted(self):
        partial = generations._publication_root(self.runtime, self.run) / 'abandoned'
        partial.mkdir()
        (partial / 'replay-evidence.json').write_text('unverified')
        job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        self.assertNotEqual(Path(self.record()['directory']), partial)
        self.assertEqual((partial / 'replay-evidence.json').read_text(), 'unverified')


class PublicationRecoveryTest(PublicationFixture):
    def test_push_accepted_response_lost_then_reopened_store_reuses_commit(self):
        self.remote.lose_push = True
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        first = self.record()
        intent = self.store.setting('delivery-push:' + self.run['id'])
        self.assertEqual(git(self.remote.bare, 'rev-parse', 'refs/heads/' + intent['branch']), first['commit'])
        self.assertEqual(self.original(), self.before)
        self.restart()
        job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        self.assertEqual(self.record(), first)
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assert_original_evidence()

    def test_pr_accepted_response_lost_then_new_interpreter_recovers_without_push_or_create(self):
        self.remote.lose_pr = True
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        first = self.record()
        intent = self.store.setting('delivery-push:' + self.run['id'])
        self.runtime.close()
        script = '''import sys
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from tests.test_runtime import FakeAgent
from tests.test_generation_publication import LocalRemote
from tests.test_delivery_replay_checks import controlled_execution_environment
root, project, approval = sys.argv[1:]
remote = LocalRemote(root)
runtime = Runtime(Store(remote.root / 'state'), FakeAgent())
runtime.evaluator._execution_environment = controlled_execution_environment
try:
    runtime.store.execute("UPDATE tasks SET state='queued',available=0 WHERE id=?", (approval,))
    job = runtime.store.claim(project, runtime.config(project))
    with remote.install():
        runtime.process(job)
    row = runtime.store.one('SELECT state,result FROM runs WHERE id=?', (job['id'],))
    assert row['state'] == 'completed', row
    assert remote.pushes == 0
finally:
    runtime.close()
'''
        child = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), self.project_id, self.approval],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.restart()
        self.assertEqual(self.record(), first)
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assert_original_evidence()

    def test_superseded_receipt_uses_generation_base_without_workspace(self):
        self.remote.lose_pr = True
        self.process_approval()
        record = self.record()
        self.store.execute("UPDATE runs SET state='superseded' WHERE id=?", (self.run['id'],))
        with self.remote.install():
            # Recovery reads only the journal, not live candidate files or approval.
            self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
            delivery._recover_superseded_receipt(self.runtime, self.project, self.original(), self.config, 1000)
        result = json.loads(self.original()['result'])
        self.assertTrue(result['close_pending'])
        self.assertTrue(result['delivery_recovery_checked'])
        self.assertEqual(self.store.setting('delivery:' + self.run['id'])['base'], record['parent'])


class PublicationRefusalTest(PublicationFixture):
    def prepare_failed_push(self):
        self.remote.lose_push = True
        self.assertEqual(self.process_approval()['state'], 'blocked')
        return self.record(), self.store.setting('delivery-push:' + self.run['id'])

    def test_revocation_second_advance_and_tree_tamper_refuse_without_new_write(self):
        record, intent = self.prepare_failed_push()
        before_pushes = self.remote.pushes
        for kind in ('revoked', 'advanced', 'tree', 'dependencies', 'target'):
            with self.subTest(kind=kind):
                self.restart()
                candidate = Path(record['directory']) / 'candidate'
                if kind == 'revoked':
                    self.store.execute("UPDATE tasks SET evidence='[]' WHERE id=?", (self.approval,))
                elif kind == 'advanced':
                    (self.repo / 'advance-again.txt').write_text('second advance')
                    git(self.repo, 'add', 'advance-again.txt')
                    git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                        'commit', '--quiet', '-m', 'advance again')
                    git(self.remote.bare, 'fetch', '--quiet', str(self.repo), 'HEAD:refs/heads/main')
                elif kind == 'tree':
                    (candidate / 'calculator.py').write_text('unverified\n')
                    git(candidate, 'add', 'calculator.py')
                elif kind == 'dependencies':
                    (self.run_dir / 'dependencies/deps/data').write_text('unverified')
                elif kind == 'target':
                    self.runtime.configure({'delivery': {'base_branch': 'release'}}, self.project_id)
                job = self.process_approval()
                self.assertNotEqual(job['state'], 'completed', job['result'])
                self.assertEqual(self.original()['state'], 'awaiting_approval')
                self.assertEqual(self.remote.pushes, before_pushes)
                self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)
                self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 0)
                # Restore fixture inputs between independent refusals.
                self.store.execute('UPDATE tasks SET evidence=? WHERE id=?',
                                   (encode([{'authorization': 'per_candidate'}]), self.approval))
                git(self.remote.bare, 'update-ref', 'refs/heads/main', self.current)
                git(candidate, 'read-tree', record['tree'])
                git(candidate, 'checkout-index', '-a', '-f')
                (self.run_dir / 'dependencies/deps/data').write_text('sealed')
                self.runtime.configure({'delivery': {'base_branch': self.base_branch}}, self.project_id)

    def test_authorization_revoked_after_push_blocks_pr_and_preserves_intent(self):
        def intent_already_durable():
            record = self.record()
            intent = self.store.setting('delivery-push:' + self.run['id'])
            self.assertEqual(intent['head'], record['commit'])
            self.assertEqual(intent['tree'], record['tree'])
            self.assertEqual(intent['base'], record['parent'])
            self.assertEqual(intent['approval'], self.approval)
        self.remote.before_push = intent_already_durable
        self.remote.after_push = lambda: self.store.execute(
            "UPDATE tasks SET evidence='[]' WHERE id=?", (self.approval,))
        job = self.process_approval()
        self.assertEqual(job['state'], 'interrupted', job['result'])
        self.assertEqual(self.remote.pushes, 1)
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 0)
        intent = self.store.setting('delivery-push:' + self.run['id'])
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (self.approval,))
        self.runtime.deliver(self.run['id'], automatic=False)
        self.assertIsNotNone(self.record())
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)

    def test_remote_branch_changed_after_push_blocks_pr(self):
        def replace_remote_head():
            intent = self.store.setting('delivery-push:' + self.run['id'])
            git(self.remote.bare, 'update-ref', 'refs/heads/' + intent['branch'], self.current)
        self.remote.after_push = replace_remote_head
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertIn('Remote generation branch differs', job['result'])
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 0)
        self.assertEqual(self.original(), self.before)
        self.assertIsNotNone(self.store.setting('delivery-push:' + self.run['id']))

    def test_old_push_journal_blocks_replay_and_retains_original(self):
        journal = {'branch': self.binding['branch'], 'head': self.binding['head'],
                   'base': self.base, 'repository': 'example/project'}
        self.store.put_setting('delivery-push:' + self.run['id'], journal)
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertIsNone(self.record())
        self.assertEqual(self.original(), self.before)
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), journal)
        self.assertEqual(self.remote.pushes, 0)

    def test_retention_preserves_generation_and_uncertain_journal(self):
        record, intent = self.prepare_failed_push()
        self.assertEqual(retention_hold(self.store, self.run['id'], self.project_id),
                         'delivery-generation:' + self.run['id'])
        self.assertFalse(_remove_run_artifacts(self.store, self.run['id'], self.project_id, self.run_dir))
        self.assertTrue((Path(record['directory']) / 'candidate').is_dir())
        self.assertTrue(self.candidate.is_dir())
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)


class PublicationReapprovalTest(PublicationFixture):
    def test_explicit_reapproval_rebuilds_unpublished_stale_generation(self):
        self.assert_needs_reapproval(self.run_publication_job())
        first = self.record()
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))
        (self.repo / 'again.txt').write_text('next baseline')
        git(self.repo, 'add', 'again.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--quiet', '-m', 'next baseline')
        current = git(self.repo, 'rev-parse', 'HEAD')
        git(self.remote.bare, 'fetch', '--quiet', str(self.repo), 'HEAD:refs/heads/main')
        self.runtime.deliver(self.run['id'], automatic=False)
        self.assertEqual(self.record(), first)
        self.assert_needs_reapproval(self.run_publication_job())
        self.assertTrue(Path(first['directory']).is_dir())
        self.runtime.deliver(self.run['id'], automatic=False)
        self.assertEqual(self.run_publication_job()['state'], 'completed')
        second = self.record()
        self.assertNotEqual(first['generation'], second['generation'])
        self.assertEqual(second['parent'], current)
        self.assert_original_evidence()
        history = self.store.query('SELECT value FROM settings WHERE key LIKE ?',
                                   ('delivery-generation-history:' + self.run['id'] + ':%',))
        self.assertEqual([json.loads(row['value']) for row in history], [first])


class AcceptancePublicationTest(PublicationFixture):
    def setUp(self):
        super().setUp()
        from adaptive_rsi.acceptance import seal
        from adaptive_rsi.pipeline import _evaluation_source
        from tests.test_runtime import CHECKS
        spec = {'files': [{'path': '.rsi-acceptance/check.py', 'content':
                          'from calculator import add\nassert add(2, 3) == 5\n'}],
                'checks': [{'name': 'addition', 'argv': ['python3', '-c',
                            "exec(open('.rsi-acceptance/check.py').read())"], 'criteria': [0]}]}
        acceptance = seal(spec, ['addition works'], self.root / 'sealed-acceptance')
        acceptance.update(change_kind='behavior', contract={'kind': 'behavior'})
        (self.run_dir / 'acceptance.json').write_text(json.dumps(acceptance))
        result = self.runtime.evaluator.evaluate(self.baseline, self.candidate, CHECKS,
            self.root / 'original-acceptance-eval', self.config, seeds=self.run_dir / 'dependencies',
            cache_scope=self.project_id, cache_base=self.base, environment_digest=self.digest,
            acceptance=acceptance, test_first=True, test_first_kind='behavior', changed_files=self.files)
        self.assertEqual(result['decision'], 'pass', result)
        result['source'] = _evaluation_source(self.config, self.base, CHECKS, self.patch,
                                              self.digest, acceptance, True, 'behavior')
        outcome = json.loads(self.run['result'])
        outcome['evaluation'] = result
        self.store.execute('UPDATE runs SET result=? WHERE id=?', (encode(outcome), self.run['id']))
        self.run = self.original()
        self.before = self.run
        self.reapprove()
        self.acceptance = acceptance

    def test_runtime_replays_sealed_acceptance_and_test_first(self):
        job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        evaluation = self.record()['evaluation']
        self.assertEqual(evaluation['acceptance_hash'], self.acceptance['hash'])
        self.assertEqual(evaluation['contract'], self.acceptance['contract'])
        self.assertTrue(evaluation['checks']['acceptance_candidate'][0]['passed'])
        self.assertEqual(evaluation['replay_test_first_improvement']['decision'], 'pass')
        self.assert_original_evidence()

    def test_changed_sealed_acceptance_never_pushes(self):
        (self.run_dir / 'acceptance.json').write_text('{}')
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertEqual(self.remote.pushes, 0)
        self.assertIsNone(self.record())
        original = self.original()
        self.assertEqual(original['state'], 'superseded')
        outcome = json.loads(original['result'])
        self.assertIn('sealed acceptance evidence', outcome.pop('reason'))
        self.assertEqual(outcome, json.loads(self.before['result']))
        self.assertEqual({**original, 'state': self.before['state'], 'result': self.before['result']}, self.before)


class PolicyPublicationTest(RuntimePublicationTest):
    initial_automatic = True
    base_branch = ''

    def test_policy_cannot_approve_a_prepared_generation(self):
        self.assert_needs_reapproval(self.run_publication_job())
        record = self.record()
        self.assertEqual(self.runtime.deliver(self.run['id'], automatic=True), self.approval)
        pending = self.store.one('SELECT state FROM tasks WHERE id=?', (self.approval,))
        self.assertEqual(pending['state'], 'blocked')
        unapproved = self.run_publication_job()
        self.assertEqual(unapproved['state'], 'blocked', unapproved['result'])
        self.assertEqual(self.record(), record)
        self.assertIsNone(self.store.setting('delivery-push:' + self.run['id']))
        self.assertEqual(self.remote.pushes, 0)
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 0)


class ReapprovedModePublicationTest(RuntimePublicationTest):
    initial_automatic = True

    def setUp(self):
        super().setUp()
        self.runtime.configure({'delivery': {'mode': 'local', 'auto_merge': False}}, self.project_id)
        self.store.execute("UPDATE tasks SET state='blocked' WHERE id=?", (self.approval,))
        self.runtime.deliver(self.run['id'], automatic=False)
        self.config = self.runtime.config(self.project_id)


class PublicationCreateIdentityTest(PublicationFixture):
    def test_head_advances_between_final_verify_and_create_is_never_published(self):
        def advance():
            intent = self.store.setting('delivery-push:' + self.run['id'])
            commit = git(self.remote.bare, '-c', 'user.name=Other', '-c',
                         'user.email=other@example.com', 'commit-tree', self.record()['tree'],
                         '-p', intent['head'], '-m', 'concurrent head')
            git(self.remote.bare, 'update-ref', 'refs/heads/' + intent['branch'], commit)
        self.remote.before_create = advance
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        record = self.record()
        intent = self.store.setting('delivery-push:' + self.run['id'])
        receipt = self.store.setting('delivery-generation-pr:' + self.run['id'])
        self.assertNotEqual(receipt['observation']['headRefOid'], record['commit'])
        self.assertEqual(receipt['url'], json.loads(self.remote.api.read_text())['pr']['url'])
        self.assertEqual(self.original(), self.before)
        self.assertIsNone(self.store.setting('delivery:' + self.run['id']))
        self.restart()
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertEqual(self.record(), record)
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), intent)
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assertEqual(self.original(), self.before)

    def test_lost_create_response_with_competing_head_is_rejected_on_retry(self):
        def advance():
            intent = self.store.setting('delivery-push:' + self.run['id'])
            git(self.remote.bare, 'update-ref', 'refs/heads/' + intent['branch'], self.current)
        self.remote.before_create = advance
        self.remote.lose_pr = True
        self.assertEqual(self.process_approval()['state'], 'blocked')
        record = self.record()
        journal = self.store.setting('delivery-push:' + self.run['id'])
        self.restart()
        job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assertEqual(self.record(), record)
        self.assertEqual(self.store.setting('delivery-push:' + self.run['id']), journal)
        self.assertEqual(self.original(), self.before)
        self.assertIsNone(self.store.setting('delivery:' + self.run['id']))
        receipt = self.store.setting('delivery-generation-pr:' + self.run['id'])
        self.assertEqual(receipt['observation']['headRefOid'], self.current)

    def test_created_pr_repo_source_target_and_state_must_match(self):
        original_view = self.remote.pr_view
        for field, replacement in (('url', 'https://github.com/example/other/pull/1'),
                                   ('headRepository', {'nameWithOwner': 'example/fork'}),
                                   ('headRepositoryOwner', {'login': 'other'}),
                                   ('baseRefName', 'release'), ('state', 'CLOSED')):
            with self.subTest(field=field):
                def changed_view(repo, url):
                    return {**original_view(repo, url), field: replacement}
                with patch.object(self.remote, 'pr_view', side_effect=changed_view):
                    job = self.process_approval()
                self.assertEqual(job['state'], 'blocked', job['result'])
                self.assertEqual(self.original(), self.before)
                self.assertIsNone(self.store.setting('delivery:' + self.run['id']))
                self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assertEqual(self.process_approval()['state'], 'completed')
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)

    def test_created_pr_identity_read_failure_retries_saved_url_with_empty_inventory(self):
        with patch.object(self.remote, 'pr_view', side_effect=RuntimeError('identity query unavailable')):
            job = self.process_approval()
        self.assertEqual(job['state'], 'blocked', job['result'])
        receipt = self.store.setting('delivery-generation-pr:' + self.run['id'])
        self.assertIn('identity query unavailable', receipt['error'])
        self.assertEqual(self.original(), self.before)
        self.assertIsNone(self.store.setting('delivery:' + self.run['id']))
        self.restart()
        checked = self.remote.checked
        def delayed_inventory(args, **kwargs):
            if args[:3] == ['gh', 'pr', 'list']:
                return '[]'
            return checked(args, **kwargs)
        with patch.object(self.remote, 'checked', side_effect=delayed_inventory):
            job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        self.assertEqual(json.loads(self.remote.api.read_text())['creates'], 1)
        self.assertEqual(self.remote.pushes, 1)
        self.assertEqual(self.original()['state'], 'pr_open')


class GenerationTrackingRepositoryTest(PublicationFixture):
    initial_automatic = True
    automatic_merge = True

    def test_real_tick_rejects_repository_switch_at_entry_and_final_admission(self):
        job = self.process_approval()
        self.assertEqual(job['state'], 'completed', job['result'])
        state = json.loads(self.remote.api.read_text())
        pr = {**state['pr'], 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
              'statusCheckRollup': [{'conclusion': 'SUCCESS'}]}
        policy = self.store.setting('delivery:' + self.run['id'])
        identity = self.store.setting('run-baseline:' + self.run['id'])
        self.assertNotEqual(identity['sha'], policy['base'])
        def switch():
            self.runtime.configure({'github': {'repository': 'example/replacement'}}, self.project_id)
            git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/example/replacement.git')
        for number, stage in enumerate(('entry', 'fetch', 'normal'), 1):
            with self.subTest(stage=stage):
                self.runtime.configure({'github': {'repository': 'example/project'}}, self.project_id)
                git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/example/project.git')
                if stage == 'entry':
                    switch()
                def baseline(*args):
                    if stage == 'fetch':
                        switch()
                    return self.repo, policy['base'], policy['branch']
                with patch.object(delivery, 'pr_view', return_value=pr) as view, \
                        patch.object(delivery, 'baseline', side_effect=baseline) as fetch, \
                        patch.object(delivery, 'checked', return_value='') as command:
                    delivery.tick(self.runtime, now=10000000000 + number * 120, project_id=self.project_id)
                if stage == 'normal':
                    command.assert_called_once_with(['gh', 'pr', 'merge', pr['url'], '--repo',
                        'example/project', '--squash', '--match-head-commit', policy['head']])
                    fetch.assert_called_once()
                else:
                    command.assert_not_called()
                    if stage == 'entry':
                        view.assert_not_called()
                    else:
                        fetch.assert_called_once()
                self.assertEqual(self.store.setting('run-baseline:' + self.run['id']), identity)
                self.assertEqual(self.store.setting('delivery:' + self.run['id']), policy)


class PublishedGenerationAdvanceTest(PublicationFixture):
    automatic_merge = True
    initial_automatic = True

    def advance_published(self):
        self.assertEqual(self.process_approval()['state'], 'completed')
        record = self.record()
        git(self.repo, 'checkout', '--quiet', 'main')
        (self.repo / 'later.txt').write_text('second baseline advance')
        git(self.repo, 'add', 'later.txt')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--quiet', '-m', 'second advance')
        current = git(self.repo, 'rev-parse', 'HEAD')
        git(self.remote.bare, 'fetch', str(self.repo), current)
        git(self.remote.bare, 'update-ref', 'refs/heads/main', current)
        api = json.loads(self.remote.api.read_text())
        api['pr']['baseRefOid'] = current
        self.remote.api.write_text(encode(api))
        return record

    def tick_local(self, now=10000):
        original_checked = self.remote.checked
        def checked(args, **kwargs):
            if args[:3] == ['gh', 'pr', 'close']:
                api = json.loads(self.remote.api.read_text())
                api['pr']['state'] = 'CLOSED'
                self.remote.api.write_text(encode(api))
                return ''
            return original_checked(args, **kwargs)
        with self.remote.install(), patch.object(delivery, 'checked', side_effect=checked):
            delivery.tick(self.runtime, now=now, project_id=self.project_id)

    def test_published_generation_second_advance_requeues_without_updating_tree(self):
        record = self.advance_published()
        self.tick_local()
        row = self.original()
        outcome = json.loads(row['result'])
        self.assertEqual(row['state'], 'superseded', outcome)
        self.assertEqual(outcome['delivery_status'], 'stale')
        self.assertFalse(outcome['close_pending'])
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (row['task'],))['state'], 'queued')
        self.assertEqual(self.record(), record)
        self.assertEqual(self.remote.pushes, 1)
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'CLOSED')
        self.tick_local(11000)
        self.assertEqual(self.original()['state'], 'superseded')

    def assert_second_observation_refused(self, mutate):
        record = self.advance_published()
        policy = self.store.setting('delivery:' + self.run['id'])
        intent = self.store.setting('delivery-push:' + self.run['id'])
        observations = []
        def view(repo, url):
            pr = self.remote.pr_view(repo, url)
            observations.append(pr['headRefOid'])
            if len(observations) >= 2:
                mutate(pr)
            return pr
        with self.remote.install(), patch.object(delivery, 'pr_view', side_effect=view), \
                patch.object(delivery, 'checked', side_effect=self.remote.checked) as command:
            delivery.tick(self.runtime, now=10000, project_id=self.project_id)
            row = self.original()
            outcome = json.loads(row['result'])
            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0], record['commit'])
            self.assertEqual(row['state'], 'superseded')
            self.assertTrue(outcome['close_pending'])
            self.assertIn('close_blocked_reason', outcome)
            self.assertNotIn('close_attempts', outcome)
            self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (row['task'],))['state'], 'blocked')
            # 退避后仍不能将不明身份当作已结束，也不能重新入队。
            delivery.tick(self.runtime, now=18000, project_id=self.project_id)
            self.assertFalse(any(call.args[0][:3] == ['gh', 'pr', 'close'] for call in command.call_args_list))
        self.assertTrue(json.loads(self.original()['result'])['close_pending'])
        self.assertEqual(self.record(), record)
        self.assertEqual(self.store.setting('delivery:' + row['id']), policy)
        self.assertEqual(self.store.setting('delivery-push:' + row['id']), intent)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE run=? AND kind='delivery_close_blocked'", (row['id'],)))
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'OPEN')

    def test_second_observation_changed_head_never_closes_or_requeues(self):
        self.assert_second_observation_refused(lambda pr: pr.update(headRefOid='f' * 40))

    def test_second_observation_all_identity_fields_fail_closed(self):
        self.advance_published()
        original = self.original()
        valid = self.remote.pr_view('example/project', json.loads(original['result'])['pr'])
        cases = [('missing ' + key, key, None) for key in
                 ('url', 'headRefOid', 'headRefName', 'baseRefName', 'headRepository', 'headRepositoryOwner')]
        cases += [('changed ' + key, key, value) for key, value in (
            ('url', 'https://github.com/example/project/pull/2'),
            ('url', 'https://github.com/other/project/pull/1'),
            ('headRefName', 'other-source'), ('baseRefName', 'release'),
            ('headRepository', {'nameWithOwner': 'example/other'}),
            ('headRepositoryOwner', {'login': 'other'}), ('state', 'UNKNOWN'))]
        for index, (label, key, value) in enumerate(cases):
            with self.subTest(label=label):
                self.store.execute("UPDATE runs SET state='pr_open',result=? WHERE id=?",
                                   (original['result'], original['id']))
                self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (original['task'],))
                changed = {**valid, key: value}
                if value is None:
                    changed.pop(key)
                with self.remote.install(), patch.object(delivery, 'pr_view', side_effect=[valid, changed]), \
                        patch.object(delivery, 'checked') as command:
                    delivery.tick(self.runtime, now=10000 + index * 8000, project_id=self.project_id)
                command.assert_not_called()
                outcome = json.loads(self.original()['result'])
                self.assertTrue(outcome['close_pending'])
                self.assertIn('close_blocked_reason', outcome)
                self.assertNotIn('close_attempts', outcome)
                self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (original['task'],))['state'], 'blocked')

    def test_second_observation_sealed_records_change_blocks_close(self):
        self.advance_published()
        original = self.original()
        prefixes = ('delivery:', 'run-baseline:', 'delivery-push:',
                    'delivery-generation:', 'delivery-candidate:')
        records = {prefix: self.store.setting(prefix + original['id']) for prefix in prefixes}
        for index, prefix in enumerate(prefixes):
            with self.subTest(prefix=prefix):
                for key, value in records.items():
                    self.store.put_setting(key + original['id'], value)
                self.store.execute("UPDATE runs SET state='pr_open',result=? WHERE id=?",
                                   (original['result'], original['id']))
                self.store.execute("UPDATE tasks SET state='pr_open' WHERE id=?", (original['task'],))
                count = 0
                changed = {**records[prefix], 'unexpected': 'concurrent edit'}
                def view(repo, url):
                    nonlocal count
                    count += 1
                    if count == 2:
                        self.store.put_setting(prefix + original['id'], changed)
                    return self.remote.pr_view(repo, url)
                with self.remote.install(), patch.object(delivery, 'pr_view', side_effect=view), \
                        patch.object(delivery, 'checked') as command:
                    delivery.tick(self.runtime, now=10000 + index * 8000, project_id=self.project_id)
                command.assert_not_called()
                self.assertEqual(self.store.setting(prefix + original['id']), changed)
                self.assertTrue(json.loads(self.original()['result'])['close_pending'])
                self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (original['task'],))['state'], 'blocked')

    def test_second_observation_live_query_failure_fails_closed_without_write_lock(self):
        # 第二次 view 开始后才模拟 origin 查询失败；查询回调能写独立连接，
        # 也证明准入没有跨 Git 查询持有 SQLite 写锁。
        origin = delivery.origin_repository
        def fail_origin(path):
            self.store.put_setting('test:close-admission-lock-probe', True)
            raise RuntimeError('origin unavailable')
        with patch.object(delivery, 'origin_repository', side_effect=origin) as probe:
            def mutate(pr):
                probe.side_effect = fail_origin
            self.assert_second_observation_refused(mutate)
        self.assertTrue(self.store.setting('test:close-admission-lock-probe'))

    def test_legacy_generation_replan_without_target_preserves_pr_and_blocks_queue(self):
        self.advance_published()
        row = self.original()
        outcome = json.loads(row['result'])
        outcome.update(generation_replan=True, replanned=True, close_pending=True)
        self.store.execute("UPDATE runs SET state='superseded',result=? WHERE id=?", (encode(outcome), row['id']))
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (row['task'],))
        with self.remote.install(), patch.object(delivery, 'checked') as command:
            delivery.tick(self.runtime, now=10000, project_id=self.project_id)
        command.assert_not_called()
        self.assertTrue(json.loads(self.original()['result'])['close_pending'])
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (row['task'],))['state'], 'blocked')
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'OPEN')

    def test_second_observation_repository_switch_never_closes(self):
        def switch(pr):
            self.runtime.configure({'github': {'repository': 'example/replacement'}}, self.project_id)
        self.assert_second_observation_refused(switch)

    def test_second_observation_global_repository_switch_never_closes(self):
        def switch(pr):
            self.runtime.configure({'github': {'repository': ''}}, self.project_id)
            self.runtime.configure({'github': {'repository': 'example/replacement'}})
        self.assert_second_observation_refused(switch)

    def test_second_observation_base_branch_switch_never_closes(self):
        def switch(pr):
            self.runtime.configure({'delivery': {'base_branch': 'release'}}, self.project_id)
        self.assert_second_observation_refused(switch)

    def test_second_observation_origin_switch_never_closes(self):
        def switch(pr):
            git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/example/replacement.git')
        self.assert_second_observation_refused(switch)

    def test_second_observation_revocation_blocks_task_until_authorized_retry(self):
        self.assert_second_observation_refused(
            lambda pr: self.runtime.configure({'delivery': {'auto_merge': False}}, self.project_id))
        self.runtime.configure({'delivery': {'auto_merge': True}}, self.project_id)
        self.tick_local(26000)
        outcome = json.loads(self.original()['result'])
        self.assertFalse(outcome['close_pending'])
        self.assertNotIn('close_blocked_reason', outcome)
        self.assertEqual(outcome['close_attempts'], 1)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.run['task'],))['state'], 'queued')

    def test_second_observation_cancellation_blocks_task(self):
        self.assert_second_observation_refused(lambda pr: self.runtime.stop.set())

    def test_second_advance_without_current_authority_does_not_close(self):
        self.advance_published()
        self.runtime.configure({'delivery': {'auto_merge': False}}, self.project_id)
        self.tick_local()
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'OPEN')

    def test_same_baseline_waits_for_ci_without_replanning(self):
        self.assertEqual(self.process_approval()['state'], 'completed')
        api = json.loads(self.remote.api.read_text())
        api['pr']['statusCheckRollup'] = [{'status': 'IN_PROGRESS', 'name': 'checks'}]
        self.remote.api.write_text(encode(api))
        self.tick_local()
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertNotIn('generation_replan', json.loads(self.original()['result']))

    def test_retry_exhaustion_blocks_task(self):
        self.advance_published()
        self.store.execute('UPDATE tasks SET attempts=? WHERE id=?',
                           (self.config['limits']['retries'] + 1, self.run['task']))
        self.tick_local()
        self.assertEqual(self.original()['state'], 'superseded')
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.run['task'],))['state'], 'blocked')


    def test_revocation_during_observation_prevents_replan_and_close(self):
        self.advance_published()
        def view(repo, url):
            self.runtime.configure({'delivery': {'auto_merge': False}}, self.project_id)
            return self.remote.pr_view(repo, url)
        with self.remote.install(), patch.object(delivery, 'pr_view', side_effect=view):
            delivery.tick(self.runtime, now=10000, project_id=self.project_id)
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'OPEN')

    def test_cancellation_during_observation_prevents_replan(self):
        self.advance_published()
        def view(repo, url):
            self.runtime.stop.set()
            return self.remote.pr_view(repo, url)
        with self.remote.install(), patch.object(delivery, 'pr_view', side_effect=view):
            delivery.tick(self.runtime, now=10000, project_id=self.project_id)
        self.assertEqual(self.original()['state'], 'pr_open')
        self.assertEqual(json.loads(self.remote.api.read_text())['pr']['state'], 'OPEN')

    def test_failed_close_is_bounded_and_revocation_prevents_retry(self):
        self.advance_published()
        with self.remote.install(), patch.object(delivery, 'checked', side_effect=RuntimeError('close rejected')):
            delivery.tick(self.runtime, now=10000, project_id=self.project_id)
        row = self.original()
        self.assertEqual(row['state'], 'superseded')
        outcome = json.loads(row['result'])
        self.assertEqual(outcome['close_attempts'], 1)
        self.runtime.configure({'delivery': {'auto_merge': False}}, self.project_id)
        with self.remote.install(), patch.object(delivery, 'checked') as command:
            delivery.tick(self.runtime, now=20000, project_id=self.project_id)
            command.assert_not_called()
        self.runtime.configure({'delivery': {'auto_merge': True}}, self.project_id)
        outcome['close_attempts'] = delivery.CLOSE_ATTEMPT_LIMIT - 1
        self.store.execute('UPDATE runs SET result=? WHERE id=?', (encode(outcome), row['id']))
        with self.remote.install(), patch.object(delivery, 'checked', side_effect=RuntimeError('close rejected')):
            delivery.tick(self.runtime, now=30000, project_id=self.project_id)
        outcome = json.loads(self.original()['result'])
        self.assertIn('close_abandoned', outcome)
        self.assertFalse(outcome['close_pending'])


class SupersededGenerationIdentityTest(PublicationFixture):
    def lost_merged(self):
        self.remote.lose_pr = True
        self.process_approval()
        self.store.execute("UPDATE runs SET state='superseded' WHERE id=?", (self.run['id'],))
        api = json.loads(self.remote.api.read_text())
        api['pr'].update(state='MERGED', mergeCommit={'oid': self.record()['commit']})
        self.remote.api.write_text(encode(api))
        return api

    def recover_local(self, hook=None):
        original = self.remote.checked
        def checked(args, **kwargs):
            result = original(args, **kwargs)
            if args[:3] == ['gh', 'pr', 'list']:
                self.assertIn('headRepository', args[-1].split(','))
                if hook:
                    hook()
            return result
        with self.remote.install(), patch.object(delivery, 'checked', side_effect=checked):
            delivery._recover_superseded_receipt(self.runtime, self.project, self.original(), self.config, 10000)

    def test_same_owner_other_repository_or_missing_repository_is_never_adopted(self):
        api = self.lost_merged()
        for repository in ({'nameWithOwner': 'example/other-fork'}, None):
            with self.subTest(repository=repository):
                api['pr']['headRepository'] = repository
                self.remote.api.write_text(encode(api))
                self.recover_local()
                self.assertEqual(self.original()['state'], 'superseded')
                self.assertNotIn('pr', json.loads(self.original()['result']))
                self.assertIsNone(self.store.setting('delivery-generation-pr:' + self.run['id']))

    def test_missing_merge_commit_preserves_reconciliation(self):
        api = self.lost_merged()
        api['pr']['mergeCommit'] = None
        self.remote.api.write_text(encode(api))
        self.recover_local()
        self.assertEqual(self.original()['state'], 'superseded')
        self.assertNotIn('pr', json.loads(self.original()['result']))
        api['pr']['mergeCommit'] = {'oid': self.record()['commit']}
        self.remote.api.write_text(encode(api))
        self.recover_local()
        self.assertEqual(self.original()['state'], 'merged')

    def test_conflicting_and_malformed_receipts_are_preserved(self):
        self.lost_merged()
        key = 'delivery-generation-pr:' + self.run['id']
        valid = {'generation': self.record()['generation'], 'url': 'https://github.com/example/project/pull/1'}
        for raw in ('{broken', encode({**valid, 'generation': 'wrong'}),
                    encode({**valid, 'url': 'https://github.com/example/project/pull/2'}),
                    encode({**valid, 'error': None}), encode({**valid, 'verified_merge': None}),
                    encode({**valid, 'observation': {}})):
            with self.subTest(raw=raw):
                self.store.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, raw))
                self.recover_local()
                self.assertEqual(self.original()['state'], 'superseded')
                self.assertEqual(self.store.one('SELECT value FROM settings WHERE key=?', (key,))['value'], raw)
                self.assertTrue(retention_hold(self.store, self.run['id'], self.project_id))
                self.store.execute('UPDATE runs SET ended=? WHERE id=?',
                                   (time.time() - 91 * 86400, self.run['id']))
                self.store.put_setting('maintenance-last', 0)
                self.runtime.maintenance()
                self.assertTrue(Path(self.record()['directory']).exists())
                self.assertEqual(self.store.one('SELECT value FROM settings WHERE key=?', (key,))['value'], raw)

    def test_authorization_change_during_observation_is_not_adopted(self):
        self.lost_merged()
        self.recover_local(lambda: self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,)))
        self.assertEqual(self.original()['state'], 'superseded')
        self.assertNotIn('pr', json.loads(self.original()['result']))
        self.recover_local()
        self.assertEqual(self.original()['state'], 'merged')

    def test_all_remote_identity_fields_must_match(self):
        api = self.lost_merged()
        original = dict(api['pr'])
        for delta in ({'baseRefName': 'other'}, {'url': 'https://github.com/example/other/pull/1'},
                      {'headRefOid': 'f' * 40}, {'headRefName': 'other'},
                      {'headRepositoryOwner': {'login': 'other'}}, {'mergeCommit': {'oid': 'invalid'}}):
            with self.subTest(delta=delta):
                api['pr'] = {**original, **delta}
                self.remote.api.write_text(encode(api))
                self.recover_local()
                self.assertEqual(self.original()['state'], 'superseded')
                self.assertNotIn('pr', json.loads(self.original()['result']))
                self.assertIsNone(self.store.setting('delivery-generation-pr:' + self.run['id']))

    def test_record_changes_during_observation_keep_evidence(self):
        self.lost_merged()
        for prefix in ('delivery-generation:', 'delivery-candidate:', 'delivery-push:'):
            with self.subTest(prefix=prefix):
                key = prefix + self.run['id']
                original = self.store.setting(key)
                self.recover_local(lambda: self.store.put_setting(key, {**original, 'authorization': 'changed'}))
                self.assertEqual(self.original()['state'], 'superseded')
                self.assertNotIn('pr', json.loads(self.original()['result']))
                self.assertIsNone(self.store.setting('delivery-generation-pr:' + self.run['id']))
                self.assertEqual(self.store.setting(key)['authorization'], 'changed')
                self.store.put_setting(key, original)


if __name__ == '__main__':
    unittest.main()
