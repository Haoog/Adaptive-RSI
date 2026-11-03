import json
from pathlib import Path
import tempfile
import time
import unittest

from adaptive_rsi.store import Store


class LegacyQueueImportTest(unittest.TestCase):
    def test_import_is_idempotent_and_preserves_reserved_files(self):
        from adaptive_rsi.legacy_queue import import_queue

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / 'snapshot'
            queue.mkdir()
            (queue / 'be-001.md').write_text('# Task\nOriginal\n', encoding='utf-8')
            for name in ('.claim', '.hold', 'failed', 'done'):
                (queue / name).mkdir()
                (queue / name / 'other.md').write_text(name, encoding='utf-8')
            store = Store(root / 'state')
            self.addCleanup(store.close)
            store.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,?)", ('p', 'p', str(root), time.time()))
            first = import_queue(store, 'p', queue)
            second = import_queue(store, 'p', queue)
            self.assertEqual((len(first['imported']), len(second['existing'])), (1, 1))
            self.assertEqual({x['status'] for x in first['reserved']}, {'.claim', '.hold', 'failed', 'done'})
            rows = store.query('SELECT * FROM tasks')
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['source'], rows[0]['state'], rows[0]['body']), ('goal', 'queued', '# Task\nOriginal\n'))
            evidence = json.loads(rows[0]['evidence'])[0]
            self.assertEqual((evidence['legacy_id'], evidence['source_path']), ('be-001.md', str((queue / 'be-001.md').resolve())))
            self.assertEqual((queue / 'be-001.md').read_text(), '# Task\nOriginal\n')

    def test_changed_content_rolls_back_new_items(self):
        from adaptive_rsi.legacy_queue import import_queue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / 'queue'
            queue.mkdir()
            original = queue / 'z.md'
            original.write_text('original')
            store = Store(root / 'state')
            self.addCleanup(store.close)
            store.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,?)", ('p', 'p', str(root), time.time()))
            import_queue(store, 'p', queue)
            (queue / 'a.md').write_text('new')
            original.write_text('changed')
            with self.assertRaisesRegex(ValueError, 'changed'):
                import_queue(store, 'p', queue)
            self.assertEqual(len(store.query('SELECT id FROM tasks')), 1)

    def test_symlinks_and_hidden_files_are_not_imported(self):
        from adaptive_rsi.legacy_queue import import_queue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / 'queue'
            queue.mkdir()
            (queue / '.hold.md').write_text('held')
            (queue / 'target.txt').write_text('target')
            (queue / 'link.md').symlink_to(queue / 'target.txt')
            store = Store(root / 'state')
            self.addCleanup(store.close)
            store.execute("INSERT INTO projects VALUES (?,?,?,'{}',1,?)", ('p', 'p', str(root), time.time()))
            self.assertEqual(import_queue(store, 'p', queue)['imported'], [])
            self.assertEqual(store.query('SELECT id FROM tasks'), [])

    def test_requires_paused_project(self):
        from adaptive_rsi.legacy_queue import import_queue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / 'queue'
            queue.mkdir()
            (queue / 'task.md').write_text('task')
            store = Store(root / 'state')
            self.addCleanup(store.close)
            store.execute("INSERT INTO projects VALUES (?,?,?,'{}',0,?)", ('p', 'p', str(root), time.time()))
            with self.assertRaisesRegex(ValueError, 'paused'):
                import_queue(store, 'p', queue)
            self.assertEqual(store.query('SELECT id FROM tasks'), [])
