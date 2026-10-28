"""Structured validation metadata never reflects arbitrary input field names."""
import unittest

from adaptive_rsi.error_details import detail
from tests import test_server_hardening


class ErrorDetailsTest(unittest.TestCase):
    def test_stable_known_reasons_and_schema_paths(self):
        self.assertEqual(detail(ValueError('Maximum concurrency is 4')),
                         {'reason_code': 'config_concurrency', 'field_path': 'limits.concurrency'})
        self.assertEqual(detail(ValueError('limits.concurrency has the wrong type')),
                         {'reason_code': 'config_field_type', 'field_path': 'limits.concurrency'})
        self.assertEqual(detail(ValueError('Invalid positive limit: min_free_disk_mb')),
                         {'reason_code': 'config_limit', 'field_path': 'limits.min_free_disk_mb'})

    def test_unknown_paths_and_secret_exception_text_are_never_reflected(self):
        for message in ['Unknown setting: PRIVATE-SECRET', 'PRIVATE-SECRET must be an object',
                        'Invalid positive limit: PRIVATE-SECRET', 'limits.concurrency has the wrong type PRIVATE-SECRET']:
            with self.subTest(message=message):
                self.assertEqual(detail(ValueError(message)), {'reason_code': 'config_invalid', 'field_path': None})

    def test_real_http_returns_stable_reason_path_and_diagnostic_header(self):
        fixture = test_server_hardening.ServerHardeningTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.store.close)
        self.addCleanup(fixture.doCleanups)
        from adaptive_rsi.config import validate
        fixture.runtime.configure = lambda values, *args, **kwargs: validate(values)
        status, headers, body = fixture.request('/api/config', 'POST', {'values': {'limits': {'concurrency': 5}}})
        import json
        result = json.loads(body)
        self.assertEqual(status, 400)
        self.assertEqual(result['reason_code'], 'config_concurrency')
        self.assertEqual(result['field_path'], 'limits.concurrency')
        self.assertRegex(headers['X-RSI-Request-ID'], '^[a-f0-9]{16}$')
        status, headers, body = fixture.request('/api/config', 'POST', {'values': {'PRIVATE-SECRET': 'PRIVATE-VALUE'}})
        self.assertNotIn(b'PRIVATE', body)
        self.assertIsNone(json.loads(body)['field_path'])
