import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from adaptive_rsi import auth_sessions
from adaptive_rsi.auth_sessions import AuthError, AuthSessions, CodexAuth, OpenCodeAuth
from adaptive_rsi.cli_worker import CODEX_AUTH_LOCK
from adaptive_rsi.opencode import AUTH_LOCK, OpenCodeCapabilities, load_auth_export, merge_auth_export


@contextlib.contextmanager
def held_by_model(lock):
    held, release = threading.Event(), threading.Event()

    def model():
        with lock:
            held.set()
            release.wait(5)
    thread = threading.Thread(target=model)
    thread.start()
    if not held.wait(1):
        raise AssertionError("Model did not acquire credential lock")
    try:
        yield
    finally:
        release.set()
        thread.join(2)


class FakeAuth:
    def __init__(self, backend, gate=None, failure=None, method="auto", account=None):
        self.backend = backend
        self.gate = gate or threading.Event()
        self.failure = failure
        self.method = method
        self.account = account
        self.temp = tempfile.TemporaryDirectory()
        self.credentials = Path(self.temp.name) / "auth.json"
        self.closed = False
        self.provider = None
        self.code = None
        self.inputs = None

    def providers(self):
        return [{"id": "openai", "name": "openai", "methods": [
            {"index": 0, "label": "device", "type": "oauth", "supported": True},
            {"index": 1, "label": "API key", "type": "api", "supported": True}]}]

    def start(self, provider=None, method=0, inputs=None):
        self.provider, self.inputs = provider, inputs
        if self.failure:
            raise AuthError(self.failure)
        return {"verification_url": "https://example.test/device?private=authorization-link", "user_code": "SENSITIVE-CODE",
                "method": self.method, "instructions": "SENSITIVE-INSTRUCTIONS"}

    def _finish(self, stop, deadline):
        while not self.gate.wait(0.01):
            if self.closed or stop.is_set() or time.time() >= deadline:
                return False
        value = {"tokens": {"refresh_token": "SECRET-REFRESH", "access_token": "SECRET-ACCESS"}} if self.backend == "codex" else {
            self.provider: {"type": "oauth", "refresh": "SECRET-REFRESH", "access": "SECRET-ACCESS", "expires": 1234}}
        self.credentials.write_text(json.dumps(value))
        return True

    def wait(self, stop, deadline):
        return self._finish(stop, deadline)

    def complete(self, code="", timeout=600):
        self.code = code
        return self._finish(threading.Event(), time.time() + timeout)

    def close(self):
        self.closed = True
        # 测试保留临时凭据到fixture销毁，以免与读写线程竞争。


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex"), "XDG_DATA_HOME": str(self.root / "data")})
        self.env.start()
        self.created = []
        self.gate = threading.Event()
        self.options = {}

        def factory(backend):
            worker = FakeAuth(backend, self.gate, **self.options)
            self.created.append(worker)
            return worker
        self.factory = factory
        self.manager = AuthSessions(self.root / "state", factory=factory, ttl=2)

    def tearDown(self):
        self.manager.close()
        for worker in self.created:
            worker.temp.cleanup()
        self.env.stop()
        self.temp.cleanup()

    def wait_state(self, session, state, manager=None):
        manager = manager or self.manager
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = manager.status(session["id"])
            if result["state"] == state:
                return result
            time.sleep(0.005)
        self.fail("Expected " + state + ": " + repr(result))

    def test_corrupt_credentials_preserve_healthy_backend_snapshot(self):
        for broken in ("codex", "opencode"):
            with self.subTest(backend=broken):
                def read(path):
                    if ("codex" in str(path)) == (broken == "codex"):
                        raise AuthError("invalid_credentials")
                    return {"tokens": {"refresh_token": "fixture"}, "openai": {"type": "api"}}
                with patch("adaptive_rsi.auth_sessions._read_json", side_effect=read):
                    result = self.manager.snapshot()
                self.assertEqual(result[broken]["error"], "invalid_credentials")
                self.assertEqual(result[broken]["state"], "signed_out")
                self.assertEqual(result["opencode" if broken == "codex" else "codex"]["state"], "authenticated")

    def test_status_and_busy_return_while_credential_install_is_blocked(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        session = self.manager.reserve('opencode', 'openai')
        worker = FakeAuth('opencode')
        self.created.append(worker)
        worker.capabilities = types.SimpleNamespace(modern=True)
        worker.export_credential = lambda provider: [{'fixture': True}]
        def install(*args):
            entered.set()
            release.wait(3)
        worker.install_credential = install
        writer = threading.Thread(target=self.manager._save,
                                  args=(worker, 'opencode', 'openai', session['id'], threading.Event()))
        results = []
        def read():
            results.append((self.manager.status(session['id']), self.manager.busy, self.manager.snapshot()))
            returned.set()
        writer.start()
        try:
            self.assertTrue(entered.wait(2))
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(returned.wait(1), 'status/busy blocked on credential import')
            self.assertEqual(results[0][0]['state'], 'starting')
            self.assertEqual(results[0][1], 1)
        finally:
            release.set()
            writer.join(3)
            if 'reader' in locals():
                reader.join(3)

    def test_busy_tolerates_session_pruned_during_status(self):
        session = self.manager.reserve('opencode', 'openai')
        original = self.manager.status
        def status(session_id):
            with self.manager.lock:
                self.manager.sessions.pop(session_id)
                self.manager._publish_sessions()
            return original(session_id)
        with patch.object(self.manager, 'status', side_effect=status):
            self.assertEqual(self.manager.busy, 0)

    def test_complete_rechecks_expiration_under_commit_lock(self):
        session = self.manager.reserve('opencode', 'openai')
        self.manager._update(session['id'], state='awaiting_authorization', method='code')
        with patch.object(self.manager, 'status', return_value={'state': 'awaiting_authorization'}), \
             patch.object(self.manager, 'now', return_value=session['expires_at'] + 1):
            with self.assertRaises(AuthError):
                self.manager.complete(session['id'], 'fixture-code')
        self.assertEqual(self.manager.status(session['id'])['state'], 'expired')
        self.assertNotIn('_code', self.manager.sessions[session['id']])

    def test_host_snapshot_lists_credentials_once(self):
        manager = AuthSessions(self.root / "host-snapshot")
        try:
            with patch("adaptive_rsi.auth_sessions.shutil.which", return_value="/fixture/opencode"), patch(
                    "adaptive_rsi.auth_sessions.detect_opencode",
                    return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))), patch(
                    "adaptive_rsi.auth_sessions.auth_providers", return_value=["openai"]) as discover:
                self.assertEqual(manager.snapshot()["opencode"]["authenticated_providers"], ["openai"])
                discover.assert_called_once()
                self.assertEqual(discover.call_args.args[0], "/fixture/opencode")
        finally:
            manager.close()

    def test_deeply_nested_json_is_rejected_without_breaking_snapshot_or_startup(self):
        payload = '[' * 100000 + ']' * 100000
        path = self.root / 'codex/auth.json'
        path.parent.mkdir(parents=True)
        path.write_text(payload)
        with self.assertRaises(AuthError) as caught:
            auth_sessions._read_json(path)
        self.assertEqual(caught.exception.code, 'invalid_credentials')
        self.assertEqual(self.manager.snapshot()['codex']['error'], 'invalid_credentials')
        state = self.root / 'nested-state'
        state.mkdir()
        (state / 'auth-sessions.json').write_text(payload)
        manager = AuthSessions(state, factory=self.factory)
        try:
            self.assertEqual(manager.sessions, {})
            self.assertEqual((state / 'auth-sessions.json').read_text(), payload)
        finally:
            manager.close()

    def test_modern_logout_removes_legacy_provider_without_export(self):
        path = auth_sessions.auth_path()
        auth_sessions._write_json(path, {'openai': {'type': 'api', 'key': 'fixture'},
                                        'other': {'type': 'api', 'key': 'keep'}})
        manager = AuthSessions(self.root / 'modern-logout')
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                 patch('adaptive_rsi.auth_sessions.logout_provider', return_value=True), \
                 patch('adaptive_rsi.auth_sessions.remove_auth_export', return_value=False), \
                 patch.object(manager, 'snapshot', return_value={}):
                manager.logout('opencode', 'openai')
            self.assertEqual(auth_sessions._read_json(path), {'other': {'type': 'api', 'key': 'keep'}})
        finally:
            manager.close()

    def test_logout_normalizes_auth_export_removal_failures(self):
        manager = AuthSessions(self.root / 'export-removal-error')
        try:
            for failure in (RuntimeError('corrupt export'), OSError('disk full')):
                with self.subTest(failure=type(failure).__name__):
                    with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                         patch('adaptive_rsi.auth_sessions.remove_auth_export', side_effect=failure), \
                         patch('adaptive_rsi.auth_sessions.logout_provider') as cli_logout:
                        with self.assertRaises(AuthError) as caught:
                            manager.logout('opencode', 'openai')
                    self.assertEqual(caught.exception.code, 'auth_protocol_error')
                    cli_logout.assert_not_called()
        finally:
            manager.close()

    def test_logout_failure_keeps_active_session_running(self):
        manager = AuthSessions(self.root / "export-failure-session", factory=self.factory, ttl=600)
        try:
            session = manager.start("opencode", "openai")
            self.wait_state(session, "awaiting_authorization", manager)
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                 patch('adaptive_rsi.auth_sessions.remove_auth_export', side_effect=RuntimeError('corrupt export')), \
                 patch('adaptive_rsi.auth_sessions.logout_provider') as cli_logout:
                with self.assertRaises(AuthError) as caught:
                    manager.logout('opencode', 'openai')
            self.assertEqual(caught.exception.code, 'auth_protocol_error')
            cli_logout.assert_not_called()
            # 凭据移除失败不得产生「会话已取消」的副作用，登录仍可继续。
            self.assertEqual(manager.status(session['id'])['state'], 'awaiting_authorization')
            self.assertFalse(self.created[-1].closed)
        finally:
            manager.close()

    def test_logout_normalizes_real_export_write_failure(self):
        merge_auth_export([{'integrationID': 'openai', 'id': 'credential',
                            'value': {'type': 'key', 'key': 'PRIVATE-KEY'}},
                           {'integrationID': 'other', 'id': 'keep',
                            'value': {'type': 'key', 'key': 'PRIVATE-OTHER'}}])
        manager = AuthSessions(self.root / 'export-write-error')
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value=None), \
                 patch('adaptive_rsi.opencode._private_json', side_effect=OSError('read-only')):
                with self.assertRaises(AuthError) as caught:
                    manager.logout('opencode', 'openai')
            self.assertEqual(caught.exception.code, 'auth_protocol_error')
            self.assertEqual([item['integrationID'] for item in load_auth_export()],
                             ['openai', 'other'])
        finally:
            manager.close()

    def test_authorization_rejection_within_ttl_is_not_expiration(self):
        worker = FakeAuth('opencode', self.gate)
        self.created.append(worker)
        with patch.object(worker, 'complete', return_value=False), \
             patch.object(self.manager, 'factory', return_value=worker):
            session = self.manager.start('opencode', 'openai')
            result = self.wait_state(session, 'failed')
        self.assertEqual(result['error'], 'authorization_failed')
        self.assertGreater(result['expires_at'], time.time())

    def test_unexpected_worker_error_emits_type_without_sensitive_message(self):
        with patch.object(self.manager, 'factory', side_effect=ValueError('SECRET-CREDENTIAL')), \
             self.assertLogs('adaptive_rsi.auth_sessions', level='WARNING') as logs:
            session = self.manager.start('opencode', 'openai')
            self.assertEqual(self.wait_state(session, 'failed')['error'], 'auth_protocol_error')
        self.assertIn('ValueError', '\n'.join(logs.output))
        self.assertIn(session['id'], '\n'.join(logs.output))
        self.assertNotIn('SECRET-CREDENTIAL', '\n'.join(logs.output))

    def test_production_opencode_snapshot_rejects_deep_legacy_json(self):
        path = auth_sessions.auth_path()
        path.parent.mkdir(parents=True)
        payload = '[' * 100000 + ']' * 100000
        path.write_text(payload)
        for binary in (None, '/fixture/opencode'):
            with self.subTest(binary=binary):
                manager = AuthSessions(self.root / ('legacy-host' if binary else 'legacy-fallback'))
                try:
                    with patch('adaptive_rsi.auth_sessions.shutil.which', return_value=binary), \
                         patch('adaptive_rsi.opencode.shutil.which', return_value=binary), \
                         patch('adaptive_rsi.auth_sessions.detect_opencode',
                               return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))):
                        result = manager.snapshot()
                    self.assertEqual(result['opencode']['error'], 'invalid_credentials')
                    self.assertEqual(result['codex']['state'], 'signed_out')
                    self.assertEqual(path.read_text(), payload)
                finally:
                    manager.close()

    def test_launch_rejects_unvalidated_or_changed_reserved_inputs_without_spawning(self):
        inputs = {'region': 'original'}
        session = self.manager.reserve('opencode', 'openai', method=1, inputs=inputs)
        inputs['region'] = 'mutated'
        with patch.object(self.manager, '_thread') as spawn:
            for invalid in (123, [], {'region': 'x' * 2049}):
                with self.subTest(inputs=invalid):
                    with self.assertRaises(AuthError) as caught:
                        self.manager.launch(session['id'], 1, invalid)
                    self.assertEqual(caught.exception.code, 'invalid_auth_inputs')
            with self.assertRaises(AuthError):
                self.manager.launch(session['id'], 1, {'region': 'changed'})
            spawn.assert_not_called()
            self.manager.launch(session['id'], 1, {'region': 'original'})
            spawn.assert_called_once()
        self.assertNotIn('region', self.manager.file.read_text())

    def test_concurrent_provider_discovery_starts_one_worker(self):
        manager = AuthSessions(self.root / 'single-discovery')
        entered, release = threading.Event(), threading.Event()
        answers, failures, made = [], [], []
        def discover():
            entered.set()
            release.wait(2)
            return [{'id': 'openai', 'methods': []}]
        def factory(backend):
            worker = FakeAuth(backend)
            worker.providers = discover
            made.append(worker)
            return worker
        def request():
            try:
                answers.append(manager.providers())
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=request) for _ in range(3)]
        try:
            with patch.object(manager, 'factory', side_effect=factory):
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(1))
                time.sleep(.05)
                self.assertEqual(len(made), 1)
                release.set()
                for thread in threads:
                    thread.join(2)
                self.assertFalse(failures)
                self.assertEqual(answers, [[{'id': 'openai', 'methods': []}]] * 3)
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
            manager.close()
            for worker in made:
                worker.temp.cleanup()

    def test_key_save_uses_one_worker_and_busy_rejects_before_discovery(self):
        with patch('adaptive_rsi.auth_sessions.shutil.which', return_value=None):
            self.manager.set_api_key('opencode', 'openai', 'fixture-key')
        self.assertEqual(len(self.created), 1)
        session = self.manager.reserve('opencode', 'openai')
        with self.assertRaises(AuthError) as caught:
            self.manager.set_api_key('opencode', 'openai', 'fixture-key')
        self.assertEqual(caught.exception.code, 'auth_busy')
        self.assertEqual(len(self.created), 1)
        self.manager.cancel(session['id'])

    def test_provider_discovery_failure_releases_waiters_and_allows_retry(self):
        manager = AuthSessions(self.root / 'failed-flight')
        entered, release = threading.Event(), threading.Event()
        errors = []
        def factory(backend):
            entered.set()
            release.wait(2)
            raise AuthError('auth_protocol_error')
        def request():
            try:
                manager.providers()
            except AuthError as exc:
                errors.append(exc.code)
        threads = [threading.Thread(target=request) for _ in range(3)]
        try:
            with patch.object(manager, 'factory', side_effect=factory) as create:
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(1))
                time.sleep(.05)
                release.set()
                for thread in threads:
                    thread.join(2)
                self.assertEqual(errors, ['auth_protocol_error'] * 3)
                self.assertEqual(create.call_count, 1)
                self.assertEqual(manager.provider_flights, {})
            worker = FakeAuth('opencode')
            self.created.append(worker)
            with patch.object(manager, 'factory', return_value=worker):
                self.assertEqual(manager.providers()[0]['id'], 'openai')
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
            manager.close()

    def test_key_cleanup_failure_always_releases_credential_lock(self):
        credential_lock = threading.Lock()
        worker = FakeAuth('opencode')
        self.created.append(worker)
        with patch.object(self.manager, 'factory', return_value=worker), \
             patch.object(worker, 'close', side_effect=OSError('cleanup failed')), \
             patch('adaptive_rsi.auth_sessions.AUTH_LOCK', credential_lock), \
             patch.object(self.manager, 'snapshot', return_value={}):
            with self.assertRaises(OSError):
                self.manager.set_api_key('opencode', 'openai', 'fixture-key')
            acquired = credential_lock.acquire(blocking=False)
            if acquired:
                credential_lock.release()
            self.assertTrue(acquired)

    def test_key_export_finishing_after_close_cannot_install_credentials(self):
        entered, release = threading.Event(), threading.Event()
        installed, errors = [], []
        worker = FakeAuth('opencode')
        self.created.append(worker)
        worker.capabilities = types.SimpleNamespace(modern=True)
        worker.set_api_key = lambda *args: None
        def export(provider):
            entered.set()
            release.wait(2)
            return [{'fixture': True}]
        worker.export_credential = export
        worker.install_credential = lambda *args: installed.append(True)
        def save():
            try:
                self.manager.set_api_key('opencode', 'openai', 'fixture-key')
            except AuthError as exc:
                errors.append(exc.code)
        with patch.object(self.manager, 'factory', return_value=worker), \
             patch.object(self.manager, 'snapshot', return_value={}):
            thread = threading.Thread(target=save)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.manager.close()
            finally:
                release.set()
                thread.join(2)
        self.assertEqual(installed, [])
        self.assertEqual(errors, ['service_closed'])

    def test_discovery_cleanup_error_is_shared_and_not_cached(self):
        manager = AuthSessions(self.root / 'cleanup-flight')
        entered, release = threading.Event(), threading.Event()
        errors = []
        worker = FakeAuth('opencode')
        self.created.append(worker)
        def discover():
            entered.set()
            release.wait(2)
            return [{'id': 'openai', 'methods': []}]
        def request():
            try:
                manager.providers()
            except OSError:
                errors.append('cleanup')
        threads = [threading.Thread(target=request) for _ in range(2)]
        try:
            with patch.object(manager, 'factory', return_value=worker) as create, \
                 patch.object(worker, 'providers', side_effect=discover), \
                 patch.object(worker, 'close', side_effect=OSError('cleanup failed')):
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(1))
                time.sleep(.05)
                release.set()
                for thread in threads:
                    thread.join(2)
                self.assertEqual(errors, ['cleanup', 'cleanup'])
                self.assertFalse(manager.providers_cache)
                self.assertFalse(manager.provider_flights)
                with self.assertRaises(OSError):
                    manager.providers()
                self.assertEqual(create.call_count, 2)
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
            manager.close()

    def test_discovery_and_cleanup_failure_share_final_error(self):
        manager = AuthSessions(self.root / 'double-failure-flight')
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        errors = []
        worker = FakeAuth('opencode')
        self.created.append(worker)
        def discover():
            entered.set()
            release.wait(2)
            raise AuthError('auth_protocol_error')
        def request():
            try:
                manager.providers()
            except Exception as exc:
                errors.append(type(exc).__name__)
        leader = threading.Thread(target=request)
        follower = threading.Thread(target=request)
        try:
            with patch.object(manager, 'factory', return_value=worker), \
                 patch.object(worker, 'providers', side_effect=discover), \
                 patch.object(worker, 'close', side_effect=OSError('cleanup failed')):
                leader.start()
                self.assertTrue(entered.wait(1))
                event = manager.provider_flights[('opencode', 0)]['done']
                original_wait = event.wait
                def wait(timeout=None):
                    waiting.set()
                    return original_wait(timeout)
                with patch.object(event, 'wait', side_effect=wait):
                    follower.start()
                    self.assertTrue(waiting.wait(1))
                    release.set()
                    leader.join(2)
                    follower.join(2)
            self.assertEqual(errors, ['OSError', 'OSError'])
            self.assertFalse(manager.provider_flights)
            self.assertFalse(manager.providers_cache)
        finally:
            release.set()
            leader.join(2)
            if follower.ident is not None:
                follower.join(2)
            manager.close()

    def test_production_opencode_snapshot_rejects_non_object_legacy_json(self):
        path = auth_sessions.auth_path()
        path.parent.mkdir(parents=True)
        path.write_text('[]')
        manager = AuthSessions(self.root / 'list-state')
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value=None), \
                 patch('adaptive_rsi.opencode.shutil.which', return_value=None):
                self.assertEqual(manager.snapshot()['opencode']['error'], 'invalid_credentials')
            self.assertEqual(path.read_text(), '[]')
        finally:
            manager.close()

    def test_concurrent_snapshot_preserves_failure_cache_shape(self):
        manager = AuthSessions(self.root / 'racing-status-cache')
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        class Cache(dict):
            reads = 0
            def get(cache, key, default=None):
                if threading.current_thread() is thread and key == 'opencode':
                    cache.reads += 1
                    if cache.reads == 2:
                        entered.set()
                        self.assertTrue(release.wait(2))
                return super().get(key, default)
        manager.status_cache = Cache()
        def request():
            try:
                results.append(manager.snapshot()['opencode'])
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=request)
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/cli'), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))), \
                 patch('adaptive_rsi.auth_sessions.auth_providers', side_effect=RuntimeError('OpenCode credential listing failed')):
                thread.start()
                self.assertTrue(entered.wait(2))
                # Cache.get executes under the manager lock; publish the completed
                # concurrent discovery directly at this controlled boundary.
                manager.status_cache['opencode'] = (float('inf'), {
                    'state': 'signed_out', 'available': True, 'error': 'opencode_error',
                    'transport': 'host', 'authenticated_providers': []})
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(results[0]['error'], 'opencode_error')
                self.assertEqual(manager.snapshot()['opencode']['error'], 'opencode_error')
        finally:
            release.set()
            thread.join(2)
            manager.close()

    def test_failed_snapshot_discovery_is_cached_and_invalidated(self):
        manager = AuthSessions(self.root / 'failed-status-cache')
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/cli'), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))) as detect, \
                 patch('adaptive_rsi.auth_sessions.auth_providers', side_effect=RuntimeError('OpenCode credential listing failed')) as discover, \
                 patch('adaptive_rsi.auth_sessions.time.monotonic', return_value=100) as clock:
                for _ in range(3):
                    self.assertEqual(manager.snapshot()['opencode']['error'], 'opencode_error')
                self.assertEqual((detect.call_count, discover.call_count), (1, 1))
                with manager.lock:
                    manager._invalidate_caches('opencode')
                manager.snapshot()
                self.assertEqual(discover.call_count, 2)
                clock.return_value = 126
                stale = manager.snapshot()['opencode']
                # 过期条目先返回旧结果，探测转入后台线程。
                self.assertEqual(stale['error'], 'opencode_error')
                self.assertTrue(stale['refreshing'])
                for _ in range(200):
                    if not manager.status_refreshes:
                        break
                    time.sleep(0.01)
                self.assertEqual(discover.call_count, 3)
                self.assertFalse(manager.status_refreshes)
                self.assertGreater(manager.status_cache['opencode'][0], 126)
                self.assertNotIn('refreshing', manager.snapshot()['opencode'])
        finally:
            manager.close()
        self.assertFalse(manager.status_cache)

    def test_stale_snapshot_returns_immediately_and_refreshes_in_background(self):
        manager = AuthSessions(self.root / 'stale-while-revalidate')
        calls = []

        def detect(binary, **kwargs):
            calls.append(binary)
            if len(calls) > 1:
                time.sleep(2)
            return OpenCodeCapabilities('1.19.3', (1, 19, 3))

        def providers(binary, capabilities):
            return ['openai'] if len(calls) < 2 else []

        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', side_effect=detect), \
                 patch('adaptive_rsi.auth_sessions.auth_providers', side_effect=providers):
                self.assertEqual(manager.snapshot()['opencode']['authenticated_providers'], ['openai'])
                with manager.cache_lock:
                    expires, value = manager.status_cache['opencode']
                    manager.status_cache['opencode'] = (time.monotonic() - 1, value)
                started = time.perf_counter()
                stale = manager.snapshot()['opencode']
                elapsed = time.perf_counter() - started
                # 探测在后台 sleep 2s，请求线程只读缓存。
                self.assertLess(elapsed, 0.2)
                self.assertTrue(stale['refreshing'])
                self.assertEqual(stale['authenticated_providers'], ['openai'])
                for _ in range(300):
                    if not manager.status_refreshes:
                        break
                    time.sleep(0.01)
                self.assertFalse(manager.status_refreshes)
                self.assertEqual(manager.snapshot()['opencode']['authenticated_providers'], [])
        finally:
            manager.close()

    def test_stale_snapshot_does_not_wait_for_the_session_lock(self):
        """安装凭据/登出持有会话锁时，过期快照仍须立即返回。"""
        manager = AuthSessions(self.root / 'stale-while-locked')
        locked, release = threading.Event(), threading.Event()

        def hold_lock():
            with manager.lock:
                locked.set()
                release.wait(5)

        holder = threading.Thread(target=hold_lock, daemon=True)
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))), \
                 patch('adaptive_rsi.auth_sessions.auth_providers', return_value=['openai']):
                self.assertEqual(manager.snapshot()['opencode']['authenticated_providers'], ['openai'])
                with manager.cache_lock:
                    expires, value = manager.status_cache['opencode']
                    manager.status_cache['opencode'] = (time.monotonic() - 1, value)
                # 另一个线程持锁，模拟 _save/logout 在 CLI 子进程期间的会话锁占用。
                holder.start()
                self.assertTrue(locked.wait(2))
                started = time.perf_counter()
                stale = manager.snapshot()['opencode']
                elapsed = time.perf_counter() - started
                release.set()
                holder.join(2)
                self.assertLess(elapsed, 0.2)
                self.assertTrue(stale['refreshing'])
                self.assertEqual(stale['authenticated_providers'], ['openai'])
                for _ in range(300):
                    if not manager.status_refreshes:
                        break
                    time.sleep(0.01)
                self.assertFalse(manager.status_refreshes)
        finally:
            release.set()
            holder.join(2)
            manager.close()

    def test_invalidation_during_background_refresh_keeps_new_credentials(self):
        manager = AuthSessions(self.root / 'invalidate-during-refresh')
        path = auth_sessions.auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'openai': {'type': 'api', 'key': 'OLD'}}))
        entered, release = threading.Event(), threading.Event()
        calls = []

        def providers(binary, capabilities):
            data = json.loads(path.read_text())
            calls.append(sorted(data))
            if len(calls) == 2:
                entered.set()
                release.wait(3)
            return sorted(key for key, value in data.items() if isinstance(value, dict))

        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', return_value='/fixture/opencode'), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))), \
                 patch('adaptive_rsi.auth_sessions.auth_providers', side_effect=providers):
                self.assertEqual(manager.snapshot()['opencode']['authenticated_providers'], ['openai'])
                with manager.cache_lock:
                    expires, value = manager.status_cache['opencode']
                    manager.status_cache['opencode'] = (time.monotonic() - 1, value)
                self.assertTrue(manager.snapshot()['opencode']['refreshing'])
                self.assertTrue(entered.wait(2))
                # 模拟登录/登出：写新凭据并使缓存失效，下一次快照必须同步读到新值。
                path.write_text(json.dumps({'anthropic': {'type': 'api', 'key': 'NEW'}}))
                with manager.lock:
                    manager._invalidate_caches('opencode')
                fresh = manager.snapshot()['opencode']
                self.assertFalse(fresh.get('refreshing'))
                self.assertEqual(fresh['authenticated_providers'], ['anthropic'])
                # 旧代际的后台结果返回后不能覆盖失效后的新缓存。
                release.set()
                for _ in range(300):
                    if not manager.status_refreshes:
                        break
                    time.sleep(0.01)
                self.assertFalse(manager.status_refreshes)
                self.assertEqual(manager.snapshot()['opencode']['authenticated_providers'], ['anthropic'])
                self.assertEqual(calls, [['openai'], ['openai'], ['anthropic']])
        finally:
            release.set()
            manager.close()

    def test_snapshot_reports_container_readiness_for_both_backends(self):
        for docker, exit_code, error in ((None, 0, 'auth_container_unavailable'),
                                          ('/fixture/docker', 1, 'auth_worker_image_missing'),
                                          ('/fixture/docker', 0, None)):
            with self.subTest(docker=docker, exit_code=exit_code):
                manager = AuthSessions(self.root / 'readiness', config={'sandbox': {'backend': 'docker'}})
                try:
                    with patch('adaptive_rsi.auth_sessions.shutil.which', side_effect=lambda name: docker if name == 'docker' else '/fixture/' + name), \
                         patch('adaptive_rsi.auth_sessions.run_process', return_value={'exit_code': exit_code, 'error': ''}) as inspect, \
                         patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))), \
                         patch('adaptive_rsi.auth_sessions.auth_providers', return_value=[]):
                        result = manager.snapshot()
                    for backend in ('codex', 'opencode'):
                        self.assertEqual(result[backend]['available'], error is None)
                        self.assertEqual(result[backend].get('error'), error)
                        self.assertEqual(result[backend]['transport'], 'docker')
                    self.assertEqual(inspect.call_count, int(docker is not None))
                finally:
                    manager.close()

    def test_container_readiness_probe_is_cached_and_invalidated(self):
        manager = AuthSessions(self.root / 'readiness-cache', config={'sandbox': {'backend': 'docker'}})
        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which',
                       side_effect=lambda name: '/fixture/docker' if name == 'docker' else None), \
                 patch('adaptive_rsi.auth_sessions.run_process',
                       return_value={'exit_code': 1, 'error': ''}) as inspect, \
                 patch('adaptive_rsi.auth_sessions.detect_opencode',
                       return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))), \
                 patch('adaptive_rsi.auth_sessions.auth_providers', return_value=[]):
                first = manager.snapshot()
                second = manager.snapshot()
                self.assertEqual([first['codex']['error'], second['codex']['error']],
                                 ['auth_worker_image_missing', 'auth_worker_image_missing'])
                # 两次快照只探测一次镜像；codex/opencode 共用同一次子进程结果。
                self.assertEqual(inspect.call_count, 1)
                with manager.cache_lock:
                    key = next(iter(manager.container_cache))
                    manager.container_cache[key] = (time.monotonic() - 1, 'auth_worker_image_missing')
                self.assertEqual(manager.snapshot()['opencode']['error'], 'auth_worker_image_missing')
                # 过期值先复用，后台线程完成 inspect 后才增加调用次数。
                deadline = time.monotonic() + 3
                while manager.container_refreshes and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertFalse(manager.container_refreshes)
                self.assertEqual(inspect.call_count, 2)
                with manager.lock:
                    manager._invalidate_caches('codex')
                self.assertEqual(manager.snapshot()['codex']['error'], 'auth_worker_image_missing')
                self.assertEqual(inspect.call_count, 3)
        finally:
            manager.close()
        self.assertFalse(manager.container_cache)

    def test_stale_container_readiness_returns_immediately_and_refreshes_in_background(self):
        manager = AuthSessions(self.root / 'container-stale', config={'sandbox': {'backend': 'docker'}})
        calls = []

        def inspect(argv, timeout=None):
            calls.append(argv)
            if len(calls) > 1:
                time.sleep(1)
                return {'exit_code': 1, 'error': ''}
            return {'exit_code': 0, 'error': ''}

        def which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', side_effect=which), \
                 patch('adaptive_rsi.auth_sessions.run_process', side_effect=inspect), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))):
                self.assertTrue(manager.snapshot()['codex']['available'])
                with manager.cache_lock:
                    key = next(iter(manager.container_cache))
                    expires, value = manager.container_cache[key]
                    manager.container_cache[key] = (time.monotonic() - 1, value)
                started = time.perf_counter()
                stale = manager.snapshot()
                elapsed = time.perf_counter() - started
                # inspect 在后台 sleep 1s，请求线程只读旧的就绪状态。
                self.assertLess(elapsed, 0.2)
                self.assertTrue(stale['codex']['available'])
                for _ in range(200):
                    if not manager.container_refreshes:
                        break
                    time.sleep(0.01)
                self.assertFalse(manager.container_refreshes)
                refreshed = manager.snapshot()
                self.assertFalse(refreshed['codex']['available'])
                self.assertEqual(refreshed['codex']['error'], 'auth_worker_image_missing')
        finally:
            manager.close()

    def test_overlapping_request_reuses_stale_container_readiness_during_refresh(self):
        manager = AuthSessions(self.root / 'container-overlap', config={'sandbox': {'backend': 'docker'}})
        calls = []
        release = threading.Event()

        def inspect(argv, timeout=None):
            calls.append(argv)
            if len(calls) > 1:
                # 后台刷新卡住，保证第二个请求到达时探测仍在进行。
                release.wait(5)
                return {'exit_code': 1, 'error': ''}
            return {'exit_code': 0, 'error': ''}

        def which(name):
            return '/usr/bin/docker' if name == 'docker' else None

        try:
            with patch('adaptive_rsi.auth_sessions.shutil.which', side_effect=which), \
                 patch('adaptive_rsi.auth_sessions.run_process', side_effect=inspect), \
                 patch('adaptive_rsi.auth_sessions.detect_opencode', return_value=OpenCodeCapabilities('1.19.3', (1, 19, 3))):
                self.assertTrue(manager.snapshot()['codex']['available'])
                with manager.cache_lock:
                    key = next(iter(manager.container_cache))
                    expires, value = manager.container_cache[key]
                    manager.container_cache[key] = (time.monotonic() - 1, value)
                self.assertTrue(manager.snapshot()['codex']['available'])
                for _ in range(200):
                    if manager.container_refreshes:
                        break
                    time.sleep(0.01)
                self.assertTrue(manager.container_refreshes)
                before = len(calls)
                started = time.perf_counter()
                stale = manager.snapshot()
                elapsed = time.perf_counter() - started
                # 已有后台刷新在跑时，请求线程直接复用旧值，不再同步 inspect。
                self.assertLess(elapsed, 0.2)
                self.assertEqual(len(calls), before)
                self.assertTrue(stale['codex']['available'])
                release.set()
                for _ in range(200):
                    if not manager.container_refreshes:
                        break
                    time.sleep(0.01)
                self.assertFalse(manager.container_refreshes)
        finally:
            release.set()
            manager.close()

    def test_initialization_never_starts_cli(self):
        self.assertEqual(self.created, [])
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.snapshot()["codex"]["state"], "signed_out")

    def test_container_snapshot_reads_v1_auth_without_host_opencode_binary(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "PRIVATE-REFRESH"}}))
        manager = AuthSessions(self.root / "container-state", config={"sandbox": {"backend": "docker"}})
        try:
            with patch("adaptive_rsi.auth_sessions.shutil.which",
                       side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None), patch(
                    "adaptive_rsi.auth_sessions.run_process", return_value={"exit_code": 0, "error": None}), patch(
                    "adaptive_rsi.auth_sessions.detect_opencode",
                    return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))):
                snapshot = manager.snapshot()
        finally:
            manager.close()
        self.assertEqual(snapshot["opencode"]["state"], "authenticated")
        self.assertEqual(snapshot["opencode"]["transport"], "docker")
        self.assertEqual(snapshot["opencode"]["authenticated_providers"], ["openai"])
        self.assertNotIn("PRIVATE-REFRESH", json.dumps(snapshot))

    def test_container_logout_removes_persisted_v2_export_without_host_cli(self):
        merge_auth_export([{"integrationID": "opencode-go", "id": "credential",
                            "value": {"type": "key", "key": "PRIVATE-KEY"}}])
        manager = AuthSessions(self.root / "container-logout", config={"sandbox": {"backend": "docker"}})
        try:
            with patch("adaptive_rsi.auth_sessions.shutil.which",
                       side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None):
                manager.logout("opencode", "opencode-go")
        finally:
            manager.close()
        self.assertEqual(load_auth_export(), [])

    def test_codex_wait_records_account_identity_and_requests_once(self):
        worker = object.__new__(CodexAuth)
        worker.notifications = [{"method": "account/login/completed",
                                 "params": {"loginId": "fixture-login", "success": True}}]
        worker.login_id = "fixture-login"
        calls = []

        def request(method, params, timeout=25):
            calls.append((method, params))
            return {"account": {"email": "a@example.test", "planType": "plus", "tokens": "SECRET"}}

        worker.request = request
        self.assertTrue(worker.wait(threading.Event(), time.time() + 1))
        self.assertEqual(calls, [("account/read", {"refreshToken": False})])
        accounts = auth_sessions._account_entries(worker.account)
        self.assertEqual(accounts, [{"label": "a@example.test", "plan": "plus"}])
        snapshot = AuthSessions._public({"state": "completed", "accounts": accounts})
        self.assertEqual(snapshot["accounts"], accounts)
        self.assertEqual(AuthSessions._public({"state": "failed"})["accounts"], [])
        private = AuthSessions._public({"accounts": [{"label": "a@example.test", "plan": "plus", "token": "SECRET"}]})
        self.assertNotIn("SECRET", json.dumps(private))
        self.assertEqual(len(AuthSessions._public({"accounts": [{"label": "a"}, {"label": "b"}, {"label": "c"}]})["accounts"]), 2)

    def test_codex_device_completion_persists_compatible_private_credentials(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.assertEqual(self.manager.busy, 1)
        self.gate.set()
        result = self.wait_state(session, "completed")
        path = self.root / "codex/auth.json"
        self.assertEqual(json.loads(path.read_text())["tokens"]["refresh_token"], "SECRET-REFRESH")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("user_code", result)
        self.assertNotIn("verification_url", result)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.snapshot()["codex"]["state"], "authenticated")

    def test_codex_completion_exposes_and_persists_account_identity(self):
        self.options["account"] = {"email": "a@example.test", "planType": "plus", "tokens": "SECRET"}
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        result = self.wait_state(session, "completed")
        self.assertEqual(result["accounts"], [{"label": "a@example.test", "plan": "plus"}])
        self.assertNotIn("SECRET", json.dumps(result))
        stored = json.loads(self.manager.file.read_text())[session["id"]]
        self.assertEqual(set(stored), {"id", "backend", "provider", "state", "expires_at", "error", "accounts"})
        self.assertEqual(stored["accounts"], [{"label": "a@example.test", "plan": "plus"}])
        listed = {row["id"]: row for row in self.manager.snapshot()["sessions"]}
        self.assertEqual(listed[session["id"]]["accounts"], [{"label": "a@example.test", "plan": "plus"}])
        restarted = AuthSessions(self.root / "state", factory=self.factory, ttl=2)
        try:
            self.assertEqual(restarted.status(session["id"])["accounts"],
                             [{"label": "a@example.test", "plan": "plus"}])
        finally:
            restarted.close()

    def test_pending_authorization_material_never_enters_metadata_or_snapshot(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        stored = self.manager.file.read_text() + json.dumps(self.manager.snapshot())
        for secret in ("SENSITIVE", "authorization-link", "example.test", "SECRET"):
            self.assertNotIn(secret, stored)
        self.assertEqual(self.manager.file.stat().st_mode & 0o777, 0o600)

    def test_cancel_prevents_late_credentials_and_allows_relogin(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.assertEqual(self.manager.cancel(session["id"])["state"], "cancelled")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())
        replacement = self.manager.start("codex")
        self.wait_state(replacement, "completed")

    def test_rejects_concurrent_backend_login(self):
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        with self.assertRaisesRegex(AuthError, "auth_busy"):
            self.manager.start("opencode", "github-copilot")
        self.assertEqual(self.manager.busy, 1)

    def test_confirmed_failed_code_creates_new_attempt_in_same_session(self):
        class RetryWorker(FakeAuth):
            def __init__(self, backend):
                super().__init__(backend, method="code")
                self.starts = 0

            def start(self, provider=None, method=0, inputs=None):
                self.starts += 1
                result = super().start(provider, method, inputs)
                result["verification_url"] = "https://example.test/attempt/" + str(self.starts)
                return result

            def complete(self, code="", timeout=600):
                if code == "wrong":
                    raise auth_sessions.AuthAttemptFailed()
                self.gate.set()
                return super().complete(code, timeout)

        worker = RetryWorker("opencode")
        self.created.append(worker)
        self.manager.factory = lambda backend: worker
        session = self.manager.start("opencode", "openai")
        first = self.wait_state(session, "awaiting_authorization")
        self.manager.complete(session["id"], "wrong", first["attempt_generation"])
        second = self.wait_state(session, "awaiting_authorization")
        self.assertEqual(second["error"], "authorization_failed")
        self.assertEqual(second["expires_at"], first["expires_at"])
        self.assertEqual(second["verification_url"], "https://example.test/attempt/2")
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "stale", first["attempt_generation"])
        self.manager.complete(session["id"], "correct", second["attempt_generation"])
        self.wait_state(session, "completed")
        self.assertEqual(worker.starts, 2)

    def test_confirmed_failures_stop_after_five_attempts(self):
        class RetryWorker(FakeAuth):
            def __init__(self):
                super().__init__("opencode", method="code")
                self.starts = 0
                self.completions = 0

            def start(self, provider=None, method=0, inputs=None):
                self.starts += 1
                return super().start(provider, method, inputs)

            def complete(self, code="", timeout=600):
                self.completions += 1
                raise auth_sessions.AuthAttemptFailed()

        worker = RetryWorker()
        self.created.append(worker)
        self.manager.factory = lambda backend: worker
        self.manager.ttl = 10
        session = self.manager.start("opencode", "openai")
        for generation in range(1, 6):
            current = self.wait_state(session, "awaiting_authorization")
            self.assertEqual(current["attempt_generation"], generation)
            self.manager.complete(session["id"], "wrong", generation)
        self.assertEqual(self.wait_state(session, "failed")["error"], "authorization_failed")
        self.assertEqual((worker.starts, worker.completions), (5, 5))
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "again", 5)

    def test_atomic_json_write_needs_no_post_replace_chmod(self):
        path = self.root / "atomic.json"
        with patch.object(Path, "chmod", side_effect=OSError("fixture")):
            auth_sessions._write_json(path, {"value": "new"})
        self.assertEqual(json.loads(path.read_text()), {"value": "new"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_complete_persist_failure_does_not_consume_code(self):
        self.options["method"] = "code"
        session = self.manager.start("opencode", "openai")
        current = self.wait_state(session, "awaiting_authorization")
        worker = self.created[0]
        with patch.object(self.manager, "_persist", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.manager.complete(session["id"], "fixture-code", current["attempt_generation"])
        time.sleep(.2)
        self.assertIsNone(worker.code)
        self.assertEqual(self.manager.status(session["id"])["state"], "awaiting_authorization")
        with self.manager.lock:
            self.assertNotIn("_code", self.manager.sessions[session["id"]])
            self.assertNotIn("_retry_capable", self.manager.sessions[session["id"]])

    def test_legacy_client_failure_remains_terminal(self):
        class RetryWorker(FakeAuth):
            def __init__(self):
                super().__init__("opencode", method="code")
                self.starts = 0

            def start(self, provider=None, method=0, inputs=None):
                self.starts += 1
                return super().start(provider, method, inputs)

            def complete(self, code="", timeout=600):
                raise auth_sessions.AuthAttemptFailed()

        worker = RetryWorker()
        self.created.append(worker)
        self.manager.factory = lambda backend: worker
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        self.manager.complete(session["id"], "wrong")
        self.assertEqual(self.wait_state(session, "failed")["error"], "authorization_failed")
        self.assertEqual(worker.starts, 1)

    def test_cancel_during_restart_prevents_later_install(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()

        class RetryWorker(FakeAuth):
            def __init__(self):
                super().__init__("opencode", method="code")
                self.starts = 0

            def start(self, provider=None, method=0, inputs=None):
                self.starts += 1
                if self.starts == 2:
                    entered.set()
                    release.wait(3)
                return super().start(provider, method, inputs)

            def complete(self, code="", timeout=600):
                raise auth_sessions.AuthAttemptFailed()

        worker = RetryWorker()
        self.created.append(worker)
        self.manager.factory = lambda backend: worker
        session = self.manager.start("opencode", "openai")
        first = self.wait_state(session, "awaiting_authorization")
        self.manager.complete(session["id"], "wrong", first["attempt_generation"])
        self.assertTrue(entered.wait(2))
        thread = threading.Thread(target=lambda: (self.manager.cancel(session["id"]), cancelled.set()))
        thread.start()
        try:
            self.assertTrue(cancelled.wait(1))
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.status(session["id"])["state"], "cancelled")
        self.assertEqual(worker.starts, 2)
        self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_restart_does_not_publish_after_original_expiration(self):
        clock = [1000.0]

        class RetryWorker(FakeAuth):
            def __init__(self):
                super().__init__("opencode", method="code")
                self.starts = 0

            def start(self, provider=None, method=0, inputs=None):
                self.starts += 1
                if self.starts == 2:
                    clock[0] += 3
                return super().start(provider, method, inputs)

            def complete(self, code="", timeout=600):
                raise auth_sessions.AuthAttemptFailed()

        worker = RetryWorker()
        self.created.append(worker)
        self.manager.factory = lambda backend: worker
        self.manager.now = lambda: clock[0]
        session = self.manager.start("opencode", "openai")
        first = self.wait_state(session, "awaiting_authorization")
        self.manager.complete(session["id"], "wrong", first["attempt_generation"])
        result = self.wait_state(session, "expired")
        self.assertEqual(result["error"], "auth_expired")
        self.assertNotIn("verification_url", result)
        self.assertEqual(worker.starts, 2)

    def test_busy_expires_without_browser_polling(self):
        self.manager.ttl = 1
        session = self.manager.start("codex")
        time.sleep(1.05)
        self.assertEqual(self.manager.busy, 0)
        # status() 的过期回收是非阻塞的：worker 正在持锁落盘时会按契约返回陈旧
        # ACTIVE 快照（见 status() 注释），因此这里断言最终收敛，而不是单次调用
        # 必定完成回收；busy 已在上面证明过期会话不占容量。
        self.wait_state(session, "expired")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())

    def test_start_tolerates_expiry_between_reserve_and_launch(self):
        # 注入时钟，确定性地复现 reserve→launch 之间 ttl 耗尽的窗口，不依赖 sleep 撞运气。
        clock = [1000.0]
        self.manager.now = lambda: clock[0]
        self.manager.ttl = 5
        launch = self.manager.launch

        def slow_launch(session_id, method=0, inputs=None):
            clock[0] += 6  # 调度停顿：launch 之前已越过 expires_at
            return launch(session_id, method, inputs)

        with patch.object(self.manager, "launch", slow_launch):
            session = self.manager.start("codex")
        self.assertEqual((session["state"], session["error"]), ("expired", "auth_expired"))
        self.assertNotIn("_reserved", session)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.status(session["id"])["state"], "expired")
        self.assertEqual(self.created, [])  # worker 从未派发，凭据不会被写入
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())
        # 过期会话不能借 complete() 写凭据；既有 invalid_auth_state 语义不变。
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "SECRET-CODE")

    def test_launch_returns_expired_state_for_unlaunched_reserved_session(self):
        clock = [2000.0]
        self.manager.now = lambda: clock[0]
        self.manager.ttl = 5
        session = self.manager.reserve("codex")
        self.assertEqual(self.manager.busy, 1)
        clock[0] += 6
        # launch 自己发现窗口已过期：返回终态而不是抛 invalid_auth_state。
        result = self.manager.launch(session["id"])
        self.assertEqual((result["state"], result["error"]), ("expired", "auth_expired"))
        self.assertNotIn("_reserved", result)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.created, [])
        # 只 reserve、永远不会 launch 的会话（准入被拦下）仍由过期扫描兜底回收。
        abandoned = self.manager.reserve("codex")
        self.assertEqual(self.manager.busy, 1)
        clock[0] += 6
        self.assertEqual(self.manager.status(abandoned["id"])["state"], "expired")
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.launch(abandoned["id"])["state"], "expired")
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())

    def test_restart_marks_active_session_interrupted_without_restarting_cli(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        second = AuthSessions(self.manager.root, factory=lambda backend: self.fail("CLI restarted"))
        try:
            state = second.status(session["id"])
            self.assertEqual(state["state"], "interrupted")
            self.assertNotIn("user_code", state)
        finally:
            second.close()

    def test_startup_tolerates_unwritable_state_directory(self):
        state = self.root / "readonly-state"
        state.mkdir()
        file = state / "auth-sessions.json"
        original = json.dumps({"old-session": {
            "id": "old-session", "backend": "codex", "provider": None, "state": "completed",
            "expires_at": time.time() + 600, "error": ""}})
        file.write_text(original)
        state.chmod(0o500)
        try:
            manager = AuthSessions(state, factory=self.factory, ttl=600)
        finally:
            state.chmod(0o700)
        self.addCleanup(manager.close)
        # 整理旧文件失败不能阻止启动：已加载会话仍可读，首个异常留给排查，原文件不被破坏。
        self.assertIsInstance(manager._persist_error, OSError)
        self.assertEqual([item["id"] for item in manager.snapshot()["sessions"]], ["old-session"])
        self.assertEqual(file.read_text(), original)
        # 目录恢复可写后，后续操作照常落盘，内存会话一并保留。
        session = manager.reserve("codex")
        self.assertEqual(session["state"], "starting")
        stored = json.loads(file.read_text())
        self.assertEqual(stored["old-session"]["state"], "completed")
        self.assertEqual(stored[session["id"]]["state"], "starting")

    def test_startup_without_state_file_and_unwritable_directory(self):
        state = self.root / "missing-state"
        state.mkdir()
        state.chmod(0o500)
        try:
            manager = AuthSessions(state, factory=self.factory, ttl=600)
        finally:
            state.chmod(0o700)
        self.addCleanup(manager.close)
        # 没有旧文件时不尝试写盘，构造与快照都不受只读目录影响。
        self.assertIsNone(manager._persist_error)
        self.assertEqual(manager.snapshot()["sessions"], [])

    def test_codex_login_merges_with_existing_auth_fields(self):
        # 用户在目标文件里手放的 OPENAI_API_KEY 或新版本未知字段不能被登录写回抹掉。
        path = self.root / "codex/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"OPENAI_API_KEY": "FIXTURE-KEY",
                                    "tokens": {"refresh_token": "OLD-REFRESH"},
                                    "extra_field": 1}))
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.wait_state(session, "completed")
        data = json.loads(path.read_text())
        self.assertEqual(data["OPENAI_API_KEY"], "FIXTURE-KEY")
        self.assertEqual(data["extra_field"], 1)
        self.assertEqual(data["tokens"]["refresh_token"], "SECRET-REFRESH")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_opencode_merges_only_selected_provider(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"other": {"type": "api", "key": "OTHER-KEY"}}))
        session = self.manager.start("opencode", "openai", inputs={"deploymentType": "github.com"})
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.wait_state(session, "completed")
        data = json.loads(path.read_text())
        self.assertEqual(data["other"]["key"], "OTHER-KEY")
        self.assertEqual(data["openai"]["type"], "oauth")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.created[0].inputs, {"deploymentType": "github.com"})

    def test_code_completion_is_private_and_validates_state(self):
        self.options["method"] = "code"
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        with self.assertRaisesRegex(AuthError, "invalid_auth_code"):
            self.manager.complete(session["id"], "")
        self.assertEqual(self.manager.complete(session["id"], " SECRET-CALLBACK ")["state"], "completing")
        self.gate.set()
        self.wait_state(session, "completed")
        self.assertEqual(self.created[0].code, "SECRET-CALLBACK")
        self.assertNotIn("SECRET-CALLBACK", self.manager.file.read_text())
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.complete(session["id"], "again")

    def test_specific_provider_key_and_logout_preserve_other_accounts(self):
        self.manager.set_api_key("opencode", "openai", " PRIVATE-KEY ")
        path = self.root / "data/opencode/auth.json"
        self.assertEqual(json.loads(path.read_text())["openai"]["key"], "PRIVATE-KEY")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE-KEY", json.dumps(self.manager.snapshot()))
        self.manager.logout("opencode", "openai")
        self.assertEqual(json.loads(path.read_text()), {})
        with self.assertRaisesRegex(AuthError, "unsupported_auth_method"):
            self.manager.set_api_key("opencode", "unknown", "key")

    def test_corrupt_legacy_auth_does_not_block_login_and_is_preserved(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt")
        self.assertEqual(self.manager.snapshot()["opencode"]["error"], "invalid_credentials")
        self.assertEqual(list(path.parent.glob("auth.json.invalid-*")), [])
        self.assertEqual(path.read_text(), "{corrupt")
        session = self.manager.start("opencode", "openai")
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.assertEqual(self.wait_state(session, "completed")["state"], "completed")
        self.assertEqual(json.loads(path.read_text())["openai"]["type"], "oauth")
        backups = list(path.parent.glob("auth.json.invalid-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{corrupt")
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.snapshot()["opencode"]["state"], "authenticated")

    def test_corrupt_legacy_auth_does_not_block_key_save_or_logout(self):
        path = self.root / "data/opencode/auth.json"
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt")
        self.manager.set_api_key("opencode", "openai", "PRIVATE-KEY")
        self.assertEqual(json.loads(path.read_text())["openai"]["key"], "PRIVATE-KEY")
        path.write_text("{corrupt-again")
        self.manager.logout("opencode", "openai")
        self.assertFalse(path.exists())
        self.assertEqual(sorted(item.read_text() for item in path.parent.glob("auth.json.invalid-*")),
                         ["{corrupt", "{corrupt-again"])
        self.assertEqual(self.manager.snapshot()["opencode"]["state"], "signed_out")

    def test_failure_codes_do_not_reflect_provider_error_or_secrets(self):
        self.options["failure"] = "device_auth_unavailable"
        session = self.manager.start("codex")
        result = self.wait_state(session, "failed")
        self.assertEqual(result["error"], "device_auth_unavailable")
        self.assertNotIn("user_code", result)

    def test_close_interrupts_active_sessions(self):
        session = self.manager.start("codex")
        self.wait_state(session, "awaiting_authorization")
        self.manager.close()
        self.assertEqual(self.manager.status(session["id"])["state"], "interrupted")
        self.assertTrue(self.created[0].closed)
        with self.assertRaisesRegex(AuthError, "service_closed"):
            self.manager.start("codex")

    def state_write_error(self, manager):
        """只让状态文件写入抛 OSError(28)；凭据等其他文件照常写。"""
        real = auth_sessions._write_json

        def write(path, value):
            if Path(path) == manager.file:
                raise OSError(28, "No space left on device")
            return real(path, value)

        return patch("adaptive_rsi.auth_sessions._write_json", side_effect=write)

    def test_failed_state_write_releases_reserved_session(self):
        manager = AuthSessions(self.root / "state", factory=self.factory, ttl=600)
        self.addCleanup(manager.close)
        with self.state_write_error(manager):
            with self.assertRaises(OSError):
                manager.reserve("codex")
        # 写盘失败的登记必须回滚：不留幽灵会话，不把后端锁成 auth_busy。
        self.assertEqual(manager.sessions, {})
        self.assertEqual(manager.busy, 0)
        session = manager.reserve("codex")
        self.assertEqual(session["state"], "starting")
        self.assertEqual(manager.busy, 1)
        self.assertEqual(json.loads(manager.file.read_text())[session["id"]]["state"], "starting")

    def test_failed_state_write_restores_previous_session_values(self):
        manager = AuthSessions(self.root / "state", factory=self.factory, ttl=600)
        self.addCleanup(manager.close)
        session = manager.reserve("codex")
        with self.state_write_error(manager):
            with self.assertRaises(OSError):
                manager.cancel(session["id"])
        # 拒绝写入时内存回滚到旧值，不能出现「内存已取消、磁盘仍 starting」。
        self.assertEqual(manager.status(session["id"])["state"], "starting")
        self.assertEqual(json.loads(manager.file.read_text())[session["id"]]["state"], "starting")
        self.assertEqual(manager.busy, 1)
        # 恢复可写后同一会话照常推进并落盘。
        self.assertEqual(manager.cancel(session["id"])["state"], "cancelled")
        self.assertEqual(json.loads(manager.file.read_text())[session["id"]]["state"], "cancelled")
        self.assertEqual(manager.snapshot()["sessions"][-1]["id"], session["id"])

    def test_failed_terminal_write_preserves_old_session_beyond_history_limit(self):
        manager = AuthSessions(self.root / 'old-state', factory=self.factory, ttl=600)
        self.addCleanup(manager.close)
        session = manager.reserve('codex')
        for index in range(31):
            key = 'history-' + str(index)
            manager.sessions[key] = {'id': key, 'backend': 'codex', 'state': 'completed',
                                     'expires_at': time.time() + 600, 'error': ''}
        with self.state_write_error(manager):
            manager._run(session['id'], 0, None)
        self.assertEqual(manager.status(session['id'])['state'], 'failed')
        self.assertEqual(manager.busy, 0)
        self.assertTrue(self.created[-1].closed)

    def test_worker_exit_releases_busy_session_after_state_write_failure(self):
        manager = AuthSessions(self.root / "worker-state", factory=self.factory, ttl=600)
        self.addCleanup(manager.close)
        session = manager.reserve("codex")
        with self.state_write_error(manager):
            try:
                manager._run(session["id"], 0, None)
            except OSError:
                pass
        self.assertEqual(manager.status(session["id"])["state"], "failed")
        self.assertEqual(manager.busy, 0)
        self.assertEqual(manager.workers, {})
        self.assertTrue(self.created[-1].closed)
        replacement = manager.reserve("codex")
        self.assertEqual(replacement["state"], "starting")
        self.assertEqual(json.loads(manager.file.read_text())[session["id"]]["state"], "failed")

    def test_failed_state_write_does_not_abort_close(self):
        manager = AuthSessions(self.root / "state", factory=self.factory, ttl=600)
        self.addCleanup(manager.close)
        session = manager.start("codex")
        self.wait_state(session, "awaiting_authorization", manager)
        worker = self.created[0]
        with self.state_write_error(manager):
            manager.close()
        # 持久化失败只记录，不影响 workers/线程回收，也不向 Runtime._close 抛出。
        self.assertTrue(manager.closed)
        self.assertTrue(worker.closed)
        self.assertEqual(manager.workers, {})
        self.assertEqual(manager.threads, set())
        self.assertIsInstance(manager._close_error, OSError)
        self.assertEqual(manager.status(session["id"])["state"], "interrupted")

    def test_reserve_registers_busy_session_before_any_worker_launch(self):
        session = self.manager.reserve("codex")
        self.assertEqual(session["state"], "starting")
        # 登记返回后 busy 立即计入 starting 会话，更新准入不能再 commit；
        # 慢的 worker 启动尚未发生，留待 launch 在临界区外执行。
        self.assertEqual(self.manager.busy, 1)
        self.assertEqual(self.created, [])
        self.manager.launch(session["id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.created:
            time.sleep(0.005)
        self.assertEqual(len(self.created), 1)
        self.wait_state(session, "awaiting_authorization")
        self.gate.set()
        self.wait_state(session, "completed")

    def test_reserve_sweeps_expired_session_status_could_not_reap(self):
        # status() 的过期回收走非阻塞 acquire；锁竞争时它直接返回陈旧的
        # ACTIVE 快照，sessions 里留下一条已过期但仍是 starting 的行。
        # 之后 reserve() 持锁时必须先扫掉它，否则会把已失效尝试误判为
        # auth_busy 并在整个 TTL 内拒登录。
        now = [1000.0]
        manager = AuthSessions(self.root / "state-expiry", factory=lambda backend: None,
                               ttl=60, now=lambda: now[0])
        self.addCleanup(manager.close)
        session = manager.reserve("codex")
        now[0] = 2000.0  # 已过 expires_at，但行仍未被任何人扫描
        held, release = threading.Event(), threading.Event()
        def hold():
            with manager.lock:
                held.set()
                release.wait(2)
        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            # 锁被占，status 走非阻塞分支直接返回未扫描的 ACTIVE 快照。
            self.assertEqual(manager.status(session["id"])["state"], "starting")
        finally:
            release.set()
            thread.join(2)
        # 锁空闲；reserve 持锁后必须先扫掉这条过期行再判 busy。
        replacement = manager.reserve("codex")
        self.assertEqual(replacement["state"], "starting")
        self.assertEqual(manager.status(session["id"])["state"], "expired")

    def test_thread_start_failure_does_not_leak_unstarted_thread(self):
        # Thread.start() 抛错时 _thread 必须从 self.threads 摘除；否则 close()
        # 会对从未启动的线程 join() 而抛 RuntimeError。
        def boom(self):
            raise RuntimeError("cannot start")
        with patch.object(threading.Thread, "start", boom):
            with self.assertRaises(RuntimeError):
                self.manager._thread(lambda: None)
        self.assertEqual(len(self.manager.threads), 0)
        self.manager.close()  # 必须幂等通过，不因泄漏线程抛错

    def test_launch_rejects_unknown_or_terminal_sessions(self):
        with self.assertRaisesRegex(AuthError, "unknown_session"):
            self.manager.launch("missing")
        session = self.manager.reserve("codex")
        self.manager.close()
        self.assertEqual(self.manager.status(session["id"])["state"], "interrupted")
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.launch(session["id"])

    def test_start_returns_expired_when_ttl_elapses_within_reserve_launch_window(self):
        # 确定性复现 3.9 上的时序竞态：expires_at 直接落在过去，
        # reserve 与 launch 之间不需要真实耗掉任何时间。
        self.manager.ttl = -1.0
        session = self.manager.start("codex")
        self.assertEqual(session["state"], "expired")
        self.assertEqual(session["error"], "auth_expired")
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.created, [])
        self.gate.set()
        self.assertFalse((self.root / "codex/auth.json").exists())

    def test_launch_still_rejects_interrupted_sessions(self):
        session = self.manager.reserve("codex")
        self.manager.close()
        self.assertEqual(self.manager.status(session["id"])["state"], "interrupted")
        with self.assertRaisesRegex(AuthError, "invalid_auth_state"):
            self.manager.launch(session["id"])

    def test_rejects_invalid_provider_method_and_inputs(self):
        for kwargs in ({"backend": "unknown"}, {"backend": []}, {"backend": "opencode", "provider": "../oops"},
                       {"backend": "codex", "method": True}, {"backend": "codex", "inputs": {"x": []}}):
            with self.assertRaises(AuthError):
                self.manager.start(**kwargs)

    def test_waiting_for_model_credentials_does_not_block_cancel_status_or_close(self):
        for backend, lock in (("codex", CODEX_AUTH_LOCK), ("opencode", AUTH_LOCK)):
            for operation in ("cancel", "status", "close"):
                with self.subTest(backend=backend, operation=operation):
                    self.manager.close()
                    self.manager = AuthSessions(self.root / (backend + operation), factory=self.factory, ttl=3)
                    self.gate.set()
                    completed, result = threading.Event(), []
                    with held_by_model(lock):
                        session = self.manager.start(backend, "openai" if backend == "opencode" else None)
                        self.wait_state(session, "completing")

                        def request():
                            if operation == "close":
                                self.manager.close()
                            else:
                                result.append(getattr(self.manager, operation)(session["id"]))
                            completed.set()
                        thread = threading.Thread(target=request)
                        thread.start()
                        self.assertTrue(completed.wait(0.5), "Account controls waited for the active model")
                        if operation == "status":
                            self.assertEqual(result[0]["state"], "completing")
                            self.manager.cancel(session["id"])
                    thread.join(2)
                    self.assertIn(self.manager.status(session["id"])["state"], {"cancelled", "interrupted"})
                    self.assertFalse((self.root / "codex/auth.json").exists())
                    self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_snapshot_and_mutations_never_wait_for_running_model_credential_lock(self):
        for backend, lock in (("codex", CODEX_AUTH_LOCK), ("opencode", AUTH_LOCK)):
            with self.subTest(backend=backend), held_by_model(lock):
                result, completed = [], threading.Event()
                thread = threading.Thread(target=lambda: (result.append(self.manager.snapshot()), completed.set()))
                thread.start()
                self.assertTrue(completed.wait(0.5), "Status waited for the active model")
                thread.join(1)
                self.assertEqual(result[0][backend]["state"], "signed_out")
                with self.assertRaisesRegex(AuthError, "auth_credentials_busy"):
                    self.manager.logout(backend, "openai" if backend == "opencode" else None)
                if backend == "opencode":
                    with self.assertRaisesRegex(AuthError, "auth_credentials_busy"):
                        self.manager.set_api_key("opencode", "openai", "PRIVATE-KEY")

    def test_delayed_provider_lookup_cannot_save_key_after_service_closes(self):
        discovered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def providers():
            discovered.set()
            release.wait(2)
            return [{"id": "openai", "methods": [{"type": "api"}]}]

        def save():
            try:
                self.manager.set_api_key("opencode", "openai", "PRIVATE-KEY")
            except AuthError as exc:
                errors.append(exc.code)
            finally:
                finished.set()
        worker = FakeAuth('opencode')
        self.created.append(worker)
        with patch.object(self.manager, 'factory', return_value=worker), patch.object(worker, 'providers', providers):
            thread = threading.Thread(target=save)
            thread.start()
            self.assertTrue(discovered.wait(1))
            self.manager.close()
            release.set()
            self.assertTrue(finished.wait(1))
            thread.join(1)
        self.assertEqual(errors, ["service_closed"])
        self.assertFalse((self.root / "data/opencode/auth.json").exists())

    def test_service_close_stops_active_provider_discovery_process(self):
        entered, released, finished = threading.Event(), threading.Event(), threading.Event()

        class Discovery(FakeAuth):
            def providers(worker):
                entered.set()
                released.wait(2)
                return super(Discovery, worker).providers()

            def close(worker):
                released.set()
                super(Discovery, worker).close()
        worker = Discovery("opencode")
        self.created.append(worker)
        with patch.object(self.manager, "factory", return_value=worker):
            thread = threading.Thread(target=lambda: (self.manager.providers(), finished.set()))
            thread.start()
            self.assertTrue(entered.wait(1))
            self.manager.close()
            self.assertTrue(finished.wait(0.5))
            thread.join(1)
        self.assertTrue(worker.closed)

    def test_status_expiry_closes_worker_outside_lock(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingClose(FakeAuth):
            def close(worker_self):
                entered.set()
                release.wait(2)
                super().close()
        created = []

        def factory(backend):
            worker = BlockingClose(backend)
            created.append(worker)
            return worker
        manager = AuthSessions(self.root / "expiry-lock", factory=factory, ttl=0.5)
        try:
            session = manager.start("opencode", "openai")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and manager.status(session["id"])["state"] == "starting":
                time.sleep(0.005)
            self.assertEqual(manager.status(session["id"])["state"], "awaiting_authorization")
            time.sleep(0.6)
            thread = threading.Thread(target=manager.status, args=(session["id"],))
            thread.start()
            self.assertTrue(entered.wait(1), "Expired worker was not closed")
            # 引用已在锁内摘除，慢的 close() 不再持有会话锁。
            self.assertNotIn(session["id"], manager.workers)
            self.assertTrue(manager.lock.acquire(timeout=0.5), "Expiry close blocked the session lock")
            manager.lock.release()
            release.set()
            thread.join(2)
            self.assertTrue(created[0].closed)
            self.assertEqual(manager.status(session["id"])["state"], "expired")
        finally:
            manager.close()
            for worker in created:
                worker.temp.cleanup()

    def test_providers_cache_hit_and_invalidation(self):
        manager = AuthSessions(self.root / "providers-cache", ttl=5)
        made = []

        def factory(backend, cancel=None):
            worker = FakeAuth(backend, self.gate)
            made.append(worker)
            return worker
        try:
            with patch.object(manager, "factory", side_effect=factory):
                first = manager.providers("opencode")
                second = manager.providers("opencode")
                self.assertEqual(first, second)
                self.assertEqual(len(made), 1, "Repeated discovery within TTL should hit the cache")
                session = manager.start("opencode", "openai")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and manager.status(session["id"])["state"] == "starting":
                    time.sleep(0.005)
                self.assertEqual(manager.status(session["id"])["state"], "awaiting_authorization")
                manager.providers("opencode")
                # 会话 worker 之外，start 失效后重新发现了一次。
                self.assertEqual(len(made), 3, "Cache should be invalidated after start")
                manager.cancel(session["id"])
                # 避免本机真实 opencode CLI 影响快照断言。
                with patch("adaptive_rsi.auth_sessions.shutil.which", return_value=None):
                    manager.set_api_key("opencode", "openai", "PRIVATE-KEY")
                    self.assertNotIn("opencode", manager.providers_cache)
                    manager.providers("opencode")
                    self.assertIn("opencode", manager.providers_cache)
                    manager.logout("opencode", "openai")
                    self.assertNotIn("opencode", manager.providers_cache)
        finally:
            manager.close()
            for worker in made:
                worker.temp.cleanup()
        self.assertEqual(manager.providers_cache, {}, "close should clear the providers cache")

    def test_oauth_completion_invalidates_caches_populated_during_authorization(self):
        self.manager.close()
        self.manager = AuthSessions(self.root / "oauth-completion-cache", ttl=5)
        with patch.object(self.manager, "factory", side_effect=lambda backend, cancel=None: self.factory(backend)), patch(
                "adaptive_rsi.auth_sessions.shutil.which", return_value=None):
            session = self.manager.start("opencode", "openai")
            self.wait_state(session, "awaiting_authorization")
            self.assertEqual(self.manager.snapshot()["opencode"]["state"], "signed_out")
            self.manager.providers("opencode")
            self.assertIn("opencode", self.manager.status_cache)
            self.assertIn("opencode", self.manager.providers_cache)

            self.gate.set()
            self.wait_state(session, "completed")
            self.assertNotIn("opencode", self.manager.status_cache)
            self.assertNotIn("opencode", self.manager.providers_cache)
            account = self.manager.snapshot()["opencode"]
            self.assertEqual(account["state"], "authenticated")
            self.assertEqual(account["authenticated_providers"], ["openai"])

    def test_inflight_discovery_cannot_repopulate_caches_after_auth_changes(self):
        for operation in ("save", "logout", "set_api_key", "start"):
            with self.subTest(operation=operation):
                self.manager.close()
                self.manager = AuthSessions(self.root / ("inflight-" + operation), ttl=5)
                self.gate = threading.Event()
                path = self.root / "data/opencode/auth.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"openai": {"type": "api", "key": "TEST-KEY"}}
                                          if operation == "logout" else {}))
                entered = {kind: threading.Event() for kind in ("status", "providers")}
                release = threading.Event()
                calls = {kind: 0 for kind in entered}
                errors = []

                def discover(kind):
                    calls[kind] += 1
                    connected = "openai" in json.loads(path.read_text())
                    if calls[kind] == 1:
                        entered[kind].set()
                        if not release.wait(3):
                            raise AssertionError("Discovery was not released")
                    return connected

                class Discovery(FakeAuth):
                    def providers(worker):
                        rows = super().providers()
                        rows[0]["connected"] = discover("providers")
                        return rows

                def factory(backend, cancel=None):
                    worker = Discovery(backend, self.gate)
                    self.created.append(worker)
                    return worker

                def lookup(kind):
                    try:
                        if kind == "status":
                            self.manager.snapshot()
                        else:
                            self.manager.providers("opencode")
                    except Exception as exc:
                        errors.append(exc)

                with patch.object(self.manager, "factory", side_effect=factory), patch(
                        "adaptive_rsi.auth_sessions.shutil.which", return_value=None), patch(
                        "adaptive_rsi.auth_sessions.auth_providers",
                        side_effect=lambda *args: ["openai"] if discover("status") else []):
                    if operation == "save":
                        session = self.manager.start("opencode", "openai")
                        self.wait_state(session, "awaiting_authorization")
                    threads = [threading.Thread(target=lookup, args=(kind,)) for kind in entered]
                    try:
                        for thread in threads:
                            thread.start()
                        for event in entered.values():
                            self.assertTrue(event.wait(1), "Discovery did not capture the old credentials")
                        if operation == "save":
                            self.gate.set()
                            self.wait_state(session, "completed")
                        elif operation == "logout":
                            self.manager.logout("opencode", "openai")
                        elif operation == "set_api_key":
                            self.manager.set_api_key("opencode", "openai", "TEST-NEW-KEY")
                        else:
                            session = self.manager.start("opencode", "openai")
                            self.wait_state(session, "awaiting_authorization")

                        expected = operation in {"save", "set_api_key"}
                        self.assertEqual(bool(self.manager.snapshot()["opencode"]["authenticated_providers"]), expected)
                        self.assertEqual(self.manager.providers("opencode")[0]["connected"], expected)
                        fresh_calls = dict(calls)
                        fresh_status = self.manager.status_cache["opencode"]
                        fresh_providers = self.manager.providers_cache["opencode"]
                        # 先发布新结果，再允许旧发现返回，验证旧结果不能覆盖新缓存。
                        release.set()
                        for thread in threads:
                            thread.join(1)
                            self.assertFalse(thread.is_alive())
                        self.assertEqual(errors, [])
                        self.assertIs(self.manager.status_cache["opencode"], fresh_status)
                        self.assertIs(self.manager.providers_cache["opencode"], fresh_providers)
                        self.assertEqual(bool(self.manager.snapshot()["opencode"]["authenticated_providers"]), expected)
                        self.assertEqual(self.manager.providers("opencode")[0]["connected"], expected)
                        self.assertEqual(calls, fresh_calls, "Fresh discoveries should remain cached")
                    finally:
                        release.set()
                        for thread in threads:
                            thread.join(2)
                        self.manager.close()

    def test_custom_factory_providers_skip_cache(self):
        first = self.manager.providers("opencode")
        second = self.manager.providers("opencode")
        self.assertEqual(first, second)
        self.assertEqual(len(self.created), 2, "Custom factory should never use the providers cache")
        self.assertEqual(self.manager.providers_cache, {})

    def test_v2_provider_connection_count_preserves_connected_semantics(self):
        worker = object.__new__(OpenCodeAuth)
        worker.container = False
        worker.capabilities = None
        worker._v2_integrations = lambda: [
            {"id": "missing", "name": "Missing", "methods": [{"type": "key"}]},
            {"id": "empty", "name": "Empty", "methods": [{"type": "key"}], "connections": []},
            {"id": "two", "name": "Two", "methods": [{"type": "key"}],
             "connections": [{"type": "credential", "id": "one"},
                             {"type": "credential", "id": "two"}]}]
        with patch("adaptive_rsi.auth_sessions.shutil.which", return_value=None):
            rows = {row["id"]: row for row in worker._v2_providers()}
        self.assertEqual((rows["missing"]["connected"], rows["missing"]["connection_count"]), (False, 0))
        self.assertEqual((rows["empty"]["connected"], rows["empty"]["connection_count"]), (False, 0))
        self.assertEqual((rows["two"]["connected"], rows["two"]["connection_count"]), (True, 2))


class OpenCodeServeTests(unittest.TestCase):
    def make_worker(self):
        worker = object.__new__(OpenCodeAuth)
        worker.container = False
        worker.cancel = threading.Event()
        worker.control = None
        worker.capabilities = OpenCodeCapabilities("1.19.3", (1, 19, 3))
        worker.base = "http://127.0.0.1:41001"
        return worker

    def test_auto_poll_retries_transient_errors_with_bounded_failures(self):
        worker = self.make_worker()
        worker.provider, worker.attempt_id = 'openai', 'fixture-attempt'
        for failures in (1, 9, 10):
            with self.subTest(failures=failures), \
                 patch.object(worker, 'http', side_effect=[AuthError('auth_protocol_error')] * failures +
                              [{'data': {'status': 'pending'}}, {'data': {'status': 'complete'}}]) as http, \
                 patch('adaptive_rsi.auth_sessions.time.sleep'):
                if failures < 10:
                    self.assertTrue(worker._v2_complete())
                    self.assertEqual(http.call_count, failures + 2)
                else:
                    with self.assertRaises(AuthError) as caught:
                        worker._v2_complete()
                    self.assertEqual(caught.exception.code, 'auth_protocol_error')
                    self.assertEqual(http.call_count, 10)

    def test_dead_process_gets_one_new_port_retry(self):
        worker = self.make_worker()
        launches, checks, alive = [], [], {"value": False}

        def launch(argv):
            launches.append(list(argv))
            # 第一次启动的进程立即死亡（如端口被抢占），重试后存活。
            alive["value"] = len(launches) > 1
        worker.launch = launch
        worker.alive = lambda: alive["value"]

        def http(method, path, body=None, timeout=25):
            checks.append(path)
            if len(checks) == 1:
                raise AuthError("auth_protocol_error")
            return {}
        worker.http = http

        worker._serve("fake-opencode", 41001)
        self.assertEqual(len(launches), 2, "Dead process should get exactly one retry on a new port")
        self.assertEqual(launches[0], ["fake-opencode", "serve", "--pure", "--log-level", "ERROR",
                                       "--hostname", "127.0.0.1", "--port", "41001"])
        self.assertNotEqual(launches[1][-1], "41001")
        self.assertTrue(worker.base.endswith(":" + launches[1][-1]))
        self.assertEqual(checks, ["/global/health", "/global/health"])

    def test_retry_happens_only_once(self):
        worker = self.make_worker()
        launches = []
        worker.launch = lambda argv: launches.append(list(argv))
        worker.alive = lambda: False

        def http(method, path, body=None, timeout=25):
            raise AuthError("auth_protocol_error")
        worker.http = http
        with self.assertRaisesRegex(AuthError, "auth_protocol_error"):
            worker._serve("fake-opencode", 41002)
        self.assertEqual(len(launches), 2, "Retry must not loop")

    def test_alive_process_polls_until_deadline_without_retry(self):
        worker = self.make_worker()
        launches, sleeps = [], []
        worker.launch = lambda argv: launches.append(list(argv))
        worker.alive = lambda: True

        def http(method, path, body=None, timeout=25):
            raise AuthError("auth_protocol_error")
        worker.http = http
        clock = {"now": 0.0}

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            with self.assertRaisesRegex(AuthError, "auth_protocol_error"):
                worker._serve("fake-opencode", 41003)
        self.assertEqual(len(launches), 1, "Alive process must never be relaunched")
        self.assertAlmostEqual(clock["now"], 25.0, delta=0.1)

    def test_v2_complete_polls_with_backoff_intervals(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        clock = {"now": 0.0}
        calls, sleeps = [], []

        def fake_http(method, path, body=None, timeout=25):
            calls.append((clock["now"], path))
            if len(calls) >= 6:
                return {"data": {"status": "complete"}}
            return {"data": {"status": "pending"}}
        worker.http = fake_http

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            self.assertTrue(worker._v2_complete(timeout=600))
        intervals = [round(calls[index + 1][0] - calls[index][0], 6) for index in range(len(calls) - 1)]
        # 阶梯退避 0.5/1/2/3 封顶；同样 9.5 秒内线性 0.2s 轮询约需 48 次请求。
        self.assertEqual(intervals, [0.5, 1.0, 2.0, 3.0, 3.0])
        self.assertEqual(len(calls), 6)

    def test_v2_complete_verifies_attempt_status_instead_of_trusting_empty_response(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        for status, expected in (("failed", "authorization_failed"), ("expired", "auth_expired")):
            with self.subTest(status=status), patch.object(worker, "http", side_effect=[None, {"data": {"status": status}}]):
                with self.assertRaises(AuthError) as caught:
                    worker._v2_complete("fixture-code", timeout=30)
                self.assertEqual(caught.exception.code, expected)
        with patch.object(worker, "http", side_effect=[None, {"data": {"status": "complete"}}]) as http:
            self.assertTrue(worker._v2_complete("fixture-code", timeout=30))
        self.assertEqual([call.args[0] for call in http.call_args_list], ["POST", "GET"])
        with patch.object(worker, "http", side_effect=[{"error": "bad code"}]):
            with self.assertRaises(AuthError) as caught:
                worker._v2_complete("fixture-code", timeout=30)
            self.assertEqual(caught.exception.code, "authorization_failed")

    def test_v2_complete_checks_attempt_after_post_protocol_error(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        with patch.object(worker, "http", side_effect=[AuthError("auth_protocol_error"),
                                                       {"data": {"status": "complete"}}]) as http:
            self.assertTrue(worker._v2_complete("fixture-code", timeout=30))
        self.assertEqual([call.args[0] for call in http.call_args_list], ["POST", "GET"])
        for status, expected in (("failed", "authorization_failed"), ("expired", "auth_expired"),
                                 ("unknown", "auth_protocol_error")):
            with self.subTest(status=status), patch.object(worker, "http", side_effect=[
                    AuthError("auth_protocol_error"), {"data": {"status": status}}]):
                with self.assertRaises(AuthError) as caught:
                    worker._v2_complete("fixture-code", timeout=30)
                self.assertEqual(caught.exception.code, expected)
        with patch.object(worker, "http", side_effect=[AuthError("auth_protocol_error"),
                                                       AuthError("auth_protocol_error")]):
            with self.assertRaises(AuthError) as caught:
                worker._v2_complete("fixture-code", timeout=30)
            self.assertEqual(caught.exception.code, "auth_protocol_error")

    def test_v2_complete_polls_pending_attempt_after_post_protocol_error(self):
        """POST 失败后 GET 到 pending 不能直接失败：验证码可能已被消费，attempt 仍在处理。"""
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        clock = {"now": 0.0}
        calls = []

        def fake_http(method, path, body=None, timeout=25):
            calls.append((clock["now"], method))
            if method == "POST":
                raise AuthError("auth_protocol_error")
            return {"data": {"status": "complete" if len(calls) >= 4 else "pending"}}

        def fake_sleep(seconds):
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        worker.http = fake_http
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            self.assertTrue(worker._v2_complete("fixture-code", timeout=30))
        # 验证码只提交一次：POST 失败后只轮询既有 attempt，退避一次后拿到 complete。
        self.assertEqual([method for _, method in calls], ["POST", "GET", "GET", "GET"])
        self.assertEqual(calls[-1][0], 0.2)

        # pending 一直持续时在 deadline 结束为 auth_timeout，且绝不重发验证码。
        calls.clear()
        clock["now"] = 0.0

        def pending_http(method, path, body=None, timeout=25):
            calls.append((clock["now"], method))
            if method == "POST":
                raise AuthError("auth_protocol_error")
            return {"data": {"status": "pending"}}
        worker.http = pending_http
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            with self.assertRaises(AuthError) as caught:
                worker._v2_complete("fixture-code", timeout=30)
        self.assertEqual(caught.exception.code, "auth_timeout")
        self.assertEqual([method for _, method in calls].count("POST"), 1)
        self.assertAlmostEqual(clock["now"], 10.0, places=6)

        # POST 错误体已经给出 pending 时不再反查一次，直接按空响应口径轮询。
        calls.clear()
        clock["now"] = 0.0

        def detail_pending_http(method, path, body=None, timeout=25):
            calls.append((clock["now"], method))
            if method == "POST":
                raise AuthError("auth_protocol_error", detail={"attempt_status": "pending"})
            return {"data": {"status": "complete"}}
        worker.http = detail_pending_http
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            self.assertTrue(worker._v2_complete("fixture-code", timeout=30))
        self.assertEqual([method for _, method in calls], ["POST", "GET"])

    def test_http_error_keeps_bounded_response_detail(self):
        worker = self.make_worker()
        worker.header = "Basic fixture"
        error = urllib.error.HTTPError(
            worker.base + "/complete", 400, "Bad Request", {},
            io.BytesIO(b'{"error":"invalid_grant","message":"code rejected",'
                       b'"data":{"status":"failed"}}'))
        opener = types.SimpleNamespace(open=lambda request, timeout=None: (_ for _ in ()).throw(error))
        with patch("adaptive_rsi.auth_sessions.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(AuthError) as caught:
                worker.http("POST", "/complete", {"code": "fixture-code"})
        self.assertEqual(caught.exception.code, "auth_protocol_error")
        self.assertEqual(caught.exception.detail, {"http_status": 400, "error": "invalid_grant",
                                                   "message": "code rejected", "attempt_status": "failed"})

    def test_v2_complete_uses_error_body_attempt_status_without_fallback_get(self):
        worker = self.make_worker()
        worker.capabilities = OpenCodeCapabilities("2.1.3", (2, 1, 3))
        worker.provider, worker.attempt_id = "openai", "attempt-1"
        worker.header = "Basic fixture"
        for status, expected in (("failed", "authorization_failed"), ("expired", "auth_expired")):
            calls = []

            def fail(request, timeout=None):
                calls.append((request.get_method(), request.full_url))
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                             io.BytesIO(json.dumps({"data": {"status": status}}).encode()))
            opener = types.SimpleNamespace(open=fail)
            with self.subTest(status=status), \
                 patch("adaptive_rsi.auth_sessions.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(AuthError) as caught:
                    worker.complete("fixture-code", timeout=30)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.detail["attempt_status"], status)
            self.assertEqual([method for method, _ in calls], ["POST"],
                             "错误体已给出 attempt 终态时不得再发兜底 GET")
        calls = []

        def complete(request, timeout=None):
            calls.append(request.get_method())
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"data":{"status":"complete"}}'))
        opener = types.SimpleNamespace(open=complete)
        with patch("adaptive_rsi.auth_sessions.urllib.request.build_opener", return_value=opener):
            self.assertTrue(worker.complete("fixture-code", timeout=30))
        self.assertEqual(calls, ["POST"])

    def test_error_detail_never_enters_api_response(self):
        from adaptive_rsi.server import Handler
        handler = object.__new__(Handler)
        responses = []
        handler.respond = lambda payload, status=200: responses.append((payload, status))
        handler.api_error(AuthError("auth_protocol_error", detail={
            "http_status": 400, "error": "invalid_grant", "message": "code rejected"}))
        self.assertEqual(responses, [({"error": "Request failed: auth_protocol_error",
                                       "error_code": "auth_protocol_error"}, 400)])
        self.assertNotIn("code rejected", json.dumps(responses))

    def test_v2_complete_retries_transient_status_lookup_after_code(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        transient = AuthError("auth_protocol_error")
        with patch.object(worker, "http", side_effect=[None, transient, {"data": {"status": "pending"}},
                                                       {"data": {"status": "complete"}}]), \
             patch("adaptive_rsi.auth_sessions.time.sleep"):
            self.assertTrue(worker._v2_complete("fixture-code", timeout=30))
        with patch.object(worker, "http", side_effect=[None] + [transient] * 3), \
             patch("adaptive_rsi.auth_sessions.time.sleep"):
            with self.assertRaises(AuthError) as caught:
                worker._v2_complete("fixture-code", timeout=30)
            self.assertEqual(caught.exception.code, "auth_protocol_error")

    def test_v2_complete_times_out_at_deadline_with_backoff(self):
        worker = object.__new__(OpenCodeAuth)
        worker.provider = "openai"
        worker.attempt_id = "attempt-1"
        clock = {"now": 0.0}
        calls, sleeps = [], []

        def fake_http(method, path, body=None, timeout=25):
            calls.append(clock["now"])
            return {"data": {"status": "pending"}}
        worker.http = fake_http

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"], sleep=fake_sleep)
        with patch("adaptive_rsi.auth_sessions.time", fake_time):
            with self.assertRaisesRegex(AuthError, "auth_timeout"):
                worker._v2_complete(timeout=10)
        # 10 秒窗口内 6 次请求（线性 0.2s 方案约 50 次），最后一次 sleep 不越过截止时间。
        self.assertEqual(len(calls), 6)
        self.assertEqual(sleeps[-1], 0.5)
        self.assertAlmostEqual(clock["now"], 10.0, places=6)


class RealAuthChecks:
    def make(self, cls):
        return cls()

    def test_codex_account_read_is_isolated_and_never_logs_in(self):
        worker = self.make(CodexAuth)
        try:
            result = worker.request("account/read", {"refreshToken": False})
            self.assertIsNone(result["account"])
            self.assertFalse(worker.credentials.exists())
            self.assertEqual(worker.environment["CODEX_HOME"], str(worker.home / ".codex"))
        finally:
            worker.close()

    def test_opencode_lists_builtin_methods_without_authorization(self):
        worker = self.make(OpenCodeAuth)
        try:
            rows = worker.providers()
            openai = next(row for row in rows if row["id"] == "openai")
            self.assertTrue(any(item["supported"] and "headless" in item["label"] for item in openai["methods"]))
            self.assertTrue(any(not item["supported"] and "browser" in item["label"] for item in openai["methods"]))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(worker.base + "/provider/auth")
            self.assertEqual(caught.exception.code, 401)
            self.assertFalse(worker.credentials.exists())
            self.assertEqual(worker.environment["OPENCODE_PURE"], "true")
            self.assertEqual(worker.environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
            self.assertNotIn("OPENCODE_CONFIG", worker.environment)
        finally:
            worker.close()


@unittest.skipUnless(os.environ.get("RSI_AUTH_CLI_TESTS") == "1", "Enable RSI_AUTH_CLI_TESTS=1 for read-only CLI checks")
class RealAuthCLITests(RealAuthChecks, unittest.TestCase):
    pass


@unittest.skipUnless(os.environ.get("RSI_AUTH_DOCKER_TESTS") == "1", "Enable RSI_AUTH_DOCKER_TESTS=1 for read-only Docker checks")
class RealAuthDockerTests(RealAuthChecks, unittest.TestCase):
    def make(self, cls):
        return cls({"sandbox": {"backend": "docker", "worker_image": os.environ.get("RSI_WORKER_IMAGE", "adaptive-rsi-worker:local")}})

    def test_parent_crash_removes_container_and_temporary_credentials(self):
        script = """import json,os
from adaptive_rsi.auth_sessions import OpenCodeAuth
worker=OpenCodeAuth({'sandbox':{'backend':'docker','worker_image':os.environ.get('RSI_WORKER_IMAGE','adaptive-rsi-worker:local')}})
print(json.dumps({'home':str(worker.home),'container':worker.container_name}),flush=True)
os._exit(0)
"""
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            details = json.loads(process.stdout.readline())
            process.wait(timeout=35)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                inspection = subprocess.run(["docker", "container", "inspect", details["container"]],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                if inspection.returncode and not Path(details["home"]).exists():
                    break
                time.sleep(0.1)
            self.assertNotEqual(inspection.returncode, 0)
            self.assertFalse(Path(details["home"]).exists())
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


_PROTOCOL_FIXTURE = r'''#!/usr/bin/env python3
import base64,json,os,pathlib,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
if sys.argv[1:]==['--version']:
 print('opencode v1.19.3')
 raise SystemExit(0)
home=pathlib.Path(os.environ['HOME'])
assert os.environ['CODEX_HOME']==str(home/'.codex')
assert 'OPENAI_API_KEY' not in os.environ
if pathlib.Path(sys.argv[0]).name=='codex':
 for line in sys.stdin:
  msg=json.loads(line)
  method=msg['method']
  if method=='initialized': continue
  result={}
  if method=='account/login/start':
   assert msg['params']=={'type':'chatgptDeviceCode'}
   result={'type':'chatgptDeviceCode','loginId':'fixture-login','verificationUrl':'https://example.test/device','userCode':'PRIVATE-USER-CODE'}
   print(json.dumps({'id':msg['id'],'result':result}),flush=True)
   (home/'.codex/auth.json').write_text(json.dumps({'tokens':{'refresh_token':'FIXTURE-REFRESH','access_token':'FIXTURE-ACCESS'}}))
   print(json.dumps({'method':'account/login/completed','params':{'loginId':'unrelated','success':False}}),flush=True)
   print(json.dumps({'method':'account/login/completed','params':{'loginId':'fixture-login','success':True}}),flush=True)
   continue
  if method=='account/read': result={'account':{'type':'chatgpt'}}
  print(json.dumps({'id':msg['id'],'result':result}),flush=True)
else:
 assert os.environ['OPENCODE_PURE']=='true'
 assert os.environ['OPENCODE_DISABLE_PROJECT_CONFIG']=='true'
 assert 'OPENCODE_CONFIG' not in os.environ
 assert '--pure' in sys.argv
 assert sys.argv[1]=='serve'
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args): pass
  def request(self):
   expected='Basic '+base64.b64encode((os.environ['OPENCODE_SERVER_USERNAME']+':'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
   if self.headers.get('Authorization')!=expected:
    self.send_response(401);self.end_headers();return
   value=True
   if self.path=='/global/health': value={'healthy':True,'python':list(sys.version_info[:2])}
   if self.path=='/provider/auth': value={'openai':[{'type':'oauth','label':'ChatGPT (headless)'},{'type':'api','label':'API key'}]}
   if self.command=='POST':
    body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
    assert body['method']==0
    if self.path.endswith('/authorize'): value={'url':'https://example.test/device','method':'auto','instructions':'Enter code PRIVATE-USER-CODE'}
    if self.path.endswith('/callback'):
     path=home/'.local/share/opencode/auth.json';path.parent.mkdir(parents=True,exist_ok=True)
     path.write_text(json.dumps({'openai':{'type':'oauth','refresh':'FIXTURE-REFRESH','access':'FIXTURE-ACCESS','expires':123}}))
   self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
  do_GET=request
  do_POST=request
 class LoopbackServer(HTTPServer):
  def server_bind(self):
   TCPServer.server_bind(self)
   self.server_name='localhost'
   self.server_port=self.server_address[1]
 port=int(sys.argv[sys.argv.index('--port')+1])
 LoopbackServer(('127.0.0.1',port),Handler).serve_forever()
'''


_PROTOCOL_V2_FIXTURE = r'''#!/usr/bin/env python3
import base64,json,os,pathlib,sys
from http.server import BaseHTTPRequestHandler,HTTPServer
from socketserver import TCPServer
args=sys.argv[1:]
if args==['--version']:
 print('opencode v2.1.3')
 raise SystemExit(0)
home=pathlib.Path(os.environ['HOME'])
data_home=pathlib.Path(os.environ['XDG_DATA_HOME'])
auth_file=data_home/'opencode/fake-auth.json'
if args[:1]==['api']:
 assert args==['api','GET','/api/model','--standalone']
 print(json.dumps({'location':{},'data':[{'id':'fixture','modelID':'fixture','providerID':'opencode-go','name':'Fixture','capabilities':{'tools':True},'variants':[{'id':'low'}]}]}))
 raise SystemExit(0)
def credentials():
 if not auth_file.is_file(): return []
 value=json.loads(auth_file.read_text())
 return value if isinstance(value,list) else []
def save(rows):
 auth_file.parent.mkdir(parents=True,exist_ok=True)
 auth_file.write_text(json.dumps(rows));auth_file.chmod(0o600)
if args[:2]==['auth','list']:
 rows=[]
 for item in credentials():
  rows.append({'id':item['integrationID'],'name':item['integrationID'],'connections':[{'type':'credential','id':item['id'],'label':item.get('label',''), 'method':item.get('value',{}).get('type','key')}]})
 print(json.dumps(rows));raise SystemExit(0)
if args[:2]==['auth','export']:
 target=args[3] if len(args)>3 else None
 rows=[item for item in credentials() if not target or item['integrationID']==target]
 print(json.dumps(rows));raise SystemExit(0)
if args[:2]==['auth','import']:
 source=pathlib.Path(args[3])
 incoming=json.loads(source.read_text())
 current={item['integrationID']:item for item in credentials()}
 for item in incoming: current[item['integrationID']]=item
 save(list(current.values()));raise SystemExit(0)
if args[:2]==['auth','logout']:
 target=args[3]
 save([item for item in credentials() if item['integrationID']!=target]);raise SystemExit(0)
assert args[0]=='serve' and '--pure' not in args and '--log-level' not in args
assert os.environ['OPENCODE_PURE']=='true'
assert os.environ['OPENCODE_SERVER_USERNAME']=='opencode'
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def request(self):
  expected='Basic '+base64.b64encode((os.environ['OPENCODE_SERVER_USERNAME']+':'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
  if self.headers.get('Authorization')!=expected:
   self.send_response(401);self.end_headers();return
  value={}
  if self.path=='/api/info': value={'status':'ok'}
  if self.path=='/api/integration':
   status=[]
   for item in credentials():
    status.append({'type':'credential','id':item['id'],'label':item.get('label',''), 'method':item.get('value',{}).get('type','key')})
   value={'location':{},'data':[
    {'id':'openai','name':'OpenAI','methods':[{'type':'key'},{'id':'chatgpt-browser','type':'oauth','label':'Codex browser (legacy)'},{'id':'chatgpt-headless','type':'oauth','label':'Codex device code'}],'connections':[]},
    {'id':'opencode-go','name':'OpenCode Go','methods':[{'type':'key'}],'connections':[item for item in status if item['id']=='v2-key']}]}
  if self.path=='/api/integration/openai/connect/oauth' and self.command=='POST':
   body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
   assert body['methodID']=='chatgpt-headless'
   value={'location':{},'data':{'attemptID':'attempt-1','url':'https://example.test/device','instructions':'Complete in browser','mode':'auto','time':{'created':1,'expires':9999999999}}}
  if self.path=='/api/integration/openai/connect/oauth/attempt-1' and self.command=='GET':
   rows=credentials()
   if not any(item['integrationID']=='openai' for item in rows):
    rows.append({'id':'credential-oauth','integrationID':'openai','label':'OAuth','active':True,'value':{'type':'oauth','refresh':'V2-REFRESH','access':'V2-ACCESS'}})
    save(rows)
   value={'location':{},'data':{'status':'complete','time':{'created':1,'expires':9999999999}}}
  if self.path=='/api/integration/opencode-go/connect/key' and self.command=='POST':
   body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
   rows=credentials()
   rows.append({'id':'v2-key','integrationID':'opencode-go','label':'API key','active':True,'value':{'type':'key','key':body['key']}})
   save(rows)
   self.send_response(204);self.end_headers();return
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
 do_GET=request
 do_POST=request
class LoopbackServer(HTTPServer):
 def server_bind(self):
  TCPServer.server_bind(self);self.server_name='localhost';self.server_port=self.server_address[1]
port=int(args[args.index('--port')+1])
LoopbackServer(('127.0.0.1',port),Handler).serve_forever()
'''


class AuthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for binary in ("codex", "opencode"):
            script = self.root / binary
            script.write_text(_PROTOCOL_FIXTURE.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
            script.chmod(0o700)
        self.original_which = __import__("shutil").which
        self.which = patch("adaptive_rsi.auth_sessions.shutil.which", side_effect=lambda name:
                           str(self.root / name) if name in {"codex", "opencode"} else self.original_which(name))
        self.which.start()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "saved-codex"),
            "XDG_DATA_HOME": str(self.root / "saved-data"), "OPENCODE_CONFIG": "/untrusted/project/plugin.json",
            "OPENAI_API_KEY": "DO-NOT-INHERIT"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.which.stop()
        self.temp.cleanup()

    def test_codex_protocol_uses_device_code_and_matches_completion_id(self):
        manager = AuthSessions(self.root / "state")
        try:
            session = manager.start("codex")
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                result = manager.status(session["id"])
                if result["state"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "completed", result)
            data = json.loads((self.root / "saved-codex/auth.json").read_text())
            self.assertEqual(data["tokens"]["refresh_token"], "FIXTURE-REFRESH")
        finally:
            manager.close()

    def test_codex_rejects_unsupported_method_and_inputs_before_device_code(self):
        worker = CodexAuth()
        try:
            for kwargs in ({"method": 1}, {"method": 7}, {"inputs": {"k": "v"}},
                           {"method": 1, "inputs": {"k": "v"}}):
                with self.subTest(kwargs=kwargs), self.assertRaises(AuthError) as caught:
                    worker.start(**kwargs)
                self.assertEqual(caught.exception.code, "unsupported_auth_method")
            fields = worker.start()
            self.assertEqual(fields["verification_url"], "https://example.test/device")
            self.assertEqual(fields["user_code"], "PRIVATE-USER-CODE")
            self.assertEqual(fields["method"], "auto")
        finally:
            worker.close()

    def test_codex_manager_reports_unsupported_method_and_inputs_as_session_error(self):
        # API 客户端拿到的是会话终态错误，不能静默回落到 device code 流程。
        for index, kwargs in enumerate(({"method": 1}, {"inputs": {"k": "v"}})):
            manager = AuthSessions(self.root / f"state-unsupported-{index}")
            try:
                session = manager.start("codex", **kwargs)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    result = manager.status(session["id"])
                    if result["state"] in {"failed", "completed", "expired"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(result["state"], "failed", result)
                self.assertEqual(result["error"], "unsupported_auth_method")
            finally:
                manager.close()

    def test_opencode_http_protocol_is_authenticated_and_persists_callback(self):
        worker = OpenCodeAuth()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(worker.base + "/provider/auth")
            self.assertEqual(caught.exception.code, 401)
            rows = worker.providers()
            self.assertEqual(rows[0]["id"], "openai")
            self.assertTrue(rows[0]["methods"][0]["supported"])
            fields = worker.start("openai", 0)
            self.assertEqual(fields["method"], "auto")
            self.assertTrue(worker.complete())
            self.assertEqual(json.loads(worker.credentials.read_text())["openai"]["refresh"], "FIXTURE-REFRESH")
        finally:
            home = worker.home
            worker.close()
        self.assertFalse(home.exists())

    def test_opencode_v1_session_save_and_api_key_use_auth_json_not_v2_integration_api(self):
        manager = AuthSessions(self.root / "v1-state")
        try:
            session = manager.start("opencode", "openai", 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = manager.status(session["id"])
                if result["state"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(result["state"], "completed", result)
            path = self.root / "saved-data/opencode/auth.json"
            self.assertEqual(json.loads(path.read_text())["openai"]["refresh"], "FIXTURE-REFRESH")
            self.assertFalse((self.root / "saved-data/opencode/fake-auth.json").exists())

            manager.set_api_key("opencode", "openai", "V1-PRIVATE-KEY")
            self.assertEqual(json.loads(path.read_text())["openai"], {"type": "api", "key": "V1-PRIVATE-KEY"})
            self.assertFalse((self.root / "saved-data/opencode/fake-auth.json").exists())
        finally:
            manager.close()

    def test_loopback_fixture_skips_reverse_dns_and_uses_current_python(self):
        script = self.root / "opencode"
        fixture = script.read_text().replace(
            "from socketserver import TCPServer",
            "from socketserver import TCPServer\nimport socket\n"
            "def forbidden_lookup(*args): raise OSError('reverse DNS unavailable')\n"
            "socket.getfqdn=forbidden_lookup", 1)
        script.write_text(fixture)
        worker = OpenCodeAuth()
        try:
            self.assertEqual(worker.http("GET", "/global/health")["python"], list(sys.version_info[:2]))
            self.assertEqual(worker.providers()[0]["id"], "openai")
        finally:
            worker.close()

    def test_invalid_protocol_input_never_starts_authorization(self):
        worker = OpenCodeAuth()
        try:
            with self.assertRaisesRegex(AuthError, "invalid_auth_inputs"):
                worker.start("openai", 0, {"unknown": "PRIVATE"})
            with self.assertRaisesRegex(AuthError, "unsupported_auth_method"):
                worker.start("openai", 1)
            self.assertFalse(worker.credentials.exists())
        finally:
            worker.close()

    def test_parent_crash_removes_isolated_home_and_child_server(self):
        script = """import json,os,shutil
from adaptive_rsi.auth_sessions import OpenCodeAuth
original=shutil.which
shutil.which=lambda name: %r if name=='opencode' else original(name)
worker=OpenCodeAuth()
print(json.dumps({'home':str(worker.home),'base':worker.base}),flush=True)
os._exit(0)
""" % str(self.root / "opencode")
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            details = json.loads(process.stdout.readline())
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while Path(details["home"]).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Path(details["home"]).exists())
            with self.assertRaises(urllib.error.URLError):
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(details["base"] + "/global/health", timeout=1)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


class OpenCodeV2AuthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root / "opencode"
        script.write_text(_PROTOCOL_V2_FIXTURE.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
        script.chmod(0o700)
        self.original_which = __import__("shutil").which
        self.which = patch("adaptive_rsi.auth_sessions.shutil.which", side_effect=lambda name:
                           str(script) if name == "opencode" else self.original_which(name))
        self.which.start()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "saved-codex"),
            "XDG_DATA_HOME": str(self.root / "saved-data"),
            "XDG_CONFIG_HOME": str(self.root / "saved-config"),
            "XDG_CACHE_HOME": str(self.root / "saved-cache")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.which.stop()
        self.temp.cleanup()

    def wait_state(self, manager, session, state):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = manager.status(session["id"])
            if result["state"] in {"completed", "failed", "expired"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["state"], state, result)
        return result

    def test_v2_oauth_key_snapshot_and_logout_use_integration_api_and_import(self):
        manager = AuthSessions(self.root / "state")
        try:
            initial = manager.snapshot()["opencode"]
            self.assertEqual(initial["version"], "2.1.3")
            self.assertEqual(initial["state"], "signed_out")
            providers = manager.providers("opencode")
            openai = next(row for row in providers if row["id"] == "openai")
            self.assertFalse(next(method for method in openai["methods"] if "browser" in method["label"])["supported"])
            oauth = next(method for method in openai["methods"] if method["type"] == "oauth" and method["supported"])
            session = manager.start("opencode", "openai", oauth["index"])
            result = self.wait_state(manager, session, "completed")
            self.assertNotIn("verification_url", result)
            stored = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            self.assertEqual(stored[0]["integrationID"], "openai")
            self.assertEqual(manager.snapshot()["opencode"]["authenticated_providers"], ["openai"])

            manager.set_api_key("opencode", "opencode-go", "V2-PRIVATE-KEY")
            stored = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            key = next(item for item in stored if item["integrationID"] == "opencode-go")
            self.assertEqual(key["value"], {"type": "key", "key": "V2-PRIVATE-KEY"})
            self.assertEqual(manager.snapshot()["opencode"]["authenticated_providers"], ["openai", "opencode-go"])
            connected = next(row for row in manager.providers("opencode") if row["id"] == "opencode-go")
            self.assertTrue(connected["connected"])

            manager.logout("opencode", "openai")
            remaining = json.loads((self.root / "saved-data/opencode/fake-auth.json").read_text())
            self.assertEqual([item["integrationID"] for item in remaining], ["opencode-go"])
        finally:
            manager.close()

    def test_v2_worker_install_writes_legacy_auth_when_host_cli_is_v1(self):
        worker = object.__new__(OpenCodeAuth)
        worker.home = self.root / "worker-home"
        credentials = [{"id": "credential", "integrationID": "provider", "label": "Provider",
                        "active": True, "value": {"type": "oauth", "refresh": "private-refresh"}}]
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "fallback-data")}), patch(
                "adaptive_rsi.auth_sessions.shutil.which", return_value="/fake/opencode"), patch(
                "adaptive_rsi.auth_sessions.detect_opencode",
                return_value=OpenCodeCapabilities("1.19.3", (1, 19, 3))), patch(
                "adaptive_rsi.auth_sessions.import_auth") as imported:
            worker.install_credential("provider", credentials)
            stored = json.loads((self.root / "fallback-data/opencode/auth.json").read_text())
            exported = load_auth_export()
        imported.assert_not_called()
        self.assertEqual(stored["provider"]["refresh"], "private-refresh")
        self.assertEqual(exported, [])
