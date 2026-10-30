"""Force directory/leaf swaps at host-tool boundaries using real filesystem links."""
import contextlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from adaptive_rsi.native import WorkspaceTools
from adaptive_rsi.process import Cancelled


@unittest.skipUnless(os.name == 'posix', 'Descriptor-relative source tools require POSIX')
class NativePathRaceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.outside = root / 'outside'
        self.outside.mkdir()
        (self.outside / 'secret.txt').write_text('outside secret')
        (self.workspace / 'folder').mkdir()
        (self.workspace / 'folder/secret.txt').write_text('workspace source')
        self.tools = WorkspaceTools(self.workspace, 'implementation', root / 'state', {'limits': {'command_seconds': 60}}, None, ())

    def test_parent_symlink_inserted_after_path_validation_cannot_escape(self):
        checked = self.tools.path('folder/secret.txt')
        (self.workspace / 'folder').rename(self.workspace / 'retained')
        (self.workspace / 'folder').symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.tools.read_source(checked, 1024)
        with self.assertRaises(OSError):
            self.tools.write_source(checked, 'overwrite')
        self.assertEqual((self.outside / 'secret.txt').read_text(), 'outside secret')

    def test_opened_leaf_descriptor_is_not_redirected_by_later_link_swap(self):
        real_open = os.open
        def swapping_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == 'secret.txt':
                leaf = self.workspace / 'folder/secret.txt'
                leaf.unlink()
                leaf.symlink_to(self.outside / 'secret.txt')
            return descriptor
        with patch('adaptive_rsi.native.os.open', side_effect=swapping_open):
            self.assertEqual(self.tools.read_source(self.tools.path('folder/secret.txt'), 1024), 'workspace source')

    def test_link_swap_at_atomic_replace_does_not_overwrite_link_target(self):
        real_replace = os.replace
        def swapping_replace(source, destination, *args, **kwargs):
            leaf = self.workspace / 'folder/secret.txt'
            leaf.unlink()
            leaf.symlink_to(self.outside / 'secret.txt')
            return real_replace(source, destination, *args, **kwargs)
        with patch('adaptive_rsi.native.os.replace', side_effect=swapping_replace):
            self.tools.write_source(self.tools.path('folder/secret.txt'), 'new source')
        self.assertEqual((self.outside / 'secret.txt').read_text(), 'outside secret')
        self.assertFalse((self.workspace / 'folder/secret.txt').is_symlink())
        self.assertEqual((self.workspace / 'folder/secret.txt').read_text(), 'new source')

    def test_chunked_slow_read_checks_cancellation_between_chunks(self):
        source = self.workspace / 'folder/secret.txt'
        source.write_bytes(b'x' * 1024 * 1024)
        real_fdopen, reads = os.fdopen, []
        class SlowStream:
            def __init__(self, stream): self.stream = stream
            def fileno(self): return self.stream.fileno()
            def read(self, size):
                reads.append(size)
                time.sleep(0.02)
                return self.stream.read(size)
        @contextlib.contextmanager
        def slow_fdopen(fd, mode):
            with real_fdopen(fd, mode) as stream:
                yield SlowStream(stream)
        self.tools.cancel = lambda: len(reads) >= 2
        began = time.monotonic()
        with patch('adaptive_rsi.native.os.fdopen', side_effect=slow_fdopen):
            with self.assertRaises(Cancelled):
                self.tools.read_source(source, 1024 * 1024)
        self.assertEqual(len(reads), 2)
        self.assertLess(time.monotonic() - began, 0.5)

    def test_cancelled_search_retains_bounded_locations_without_source_text(self):
        from adaptive_rsi.native import execute_tools
        (self.workspace / 'a.txt').write_text('needle private-source-content\n')
        (self.workspace / 'b.txt').write_bytes(b'x' * 1024 * 1024)
        self.tools._file_cache = ['a.txt', 'b.txt']
        original, reads, trace = self.tools.read_source, [], []
        def slow_source(path, maximum):
            if path.name == 'b.txt':
                time.sleep(0.02)
                reads.append(path.name)
                self.tools.check_cancelled()
            return original(path, maximum)
        self.tools.cancel = lambda: bool(reads)
        self.tools.trace = trace.append
        began = time.monotonic()
        with patch.object(self.tools, 'read_source', side_effect=slow_source):
            with self.assertRaises(Cancelled) as interrupted:
                execute_tools(self.tools, [('sensitive-provider-call-id', 'search', {'text': 'needle'})])
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(interrupted.exception.partial_results, [{'path': 'a.txt', 'line': 1}])
        self.assertEqual(trace[-1]['partial_results'], [{'path': 'a.txt', 'line': 1}])
        self.assertEqual(trace[-1]['state'], 'cancelled')
        self.assertNotIn('private-source-content', str(trace))
        self.assertNotIn('sensitive-provider-call-id', str(trace))

    def test_native_trace_survives_reopen_without_arguments_or_source_secrets(self):
        import json
        from types import SimpleNamespace
        from adaptive_rsi.config import DEFAULTS
        from adaptive_rsi.native import NativeAgent
        from adaptive_rsi.store import Store
        secret = 'API_SECRET_SHOULD_NOT_PERSIST'
        (self.workspace / 'source.txt').write_text(secret)
        store = Store(self.workspace.parent / 'trace-state')
        fake = SimpleNamespace(root=store.root, reserve_call=lambda *args: 'host-call', record_usage=lambda *args: None,
                               event=store.event, execute=lambda *args: None, catalog_entry=lambda *a, **k: ({}, None))
        replies = [{'output': [{'type': 'function_call', 'call_id': secret, 'name': 'read_file', 'arguments': json.dumps({'path': 'source.txt', 'start_line': 1, 'end_line': 1})}]},
                   {'output': [{'type': 'function_call', 'call_id': 'finish', 'name': 'finish', 'arguments': json.dumps({'summary': 'done'})}]}]
        try:
            with patch('adaptive_rsi.native.Credentials') as credentials, patch('adaptive_rsi.native.post_json', side_effect=replies):
                credentials.return_value.get.return_value = None
                NativeAgent(fake).invoke('implementation', secret, self.workspace, 'p', 'r', DEFAULTS,
                    {'model': 'test', 'effort': 'default'}, {'id': 'test', 'protocol': 'responses', 'base_url': 'https://invalid.example'})
        finally:
            store.close()
        reopened = Store(store.root)
        try:
            rows = reopened.query("SELECT data FROM events WHERE kind='native_tool' ORDER BY seq")
            self.assertEqual([json.loads(row['data'])['state'] for row in rows], ['started', 'completed'])
            self.assertEqual(len({json.loads(row['data'])['invocation'] for row in rows}), 1)
            self.assertNotIn(secret, json.dumps(reopened.query('SELECT * FROM events')))
        finally:
            reopened.close()

    def test_trace_storage_failure_does_not_turn_successful_tool_into_failure(self):
        from adaptive_rsi.native import execute_tools
        self.tools.trace = lambda detail: (_ for _ in ()).throw(OSError('unavailable'))
        result = execute_tools(self.tools, [('call', 'read_file', {'path': 'folder/secret.txt', 'start_line': 1, 'end_line': 1})])
        self.assertIn('workspace source', result[0][2])

    def test_killed_tool_process_leaves_started_receipt_and_never_replays_write(self):
        import subprocess
        import sys
        from adaptive_rsi.runtime import Runtime
        from adaptive_rsi.store import Store
        state = self.workspace.parent / 'interrupted-state'
        marker = self.workspace.parent / 'after-write'
        script = r'''
import json,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from adaptive_rsi.config import DEFAULTS
from adaptive_rsi.native import NativeAgent,WorkspaceTools
from adaptive_rsi.store import Store
state,workspace,marker=map(Path,sys.argv[1:])
store=Store(state)
fake=SimpleNamespace(root=store.root,reserve_call=lambda *args:'host-call',record_usage=lambda *args:None,event=store.event,execute=lambda *args:None,catalog_entry=lambda *a,**k:({},None))
response={'output':[{'type':'function_call','call_id':'write','name':'write_file','arguments':json.dumps({'path':'written.txt','content':'written once'})}]}
original=WorkspaceTools.execute
def interrupted(tools,name,args):
    result=original(tools,name,args)
    marker.write_text('write completed before terminal receipt')
    while True:time.sleep(1)
with patch('adaptive_rsi.native.Credentials') as credentials,patch('adaptive_rsi.native.post_json',return_value=response),patch.object(WorkspaceTools,'execute',interrupted):
    credentials.return_value.get.return_value=None
    NativeAgent(fake).invoke('implementation','fixture',workspace,'p','r',DEFAULTS,{'model':'test','effort':'default'},{'id':'test','protocol':'responses','base_url':'https://invalid.example'})
'''
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(state), str(self.workspace), str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), 'child reached write/receipt interruption boundary')
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        recovered = Runtime.open(state, agent=object())
        try:
            recovered.configure({"updates": {"mode": "manual"}})
            recovered.start(paused=True)
            rows = recovered.store.query("SELECT data FROM events WHERE kind='native_tool'")
            self.assertEqual(len(rows), 1)
            self.assertIn('started', rows[0]['data'])
            self.assertNotIn('completed', rows[0]['data'])
            self.assertEqual((self.workspace / 'written.txt').read_text(), 'written once')
            self.assertFalse(recovered.store.query('SELECT * FROM tasks'))
        finally:
            recovered.close()

    def test_command_trace_distinguishes_success_nonzero_exit_and_transport_error(self):
        from adaptive_rsi.native import execute_tools
        for exit_code, error, expected in ((0, None, 'completed'), (3, None, 'failed'), (0, 'timeout', 'failed')):
            with self.subTest(exit_code=exit_code, error=error):
                trace = []
                self.tools.trace = trace.append
                with patch.object(self.tools, 'execute', return_value={'exit_code': exit_code, 'error': error, 'stdout': '', 'stderr': ''}):
                    execute_tools(self.tools, [('call', 'run_command', {'argv': ['fixture'], 'cwd': '.'})])
                self.assertEqual(trace[-1]['state'], expected)
