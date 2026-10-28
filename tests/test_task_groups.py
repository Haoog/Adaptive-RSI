"""Failed group splitting preserves provenance and never manufactures completion evidence."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.batching import execute
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.task_groups import lineage


class TaskGroupsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',1,?)", (str(self.root / 'project'), time.time()))
        self.originals = [self.store.add_task('p', 'goal', name, name, body='Requirement ' + name,
                          acceptance=[name + ' passes']) for name in ('one', 'two')]
        self.group = execute(self.runtime, 'p', self.originals, 'group', 'group', preview=False)['id']

    def fail(self):
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (self.group,))
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES "
                           "('failed-group',?,'p','failed',1,2,?)",
                           (self.group, json.dumps({'summary': 'One group requirement failed'})))

    def split(self, request='split'):
        preview = execute(self.runtime, 'p', [self.group], 'split_retry')
        return execute(self.runtime, 'p', [self.group], 'split_retry', request,
                       preview=False, expected=preview['revision'])

    def test_failed_group_split_restores_original_requirements_and_preserves_failure(self):
        self.fail()
        report = self.split()
        self.assertTrue(report['complete'])
        self.assertEqual({row['id'] for row in report['receipts']}, set(self.originals))
        for identifier, name in zip(self.originals, ('one', 'two')):
            row = self.store.one('SELECT * FROM tasks WHERE id=?', (identifier,))
            self.assertEqual(row['state'], 'queued')
            self.assertEqual(row['body'], 'Requirement ' + name)
            self.assertEqual(json.loads(row['acceptance']), [name + ' passes'])
            self.assertTrue(lineage(self.store, identifier, 'p')['groups'][0]['restored'])
        self.assertEqual(self.store.one("SELECT state FROM runs WHERE id='failed-group'")['state'], 'failed')
        self.assertFalse(self.store.query('SELECT * FROM task_completions'))
        self.runtime.pause('p', False)
        claimed = self.store.claim('p', self.runtime.config('p'))
        self.assertIn(claimed['task'], self.originals)

    def test_retrying_same_split_request_returns_identical_receipt(self):
        self.fail()
        result = self.split()
        repeated = execute(self.runtime, 'p', [self.group], 'split_retry', 'split', preview=False, expected='old')
        self.assertEqual(repeated, result)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='group_split_retry'")['n'], 1)

    def test_changed_split_preview_rejects_every_original(self):
        self.fail()
        preview = execute(self.runtime, 'p', [self.group], 'split_retry')
        self.store.execute('UPDATE tasks SET priority=priority+1 WHERE id=?', (self.originals[0],))
        with self.assertRaisesRegex(ValueError, 'preview changed'):
            execute(self.runtime, 'p', [self.group], 'split_retry', 'split', preview=False, expected=preview['revision'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='superseded'")['n'], 2)

    def test_group_requirement_revisions_are_retained_in_independently_verified_task(self):
        self.fail()
        self.runtime.revise_task(self.group, 'New shared requirement')
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (self.group,))
        revised = self.store.one('SELECT * FROM tasks WHERE id=?', (self.group,))
        preview = execute(self.runtime, 'p', [self.group], 'split_retry')
        self.assertTrue(preview['supplement_required'])
        result = self.split()
        supplement = next(row for row in result['receipts'] if row.get('revised_specification'))
        task = self.store.one('SELECT * FROM tasks WHERE id=?', (supplement['id'],))
        self.assertEqual(task['body'], revised['body'])
        self.assertEqual(task['acceptance'], revised['acceptance'])
        self.assertEqual(task['state'], 'queued')
        self.assertFalse(self.store.query('SELECT * FROM task_completions'))
        self.assertEqual(len(execute(self.runtime, 'p', [self.group], 'split_retry', 'split', preview=False)['receipts']), 3)

    def test_unstarted_group_revision_cannot_be_discarded_by_ungroup(self):
        self.runtime.revise_task(self.group, 'New shared requirement')
        with self.assertRaisesRegex(ValueError, 'reconcile revisions'):
            execute(self.runtime, 'p', [self.group], 'ungroup', 'undo', preview=False)

    def test_lineage_pages_do_not_drop_older_memberships(self):
        for index in range(105):
            group = self.store.add_task('p', 'goal', 'old-' + str(index), 'History')
            self.store.execute('INSERT INTO task_group_members(group_task,original_task) VALUES (?,?)',
                               (group, self.originals[0]))
        first = lineage(self.store, self.originals[0], 'p')
        self.assertTrue(first['groups_truncated'])
        second = lineage(self.store, self.originals[0], 'p', first['next_before'])
        self.assertFalse(second['groups_truncated'])
        ids = [row['id'] for row in first['groups'] + second['groups']]
        self.assertEqual(len(ids), 106)
        self.assertEqual(len(set(ids)), 106)

    def test_active_run_must_stop_before_split(self):
        self.fail()
        self.store.execute("UPDATE runs SET ended=NULL WHERE id='failed-group'")
        with self.assertRaisesRegex(ValueError, 'stop before splitting'):
            self.split()

    def test_old_group_cannot_restore_members_owned_by_a_new_group(self):
        self.fail()
        self.split()
        execute(self.runtime, 'p', self.originals, 'group', 'new-group', preview=False)
        with self.assertRaisesRegex(ValueError, 'Original task changed'):
            execute(self.runtime, 'p', [self.group], 'split_retry', 'different-request', preview=False)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='superseded'")['n'], 2)

    def test_split_preserves_user_reservation_instead_of_silently_running_early(self):
        reserved = time.time() + 86400
        self.store.execute('UPDATE tasks SET available=? WHERE id IN (?,?)', (reserved, *self.originals))
        self.fail()
        self.split()
        for identifier in self.originals:
            self.assertEqual(self.store.one('SELECT available FROM tasks WHERE id=?', (identifier,))['available'], reserved)

    def test_split_audit_failure_rolls_back_group_members_and_receipt(self):
        self.fail()
        preview = execute(self.runtime, 'p', [self.group], 'split_retry')
        self.store.execute("CREATE TRIGGER fail_split BEFORE INSERT ON events WHEN NEW.kind='group_split_retry' "
                           "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            execute(self.runtime, 'p', [self.group], 'split_retry', 'split', preview=False, expected=preview['revision'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='superseded'")['n'], 2)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.group,))['state'], 'failed')
        self.assertIsNone(self.store.setting('batch:p:split'))
        self.assertFalse(self.store.query('SELECT * FROM task_group_members WHERE restored IS NOT NULL'))

    def test_lineage_links_whole_group_evidence_without_marking_originals_passed(self):
        self.fail()
        row = lineage(self.store, self.originals[0], 'p')['groups'][0]
        self.assertEqual(row['id'], self.group)
        self.assertEqual(row['evidence_scope'], 'whole_group')
        self.assertEqual(row['latest_run']['id'], 'failed-group')
        self.assertIn('failed', row['latest_run']['summary'])
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.originals[0],))['state'], 'superseded')
        self.assertEqual({row['id'] for row in lineage(self.store, self.group, 'p')['members']}, set(self.originals))

    def test_legacy_group_members_are_backfilled_without_changing_original_states(self):
        self.store.execute('DROP TABLE task_group_members')
        self.store.execute('DELETE FROM schema_migrations WHERE version>=5')
        self.store.close()
        reopened = Store(self.store.root)
        try:
            self.assertEqual(lineage(reopened, self.originals[0], 'p')['groups'][0]['id'], self.group)
            self.assertEqual(reopened.one('SELECT state FROM tasks WHERE id=?', (self.originals[0],))['state'], 'superseded')
        finally:
            reopened.close()

    def test_batch_retry_crash_after_effect_is_reconciled_without_repeating_effect(self):
        task = self.store.add_task('p', 'goal', 'retry-crash', 'Retry crash')
        self.store.execute("UPDATE tasks SET state='failed' WHERE id=?", (task,))
        original = self.store.put_setting
        def fail_receipt(key, value):
            if key == 'batch:p:crash:' + task and value.get('state') == 'accepted':
                raise OSError('receipt write interrupted after business effect')
            return original(key, value)
        with patch.object(self.store, 'put_setting', side_effect=fail_receipt):
            with self.assertRaises(OSError):
                execute(self.runtime, 'p', [task], 'retry', 'crash', preview=False)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')
        result = execute(self.runtime, 'p', [task], 'retry', 'crash', preview=False)
        self.assertTrue(result['complete'])
        self.assertTrue(result['receipts'][0]['reconciled'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='manual_retry'")['n'], 1)

    def test_conflict_preview_covers_parents_renames_case_and_unicode(self):
        from adaptive_rsi.batching import normalized_scope, overlapping_scopes
        samples = [('src', 'src/a.py'), ('SRC/A.py', 'src/a.py'),
                   ('cafe\u0301/main.py', 'caf\u00e9/main.py'), ('src\\old.py', 'src/old.py')]
        for first, second in samples:
            with self.subTest(first=first, second=second):
                self.assertTrue(overlapping_scopes({normalized_scope(first)}, {normalized_scope(second)}))
        self.assertFalse(overlapping_scopes({'src'}, {'src-other/file.py'}))
        self.store.execute('UPDATE tasks SET evidence=? WHERE id=?',
                           (json.dumps([{'old_path': 'src/old.py', 'new_path': 'lib/new.py'}]), self.originals[0]))
        self.store.execute('UPDATE tasks SET evidence=? WHERE id=?',
                           (json.dumps([{'path': 'lib'}]), self.originals[1]))
        report = execute(self.runtime, 'p', self.originals, 'retry')
        self.assertEqual(report['conflicts'][0]['paths'], ['lib/new.py'])

    def test_merged_group_exposes_outcome_but_does_not_invent_individual_passes(self):
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (self.group,))
        self.store.execute("INSERT INTO runs(id,task,project,state,started,ended,result) VALUES ('merged-group',?,'p','merged',1,2,?)",
                           (self.group, json.dumps({'summary': 'Whole group passed', 'pr': 'https://example.test/pr/1'})))
        outcome = lineage(self.store, self.originals[0], 'p')['groups'][0]
        self.assertEqual(outcome['state'], 'merged')
        self.assertEqual(outcome['latest_run']['pr'], 'https://example.test/pr/1')
        self.assertFalse(self.store.query('SELECT * FROM task_completions'))
        with self.assertRaisesRegex(ValueError, 'stopped failed or cancelled'):
            self.split()

    def test_cancelled_group_can_restore_sources_for_fresh_verification(self):
        self.runtime.cancel_task(self.group)
        result = self.split()
        self.assertEqual(len(result['receipts']), 2)
        self.assertTrue(all(row['fresh_verification_required'] for row in result['receipts']))
        self.assertFalse(self.store.query('SELECT * FROM task_completions'))

    def test_batch_receipt_survives_store_reopen(self):
        self.fail()
        result = self.split()
        self.store.close()
        reopened = Store(self.store.root)
        try:
            self.assertEqual(reopened.setting('batch:p:split'), result)
        finally:
            reopened.close()
