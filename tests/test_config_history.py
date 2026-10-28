"""Configuration undo must preserve revisions, privacy, and sparse overrides."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.config_history import decode_snapshot, page, public_events, snapshot_key, undo
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store, encode


class ConfigHistoryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',1,?)", (temporary.name, time.time()))

    def revision(self, project=None):
        return self.store.config_revision(project)

    def test_undo_restores_sparse_overrides_and_records_new_revision(self):
        self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p', source='console')
        revision = self.revision('p')
        self.assertTrue(page(self.runtime, 'p')['entries'][0]['can_undo'])
        undo(self.runtime, 'p', revision, revision)
        self.assertEqual(json.loads(self.runtime.project('p')['config']), {})
        history = page(self.runtime, 'p')['entries']
        self.assertEqual(history[0]['source'], 'undo')
        self.assertEqual(history[0]['undo_of'], revision)
        self.assertEqual(history[0]['revision'], revision + 1)
        with self.assertRaises(ValueError):
            undo(self.runtime, 'p', revision, revision)

    def test_later_project_or_global_edit_blocks_old_undo(self):
        self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        old = self.revision('p')
        self.runtime.configure({'schedule': {'light_minutes': 20}}, 'p')
        with self.assertRaises(ValueError):
            undo(self.runtime, 'p', old, old)
        current = self.revision('p')
        self.runtime.configure({'schedule': {'deep_minutes': 400}})
        with self.assertRaises(ValueError):
            undo(self.runtime, 'p', current, current)
        self.assertFalse(any(row['can_undo'] for row in page(self.runtime, 'p')['entries']))

    def test_unversioned_content_tampering_blocks_undo(self):
        self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        revision = self.revision('p')
        self.store.execute("UPDATE projects SET config='{}' WHERE id='p'")
        self.assertFalse(page(self.runtime, 'p')['entries'][0]['can_undo'])
        with self.assertRaises(ValueError):
            undo(self.runtime, 'p', revision, revision)

    def test_history_write_failure_rolls_back_configuration_and_revision(self):
        with patch('adaptive_rsi.config_history.write_snapshot', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        self.assertEqual(self.revision('p'), 0)
        self.assertEqual(json.loads(self.runtime.project('p')['config']), {})

    def test_post_commit_event_failure_keeps_discoverable_history(self):
        with patch.object(self.store, 'event', side_effect=sqlite3.OperationalError('injected')):
            with self.assertRaises(sqlite3.OperationalError):
                self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        self.assertEqual(self.revision('p'), 1)
        self.assertTrue(page(self.runtime, 'p')['entries'][0]['can_undo'])

    def test_audit_projections_never_contain_string_or_numeric_credentials(self):
        secret = 'PRIVATE-CREDENTIAL-ONLY-IN-SNAPSHOT'
        legacy = {'kind': 'config', 'data': encode({'before': {}, 'after': {
            'providers': {'fixture': {'api_key': secret}}, 'password': 123456789,
            'schedule': {'light_minutes': 19}}})}
        modern = {'kind': 'config', 'data': encode({'changes': [
            {'path': 'credentials.token', 'before': None, 'after': 123456789},
            {'path': 'models.coder', 'before': None, 'after': secret}], 'unknown': secret})}
        projected = encode(public_events([legacy, modern]))
        self.assertNotIn(secret, projected)
        self.assertNotIn('123456789', projected)
        self.assertIn('19', projected)
        self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        event = self.store.one("SELECT data FROM events WHERE kind='config'")
        self.assertNotIn('before_hash', event['data'])
        self.assertNotIn('after_hash', event['data'])

    def test_corrupt_snapshot_metadata_and_wrong_key_are_isolated(self):
        self.runtime.configure({'schedule': {'light_minutes': 19}}, 'p')
        key = snapshot_key('p', self.revision('p'))
        original = json.loads(self.store.one('SELECT value FROM settings WHERE key=?', (key,))['value'])
        for field, value in [('version', True), ('created', float('nan')), ('source', 'secret'),
                             ('after_hash', 'wrong'), ('revision', 17), ('project', 'other')]:
            with self.subTest(field=field):
                malformed = {**original, field: value}
                self.store.put_setting(key, malformed)
                self.assertFalse(page(self.runtime, 'p')['entries'][0]['can_undo'])
                with self.assertRaises(ValueError):
                    undo(self.runtime, 'p', 1, 1)
        self.assertIsNone(decode_snapshot('[]'))
        self.assertIsNone(decode_snapshot('{'))

    def test_revision_cursor_is_stable_across_new_writes(self):
        for number in range(55):
            self.runtime.configure({'schedule': {'light_minutes': 20 + number}}, 'p')
        first = page(self.runtime, 'p')
        self.assertEqual(len(first['entries']), 50)
        self.runtime.configure({'schedule': {'light_minutes': 99}}, 'p')
        second = page(self.runtime, 'p', first['next_before'])
        revisions = [row['revision'] for row in first['entries'] + second['entries']]
        self.assertEqual(revisions, list(range(55, 0, -1)))
        self.assertFalse(second['has_more'])

    def test_global_undo_restores_exact_previous_configuration(self):
        self.runtime.configure({'schedule': {'light_minutes': 19}})
        revision = self.revision()
        undo(self.runtime, None, revision, revision)
        self.assertEqual(self.store.setting('config'), {})
        self.assertGreater(self.revision('p'), 0)
