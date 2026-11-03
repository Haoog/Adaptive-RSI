import json
from unittest.mock import patch

from adaptive_rsi import delivery
from tests import test_release_endpoints


class SupersededRecoveryTest(test_release_endpoints.DeliveryRecoveryTest):
    def prepare(self):
        self.store.execute("UPDATE runs SET state='superseded',path=? WHERE id=?", ('/missing', self.run['id']))
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.task['id'],))
        self.store.put_setting('delivery-push:' + self.run['id'], {
            'repository': 'example/project', 'branch': 'feat/rsi-' + self.run['id'],
            'head': self.head, 'base': self.run['base']})

    def test_scheduler_recovers_receipt_without_touching_requeued_task(self):
        self.prepare()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        before = self.store.one('SELECT * FROM tasks WHERE id=?', (self.task['id'],))
        with patch.object(delivery, 'checked', return_value=json.dumps([pr])) as command, \
             patch.object(delivery, 'github_git', side_effect=AssertionError('push')):
            delivery.schedule(self.runtime, now=10000000000)
            scheduled = self.store.one("SELECT * FROM tasks WHERE source='system_delivery' AND project=?", (self.project,))
            self.assertIsNotNone(scheduled)
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertEqual(result['pr'], self.url)
        self.assertTrue(result['close_pending'])
        self.assertEqual(self.store.setting('delivery:' + self.run['id'])['head'], self.head)
        self.assertEqual(self.store.one('SELECT * FROM tasks WHERE id=?', (self.task['id'],)), before)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][1:3], ['pr', 'list'])

    def test_empty_result_is_retried_after_interval_and_recovers_later_pr(self):
        self.prepare()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        with patch.object(delivery, 'checked', side_effect=[json.dumps([]), json.dumps([pr])]) as command:
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
            delivery.tick(self.runtime, now=10000000100, project_id=self.project)
            self.assertEqual(command.call_count, 1)
            delivery.tick(self.runtime, now=10000003600, project_id=self.project)
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertEqual(result['pr'], self.url)
        self.assertTrue(result['close_pending'])
        self.assertEqual(command.call_count, 2)

    def test_legacy_empty_check_is_retried_by_scheduler(self):
        self.prepare()
        self.store.execute('UPDATE runs SET result=? WHERE id=?',
                           (json.dumps({'delivery_recovery_checked': True}), self.run['id']))
        pr = {'url': self.url, 'state': 'MERGED', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        with patch.object(delivery, 'checked', return_value=json.dumps([pr])):
            delivery.schedule(self.runtime, now=10000000000)
            self.assertIsNotNone(self.store.one("SELECT id FROM tasks WHERE source='system_delivery' AND project=?", (self.project,)))
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertEqual(result['pr'], self.url)
        self.assertEqual(result['close_status'], 'merged')
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.task['id'],))['state'], 'queued')

    def test_no_pr_or_mismatched_pr_waits_without_remote_writes(self):
        for records in ([], [{'url': 'https://evil.test/pull/1', 'state': 'OPEN',
                              'headRefOid': self.head, 'headRefName': 'feat/rsi-' + self.run['id'],
                              'baseRefName': 'main', 'headRepositoryOwner': {'login': 'example'}}]):
            with self.subTest(records=records):
                self.prepare()
                self.store.execute('UPDATE runs SET result=? WHERE id=?', (self.run['result'], self.run['id']))
                with patch.object(delivery, 'checked', return_value=json.dumps(records)) as command:
                    delivery.tick(self.runtime, now=10000000000, project_id=self.project)
                    delivery.tick(self.runtime, now=10000000100, project_id=self.project)
                result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
                self.assertEqual(result['delivery_recovery_at'], 10000000000)
                self.assertNotIn('pr', result)
                self.assertEqual(command.call_count, 1)
                self.store.put_setting('delivery-tick:' + self.project, 0)

    def test_closed_and_merged_facts_survive_without_closing(self):
        for state in ('CLOSED', 'MERGED'):
            with self.subTest(state=state):
                self.prepare()
                self.store.execute('UPDATE runs SET result=? WHERE id=?', (self.run['result'], self.run['id']))
                pr = {'url': self.url, 'state': state, 'headRefOid': self.head,
                      'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
                      'headRepositoryOwner': {'login': 'example'}}
                with patch.object(delivery, 'checked', return_value=json.dumps([pr])) as command:
                    delivery.tick(self.runtime, now=10000000000, project_id=self.project)
                    delivery.tick(self.runtime, now=10000000100, project_id=self.project)
                result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
                self.assertEqual(result['close_status'], state.lower())
                self.assertFalse(result['close_pending'])
                self.assertEqual(command.call_count, 1)
                self.store.put_setting('delivery-tick:' + self.project, 0)

    def test_failure_retries_and_paused_project_is_not_queried(self):
        self.prepare()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        with patch.object(delivery, 'checked', side_effect=[RuntimeError('offline'), json.dumps([pr])]) as command:
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
            self.assertNotIn('delivery_recovery_checked', json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result']))
            # 远端读取失败也必须退避，不能每个调度周期重试。
            delivery.tick(self.runtime, now=10000000100, project_id=self.project)
            self.assertEqual(command.call_count, 1)
            self.runtime.pause(self.project, True)
            delivery.tick(self.runtime, now=10000000100, project_id=self.project)
            self.assertEqual(command.call_count, 1)
            self.runtime.pause(self.project, False)
            delivery.tick(self.runtime, now=10000009000, project_id=self.project)
        self.assertEqual(json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])['pr'], self.url)

    def test_close_rechecks_identity_and_preserves_task(self):
        self.prepare()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        before = self.store.one('SELECT * FROM tasks WHERE id=?', (self.task['id'],))
        with patch.object(delivery, 'checked', return_value=json.dumps([pr])):
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        with patch.object(delivery, 'pr_view', return_value=dict(pr, headRefOid='b' * 40)), \
                patch.object(delivery, 'checked') as command:
            delivery.tick(self.runtime, now=10000000100, project_id=self.project)
            command.assert_not_called()
        with patch.object(delivery, 'pr_view', return_value=pr), \
                patch.object(delivery, 'checked', return_value='') as command:
            delivery.tick(self.runtime, now=10000000200, project_id=self.project)
            self.assertEqual(command.call_args.args[0][1:3], ['pr', 'close'])
        self.assertFalse(json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])['close_pending'])
        self.assertEqual(self.store.one('SELECT * FROM tasks WHERE id=?', (self.task['id'],)), before)

    def test_untrusted_intent_never_queries_remote(self):
        self.prepare()
        self.store.put_setting('delivery-push:' + self.run['id'], {
            'repository': 'attacker/project', 'branch': 'feat/rsi-' + self.run['id'],
            'head': self.head, 'base': self.run['base']})
        with patch.object(delivery, 'checked') as command:
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
            command.assert_not_called()

    def test_pause_during_close_observation_prevents_close(self):
        self.prepare()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        with patch.object(delivery, 'checked', return_value=json.dumps([pr])):
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        def pause(*args):
            self.runtime.pause(self.project, True)
            return pr
        with patch.object(delivery, 'pr_view', side_effect=pause), patch.object(delivery, 'checked') as command:
            delivery.tick(self.runtime, now=10000000100, project_id=self.project)
            command.assert_not_called()
        self.assertTrue(json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])['close_pending'])
