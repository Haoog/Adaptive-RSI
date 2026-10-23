import unittest

from scripts.check_docs import check


class DocumentationGateTest(unittest.TestCase):
    def test_local_links_and_bilingual_pairs(self):
        self.assertEqual(check(), [])


if __name__ == "__main__":
    unittest.main()
