"""Offline publication identity regressions with real local Git candidates."""
import json
import subprocess
import unittest
from unittest.mock import patch

from adaptive_rsi import delivery
from adaptive_rsi.config import fingerprint
from adaptive_rsi.gitops import git
from adaptive_rsi.process import Cancelled, ProcessError
from adaptive_rsi.store import encode
from tests import test_release_endpoints as recovery_tests


class DeliveryBindingTest(unittest.TestCase):
    publish = recovery_tests.DeliveryRecoveryTest.publish

    def setUp(self):
        recovery_tests.DeliveryRecoveryTest.setUp(self)
        outcome = json.loads(self.run['result'])
        outcome['patch_hash'] = fingerprint(git(self.workspace, 'diff', self.run['base'], '--binary',
                                                '--no-ext-diff', '--no-textconv'))
        self.store.execute('UPDATE runs SET result=? WHERE id=?', (encode(outcome), self.run['id']))
        self.run = self.store.one('SELECT * FROM runs WHERE id=?', (self.run['id'],))
        self.key = 'delivery-push:' + self.run['id']
        self.legacy = {'branch': 'feat/rsi-' + self.run['id'], 'head': self.head,
                       'base': self.run['base'], 'repository': 'example/project', 'created': 123}
        self.store.put_setting(self.key, self.legacy)
        baseline = patch.object(delivery, 'baseline', return_value=('unused', self.run['base'], 'main'))
        baseline.start()
        self.addCleanup(baseline.stop)

    def publication_job(self):
        self.store.execute('UPDATE runs SET path=? WHERE id=?', (str(self.workspace.parent), self.run['id']))
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.approval,))
        job = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertIsNotNone(job)
        self.assertEqual(job['task'], self.approval)
        return job

    def reapprove_failed_policy_publication(self, lost_receipt, legacy=False, restart=False):
        job = self.publication_job()
        def command(argv, **kwargs):
            if argv[1:3] == ['pr', 'list']:
                return '[]'
            self.assertEqual(argv[1:3], ['pr', 'create'])
            raise ProcessError({'code': 1, 'error': 'PR created but receipt lost'})
        failure = None if lost_receipt else ProcessError({'code': 1, 'error': 'push failed'})
        with patch.object(delivery, 'checked', side_effect=command), \
                patch.object(delivery, 'github_git', side_effect=failure):
            self.runtime.process(job)
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (job['id'],))['state'], 'blocked')
        saved = self.store.setting('delivery-candidate:' + self.run['id'])
        self.assertEqual(saved['authorization'], fingerprint([{'authorization': 'project_policy'}]))
        if legacy:
            self.store.put_setting(self.key, self.legacy)
            self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-candidate:' + self.run['id'],))
        if restart:
            self.runtime.close()
            from adaptive_rsi.runtime import Runtime
            from adaptive_rsi.store import Store
            from tests.test_runtime import FakeAgent
            self.store = Store(self.root / 'state')
            self.runtime = Runtime(self.store, FakeAgent())
            self.addCleanup(self.runtime.close)
        # Revoke automatic policy, then approve through the public human path.
        self.runtime.configure({'delivery': {'mode': 'local'}}, self.project)
        self.assertEqual(self.runtime.deliver(self.run['id'], automatic=False), self.approval)
        self.assertEqual(json.loads(self.store.one('SELECT evidence FROM tasks WHERE id=?',
                                                  (self.approval,))['evidence']),
                         [{'authorization': 'per_candidate'}])
        job = self.store.claim(self.project, self.runtime.config(self.project))
        self.assertEqual(job['task'], self.approval)
        def retry_command(argv, **kwargs):
            if argv[1:3] == ['pr', 'list']:
                return encode([{'url': self.url}]) if lost_receipt else '[]'
            self.assertEqual(argv[1:3], ['pr', 'create'])
            return self.url
        with patch.object(delivery, 'checked', side_effect=retry_command) as remote, \
                patch.object(delivery, 'pr_view', return_value={'state': 'OPEN', 'headRefOid': self.head,
                                                               'baseRefName': 'main'}), \
                patch.object(delivery, 'github_git') as push:
            self.runtime.process(job)
        if lost_receipt:
            push.assert_not_called()
            self.assertEqual(remote.call_count, 1)
        else:
            push.assert_called_once_with(self.workspace, 'push', 'origin',
                                         self.head + ':refs/heads/' + self.legacy['branch'])
        result = self.store.one('SELECT state,result FROM runs WHERE id=?', (self.run['id'],))
        self.assertEqual(result['state'], 'pr_open')
        self.assertEqual(json.loads(result['result'])['pr'], self.url)
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (job['id'],))['state'], 'completed')
        self.assertEqual(self.store.setting('delivery:' + self.run['id'])['head'], self.head)
        if not legacy:
            self.assertEqual(self.store.setting('delivery-candidate:' + self.run['id']), saved)

    def test_push_failure_recovers_through_runtime_after_process_restart(self):
        self.reapprove_failed_policy_publication(lost_receipt=False, restart=True)

    def test_pr_creation_failure_recovers_through_runtime_after_process_restart(self):
        self.reapprove_failed_policy_publication(lost_receipt=True, restart=True)

    def test_failed_automatic_push_can_be_manually_reapproved_through_runtime(self):
        self.reapprove_failed_policy_publication(lost_receipt=False)

    def test_lost_automatic_pr_receipt_is_recovered_after_manual_reapproval(self):
        self.reapprove_failed_policy_publication(lost_receipt=True)

    def test_legacy_push_receipt_is_recovered_after_manual_reapproval(self):
        self.reapprove_failed_policy_publication(lost_receipt=True, legacy=True)

    def test_legacy_journal_recovers_existing_pr(self):
        with patch.object(delivery, 'checked', return_value=encode([{'url': self.url}])), \
                patch.object(delivery, 'pr_view', return_value={'state': 'OPEN', 'headRefOid': self.head,
                                                               'baseRefName': 'main'}), \
                patch.object(delivery, 'github_git') as push:
            self.assertEqual(self.publish(), self.url)
        push.assert_not_called()
        self.assertEqual(self.store.setting(self.key), self.legacy)

    def test_legacy_push_and_pr_retry_keep_exact_commit_across_restart(self):
        remote = self.root / 'remote.git'
        subprocess.run(['git', 'clone', '--bare', '--quiet', str(self.repo), str(remote)], check=True)
        attempts = []
        pushes = []
        def command(argv, **kwargs):
            if argv[1:3] == ['pr', 'list']:
                return '[]'
            self.assertEqual(argv[1:3], ['pr', 'create'])
            attempts.append(argv)
            if len(attempts) == 1:
                raise ProcessError({'code': 1, 'error': 'interrupted PR create'})
            return self.url
        def push(workspace, *args, **kwargs):
            pushes.append(args)
            receipt = self.store.setting(self.key)
            self.assertEqual(receipt['version'], 1)
            self.assertEqual(receipt['created'], 123)
            return subprocess.run(['git', '-C', str(workspace), 'push', str(remote), args[-1]],
                                  check=True, capture_output=True, text=True).stdout
        with patch.object(delivery, 'checked', side_effect=command), \
                patch.object(delivery, 'github_git', side_effect=push):
            with self.assertRaises(ProcessError):
                self.publish()
            self.runtime.close()
            from adaptive_rsi.runtime import Runtime
            from adaptive_rsi.store import Store
            from tests.test_runtime import FakeAgent
            self.store = Store(self.root / 'state')
            self.runtime = Runtime(self.store, FakeAgent())
            self.addCleanup(self.runtime.close)
            self.assertEqual(self.publish(), self.url)
        self.assertEqual(pushes, [('push', 'origin', self.head + ':refs/heads/' + self.legacy['branch'])] * 2)
        self.assertEqual(git(remote, 'rev-parse', 'refs/heads/' + self.legacy['branch']), self.head)

    def test_invalid_legacy_or_partial_binding_never_reads_remote_or_seals_candidate(self):
        bad = [{**self.legacy, key: 'changed'} for key in ('branch', 'head', 'base', 'repository')]
        bad += [{k: v for k, v in self.legacy.items() if k != 'base'},
                {**self.legacy, 'version': 1}, {}, [], '{broken']
        with patch.object(delivery, 'checked') as command, patch.object(delivery, 'github_git') as push:
            for value in bad:
                with self.subTest(value=value):
                    raw = value if value == '{broken' else encode(value)
                    self.store.execute('UPDATE settings SET value=? WHERE key=?', (raw, self.key))
                    with self.assertRaises(RuntimeError):
                        self.publish()
                    self.assertIsNone(self.store.setting('delivery-candidate:' + self.run['id']))
        command.assert_not_called()
        push.assert_not_called()

    def test_candidate_and_authorization_changes_after_remote_read_refuse_push(self):
        for change in ('approval', 'evidence', 'tree'):
            with self.subTest(change=change):
                def command(argv, **kwargs):
                    if change == 'approval':
                        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
                    elif change == 'evidence':
                        self.store.execute("UPDATE runs SET result='{}' WHERE id=?", (self.run['id'],))
                    else:
                        (self.workspace / 'calculator.py').write_text('tampered\n')
                    return '[]'
                with patch.object(delivery, 'checked', side_effect=command), \
                        patch.object(delivery, 'github_git') as push:
                    with self.assertRaises((RuntimeError, Cancelled)):
                        self.publish()
                push.assert_not_called()
                self.store.execute("UPDATE tasks SET state='planning' WHERE id=?", (self.approval,))
                self.store.execute('UPDATE runs SET result=? WHERE id=?', (self.run['result'], self.run['id']))
                git(self.workspace, 'checkout', '--', 'calculator.py')

    def test_changed_head_with_same_tree_cannot_replace_persisted_candidate(self):
        with patch.object(delivery, 'checked', return_value='[]'), \
                patch.object(delivery, 'github_git', side_effect=ProcessError({'code': 1, 'error': 'push failed'})):
            with self.assertRaises(ProcessError):
                self.publish()
        receipt = self.store.setting(self.key)
        git(self.workspace, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
            'commit', '--amend', '-m', 'different identity')
        with patch.object(delivery, 'checked') as command, patch.object(delivery, 'github_git') as push:
            with self.assertRaisesRegex(RuntimeError, 'different candidate head'):
                self.publish()
        command.assert_not_called()
        push.assert_not_called()
        self.assertEqual(self.store.setting(self.key), receipt)

    def test_revoked_approval_after_push_refuses_pr_creation(self):
        def push(*args):
            self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
        with patch.object(delivery, 'checked', return_value='[]') as command, \
                patch.object(delivery, 'github_git', side_effect=push):
            with self.assertRaises(Cancelled):
                self.publish()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(self.store.setting(self.key)['head'], self.head)
