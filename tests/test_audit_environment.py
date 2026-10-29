import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from adaptive_rsi import environment
from adaptive_rsi.config import DEFAULTS, merge
from adaptive_rsi.gitops import git

# Try an exclusive non-blocking flock on an entry directory from another process.
LOCK_PROBE = """
import errno, fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print('acquired')
except OSError as error:
    print(errno.errorcode.get(error.errno, str(error.errno)))
finally:
    os.close(fd)
"""


def exclusive_lock(path):
    result = subprocess.run([sys.executable, '-c', LOCK_PROBE, str(path)],
                            capture_output=True, text=True, timeout=120)
    return result.stdout.strip()


class AuditEnvironmentTest(unittest.TestCase):
    def repository(self, root, value='original'):
        repo = root / 'source'
        repo.mkdir()
        git(repo, 'init', '-q')
        (repo / 'requirements.txt').write_text('a==1\n')
        (repo / 'deps').mkdir()
        (repo / 'deps/value').write_text(value)
        return repo

    def environment_config(self, **overrides):
        return merge(DEFAULTS, {'environment': {'seed_paths': ['deps'], **overrides}})

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

    def test_cache_hit_reuses_entry_without_recapturing_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            config = self.environment_config()
            cache = root / 'cache'
            with patch.object(environment, 'capture', wraps=environment.capture) as frozen:
                first = environment.prepare_cached(repo, cache, config, {'python': '3.14'})
                self.assertFalse(first['reused'])
                self.assertEqual(frozen.call_count, 1)
                entries = sorted(path.name for path in cache.iterdir())
                second = environment.prepare_cached(repo, cache, config, {'python': '3.14'})
                self.assertTrue(second['reused'])
                # 命中只做一次只读摘要，不再冻结整份依赖，也不留临时副本。
                self.assertEqual(frozen.call_count, 1)
                self.assertEqual(sorted(path.name for path in cache.iterdir()), entries)
                self.assertEqual(first['digest'], second['digest'])
                self.assertEqual(first['path'], second['path'])

    def test_seed_scan_matches_frozen_digest_including_modes_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            (repo / 'deps/nested').mkdir()
            (repo / 'deps/nested/deep').write_text('deep\n')
            (repo / 'deps/nested').chmod(0o750)
            (repo / 'deps/script').write_text('#!/bin/sh\n')
            (repo / 'deps/script').chmod(0o640)
            (repo / 'deps/empty').mkdir()
            (repo / 'deps/link').symlink_to('script')
            config = self.environment_config()
            scan = environment.dependencies(repo, config)
            frozen = environment.capture(repo, root / 'frozen', config)
            # 命中前的只读摘要必须与被冻结副本的完整身份逐字段一致。
            self.assertEqual(scan, frozen)
            prepared = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertEqual(prepared['inputs']['seed'], scan)
            again = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertTrue(again['reused'])
            self.assertEqual(again['key'], prepared['key'])
            environment.materialize(again, root / 'run-dependencies')
            link = root / 'run-dependencies/deps/link'
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), 'script')
            self.assertEqual(link.read_text(), '#!/bin/sh\n')

    def test_seed_permission_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            config = self.environment_config()
            first = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            (repo / 'deps/value').chmod(0o600)
            changed = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertFalse(changed['reused'])
            self.assertNotEqual(first['key'], changed['key'])
            (repo / 'deps/value').chmod(0o644)
            back = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertTrue(back['reused'])
            self.assertEqual(first['key'], back['key'])

    def test_source_change_between_scan_and_capture_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            config = self.environment_config()
            scanned = environment.dependencies
            stale = {}
            def racing(source, value):
                stale.update(scanned(source, value))
                # 摘要基于旧内容，但在冻结前源已经变化。
                (repo / 'deps/value').write_text('changed')
                return dict(stale)
            with patch.object(environment, 'dependencies', racing):
                result = environment.prepare_cached(repo, root / 'cache', config, {'python': '3.14'})
            self.assertFalse(result['reused'])
            # 条目必须按冻结后的字节建键，不能沿用扫描时的旧摘要。
            self.assertNotEqual(result['inputs']['seed'], stale)
            self.assertEqual(result['inputs']['seed'], environment.dependencies(repo, config))
            self.assertEqual(sorted(path.name for path in (root / 'cache').iterdir()), [result['key']])
            environment.materialize(result, root / 'run-dependencies')
            self.assertEqual((root / 'run-dependencies/deps/value').read_text(), 'changed')

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
            # 拒绝发生在写入运行自有副本之前，损坏的缓存字节不会进入工作区。
            self.assertFalse((root / 'polluted/dependencies').exists())

            cached_file.write_text('original')
            cached_file.chmod(0o444)
            repaired = root / 'repaired'
            repaired.mkdir()
            _, repaired_seeds = pipeline._prepare_environment(
                runtime, run, project, config, repaired, repaired / 'workspace', config['checks'])
            cached_file.chmod(0o600)
            # 仅权限变化也必须拒绝：摘要覆盖 mode，不接受按 mtime 或大小放行。
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                pipeline._prepare_environment(
                    runtime, run, project, config, root / 'polluted-mode', root / 'polluted-mode/workspace', config['checks'])

            cached_file.chmod(0o444)
            (store.root / 'prepared-environments' / first['cache']['key']).rename(store.root / 'evicted')
            # 缓存被淘汰后，运行自有副本与工作区副本仍然完整；工作区可写，运行副本只读。
            self.assertEqual(Path(repaired_seeds, 'deps/value').read_text(), 'original')
            self.assertEqual((repaired / 'workspace/deps/value').read_text(), 'original')
            self.assertFalse(os.access(Path(repaired_seeds, 'deps/value'), os.W_OK))
            self.assertTrue(os.access(repaired / 'workspace/deps/value', os.W_OK))
            (repaired / 'workspace/deps/value').write_text('workspace only')
            self.assertEqual(Path(repaired_seeds, 'deps/value').read_text(), 'original')

    def test_prepared_cache_pruning_keeps_recent_entries_and_skips_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index in range(3):
                entry = root / ('key' + str(index))
                (entry / 'seeds').mkdir(parents=True)
                (entry / 'manifest.json').write_text('{}')
                if index < 2:
                    os.utime(entry, (1000 + index, 1000 + index))
            (root / 'sealed-inflight').mkdir()
            with patch.object(environment, 'CACHE_ENTRY_LIMIT', 1), \
                    patch.object(environment, 'CACHE_BYTE_LIMIT', 1 << 30), \
                    patch.object(environment, 'CACHE_IDLE_SECONDS', 0):
                removed = environment.prune(root)
            self.assertEqual(removed, ['key1', 'key0'])
            self.assertEqual(sorted(path.name for path in root.iterdir()), ['key2', 'sealed-inflight'])
            fresh = root / 'key3'
            (fresh / 'seeds').mkdir(parents=True)
            (fresh / 'manifest.json').write_text('{}')
            with patch.object(environment, 'CACHE_ENTRY_LIMIT', 0), \
                    patch.object(environment, 'CACHE_BYTE_LIMIT', 0), \
                    patch.object(environment, 'CACHE_IDLE_SECONDS', 3600):
                # 刚写入的条目在宽限期内不会被淘汰，避免与正在恢复的运行竞态。
                self.assertEqual(environment.prune(root), [])
            self.assertIn('key2', sorted(path.name for path in root.iterdir()))


    def test_cache_hit_refreshes_recent_use_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            config = self.environment_config()
            cache = root / 'cache'
            first = environment.prepare_cached(repo, cache, config, {'python': '3.14'})
            entry = Path(first['path']).parent
            os.utime(entry, (1000, 1000))
            second = environment.prepare_cached(repo, cache, config, {'python': '3.14'})
            self.assertTrue(second['reused'])
            # 命中必须刷新最近使用时间，否则每天复用的条目会先被当作 idle 淘汰。
            self.assertGreater(entry.stat().st_mtime, 1000)
            self.assertLess(abs(entry.stat().st_mtime - time.time()), 300)
            # 该时间只用于淘汰排序，不参与复用完整性判定。
            self.assertEqual(first['digest'], second['digest'])
            with patch.object(environment, 'CACHE_ENTRY_LIMIT', 0), \
                    patch.object(environment, 'CACHE_BYTE_LIMIT', 0), \
                    patch.object(environment, 'CACHE_IDLE_SECONDS', 3600):
                self.assertEqual(environment.prune(cache), [])
            self.assertTrue(entry.is_dir())

    def test_prune_skips_entry_while_materialize_copy_is_in_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root, value='payload')
            config = self.environment_config()
            cache = root / 'cache'
            prepared = environment.prepare_cached(repo, cache, config, {'python': '3.14'})
            entry, destination = Path(prepared['path']).parent, root / 'run-dependencies'
            paused, release, copied = threading.Event(), threading.Event(), {}
            real = environment.shutil.copystat

            def holding(source, target, **options):
                if threading.current_thread().name == 'materialize':
                    paused.set()
                    self.assertTrue(release.wait(120))
                return real(source, target, **options)

            def copy():
                try:
                    copied['result'] = environment.materialize(prepared, destination)
                except BaseException as error:  # noqa: BLE001 - surfaced by assertions
                    copied['error'] = error

            with patch.object(environment.shutil, 'copystat', holding):
                worker = threading.Thread(target=copy, name='materialize')
                worker.start()
                self.assertTrue(paused.wait(120))
                with patch.object(environment, 'CACHE_ENTRY_LIMIT', 0), \
                        patch.object(environment, 'CACHE_BYTE_LIMIT', 0), \
                        patch.object(environment, 'CACHE_IDLE_SECONDS', 0):
                    # 复制进行中：prune 必须跳过被共享锁持有的条目。
                    self.assertEqual(environment.prune(cache), [])
                    self.assertTrue(entry.is_dir())
                    self.assertEqual((destination / 'deps/value').read_text(), 'payload')
                # 跨进程同样看不到释放：独占锁必须不可获得。
                self.assertEqual(exclusive_lock(entry), 'EAGAIN')
                release.set()
                worker.join(120)
            self.assertFalse(worker.is_alive())
            self.assertNotIn('error', copied)
            # 整个复制在锁内完成，字节与摘要仍然正确。
            self.assertEqual(copied['result']['sha256'], prepared['inputs']['seed']['sha256'])
            self.assertEqual((destination / 'deps/value').read_text(), 'payload')
            self.assertEqual(exclusive_lock(entry), 'acquired')
            with patch.object(environment, 'CACHE_ENTRY_LIMIT', 0), \
                    patch.object(environment, 'CACHE_BYTE_LIMIT', 0), \
                    patch.object(environment, 'CACHE_IDLE_SECONDS', 0):
                # 复制结束后，超过 idle 与容量上限的条目可以被真正回收。
                self.assertEqual(environment.prune(cache), [entry.name])
            self.assertFalse(entry.exists())
            # 运行自有副本独立于缓存回收。
            self.assertEqual((destination / 'deps/value').read_text(), 'payload')

    def test_materialize_refuses_entry_that_vanished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = self.repository(root)
            prepared = environment.prepare_cached(
                repo, root / 'cache', self.environment_config(), {'python': '3.14'})
            environment._discard(Path(prepared['path']).parent)
            # 缓存条目消失必须显式失败，不能当作空环境静默成功。
            with self.assertRaises(FileNotFoundError):
                environment.materialize(prepared, root / 'run-dependencies')
            self.assertFalse((root / 'run-dependencies').exists())


if __name__ == '__main__':
    unittest.main()
