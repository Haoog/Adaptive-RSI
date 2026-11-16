"""Offline lifecycle regressions: real Store/maintenance, no Git or Docker."""
import copy
import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import delivery, delivery_generation
from adaptive_rsi import generation_retention as retention
from adaptive_rsi import maintenance
from adaptive_rsi.config import DEFAULTS, fingerprint
from adaptive_rsi.delivery_generation import record_verified_merge
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode
from tests.test_generation_publication import PublicationFixture


class GenerationRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state')
        self.addCleanup(self.store.close)
        self.rt = Runtime(self.store, agent=object())
        self.addCleanup(self.rt.close)
        self.now = time.time()
        self.old = self.now - 91 * 86400
        self.directory = self.store.root / 'runs' / 'r'
        self.directory.mkdir(parents=True)
        self.patch = self.directory / 'candidate.patch'
        self.patch.write_text('rollback survives')
        self.store.execute("INSERT INTO projects VALUES ('p','test','/unused','{}',0,0)")
        self.store.execute("INSERT INTO tasks(id,project,state,created,updated) VALUES ('t','p','merged',1,?)", (self.old,))
        self.store.execute("INSERT INTO runs(id,task,project,state,base,config,path,ended,result) "
                           "VALUES ('r','t','p','merged',?,?,?,?,'{}')",
                           ('a' * 40, encode(DEFAULTS), str(self.directory), self.old))
        generation = '1' * 32
        self.child = self.directory / 'generations' / ('2' * 32)
        (self.child / 'candidate').mkdir(parents=True)
        (self.child / 'candidate' / 'derived').write_text('ours')
        evaluation = {'decision': 'pass', 'source': {'base': 'b' * 40,
            'tree': 'c' * 40, 'patch_hash': 'd' * 64}}
        self.record = {'version': 1, 'generation': generation,
            'ref': 'refs/adaptive-rsi/generations/' + generation, 'commit': 'e' * 40,
            'parent': 'b' * 40, 'tree': 'c' * 40, 'approval': 'approval',
            'authorization': 'f' * 64, 'directory': str(self.child), 'snapshot': 'a' * 64,
            'environment': 'b' * 64, 'evaluation': evaluation,
            'evidence_hash': fingerprint(evaluation), 'patch_hash': 'd' * 64}
        self.intent = {'version': 2, 'generation': generation, 'ref': self.record['ref'],
            'branch': 'fix/rsi-r', 'head': self.record['commit'], 'tree': self.record['tree'],
            'base': self.record['parent'], 'repository': 'example/project', 'approval': 'approval',
            'authorization': self.record['authorization'], 'evidence': self.record['evidence_hash'],
            'environment': self.record['environment'], 'created': self.old}
        self.policy = {'head': self.record['commit'], 'base': self.record['parent'],
                       'branch': 'main', 'repository': 'example/project'}
        self.pr = {'url': 'https://github.com/example/project/pull/1', 'state': 'MERGED',
            'headRefOid': self.record['commit'], 'headRefName': self.intent['branch'],
            'baseRefName': 'main', 'headRepository': {'nameWithOwner': 'example/project'},
            'headRepositoryOwner': {'login': 'example'}, 'mergeCommit': {'oid': 'f' * 40}}
        self.outcome = {'pr': self.pr['url'], 'delivery_generation': generation,
            'delivery_status': 'merged', 'merged_sha': 'f' * 40, 'merged_at': self.old,
            'monitoring_completed': True}
        for key, value in {
                'delivery-generation:r': self.record, 'delivery-push:r': self.intent,
                'delivery:r': self.policy,
                'delivery-candidate:r': {'version': 1, 'branch': self.intent['branch'], 'repository': 'example/project',
                    'base': 'a' * 40, 'head': '0' * 40, 'tree': '1' * 40, 'evidence': '0' * 64,
                    'approval': 'first-approval', 'authorization': '1' * 64},
                'run-baseline:r': {'repository': 'example/project', 'branch': 'main', 'sha': 'a' * 40},
                'delivery-generation-pr:r': {'url': self.pr['url'], 'generation': generation}}.items():
            self.store.put_setting(key, value)
        self.store.execute('UPDATE runs SET result=? WHERE id=\'r\'', (encode(self.outcome),))
        self.confirm()

    def confirm(self):
        run = self.store.one("SELECT * FROM runs WHERE id='r'")
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assertTrue(record_verified_merge(db, run, self.policy, self.pr))

    def cycle(self):
        self.store.put_setting('maintenance-last', 0)
        self.rt.maintenance()

    def assert_held(self):
        self.assertEqual(maintenance.retention_hold(self.store, 'r', 'p'), 'delivery-generation:r')
        self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertTrue(self.child.exists())
        self.assertEqual(self.patch.read_text(), 'rollback survives')

    def test_retention_days_files_and_90_day_settings_are_separate(self):
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.now - 31 * 86400,))
        self.assertIsNone(maintenance.retention_hold(self.store, 'r', 'p'))
        self.cycle()
        self.assertFalse((self.directory / 'generations').exists())
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        self.assertIsNotNone(self.store.setting('delivery-generation-pr:r'))
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.old,))
        self.cycle()
        for prefix in retention.LIFECYCLE:
            self.assertIsNone(self.store.one('SELECT value FROM settings WHERE key=?', (prefix + 'r',)), prefix)
        self.assertEqual(self.patch.read_text(), 'rollback survives')
        self.cycle()  # retry is idempotent

    def test_open_pending_monitoring_and_unknown_states_keep_everything(self):
        for state in ('pr_open', 'awaiting_approval', 'superseded', 'unverified_merge', 'pr_closed', 'releasing', 'deploying', 'unknown'):
            with self.subTest(state=state):
                self.store.execute("UPDATE runs SET state=? WHERE id='r'", (state,))
                self.assert_held()
        self.store.execute("UPDATE runs SET state='merged' WHERE id='r'")
        for delta in ({'monitoring_completed': False}, {'monitoring_completed': 'yes'},
                      {'close_pending': True}, {'merged_sha': None}, {'delivery_generation': 'wrong'},
                      {'feedback': {'investigation_pending': True}}):
            with self.subTest(delta=delta):
                self.store.execute("UPDATE runs SET result=? WHERE id='r'", (encode({**self.outcome, **delta}),))
                self.assert_held()

    def test_recent_and_legal_holds_keep_proof_and_files(self):
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.now - 29 * 86400,))
        self.assert_held()
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.old,))
        for key in ('legal-hold:r', 'legal-hold-project:p'):
            for raw in ('true', 'null', 'broken'):
                with self.subTest(key=key, raw=raw):
                    self.store.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, raw))
                    self.assert_held()
                    retention.retire_settings(self.store, self.now)
                    self.assertIsNotNone(self.store.setting('delivery-generation:r'))
            self.store.put_setting(key, False)
        self.assertIsNone(maintenance.retention_hold(self.store, 'r', 'p'))

    def test_uncertain_push_receipt_and_corruption_fail_closed(self):
        for key in ('delivery-generation:r', 'delivery-push:r', 'delivery-generation-pr:r',
                    'delivery:r', 'run-baseline:r', 'delivery-candidate:r'):
            original = self.store.one('SELECT value FROM settings WHERE key=?', (key,))['value']
            for raw in ('broken', 'null', '[]', '{}'):
                with self.subTest(key=key, raw=raw):
                    self.store.execute('UPDATE settings SET value=? WHERE key=?', (raw, key))
                    self.assert_held()
            self.store.execute('UPDATE settings SET value=? WHERE key=?', (original, key))
        receipt = self.store.setting('delivery-generation-pr:r')
        for changed in ({'verified_merge': None}, {'generation': 'wrong'}, {'error': 'identity failed'}):
            self.store.put_setting('delivery-generation-pr:r', {**receipt, **changed})
            self.assert_held()
        self.store.put_setting('delivery-generation-pr:r', receipt)
        for delta in ({'state': 'OPEN'}, {'headRefOid': '0' * 40}, {'baseRefName': 'other'},
                      {'headRepository': {'nameWithOwner': 'fork/project'}},
                      {'headRepositoryOwner': {'login': 'fork'}}, {'headRefName': 'other'},
                      {'mergeCommit': {'oid': '0' * 40}},
                      {'url': 'https://github.com/example/other/pull/1'}):
            changed = copy.deepcopy(receipt)
            changed['verified_merge']['observation'].update(delta)
            changed['observation'].update(delta)
            self.store.put_setting('delivery-generation-pr:r', changed)
            self.assert_held()
        self.store.put_setting('delivery-generation-pr:r', receipt)
        self.store.execute("UPDATE runs SET result='[]' WHERE id='r'")
        self.assert_held()

    def test_pending_publish_job_and_changed_task_keep_generation(self):
        self.store.execute("INSERT INTO tasks(id,project,source,body,state) VALUES ('approval','p','system_publish','r','planning')")
        self.assert_held()
        self.store.execute("UPDATE tasks SET state='completed' WHERE id='approval'")
        self.store.execute("UPDATE tasks SET state='queued' WHERE id='t'")
        self.assert_held()

    def orphan(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key in retention.settings(db, 'r', retention.LIFECYCLE):
                db.execute('DELETE FROM settings WHERE key=?', (key,))
        self.store.execute("UPDATE runs SET state='failed',result='{}' WHERE id='r'")
        self.store.execute("UPDATE tasks SET state='failed' WHERE id='t'")
        self.store.put_setting('delivery-generation-preparing:r:' + self.child.name,
            {'version': 1, 'directory': str(self.child), 'created': self.old})

    def test_prejournaled_orphan_retires_only_after_safe_terminal_and_age(self):
        self.orphan()
        self.store.execute("UPDATE runs SET state='awaiting_approval' WHERE id='r'")
        self.assert_held()
        self.store.execute("UPDATE runs SET state='failed' WHERE id='r'")
        self.store.put_setting('delivery-push:r', {'unknown': True})
        self.assert_held()
        self.store.execute("DELETE FROM settings WHERE key='delivery-push:r'")
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.now,))
        self.assert_held()
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.old,))
        self.cycle()
        self.assertFalse(self.child.exists())
        self.assertEqual(self.patch.read_text(), 'rollback survives')
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])

    def test_unknown_legacy_orphan_and_symlinks_never_cross_boundary(self):
        outside = Path(self.temp.name) / 'user-project'
        outside.mkdir()
        sentinel = outside / 'candidate.patch'
        sentinel.write_text('user data')
        unknown = self.directory / 'generations' / ('9' * 32)
        unknown.mkdir()
        self.cycle()
        self.assertTrue(self.child.exists())
        unknown.rmdir()
        # A link within an owned derived tree is unlinked, never followed.
        (self.child / 'outside').symlink_to(outside, target_is_directory=True)
        self.cycle()
        self.assertFalse(self.child.exists())
        self.assertEqual(sentinel.read_text(), 'user data')

    def test_generation_child_symlink_is_retained(self):
        outside = Path(self.temp.name) / 'outside'
        self.child.rename(outside)
        self.child.symlink_to(outside, target_is_directory=True)
        self.cycle()
        self.assertTrue(self.child.is_symlink())
        self.assertTrue((outside / 'candidate' / 'derived').exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))

    def test_busy_preparation_lock_blocks_cleanup_and_settings_retirement(self):
        fd = os.open(self.directory / 'generations', os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cycle()
            self.assertTrue(self.child.exists())
            self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        finally:
            os.close(fd)
        self.cycle()
        self.assertFalse(self.child.exists())

    def test_history_proof_and_nested_run_namespace(self):
        history = {**self.record, 'generation': '3' * 32,
                   'ref': 'refs/adaptive-rsi/generations/' + '3' * 32}
        history_dir = self.directory / 'generations' / ('4' * 32)
        history_dir.mkdir()
        history['directory'] = str(history_dir)
        history_key = 'delivery-generation-history:r:archive'
        self.store.put_setting(history_key, history)
        self.assert_held()  # legacy history lacks atomic unpublished evidence
        self.store.put_setting('delivery-generation-history-proof:r:archive',
                               {'unpublished': True, 'record': fingerprint(history)})
        self.store.execute("INSERT INTO runs(id,project,state,result) VALUES ('r:child','p','pr_open','{}')")
        nested = {prefix + 'r:child': {'keep': prefix} for prefix in retention.LIFECYCLE}
        nested['delivery-generation-history:r:child:old'] = {'keep': 'history'}
        for key, value in nested.items():
            self.store.put_setting(key, value)
        self.cycle()
        self.assertFalse(history_dir.exists())
        self.assertIsNone(self.store.setting(history_key))
        for key, value in nested.items():
            self.assertEqual(self.store.setting(key), value, key)

    def test_late_registration_is_seen_before_deletion(self):
        original = retention.remove_directories
        late = self.directory / 'generations' / ('8' * 32)
        def raced(batch, directory):
            late.mkdir()
            (late / 'sentinel').write_text('new evidence')
            self.store.put_setting('delivery-generation-preparing:r:' + late.name,
                {'version': 1, 'directory': str(late), 'created': self.old})
            return original(batch, directory)
        with patch.object(retention, 'remove_directories', side_effect=raced):
            self.cycle()
        self.assertTrue(self.child.exists())
        self.assertEqual((late / 'sentinel').read_text(), 'new evidence')
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))

    def test_failure_keeps_settings_and_retry_finishes(self):
        with patch.object(retention.CleanupBatch, 'remove', side_effect=PermissionError('retry')):
            self.cycle()
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertTrue(self.child.exists())
        self.cycle()
        self.assertFalse(self.child.exists())
        self.assertIsNone(self.store.setting('delivery-generation:r'))

    def test_real_run_replacement_after_original_snapshot_is_retained(self):
        original = retention.remove_directories
        moved = self.directory.with_name('original-run')
        def replace(batch, directory):
            directory.rename(moved)
            foreign = directory / 'generations' / self.child.name / 'candidate'
            foreign.mkdir(parents=True)
            (foreign / 'derived').write_text('foreign run')
            return original(batch, directory)
        with patch.object(retention, 'remove_directories', side_effect=replace):
            self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertEqual((self.child / 'candidate' / 'derived').read_text(), 'foreign run')
        self.assertEqual((moved / 'generations' / self.child.name / 'candidate' / 'derived').read_text(), 'ours')
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])

    def test_generations_moved_outside_workspace_before_first_mutation_is_retained(self):
        # All fixtures, including this simulated external destination, are below
        # TMPDIR. No pre-existing external path is accessed or removed.
        external = Path(self.temp.name) / 'external-sentinel'
        moved = False
        def rename_before_mutation():
            nonlocal moved
            if not moved:
                moved = True
                (self.directory / 'generations').rename(external)
                (external / 'outside-sentinel').write_text('external data')
        with patch.object(retention, '_cleanup_boundary', side_effect=rename_before_mutation):
            self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertTrue(moved)
        self.assertEqual((external / self.child.name / 'candidate' / 'derived').read_text(), 'ours')
        self.assertEqual((external / 'outside-sentinel').read_text(), 'external data')
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))

    def test_moved_descendant_is_rechecked_before_unlink(self):
        external = Path(self.temp.name) / 'moved-descendant'
        moved = False
        def rename_before_mutation():
            nonlocal moved
            if not moved:
                moved = True
                (self.child / 'candidate').rename(external)
        with patch.object(retention, '_cleanup_boundary', side_effect=rename_before_mutation):
            self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertEqual((external / 'derived').read_text(), 'ours')

    def test_slow_multigeneration_cleanup_releases_writer_and_stops_on_changes(self):
        # One extra thread only: it executes real cleanup. The main thread
        # writes while cleanup pauses outside BEGIN, then changes authorization.
        for change in ('hold', 'state', 'receipt', 'registration'):
            with self.subTest(change=change):
                self._paused_cleanup_change(change)

    def _paused_cleanup_change(self, change):
        history = self.directory / 'generations' / ('4' * 32)
        history.mkdir(exist_ok=True)
        for index in range(80):
            (history / ('evidence-%03d' % index)).write_text('history')
        record = {**self.record, 'directory': str(history)}
        self.store.put_setting('delivery-generation-history:r:old', record)
        self.store.put_setting('delivery-generation-history-proof:r:old',
                               {'unpublished': True, 'record': fingerprint(record)})
        # Recreate the active evidence if an earlier subcase consumed it.
        (self.child / 'candidate').mkdir(parents=True, exist_ok=True)
        (self.child / 'candidate' / 'derived').write_text('ours')
        self.store.execute("UPDATE runs SET state='merged' WHERE id='r'")
        self.store.put_setting('legal-hold:r', False)
        self.confirm()
        paused, resume = threading.Event(), threading.Event()
        result, errors = [], []
        calls = 0
        def boundary():
            nonlocal calls
            calls += 1
            # Pause after real filesystem progress, before the next mutation.
            if calls == 2:
                paused.set()
                if not resume.wait(5):
                    raise AssertionError('cleanup resume timeout')
        def cleaner():
            try:
                result.append(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(retention, '_cleanup_boundary', side_effect=boundary):
            worker = threading.Thread(target=cleaner)
            worker.start()
            try:
                self.assertTrue(paused.wait(5))
                before = {p: p.read_text() for p in history.iterdir() if p.is_file()}
                self.assertTrue(before)
                self.assertLess(len(before) + int((self.child / 'candidate' / 'derived').exists()), 81)
                with self.store.connect() as db:
                    db.execute('PRAGMA busy_timeout=250')
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("INSERT OR REPLACE INTO settings VALUES ('unrelated-writer','true')")
                self.assertTrue(worker.is_alive())
                if change == 'hold':
                    self.store.put_setting('legal-hold:r', True)
                elif change == 'state':
                    self.store.execute("UPDATE runs SET state='pr_open' WHERE id='r'")
                elif change == 'receipt':
                    receipt = self.store.setting('delivery-generation-pr:r')
                    self.store.put_setting('delivery-generation-pr:r', {**receipt, 'error': 'changed'})
                else:
                    late = self.directory / 'generations' / ('8' * 32)
                    late.mkdir(exist_ok=True)
                    (late / 'new-evidence').write_text('new')
                    self.store.put_setting('delivery-generation-preparing:r:' + late.name,
                        {'version': 1, 'directory': str(late), 'created': self.old})
            finally:
                resume.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [False])
        for path, content in before.items():
            self.assertEqual(path.read_text(), content)
        if change == 'registration':
            self.assertEqual((late / 'new-evidence').read_text(), 'new')
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        # Restore only database authorization for the next subcase.
        self.store.put_setting('legal-hold:r', False)
        self.store.execute("UPDATE runs SET state='merged' WHERE id='r'")
        self.confirm()

    def test_bounded_batch_is_reentrant_and_preserves_evidence_until_finished(self):
        for index in range(150):
            (self.child / ('file-%03d' % index)).write_text('derived')
        self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertTrue(self.child.exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        for _ in range(5):
            if maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory):
                break
        else:
            self.fail('bounded retries did not finish')
        self.assertFalse(self.child.exists())
        self.assertEqual(self.patch.read_text(), 'rollback survives')

    def test_generation_move_after_partial_cleanup_preserves_remaining_files(self):
        for index in range(4):
            (self.child / ('extra-%d' % index)).write_text('remaining evidence')
        external = Path(self.temp.name) / 'external-after-progress'
        calls = 0
        remaining = {}
        def move_after_progress():
            nonlocal calls
            calls += 1
            if calls == 2:
                root = self.directory / 'generations'
                root.rename(external)
                remaining.update({p: p.read_text() for p in external.rglob('*') if p.is_file()})
        with patch.object(retention, '_cleanup_boundary', side_effect=move_after_progress):
            self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertTrue(remaining)
        self.assertEqual(len(remaining), 4)  # one of five files really retired
        for path, content in remaining.items():
            self.assertEqual(path.read_text(), content)

    def test_many_preparing_journals_progress_across_validation_pages(self):
        for index in range(257):
            path = self.directory / 'generations' / ('%032x' % index)
            self.store.put_setting('delivery-generation-preparing:r:' + path.name,
                {'version': 1, 'directory': str(path), 'created': self.old})
        self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertEqual((self.child / 'candidate' / 'derived').read_text(), 'ours')
        for _ in range(180):
            self.cycle()
            if not self.store.setting('delivery-generation:r'):
                break
        else:
            self.fail('valid preparation journals never retired')
        self.assertFalse(self.child.exists())
        self.assertFalse(self.store.query("SELECT key FROM settings WHERE key LIKE 'delivery-generation%:r:%'"))

    def add_history(self, count):
        # Distinct, fully evidenced unpublished generations, not dummy journals.
        for index in range(count):
            generation = '%032x' % (index + 100)
            directory = self.directory / 'generations' / generation
            directory.mkdir(parents=True)
            (directory / 'evidence').write_text('history-' + generation)
            record = {**self.record, 'generation': generation,
                      'ref': 'refs/adaptive-rsi/generations/' + generation, 'directory': str(directory)}
            self.store.put_setting('delivery-generation-history:r:' + generation, record)
            self.store.put_setting('delivery-generation-history-proof:r:' + generation,
                                   {'unpublished': True, 'record': fingerprint(record)})

    def test_more_than_256_evidenced_history_records_eventually_retire(self):
        self.add_history(257)
        self.assertEqual(len(self.store.query("SELECT key FROM settings WHERE key LIKE 'delivery-generation-history:r:%'")), 257)
        for _ in range(420):
            self.cycle()
            if not self.store.setting('delivery-generation:r'):
                break
        else:
            self.fail('valid history never retired')
        self.assertFalse((self.directory / 'generations').exists())
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertFalse(self.store.query("SELECT key FROM settings WHERE key LIKE 'delivery-generation%:r:%'"))
        self.assertEqual(self.patch.read_text(), 'rollback survives')

    def test_large_history_damaged_unconfirmed_and_hold_never_delete(self):
        self.add_history(257)
        key = 'delivery-generation-history-proof:r:' + ('%032x' % 356)
        original = self.store.setting(key)
        for mode in ('missing-proof', 'corrupt-proof', 'legal-hold'):
            with self.subTest(mode=mode):
                self.store.put_setting(key, original)
                self.store.put_setting('legal-hold:r', mode == 'legal-hold')
                if mode == 'missing-proof':
                    self.store.execute('DELETE FROM settings WHERE key=?', (key,))
                elif mode == 'corrupt-proof':
                    self.store.put_setting(key, {'unpublished': True, 'record': 'f' * 64})
                for _ in range(22):
                    self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
                self.assertEqual(len(list((self.directory / 'generations').iterdir())), 258)
                self.assertTrue(self.child.exists())
                self.assertIsNotNone(self.store.setting('delivery-generation:r'))

    def test_registration_between_large_validation_pages_invalidates_progress(self):
        self.add_history(257)
        for _ in range(4):
            self.assertIsNotNone(maintenance.retention_hold(self.store, 'r', 'p'))
        late = self.directory / 'generations' / ('9' * 32)
        late.mkdir()
        (late / 'sentinel').write_text('late preparation')
        self.store.put_setting('delivery-generation-preparing:r:' + late.name,
            {'version': 1, 'directory': str(late), 'created': time.time()})
        for _ in range(22):
            self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertEqual((late / 'sentinel').read_text(), 'late preparation')
        self.assertTrue(self.child.exists())
        self.assertEqual(len(list((self.directory / 'generations').iterdir())), 259)

    def test_progress_survives_interleaving_more_than_eight_runs(self):
        for index in range(9):
            run_id = 'paged-%02d' % index
            self.store.execute("INSERT INTO tasks(id,project,state,created,updated) VALUES (?,'p','failed',1,1)", (run_id,))
            self.store.execute("INSERT INTO runs(id,task,project,state,config,ended,result) "
                               "VALUES (?,?,'p','failed',?,?,'{}')", (run_id, run_id, encode(DEFAULTS), self.old))
            for generation in range(34):
                path = self.store.root / 'runs' / run_id / 'generations' / ('%032x' % generation)
                self.store.put_setting('delivery-generation-preparing:' + run_id + ':' + path.name,
                    {'version': 1, 'directory': str(path), 'created': self.old})
        for index in range(9):
            self.assertIsNone(retention.authorization(self.store, 'paged-%02d' % index, 'p'))
        for index in range(9):
            self.assertIsNotNone(retention.authorization(self.store, 'paged-%02d' % index, 'p'))

    def test_single_record_over_supported_bound_is_not_loaded_or_deleted(self):
        record = copy.deepcopy(self.record)
        record['evaluation']['checks'] = {'stdout': 'x' * (128 * 1024)}
        record['evidence_hash'] = fingerprint(record['evaluation'])
        self.store.put_setting('delivery-generation:r', record)
        with patch.object(retention, 'MAX_RECORD_BYTES', 64 * 1024), \
                patch.object(retention, 'object_value', wraps=retention.object_value) as parse:
            self.assertIsNotNone(maintenance.retention_hold(self.store, 'r', 'p'))
            self.assertTrue(all(len(call.args[0]) <= 64 * 1024 for call in parse.call_args_list))
        self.assertTrue(self.child.exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))

    def paused_validation(self, entry):
        paused, resume = threading.Event(), threading.Event()
        errors = []
        def boundary():
            paused.set()
            if not resume.wait(5):
                raise AssertionError('validation resume timeout')
        def worker():
            try:
                entry()
            except BaseException as exc:
                errors.append(exc)
        return paused, resume, errors, boundary, worker

    def test_slow_validation_all_entry_points_release_sqlite_writer(self):
        for entry in ('hold', 'maintenance', 'retire'):
            for change in ('hold', 'registration'):
                with self.subTest(entry=entry, change=change):
                    self.store.put_setting('legal-hold:r', False)
                    if entry == 'retire':
                        self.store.execute("UPDATE runs SET path=NULL WHERE id='r'")
                        call = lambda: retention.retire_settings(self.store, time.time())
                    else:
                        self.store.execute("UPDATE runs SET path=? WHERE id='r'", (str(self.directory),))
                        call = self.cycle if entry == 'maintenance' else lambda: maintenance.retention_hold(self.store, 'r', 'p')
                    paused, resume, errors, boundary, work = self.paused_validation(call)
                    with patch.object(retention, '_validation_boundary', side_effect=boundary):
                        thread = threading.Thread(target=work)
                        thread.start()
                        try:
                            self.assertTrue(paused.wait(5))
                            with self.store.connect() as db:
                                db.execute('PRAGMA busy_timeout=250')
                                db.execute('BEGIN IMMEDIATE')
                                db.execute("INSERT OR REPLACE INTO settings VALUES ('unrelated-writer','true')")
                            if change == 'hold':
                                self.store.put_setting('legal-hold:r', True)
                            else:
                                late = self.directory / 'generations' / ('8' * 32)
                                late.mkdir(exist_ok=True)
                                (late / 'sentinel').write_text('new registration')
                                self.store.put_setting('delivery-generation-preparing:r:' + late.name,
                                    {'version': 1, 'directory': str(late), 'created': time.time()})
                        finally:
                            resume.set()
                            thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertTrue(self.child.exists())
                    self.assertIsNotNone(self.store.setting('delivery-generation:r'))
                    # The newly inserted preparation is recent and must remain
                    # a hold even once the previous proof is invalidated.
                    self.assertIsNotNone(maintenance.retention_hold(self.store, 'r', 'p'))
                    if change == 'registration':
                        self.store.execute('DELETE FROM settings WHERE key=?', ('delivery-generation-preparing:r:' + late.name,))
                        (late / 'sentinel').unlink()
                        late.rmdir()

    def test_real_maintenance_pins_run_before_cleanup_entry_and_closes_fds(self):
        original = maintenance._remove_run_artifacts
        moved = self.directory.with_name('original-run')
        snapshots = []
        closed = []
        close = retention.DirectorySnapshot.close
        def checked_close(snapshot):
            descriptors = list(snapshot.fds)
            close(snapshot)
            for fd in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            closed.extend(descriptors)
        def replace(store, run_id, project, directory, stored=None, snapshot=None):
            self.assertIsNotNone(snapshot)
            snapshots.append(snapshot)
            # We are outside dispatch_lock and before the real delete entry.
            available = []
            def lock_probe():
                acquired = self.rt.dispatch_lock.acquire(blocking=False)
                available.append(acquired)
                if acquired:
                    self.rt.dispatch_lock.release()
            probe = threading.Thread(target=lock_probe)
            probe.start()
            probe.join(1)
            self.assertEqual(available, [True])
            directory.rename(moved)
            foreign = directory / 'generations' / self.child.name / 'candidate'
            foreign.mkdir(parents=True)
            (foreign / 'derived').write_text('foreign sentinel')
            return original(store, run_id, project, directory, stored=stored, snapshot=snapshot)
        with patch.object(maintenance, '_remove_run_artifacts', side_effect=replace), \
                patch.object(retention.DirectorySnapshot, 'close', checked_close):
            self.cycle()
        self.assertEqual((self.child / 'candidate' / 'derived').read_text(), 'foreign sentinel')
        self.assertEqual((moved / 'generations' / self.child.name / 'candidate' / 'derived').read_text(), 'ours')
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].fds, [])
        self.assertEqual(len(closed), 4)

    def test_maintenance_exception_and_completed_batch_close_snapshots(self):
        close = retention.DirectorySnapshot.close
        snapshots = []
        def checked_close(snapshot):
            fds = list(snapshot.fds)
            close(snapshot)
            for fd in fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            snapshots.append(snapshot)
        with patch.object(retention.DirectorySnapshot, 'close', checked_close), \
                patch.object(maintenance, '_remove_run_artifacts', side_effect=OSError('simulated deletion failure')):
            self.cycle()
        self.assertEqual(len(snapshots), 1)
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        with patch.object(retention.DirectorySnapshot, 'close', checked_close):
            self.cycle()
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(not snapshot.fds for snapshot in snapshots))
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])

    def test_unexpected_artifact_types_keep_files_and_lookup_path(self):
        workspace = self.directory / 'workspace'
        workspace.write_text('unexpected file must survive')
        (self.directory / 'plan.json').mkdir()
        self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertEqual(workspace.read_text(), 'unexpected file must survive')
        self.assertTrue((self.directory / 'plan.json').is_dir())
        self.assertIsNotNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])

    def test_generation_root_and_runs_root_symlinks_are_not_followed(self):
        actual = self.directory / 'generations'
        outside = Path(self.temp.name) / 'outside-generations'
        actual.rename(outside)
        actual.symlink_to(outside, target_is_directory=True)
        self.cycle()
        self.assertTrue((outside / self.child.name / 'candidate' / 'derived').exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        actual.unlink()
        outside.rename(actual)
        root = self.store.root / 'runs'
        outside_runs = Path(self.temp.name) / 'outside-runs'
        root.rename(outside_runs)
        root.symlink_to(outside_runs, target_is_directory=True)
        self.assertFalse(maintenance._remove_run_artifacts(self.store, 'r', 'p', self.directory))
        self.assertTrue((outside_runs / 'r' / 'generations' / self.child.name / 'candidate' / 'derived').exists())
        root.unlink()
        outside_runs.rename(root)

    def test_receipt_appearing_after_precheck_prevents_orphan_deletion(self):
        self.orphan()
        original = maintenance.retention_hold
        def late_receipt(store, run_id, project):
            self.assertIsNone(original(store, run_id, project))
            store.put_setting('delivery-generation-pr:r', {'unconfirmed': True})
            return None
        with patch.object(maintenance, 'retention_hold', side_effect=late_receipt):
            self.cycle()
        self.assertTrue(self.child.exists())
        self.assertIsNotNone(self.store.setting('delivery-generation-pr:r'))

    def test_automation_and_damaged_history_remain_held(self):
        self.store.put_setting('automation:r:release', {'state': 'dispatch_unknown'})
        self.assert_held()
        self.store.execute("DELETE FROM settings WHERE key='automation:r:release'")
        self.store.execute("INSERT INTO settings VALUES ('delivery-generation-history:r:broken','null')")
        self.assert_held()

    def test_safe_cleanup_rejects_suspicious_record_directory(self):
        self.record['directory'] = str(Path(self.temp.name) / 'user-project')
        self.store.put_setting('delivery-generation:r', self.record)
        self.confirm()  # even a merge proof cannot grant filesystem ownership
        self.cycle()
        self.assertTrue(self.child.exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))


    def test_runs_root_swap_between_generation_and_other_artifacts_cannot_escape(self):
        workspace = self.directory / 'workspace'
        workspace.mkdir()
        (workspace / 'ours').write_text('derived')
        outside = Path(self.temp.name) / 'user-runs'
        (outside / 'r' / 'workspace').mkdir(parents=True)
        sentinel = outside / 'r' / 'workspace' / 'user-file'
        sentinel.write_text('must survive')
        runs = self.store.root / 'runs'
        moved = self.store.root / 'moved-runs'
        original = retention.remove_directories
        def swap(*args):
            result = original(*args)
            runs.rename(moved)
            runs.symlink_to(outside, target_is_directory=True)
            return result
        try:
            with patch.object(retention, 'remove_directories', side_effect=swap):
                self.cycle()
            self.assertEqual(sentinel.read_text(), 'must survive')
            self.assertTrue((moved / 'r' / 'workspace' / 'ours').exists())
            self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        finally:
            runs.unlink()
            moved.rename(runs)
        self.cycle()
        self.assertFalse(workspace.exists())
        self.assertEqual(sentinel.read_text(), 'must survive')

    def test_missing_run_directory_can_finish_after_prior_interruption(self):
        import shutil
        shutil.rmtree(self.directory)
        self.cycle()
        self.assertIsNone(self.store.one("SELECT path FROM runs WHERE id='r'")['path'])
        self.assertIsNone(self.store.setting('delivery-generation:r'))


    def test_unpublished_history_only_terminal_retires_with_its_proof(self):
        self.orphan()
        self.store.put_setting('delivery-generation-history:r:archive', self.record)
        self.store.put_setting('delivery-generation-history-proof:r:archive',
                               {'unpublished': True, 'record': fingerprint(self.record)})
        self.cycle()
        self.assertFalse(self.child.exists())
        self.assertIsNone(self.store.setting('delivery-generation-history:r:archive'))
        self.assertIsNone(self.store.setting('delivery-generation-history-proof:r:archive'))

    def test_receipt_only_hold_also_preserves_old_derived_settings(self):
        self.orphan()
        self.store.put_setting('delivery-generation-pr:r', {'unconfirmed': True})
        self.store.put_setting('run-baseline:r', {'keep': 'uncertain receipt'})
        self.cycle()
        self.assertTrue(self.child.exists())
        self.assertEqual(self.store.setting('run-baseline:r'), {'keep': 'uncertain receipt'})

    def test_late_generation_registration_is_not_deleted(self):
        self.orphan()
        original = maintenance.retention_hold
        def registered(store, run_id, project):
            self.assertIsNone(original(store, run_id, project))
            store.put_setting('delivery-generation:r', self.record)
            return None
        with patch.object(maintenance, 'retention_hold', side_effect=registered):
            self.cycle()
        self.assertEqual(self.store.setting('delivery-generation:r'), self.record)
        self.assertTrue(self.child.exists())

    def test_legal_hold_after_file_retirement_preserves_metadata(self):
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.now - 31 * 86400,))
        self.cycle()
        self.assertFalse(self.child.exists())
        self.store.execute("UPDATE runs SET ended=? WHERE id='r'", (self.old,))
        self.store.put_setting('legal-hold-project:p', True)
        self.cycle()
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        self.assertIsNotNone(self.store.setting('delivery-push:r'))
        self.store.put_setting('legal-hold-project:p', False)
        self.cycle()
        self.assertIsNone(self.store.setting('delivery-generation:r'))


    def test_metadata_cursor_advances_past_unreconciled_records(self):
        for index in range(201):
            run_id = 'blocked-%03d' % index
            self.store.execute("INSERT INTO runs(id,project,state,ended,result) VALUES (?,'p','merged',?,'{}')", (run_id, self.old))
            self.store.put_setting('delivery-generation:' + run_id, {'corrupt': True})
        self.cycle()
        self.assertFalse(self.child.exists())
        self.assertIsNotNone(self.store.setting('delivery-generation:r'))
        for _ in range(30):
            self.cycle()
            if self.store.setting('delivery-generation:r') is None:
                break
        self.assertIsNone(self.store.setting('delivery-generation:r'))
        self.assertIsNotNone(self.store.setting('delivery-generation:blocked-000'))



