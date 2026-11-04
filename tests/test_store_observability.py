"""Measure real contention and preserve orphan evidence under bounded maintenance."""
import concurrent.futures
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.store import Store


class StoreObservabilityTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.addCleanup(self.store.close)

    def test_slow_sql_has_context_but_no_bound_values_and_excludes_pool_wait(self):
        secret = 'PRIVATE_BINDING_SHOULD_NOT_APPEAR'
        with self.store.connect() as db:
            db.create_function('slow_fixture', 1, lambda value: time.sleep(.12) or 1)
        with self.store.query_scope(request='request-123'):
            with self.store.query_scope(run='run-456', task='task-789'):
                self.store.query('SELECT slow_fixture(?) AS result', (secret,))
        self.store.query('SELECT slow_fixture(?)', (secret,))
        rows = self.store.metrics()['slow_queries']
        self.assertEqual(rows[-2]['context'], {'request': 'request-123', 'run': 'run-456', 'task': 'task-789'})
        self.assertEqual(rows[-1]['context'], {})
        self.assertGreaterEqual(rows[-1]['seconds'], .1)
        self.assertGreaterEqual(rows[-1]['total_seconds'], rows[-1]['seconds'])
        self.assertNotIn(secret, json.dumps(rows))
        self.assertNotIn('slow_fixture', json.dumps(rows))
        self.store._connection_capacity = threading.BoundedSemaphore(1)
        held, release = threading.Event(), threading.Event()
        def hold():
            with self.store.connect():
                held.set()
                release.wait(2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(hold)
            self.assertTrue(held.wait(1))
            pending = pool.submit(self.store.query, 'SELECT 1')
            time.sleep(.15)
            release.set()
            pending.result(timeout=2)
            holder.result(timeout=2)
        self.assertEqual(len(self.store.metrics()['slow_queries']), 2)

    def test_saturation_quantiles_include_actual_waits(self):
        self.store._connection_capacity = threading.BoundedSemaphore(1)
        gate = threading.Event()
        def borrow():
            gate.set()
            began = time.monotonic()
            with self.store.connect():
                return time.monotonic() - began
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            with self.store.connect():
                pending = [pool.submit(borrow) for _ in range(4)]
                self.assertTrue(gate.wait(1))
                time.sleep(.08)
            waits = [future.result(timeout=2) for future in pending]
        metrics = self.store.metrics()
        self.assertGreaterEqual(metrics['wait_p95_seconds'], .06)
        self.assertLessEqual(metrics['wait_p95_seconds'], max(waits) + .03)
        self.assertEqual(metrics['wait_quantile'], 'nearest_rank')
        self.assertEqual(metrics['borrowed'], 0)

    def test_timeouts_are_in_wait_samples_and_not_only_the_success_population(self):
        class ShortBudget:
            def acquire(self, timeout):
                time.sleep(.05)
                return False
        prior_samples = len(self.store._wait_samples)
        began = time.monotonic()
        with patch.object(self.store, '_connection_capacity', ShortBudget()):
            with self.assertRaises(sqlite3.OperationalError):
                with self.store.connect():
                    self.fail('capacity must remain closed')
        elapsed = time.monotonic() - began
        metrics = self.store.metrics()
        self.assertEqual(metrics['timeouts'], 1)
        self.assertEqual(len(self.store._wait_samples), prior_samples + 2)
        self.assertAlmostEqual(metrics['wait_p95_seconds'], elapsed, delta=.025)
        self.assertEqual(metrics['waiting'], 0)
        self.assertEqual(metrics['borrowed'], 0)

    def test_stage_quarantine_is_bounded_preserves_every_column_and_handles_nulls(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)',
                           [('missing', str(index), 'signature'+str(index), json.dumps({'original': index}), index) for index in range(205)] +
                           [(None, None, 'null-one', 'one', 1), (None, None, 'null-two', 'two', 2)])
        original = self.store.query('SELECT * FROM stages')
        first = self.store.quarantine_orphans()
        self.assertEqual(first['stages_without_run'], 100)
        self.assertTrue(first['has_more'])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM stages')['n'], 107)
        self.store.quarantine_orphans()
        last = self.store.quarantine_orphans()
        self.assertFalse(last['has_more'])
        restored = [json.loads(row['data']) for row in self.store.query('SELECT data FROM relationship_quarantine')]
        self.assertCountEqual(restored, original)
        self.assertFalse(self.store.query('SELECT * FROM stages'))

    def test_failed_quarantine_rolls_back_every_removed_row(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("INSERT INTO stages VALUES ('missing','one','sig','data',1)")
        self.store.execute("CREATE TRIGGER quarantine_failure BEFORE INSERT ON relationship_quarantine BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.quarantine_orphans()
        self.assertEqual(self.store.one('SELECT data FROM stages')['data'], 'data')
        self.assertFalse(self.store.query('SELECT * FROM relationship_quarantine'))

    def test_dependency_quarantine_restores_readiness_and_preserves_evidence(self):
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',1,1)")
        task = self.store.add_task('p', 'goal', 'dep-key', 'title', 'body')
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute("INSERT INTO task_dependencies VALUES (?,'deleted-dep')", (task,))
            db.execute("INSERT INTO task_dependencies VALUES ('ghost-task','ghost-dep')")
        ready = 'SELECT id FROM tasks WHERE id=? AND ' + self.store.ready_clause()
        # p.id IS NULL 分支把任务永久挡在 ready 之外。
        self.assertIsNone(self.store.one(ready, (task, 0)))
        result = self.store.quarantine_orphans()
        self.assertEqual(result['invalid_dependencies'], 2)
        self.assertFalse(result['has_more'])
        self.assertFalse(self.store.query('SELECT * FROM task_dependencies'))
        rows = self.store.query("SELECT table_name,data FROM relationship_quarantine ORDER BY id")
        self.assertEqual([row['table_name'] for row in rows], ['task_dependencies'] * 2)
        evidence = sorted((json.loads(row['data'])['task'], json.loads(row['data'])['dependency'])
                          for row in rows)
        self.assertEqual(evidence, sorted([(task, 'deleted-dep'), ('ghost-task', 'ghost-dep')]))
        events = self.store.query("SELECT project,data FROM events "
                                  "WHERE kind='dependency_orphan_quarantined' ORDER BY seq")
        self.assertEqual(len(events), 2)
        by_task = {json.loads(row['data'])['task']: row for row in events}
        self.assertEqual(by_task[task]['project'], 'p')
        self.assertIsNone(by_task['ghost-task']['project'])
        self.assertEqual(self.store.one(ready, (task, 0))['id'], task)
        again = self.store.quarantine_orphans()
        self.assertEqual(again['invalid_dependencies'], 0)
        self.assertFalse(again['has_more'])

    def test_relationship_audit_covers_evidence_links_and_narrow_probe_exceptions(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for project in ('p', 'q'):
                db.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,1)", (project, project, '/' + project))
            for task, project in (('t1','p'), ('t2','q'), ('bad-task','missing')):
                db.execute("INSERT INTO tasks(id,project,state) VALUES (?,?,'queued')", (task, project))
            db.execute("INSERT INTO runs(id,task,project) VALUES ('r','t1','q')")
            db.execute("INSERT INTO runs(id,task,project) VALUES ('orphan-run','missing-task','missing')")
            db.execute("INSERT INTO events(project,run) VALUES ('missing','missing-run')")
            db.execute("INSERT INTO events(project,run) VALUES ('model-center','model-test-123')")
            db.execute("INSERT INTO events(project,run) VALUES ('p','r')")
            db.execute("INSERT INTO events(project,run) VALUES (NULL,'r')")
            db.execute("INSERT INTO candidates(id,project,parent,kind) VALUES ('candidate','missing','missing-parent','strategy')")
            db.execute("INSERT INTO cases(id,project) VALUES ('case','missing')")
            db.execute("INSERT INTO skills(id,project,task) VALUES ('skill','missing','t2')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('call','missing','missing-run')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('mismatch','p','r')")
            db.execute("INSERT INTO calls(id,project,run) VALUES ('probe','model-center','model-test-123')")
            db.execute("INSERT INTO schedules(project,kind,due) VALUES ('missing','light',1)")
            db.execute("INSERT INTO task_decisions VALUES ('missing-task','{}',1)")
            db.execute("INSERT INTO task_revisions(task) VALUES ('missing-task')")
            db.execute("INSERT INTO task_revision_snapshots VALUES (999,'body','{}')")
            db.execute("INSERT INTO task_dependencies VALUES ('t1','t2')")
            db.execute("INSERT INTO task_group_members(group_task,original_task) VALUES ('t1','t2')")
            db.execute("INSERT INTO submissions VALUES ('p','request','signature','t2')")
            db.execute("INSERT INTO task_completions VALUES ('t1','q',1)")
            db.execute("INSERT INTO phase_times(run) VALUES ('missing-run')")
            db.execute("INSERT INTO remote_waits(run) VALUES ('missing-run')")
            db.execute("INSERT INTO stages VALUES ('missing-run','phase','sig','{}',1)")
        before = self.store.query('SELECT * FROM tasks')
        report = self.store.relationship_audit()
        for key, values in report.items():
            if not key.startswith('_'):
                self.assertTrue(values, key)
        self.assertEqual(len(report['events_without_project']), 1)
        self.assertEqual(len(report['event_project_mismatch']), 1)
        self.assertNotIn('probe', report['calls_without_project'])
        self.assertNotIn('probe', report['calls_without_run'])
        self.assertEqual(self.store.query('SELECT * FROM tasks'), before)

    def test_relationship_limits_disclose_truncation(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('missing', str(index), '', '{}', 1) for index in range(105)])
        report = self.store.relationship_audit()
        self.assertEqual(len(report['stages_without_run']), 100)
        self.assertTrue(report['_truncated']['stages_without_run'])
        report = self.store.relationship_audit(limit=200)
        self.assertEqual(len(report['stages_without_run']), 105)
        self.assertFalse(report['_truncated']['stages_without_run'])

    def test_http_request_id_is_available_to_slow_query_diagnostics(self):
        import urllib.request
        from adaptive_rsi.runtime import Runtime
        from adaptive_rsi.server import Server
        runtime = Runtime(self.store, agent=object())
        self.addCleanup(runtime.close)
        server = Server(runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original, injected = self.store.query, []
        def slow_query(sql, args=()):
            if not injected:
                injected.append(True)
                with self.store.connect() as db:
                    db.create_function('slow_http_fixture', 1, lambda value: time.sleep(.12) or 1)
                original('SELECT slow_http_fixture(?)', ('private-query-binding',))
            return original(sql, args)
        request = urllib.request.Request(server.local_origin + '/api/events?secret=private-query-value',
                                         headers={'Cookie': server.cookie + '=' + server.token})
        with patch.object(self.store, 'query', side_effect=slow_query):
            with urllib.request.urlopen(request, timeout=3) as response:
                identity = response.headers['X-RSI-Request-ID']
                self.assertEqual(response.status, 200)
        report = self.store.metrics()['slow_queries'][-1]
        self.assertEqual(report['context'], {'request': identity})
        self.assertNotIn('private-query', json.dumps(report))

    def test_restart_span_close_marks_phase_duration_unmeasured_but_keeps_remote_wait(self):
        # 重启后 _clock_samples 丢失：phase span 的真实执行时长无法从墙钟差还原
        # （含进程停机窗口），duration 落 -1「未测量」标记，telemetry 的
        # max(0,duration) 折算为 0 贡献而不是把停机时长计入阶段耗时。
        # remote_waits 语义不同：远端在停机期间确实仍在等待，保留墙钟差。
        from adaptive_rsi.telemetry import workflow_metrics
        self.store.execute("INSERT INTO projects VALUES ('p','fixture','/fixture','{}',1,1)")
        task = self.store.add_task('p', 'goal', 'restart-span', 'fixture')
        began = time.time()
        self.store.execute("INSERT INTO runs(id,task,project,state,started) VALUES ('r1',?,'p','executing',?)", (task, began - 7200))
        self.store.execute("INSERT INTO runs(id,task,project,state,started) VALUES ('r2',?,'p','executing',?)", (task, began))
        # r1 的 phase/wait 行不经 phase()/remote_wait() 写入：没有单调时钟样本，
        # 模拟上一进程留下的 open span。
        self.store.execute("INSERT INTO phase_times(run,phase,started) VALUES ('r1','executing',?)", (began - 7200,))
        self.store.execute("INSERT INTO remote_waits(run,reason,started) VALUES ('r1','waiting_ci',?)", (began - 3600,))
        # r2 的 span 有单调时钟样本（本进程写入），收尾后仍是正常非负时长，作为对照。
        self.store.phase('r2', 'executing')
        self.assertEqual(self.store.recover(), 2)
        span = self.store.one("SELECT ended,duration FROM phase_times WHERE run='r1'")
        self.assertIsNotNone(span)
        self.assertIsNotNone(span['ended'])
        self.assertEqual(span['duration'], -1)
        wait = self.store.one("SELECT ended,duration FROM remote_waits WHERE run='r1'")
        self.assertIsNotNone(wait)
        self.assertIsNotNone(wait['ended'])
        self.assertGreaterEqual(wait['duration'], 3600)
        control = self.store.one("SELECT duration FROM phase_times WHERE run='r2'")
        self.assertIsNotNone(control)
        self.assertGreaterEqual(control['duration'], 0)
        metrics = workflow_metrics(self.store)
        # 停机 7200s 没有计入；只剩 r2 正常收尾的毫秒级样本。
        self.assertLess(metrics['phase_seconds'].get('executing', 0), 1)
        self.assertGreaterEqual(metrics['remote_wait_seconds'].get('waiting_ci', 0), 3600)

    def test_claim_blocks_bad_tasks_in_segments_without_holding_the_write_lock(self):
        """被污染的队列：N 条坏 overrides 任务必须在各自短事务里拉黑并各写事件，
        claim 期间另一连接的写入要能插进分段间隙，而不是被整段写锁挡住。"""
        from adaptive_rsi.config import validate
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',0,0)")
        bad_tasks = [self.store.add_task('p', 'goal', 'bad-' + str(index), 'fixture')
                     for index in range(60)]
        for task in bad_tasks:
            self.store.execute("UPDATE tasks SET overrides='{bad json' WHERE id=?", (task,))
        # 坏任务先入队故按 created 排在最前；给正常任务最低优先级，保证 60 条全部先被拉黑。
        good = self.store.add_task('p', 'goal', 'good', 'fixture', priority=1)

        # 观测点：claim 线程每次 COMMIT 成功后、下一段 BEGIN 之前挂起；此刻写锁
        # 已让出，另一连接的写入必须能立即穿透。旧实现全程只有一个提交点，
        # gate 第二次 wait 就会超时——回归能被确定性地检出。
        claimed = []
        claim = threading.Thread(target=lambda: claimed.append(
            self.store.claim('p', validate({}))))
        gate, release = threading.Event(), threading.Event()
        stalls, writes = [0], []
        original = Store.connect

        @contextlib.contextmanager
        def instrumented(store):
            with original(store) as db:
                class Probe:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def commit(self):
                        result = db.commit()
                        if threading.current_thread() is claim and stalls[0] < 60:
                            stalls[0] += 1
                            gate.set()
                            release.wait(5)
                            release.clear()
                        return result

                yield Probe()

        with patch.object(Store, 'connect', instrumented):
            claim.start()
            for _ in range(60):
                self.assertTrue(gate.wait(5), 'claim must yield the write lock between segments')
                began = time.monotonic()
                self.store.execute("INSERT INTO events(project,kind,message,data,created) "
                                   "VALUES ('p','probe','write','{}',?)", (time.time(),))
                writes.append(time.monotonic() - began)
                release.set()
            deadline = time.monotonic() + 10
            while claim.is_alive() and time.monotonic() < deadline:
                release.set()
                claim.join(0.05)
        self.assertFalse(claim.is_alive())
        self.assertEqual(len(claimed), 1)
        run = claimed[0]
        self.assertIsNotNone(run)
        self.assertEqual(run['task'], good)
        blocked = self.store.one(
            "SELECT count(*) AS n FROM tasks WHERE state='blocked' AND project='p'")
        assert blocked is not None
        self.assertEqual(blocked['n'], 60)
        errors = self.store.one(
            "SELECT count(*) AS n FROM events WHERE kind='task_configuration_error' AND project='p'")
        assert errors is not None
        self.assertEqual(errors['n'], 60)
        # 间隙写入应微秒级穿透；阈值只排除「仍被整段写锁挡住」的回归，留足调度余量。
        self.assertLess(max(writes), 5.0)

    def test_claim_rechecks_evidence_before_marking_automatic_task_obsolete(self):
        """扫描读出自动触发 evidence 后、作废短事务开始前，request 把同一任务升级
        为 manual（runtime.request 对排队任务重写 evidence）：作废必须按写锁下的
        新 evidence 重判，不能把已升级的用户请求连同旧快照一起丢掉。"""
        from adaptive_rsi.config import validate
        from adaptive_rsi.store import encode
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',0,0)")
        task = self.store.add_task('p', 'system_issues', 'system:issues', 'fixture',
                                   evidence=[{"scan_trigger": "automatic"}])
        row0 = self.store.one("SELECT evidence FROM tasks WHERE id=?", (task,))
        assert row0 is not None
        self.assertEqual(row0["evidence"], encode([{"scan_trigger": "automatic"}]))
        claimed = []
        claim = threading.Thread(target=lambda: claimed.append(
            self.store.claim('p', validate({"sources": {"issues": False}}))))
        upgraded = [False]
        original = Store.connect
        real_store = self.store

        @contextlib.contextmanager
        def instrumented(store):
            with original(store) as db:
                class Probe:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def execute(self, sql, parameters=()):
                        if (threading.current_thread() is claim and not upgraded[0]
                                and sql.startswith("BEGIN IMMEDIATE")):
                            # 等价于 runtime.request(automatic=False) 在快照与
                            # 写锁之间落库的那笔 evidence 升级。
                            real_store.execute(
                                "UPDATE tasks SET evidence=? WHERE id=?",
                                (encode([{"scan_trigger": "manual"}]), task))
                            upgraded[0] = True
                        return db.execute(sql, parameters)

                yield Probe()

        with patch.object(Store, 'connect', instrumented):
            claim.start()
            claim.join(10)
        self.assertFalse(claim.is_alive())
        self.assertTrue(upgraded[0])
        row = self.store.one("SELECT state,evidence FROM tasks WHERE id=?", (task,))
        assert row is not None
        self.assertNotEqual(row["state"], "obsolete")
        self.assertEqual(row["evidence"], encode([{"scan_trigger": "manual"}]))
        self.assertEqual(len(claimed), 1)
        run = claimed[0]
        self.assertIsNotNone(run)
        self.assertEqual(run["task"], task)

    def test_light_metrics_do_not_scan_large_history_but_explicit_integrity_finds_orphans(self):
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',1,1)")
        task = self.store.add_task('p','goal','valid','valid')
        self.store.execute("INSERT INTO runs(id,task,project) VALUES ('valid-run',?,'p')", (task,))
        with self.store.connect() as db:
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('valid-run', str(index), '', '{}', 1) for index in range(50000)])
            db.execute('PRAGMA foreign_keys=OFF')
        # Foreign key mode must change outside an active transaction.
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.executemany('INSERT INTO stages VALUES (?,?,?,?,?)', [('missing', str(index), '', '{}', 1) for index in range(120)])
        began = time.monotonic()
        for _ in range(5):
            self.assertEqual(self.store.metrics()['foreign_key_check'], 'not_requested')
        self.assertLess(time.monotonic() - began, 1.0)
        integrity = self.store.metrics(integrity=True)
        self.assertEqual(integrity['foreign_key_violations'], 101)
        self.assertTrue(integrity['foreign_key_truncated'])
        audit = self.store.relationship_audit(limit=200)
        self.assertEqual(len(audit['stages_without_run']), 120)
        self.assertFalse(audit['_truncated']['stages_without_run'])

    def _reopened_store_with_events(self, rows=120000):
        # 评审场景：events 播种后丢弃连接池，再从磁盘重新打开 Store；新连接从未
        # 查询过 events，SQLite 3.46.0 之前的 PRAGMA optimize 因此不会分析它。
        self.store.execute("INSERT INTO projects VALUES ('p','p','/p','{}',1,1)")
        with self.store.connect() as db:
            db.executemany("INSERT INTO events(kind,created,project) VALUES (?,?,?)",
                           (("kind-%d" % (index % 20), time.time() - index, "p")
                            for index in range(rows)))
        self.store.close()
        store = Store(self.store.root)
        self.addCleanup(store.close)
        self.assertIsNone(store.one("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"))
        return store

    def test_maintenance_analyzes_after_reopening_store_without_prior_query(self):
        # 维护轮在 retention 之后补齐 events 统计；此前没有在同一条连接上执行过
        # 目标查询，所以不能依赖 0x10000 位让 optimize 顺手分析它。
        store = self._reopened_store_with_events()

        from adaptive_rsi.runtime import Runtime
        Runtime(store, agent=object()).maintenance()

        self.assertIsNotNone(store.one(
            "SELECT 1 FROM sqlite_stat1 WHERE tbl='events' AND idx='event_kind_created'"))
        plan = [row["detail"] for row in store.query(
            "EXPLAIN QUERY PLAN SELECT kind,count(*) FROM events WHERE created>? AND project=? "
            "GROUP BY kind", (0, "p"))]
        self.assertTrue(any("event_kind_created" in step for step in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in step for step in plan), plan)

    def test_planner_refresh_analyzes_tables_optimize_leaves_unanalyzed(self):
        # 3.46.0 之前 optimize 忽略 0x10000，也不会自动分析没有 sqlite_stat1 的表；
        # 用空 MASK（什么也不做）等价模拟旧版空转，校验显式 ANALYZE 兜底。行数取
        # 默认的 120k：抽样统计会让两个候选索引的每前缀基数都报成样本行数，
        # 规划器因此退回 project_events + TEMP B-TREE，必须真扫全表才不会退化。
        from adaptive_rsi import maintenance
        store = self._reopened_store_with_events()
        with store.connect() as db:
            db.execute("PRAGMA optimize=0").fetchall()
        self.assertIsNone(store.one("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'"))
        with patch.object(maintenance, "PLANNER_OPTIMIZE_PRAGMA", "PRAGMA optimize=0"), \
                patch.object(maintenance, "PLANNER_OPTIMIZE_ALL_TABLES", False):
            self.assertTrue(maintenance.refresh_query_planner(store))
        self.assertIsNotNone(store.one(
            "SELECT 1 FROM sqlite_stat1 WHERE tbl='events' AND idx='event_kind_created'"))
        plan = [row["detail"] for row in store.query(
            "EXPLAIN QUERY PLAN SELECT kind,count(*) FROM events WHERE created>? AND project=? "
            "GROUP BY kind", (0, "p"))]
        self.assertTrue(any("event_kind_created" in step for step in plan), plan)
        self.assertFalse(any("TEMP B-TREE" in step for step in plan), plan)

    def test_planner_refresh_failure_is_logged_not_raised(self):
        from adaptive_rsi import maintenance
        with patch.object(self.store, "connect",
                          side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs("adaptive_rsi.maintenance", level="WARNING"):
                self.assertFalse(maintenance.refresh_query_planner(self.store))
