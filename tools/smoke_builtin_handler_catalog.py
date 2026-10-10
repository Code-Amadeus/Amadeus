"""Start an isolated, model-less backend and verify built-in factory registration.

No installed agent, model, microphone or paid API is used. Test data and logs are
kept under .pytest_tmp/config-catalog for inspection.
"""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.e2e_live_product_journey import ElectronProduct
from tools.smoke_electron_model_less import model_less_backend_environment
from websockets.sync.client import connect

scratch = ROOT / '.pytest_tmp' / 'config-catalog'
scratch.mkdir(parents=True, exist_ok=True)
run = Path(tempfile.mkdtemp(prefix='handler-composition-', dir=scratch))
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
product = ElectronProduct(run_root=run, debug_port=0, no_tts=True, identity={})
env = model_less_backend_environment(product._environment(), python_executable=sys.executable)
env.update(OPENCLAW_GATEWAY_TOKEN='', OPENCLAW_PROJECT_DIR='', CODEX_APP_SERVER_PROVIDER_ENABLED='0',
           DIRECT_CODEX_PROVIDER_ENABLED='0', PI_PROVIDER_ENABLED='0', AMADEUS_ACP_PROVIDERS='[]', AMADEUS_MCP_CONNECTIONS='[]')
def http_request(method, path, authenticated=False):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        conn.request(method, path, headers={'X-Amadeus-Token': product.backend_token} if authenticated else {})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()
fixture_bootstrap = """
import asyncio
import sys
from server.handlers import composition
from server.ws_handler import RequestHandler
class FixtureHandler(RequestHandler):
    methods = ['fixture.catalog']
    async def handle(self, method, params):
        return {'registered': True, 'echo': params.get('message')}
original = composition.builtin_handler_factories
def factories(services, voice):
    return {**original(services, voice), 'fixture': FixtureHandler}
composition.builtin_handler_factories = factories
from server.app import bootstrap
asyncio.run(bootstrap(port=int(sys.argv[1])))
"""
with (run/'backend.log').open('w', encoding='utf-8') as log:
    process = subprocess.Popen([sys.executable, '-c', fixture_bootstrap, str(port)], cwd=ROOT,
        env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Backend exited with {process.returncode}; diagnostic file: {run.name}/backend.log')
            try:
                if http_request('GET','/health')[1].get('status') == 'ok': break
            except (OSError, ValueError): pass
            time.sleep(.2)
        else: raise TimeoutError('Backend did not become ready')
        with connect(f'ws://127.0.0.1:{port}/ws', subprotocols=product.backend_websocket_protocols, proxy=None, open_timeout=5) as ws:
            def request(method, params=None):
                ws.send(json.dumps({'type':'req','id':method,'method':method,'params':params or {}}))
                while True:
                    response=json.loads(ws.recv(timeout=10))
                    if response.get('type')=='res' and response.get('id')==method:
                        assert 'error' not in response['params'], (method,response['params'].get('error'))
                        return response['params']
            assert request('fixture.catalog', {'message': 'fixture-roundtrip'}) == {'registered': True, 'echo': 'fixture-roundtrip'}
            config=request('system.get_config')
            assert {'tts_mimo','tts_remote','tts_fish_audio','tts_embedded_v3'} <= {g['id'] for g in config['voice_configuration']}
            assert request('wake.status')['running'] is False
            request('asr.stop')
            request('tts.interrupt')
        assert http_request('POST','/shutdown',True)[0] == 200
        process.wait(timeout=15)
        assert process.returncode == 0
        print('PASS real backend factory extension, catalog status, voice routes and orderly shutdown')
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
