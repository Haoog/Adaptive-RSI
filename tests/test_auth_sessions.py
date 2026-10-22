import json
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from adaptive_rsi.auth_sessions import AuthError, AuthSessions, CodexAuth, OpenCodeAuth
from adaptive_rsi.cli_worker import CODEX_AUTH_LOCK
from adaptive_rsi.opencode import AUTH_LOCK


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
    def __init__(self, backend, gate=None, failure=None, method="auto"):
        self.backend = backend
        self.gate = gate or threading.Event()
        self.failure = failure
        self.method = method
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

    def wait_state(self, session, state):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.manager.status(session["id"])
            if result["state"] == state:
                return result
            time.sleep(0.005)
        self.fail("Expected " + state + ": " + repr(result))

    def test_initialization_never_starts_cli(self):
        self.assertEqual(self.created, [])
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.snapshot()["codex"]["state"], "signed_out")

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

    def test_busy_expires_without_browser_polling(self):
        self.manager.ttl = 0.03
        session = self.manager.start("codex")
        time.sleep(0.05)
        self.assertEqual(self.manager.busy, 0)
        self.assertEqual(self.manager.status(session["id"])["state"], "expired")
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

        def providers(backend):
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
        with patch.object(self.manager, "providers", providers):
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
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args): pass
  def request(self):
   expected='Basic '+base64.b64encode((os.environ['OPENCODE_SERVER_USERNAME']+':'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
   if self.headers.get('Authorization')!=expected:
    self.send_response(401);self.end_headers();return
   value=True
   if self.path=='/global/health': value={'healthy':True}
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
 port=int(sys.argv[sys.argv.index('--port')+1])
 HTTPServer(('127.0.0.1',port),Handler).serve_forever()
'''


class AuthProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for binary in ("codex", "opencode"):
            script = self.root / binary
            script.write_text(_PROTOCOL_FIXTURE)
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