class PublicationRetirementIntegrationTest(PublicationFixture):
    def test_tracking_records_verified_merge_then_offline_maintenance_retires(self):
        self.assertEqual(self.process_approval()['state'], 'completed')
        record = self.record()
        receipt_key = 'delivery-generation-pr:' + self.run['id']
        self.assertNotIn('verified_merge', self.store.setting(receipt_key))
        api = json.loads(self.remote.api.read_text())
        api['pr'].update(state='MERGED', mergeCommit={'oid': record['commit']})
        self.remote.api.write_text(encode(api))
        with self.remote.install(), patch.object(delivery, '_finish_merge'):
            delivery.tick(self.runtime, now=time.time() + 120, project_id=self.project_id)
        merged = self.original()
        self.assertEqual(merged['state'], 'merged', merged['result'])
        proof = self.store.setting(receipt_key)['verified_merge']
        self.assertEqual(proof['observation']['state'], 'MERGED')
        self.assertEqual(proof['observation']['headRefOid'], record['commit'])
        outcome = json.loads(merged['result'])
        outcome['monitoring_completed'] = True
        old = time.time() - 91 * 86400
        self.store.execute('UPDATE runs SET ended=?,result=? WHERE id=?',
                           (old, encode(outcome), self.run['id']))
        journals = self.store.query('SELECT key,value FROM settings WHERE key>=? AND key<?',
            ('delivery-generation-preparing:' + self.run['id'] + ':',
             'delivery-generation-preparing:' + self.run['id'] + ';'))
        self.assertEqual(len(journals), 1)
        for row in journals:
            value = json.loads(row['value'])
            value['created'] = old
            self.store.put_setting(row['key'], value)
        patch_file = self.run_dir / 'candidate.patch'
        patch_file.write_text('retained rollback patch')
        # This is an offline cleanup. Any accidental remote lookup fails here.
        with patch.object(delivery, 'pr_view', side_effect=AssertionError('network forbidden')), \
                patch.object(delivery_generation, 'git', side_effect=AssertionError('Git forbidden')):
            # The real replay tree is intentionally not reduced: bounded
            # maintenance may need several passes to retire all its entries.
            for _ in range(128):
                self.store.put_setting('maintenance-last', 0)
                self.runtime.maintenance()
                if self.store.setting('delivery-generation:' + self.run['id']) is None:
                    break
            else:
                self.fail('bounded maintenance did not finish the real replay tree')
        self.assertFalse(Path(record['directory']).exists())
        self.assertFalse((self.run_dir / 'generations').exists())
        self.assertEqual(patch_file.read_text(), 'retained rollback patch')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertIsNone(self.store.setting(receipt_key))

    def test_preparation_crash_leaves_durable_ownership_and_unlocked_directory(self):
        self.store.put_setting('delivery-candidate:' + self.run['id'], self.binding)
        def interrupted(*args):
            directory = Path(args[-1])
            directory.mkdir()
            (directory / 'partial').write_text('unfinished replay')
            raise RuntimeError('interrupted before SQLite generation registration')
        with patch.object(delivery_generation, 'create_generation', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                delivery_generation.prepare_publication(self.runtime, self.project, self.run,
                    self.approval, self.candidate, self.repo, self.current, self.config)
        journals = self.store.query('SELECT key,value FROM settings WHERE key>=? AND key<?',
            ('delivery-generation-preparing:' + self.run['id'] + ':',
             'delivery-generation-preparing:' + self.run['id'] + ';'))
        self.assertEqual(len(journals), 1)
        value = json.loads(journals[0]['value'])
        self.assertEqual((Path(value['directory']) / 'partial').read_text(), 'unfinished replay')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertIsNotNone(maintenance.retention_hold(self.store, self.run['id'], self.project_id))
        fd = os.open(self.run_dir / 'generations', os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


    def test_uncertain_receipt_blocks_preparation_before_evaluation(self):
        self.store.put_setting('delivery-candidate:' + self.run['id'], self.binding)
        key = 'delivery-generation-pr:' + self.run['id']
        receipt = {'generation': 'unknown', 'url': 'https://github.com/example/project/pull/1'}
        self.store.put_setting(key, receipt)
        with patch.object(delivery_generation, 'create_generation') as create:
            with self.assertRaisesRegex(delivery_generation.GenerationError, 'Unresolved PR receipt'):
                delivery_generation.prepare_publication(self.runtime, self.project, self.run,
                    self.approval, self.candidate, self.repo, self.current, self.config)
            create.assert_not_called()
        self.assertEqual(self.store.setting(key), receipt)
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))



