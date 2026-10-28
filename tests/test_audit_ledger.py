"""Completion claims require existing replay references and derived counts."""
import copy
from collections import Counter
import json
import unittest

from scripts.check_audit_ledger import LEDGER, ROOT, check, outputs


class AuditLedgerTest(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / LEDGER).read_text())

    def test_completed_rows_have_replayable_existing_references(self):
        self.assertEqual(check(self.data), [])
        for field, value in [('implementation', []), ('implementation', ['../outside.py']),
                             ('validation', ['python3 -m unittest tests.nonexistent_audit_case'])]:
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.data)
                data['items'][0][field] = value
                self.assertTrue(check(data))

    def test_counts_and_numbered_statuses_are_derived_in_both_languages(self):
        data = copy.deepcopy(self.data)
        data['items'][0]['status'] = 'pending'
        counts = Counter(item['status'] for item in data['items'])
        generated = outputs(data)
        chinese = generated['docs/validation/audit-200-progress-20261028.md']
        english = generated['docs/validation/audit-200-progress-20261028.en.md']
        self.assertIn('{} 项部分完成，{} 项尚待实施'.format(counts['partial'], counts['pending']), chinese)
        self.assertIn('{} partial, and {} pending'.format(counts['partial'], counts['pending']), english)
        self.assertIn('1. **pending**', chinese)
        self.assertIn('1. **pending**', english)

    def test_incomplete_scope_cannot_be_marked_release_ready(self):
        data = copy.deepcopy(self.data)
        data.update(release_ready=True, implementation_commit='a' * 40, ci_evidence=['example'])
        self.assertTrue(any('Release readiness' in error for error in check(data)))
        data['items'][0]['validation'] = ['node tests/web_requests.cjs && touch ignored-marker']
        self.assertTrue(any('one replay command' in error for error in check(data)))

    def test_invalid_identity_and_missing_bilingual_titles_are_rejected(self):
        for mutation in ('duplicate', 'nonobject', 'title'):
            data = copy.deepcopy(self.data)
            if mutation == 'duplicate':
                data['items'][0]['id'] = data['items'][1]['id']
            elif mutation == 'nonobject':
                data['items'][0] = 'invalid'
            else:
                del data['items'][0]['title']['en']
            with self.subTest(mutation=mutation):
                self.assertTrue(check(data))
