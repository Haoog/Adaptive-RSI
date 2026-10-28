import tempfile
import unittest
from pathlib import Path

from adaptive_rsi.store import Store
from adaptive_rsi.usage_report import report


class UsageReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
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
