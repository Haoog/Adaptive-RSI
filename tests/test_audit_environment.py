import json
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

    def test_prepared_environment_cache_runs_on_pipeline_path_and_invalidates(self):
        from types import SimpleNamespace
        from adaptive_rsi import pipeline
        from adaptive_rsi.store import Store
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / 'source'
            repo.mkdir()
            git(repo, 'init', '-q')
            (repo / 'requirements.txt').write_text('a==1\n')
            (repo / 'deps').mkdir()
            (repo / 'deps/value').write_text('original')
            config = merge(DEFAULTS, {'environment': {'seed_paths': ['deps']},
                                      'checks': [{'name': 'python', 'argv': ['python3', '--version']}]})
            store = Store(root / 'state')
            self.addCleanup(store.close)
            runtime = SimpleNamespace(store=store)
            project = {'id': 'project', 'path': str(repo)}
            run = {'id': 'run', 'project': 'project'}
            first_dir = root / 'first'
            first_dir.mkdir()
            first_workspace = first_dir / 'workspace'
            first, seeds = pipeline._prepare_environment(
                runtime, run, project, config, first_dir, first_workspace, config['checks'])
            self.assertEqual((first_workspace / 'deps/value').read_text(), 'original')
            self.assertFalse(first['cache']['reused'])
            self.assertTrue(Path(seeds).is_dir())

            second_dir = root / 'second'
            second_dir.mkdir()
            second_workspace = second_dir / 'workspace'
            second, _ = pipeline._prepare_environment(
                runtime, run, project, config, second_dir, second_workspace, config['checks'])
            self.assertTrue(second['cache']['reused'])
            self.assertEqual(first['cache']['key'], second['cache']['key'])
            self.assertEqual((second_workspace / 'deps/value').read_text(), 'original')
            (first_workspace / 'deps/value').write_text('polluted workspace')
            self.assertEqual((second_workspace / 'deps/value').read_text(), 'original')

            changed_config = merge(config, {'sandbox': {'memory_mb': 4096}})
            third_dir = root / 'third'
            third_dir.mkdir()
            third, _ = pipeline._prepare_environment(
                runtime, run, project, changed_config, third_dir, third_dir / 'workspace', changed_config['checks'])
            self.assertFalse(third['cache']['reused'])
            self.assertNotEqual(first['cache']['key'], third['cache']['key'])

            (repo / 'deps/value').write_text('changed')
            fourth_dir = root / 'fourth'
            fourth_dir.mkdir()
            fourth, _ = pipeline._prepare_environment(
                runtime, run, project, config, fourth_dir, fourth_dir / 'workspace', config['checks'])
            self.assertFalse(fourth['cache']['reused'])
            self.assertNotEqual(first['cache']['key'], fourth['cache']['key'])
            self.assertEqual((fourth_dir / 'workspace/deps/value').read_text(), 'changed')

            messages = [row['message'] for row in store.query(
                "SELECT message FROM events WHERE kind='environment' ORDER BY seq")]
            self.assertIn('Sealed dependency preparation reused', messages)
            events = [json.loads(row['data']) for row in store.query(
                "SELECT data FROM events WHERE kind='environment' ORDER BY seq")]
            self.assertTrue(any(item.get('cache', {}).get('reused') for item in events))

            (repo / 'deps/value').write_text('original')
            cached_file = store.root / 'prepared-environments' / first['cache']['key'] / 'seeds/deps/value'
            self.assertTrue(cached_file.is_file())
            cached_file.chmod(0o600)
            cached_file.write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                pipeline._prepare_environment(
                    runtime, run, project, config, root / 'polluted', root / 'polluted/workspace', config['checks'])


if __name__ == '__main__':
    unittest.main()
