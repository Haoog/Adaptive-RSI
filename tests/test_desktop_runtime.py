import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.request
from pathlib import Path

from adaptive_rsi.desktop import existing_session
from adaptive_rsi.store import Store


class DesktopRuntimeTest(unittest.TestCase):
    def test_owned_service_pauses_on_reopen_and_attachment_never_stops_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root)
            store.execute('INSERT INTO projects(id,name,path,config,paused) VALUES (?,?,?,?,0)', ('p','Test',str(root/'project'),json.dumps({'sources':{'inspection':False,'issues':False}})))
            def launch():
                process=subprocess.Popen([sys.executable,'-m','adaptive_rsi','--state-dir',directory,'desktop-runtime'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                self.addCleanup(lambda: process.poll() is None and process.kill())
                ready,_,_=select.select([process.stdout],[],[],15)
                self.assertTrue(ready,'No desktop handshake')
                line=process.stdout.readline()
                self.assertTrue(line,process.stderr.read() if process.poll() is not None else 'Empty handshake')
                return process,json.loads(line)
            def health(session):
                request=urllib.request.Request(session['local_origin']+'/api/health',headers={'Cookie':session['cookie']+'='+session['token']})
                with urllib.request.urlopen(request,timeout=3) as response:
                    return json.load(response)
            first,session=launch()
            self.assertTrue(session['owned'])
            self.assertEqual(store.one('SELECT paused FROM projects')['paused'],1)
            self.assertEqual(health(session)['api_version'],1)
            store.execute('UPDATE projects SET paused=0')
            second,attached=launch()
            self.assertFalse(attached['owned'])
            self.assertEqual(store.one('SELECT paused FROM projects')['paused'],0)
            second.stdin.close();self.assertEqual(second.wait(timeout=10),0)
            self.assertEqual(health(session)['pid'],first.pid)
            first.stdin.close();self.assertEqual(first.wait(timeout=15),0)
            self.assertFalse((root/'server.json').exists())
            third,reopened=launch()
            self.assertTrue(reopened['owned'])
            self.assertEqual(store.one('SELECT paused FROM projects')['paused'],1)
            third.stdin.close();self.assertEqual(third.wait(timeout=15),0)
            for process in [first,second,third]:
                process.stdout.close();process.stderr.close()
            self.assertEqual(store.one('SELECT count(*) AS n FROM calls')['n'],0)

    def test_desktop_updates_cannot_replace_bundled_engine(self):
        from adaptive_rsi.updates import UpdateManager
        with tempfile.TemporaryDirectory() as directory:
            runtime=SimpleNamespace(store=Store(Path(directory)),config=Mock(side_effect=AssertionError('Automatic desktop update attempted')))
            updates=UpdateManager(runtime)
            updates.desktop_managed=True
            updates.tick()
            runtime.config.assert_not_called()
            with self.assertRaisesRegex(ValueError,'desktop package'):
                updates.apply_async()

    def test_malformed_session_is_not_attached(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'server.json').write_text('{}')
            self.assertIsNone(existing_session(root))
            (root/'server.json').write_text(json.dumps({'origin':'https://example.com','cookie':'c','token':'t'}))
            self.assertIsNone(existing_session(root))
