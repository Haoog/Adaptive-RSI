"""Bounded settings/data retention regressions; entirely offline."""
import tempfile
import time
import unittest
from pathlib import Path

from adaptive_rsi.store import Store


class CleanupRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.store.execute("INSERT INTO projects VALUES ('p','test','/unused','{}',0,0)")

    def test_terminal_task_pr_feedback_and_handled_keys_are_retired(self):
        # pr-feedback:/handled: 键按任务前缀累积；任务终态超过保留期后由维护清理，
        # 而仍处于活跃窗口或 pr_open 的任务前缀必须完整保留（supersede 判读依赖）。
        from adaptive_rsi.delivery import _pr_feedback_prefix
        stale = time.time() - 91 * 86400
        for task_id, state, updated, expected in (
                ('t-done', 'merged', stale, False),      # 终态且超期：清理
                ('t-open', 'pr_open', stale, True),      # 未终态：保留
                ('t-fresh', 'merged', time.time(), True)):  # 终态但在保留期内：保留
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p',?,0,?)", (task_id, state, updated))
            prefix = _pr_feedback_prefix('p', task_id)
            self.store.execute("INSERT INTO settings VALUES (?,?)", (prefix + 'obs1', '{}'))
            self.store.execute("INSERT INTO settings VALUES (?,?)", ('handled:' + prefix + 'obs1', '1'))
        # 不属于本任务命名空间的对照键永不受任务清理影响。
        self.store.execute("INSERT INTO settings VALUES ('handled:unrelated','1')")
        self.store.execute("INSERT INTO settings VALUES ('pr-feedback:other:obs','{}')")

        from adaptive_rsi.runtime import Runtime
        rt = Runtime(self.store, agent=object())
        rt.maintenance()

        for task_id, expected in (('t-done', False), ('t-open', True), ('t-fresh', True)):
            prefix = _pr_feedback_prefix('p', task_id)
            self.assertEqual(bool(self.store.one(
                "SELECT 1 FROM settings WHERE key=?", (prefix + 'obs1',))), expected, task_id)
            self.assertEqual(bool(self.store.one(
                "SELECT 1 FROM settings WHERE key=?", ('handled:' + prefix + 'obs1',))), expected,
                'handled:' + task_id)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM settings WHERE key='handled:unrelated'"))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM settings WHERE key='pr-feedback:other:obs'"))

    def test_pr_feedback_retirement_cursor_covers_backlog_beyond_batch(self):
        # 超过 200 个终态任务带键时游标逐批推进；无键任务不占配额。201 个带键
        # 任务两轮内应全部清理（旧实现 LIMIT 200 会永远留下最后一个）。
        from adaptive_rsi.delivery import _pr_feedback_prefix
        stale = time.time() - 91 * 86400
        total = 201
        for index in range(total):
            task_id = 't-backlog-%03d' % index
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p','merged',0,?)", (task_id, stale))
            prefix = _pr_feedback_prefix('p', task_id)
            self.store.execute("INSERT INTO settings VALUES (?,?)", (prefix + 'obs', '{}'))
        # 200 个无键任务穿插其中：它们不占每轮 200 的清理配额。
        for index in range(200):
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) "
                               "VALUES (?,'p','merged',0,?)", ('t-keyless-%03d' % index, stale))

        from adaptive_rsi.runtime import Runtime
        rt = Runtime(self.store, agent=object())

        def remaining():
            return self.store.one("SELECT COUNT(*) AS n FROM settings WHERE key LIKE 'pr-feedback:%'")["n"]

        rt.maintenance()
        self.assertGreater(remaining(), 0)          # 一轮最多 200 个带键任务
        self.store.put_setting('maintenance-last', 0)
        rt.maintenance()
        self.assertEqual(remaining(), 0)            # 游标推进后剩余任务全部被覆盖
