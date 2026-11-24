import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from adaptive_rsi.queue_replay import evaluate
from adaptive_rsi.store import Store, encode
from adaptive_rsi.usage_report import policy_history
from scripts.benchmark_history import main, read_policy_history


TRACE = Path(__file__).with_name('fixtures') / 'policy_queue_trace.json'


class PolicyEvaluationTest(unittest.TestCase):
    def test_queue_cli_replays_isolation_aging_and_urgent_budget(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--policy-replay', str(TRACE), '--workers', '1',
                                   '--old-after', '30', '--quota-window', '100']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['urgent_origins'], {'user': 2, 'system': 1, 'unknown': 1})
        baseline, isolated, aging, urgent = (result['scenarios'][name] for name in
                                            ('baseline', 'isolated_planning', 'old_task_opportunity',
                                             'cross_project_user_urgency'))
        self.assertEqual(isolated['claim_seconds_saved'], 30)
        self.assertEqual(isolated['work_start_seconds_saved'], 45)
        self.assertEqual(isolated['stale_tasks'], 1)
        self.assertEqual(isolated['tasks']['stale']['rework_seconds'], 10)
        self.assertTrue(isolated['tasks']['fresh']['isolated_plan_used'])
        for scenario in result['scenarios'].values():
            entries = sorted(scenario['tasks'].values(), key=lambda row: row['claimed_at'])
            self.assertTrue(all(previous['finished_at'] <= following['claimed_at']
                                for previous, following in zip(entries, entries[1:])))
        self.assertEqual(aging['promotions'], 1)
        self.assertEqual(aging['tasks']['old']['promotion'], 'old_task_opportunity')
        old = aging['tasks']['old']
        self.assertEqual(old['claimable_wait_seconds'], old['claimed_at'] - old['ready_at'] - 5)
        self.assertLess(old['claimable_wait_seconds'], baseline['tasks']['old']['claimable_wait_seconds'])
        self.assertEqual(urgent['promotions'], 1)
        self.assertEqual(urgent['tasks']['user-one']['claim_seconds_saved'], 10)
        self.assertIsNone(urgent['tasks']['user-two']['promotion'])
        self.assertIsNone(urgent['tasks']['system']['promotion'])
        self.assertIsNone(urgent['tasks']['unknown']['promotion'])
        self.assertEqual(urgent['projects']['b']['ordinary_wait_delta_seconds'], 10)
        relaxed = evaluate(json.loads(TRACE.read_text()), workers=1, old_after=30, old_quota=2,
                           urgent_quota=2, quota_window=100)['scenarios']
        self.assertEqual(relaxed['old_task_opportunity']['promotions'], 2)
        self.assertEqual(relaxed['cross_project_user_urgency']['tasks']['user-two']['promotion'],
                         'cross_project_user_urgency')

    def test_writer_slots_are_exclusive_across_project_aliases(self):
        trace = {'format': 1, 'provenance': 'fixed_fixture',
                 'projects': {'a': {'repo': 'shared'}, 'b': {'repo': 'shared'}, 'c': {'repo': 'other'}},
                 'mainline_changes': {'shared': [], 'other': []},
                 'tasks': [dict(id=key, project=key, source='goal', created=1, ready_at=1,
                                plan_seconds=4, work_seconds=10) for key in ('a', 'b', 'c')]}
        baseline, isolation = (evaluate(trace)['scenarios'][name]
                               for name in ('baseline', 'isolated_planning'))
        original, candidate = baseline['tasks'], isolation['tasks']
        self.assertEqual(original['c']['claimed_at'], original['a']['claimed_at'])
        self.assertEqual(candidate['b']['claimed_at'], candidate['a']['finished_at'])
        self.assertLess(candidate['b']['plan_started'], candidate['a']['finished_at'])
        self.assertEqual(candidate['b']['work_start_seconds_saved'], 4)

    def test_disabled_quotas_and_missing_coverage(self):
        trace = json.loads(TRACE.read_text())
        scenarios = evaluate(trace, workers=1, old_quota=0, urgent_quota=0)['scenarios']
        base = scenarios['baseline']['tasks']
        for name in ('old_task_opportunity', 'cross_project_user_urgency'):
            self.assertEqual({key: row['claimed_at'] for key, row in base.items()},
                             {key: row['claimed_at'] for key, row in scenarios[name]['tasks'].items()})
        del trace['mainline_changes']['repo-a']
        with self.assertRaisesRegex(ValueError, 'mainline coverage'):
            evaluate(trace)

    def test_history_cli_keeps_cost_unknowns_and_quality_denominators(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'redacted.sqlite3'
            store = Store(root / 'state')
            try:
                with store.connect() as db:
                    self._seed_history(db)
                with store.connect() as db, contextlib.closing(sqlite3.connect(snapshot)) as copy:
                    db.backup(copy)
            finally:
                store.close()
            before = snapshot.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--policy-history', str(snapshot), '--start', '90', '--end', '200']), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(snapshot.read_bytes(), before)
            self.assertFalse(list(root.glob('redacted.sqlite3-*')))
            self.assertEqual(read_policy_history(snapshot, 190, 200)['automatic_task_cost']['status'],
                             'insufficient_samples')
            self.assertEqual(read_policy_history(snapshot, 90, 200, 'other')['stage_history']['groups'], [])
            Path(str(snapshot) + '-wal').touch()
            with self.assertRaisesRegex(ValueError, 'standalone'):
                read_policy_history(snapshot, 90, 200)
        history = report['stage_history']['groups']
        comparable = next(row for row in history if row['model'] == 'fixture' and row['effort'] == 'low' and
                          row['prices_usd_per_million']['mixed'] == 2)
        self.assertEqual(comparable['samples'], 11)
        self.assertEqual(comparable['distinct_tasks'], 10)
        self.assertEqual(comparable['unknown_usage_samples'], 1)
        self.assertEqual(comparable['quality'], {'observed_runs': 10, 'distinct_tasks': 9, 'passed_runs': 9,
                                                'missing_runs': 1, 'pass_fraction': 0.9, 'status': 'observed'})
        automatic = report['automatic_task_cost']
        groups = automatic['groups']
        priced = next(row for row in groups if row['routes'] and row['routes'][0]['effort'] == 'low' and
                      row['routes'][0]['prices_usd_per_million']['mixed'] == 2)
        self.assertEqual(priced['samples'], 10)
        self.assertEqual(priced['known_cost_samples'], 9)
        self.assertEqual(priced['cost_usd'], {'p25': 0.006, 'median': 0.01, 'p75': 0.014, 'samples': 9})
        self.assertEqual(priced['unknown_usage_samples'], 1)
        unknown_price = next(row for row in groups if row['routes'] and
                             row['routes'][0]['prices_usd_per_million']['mixed'] is None)
        self.assertEqual(unknown_price['unknown_price_samples'], 1)
        self.assertIsNone(unknown_price['cost_usd'])
        free = next(row for row in groups if row['routes'] and row['routes'][0]['prices_usd_per_million']['mixed'] == 0)
        self.assertEqual(free['known_cost_samples'], 1)
        self.assertIsNone(free['cost_usd'])
        self.assertIsNone(next(row for row in groups if not row['routes'])['cost_usd'])
        self.assertEqual(automatic['coverage'][0]['excluded_incomplete_or_window_boundary'], 1)

    def test_automatic_cost_includes_inconclusive_tasks_and_excludes_unfinished_tasks(self):
        # An inconclusive evolution task can follow an earlier failed, paid attempt.
        tasks = ([('completed', [('completed', 'completed', 1000)])] * 5 +
                 [('insufficient_evidence', [('failed', 'failed', 2000),
                                             ('insufficient_evidence', 'completed', 3000)])] * 3 +
                 [(state, [(run_state, 'completed', 100000)]) for state, run_state in
                  [('queued', 'failed'), ('waiting_budget', 'waiting_budget'), ('interrupted', 'interrupted')]])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                with store.connect() as db:
                    db.execute("INSERT INTO projects VALUES ('p','Fixture','fixture','{}',1,90)")
                    for index, (state, attempts) in enumerate(tasks):
                        task = str(index)
                        db.execute("INSERT INTO tasks(id,project,source,state,created,updated) "
                                   "VALUES (?,'p','system_evolve',?,100,160)", (task, state))
                        for attempt, (run_state, call_state, tokens) in enumerate(attempts):
                            run = f'{task}-{attempt}'
                            start = 110 + 30 * attempt
                            db.execute("INSERT INTO runs(id,task,project,state,started,ended,calls) "
                                       "VALUES (?,?,'p',?,?,?,1)", (run, task, run_state, start, start + 20))
                            db.execute("INSERT INTO calls(id,run,project,state,created,role,provider,model,effort,"
                                       "unit,tokens,usage_known,unit_price) VALUES (?,?,'p',?,?,'planner','mock',"
                                       "'fixture','low','http_request',?,1,2)", (run, run, call_state, start + 10, tokens))
                    automatic = policy_history(db, 90, 200)['automatic_task_cost']
            finally:
                store.close()
        self.assertEqual(automatic['coverage'], [{
            'project': 'p', 'source': 'system_evolve', 'tasks_with_ended_attempt': 11,
            'eligible_tasks': 8, 'excluded_incomplete_or_window_boundary': 3, 'unknown_cost_tasks': 0,
        }])
        group, = automatic['groups']
        self.assertEqual(group['samples'], 8)
        self.assertEqual(group['known_cost_samples'], 8)
        self.assertEqual(group['priced_calls_by_basis'], {'mixed': 11})
        self.assertEqual(group['cost_usd'], {'p25': 0.002, 'median': 0.002, 'p75': 0.01, 'samples': 8})

    @staticmethod
    def _seed_history(db):
        db.execute("INSERT INTO projects VALUES ('p','Fixture','fixture','{}',1,90)")
        for i in range(14):
            task = str(i)
            db.execute("INSERT INTO tasks(id,project,source,state,created,updated) "
                       "VALUES (?,'p','system_light','completed',?,150)", (task, 80 if i == 13 else 100))
            for attempt in range(2 if i == 0 else 1):
                run = f'{task}-{attempt}'
                state = 'failed' if i == 0 and attempt == 0 else 'completed'
                evidence = {} if i == 9 else {'evaluation': {'decision': 'pass',
                                                               'checks': {'fixed': [{'passed': True}]}},
                                               'review': {'decision': 'fail' if state == 'failed' else 'pass'}}
                db.execute("INSERT INTO runs(id,task,project,state,started,ended,result,calls) "
                           "VALUES (?,?,'p',?,110,140,?,?)", (run, task, state, encode(evidence), int(i != 12)))
                if i != 12:
                    db.execute("INSERT INTO calls(id,run,project,state,created,role,provider,model,effort,unit,"
                               "tokens,usage_known,unit_price) VALUES (?,?,'p',?,120,'planner','mock','fixture',?,"
                               "'http_request',?,?,?)",
                               (run, run, state, 'high' if i == 13 else 'low',
                                (i + 1) * 1000, int(i != 9), None if i == 10 else 0 if i == 11 else 2))