class SupersededPublicationRetirementIntegrationTest(PublicationFixture):
    def test_tracking_records_verified_merge_then_offline_maintenance_retires(self):
        self.remote.lose_pr = True
        self.process_approval()
        self.store.execute("UPDATE runs SET state='superseded' WHERE id=?", (self.run['id'],))
        # Supersession cancels the old publication job; an active retry must
        # continue to hold artifacts independently of the verified receipt.
        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (self.approval,))
        record = self.record()
        receipt_key = 'delivery-generation-pr:' + self.run['id']
        self.assertIsNone(self.store.setting(receipt_key))
        api = json.loads(self.remote.api.read_text())
        api['pr'].update(state='MERGED', mergeCommit={'oid': record['commit']})
        self.remote.api.write_text(encode(api))
        with self.remote.install():
            delivery._recover_superseded_receipt(self.runtime, self.project, self.original(),
                                                 self.config, time.time() + 60)
        merged = self.original()
        self.assertEqual(merged['state'], 'merged', merged['result'])
        proof = self.store.setting(receipt_key)['verified_merge']
        self.assertEqual(proof['observation']['state'], 'MERGED')
        self.assertEqual(proof['observation']['headRefOid'], record['commit'])
        outcome = json.loads(merged['result'])
        old = time.time() - 91 * 86400
        outcome['merged_at'] = old
        self.store.execute('UPDATE runs SET ended=?,result=? WHERE id=?',
                           (old, encode(outcome), self.run['id']))
        # Exercise the real tracking/feedback expiration path after aging.
        # It must finish monitoring without remote baseline reads.
        with self.remote.install():
            delivery.tick(self.runtime, now=time.time() + 120, project_id=self.project_id)
        self.assertTrue(json.loads(self.original()['result'])['monitoring_completed'])
        journals = self.store.query('SELECT key,value FROM settings WHERE key>=? AND key<?',
            ('delivery-generation-preparing:' + self.run['id'] + ':',
             'delivery-generation-preparing:' + self.run['id'] + ';'))
        self.assertEqual(len(journals), 1)
        for row in journals:
            value = json.loads(row['value'])
            value['created'] = old
            self.store.put_setting(row['key'], value)
        patch_file = self.run_dir / 'candidate.patch'
        patch_file.write_text('retained rollback patch')
        # This is an offline cleanup. Any accidental remote lookup fails here.
        with patch.object(delivery, 'pr_view', side_effect=AssertionError('network forbidden')), \
                patch.object(delivery_generation, 'git', side_effect=AssertionError('Git forbidden')):
            # The real replay tree is intentionally not reduced: bounded
            # maintenance may need several passes to retire all its entries.
            for _ in range(128):
                self.store.put_setting('maintenance-last', 0)
                self.runtime.maintenance()
                if self.store.setting('delivery-generation:' + self.run['id']) is None:
                    break
            else:
                self.fail('bounded maintenance did not finish the real replay tree')
        self.assertFalse(Path(record['directory']).exists())
        self.assertFalse((self.run_dir / 'generations').exists())
        self.assertEqual(patch_file.read_text(), 'retained rollback patch')
        self.assertIsNone(self.store.setting('delivery-generation:' + self.run['id']))
        self.assertIsNone(self.store.setting(receipt_key))




if __name__ == '__main__':
    unittest.main()


class LargeEvaluationRetirementIntegrationTest(PublicationFixture):
    def setUp(self):
        from tests import test_delivery_replay_checks as replay_tests
        from adaptive_rsi.gitops import git
        original = replay_tests.fixture
        def noisy_fixture(root):
            repo = original(root)
            path = repo / 'tests/test_calculator.py'
            path.write_text("import sys\nsys.stdout.write('x' * (1024 * 1024 + 64) + '\\n')\n" + path.read_text())
            git(repo, 'add', '.')
            git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                'commit', '--quiet', '-m', 'large legitimate check output')
            return repo
        with patch.object(replay_tests, 'fixture', side_effect=noisy_fixture):
            super().setUp()

    def process_approval(self):
        result = super().process_approval()
        record = self.record()
        self.assertGreater(len(encode(record['evaluation']).encode()), 1024 * 1024)
        self.assertEqual(record['evidence_hash'], fingerprint(record['evaluation']))
        self.assertEqual(record['evaluation']['decision'], 'pass')
        return result

    def test_real_large_evaluation_retires_after_verified_merge_and_monitoring(self):
        PublicationRetirementIntegrationTest.test_tracking_records_verified_merge_then_offline_maintenance_retires(self)
