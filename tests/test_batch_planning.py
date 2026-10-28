"""Independent batch planning evidence; fixtures are not production CI benchmarks."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from adaptive_rsi.batching import execute, preserve_acceptance
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.task_groups import lineage


class BatchPlanningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','p',?,'{}',1,?)", (temp.name, time.time()))
        self.tasks = [self.store.add_task('p', 'goal', name, name, evidence=[{'path': path}],
                      acceptance=['same criterion']) for name, path in [('one', 'src/a.py'), ('two', 'src/b.py')]]

    def test_recommendation_is_read_only_and_confirmation_uses_exact_snapshot(self):
        before = self.store.query('SELECT * FROM tasks ORDER BY id')
        report = execute(self.runtime, 'p', self.tasks, 'recommend')
        self.assertEqual(self.store.query('SELECT * FROM tasks ORDER BY id'), before)
        self.assertFalse(self.store.query('SELECT * FROM settings WHERE key LIKE \'batch:%\''))
        cluster = report['recommendations'][0]
        self.assertEqual(set(cluster['tasks']), set(self.tasks))
        self.assertTrue(cluster['confirmation_required'])
        self.assertTrue(cluster['full_gates_required'])
        self.store.execute('UPDATE tasks SET body=? WHERE id=?', ('changed', self.tasks[0]))
        with self.assertRaisesRegex(ValueError, 'changed'):
            execute(self.runtime, 'p', cluster['tasks'], 'group', 'confirm', False, cluster['revision'])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM tasks')["n"], 2)

    def test_recommendation_cannot_execute_without_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'preview'):
            execute(self.runtime, 'p', self.tasks, 'recommend', 'write', preview=False)
        self.assertFalse(self.store.query('SELECT * FROM task_group_members'))

    def test_missing_measurements_are_unknown_not_zero_or_speedup(self):
        report = execute(self.runtime, 'p', self.tasks, 'group')
        cost = report['verification_cost']
        for field in ('separate_seconds', 'grouped_seconds', 'shared_savings_seconds', 'failure_redo_seconds'):
            self.assertIsNone(cost[field])
        self.assertEqual(cost['ci_comparison']['status'], 'unknown')
        self.assertFalse(cost['speedup_proven'])

    def test_duplicate_criteria_have_distinct_original_evidence_entries(self):
        group = execute(self.runtime, 'p', self.tasks, 'group', 'g', False)['id']
        result = {'evaluation': {'decision': 'fail', 'criteria': ['same criterion'],
                  'coverage': [{'name': 'individual', 'criteria': [0]}],
                  'checks': {'acceptance_candidate': [{'name': 'individual', 'passed': False, 'stdout': 'failure'}]}}}
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES ('r',?,'p','failed',1,2,?)",
                           (group, json.dumps(result)))
        evidence = lineage(self.store, group, 'p')['acceptance_evidence']
        self.assertEqual(len(evidence['requirements']), 2)
        self.assertFalse(evidence['all_passed'])
        self.assertEqual({item['task'] for item in evidence['requirements']}, set(self.tasks))
        self.assertTrue(all(item['state'] == 'fail' for item in evidence['requirements']))
        self.assertEqual(evidence['requirements'][0]['checks'][0]['run'], 'r')
        self.assertEqual(evidence['requirements'][0]['checks'][0]['index'], 0)

    def test_group_requires_independent_acceptance_even_for_low_risk(self):
        from adaptive_rsi.batching import requires_independent_acceptance
        group = execute(self.runtime, 'p', self.tasks, 'group', 'g', False)['id']
        task = self.store.one('SELECT * FROM tasks WHERE id=?', (group,))
        self.assertTrue(requires_independent_acceptance(task))
        self.assertFalse(requires_independent_acceptance({'fingerprint': 'ordinary'}))

    def test_measured_local_cost_is_retained_for_later_comparison(self):
        for index, task in enumerate(self.tasks):
            result = {'evaluation': {'checks': {'candidate_full': [{'name': 'full', 'seconds': 400, 'passed': True}]}}}
            self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES (?,?,'p','failed',1,2,?)",
                               ('prior'+str(index), task, json.dumps(result)))
        preview = execute(self.runtime, 'p', self.tasks, 'group')
        self.assertEqual(preview['verification_cost']['separate_seconds'], 800)
        report = execute(self.runtime, 'p', self.tasks, 'recommend')
        self.assertFalse(report['recommendations'])
        grouped = execute(self.runtime, 'p', self.tasks, 'group', 'g', False, preview['revision'])
        saved = json.loads(self.store.one('SELECT evidence FROM tasks WHERE id=?', (grouped['id'],))['evidence'])[0]
        self.assertEqual(saved['verification_cost'], preview['verification_cost'])

    def test_missing_one_mapped_check_keeps_requirement_unknown(self):
        group = execute(self.runtime, 'p', self.tasks, 'group', 'g', False)['id']
        evaluation = {'decision': 'pass', 'criteria': ['same criterion'],
                      'coverage': [{'name': 'present', 'criteria': [0]}, {'name': 'missing', 'criteria': [0]}],
                      'checks': {'acceptance_candidate': [{'name': 'present', 'passed': True}]}}
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES ('r',?,'p','merged',1,2,?)",
                           (group, json.dumps({'evaluation': evaluation})))
        evidence = lineage(self.store, group, 'p')['acceptance_evidence']
        self.assertFalse(evidence['all_passed'])
        self.assertTrue(all(item['state'] == 'unknown' for item in evidence['requirements']))

    def test_group_summary_without_coverage_never_marks_requirements_passed(self):
        group = execute(self.runtime, 'p', self.tasks, 'group', 'g', False)['id']
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES ('r',?,'p','merged',1,2,?)",
                           (group, json.dumps({'summary': 'all passed', 'evaluation': {'decision': 'pass'}})))
        evidence = lineage(self.store, group, 'p')['acceptance_evidence']
        self.assertFalse(evidence['all_passed'])
        self.assertTrue(all(item['state'] == 'unknown' for item in evidence['requirements']))


if __name__ == '__main__':
    unittest.main()
