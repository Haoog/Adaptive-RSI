"""Build and smoke an installed wheel outside this checkout, without model calls."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]
SMOKE = '''
import hashlib,json,threading,urllib.request
from pathlib import Path
from importlib.resources import files
from adaptive_rsi import __version__
from adaptive_rsi.runtime import Runtime
from adaptive_rsi.server import Server
from adaptive_rsi.store import Store
schema=files('adaptive_rsi').joinpath('schemas/api-state-v2.schema.json').read_bytes()
runtime=Runtime(Store(Path('state')),agent=object())
server=Server(runtime)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
 request=urllib.request.Request(server.origin+'/api/contract',headers={'Cookie':server.cookie+'='+server.token})
 with urllib.request.urlopen(request,timeout=10) as response:
  contract=json.load(response);status=response.status
 assert status==200 and contract['descriptor']['api_version']==2
 print(json.dumps({'installed_version':__version__,'contract_status':status,'api_version':contract['descriptor']['api_version'],
  'schema_sha256':hashlib.sha256(schema).hexdigest(),'migrations':runtime.store.schema_migrations(),
  'models_called':runtime.store.one('SELECT count(*) AS n FROM calls')['n']}))
finally:
 server.shutdown();server.server_close();thread.join(5);runtime.close()
'''


def check():
    with tempfile.TemporaryDirectory(prefix='rsi-wheel-install-') as directory:
        temporary = Path(directory)
        subprocess.run(['uv', 'build', '--wheel', '--out-dir', str(temporary / 'dist')], cwd=ROOT, check=True,
                       stdout=sys.stderr, stderr=sys.stderr, timeout=180)
        wheels = list((temporary / 'dist').glob('*.whl'))
        if len(wheels) != 1:
            raise ValueError('Expected exactly one built wheel')
        environment = temporary / 'environment'
        venv.EnvBuilder().create(environment)
        python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        subprocess.run(['uv', 'pip', 'install', '--python', str(python), '--no-deps', str(wheels[0])],
                       cwd=temporary, check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=120)
        result = subprocess.run([str(python), '-I', '-c', SMOKE], cwd=temporary, check=False,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            print(result.stderr[-16000:], file=sys.stderr)
            raise RuntimeError('Installed-wheel smoke failed')
        report = json.loads(result.stdout)
        if report['models_called'] != 0:
            raise ValueError('Installed-wheel smoke unexpectedly called a model')
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    result = check()
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(text)
    print(text)
