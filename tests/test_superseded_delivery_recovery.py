import json
import time
from unittest.mock import patch

from adaptive_rsi import delivery, release
from adaptive_rsi.process import ProcessError
from tests import test_release_endpoints


class SupersededRecoveryTest(test_release_endpoints.DeliveryRecoveryTest):
    def prepare(self):
        self.store.execute("UPDATE runs SET state='superseded',path=? WHERE id=?", ('/missing', self.run['id']))
        self.store.execute("UPDATE tasks SET state='queued' WHERE id=?", (self.task['id'],))
        self.store.put_setting('delivery-push:' + self.run['id'], {
            'repository': 'example/project', 'branch': 'feat/rsi-' + self.run['id'],
            'head': self.head, 'base': self.run['base']})

    def pending_close(self):
        self.prepare()
        self.store.execute("UPDATE runs SET result=? WHERE id=?",
                           (json.dumps({'pr': self.url, 'replanned': True, 'close_pending': True}), self.run['id']))
        self.store.put_setting('delivery:' + self.run['id'], {
            'repository': 'example/project', 'branch': 'main', 'base': self.run['base'], 'head': self.head})

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
        # 已合入的改动不再重新实现：任务随恢复对账进入终态。
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.task['id'],))['state'], 'merged')
        self.assertEqual(result['delivery_status'], 'merged')

    def test_recovered_merge_resumes_postmerge_verification_and_release(self):
        self.prepare()
        merged_sha = 'c' * 40
        pr = {'url': self.url, 'state': 'MERGED', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}, 'mergeCommit': {'oid': merged_sha}}
        with patch.object(delivery, 'checked', return_value=json.dumps([pr])):
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        saved = self.store.one('SELECT state,result FROM runs WHERE id=?', (self.run['id'],))
        result = json.loads(saved['result'])
        self.assertEqual(result['merged_sha'], merged_sha)
        self.assertFalse(result['close_pending'])
        # 只记合并事实而停在 superseded 时，该运行退出后续扫描：同一段代码进了
        # 主线却没有合并后验证回执，release/deploy 也永远不推进。
        self.assertEqual(saved['state'], 'merged')
        with patch.object(delivery, 'checked') as command, \
                patch.object(delivery, 'feedback') as feedback, \
                patch.object(release, 'advance', return_value={}) as advance:
            delivery.tick(self.runtime, now=10000000600, project_id=self.project)
        command.assert_not_called()
        feedback.assert_called_once()
        advance.assert_called_once()

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
        # 失配后短期退避，不再每个调度周期查询同一个无法关闭的 PR。
        with patch.object(delivery, 'pr_view') as view:
            delivery.tick(self.runtime, now=10000000200, project_id=self.project)
            view.assert_not_called()
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE kind='delivery_close_blocked'"))
        with patch.object(delivery, 'pr_view', return_value=pr), \
                patch.object(delivery, 'checked', return_value='') as command:
            delivery.tick(self.runtime, now=10000004000, project_id=self.project)
            self.assertEqual(command.call_args.args[0][1:3], ['pr', 'close'])
        self.assertFalse(json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])['close_pending'])
        self.assertEqual(self.store.one('SELECT * FROM tasks WHERE id=?', (self.task['id'],)), before)

    def test_close_failure_backs_off_and_abandons_after_bounded_attempts(self):
        self.pending_close()
        pr = {'url': self.url, 'state': 'OPEN', 'headRefOid': self.head,
              'headRefName': 'feat/rsi-' + self.run['id'], 'baseRefName': 'main',
              'headRepositoryOwner': {'login': 'example'}}
        now = 10000000000
        with patch.object(delivery, 'CLOSE_ATTEMPT_LIMIT', 3), \
                patch.object(delivery, 'pr_view', return_value=pr) as view, \
                patch.object(delivery, 'checked', side_effect=ProcessError({'exit_code': 1, 'stderr': 'permission denied'})) as close:
            delivery.tick(self.runtime, now=now, project_id=self.project)
            result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
            # 尝试先落盘：失败后 run 仍待关闭，但带上了尝试次数与退避时间。
            self.assertEqual(result['close_attempts'], 1)
            self.assertEqual(result['close_retry_at'], now)
            self.assertTrue(result['close_pending'])
            self.assertEqual((view.call_count, close.call_count), (1, 1))
            # 退避窗口内的连续 tick 不再发起远端读或失败写入。
            delivery.tick(self.runtime, now=now + 100, project_id=self.project)
            delivery.tick(self.runtime, now=now + 200, project_id=self.project)
            self.assertEqual((view.call_count, close.call_count), (1, 1))
            delivery.tick(self.runtime, now=now + 300, project_id=self.project)
            self.assertEqual((view.call_count, close.call_count), (2, 2))
            # 第二次失败后间隔翻倍到 600s。
            delivery.tick(self.runtime, now=now + 800, project_id=self.project)
            self.assertEqual((view.call_count, close.call_count), (2, 2))
            delivery.tick(self.runtime, now=now + 900, project_id=self.project)
            self.assertEqual((view.call_count, close.call_count), (3, 3))
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertFalse(result['close_pending'])
        self.assertEqual(result['close_attempts'], 3)
        self.assertEqual(result['close_retry_at'], now + 900)
        self.assertIn('3 attempts', result['close_abandoned'])
        # 前两次失败各留一条 delivery_error；达到上限只记一条终止事件。
        self.assertEqual(len(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='delivery_error'",
                                              (self.run['id'],))), 2)
        self.assertEqual(len(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='delivery_close_abandoned'",
                                              (self.run['id'],))), 1)
        # 终态后退出轮询：后续 tick 既不查询远端也不再写失败调用。
        with patch.object(delivery, 'pr_view') as view, patch.object(delivery, 'checked') as close:
            delivery.tick(self.runtime, now=now + 1200, project_id=self.project)
            delivery.tick(self.runtime, now=now + 90000, project_id=self.project)
        view.assert_not_called()
        close.assert_not_called()
        self.assertEqual(self.store.one('SELECT state FROM runs WHERE id=?', (self.run['id'],))['state'], 'superseded')
        self.assertEqual(len(self.store.query("SELECT 1 FROM events WHERE run=? AND kind='delivery_close_abandoned'",
                                              (self.run['id'],))), 1)

    def test_bounded_attempts_abandon_recovery_and_release_keys_to_cleanup(self):
        self.prepare()
        with patch.object(delivery, 'RECOVERY_ATTEMPT_LIMIT', 3), \
                patch.object(delivery, 'checked', return_value='[]') as command:
            for step in range(3):
                delivery.tick(self.runtime, now=10000000000 + step * 3600, project_id=self.project)
            result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
            self.assertEqual(result['delivery_recovery_attempts'], 3)
            self.assertIn('exhausted', result['delivery_recovery_abandoned'])
            # 第 3 次尝试在远端读取前就达到上限：只查询 2 次后落终态。
            self.assertEqual(command.call_count, 2)
            # 终态后退出恢复候选：不再发起远端查询。
            for step in range(3, 5):
                delivery.tick(self.runtime, now=10000000000 + step * 3600, project_id=self.project)
            self.assertEqual(command.call_count, 2)
            delivery.schedule(self.runtime, now=10000018000)
            self.assertIsNone(self.store.one("SELECT 1 FROM tasks WHERE source='system_delivery' AND project=? "
                                             "AND state IN ('queued','planning','executing','evaluating','waiting_budget')",
                                             (self.project,)))
        # 已放弃的运行不再占用清理豁免：delivery-push 等键随保留期回收。
        self.assertIsNotNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                            ('delivery-push:' + self.run['id'],)))
        self.store.execute('UPDATE runs SET ended=? WHERE id=?', (time.time() - 91 * 86400, self.run['id']))
        self.runtime.maintenance()
        self.assertIsNone(self.store.one('SELECT 1 FROM settings WHERE key=?',
                                         ('delivery-push:' + self.run['id'],)))

    def test_corrupt_push_journal_is_terminal_after_one_event(self):
        self.prepare()
        self.store.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                           ('delivery-push:' + self.run['id'], '{not json'))
        with patch.object(delivery, 'checked') as command:
            for step in range(3):
                delivery.tick(self.runtime, now=10000000000 + step * 3600, project_id=self.project)
        command.assert_not_called()
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertIn('journal', result['delivery_recovery_abandoned'])
        self.assertNotIn('delivery_recovery_attempts', result)
        events = self.store.query("SELECT kind FROM events WHERE kind='recovery_blocked' AND run=?",
                                  (self.run['id'],))
        self.assertEqual(len(events), 1)

    def test_missing_baseline_identity_blocks_before_counting_or_query(self):
        self.prepare()
        self.store.execute('DELETE FROM settings WHERE key=?', ('run-baseline:' + self.run['id'],))
        with patch.object(delivery, 'checked') as command:
            for step in range(3):
                delivery.tick(self.runtime, now=10000000000 + step * 3600, project_id=self.project)
        command.assert_not_called()
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        # 未发出任何查询：既不计次，也不能留下「查了 72 次都没找到」的假终态。
        self.assertNotIn('delivery_recovery_attempts', result)
        self.assertIn('baseline identity', result['delivery_recovery_abandoned'])
        events = self.store.query("SELECT kind FROM events WHERE kind='recovery_blocked' AND run=?",
                                  (self.run['id'],))
        self.assertEqual(len(events), 1)

    def test_query_attempt_is_counted_only_when_remote_is_read(self):
        self.prepare()
        with patch.object(delivery, 'checked', return_value='[]') as command:
            delivery.tick(self.runtime, now=10000000000, project_id=self.project)
        result = json.loads(self.store.one('SELECT result FROM runs WHERE id=?', (self.run['id'],))['result'])
        self.assertEqual(result['delivery_recovery_attempts'], 1)
        self.assertNotIn('delivery_recovery_abandoned', result)
        self.assertEqual(command.call_count, 1)

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
