"""Stable health observations apply to all endpoints and both delivery directions."""
import json
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_rsi.config import validate
from adaptive_rsi.release import deployment_health, health_sample, key, owner_key, stable_key
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class DeploymentObservationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.project, self.run = {'id': 'p'}, {'id': 'run', 'project': 'p'}
        self.policy = {'health_url': 'https://app.example/health', 'health_attempts': 5,
                       'health_consecutive_successes': 2, 'health_interval_seconds': 10}
        self.data = {'state': 'workflow_succeeded', 'repository': 'example/project', 'sha': 'b' * 40}
        authorization = patch('adaptive_rsi.release.authorized', return_value='example/project')
        authorization.start()
        self.addCleanup(authorization.stop)

    def tick(self, healthy, now):
        with patch('adaptive_rsi.release.time.time', return_value=now), patch('adaptive_rsi.release.health_check',
                return_value={'healthy': healthy, 'sha': self.data['sha']}):
            self.data = deployment_health(self.runtime, self.project, self.run, self.data, self.policy, None)
        return self.data

    def test_successes_are_consecutive_and_survive_reload(self):
        self.assertEqual(self.tick(True, 100)['state'], 'workflow_succeeded')
        self.data = self.store.setting(key('run', 'deploy'))
        self.assertEqual(self.data['health_successes'], 1)
        self.assertEqual(self.tick(False, 110)['health_successes'], 0)
        self.assertEqual(self.tick(True, 120)['state'], 'workflow_succeeded')
        self.assertEqual(self.tick(True, 130)['state'], 'healthy')
        stable = self.store.setting(stable_key('p', 'example/project', self.policy['health_url']))
        self.assertEqual(stable['sha'], self.data['sha'])
        self.assertEqual(len(self.data['health_observations']), 4)

    def test_unknown_breaks_consecutive_successes(self):
        self.tick(True, 100)
        self.tick(None, 110)
        self.assertEqual(self.tick(True, 120)['state'], 'workflow_succeeded')

    def test_warmup_does_not_consume_unknown_observation_window(self):
        self.policy['health_warmup_seconds'] = 900
        self.tick(None, 100)
        self.assertEqual(self.tick(None, 1000)['state'], 'workflow_succeeded')

    def test_direct_transport_error_is_unconfirmed(self):
        from adaptive_rsi.release import direct_health_check
        with patch('urllib.request.build_opener', side_effect=OSError('connection failed')):
            report = direct_health_check(self.policy['health_url'], self.data['sha'])
        self.assertIsNone(report['healthy'])

    def test_rollback_unknown_preserves_ownership(self):
        from adaptive_rsi.release import deployment_terminal
        self.data.update(state='rolling_back', rollback={'state': 'workflow_succeeded', 'sha': 'c' * 40})
        with patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data):
            self.tick(None, 100)
            result = self.tick(None, 1000)
        self.assertEqual(result['state'], 'rollback_health_unconfirmed')
        self.assertFalse(deployment_terminal(result, self.policy))

    def test_health_save_error_does_not_publish_partial_success(self):
        import copy
        self.policy['health_consecutive_successes'] = 1
        for state in ('workflow_succeeded', 'rolling_back'):
            data = {**self.data, 'state': state}
            if state == 'rolling_back':
                data['rollback'] = {'state': 'workflow_succeeded', 'sha': 'c' * 40}
            original = copy.deepcopy(data)
            calls = []
            def persist(runtime, run, kind, value, **kwargs):
                calls.append(kwargs)
                raise TypeError('injected internal error')
            with self.subTest(state=state), patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, value, **kwargs: value), \
                 patch('adaptive_rsi.release.health_check', return_value={'healthy': True}):
                with self.assertRaisesRegex(TypeError, 'injected internal error'):
                    deployment_health(self.runtime, self.project, self.run, data, self.policy, None, persist=persist)
            self.assertEqual(data, original)
            self.assertEqual(len(calls), 1)
            self.assertIn('stable', calls[0])

    def test_healthy_observation_clears_stale_rollback_warnings(self):
        self.policy['health_consecutive_successes'] = 1
        self.data.update(rollback_unavailable=True, rollback_blocked_reason='data_compatibility',
                         rollback_recovery_notes='stale warning', rollback_target={'sha': 'c' * 40})
        result = self.tick(True, 100)
        self.assertEqual(result['state'], 'healthy')
        for field in ('rollback_unavailable', 'rollback_blocked_reason', 'rollback_recovery_notes'):
            self.assertNotIn(field, result)
        self.assertEqual(result['rollback_target'], {'sha': 'c' * 40})

    def test_interval_skips_probes_and_exhaustion_is_failure_even_after_last_success(self):
        self.policy['health_attempts'] = 2
        self.tick(False, 100)
        with patch('adaptive_rsi.release.time.time', return_value=105), patch('adaptive_rsi.release.health_check') as probe:
            deployment_health(self.runtime, self.project, self.run, self.data, self.policy, None)
            probe.assert_not_called()
        result = self.tick(True, 110)
        self.assertEqual(result['state'], 'health_failed')
        self.assertTrue(result['health']['healthy'])
        self.assertIsNone(self.store.setting(stable_key('p', 'example/project', self.policy['health_url'])))

    def test_parallel_probes_inherit_context_and_unknown_is_bounded(self):
        from contextvars import ContextVar
        from adaptive_rsi.release import observe_health
        context = ContextVar('fixture_health_context', default=None)
        token = context.set('operation')
        try:
            self.policy['health_urls'] = ['https://eu.example/health']
            def probe(url, sha, timeout):
                self.assertEqual(context.get(), 'operation')
                return {'healthy': True}
            with patch('adaptive_rsi.release.health_check', side_effect=probe):
                self.assertTrue(health_sample(self.policy, 'b' * 40)['healthy'])
        finally:
            context.reset(token)
        data = {}
        for now in (100, 1000):
            state = observe_health(data, {'healthy': None}, self.policy, now)
        self.assertEqual(state, 'health_unconfirmed')
        self.assertLessEqual(len(data['health_observations']), 10)

    def test_health_transport_failure_is_unknown_and_can_be_retried(self):
        from adaptive_rsi.release import health_check
        with patch('adaptive_rsi.process.checked', side_effect=OSError('fixture unavailable')):
            report = health_check(self.policy['health_url'], self.data['sha'])
        self.assertIsNone(report['healthy'])
        self.data['state'] = 'health_unconfirmed'
        self.data.update(operation_id='fixture', health_started_at=1, health_retry_at=100, health_observations=[report])
        from adaptive_rsi.release import retry
        with patch.object(self.runtime.store, 'one', return_value={'id': 'run', 'result': '{}', 'config': json.dumps({'delivery': {'deploy': self.policy}})}), \
             patch.object(self.runtime, 'project', return_value=self.project), \
             patch('adaptive_rsi.release.authorized', return_value='example/project'), \
             patch('adaptive_rsi.release._read_automation', return_value=(dict(self.data), '{}')), \
             patch('adaptive_rsi.release._retry_target_allowed', return_value=True), \
             patch('adaptive_rsi.release.deployment_owner', return_value=None), \
             patch('adaptive_rsi.release.save'):
            result = retry(self.runtime, 'p', 'run', 'deploy')
        self.assertEqual(result['state'], 'workflow_succeeded')
        self.assertNotIn('health_started_at', result)

    def test_health_retry_rechecks_owner_after_validation(self):
        from adaptive_rsi.release import retry
        for state in ('health_unconfirmed', 'rollback_health_unconfirmed'):
            data = {**self.data, 'state': state, 'operation_id': 'fixture',
                    'rollback': {'state': 'workflow_succeeded', 'sha': 'c' * 40}}
            run = {'id': 'run', 'result': '{}', 'config': json.dumps({'delivery': {'deploy': self.policy}})}
            with self.subTest(state=state), patch.object(self.runtime.store, 'one', return_value=run), \
                 patch.object(self.runtime, 'project', return_value=self.project), \
                 patch('adaptive_rsi.release._read_automation', return_value=(data, '{}')), \
                 patch('adaptive_rsi.release._retry_target_allowed', return_value=True), \
                 patch('adaptive_rsi.release.deployment_owner', side_effect=[{'run': 'run'}, {'run': 'newer'}]), \
                 patch('adaptive_rsi.release.save') as save:
                with self.assertRaisesRegex(ValueError, 'newer deployment'):
                    retry(self.runtime, 'p', 'run', 'deploy')
                save.assert_not_called()

    def test_all_endpoints_must_agree_with_shared_deadline(self):
        self.policy['health_urls'] = ['https://eu.example/health', 'https://us.example/health']
        def probe(url, sha, timeout):
            self.assertLessEqual(timeout, 15)
            return {'url': url, 'sha': sha, 'healthy': 'eu.' not in url}
        with patch('adaptive_rsi.release.health_check', side_effect=probe) as check:
            report = health_sample(self.policy, 'b' * 40)
        self.assertFalse(report['healthy'])
        self.assertEqual(len(report['checks']), 3)
        self.assertEqual(check.call_count, 3)
        barrier = threading.Barrier(3)
        def simultaneous(url, sha, timeout):
            barrier.wait(timeout=1)
            return {'url': url, 'healthy': True}
        with patch('adaptive_rsi.release.health_check', side_effect=simultaneous) as check:
            report = health_sample(self.policy, 'b' * 40)
        self.assertEqual(check.call_count, 3)
        self.assertTrue(report['healthy'])
        self.assertEqual(len(report['checks']), 3)

    def test_rollback_requires_same_consecutive_observation(self):
        self.data.update(state='rolling_back', rollback={'state': 'workflow_succeeded', 'sha': 'c' * 40})
        with patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data):
            self.assertEqual(self.tick(True, 100)['state'], 'rolling_back')
            self.assertEqual(self.tick(False, 110)['rollback']['health_successes'], 0)
            self.assertEqual(self.tick(True, 120)['state'], 'rolling_back')
            self.assertEqual(self.tick(True, 130)['state'], 'rolled_back')

    def test_rollback_owner_change_during_probe_discards_evidence(self):
        self.data.update(state='rolling_back', rollback={'state': 'workflow_succeeded', 'sha': 'c' * 40})
        def probe(url, sha):
            self.store.put_setting(owner_key('p', 'example/project', self.policy), {'run': 'newer', 'sha': 'd' * 40})
            return {'healthy': True, 'sha': sha}
        with patch('adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data), patch(
                'adaptive_rsi.release.health_check', side_effect=probe):
            result = deployment_health(self.runtime, self.project, self.run, self.data, self.policy, None)
        self.assertEqual(result['state'], 'superseded')
        self.assertNotIn('health', result['rollback'])

    def test_invalid_policies_are_rejected_and_legacy_defaults_remain(self):
        for policy in [dict(health_consecutive_successes=2), dict(health_consecutive_successes=0),
                       dict(health_consecutive_successes=True), dict(health_urls=['http://unsafe']),
                       dict(health_urls=['https://u:p@host/health']), dict(health_urls=['https://host/?token=1']),
                       dict(health_urls=['https://host/'] * 2), dict(health_urls=['https://host/'+str(i) for i in range(8)])]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate({'delivery': {'deploy': {'health_url': self.policy['health_url'], **policy}}})
        with self.assertRaises(ValueError):
            validate({'delivery': {'deploy': {'health_urls': ['https://other/health']}}})
        self.assertEqual(validate({})['delivery']['deploy']['health_consecutive_successes'], 1)
        with patch('adaptive_rsi.release.health_check', return_value={'healthy': True}) as probe:
            health_sample({'health_url': 'https://app.example/health'}, 'b' * 40)
        probe.assert_called_once_with('https://app.example/health', 'b' * 40)

    def test_undeclared_or_incompatible_migration_never_dispatches_rollback(self):
        self.data.update(state='health_failed', rollback_target={'sha': 'c' * 40})
        self.policy.update(automatic_rollback=True, rollback_recovery_notes='Restore the schema backup before deploying the compatible binary.')
        for policy in ({**self.policy}, {**self.policy, 'rollback_data_policy': 'manual'}):
            data = json.loads(json.dumps(self.data))
            with self.subTest(policy=policy), patch('adaptive_rsi.release.api') as dispatch, patch('adaptive_rsi.release.validate_pin') as pin:
                result = deployment_health(self.runtime, self.project, self.run, data, policy, None)
            self.assertEqual(result['state'], 'health_failed')
            self.assertTrue(result['rollback_unavailable'])
            self.assertEqual(result['rollback_blocked_reason'], 'data_compatibility')
            self.assertIn('schema backup', result['rollback_recovery_notes'])
            dispatch.assert_not_called()
            pin.assert_not_called()

    def test_explicit_compatible_policy_permits_the_authorized_rollback(self):
        self.data.update(state='health_failed', rollback_target={'sha': 'c' * 40},
                         workflow='.github/workflows/deploy.yml', ref='trusted', workflow_sha='a' * 40,
                         operation_id='deployment-operation')
        self.policy.update(automatic_rollback=True, rollback_data_policy='backward_compatible',
                           workflow='.github/workflows/deploy.yml', ref='trusted', sha='a' * 40)
        with patch('adaptive_rsi.release.api') as dispatch, patch('adaptive_rsi.release.validate_pin'), patch(
                'adaptive_rsi.release.reconcile', side_effect=lambda runtime, run, kind, data, **kwargs: data):
            result = deployment_health(self.runtime, self.project, self.run, self.data, self.policy, None)
        self.assertEqual(result['state'], 'rolling_back')
        self.assertEqual(result['rollback']['sha'], 'c' * 40)
        dispatch.assert_called_once()

    def test_migration_declaration_and_recovery_notes_are_validated(self):
        for policy in [{'rollback_data_policy': 'assume_safe'}, {'rollback_recovery_notes': 'x' * 2001}]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate({'delivery': {'deploy': policy}})
        self.assertEqual(validate({})['delivery']['deploy']['rollback_data_policy'], 'manual')
