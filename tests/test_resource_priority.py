"""Real contending threads verify admission order, isolation and cancellation."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest

from adaptive_rsi.process import Cancelled
from adaptive_rsi.resources import ExecutionResources


class ResourcePriorityTest(unittest.TestCase):
    def wait_for_queue(self, resources, length):
        deadline = time.monotonic() + 2
        while resources.status()['waiting'] != length:
            if time.monotonic() >= deadline:
                self.fail('Reservations did not reach the expected queue length')
            threading.Event().wait(.002)

    def test_priority_then_fifo_orders_real_contenders(self):
        resources = ExecutionResources(capacity=1)
        order = []
        def work(name, priority):
            with resources.acquire(priority=priority):
                order.append(name)
        with ThreadPoolExecutor(max_workers=3) as pool:
            with resources.acquire():
                low = pool.submit(work, 'command', 'command')
                self.wait_for_queue(resources, 1)
                first = pool.submit(work, 'first-model', 'model')
                self.wait_for_queue(resources, 2)
                second = pool.submit(work, 'second-model', 'model')
                self.wait_for_queue(resources, 3)
                self.assertEqual(resources.status()['waiting_by_priority'], {'command': 1, 'model': 2})
            for future in (low, first, second):
                future.result(timeout=2)
        self.assertEqual(order, ['first-model', 'second-model', 'command'])
        status = resources.status()
        self.assertEqual(status['admissions'], 4)
        self.assertEqual(status['denied']['capacity'], 3)
        self.assertEqual(status['wait_samples'], 4)
        self.assertEqual(len(status['recent_wait_events']), 4)
        self.assertTrue(all(event['outcome'] == 'admitted' for event in status['recent_wait_events']))
        self.assertGreater(status['wait_p95_seconds'], 0)
        self.assertEqual(status['waiting'], 0)

    def test_aged_low_priority_precedes_new_high_priority(self):
        resources = ExecutionResources(capacity=1, starvation_seconds=.03)
        order = []
        def work(name, priority):
            with resources.acquire(priority=priority):
                order.append(name)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with resources.acquire():
                old = pool.submit(work, 'aged', 'benchmark')
                self.wait_for_queue(resources, 1)
                threading.Event().wait(.04)
                new = pool.submit(work, 'fresh', 'model')
                self.wait_for_queue(resources, 2)
            old.result(timeout=2)
            new.result(timeout=2)
        self.assertEqual(order, ['aged', 'fresh'])

    def test_aged_exclusive_blocks_new_readers_until_it_finishes(self):
        resources = ExecutionResources(capacity=2, starvation_seconds=.02)
        order = []
        def exclusive():
            with resources.acquire(exclusive=True, priority='benchmark'):
                self.assertEqual(resources.status()['active'], 0)
                self.assertTrue(resources.status()['exclusive'])
                with resources.acquire():
                    order.append('exclusive')
        def shared():
            with resources.acquire(priority='model'):
                self.assertFalse(resources.status()['exclusive'])
                order.append('shared')
        with ThreadPoolExecutor(max_workers=2) as pool:
            with resources.acquire():
                writer = pool.submit(exclusive)
                self.wait_for_queue(resources, 1)
                threading.Event().wait(.03)
                reader = pool.submit(shared)
                self.wait_for_queue(resources, 2)
                self.assertEqual(order, [])
                self.assertEqual(resources.status()['waiting_writers'], 1)
            writer.result(timeout=2)
            reader.result(timeout=2)
        self.assertEqual(order, ['exclusive', 'shared'])

    def test_cancelled_waiter_is_removed_and_never_blocks_others(self):
        resources = ExecutionResources(capacity=1)
        stop = threading.Event()
        def cancelled():
            with resources.acquire(exclusive=True, cancel=stop.is_set):
                self.fail('Cancelled reservation executed')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with resources.acquire():
                future = pool.submit(cancelled)
                self.wait_for_queue(resources, 1)
                stop.set()
                with self.assertRaises(Cancelled):
                    future.result(timeout=2)
                status = resources.status()
                self.assertEqual(status['waiting'], 0)
                self.assertEqual(status['waiting_writers'], 0)
                self.assertEqual(status['denied']['cancelled'], 1)
                self.assertEqual(status['recent_wait_events'][-1]['outcome'], 'cancelled')
            with resources.acquire():
                pass
        self.assertEqual(resources.status()['admissions'], 2)
        self.assertEqual(resources.status()['wait_samples'], 3)

    def test_pre_cancel_nested_upgrade_and_invalid_parameters(self):
        resources = ExecutionResources()
        with self.assertRaises(Cancelled), resources.acquire(cancel=lambda: True):
            self.fail('Pre-cancelled reservation executed')
        with resources.acquire():
            with self.assertRaises(RuntimeError), resources.acquire(exclusive=True):
                self.fail('Shared reservation upgraded')
            self.assertEqual(resources.status()['active'], 1)
        self.assertEqual(resources.status()['waiting'], 0)
        for arguments in ({'capacity': 0}, {'capacity': True}, {'starvation_seconds': float('nan')},
                          {'priorities': {'model': True}}, {'priorities': {'model': 101}}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ExecutionResources(**arguments)
