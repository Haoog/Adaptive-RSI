"""Low resource observations delay claims and recover without failed attempts."""
from concurrent.futures import Future
import io
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from adaptive_rsi.config import validate
from adaptive_rsi.resource_pressure import MIB, PressureMonitor, available_memory
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.store import Store


class ResourcePressureTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.runtime = Runtime(self.store, agent=object())
        self.addCleanup(self.runtime.close)
        self.store.execute("INSERT INTO projects VALUES ('p','project',?,'{}',0,?)", (temporary.name, time.time()))
        self.runtime.configure({'limits': {'min_free_disk_mb': 256}}, 'p')
        self.task = self.store.add_task('p', 'goal', 'resource-pressure', 'Queued task')

    def test_disk_pressure_does_not_claim_fail_or_retry_and_release_recovers(self):
        eligible = [(self.runtime.project('p'), self.runtime.config('p'))]
        pending = Future()
        with patch.object(self.runtime.pool, 'submit', return_value=pending) as submit:
            with patch('adaptive_rsi.resource_pressure.shutil.disk_usage', return_value=types.SimpleNamespace(free=1)):
                self.runtime.dispatch(eligible)
                self.runtime.dispatch(eligible)
            submit.assert_not_called()
            task = self.store.one('SELECT state,attempts FROM tasks WHERE id=?', (self.task,))
            self.assertEqual(task, {'state': 'queued', 'attempts': 0})
            self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 0)
            self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='resource_pressure'")['n'], 1)
            with patch('adaptive_rsi.resource_pressure.shutil.disk_usage', return_value=types.SimpleNamespace(free=1024 * MIB)):
                self.runtime.dispatch(eligible)
            submit.assert_called_once()
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM runs')['n'], 1)
        self.assertTrue(self.runtime.resource_pressure.projects['p']['admitted'])
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM events WHERE kind='resource_pressure'")['n'], 2)

    def test_blocked_project_does_not_prevent_another_ready_project(self):
        self.store.execute("INSERT INTO projects VALUES ('other','other',?,'{}',0,?)", (str(self.store.root), time.time()))
        self.store.add_task('other', 'goal', 'other', 'Other disk')
        first = self.runtime.project('p')
        def disk(path):
            return types.SimpleNamespace(free=1 if str(path) == first['path'] else 1024 * MIB)
        with patch('adaptive_rsi.resource_pressure.shutil.disk_usage', side_effect=disk), patch.object(
                self.runtime.pool, 'submit', return_value=Future()):
            self.runtime.dispatch([(first, self.runtime.config('p')),
                                   (self.runtime.project('other'), self.runtime.config('other'))])
        self.assertEqual(self.store.one('SELECT project FROM runs')['project'], 'other')
        self.assertEqual(self.store.one('SELECT state FROM tasks WHERE id=?', (self.task,))['state'], 'queued')

    def test_memory_cache_rechecks_and_explicit_unknown_fails_closed(self):
        monitor = PressureMonitor()
        config = validate({'limits': {'min_free_disk_mb': 0, 'min_available_memory_mb': 128}})
        project = self.runtime.project('p')
        with patch('adaptive_rsi.resource_pressure.time.monotonic', side_effect=[0, 1, 6]), patch(
                'adaptive_rsi.resource_pressure.available_memory', side_effect=[(1, 'controlled'), (256 * MIB, 'controlled')]) as probe:
            self.assertFalse(monitor.check(self.store.root, project, config)['admitted'])
            self.assertFalse(monitor.check(self.store.root, project, config)['admitted'])
            self.assertTrue(monitor.check(self.store.root, project, config)['admitted'])
        self.assertEqual(probe.call_count, 2)
        monitor.memory_at = None
        with patch('adaptive_rsi.resource_pressure.available_memory', side_effect=OSError('unavailable')):
            report = monitor.check(self.store.root, project, config)
        self.assertEqual(report['reasons'], ['memory_unknown'])
        self.assertIsNone(report['memory']['available_bytes'])

    def test_disabled_probes_do_not_read_host_and_unknown_disk_blocks(self):
        config = validate({'limits': {'min_free_disk_mb': 0, 'min_available_memory_mb': 0}})
        with patch('adaptive_rsi.resource_pressure.shutil.disk_usage') as disk, patch('adaptive_rsi.resource_pressure.available_memory') as memory:
            self.assertTrue(PressureMonitor().check(self.store.root, self.runtime.project('p'), config)['admitted'])
            disk.assert_not_called()
            memory.assert_not_called()
        with patch('adaptive_rsi.resource_pressure.shutil.disk_usage', side_effect=OSError('no stat')):
            report = PressureMonitor().check(self.store.root, self.runtime.project('p'), validate({'limits': {'min_free_disk_mb': 256}}))
        self.assertEqual(report['reasons'], ['disk_state_unknown', 'disk_project_unknown'])

    def test_macos_measurement_declares_estimate_and_linux_caps_visible_cgroup(self):
        output = 'Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 10.\nPages inactive: 20.\nPages speculative: 3.\n'
        with patch('adaptive_rsi.resource_pressure.sys.platform', 'darwin'), patch(
                'adaptive_rsi.resource_pressure.subprocess.check_output', return_value=output):
            free, basis = available_memory()
        self.assertEqual(free, 33 * 16384)
        self.assertEqual(basis, 'macos_reclaimable_estimate')
        def source(path, *args, **kwargs):
            return io.StringIO('MemAvailable: 1048576 kB\n' if str(path) == '/proc/meminfo' else '0::/group\n')
        def contents(path, *args, **kwargs):
            return '1024' if str(path).endswith('memory.max') else '900'
        with patch('adaptive_rsi.resource_pressure.sys.platform', 'linux'), patch.object(Path, 'open', source), patch.object(Path, 'read_text', contents):
            free, basis = available_memory()
        self.assertEqual(free, 124)
        self.assertIn('cgroup', basis)

    def test_thresholds_reject_invalid_ranges_and_booleans(self):
        for threshold in (-1, 1048577, True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                validate({'limits': {'min_free_disk_mb': threshold}})
