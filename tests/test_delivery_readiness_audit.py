"""Offline contract regressions; these do not prove live GitHub acceptance."""
import copy
import json
import unittest
from unittest.mock import patch

from adaptive_rsi import delivery, readiness, release
from adaptive_rsi.config import DEFAULTS
import tests.test_release_endpoints as recovery_tests
from adaptive_rsi.gitops import git


class ClosedPublicationTest(unittest.TestCase):
    setUp = recovery_tests.DeliveryRecoveryTest.setUp
    publish = recovery_tests.DeliveryRecoveryTest.publish

    def test_closed_pr_never_reports_open_or_writes_remote(self):
        with patch.object(delivery, 'baseline', return_value=('unused', self.run['base'], 'main')), \
                patch.object(delivery, 'checked', return_value=json.dumps([{'url': self.url}])) as command, \
                patch.object(delivery, 'pr_view', return_value={'state': 'CLOSED', 'headRefOid': self.head, 'baseRefName': 'main'}), \
                patch.object(delivery, 'github_git') as push:
            with self.assertRaisesRegex(RuntimeError, 'explicitly reopen'):
                self.publish()
        push.assert_not_called()
        self.assertEqual(command.call_count, 1)
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (self.run['id'],))['state'], 'awaiting_approval')
        self.assertIsNone(self.store.setting('delivery:' + self.run['id']))


class BaselineAndCleanupTest(unittest.TestCase):
    setUp = recovery_tests.DeliveryRecoveryTest.setUp
    publish = recovery_tests.DeliveryRecoveryTest.publish

    def test_committed_changes_are_read_between_clean_snapshots(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app.py').write_text('VALUE = 0\n')
            (root / 'tests').mkdir()
            (root / 'tests/test_app.py').write_text('assert True\n')
            git(root, 'init', '-q')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'base')
            start = git(root, 'rev-parse', 'HEAD')
            (root / 'tests/test_app.py').write_text('assert False\n')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'change test')
            end = git(root, 'rev-parse', 'HEAD')
            self.assertEqual(delivery.committed_changes(root, start, end), ['tests/test_app.py'])
            self.assertEqual(delivery.committed_changes(root, end, end), [])

    def test_diverged_baseline_is_explained_without_overwriting_any_ref(self):
        old = self.run['base']
        candidate = self.head
        git(self.workspace, 'checkout', '--detach', old)
        (self.workspace / 'other.txt').write_text('independent mainline')
        git(self.workspace, 'add', '.')
        git(self.workspace, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'other')
        current = git(self.workspace, 'rev-parse', 'HEAD')
        result = delivery.baseline_change(self.workspace, candidate, current)
        self.assertEqual(result['relation'], 'diverged')
        self.assertTrue(result['evidence_invalidated'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), current)

    def test_rewritten_baseline_preserves_candidate_history(self):
        result = delivery.baseline_change(self.workspace, self.head, self.run['base'])
        self.assertEqual(result['relation'], 'rewritten')
        self.assertTrue(result['evidence_invalidated'])
        self.assertEqual(git(self.workspace, 'rev-parse', 'HEAD'), self.head)

    def test_full_cleanup_page_yields_no_candidates(self):
        branches = [{'name': 'feat/rsi-' + str(i), 'commit': {'sha': str(i)}} for i in range(100)]
        prs = [{'state': 'CLOSED', 'head': {'ref': row['name'], 'sha': row['commit']['sha']}} for row in branches]
        with patch.object(release, 'api', side_effect=[branches, prs]):
            result = delivery.branch_cleanup_preview('example/project')
        self.assertFalse(result['complete'])
        self.assertEqual(result['candidates'], [])

    def test_cleanup_preview_protects_open_pr_recovery_and_unknown_branches(self):
        branches = [{'name': 'feat/rsi-old', 'commit': {'sha': 'old'}},
                    {'name': 'fix/rsi-open', 'commit': {'sha': 'open'}},
                    {'name': 'feat/rsi-backup', 'commit': {'sha': 'backup'}},
                    {'name': 'main', 'commit': {'sha': 'main'}}]
        prs = [{'state': 'CLOSED', 'head': {'ref': 'feat/rsi-old', 'sha': 'old'}},
               {'state': 'OPEN', 'head': {'ref': 'fix/rsi-open', 'sha': 'open'}}]
        with patch.object(release, 'api', side_effect=[branches, prs]) as command:
            result = delivery.branch_cleanup_preview('example/project', recovery_refs=['backup'])
        self.assertEqual(result['candidates'], ['feat/rsi-old'])
        self.assertFalse(result['deletion_performed'])
        self.assertEqual(command.call_count, 2)
        self.assertTrue(all(len(call.args) == 2 for call in command.call_args_list))


class AttributionTest(unittest.TestCase):
    def test_later_mainline_failure_does_not_attribute_candidate_regression(self):
        result = delivery.failure_attribution('merged', 'later', {'decision': 'fail', 'checks': {'baseline': [{'passed': True}], 'candidate': [{'passed': False}]}})
        self.assertEqual(result['scope'], 'subsequent_mainline')
        self.assertFalse(result['candidate_regression_proven'])

    def test_unverified_merge_baseline_stays_unknown(self):
        result = delivery.failure_attribution('merged', 'later', {'decision': 'fail', 'checks': {}})
        self.assertEqual(result['scope'], 'unknown')


