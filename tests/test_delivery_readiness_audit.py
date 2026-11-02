"""Offline contract regressions; these do not prove live GitHub acceptance."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import delivery, readiness, release
from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.process import ProcessError
import tests.test_delivery_loop as delivery_tests
import tests.test_release_endpoints as recovery_tests
from adaptive_rsi.gitops import git
from adaptive_rsi.store import encode

# 只读抓取的 GitHub 保护规则响应，保留端点与抓取时间；见
# docs/validation/ruleset-verification-evidence-20261101.md。
PROTECTION_FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'github_protection'


def captured(name):
    return json.loads((PROTECTION_FIXTURES / name).read_text(encoding='utf-8'))


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
        self.assertEqual(result['disclaimer'], 'later-mainline 失败不记候选回归')

    def test_unverified_merge_baseline_stays_unknown(self):
        result = delivery.failure_attribution('merged', 'later', {'decision': 'fail', 'checks': {}})
        self.assertEqual(result['scope'], 'unknown')
        self.assertEqual(result['disclaimer'], '')


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

    def replay(self, *names):
        """Drive the real decision code with responses captured from live `gh api` reads."""
        pending = list(names)

        def command(argv, **kwargs):
            data = captured(pending.pop(0))
            if 'result' in data:
                raise ProcessError(data['result'])
            return json.dumps(data['response'])

        with patch.object(delivery, 'checked', side_effect=command) as invoked:
            result = delivery.merge_protection(self.config)
        self.assertEqual(pending, [], 'unused captures: ' + repr(pending))
        self.assertEqual(invoked.call_count, len(names))
        return result

    def test_managed_repository_classic_payload_marks_both_sources_observed(self):
        result = self.replay('managed-classic-protection.json', 'managed-branch-rules-empty.json')
        self.assertEqual(result['sources'], {'classic': 'observed', 'rulesets': 'observed'})
        self.assertTrue(result['complete'])
        self.assertEqual(result['state'], 'strict')
        self.assertTrue(result['strict_checks'])
        self.assertFalse(result['merge_queue'])

    def test_unprotected_branch_payload_keeps_state_unknown(self):
        # Real 404 "Branch not protected" plus an empty rules page: only the
        # ruleset source is observed, so no strictness or mergeability is claimed.
        result = self.replay('unprotected-classic-404.json', 'unprotected-branch-rules-empty.json')
        self.assertEqual(result['sources'], {'classic': 'unknown', 'rulesets': 'observed'})
        self.assertFalse(result['complete'])
        self.assertEqual(result['state'], 'unknown')
        self.assertIsNone(result['strict_checks'])
        self.assertFalse(result['merge_queue'])

    def test_plan_forbidden_payloads_keep_every_source_unknown(self):
        # Real 403 "Upgrade to GitHub Pro" on a private repository without branch
        # rules: permission-limited reads must stay unknown, never "mergeable".
        result = self.replay('plan-forbidden-classic-403.json', 'plan-forbidden-rules-403.json')
        self.assertEqual(result['sources'], {'classic': 'unknown', 'rulesets': 'unknown'})
        self.assertFalse(result['complete'])
        self.assertEqual(result['state'], 'unknown')
        self.assertIsNone(result['strict_checks'])
        self.assertIsNone(result['merge_queue'])

    def test_ruleset_only_payload_never_claims_strict_without_classic_read(self):
        # A live ruleset-only repository: the classic endpoint 404s, the rules page
        # carries the required checks without a strict policy.
        result = self.replay('reference-classic-404.json', 'reference-ruleset-status-checks.json')
        self.assertEqual(result['sources']['classic'], 'unknown')
        self.assertEqual(result['sources']['rulesets'], 'observed')
        self.assertFalse(result['complete'])
        self.assertEqual(result['state'], 'unknown')
        self.assertIsNone(result['strict_checks'])
        self.assertFalse(result['merge_queue'])

    def test_captured_classic_and_ruleset_payloads_combine_only_in_replay(self):
        """No managed repository has both configurations; this pair is replayed, not live."""
        result = self.replay('managed-classic-protection.json',
                             'reference-ruleset-merge-queue.json')
        self.assertEqual(result['sources'], {'classic': 'observed', 'rulesets': 'observed'})
        self.assertTrue(result['complete'])
        self.assertEqual(result['state'], 'strict')
        self.assertTrue(result['strict_checks'])
        self.assertTrue(result['merge_queue'])

    def test_captured_fixtures_keep_endpoint_and_time_provenance(self):
        names = sorted(path.name for path in PROTECTION_FIXTURES.glob('*.json'))
        self.assertGreaterEqual(len(names), 9)
        for name in names:
            data = captured(name)
            self.assertTrue(data['endpoint'].startswith('repos/'), name)
            self.assertRegex(data['captured'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
            self.assertTrue(data['read_only'], name)
            self.assertEqual('response' in data, 'result' not in data, name)


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
        self.assertIsNone(result['timing']['reverification_seconds'])
        self.assertEqual(result['timing']['completed_at_basis'], 'updated_at')
        self.assertFalse(isinstance(result['timing']['build_seconds'], (int, float)))
        self.assertFalse(isinstance(result['timing']['reverification_seconds'], (int, float)))

    def test_unavailable_build_phase_never_outputs_numeric_duration(self):
        data = {'repository': 'example/project', 'workflow_run_id': 12, 'workflow_sha': 'c', 'operation_id': 'op-build', 'workflow': '.github/workflows/release.yml'}
        remote = {'id': 12, 'head_sha': 'c', 'display_title': 'op-build', 'event': 'workflow_dispatch', 'path': data['workflow'], 'status': 'completed', 'conclusion': 'success',
                  'created_at': '2026-10-28T01:00:00Z', 'run_started_at': '2026-10-28T01:00:30Z', 'updated_at': '2026-10-28T01:05:00Z'}
        with patch.object(release, 'api', return_value=remote):
            result = release.reconcile(None, {}, 'release', data, persist=False)
        timing = result['timing']
        for phase in ('queue_seconds', 'execution_seconds', 'build_seconds', 'reverification_seconds'):
            self.assertIn(phase, timing)
        self.assertEqual(timing['queue_seconds'], 30)
        self.assertEqual(timing['execution_seconds'], 270)
        self.assertIsNone(timing['build_seconds'])
        self.assertIsNone(timing['reverification_seconds'])
        self.assertNotEqual(timing['build_seconds'], 0)
        self.assertNotEqual(timing['reverification_seconds'], 0)
        self.assertFalse(isinstance(timing['build_seconds'], (int, float)))
        self.assertFalse(isinstance(timing['reverification_seconds'], (int, float)))

    def test_missing_remote_timestamps_yield_none_rather_than_zero_or_omission(self):
        data = {'repository': 'example/project', 'workflow_run_id': 15, 'workflow_sha': 'd', 'operation_id': 'op-pending', 'workflow': '.github/workflows/release.yml'}
        remote = {'id': 15, 'head_sha': 'd', 'display_title': 'op-pending', 'event': 'workflow_dispatch', 'path': data['workflow'], 'status': 'in_progress',
                  'created_at': '2026-10-28T02:00:00Z', 'run_started_at': None, 'updated_at': '2026-10-28T02:00:10Z'}
        with patch.object(release, 'api', return_value=remote):
            result = release.reconcile(None, {}, 'release', data, persist=False)
        timing = result['timing']
        for phase in ('queue_seconds', 'execution_seconds', 'build_seconds', 'reverification_seconds'):
            self.assertIn(phase, timing)
        self.assertIsNone(timing['queue_seconds'])
        self.assertIsNone(timing['execution_seconds'])
        self.assertIsNone(timing['build_seconds'])
        self.assertIsNone(timing['reverification_seconds'])
        self.assertNotEqual(timing['queue_seconds'], 0)
        self.assertNotEqual(timing['execution_seconds'], 0)


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

    def test_later_mainline_regression_does_not_block_release_of_verified_merge(self):
        from tests.test_release import ReleaseAutomationTest, MERGED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            outcome = {'merged_sha': MERGED, 'feedback': {
                'state': 'regressed', 'head': 'd' * 40,
                'merge': {'sha': MERGED, 'state': 'verified', 'checked_at': 1, 'decision': 'pass'}}}
            with patch.object(release, 'api', side_effect=case.api):
                data = release.advance(case.runtime, case.runtime.project(case.project), case.run, outcome)
            self.assertEqual(data['release']['state'], 'submitted')
            dispatch = [item for item in case.commands if '/dispatches' in item[0]]
            self.assertEqual(len(dispatch), 1)
            self.assertEqual(dispatch[0][2]['inputs[rsi_sha]'], MERGED)
        finally:
            case.tearDown()

    def test_legacy_verified_receipt_without_head_is_locally_pinned_before_dispatch(self):
        from tests.test_release import ReleaseAutomationTest, MERGED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            (case.repo / 'calculator.py').write_text('def add(a,b):\n    return a+b\n')
            git(case.repo, 'add', '.')
            git(case.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'fixed')
            target = git(case.repo, 'rev-parse', 'HEAD')
            # Pin the exact check the receipt must re-run. Relying on adapter
            # detection would make the delivery gate depend on the host's
            # available toolchain rather than on sealed project configuration.
            sealed = json.loads(case.run['config'])
            sealed['checks'] = [{'name': 'calculator behaviour', 'cwd': '.',
                                 'argv': ['python3', '-c',
                                          'import calculator,sys; sys.exit(0 if calculator.add(2,3)==5 else 1)']}]
            case.runtime.store.execute('UPDATE runs SET config=? WHERE id=?',
                                       (encode(sealed), case.run['id']))
            case.run = case.runtime.store.one('SELECT * FROM runs WHERE id=?', (case.run['id'],))
            outcome = {'merged_sha': target, 'feedback': {'state': 'verified'}}
            # The sandbox backend is macOS-only in CI, so stub the execution while
            # asserting it was asked to evaluate two snapshots of the exact target.
            checked = []
            def evaluate(baseline, candidate, checks, directory, config, **kwargs):
                checked.append((git(baseline, 'rev-parse', 'HEAD'), git(candidate, 'rev-parse', 'HEAD'), checks))
                return {'decision': 'pass', 'checks': {'candidate': [{'name': 'calculator behaviour', 'passed': True}]}}
            with patch.object(release, 'api', side_effect=case.api), \
                    patch.object(case.runtime.evaluator, 'evaluate', side_effect=evaluate):
                data = release.advance(case.runtime, case.runtime.project(case.project), case.run, outcome)
            self.assertEqual(data['release']['state'], 'submitted', data['release'].get('error'))
            self.assertEqual(checked, [(target, target, sealed['checks'])])
            stored = json.loads(case.runtime.store.one('SELECT result FROM runs WHERE id=?',
                                                        (case.run['id'],))['result'])
            self.assertEqual(stored['feedback']['head'], target)
            self.assertEqual(stored['feedback']['head_pinned']['basis'],
                             'exact_target_fixed_checks')
            self.assertEqual(stored['feedback']['head_pinned']['evaluation']['decision'], 'pass')
            self.assertFalse(release.feedback_target_status(
                {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}, MERGED)[0])
            self.assertFalse(release.feedback_target_status(
                {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}, 'c' * 40)[0])
        finally:
            case.tearDown()

    def test_legacy_receipt_failing_exact_target_checks_has_no_remote_writes(self):
        from tests.test_release import ReleaseAutomationTest
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            target = git(case.repo, 'rev-parse', 'HEAD')
            outcome = {'merged_sha': target, 'feedback': {'state': 'verified'}}
            with patch.object(release, 'api', side_effect=case.api):
                data = release.advance(case.runtime, case.runtime.project(case.project), case.run, outcome)
            self.assertEqual(data['release']['state'], 'blocked_verification')
            self.assertNotIn('head', outcome['feedback'])
            self.assertFalse(any(method != 'GET' for _, method, _ in case.commands))
        finally:
            case.tearDown()

    def test_retry_rejects_verified_feedback_for_a_different_target(self):
        from tests.test_release import ReleaseAutomationTest, MERGED, TRUSTED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            result = json.loads(case.run['result'])
            result['feedback'] = {'state': 'verified', 'head': 'd' * 40}
            case.runtime.store.execute('UPDATE runs SET result=? WHERE id=?',
                                       (json.dumps(result), case.run['id']))
            case.runtime.store.put_setting(release.key(case.run['id'], 'release'), {
                'state': 'failed', 'operation_id': 'rsi-operation', 'sha': MERGED,
                'workflow_run_id': 17, 'attempt': 1, 'repository': 'example/project',
                'workflow': case.policy['workflow'], 'workflow_sha': TRUSTED,
                'ref': case.policy['ref']})
            with patch.object(release, 'api', side_effect=case.api):
                with self.assertRaisesRegex(ValueError, 'exact target'):
                    release.retry(case.runtime, case.project, case.run['id'], 'release')
            self.assertFalse(any(endpoint.endswith('/rerun') for endpoint, _, _ in case.commands))
            self.assertFalse(any('/dispatches' in endpoint for endpoint, _, _ in case.commands))
        finally:
            case.tearDown()

    def test_advance_checks_persisted_dispatch_target_not_only_merge_sha(self):
        from tests.test_release import ReleaseAutomationTest, MERGED, TRUSTED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            other = 'c' * 40
            case.runtime.store.put_setting(release.key(case.run['id'], 'release'), {
                'state': 'pending', 'operation_id': 'rsi-stale', 'sha': other,
                'repository': 'example/project', 'workflow': case.policy['workflow'],
                'ref': case.policy['ref'], 'workflow_sha': TRUSTED})
            outcome = {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}
            with patch.object(release, 'api', side_effect=case.api):
                data = release.advance(case.runtime, case.runtime.project(case.project),
                                       case.run, outcome)
            self.assertEqual(data['release']['state'], 'blocked_verification')
            self.assertFalse(any('/dispatches' in endpoint for endpoint, _, _ in case.commands))
        finally:
            case.tearDown()

    def test_legacy_verified_receipt_without_local_evidence_stays_blocked(self):
        from tests.test_release import ReleaseAutomationTest, MERGED
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            case.runtime.store.execute("DELETE FROM settings WHERE key='delivery:' || ?",
                                       (case.run['id'],))
            outcome = {'merged_sha': MERGED, 'feedback': {'state': 'verified'}}
            with patch.object(release, 'api', side_effect=case.api):
                data = release.advance(case.runtime, case.runtime.project(case.project),
                                       case.run, outcome)
            self.assertEqual(data['release']['state'], 'blocked_verification')
            self.assertFalse(any('/dispatches' in endpoint for endpoint, _, _ in case.commands))
        finally:
            case.tearDown()

    def test_missing_artifact_wrapper_does_not_hide_verified_merge_evidence(self):
        from tests.test_release import MERGED
        outcome = {'merged_sha': MERGED, 'feedback': {
            'state': 'insufficient_evidence', 'reason': 'missing artifacts',
            'previous': {'state': 'verified', 'head': MERGED,
                         'merge': {'sha': MERGED, 'state': 'verified'}}}}
        self.assertTrue(release.feedback_target_status(outcome, MERGED)[0])
        later = {'merged_sha': MERGED, 'feedback': {
            'state': 'insufficient_evidence', 'reason': 'missing artifacts',
            'previous': {'state': 'verified', 'head': 'd' * 40,
                         'merge': {'sha': MERGED, 'state': 'verified'}}}}
        self.assertTrue(release.feedback_target_status(later, MERGED)[0])


class PostmergeReleaseEvidenceTest(unittest.TestCase):
    setUp = delivery_tests.DeliveryLoopTest.setUp
    tearDown = delivery_tests.DeliveryLoopTest.tearDown
    tracked_pr = delivery_tests.DeliveryLoopTest.tracked_pr

    def test_merge_release_evidence_survives_later_mainline_regression(self):
        from adaptive_rsi.delivery import feedback
        run = self.tracked_pr()
        directory = self.store.root / 'runs' / run['id']
        directory.mkdir(parents=True)
        run['path'] = str(directory)
        merged = git(self.repo, 'rev-parse', 'HEAD')
        outcome = {'merged_sha': merged, 'merged_at': time.time()}
        self.runtime.configure({'delivery': {'feedback_minutes': 5},
                                'github': {'repository': 'example/project'}}, self.project)
        passing = {'decision': 'pass', 'checks': {'candidate': [{'passed': True}]}}
        failing = {'decision': 'fail', 'checks': {'baseline': [{'passed': True}],
                                                  'candidate': [{'passed': False}]}}
        now = time.time()
        with patch('adaptive_rsi.delivery.baseline', return_value=(str(self.repo), merged, 'main')), \
                patch.object(self.runtime.evaluator, 'evaluate',
                             side_effect=[passing, failing]) as evaluate:
            feedback(self.runtime, self.runtime.project(self.project), run, outcome,
                     self.runtime.config(self.project), now)
            self.assertEqual(outcome['feedback']['merge'], {
                'sha': merged, 'state': 'verified', 'checked_at': now, 'decision': 'pass'})
            self.assertEqual(outcome['feedback']['state'], 'verified')
            (self.repo / 'calculator.py').write_text('def add(a,b):\n    return a-b\n')
            git(self.repo, 'add', '.')
            git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
                'commit', '-qm', 'later mainline')
            later = git(self.repo, 'rev-parse', 'HEAD')
            with patch('adaptive_rsi.delivery.baseline', return_value=(str(self.repo), later, 'main')):
                feedback(self.runtime, self.runtime.project(self.project), run, outcome,
                         self.runtime.config(self.project), now + 301)
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(outcome['feedback']['state'], 'regressed')
        self.assertEqual(outcome['feedback']['head'], later)
        self.assertEqual(outcome['feedback']['merge']['sha'], merged)
        self.assertEqual(outcome['feedback']['merge']['state'], 'verified')

    def test_legacy_receipt_with_recorded_head_gains_exact_merge_evidence(self):
        from adaptive_rsi.delivery import feedback
        run = self.tracked_pr()
        directory = self.store.root / 'runs' / run['id']
        directory.mkdir(parents=True)
        run['path'] = str(directory)
        merged = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'calculator.py').write_text('def add(a,b):\n    return a+b\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
            'commit', '-qm', 'later mainline')
        later = git(self.repo, 'rev-parse', 'HEAD')
        self.runtime.configure({'delivery': {'feedback_minutes': 5},
                                'github': {'repository': 'example/project'}}, self.project)
        passing = {'decision': 'pass', 'checks': {'candidate': [{'passed': True}]}}
        now = time.time()
        # Historical receipt: it already recorded a later mainline head and has no
        # independent record for the merge commit that release must publish.
        outcome = {'merged_sha': merged, 'merged_at': now - 600,
                   'feedback': {'state': 'verified', 'head': later, 'checked_at': now - 600,
                                'evaluation': passing}}
        with patch('adaptive_rsi.delivery.baseline', return_value=(str(self.repo), later, 'main')), \
                patch.object(self.runtime.evaluator, 'evaluate', return_value=passing) as evaluate:
            feedback(self.runtime, self.runtime.project(self.project), run, outcome,
                     self.runtime.config(self.project), now)
        # Only the merge commit is re-read; the later mainline delta stays reused.
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(outcome['feedback']['head'], later)
        self.assertEqual(outcome['feedback']['merge'], {
            'sha': merged, 'state': 'verified', 'checked_at': now, 'decision': 'pass'})
        self.assertTrue(release.feedback_target_status(outcome, merged)[0])
        self.assertFalse(release.feedback_target_status(outcome, later)[0])
        stored = json.loads(self.store.one('SELECT result FROM runs WHERE id=?',
                                           (run['id'],))['result'])
        self.assertEqual(stored['feedback']['merge']['sha'], merged)

    def test_transient_merge_evidence_is_reverified_instead_of_staying_frozen(self):
        from adaptive_rsi.delivery import feedback
        run = self.tracked_pr()
        directory = self.store.root / 'runs' / run['id']
        directory.mkdir(parents=True)
        run['path'] = str(directory)
        merged = git(self.repo, 'rev-parse', 'HEAD')
        self.runtime.configure({'delivery': {'feedback_minutes': 5},
                                'github': {'repository': 'example/project'}}, self.project)
        passing = {'decision': 'pass', 'checks': {'candidate': [{'passed': True}]}}
        failing = {'decision': 'fail', 'checks': {'baseline': [{'passed': True}],
                                                  'candidate': [{'passed': False}]}}
        unavailable = {'decision': 'insufficient_evidence', 'checks': {}}
        now = time.time()
        stale = now - 600
        outcome = {'merged_sha': merged, 'merged_at': stale,
                   'feedback': {'state': 'insufficient_evidence', 'head': merged,
                                'checked_at': stale, 'evaluation': unavailable,
                                'merge': {'sha': merged, 'state': 'insufficient_evidence',
                                          'checked_at': stale,
                                          'decision': 'insufficient_evidence'}}}
        with patch('adaptive_rsi.delivery.baseline', return_value=(str(self.repo), merged, 'main')), \
                patch.object(self.runtime.evaluator, 'evaluate',
                             side_effect=[passing, failing]) as evaluate:
            # A missing executable or artifact at the first poll must not freeze
            # the run: the next poll re-evaluates the merge commit itself.
            feedback(self.runtime, self.runtime.project(self.project), run, outcome,
                     self.runtime.config(self.project), now)
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(outcome['feedback']['merge']['state'], 'verified')
            self.assertEqual(outcome['feedback']['state'], 'verified')
            self.assertTrue(release.feedback_target_status(outcome, merged)[0])
            (self.repo / 'calculator.py').write_text('def add(a,b):\n    return a-b\n')
            git(self.repo, 'add', '.')
            git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
                'commit', '-qm', 'later mainline')
            later = git(self.repo, 'rev-parse', 'HEAD')
            with patch('adaptive_rsi.delivery.baseline',
                       return_value=(str(self.repo), later, 'main')):
                feedback(self.runtime, self.runtime.project(self.project), run, outcome,
                         self.runtime.config(self.project), now + 601)
        # The settled merge record is not re-evaluated; only the new delta is.
        self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(outcome['feedback']['head'], later)
        self.assertEqual(outcome['feedback']['state'], 'regressed')
        self.assertEqual(outcome['feedback']['merge']['state'], 'verified')
        self.assertTrue(release.feedback_target_status(outcome, merged)[0])

    def test_blocked_legacy_receipt_is_repairable_by_the_next_poll(self):
        from tests.test_release import ReleaseAutomationTest
        from adaptive_rsi.delivery import feedback
        case = ReleaseAutomationTest()
        case.setUp()
        try:
            (case.repo / 'calculator.py').write_text('def add(a,b):\n    return a+b\n')
            git(case.repo, 'add', '.')
            git(case.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
                'commit', '-qm', 'merged')
            merged = git(case.repo, 'rev-parse', 'HEAD')
            (case.repo / 'notes.txt').write_text('later mainline\n')
            git(case.repo, 'add', '.')
            git(case.repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
                'commit', '-qm', 'later mainline')
            later = git(case.repo, 'rev-parse', 'HEAD')
            passing = {'decision': 'pass', 'checks': {'candidate': [{'passed': True}]}}
            now = time.time()
            outcome = {'merged_sha': merged, 'merged_at': now - 600,
                       'feedback': {'state': 'verified', 'head': later, 'checked_at': now - 600,
                                    'evaluation': passing}}
            case.runtime.store.execute('UPDATE runs SET result=? WHERE id=?',
                                       (json.dumps(outcome), case.run['id']))
            with patch.object(release, 'api', side_effect=case.api):
                blocked = release.advance(case.runtime, case.runtime.project(case.project),
                                          case.run, outcome)
            self.assertEqual(blocked['release']['state'], 'blocked_verification')
            self.assertFalse(any('/dispatches' in endpoint for endpoint, _, _ in case.commands))
            case.runtime.store.put_setting('run-baseline:' + case.run['id'],
                                           {'repository': 'example/project', 'branch': 'main'})
            directory = case.runtime.store.root / 'runs' / case.run['id']
            directory.mkdir(parents=True)
            run = case.runtime.store.one('SELECT * FROM runs WHERE id=?', (case.run['id'],))
            run['path'] = str(directory)
            case.runtime.configure({'delivery': {'feedback_minutes': 5},
                                    'github': {'repository': 'example/project'}}, case.project)
            with patch('adaptive_rsi.delivery.baseline',
                       return_value=(str(case.repo), later, 'main')), \
                    patch.object(case.runtime.evaluator, 'evaluate', return_value=passing):
                feedback(case.runtime, case.runtime.project(case.project), run, outcome,
                         case.runtime.config(case.project), now)
            # The repaired receipt must survive a restart: reload it from the store.
            stored = json.loads(case.runtime.store.one(
                'SELECT result FROM runs WHERE id=?', (case.run['id'],))['result'])
            self.assertEqual(stored['feedback']['merge']['sha'], merged)
            with patch.object(release, 'api', side_effect=case.api):
                repaired = release.advance(case.runtime, case.runtime.project(case.project),
                                           case.run, stored)
            self.assertEqual(repaired['release']['state'], 'submitted')
            dispatch = [item for item in case.commands if '/dispatches' in item[0]]
            self.assertEqual(len(dispatch), 1)
            self.assertEqual(dispatch[0][2]['inputs[rsi_sha]'], merged)
        finally:
            case.tearDown()


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
