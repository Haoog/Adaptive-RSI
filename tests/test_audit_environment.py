import tempfile
import unittest
from pathlib import Path
from adaptive_rsi import environment
from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.gitops import git


class AuditEnvironmentTest(unittest.TestCase):
    def test_cache_invalidates_tools_locks_seeds_and_restores_private_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / 'source'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'requirements.txt').write_text('a==1\n')
            (repo / 'deps').mkdir()
            (repo / 'deps/value').write_text('original')
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']}})
            first = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            second = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertEqual(first['key'], second['key'])
            self.assertTrue(second['reused'])
            one, two = root / 'one', root / 'two'
            environment.restore(first['path'], one, config)
            (one / 'deps/value').write_text('polluted')
            environment.restore(second['path'], two, config)
            self.assertEqual((two / 'deps/value').read_text(), 'original')
            changed = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.15'})
            self.assertNotEqual(first['key'], changed['key'])
            (repo / 'requirements.txt').write_text('a==2\n')
            locked = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertNotEqual(first['key'], locked['key'])
            (repo / 'deps/value').write_text('new')
            seeded = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertNotEqual(locked['key'], seeded['key'])


if __name__ == '__main__':
    unittest.main()
