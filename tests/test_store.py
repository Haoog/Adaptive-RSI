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


if __name__ == "__main__":
    unittest.main()
