"""Bounded settings/data retention regressions; entirely offline."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from adaptive_rsi import maintenance
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

    def test_legacy_baseline_project_keys_are_retired(self):
        # baseline:<project> 旧版每次 fetch 覆写且无任何读取方；维护清理存量死键，
        # 但不能误伤相邻命名空间 run-baseline:/evaluation-baseline:。
        for key in ('baseline:p1', 'baseline:p2'):
            self.store.execute("INSERT INTO settings VALUES (?,?)",
                               (key, '{"sha":"base","branch":"main"}'))
        for key in ('run-baseline:r1', 'evaluation-baseline:scope'):
            self.store.execute("INSERT INTO settings VALUES (?,?)", (key, '{}'))

        from adaptive_rsi.runtime import Runtime
        Runtime(self.store, agent=object()).maintenance()

        for key in ('baseline:p1', 'baseline:p2'):
            self.assertIsNone(self.store.one("SELECT 1 FROM settings WHERE key=?", (key,)), key)
        for key in ('run-baseline:r1', 'evaluation-baseline:scope'):
            self.assertIsNotNone(self.store.one("SELECT 1 FROM settings WHERE key=?", (key,)), key)

    def test_expired_run_history_is_compacted_after_artifacts_are_removed(self):
        # 只删产物不会让 state.sqlite3 收缩：过期运行的 result 里带着完整检查
        # 输出，stages 里带着补丁，events 按 run 累积。产物确认删除后必须同时
        # 压缩这些行，否则库随运行数无界增长；顺序反了（先压缩后删产物）会在
        # 删除失败时永久丢掉证据。
        run_id = 'r-compact'
        directory = self.store.root / 'runs' / run_id
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'evidence.txt').write_text('x')
        blob = 'y' * 20000
        result = {"evaluation": {"decision": "fail",
                                 "reason": "Candidate failed independent checks",
                                 "checks": {"baseline": [{"name": "Python", "exit_code": 1,
                                                          "output": blob}]}},
                  "failure": {"code": "checks"}}
        ended = time.time() - 40 * 86400
        self.store.execute(
            "INSERT INTO runs(id,project,state,path,ended,result) VALUES (?,'p','failed',?,?,?)",
            (run_id, str(directory), ended, json.dumps(result)))
        self.store.execute(
            "INSERT INTO stages(run,stage,signature,data,created) VALUES (?,?,?,?,?)",
            (run_id, 'candidate', 'sig', blob, ended))
        self.store.execute(
            "INSERT INTO events(project,run,kind,message,data,created) VALUES ('p',?,?,?,?,?)",
            (run_id, 'failed', 'boom', '{}', ended))

        from adaptive_rsi.runtime import Runtime
        Runtime(self.store, agent=object()).maintenance()

        stored = self.store.one("SELECT result,path FROM runs WHERE id=?", (run_id,))
        self.assertIsNotNone(stored, '运行行保留，历史仍可查')
        payload = json.loads(stored['result'])
        self.assertTrue(payload.get('compacted'))
        # 保留 evaluation 这一层：dashboard 与 web 前端都从
        # result['evaluation'] 读决策和检查状态。
        self.assertEqual(payload['evaluation']['decision'], 'fail')
        self.assertEqual(payload['evaluation']['reason'],
                         'Candidate failed independent checks')
        self.assertNotIn('output', payload['evaluation']['checks']['baseline'][0])
        self.assertIsNone(self.store.one("SELECT 1 FROM stages WHERE run=?", (run_id,)))
        self.assertIsNone(self.store.one("SELECT 1 FROM events WHERE run=?", (run_id,)))

    def test_runs_whose_artifacts_were_already_removed_are_backfilled(self):
        # 旧版本删产物时会把 runs.path 置空；候选查询是 `path IS NOT NULL`，
        # 这些运行再也不会回到清理循环里，历史压缩必须单独回填，否则升级前
        # 删掉产物的一大批运行永远不会被压缩。
        run_id = 'r-retired'
        blob = 'q' * 20000
        result = {"evaluation": {"decision": "fail", "reason": "retired",
                                 "checks": {"baseline": [{"name": "Python", "exit_code": 1,
                                                          "output": blob}]}}}
        ended = time.time() - 40 * 86400
        self.store.execute(
            "INSERT INTO runs(id,project,state,path,ended,result) VALUES (?,'p','failed',NULL,?,?)",
            (run_id, ended, json.dumps(result)))
        self.store.execute(
            "INSERT INTO stages(run,stage,signature,data,created) VALUES (?,?,?,?,?)",
            (run_id, 'candidate', 'sig', blob, ended))

        from adaptive_rsi.runtime import Runtime
        Runtime(self.store, agent=object()).maintenance()

        payload = json.loads(self.store.one("SELECT result FROM runs WHERE id=?",
                                            (run_id,))['result'])
        self.assertTrue(payload.get('compacted'), '产物已删除的运行也要回填压缩')
        self.assertEqual(payload['evaluation']['reason'], 'retired')
        self.assertIsNone(self.store.one("SELECT 1 FROM stages WHERE run=?", (run_id,)))

    def test_history_is_not_compacted_when_artifact_removal_is_refused(self):
        # 删除被拒绝时 _remove_run_artifacts 返回 False（不抛异常）。此时产物还在
        # 磁盘上，压缩会让数据库里的证据先消失，留下半份记录。
        run_id = 'r-kept'
        directory = self.store.root / 'runs' / run_id
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'evidence.txt').write_text('x')
        (directory / 'unknown-leftover.txt').write_text('keeps the path')
        blob = 'z' * 20000
        result = {"evaluation": {"decision": "fail", "reason": "kept",
                                 "checks": {"baseline": [{"name": "Python", "exit_code": 1,
                                                          "output": blob}]}}}
        ended = time.time() - 40 * 86400
        self.store.execute(
            "INSERT INTO runs(id,project,state,path,ended,result) VALUES (?,'p','failed',?,?,?)",
            (run_id, str(directory), ended, json.dumps(result)))
        self.store.execute(
            "INSERT INTO stages(run,stage,signature,data,created) VALUES (?,?,?,?,?)",
            (run_id, 'candidate', 'sig', blob, ended))
        self.store.execute(
            "INSERT INTO events(project,run,kind,message,data,created) VALUES ('p',?,?,?,?,?)",
            (run_id, 'failed', 'boom', '{}', ended))

        from adaptive_rsi.runtime import Runtime
        Runtime(self.store, agent=object()).maintenance()

        payload = json.loads(self.store.one("SELECT result FROM runs WHERE id=?",
                                            (run_id,))['result'])
        self.assertNotIn('compacted', payload)
        self.assertIsNotNone(self.store.one("SELECT 1 FROM stages WHERE run=?", (run_id,)))
        self.assertIsNotNone(self.store.one("SELECT 1 FROM events WHERE run=?", (run_id,)))

    def test_deep_nested_dependency_tree_is_removed_without_recursion(self):
        # 深层嵌套（node_modules 式依赖树）超过默认递归上限；此前递归删除
        # 触发 RecursionError 且运行永久卡在保留清理里。目录构造与后续删除
        # 一样走 dir_fd 相对路径，避免绝对路径超出 macOS PATH_MAX。
        depth = 1200
        directory = self.store.root / 'runs' / 'r'
        (directory / 'workspace').mkdir(parents=True)
        (directory / 'workspace' / 'evidence').write_text('original')
        self.store.execute(
            "INSERT INTO runs(id,project,state,path,ended,result) VALUES ('r','p','failed',?,1,'{}')",
            (str(directory),))
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_text('must remain')
        (directory / 'dependencies').mkdir()
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(directory / 'dependencies', flags)
        try:
            for level in range(depth):
                os.mkdir('d', dir_fd=descriptor)
                deeper = os.open('d', flags, dir_fd=descriptor)
                if level:
                    # 除根目录外每层封成只读，覆盖依赖副本冻结读权限的删除路径；
                    # 下一层目录已经建好才封父目录，构造本身不需要上行。
                    os.chmod('..', 0o555, dir_fd=deeper)
                os.close(descriptor)
                descriptor = deeper
            fd = os.open('evidence.txt', os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=descriptor)
            os.close(fd)
            os.symlink(str(outside), 'link', dir_fd=descriptor)
        finally:
            os.close(descriptor)

        self.assertTrue(maintenance._remove_run_artifacts(self.store, 'r', 'p', directory))

        self.assertFalse((directory / 'dependencies').exists())
        self.assertFalse((directory / 'workspace').exists())
        row = self.store.one('SELECT path FROM runs WHERE id=?', ('r',))
        self.assertIsNotNone(row)
        self.assertIsNone(row['path'])
        # 深层只读树中的符号链接只被解除，不得沿链接修改或删除外部目标。
        self.assertEqual(sentinel.read_text(), 'must remain')
