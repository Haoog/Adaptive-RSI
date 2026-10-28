"""Independent blockers remain visible when one condition is cleared."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.blocking import explain
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class BlockingCausesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',1,?)", (temporary.name, time.time()))
        self.root = self.task('root')
        self.first = self.task('first')
        self.second = self.task('second')
        self.link(self.root, self.first)
        self.link(self.first, self.second)
        self.opening = patch.object(self.runtime, 'next_effective_schedule', side_effect=lambda project, now: now)
        self.opening.start()
        self.addCleanup(self.opening.stop)

    def task(self, title, project='p'):
        return self.store.add_task(project, 'goal', title, title)

    def link(self, task, dependency):
        self.store.execute('INSERT INTO task_dependencies VALUES (?,?)', (task, dependency))

    def test_paused_backoff_and_chain_remain_independent(self):
        self.store.execute('UPDATE tasks SET available=? WHERE id=?', (time.time() + 300, self.root))
        report = explain(self.runtime, 'p', self.root)
        self.assertEqual(report['shared_reasons'], [{'code': 'paused'}])
        nodes = {node['id']: node for node in report['nodes']}
        self.assertEqual({reason['code'] for reason in nodes[self.root]['reasons']}, {'backoff', 'dependency'})
        self.assertEqual(nodes[self.first]['reasons'], [{'code': 'dependency', 'parents': [self.second]}])
        self.store.execute('UPDATE projects SET paused=0')
        report = explain(self.runtime, 'p', self.root)
        self.assertEqual(report['shared_reasons'], [])
        self.assertEqual(len(next(node for node in report['nodes'] if node['id'] == self.root)['reasons']), 2)
        self.store.execute('UPDATE tasks SET available=0 WHERE id=?', (self.root,))
        report = explain(self.runtime, 'p', self.root)
        self.assertEqual(next(node for node in report['nodes'] if node['id'] == self.root)['reasons'],
                         [{'code': 'dependency', 'parents': [self.first]}])

    def test_merging_parent_removes_that_chain_without_hiding_other_parent(self):
        other = self.task('parallel-parent')
        self.link(self.root, other)
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (self.first,))
        report = explain(self.runtime, 'p', self.root)
        self.assertNotIn(self.first, {node['id'] for node in report['nodes']})
        self.assertEqual(report['edges'], [{'task': self.root, 'dependency': other}])

    def test_corrupt_legacy_foreign_or_missing_dependency_never_exposes_foreign_title(self):
        self.store.execute("INSERT INTO projects VALUES ('foreign','foreign',?,'{}',1,?)", (str(self.store.root / 'foreign'), time.time()))
        foreign = self.task('PRIVATE-OTHER-PROJECT-TITLE', 'foreign')
        # Legacy corruption is inserted with FK enforcement off only in this test.
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.root, foreign))
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.first, 'missing'))
        report = explain(self.runtime, 'p', self.root)
        self.assertNotIn('PRIVATE-OTHER-PROJECT-TITLE', str(report))
        self.assertEqual(sum(reason['code'] == 'invalid_dependency' for node in report['nodes'] for reason in node['reasons']), 2)
        with self.assertRaises(ValueError):
            explain(self.runtime, 'p', foreign)

    def test_cycle_and_graph_truncation_remain_bounded_and_explicit(self):
        self.link(self.second, self.root)
        report = explain(self.runtime, 'p', self.root, limit=2)
        self.assertTrue(report['truncated'])
        self.assertLessEqual(len(report['nodes']), 2)
        self.assertLessEqual(len(report['edges']), 2)
        self.assertEqual(len(explain(self.runtime, 'p', self.root)['nodes']), 3)

    def test_working_window_and_resource_pressure_coexist(self):
        self.opening.stop()
        now = time.time()
        self.runtime.resource_pressure.projects['p'] = {'checked_at': now, 'admitted': False,
            'min_free_disk_bytes': 0, 'min_available_memory_bytes': 0, 'reasons': ['memory']}
        with patch.object(self.runtime, 'next_effective_schedule', return_value=now + 300):
            report = explain(self.runtime, 'p', self.root)
        self.assertEqual({reason['code'] for reason in report['shared_reasons']}, {'paused', 'working_hours', 'resource_pressure'})
        self.runtime.configure({'limits': {'min_free_disk_mb': 1}}, 'p')
        report = explain(self.runtime, 'p', self.root)
        self.assertNotIn('resource_pressure', {reason['code'] for reason in report['shared_reasons']})

    def test_global_capacity_from_another_project_clears_without_hiding_local_causes(self):
        self.store.execute("INSERT INTO projects VALUES ('other','other',?,'{}',0,?)", (str(self.store.root / 'other'), time.time()))
        other = self.task('PRIVATE-OTHER-RUN', 'other')
        self.store.execute("INSERT INTO runs(id,project,task,state,started) VALUES ('private-run','other',?,'executing',?)", (other, time.time()))
        report = explain(self.runtime, 'p', self.root)
        self.assertIn({'code': 'capacity', 'active': 1, 'capacity': 1}, report['shared_reasons'])
        self.assertNotIn('private-run', str(report))
        self.store.execute('UPDATE runs SET ended=?', (time.time(),))
        report = explain(self.runtime, 'p', self.root)
        self.assertNotIn('capacity', {reason['code'] for reason in report['shared_reasons']})
        self.assertIn({'code': 'paused'}, report['shared_reasons'])
        self.assertTrue(next(node for node in report['nodes'] if node['id'] == self.root)['reasons'])

    def test_explanation_does_not_recover_or_delete_update_barrier(self):
        lease = {'phase': 'draining', 'expires': 0, 'lease': 'stale-token', 'version': '99.0.0'}
        self.store.put_setting('desktop-update-barrier', lease)
        with patch.object(self.runtime.updates, 'is_busy', side_effect=AssertionError('mutating recovery')):
            report = explain(self.runtime, 'p', self.root)
        self.assertIn({'code': 'update_in_progress'}, report['shared_reasons'])
        self.assertEqual(self.store.setting('desktop-update-barrier'), lease)
