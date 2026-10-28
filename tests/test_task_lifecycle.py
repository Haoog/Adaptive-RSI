"""Cancellation impact and exact requirement history at transactional boundaries."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store
from adaptive_rsi.task_lifecycle import cancellation_preview, intervention_history
from adaptive_rsi.task_revisions import revision_detail, revision_page


class TaskLifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        for project in ('p', 'q'):
            self.store.execute('INSERT INTO projects VALUES (?,?,?,\'{}\',1,?)',
                               (project, project, str(Path(temporary.name) / project), time.time()))

    def task(self, name, project='p'):
        return self.store.add_task(project, 'goal', name, name, 'Original requirements')

    def preview(self, task):
        with self.store.connect() as db:
            db.execute('BEGIN')
            return cancellation_preview(db, task)

    def chain(self):
        a, b, c = [self.task(name) for name in ('root', 'child', 'grandchild')]
        self.runtime.set_dependency(b, a)
        self.runtime.set_dependency(c, b)
        return a, b, c

    def test_default_cancellation_does_not_expand_scope(self):
        a, b, c = self.chain()
        plan = self.preview(a)
        self.assertEqual({row['id'] for row in plan['affected']}, {b, c})
        report = self.runtime.cancel_task(a, expected=plan['revision'])
        self.assertEqual({row['id'] for row in report['receipts'] if row['state'] == 'cancelled'}, {a})
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (b,))['state'], 'queued')

    def test_explicit_cascade_matches_preview_and_protects_finished_tasks(self):
        a, b, c = self.chain()
        self.store.execute("UPDATE tasks SET state='merged' WHERE id=?", (c,))
        plan = self.preview(a)
        report = self.runtime.cancel_task(a, cascade=True, expected=plan['revision'])
        self.assertEqual({row['id'] for row in report['receipts']}, {a, b, c})
        self.assertEqual({row['id'] for row in report['receipts'] if row['state'] == 'cancelled'}, {a, b})
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (c,))['state'], 'merged')
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='cancelled'")['n'], 2)

    def test_changed_preview_rejects_entire_cascade(self):
        a, b, c = self.chain()
        plan = self.preview(a)
        self.runtime.set_dependency(c, a, urgency=1)
        with self.assertRaisesRegex(ValueError, 'preview changed'):
            self.runtime.cancel_task(a, cascade=True, expected=plan['revision'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='cancelled'")['n'], 0)

    def test_cascade_requires_preview_and_strict_boolean(self):
        task = self.task('root')
        with self.assertRaisesRegex(ValueError, 'Preview'):
            self.runtime.cancel_task(task, cascade=True)
        with self.assertRaisesRegex(ValueError, 'boolean'):
            self.runtime.cancel_task(task, cascade='false')

    def test_cross_project_link_is_not_an_authorized_cancellation(self):
        root, foreign = self.task('root'), self.task('foreign', 'q')
        self.store.execute('INSERT INTO task_dependencies VALUES (?,?)', (foreign, root))
        plan = self.preview(root)
        self.assertEqual(plan['affected'], [])
        self.runtime.cancel_task(root, cascade=True, expected=plan['revision'])
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (foreign,))['state'], 'queued')

    def test_invalid_legacy_cycle_terminates_preview(self):
        a, b = self.task('a'), self.task('b')
        self.store.execute('INSERT INTO task_dependencies VALUES (?,?)', (a, b))
        self.store.execute('INSERT INTO task_dependencies VALUES (?,?)', (b, a))
        self.assertEqual([row['id'] for row in self.preview(a)['affected']], [b])

    def test_audit_failure_rolls_back_all_cancellations(self):
        a, b, c = self.chain()
        plan = self.preview(a)
        self.store.execute("CREATE TRIGGER fail_cancel BEFORE INSERT ON events WHEN NEW.kind='cancelled' "
                           "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.cancel_task(a, cascade=True, expected=plan['revision'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='cancelled'")['n'], 0)

    def test_revision_snapshots_reconstruct_each_result_and_preserve_source_text(self):
        task = self.task('requirements')
        self.runtime.revise_task(task, 'First supplement')
        self.runtime.revise_task(task, 'Second supplement')
        rows = self.store.query('SELECT id FROM task_revisions WHERE task=? ORDER BY id', (task,))
        first, second = [revision_detail(self.store, row['id'], 'p') for row in rows]
        self.assertEqual(first['previous_body'], 'Original requirements')
        self.assertEqual(first['after_body'], second['previous_body'])
        self.assertEqual(second['after_body'], self.store.one('SELECT body FROM tasks WHERE id=?', (task,))['body'])
        self.assertIn('+Second supplement', second['diff'])
        self.assertTrue(second['impact']['fresh_verification_required'])
        with self.assertRaisesRegex(ValueError, 'not found'):
            revision_detail(self.store, first['id'], 'q')

    def test_legacy_revision_does_not_invent_resulting_requirements(self):
        task = self.task('legacy')
        self.store.execute('INSERT INTO task_revisions(task,previous_body,supplement,created) VALUES (?,?,?,?)',
                           (task, 'before', 'supplement', time.time()))
        row = self.store.one('SELECT id FROM task_revisions WHERE task=?', (task,))
        result = revision_detail(self.store, row['id'], 'p')
        self.assertFalse(result['snapshot_available'])
        self.assertIsNone(result['after_body'])

    def test_revision_snapshot_failure_does_not_modify_requirements(self):
        task = self.task('atomic')
        self.store.execute('CREATE TRIGGER fail_snapshot BEFORE INSERT ON task_revision_snapshots '
                           "BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.revise_task(task, 'must not stick')
        self.assertEqual(self.store.one('SELECT body FROM tasks WHERE id=?', (task,))['body'], 'Original requirements')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM task_revisions')['n'], 0)

    def test_intervention_reason_is_optional_and_retained_verbatim_with_effects(self):
        task = self.task('reason')
        reason = '  用户说明\nkeep original wording  '
        self.runtime.revise_task(task, 'New requirement', reason=reason)
        event = json.loads(self.store.one("SELECT data FROM events WHERE kind='requirements_revised'")['data'])
        self.assertEqual(event['reason'], reason)
        self.assertNotEqual(event['before']['body_hash'], event['after']['body_hash'])
        detail = revision_detail(self.store, event['revision'], 'p')
        self.assertEqual(detail['impact']['reason'], reason)
        self.runtime.cancel_task(task, reason=reason)
        event = json.loads(self.store.one("SELECT data FROM events WHERE kind='cancelled'")['data'])
        self.assertEqual(event['reason'], reason)
        self.assertEqual(event['after']['state'], 'cancelled')
        self.runtime.retry(task)
        event = json.loads(self.store.one("SELECT data FROM events WHERE kind='manual_retry'")['data'])
        self.assertIsNone(event['reason'])
        self.assertEqual(event['before']['state'], 'cancelled')

    def test_invalid_intervention_reason_has_no_side_effects(self):
        task = self.task('bounded reason')
        for reason in ('x' * 2001, {}, 1):
            with self.assertRaisesRegex(ValueError, '2000'):
                self.runtime.cancel_task(task, reason=reason)
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (task,))['state'], 'queued')

    def test_intervention_pages_are_stable_when_new_events_arrive(self):
        task = self.task('history')
        with self.store.connect() as db:
            for index in range(205):
                db.execute("INSERT INTO events(project,kind,data,created) VALUES ('p','manual_retry',?,?)",
                           (json.dumps({'task': task, 'reason': str(index)}), time.time()))
        first = intervention_history(self.store, task, 'p')
        self.assertEqual(len(first['entries']), 100)
        self.store.event('cancelled', 'newer', 'p', data={'task': task, 'reason': 'newer'})
        second = intervention_history(self.store, task, 'p', first['next_before'])
        third = intervention_history(self.store, task, 'p', second['next_before'])
        rows = first['entries'] + second['entries'] + third['entries']
        self.assertEqual(len(rows), 205)
        self.assertEqual(len({row['seq'] for row in rows}), 205)
        self.assertFalse(third['has_more'])
        self.assertNotIn('newer', {row['data']['reason'] for row in rows})
        with self.assertRaisesRegex(ValueError, 'not found'):
            intervention_history(self.store, task, 'q')

    def test_old_revisions_remain_accessible_after_more_than_one_hundred_edits(self):
        task = self.task('long history')
        for index in range(105):
            self.runtime.revise_task(task, str(index))
        first = revision_page(self.store, task, 'p')
        second = revision_page(self.store, task, 'p', first['next_before'])
        self.assertEqual(len(first['revisions']), 100)
        self.assertEqual(len(second['revisions']), 5)
        oldest = revision_detail(self.store, second['revisions'][-1]['id'], 'p')
        self.assertEqual(oldest['previous_body'], 'Original requirements')
        self.assertEqual(oldest['after_body'], 'Original requirements\n\n0')
        self.assertFalse(second['has_more'])

    def test_delivery_approval_history_identifies_candidate_and_publication_effect(self):
        task = self.task('approval')
        outcome = {'evaluation': {'decision': 'pass'}, 'review': {'decision': 'pass'}}
        self.store.execute("INSERT INTO runs(id,project,task,state,result,started,ended) "
                           "VALUES ('candidate','p',?,'awaiting_approval',?,?,?)",
                           (task, json.dumps(outcome), time.time(), time.time()))
        publication = self.runtime.deliver('candidate', reason='人工核对通过')
        history = intervention_history(self.store, task, 'p')
        event = next(row for row in history['entries'] if row['kind'] == 'delivery_approved')
        self.assertEqual(event['run'], 'candidate')
        self.assertEqual(event['data']['candidate'], 'candidate')
        self.assertEqual(event['data']['reason'], '人工核对通过')
        self.assertEqual(event['data']['before']['candidate_state'], 'awaiting_approval')
        self.assertEqual(event['data']['after'], {'publication_task': publication, 'publication_state': 'queued'})

    def test_large_impact_still_allows_stopping_root_but_forbids_unpreviewed_cascade(self):
        root = self.task('large root')
        with self.store.connect() as db:
            parent = root
            for index in range(1001):
                child = 'child-' + str(index)
                db.execute("INSERT INTO tasks(id,project,source,fingerprint,title,state,created,updated) "
                           "VALUES (?,'p','goal',?,?,'queued',?,?)", (child, child, child, time.time(), time.time()))
                db.execute('INSERT INTO task_dependencies VALUES (?,?)', (child, parent))
                parent = child
        plan = self.preview(root)
        self.assertTrue(plan['truncated'])
        self.assertEqual(len(plan['affected']), 999)
        with self.assertRaisesRegex(ValueError, 'truncated'):
            self.runtime.cancel_task(root, cascade=True, expected=plan['revision'])
        report = self.runtime.cancel_task(root, expected=plan['revision'])
        self.assertTrue(report['impact_truncated'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks WHERE state='cancelled'")['n'], 1)

    def test_approval_audit_failure_rolls_back_new_publication_and_requeue(self):
        for previous in (None, 'cancelled'):
            with self.subTest(previous=previous):
                name = 'candidate-' + str(previous)
                task = self.task('approval-' + str(previous))
                outcome = {'evaluation': {'decision': 'pass'}, 'review': {'decision': 'pass'}}
                self.store.execute("INSERT INTO runs(id,project,task,state,result) VALUES (?,'p',?,'awaiting_approval',?)",
                                   (name, task, json.dumps(outcome)))
                publication = None
                if previous:
                    publication = self.store.add_task('p', 'system_publish', 'publish:' + name, 'publish', name)
                    self.store.execute('UPDATE tasks SET state=? WHERE id=?', (previous, publication))
                self.store.execute("CREATE TRIGGER fail_approval BEFORE INSERT ON events WHEN NEW.kind='delivery_approved' "
                                   "BEGIN SELECT RAISE(ABORT,'injected'); END")
                with self.assertRaises(sqlite3.IntegrityError):
                    self.runtime.deliver(name, reason='must be atomic')
                self.store.execute('DROP TRIGGER fail_approval')
                row = self.store.one('SELECT state,evidence FROM tasks WHERE fingerprint=?', ('publish:' + name,))
                if publication:
                    self.assertEqual(row, {'state': previous, 'evidence': '[]'})
                else:
                    self.assertIsNone(row)
                self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='delivery_approved'")['n'], 0)
