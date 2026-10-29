"""Dependency DAG admission, migrations and interruption boundaries."""
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from adaptive_rsi.experience import queue_status
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class DependencyDAGTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',1,?)", (temporary.name, time.time()))
        self.tasks = [self.store.add_task('p', 'goal', str(index), str(index)) for index in range(4)]

    def edges(self, task):
        return [row['dependency'] for row in self.store.query('SELECT dependency FROM task_dependencies WHERE task=? ORDER BY dependency', (task,))]

    def test_diamond_waits_for_every_predecessor_and_then_claims(self):
        root, left, right, leaf = self.tasks
        self.runtime.set_dependency(left, root)
        self.runtime.set_dependency(right, root)
        self.runtime.set_dependency(leaf, [left, right])
        self.store.execute("UPDATE tasks SET state='merged' WHERE id IN (?,?)", (root, left))
        status = next(row for row in queue_status(self.store, 'p') if row['id'] == leaf)
        self.assertEqual(status['wait_reason'], 'dependency')
        self.assertEqual(len(status['dependencies']), 2)
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM tasks WHERE id=? AND ' + self.store.ready_clause(), (leaf, time.time())).fetchone())
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (right,))
        self.runtime.pause('p', False)
        self.assertEqual(self.store.claim('p', self.runtime.config('p'))['task'], leaf)

    def test_cycle_via_second_predecessor_is_rejected_atomically(self):
        root, left, right, leaf = self.tasks
        self.runtime.set_dependency(leaf, [left, right])
        self.runtime.set_dependency(right, root)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.runtime.set_dependency(root, leaf)
        self.assertEqual(self.edges(root), [])
        self.assertEqual(set(self.edges(leaf)), {left, right})

    def test_missing_cross_project_duplicate_and_self_nodes_are_rejected(self):
        self.store.execute("INSERT INTO projects VALUES ('q','other','/other','{}',1,1)")
        cross = self.store.add_task('q', 'goal', 'cross', 'cross')
        for value in (['missing'], [cross], [self.tasks[0]], [self.tasks[1], self.tasks[1]], [1], list(map(str, range(101)))):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.runtime.set_dependency(self.tasks[0], value)
                self.assertEqual(self.edges(self.tasks[0]), [])

    def test_cancelled_parent_blocks_child_but_preserves_other_edges(self):
        root, left, right, leaf = self.tasks
        self.runtime.set_dependency(leaf, [left, right])
        self.runtime.cancel_task(left)
        self.runtime.release_dead_dependencies()
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (leaf,))['state'], 'blocked')
        self.assertEqual(self.edges(leaf), [right])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='dependency_released'")['n'], 1)
        self.runtime.release_dead_dependencies()
        self.assertEqual(self.edges(leaf), [right])

    def test_dead_parent_audit_failure_rolls_back_edge_removal(self):
        self.runtime.set_dependency(self.tasks[0], [self.tasks[1], self.tasks[2]])
        self.runtime.cancel_task(self.tasks[1])
        self.store.execute("CREATE TRIGGER reject_dead_parent BEFORE INSERT ON events WHEN NEW.kind='dependency_released' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.release_dead_dependencies()
        self.assertEqual(len(self.edges(self.tasks[0])), 2)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.tasks[0],))['state'], 'queued')

    def test_missing_historical_parent_is_not_treated_as_merged(self):
        with self.store.connect() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.tasks[0], 'missing'))
        status = next(row for row in queue_status(self.store, 'p') if row['id'] == self.tasks[0])
        self.assertEqual(status['wait_reason'], 'dependency')
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM tasks WHERE id=? AND ' + self.store.ready_clause(), (self.tasks[0], time.time())).fetchone())

    def test_ancestry_budget_is_combined_across_all_selected_parents(self):
        nodes = [self.store.add_task('p', 'goal', 'node'+str(index), 'node') for index in range(1001)]
        with self.store.connect() as db:
            db.executemany('INSERT INTO task_dependencies VALUES (?,?)', [(nodes[index], nodes[index+1]) for index in range(0, 499)] + [(nodes[index], nodes[index+1]) for index in range(500, 1000)])
        with self.assertRaisesRegex(ValueError, '1000 ancestors'):
            self.runtime.set_dependency(self.tasks[0], [nodes[0], nodes[500]])
        self.assertEqual(self.edges(self.tasks[0]), [])

    def test_legacy_single_edge_migrates_and_duplicate_edges_remain_forbidden(self):
        self.runtime.set_dependency(self.tasks[0], self.tasks[1])
        with self.store.connect() as db:
            db.execute('DROP TABLE task_dependencies')
            db.execute('CREATE TABLE task_dependencies(task TEXT PRIMARY KEY,dependency TEXT)')
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.tasks[0], self.tasks[1]))
            db.execute('CREATE INDEX task_dependencies_dependency ON task_dependencies(dependency)')
            db.execute('DELETE FROM schema_migrations WHERE version>=6')
            for column in ('input_price', 'output_price', 'cache_read_price', 'cache_write_price', 'price_basis'):
                db.execute('ALTER TABLE calls DROP COLUMN ' + column)
        self.store.close()
        reopened = Store(self.store.root)
        try:
            self.assertEqual(reopened.one('SELECT dependency FROM task_dependencies WHERE task=?', (self.tasks[0],))['dependency'], self.tasks[1])
            reopened.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.tasks[0], self.tasks[2]))
            with self.assertRaises(sqlite3.IntegrityError):
                reopened.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.tasks[0], self.tasks[2]))
        finally:
            reopened.close()

    def test_invalid_legacy_edge_blocks_migration_without_erasing_evidence(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE task_dependencies')
            db.execute('CREATE TABLE task_dependencies(task TEXT PRIMARY KEY,dependency TEXT)')
            db.execute('INSERT INTO task_dependencies VALUES (?,?)', (self.tasks[0], self.tasks[0]))
            db.execute('CREATE INDEX task_dependencies_dependency ON task_dependencies(dependency)')
            db.execute('DELETE FROM schema_migrations WHERE version>=6')
            for column in ('input_price', 'output_price', 'cache_read_price', 'cache_write_price', 'price_basis'):
                db.execute('ALTER TABLE calls DROP COLUMN ' + column)
        self.store.close()
        with self.assertRaises(sqlite3.IntegrityError):
            Store(self.store.root)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT task,dependency FROM task_dependencies').fetchone(), (self.tasks[0], self.tasks[0]))
            self.assertIsNone(db.execute('SELECT 1 FROM schema_migrations WHERE version=6').fetchone())