class ProtectionRulesTest(unittest.TestCase):
    def setUp(self):
        delivery._PROTECTION_CACHE.clear()
        self.config = copy.deepcopy(DEFAULTS)
        self.config['github']['repository'] = 'example/project'
        self.config['delivery']['base_branch'] = 'main'

    def test_active_rules_endpoint_can_establish_strictness_without_classic_protection(self):
        def command(argv, **kwargs):
            if argv[2].endswith('/protection'):
                raise RuntimeError('HTTP 404')
            self.assertIn('/rules/branches/main', argv[2])
            return json.dumps([{'type': 'required_status_checks', 'parameters': {'strict_required_status_checks_policy': True}}, {'type': 'merge_queue'}])
        with patch.object(delivery, 'checked', side_effect=command):
            result = delivery.merge_protection(self.config)
        self.assertEqual(result['state'], 'strict')
        self.assertTrue(result['merge_queue'])
        self.assertEqual(result['sources']['classic'], 'unknown')
        self.assertEqual(result['sources']['rulesets'], 'observed')

    def test_ruleset_read_failure_is_unknown_even_with_classic_rules(self):
        with patch.object(delivery, 'checked', side_effect=[json.dumps({'required_status_checks': {'strict': False}}), RuntimeError('HTTP 403')]):
            result = delivery.merge_protection(self.config)
        self.assertIsNone(result['merge_queue'])
        self.assertFalse(result['complete'])


class CapabilityTest(unittest.TestCase):
    def test_read_only_repository_permissions_never_imply_write(self):
        with patch.object(readiness, 'run_process', return_value={'exit_code': 0, 'error': '', 'stdout': json.dumps({'permissions': {'pull': True, 'push': False}})}) as command:
            result = readiness.github_capabilities('example/project')
        self.assertEqual(result['read'], 'available')
        self.assertEqual(result['write'], 'unavailable')
        self.assertEqual(result['actions_write'], 'unknown')
        self.assertNotIn('POST', command.call_args.args[0])

    def test_missing_permissions_are_unknown(self):
        with patch.object(readiness, 'run_process', return_value={'exit_code': 0, 'error': '', 'stdout': '{}'}):
            self.assertEqual(readiness.github_capabilities('example/project')['write'], 'unknown')


class RemoteTimingTest(unittest.TestCase):
    def test_remote_timestamps_preserved_with_unknown_build_phase(self):
        data = {'repository': 'example/project', 'workflow_run_id': 7, 'workflow_sha': 'a', 'operation_id': 'op', 'workflow': '.github/workflows/publish.yml'}
        remote = {'id': 7, 'head_sha': 'a', 'display_title': 'op', 'event': 'workflow_dispatch', 'path': data['workflow'], 'status': 'completed', 'conclusion': 'success',
                  'created_at': '2026-10-28T00:00:00Z', 'run_started_at': '2026-10-28T00:01:00Z', 'updated_at': '2026-10-28T00:03:00Z'}
        with patch.object(release, 'api', return_value=remote):
            result = release.reconcile(None, {}, 'release', data, persist=False)
        self.assertEqual(result['timing']['queue_seconds'], 60)
        self.assertEqual(result['timing']['execution_seconds'], 120)
        self.assertIsNone(result['timing']['build_seconds'])
        self.assertEqual(result['timing']['completed_at_basis'], 'updated_at')


class AuthorizationReceiptTest(unittest.TestCase):
    def test_later_verified_head_cannot_authorize_failed_merge(self):
        from tests.test_release import ReleaseAutomationTest, MERGED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            outcome = {'merged_sha': MERGED, 'feedback': {'state': 'verified', 'head': 'd' * 40,
                       'evaluation': {'decision': 'pass'}}}
            with patch.object(release, 'api', side_effect=case.api):
                data = release.advance(case.runtime, case.runtime.project(case.project), case.run, outcome)
            self.assertEqual(data['release']['state'], 'blocked_verification')
            self.assertFalse(any(method == 'POST' for _, method, _ in case.commands))
        finally:
            case.tearDown()

    def test_receipt_binds_exact_dispatch_inputs_and_target_policy(self):
        from tests.test_release import ReleaseAutomationTest
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            with patch.object(release, 'api', side_effect=case.api):
                data = case.tick()['release']
            self.assertEqual(data['authorization']['policy_hash'], release.fingerprint(release.normalized_policy('release', case.policy)))
            self.assertEqual(data['authorization']['inputs_hash'], release.fingerprint({'rsi_operation_id': data['operation_id'], 'rsi_sha': data['sha']}))
        finally:
            case.tearDown()

    def test_multiple_matches_remain_unknown_and_expose_candidates(self):
        data = {'repository': 'example/project', 'workflow_sha': 'a', 'operation_id': 'op', 'workflow': '.github/workflows/publish.yml', 'state': 'dispatch_unknown'}
        with patch.object(release, 'api', return_value={'workflow_runs': [{'id': 1, 'display_title': 'op'}, {'id': 2, 'display_title': 'op'}]}) as command:
            result = release.reconcile(None, {}, 'release', data, persist=False)
        self.assertEqual(result['state'], 'dispatch_unknown')
        self.assertEqual(result['reconciliation']['candidate_run_ids'], [1, 2])
        self.assertEqual(result['reconciliation']['state'], 'manual_inspection_required')
        command.assert_called_once()


class RateLimitTest(unittest.TestCase):
    def tearDown(self):
        release._API_BACKOFF.clear()

    def test_secondary_limit_blocks_following_calls_without_replaying_write(self):
        with patch.object(release, 'checked', side_effect=RuntimeError('secondary rate limit (HTTP 403)')) as command:
            with self.assertRaises(release.ApiRejected):
                release.api('example/project', 'actions/workflows/a/dispatches', 'POST')
            with self.assertRaisesRegex(RuntimeError, 'rate limit'):
                release.api('example/project', 'actions/workflows/a/dispatches', 'POST')
            self.assertEqual(command.call_count, 1)
