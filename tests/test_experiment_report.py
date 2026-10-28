"""Unknown measurements are never turned into zero-cost experimental wins."""
import unittest

from adaptive_rsi.experiment_report import comparison


def trial(case='one', source='real', size='small', repeat=0):
    return {'case': case, 'source': source, 'split': 'holdout', 'stratum': [source, size], 'repetition': repeat,
            'stable': {'pass': False, 'tokens': 100, 'seconds': 2, 'usage_known': True},
            'candidate': {'pass': True, 'tokens': 50, 'seconds': 1, 'usage_known': True}}


class ExperimentReportTest(unittest.TestCase):
    def test_known_quality_usage_and_latency_report_independent_families(self):
        report = comparison([trial(repeat=index) for index in range(5)])
        self.assertTrue(report['comparable'])
        self.assertEqual(report['token_ratio'], 0.5)
        self.assertEqual(report['latency_ratio'], 0.5)
        self.assertEqual(report['cohorts'][0]['wins'], 1)
        self.assertEqual(report['cohorts'][0]['families'], 1)
        self.assertEqual(report['unit'], 'tokens_not_currency')

    def test_missing_usage_zero_baseline_and_actual_zero_candidate_are_distinct(self):
        row = trial()
        row['candidate']['usage_known'] = False
        self.assertEqual(comparison([row])['cost_state'], 'unknown_usage')
        row['candidate']['usage_known'] = True
        row['stable']['tokens'] = 0
        self.assertEqual(comparison([row])['cost_state'], 'zero_baseline')
        row['stable']['tokens'] = 100
        row['candidate']['tokens'] = 0
        self.assertEqual(comparison([row])['token_ratio'], 0)
        self.assertEqual(comparison([row])['cost_state'], 'known')

    def test_cohorts_separate_real_synthetic_sizes_and_preserve_losses(self):
        first = trial()
        loss = trial(repeat=1)
        loss['stable']['pass'] = True
        loss['candidate']['pass'] = False
        report = comparison([first, loss, trial('two', 'synthetic', 'large')])
        self.assertEqual(len(report['cohorts']), 2)
        real = next(row for row in report['cohorts'] if row['source'] == 'real')
        self.assertEqual((real['wins'], real['losses']), (0, 1))

    def test_missing_or_invalid_data_is_not_comparable(self):
        for number in (None, -1, True, float('inf'), float('nan'), 10**1000):
            with self.subTest(number=str(number)[:20]):
                row = trial()
                row['stable']['tokens'] = number
                report = comparison([row])
                self.assertIsNone(report['token_ratio'])
                self.assertFalse(report['comparable'])
        self.assertFalse(comparison([])['comparable'])
        self.assertFalse(comparison([{}])['comparable'])
        row = trial()
        row['stable']['seconds'] = None
        self.assertIsNone(comparison([row])['latency_ratio'])
