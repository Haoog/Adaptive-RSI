import contextlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi.store import POOL_SIZE, Store


class StorePoolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)

    def test_transition_resolves_stop_before_completion_and_retry_fields(self):
        from adaptive_rsi.config import validate
        self.store.execute("INSERT INTO projects VALUES ('p','fixture','/fixture','{}',0,0)")
        for requested in ["executing", "completed", "awaiting_approval", "failed", "waiting_budget", "pr_open", "merged"]:
            for cancelled in [False, True]:
                with self.subTest(requested=requested, cancelled=cancelled):
                    task = self.store.add_task("p", "goal", requested + str(cancelled), "fixture")
                    run = self.store.claim("p", validate({}))
                    self.store.put_setting("run-stop:" + run["id"], True)
                    if cancelled:
                        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task,))
                    self.assertTrue(self.store.transition(run["id"], requested, {"receipt": "preserved"}, requested != "executing",
                        task_fields={"state": "queued", "available": 123, "infra_retries": 2, "budget_waits": 1}))
                    saved = self.store.one("SELECT state,ended,result FROM runs WHERE id=?", (run["id"],))
                    expected = requested if requested in ["pr_open", "merged"] else "cancelled" if cancelled else "interrupted"
                    self.assertEqual(saved["state"], expected)
                    self.assertIsNotNone(saved["ended"])
                    self.assertEqual(json.loads(saved["result"])["receipt"], "preserved")
                    if requested not in ["pr_open", "merged"]:
                        self.assertEqual(self.store.one("SELECT state,available,infra_retries,budget_waits FROM tasks WHERE id=?", (task,)),
                                         {"state": expected, "available": 0, "infra_retries": 0, "budget_waits": 0})
                    self.assertFalse(self.store.transition(run["id"], "completed", {}, True))
                    self.store.execute("UPDATE tasks SET state='completed' WHERE id=?", (task,))

    def count_new_connections(self):
        """Patch Store._new_connection and return the list of created connections."""
        created = []
        original = Store._new_connection
        lock = threading.Lock()

        def counting(store_self):
            db = original(store_self)
            with lock:
                created.append(db)
            return db

        patcher = patch.object(Store, "_new_connection", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created

    def test_concurrent_queries_reuse_a_bounded_pool(self):
        self.store.execute("INSERT INTO projects(id,name,path) VALUES ('p','p','/p')")
        created = self.count_new_connections()
        threads, iterations = 16, 50
        errors = []
        max_pool_size = [0]

        def worker(index):
            try:
                for _ in range(iterations):
                    self.store.execute("INSERT INTO events(project,kind,message,data,created) VALUES (?,?,?,?,?)",
                                       ("p", "worker-" + str(index), "m", "{}", 0.0))
                    rows = self.store.query("SELECT count(*) AS n FROM events WHERE kind=?", ("worker-" + str(index),))
                    if rows[0]["n"] > iterations:
                        raise AssertionError("worker " + str(index) + " saw " + str(rows[0]["n"]) + " rows")
                    max_pool_size[0] = max(max_pool_size[0], self.store._pool.qsize())
            except Exception as exc:  # 收集任意线程失败后在主线程统一断言
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
        for item in workers:
            item.start()
        for item in workers:
            item.join(60)
        self.assertFalse(any(item.is_alive() for item in workers))
        self.assertEqual(errors, [])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE project='p'")["n"], threads * iterations)
        # 1600 次借出只允许创建受并发上限约束的少量连接：池确实在复用且有界。
        self.assertLessEqual(max_pool_size[0], POOL_SIZE)
        self.assertLessEqual(len(created), threads)
        self.assertLess(len(created), threads * iterations * 2)

    def test_wal_and_foreign_keys_survive_pool_reuse(self):
        self.assertEqual(self.store.query("PRAGMA journal_mode"), [{"journal_mode": "wal"}])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute("INSERT INTO tasks(id,project,title,state) VALUES ('t','missing','x','queued')")
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks")["n"], 0)

    def test_stream_returns_connection_and_abandoned_streams_do_not_leak(self):
        self.store.execute("INSERT INTO projects(id,name,path) VALUES ('p','p','/p')")
        for index in range(3):
            self.store.execute("INSERT INTO events(project,kind,message,data,created) VALUES ('p','k','m','{}',?)", (float(index),))
        self.assertEqual(self.store._pool.qsize(), 1)

        rows = self.store.stream("SELECT seq FROM events ORDER BY seq")
        self.assertEqual(next(rows)["seq"], 1)
        rows.close()
        # 中途放弃的 stream 不归还连接：池必须保持为空而不是累积半途连接。
        self.assertEqual(self.store._pool.qsize(), 0)
        self.assertEqual([row["seq"] for row in self.store.stream("SELECT seq FROM events ORDER BY seq")], [1, 2, 3])
        self.assertEqual(self.store._pool.qsize(), 1)

    def test_failed_transaction_is_closed_instead_of_returned_to_the_pool(self):
        self.assertEqual(self.store._pool.qsize(), 1)
        created = self.count_new_connections()
        with self.assertRaises(sqlite3.OperationalError):
            with self.store.connect() as db:
                db.execute("SELECT missing_column FROM settings")
        self.assertEqual(self.store._pool.qsize(), 0)
        # 坏连接未被归还：下一次查询必须新建连接且结果正确。
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM settings"), [{"n": 1}])
        self.assertEqual(len(created), 1)
        self.assertEqual(self.store._pool.qsize(), 1)

    def test_replaced_database_file_is_detected_and_reopened(self):
        # 升级回滚会整体替换 state.sqlite3（updates.py 连同 -wal/-shm 一起删除），
        # 池中指向旧文件的连接必须弃用，不能读到替换前的数据。
        self.store.put_setting("sentinel", {"project": "keep-me"})
        self.assertEqual(self.store._pool.qsize(), 1)
        backup = Path(self.temp.name) / "restore.sqlite3"
        with contextlib.closing(sqlite3.connect(str(backup))) as target:
            with self.store.connect() as source:
                source.backup(target)
        self.store.put_setting("sentinel", {"project": "mutated"})
        for suffix in ["-wal", "-shm"]:
            Path(str(self.store.path) + suffix).unlink(missing_ok=True)
        os.replace(backup, self.store.path)
        self.assertEqual(self.store.setting("sentinel"), {"project": "keep-me"})
        self.assertEqual(self.store._pool.qsize(), 1)

    def test_close_drains_the_pool(self):
        self.store.query("SELECT count(*) AS n FROM settings")
        self.assertEqual(self.store._pool.qsize(), 1)
        self.store.close()
        self.assertEqual(self.store._pool.qsize(), 0)
        self.assertEqual(self.store.query("SELECT count(*) AS n FROM settings"), [{"n": 1}])
        self.assertEqual(self.store._pool.qsize(), 1)


class RecordUsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        self.store.execute("INSERT INTO runs(id,task,project,state,config,started) VALUES ('run',NULL,'p','planning','{}',0)")
        self.store.execute("INSERT INTO calls(id,project,run,role,model,effort,state,tokens,created) "
                           "VALUES ('call','p','run','planning','m','high','reserved',0,0)")

    @staticmethod
    def usage(tokens):
        return {"tokens": tokens, "input_tokens": tokens, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_known": True}

    def test_repeated_recording_is_idempotent_for_run_tokens(self):
        self.store.record_usage("call", "run", self.usage(100), 1.0)
        self.store.record_usage("call", "run", self.usage(100), 1.5)
        self.assertEqual(self.store.one("SELECT tokens FROM runs WHERE id='run'")["tokens"], 100)
        self.assertEqual(self.store.one("SELECT seconds FROM calls WHERE id='call'")["seconds"], 1.5)

    def test_usage_corrections_keep_run_and_call_totals_consistent(self):
        self.store.record_usage("call", "run", self.usage(100), 1.0)
        self.store.record_usage("call", "run", self.usage(40), 2.0)
        self.assertEqual(self.store.one("SELECT tokens FROM calls WHERE id='call'")["tokens"], 40)
        self.assertEqual(self.store.one("SELECT tokens FROM runs WHERE id='run'")["tokens"], 40)
        self.store.record_usage("call", "run", self.usage(150), 3.0)
        self.assertEqual(self.store.one("SELECT tokens FROM runs WHERE id='run'")["tokens"], 150)

    def test_record_usage_transitions_state_to_completed(self):
        call_row = self.store.one("SELECT state FROM calls WHERE id='call'")
        assert call_row is not None
        self.assertEqual(call_row["state"], "reserved")
        self.store.record_usage("call", "run", self.usage(100), 1.0)
        call_row = self.store.one("SELECT state FROM calls WHERE id='call'")
        assert call_row is not None
        self.assertEqual(call_row["state"], "completed")

    def test_record_usage_robust_to_missing_tokens_and_partial_details(self):
        self.store.record_usage("call", "run", {"usage_known": True}, 1.0)
        call_row = self.store.one("SELECT state, tokens, input_tokens, output_tokens, usage_known FROM calls WHERE id='call'")
        self.assertEqual(call_row, {
            "state": "completed",
            "tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_known": 1,
        })
        run_row = self.store.one("SELECT tokens FROM runs WHERE id='run'")
        assert run_row is not None
        self.assertEqual(run_row["tokens"], 0)

        self.store.execute("INSERT INTO calls(id,project,run,role,model,effort,state,tokens,created) "
                           "VALUES ('call-partial','p','run','planning','m','high','reserved',0,0)")
        self.store.record_usage("call-partial", "run", {"input_tokens": 5, "usage_known": True}, 0.5)
        partial_row = self.store.one("SELECT state, tokens, input_tokens, usage_known FROM calls WHERE id='call-partial'")
        self.assertEqual(partial_row, {
            "state": "completed",
            "tokens": 5,
            "input_tokens": 5,
            "usage_known": 1,
        })

    def test_recover_only_interrupts_uncompleted_calls(self):
        self.store.record_usage("call", "run", self.usage(100), 1.0)
        self.store.execute("INSERT INTO calls(id,project,run,role,model,effort,state,tokens,created) "
                           "VALUES ('call-in-flight','p','run','planning','m','high','reserved',0,0)")
        self.store.recover()
        call_row = self.store.one("SELECT state FROM calls WHERE id='call'")
        in_flight_row = self.store.one("SELECT state FROM calls WHERE id='call-in-flight'")
        assert call_row is not None and in_flight_row is not None
        self.assertEqual(call_row["state"], "completed")
        self.assertEqual(in_flight_row["state"], "interrupted")


class MigrationLedgerHealTest(unittest.TestCase):
    """账本缺行时只补账本，不重放迁移；无法确认时明确报错。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"

    def seed(self):
        store = Store(self.root)
        store.execute("INSERT INTO projects VALUES ('p','fixture','/fixture','{}',0,0)")
        task = store.add_task("p", "goal", "ledger-heal", "fixture title")
        store.execute("INSERT INTO task_revisions(task,previous_body,created) VALUES (?,?,?)", (task, "old", 1))
        store.execute("INSERT INTO task_revision_snapshots VALUES (1,'after','impact')")
        store.close()
        return task

    def versions(self):
        with sqlite3.connect(str(self.root / "state.sqlite3")) as db:
            return [row[0] for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")]

    @staticmethod
    def applied_versions():
        """All versions this runtime knows, so assertions follow new migrations without edits."""
        from adaptive_rsi.schema_migrations import scripts
        return list(range(1, max(version for version, _ in scripts()) + 1))

    def pending_apply(self, pending):
        """Replace apply() with a recorder so a replay attempt is visible without running migrations."""
        from adaptive_rsi.schema_migrations import scripts
        top = max(version for version, _ in scripts())

        def record(db):
            pending.append([version for version in range(1, top + 1)
                            if not db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone()])
        return record

    def test_every_migration_registers_ledger_evidence(self):
        from adaptive_rsi.schema_migrations import EVIDENCE, scripts
        self.assertEqual(set(EVIDENCE), {1, 2} | {version for version, _ in scripts()})

    def test_interrupted_bootstrap_is_completed_on_reopen(self):
        """executescript 已提交、补列事务未提交即中断：重开必须补完 bootstrap，不得误判已封版。"""
        from adaptive_rsi.schema_migrations import BOOTSTRAP_SCHEMA
        path = self.root / "state.sqlite3"
        self.root.mkdir(parents=True)
        with sqlite3.connect(str(path)) as db:
            db.executescript(BOOTSTRAP_SCHEMA)
        with sqlite3.connect(str(path)) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT version FROM schema_migrations")], [])
            self.assertNotIn("request_log_created",
                             {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")})
        pending = []
        with patch("adaptive_rsi.schema_migrations.apply", side_effect=self.pending_apply(pending)):
            reopened = Store(self.root)
        reopened.close()
        for table, expected in [("tasks", ("urgency", "infra_retries", "budget_waits")),
                                ("calls", ("provider", "unit_price", "unit", "input_tokens", "output_tokens",
                                           "cache_read_tokens", "cache_write_tokens", "usage_known")),
                                ("phase_times", ("duration",)), ("remote_waits", ("duration",))]:
            with sqlite3.connect(str(path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
            self.assertTrue(set(expected) <= columns, table)
        self.assertEqual(self.versions(), [1, 2])  # bootstrap 补写 1–2；apply 被 recorder 替换，不落库
        with sqlite3.connect(str(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(pending, [self.applied_versions()[2:]])  # apply 继续 3–8，未重放已生效迁移
        final = Store(self.root)
        try:
            self.assertEqual(self.versions(), self.applied_versions())
        finally:
            final.close()
        with sqlite3.connect(str(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_gap_backfills_ledger_row_without_replaying_migration(self):
        import hashlib
        from adaptive_rsi.schema_migrations import scripts
        task = self.seed()
        store = Store(self.root)
        store.execute("DELETE FROM schema_migrations WHERE version=4")
        store.close()
        self.assertEqual(self.versions(), self.applied_versions()[:3] + self.applied_versions()[4:])
        pending = []
        with self.assertLogs("adaptive_rsi.schema_migrations", level="WARNING") as logged, \
                patch("adaptive_rsi.schema_migrations.apply", side_effect=self.pending_apply(pending)):
            reopened = Store(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(self.versions(), self.applied_versions())
        self.assertEqual(pending, [[]])  # apply 运行时已无待执行迁移
        self.assertIn("version(s) 4", logged.output[0])
        healed = reopened.one("SELECT checksum FROM schema_migrations WHERE version=4")
        content = next(bytes_ for version, bytes_ in scripts() if version == 4)
        self.assertEqual(healed["checksum"], hashlib.sha256(content).hexdigest())
        self.assertEqual(reopened.one("SELECT title FROM tasks WHERE id=?", (task,))["title"], "fixture title")
        self.assertEqual(reopened.one("SELECT after_body FROM task_revision_snapshots")["after_body"], "after")

    def test_lost_trailing_rows_and_empty_ledger_are_backfilled_without_bootstrap(self):
        task = self.seed()
        store = Store(self.root)
        store.execute("DELETE FROM schema_migrations WHERE version>=5")
        store.close()
        reopened = Store(self.root)
        try:
            self.assertEqual(self.versions(), self.applied_versions())
            self.assertEqual(reopened.one("SELECT title FROM tasks WHERE id=?", (task,))["title"], "fixture title")
        finally:
            reopened.close()
        with sqlite3.connect(str(self.root / "state.sqlite3")) as db, db:
            db.execute("DELETE FROM schema_migrations")
        with patch.object(Store, "_bootstrap", side_effect=AssertionError("bootstrap repeated")):
            reopened = Store(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(self.versions(), self.applied_versions())

    def test_gap_without_visible_effect_fails_instead_of_starting(self):
        self.seed()
        store = Store(self.root)
        store.execute("DROP TABLE task_revision_snapshots")
        store.execute("DELETE FROM schema_migrations WHERE version=4")
        store.close()
        with self.assertRaisesRegex(RuntimeError,
                                    "gap at version 4.*task_revision_snapshots.*version %d" % self.applied_versions()[-1]):
            Store(self.root)
        self.assertEqual(self.versions(), self.applied_versions()[:3] + self.applied_versions()[4:])  # 失败启动不留下半个补账

    def test_unregistered_migration_version_is_left_to_apply(self):
        from adaptive_rsi.schema_migrations import scripts
        self.seed()
        store = Store(self.root)
        store.execute("DELETE FROM schema_migrations WHERE version=7")
        store.close()
        future = max(version for version, _ in scripts()) + 1
        extra = scripts() + [(future, b"CREATE TABLE future_probe(id TEXT);\n")]
        with patch("adaptive_rsi.schema_migrations.scripts", return_value=extra):
            reopened = Store(self.root)
        self.addCleanup(reopened.close)
        self.assertEqual(self.versions(), list(range(1, future + 1)))
        self.assertEqual(reopened.one("SELECT count(*) AS n FROM sqlite_master WHERE name='future_probe'")["n"], 1)

    def test_foreign_and_rewritten_history_still_fail(self):
        self.seed()
        for statement, message in [
                ("UPDATE schema_migrations SET checksum='changed' WHERE version=5", "checksum mismatch"),
                ("INSERT INTO schema_migrations VALUES (999,'future',0)", "newer than this runtime")]:
            with self.subTest(message=message):
                root = Path(self.temp.name) / ("corrupt-" + message.split()[0])
                store = Store(root)
                store.execute(statement)
                store.close()
                with self.assertRaisesRegex(RuntimeError, message):
                    Store(root)


if __name__ == "__main__":
    unittest.main()
