"""Feature descriptors remain additive and do not expose runtime secrets."""
import unittest

from adaptive_rsi.contracts import CAPABILITIES, descriptor


class FrontendCapabilityContractTest(unittest.TestCase):
    def test_additive_capabilities_are_sorted_and_stable(self):
        result = descriptor()
        self.assertEqual(result['api_version'], 2)
        self.assertEqual(result['capabilities'], sorted(CAPABILITIES))
        for name in ('task-batch', 'operations', 'config-revisions', 'config-history', 'config-repair'):
            self.assertIn(name, result['capabilities'])
        self.assertEqual(set(result), {'api_version', 'capabilities', 'errors'})


if __name__ == '__main__':
    unittest.main()
