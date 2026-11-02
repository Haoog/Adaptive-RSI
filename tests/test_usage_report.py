import tempfile
import time
import unittest
from pathlib import Path

from adaptive_rsi.store import Store
from adaptive_rsi.usage_report import report


class UsageReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.close)
        for project in ['a', 'b']:
            self.store.execute('INSERT INTO projects(id,name,path) VALUES (?,?,?)', (project, project, '/' + project))
            self.store.execute('INSERT INTO tasks(id,project,title,source,state) VALUES (?,?,?,?,?)', (project, project, 'Task '+project, 'goal', 'failed'))
        for run, project in [('one', 'a'), ('two', 'a'), ('other', 'b')]:
            self.store.execute('INSERT INTO runs(id,task,project) VALUES (?,?,?)', (run, project, project))
        for ident, project, run, tokens, known, state in [('c1','a','one',100,1,'done'),('c2','a','two',50,1,'failed'),('c3','a','two',0,0,'failed'),('c4','b','other',900,1,'done'),('c5','a',None,10,1,'done')]:
            self.store.execute('INSERT INTO calls(id,project,run,role,model,provider,effort,state,tokens,usage_known,seconds,created,unit_price,input_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (ident,project,run,'planner','model','provider','high',state,tokens,known,2,100,2 if known else None,tokens))

    def query(self, **kwargs):
        return report(self.store, {k:[str(v)] for k,v in {'start':0,'end':200,**kwargs}.items()})

    def test_task_attempts_failures_unknown_and_background(self):
        result = self.query(project='a',sort='tokens')
        self.assertEqual(result['summary']['tokens'],160)
        self.assertEqual(result['summary']['unknown_calls'],1)
        self.assertEqual(result['summary']['failed_calls'],2)
        self.assertEqual(result['total'],2)
        self.assertEqual(result['tasks'][0]['attempts'],2)
        self.assertEqual(result['tasks'][1]['background'],1)
        self.assertAlmostEqual(result['summary']['estimated_usd'],.00032)
        self.assertEqual(result['summary']['unpriced_calls'],1)

    def test_recorded_call_has_zero_pending_and_failed_calls(self):
        self.store.execute("INSERT INTO projects(id,name,path) VALUES ('p-clean','p-clean','/p-clean')")
        self.store.execute("INSERT INTO tasks(id,project,title,source,state) VALUES ('t-clean','p-clean','T','goal','planning')")
        self.store.execute("INSERT INTO runs(id,task,project) VALUES ('r-clean','t-clean','p-clean')")
        config = {'providers': [{'id': 'openai'}], 'limits': {}}
        call = self.store.reserve_call('p-clean', 'r-clean', 'planner', {'provider': 'openai', 'model': 'model', 'effort': 'high'}, config)
        self.store.record_usage(call, 'r-clean', {'tokens': 20, 'input_tokens': 20, 'output_tokens': 0,
                                                  'cache_read_tokens': 0, 'cache_write_tokens': 0, 'usage_known': True}, 1.0)
        call_row = self.store.one("SELECT state FROM calls WHERE id=?", (call,))
        assert call_row is not None
        self.assertEqual(call_row["state"], "completed")
        rep = report(self.store, {'project': ['p-clean'], 'start': ['0'], 'end': [str(time.time() + 3600)]})
        self.assertEqual(rep['summary']['calls'], 1)
        self.assertEqual(rep['summary']['pending_calls'], 0)
        self.assertEqual(rep['summary']['failed_calls'], 0)

    def test_all_report_reads_share_one_snapshot(self):
        original_query, original_stream = self.store.query, self.store.stream
        connections = []

        def query(sql, args=()):
            connections.append(getattr(self.store._read_snapshot, 'connection', None))
            return original_query(sql, args)

        def stream(sql, args=()):
            connections.append(getattr(self.store._read_snapshot, 'connection', None))
            yield from original_stream(sql, args)

        self.store.query, self.store.stream = query, stream
        try:
            result = self.query(project='a', task='a')
        finally:
            self.store.query, self.store.stream = original_query, original_stream
        self.assertTrue(connections)
        self.assertTrue(all(connection is not None for connection in connections))
        self.assertEqual(len({id(connection) for connection in connections}), 1)
        self.assertEqual(result['summary']['calls'], result['call_total'])

    def test_task_detail_isolated_and_paginated(self):
        result=self.query(project='a',task='a',limit=1,offset=1)
        self.assertEqual(result['call_total'],3)
        self.assertEqual(len(result['calls']),1)
        self.assertNotIn('body',result['calls'][0])
        self.assertEqual(self.query(project='b',task='a')['summary']['calls'],0)
        self.assertEqual(self.query(start=101)['summary']['calls'],0)
        self.assertEqual(self.query(q='Task b')['summary']['tokens'],900)

    def test_validation(self):
        for params in [{'start':'nan'},{'end':0},{'limit':101},{'offset':-1},{'project':'missing'},{'sort':'sql'}]:
            with self.subTest(params=params),self.assertRaises(ValueError):
                self.query(**params)

    def test_empty_not_zero_cost(self):
        result=self.query(start=101)
        self.assertIsNone(result['summary']['estimated_usd'])
        self.assertEqual(result['summary']['tokens'],0)
        self.assertEqual(result['tasks'],[])

    def test_daily_usage_and_custom_dates_use_requested_timezone(self):
        result = self.query(project='a', timezone='America/Los_Angeles')
        self.assertEqual(result['timezone'], 'America/Los_Angeles')
        self.assertEqual(result['daily'][0]['date'], '1969-12-31')
        self.assertTrue(result['daily'][0]['date'])

    def test_invalid_timezone_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'timezone'):
            self.query(timezone='Not/AZone')

    def test_streamed_usage_rows_are_available_without_fetchall(self):
        rows = list(self.store.stream("SELECT id,tokens FROM calls ORDER BY id"))
        self.assertEqual([row["id"] for row in rows], ["c1", "c2", "c3", "c4", "c5"])

    def add_priced_call(self, ident, *, run="one", project="a", role="planner", model="model",
                        provider="provider", effort="high", created=100, state="completed",
                        usage_known=1, tokens=200, input_tokens=0, output_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, unit_price=None, prices=None,
                        seconds=2):
        prices = prices or {}
        self.store.execute(
            "INSERT INTO calls(id,project,run,role,model,provider,effort,state,tokens,usage_known,"
            "seconds,created,unit_price,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,"
            "input_price,output_price,cache_read_price,cache_write_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident, project, run, role, model, provider, effort, state, tokens, usage_known,
             seconds, created, unit_price, input_tokens, output_tokens, cache_read_tokens,
             cache_write_tokens, prices.get("input"), prices.get("output"),
             prices.get("cache_read"), prices.get("cache_write")))

    def test_component_prices_and_unit_price_fallback_without_double_counting(self):
        self.store.execute("DELETE FROM calls")
        self.add_priced_call("components", tokens=200, input_tokens=100, output_tokens=50,
                             cache_read_tokens=30, cache_write_tokens=20,
                             prices={"input": 1, "output": 2, "cache_read": .5, "cache_write": 3})
        self.add_priced_call("blended", tokens=200, input_tokens=100, output_tokens=100, unit_price=10)
        self.add_priced_call("partial", tokens=100, input_tokens=50, output_tokens=50,
                             prices={"input": 1})
        self.add_priced_call("partial_fallback", tokens=200, input_tokens=100, output_tokens=100,
                             unit_price=5, prices={"input": 1})
        self.add_priced_call("unknown", tokens=200, usage_known=0,
                             prices={"input": 1, "output": 2, "cache_read": .5, "cache_write": 3})
        result = self.query(project="a")
        summary = result["summary"]
        self.assertEqual((summary["calls"], summary["priced_calls"], summary["unpriced_calls"]), (5, 3, 2))
        self.assertEqual((summary["component_priced_calls"], summary["unit_price_priced_calls"]), (1, 2))
        self.assertEqual((summary["unknown_calls"], summary["known_unpriced_calls"]), (1, 1))
        # 缓存读取已含在 tokens 中，输入仅按未命中的 100 计费；混合单价继续作为回退。
        self.assertAlmostEqual(summary["estimated_usd"], .000275 + .002 + .001)
        self.assertEqual((result["tasks"][0]["priced_calls"], result["tasks"][0]["unpriced_calls"]), (3, 2))
        self.assertEqual((result["stages"][0]["priced_calls"], result["stages"][0]["unpriced_calls"]), (3, 2))

    def test_reference_interval_requires_enough_prior_same_group_samples(self):
        self.store.execute("DELETE FROM calls")
        for index in range(10):
            run = "history-" + str(index)
            self.store.execute("INSERT INTO runs(id,task,project) VALUES (?,?,?)", (run, "a", "a"))
            self.add_priced_call("history-" + str(index), run=run, created=10 + index,
                                 tokens=(index + 1) * 100, input_tokens=(index + 1) * 100,
                                 seconds=index + 1, prices={"input": 1})
        self.add_priced_call("current", created=100, tokens=600, input_tokens=600,
                             prices={"input": 1})
        self.add_priced_call("few-current", created=100, role="analysis", model="model-b",
                             tokens=600, input_tokens=600, prices={"input": 1})
        for index in range(3):
            run = "few-" + str(index)
            self.store.execute("INSERT INTO runs(id,task,project) VALUES (?,?,?)", (run, "a", "a"))
            self.add_priced_call("few-" + str(index), run=run, created=10 + index,
                                 role="analysis", model="model-b", tokens=300,
                                 input_tokens=300, prices={"input": 1})
        result = self.query(project="a", start=100, end=200)
        references = result["references"]
        self.assertEqual(references["min_samples"], 8)
        self.assertEqual(references["sample_limit"], 200)
        self.assertIn("completed_calls", references["source"])
        groups = {(row["kind"], row["role"], row["model"]): row for row in references["groups"]}
        planner = groups[("goal", "planner", "model")]
        self.assertEqual((planner["history"]["denominator"], planner["history"]["samples"]), (10, 10))
        self.assertEqual(planner["history"]["status"], "ok")
        self.assertAlmostEqual(planner["history"]["tokens"]["p25"], 325)
        self.assertAlmostEqual(planner["history"]["tokens"]["median"], 550)
        self.assertAlmostEqual(planner["history"]["tokens"]["p75"], 775)
        self.assertAlmostEqual(planner["history"]["seconds"]["median"], 5.5)
        analysis = groups[("goal", "analysis", "model-b")]
        self.assertEqual((analysis["history"]["denominator"], analysis["history"]["samples"]), (3, 3))
        self.assertEqual(analysis["history"]["status"], "insufficient")
        self.assertIsNone(analysis["history"]["tokens"])
        self.assertIsNone(analysis["history"]["seconds"])

    def test_run_and_stage_breakdown_reports_retries_failures_and_cache_reuse(self):
        self.store.execute("DELETE FROM calls")
        self.store.execute("DELETE FROM runs")
        self.store.execute("INSERT INTO runs(id,task,project,state) VALUES ('r1','a','a','completed'),"
                           "('r2','a','a','failed')")
        self.add_priced_call("p1", run="r1", tokens=100, input_tokens=100, state="failed",
                             prices={"input": 1}, created=100)
        self.add_priced_call("p2", run="r1", tokens=200, input_tokens=160, cache_read_tokens=40,
                             prices={"input": 1, "cache_read": .5}, created=101)
        self.add_priced_call("q1", run="r1", role="review", model="reviewer", created=102)
        self.add_priced_call("p3", run="r2", tokens=150, input_tokens=150, state="failed",
                             prices={"input": 1}, created=103)
        self.store.event("stage_reused", "Reused verified stage inputs", project="a", run="r1",
                         data={"stage": "acceptance"})
        result = self.query(project="a")
        runs = {row["id"]: row for row in result["runs"]}
        self.assertEqual((runs["r1"]["calls"], runs["r1"]["failed_calls"], runs["r1"]["retry_calls"]), (3, 1, 1))
        self.assertEqual((runs["r1"]["reused_calls"], runs["r1"]["recovered_stages"]), (1, 1))
        self.assertEqual(runs["r2"]["retry_calls"], 0)
        stages = {(row["role"], row["model"]): row for row in result["stages"]}
        self.assertEqual((stages[("planner", "model")]["calls"],
                          stages[("planner", "model")]["retry_calls"]), (3, 1))
        self.assertEqual(stages[("review", "reviewer")]["retry_calls"], 0)
        task = result["tasks"][0]
        self.assertEqual((task["attempts"], task["retry_calls"], task["kind"]), (2, 1, "goal"))

    def test_price_denominators_are_consistent_across_aggregates(self):
        self.store.execute("UPDATE calls SET run='one' WHERE id='c5'")
        result = self.query(project="a")
        summary = result["summary"]
        self.assertEqual(summary["priced_calls"] + summary["unpriced_calls"], summary["calls"])
        for key in ["roles", "tasks", "runs", "stages", "daily"]:
            self.assertEqual(sum(row["calls"] for row in result[key]), summary["calls"], key)
            for row in result[key]:
                self.assertEqual(row["priced_calls"] + row["unpriced_calls"], row["calls"], key)
        self.assertAlmostEqual(sum(row["estimated_usd"] or 0 for row in result["roles"]),
                               summary["estimated_usd"] or 0)
        self.assertAlmostEqual(sum(row["estimated_usd"] or 0 for row in result["tasks"]),
                               summary["estimated_usd"] or 0)

    def test_additive_price_migration_preserves_legacy_calls(self):
        import hashlib
        import sqlite3
        from adaptive_rsi.schema_migrations import scripts
        root = Path(self.temp.name) / "legacy"
        root.mkdir()
        with sqlite3.connect(root / "state.sqlite3") as db:
            db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,applied REAL NOT NULL)")
            for version in (1, 2):
                db.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                           (version, hashlib.sha256(("adaptive-rsi-schema-v" + str(version)).encode()).hexdigest(), 0))
            for version, content in scripts():
                if version <= 6:
                    db.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                               (version, hashlib.sha256(content).hexdigest(), 0))
            db.execute("CREATE TABLE calls(id TEXT PRIMARY KEY,project TEXT,run TEXT,role TEXT,model TEXT,"
                       "effort TEXT,state TEXT,tokens INTEGER DEFAULT 0,seconds REAL DEFAULT 0,created REAL,"
                       "provider TEXT DEFAULT 'openai',unit_price REAL,unit TEXT DEFAULT 'unknown',"
                       "input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,"
                       "cache_read_tokens INTEGER DEFAULT 0,cache_write_tokens INTEGER DEFAULT 0,"
                       "usage_known INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT,run TEXT,"
                       "kind TEXT,message TEXT,data TEXT,created REAL)")
            db.execute("INSERT INTO calls(id,project,state,tokens,created,unit_price) VALUES ('old','p','completed',123,1,2)")
            db.execute("PRAGMA user_version=2")
        db.close()
        store = Store(root)
        self.addCleanup(store.close)
        self.assertEqual([row["version"] for row in store.schema_migrations()], [1, 2, 3, 4, 5, 6, 7, 8])
        row = store.one("SELECT * FROM calls WHERE id='old'")
        self.assertEqual((row["tokens"], row["unit_price"], row["input_price"]), (123, 2, None))
        self.assertIsNone(row["price_basis"])
