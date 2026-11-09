"""Install loopback transport/dialog fixtures before the owned app's startup.

Fixtures replace transport and dialogs, while the shipping startup, update
boundary and installer still execute. The inspector belongs only to the
explicitly launched disposable smoke process.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def release_fixture_script(fixture: Path, state=None):
    code = (ROOT / 'electron/tests/fixtures/extensionRelease.cjs').read_text(encoding='utf-8')
    return (code + f';await setupReleaseFixture({json.dumps(str(fixture))});'
            + 'Object.assign(extensionReleaseFixture,' + json.dumps(state or {}) + ');')


async def restart_product(product, script: str):
    from tools.probes.extension_release_shell import evaluate_main
    from tools.smoke_electron_model_less import _wait_enabled

    previous = product.process.pid
    await evaluate_main(product, "(()=>{setImmediate(()=>process.getBuiltinModule('module').createRequire(process.execPath)('electron').app.quit());return true})()")
    await asyncio.to_thread(product.process.wait, 30)
    await product.stop()
    count = getattr(product, '_extension_restarts', 0) + 1
    product._extension_restarts = count
    (product.run_root / f'extension-restart-{count}.log').write_bytes(product.log_path.read_bytes())
    await product.start(startup_timeout=180, main_startup_script=script)
    assert product.process.pid != previous
    await _wait_enabled(product.page.locator('textarea[placeholder*="Type a message"]'), timeout=60)


async def confirm_update(product, package_id: str, version: str):
    """Keep only after the caller has checked the actual installed behavior."""
    for _ in range(60):
        result = await product.page.evaluate("window.amadeus.extensionCatalog({action:'health'})")
        assert result['ok'], result.get('error')
        pending = result['catalog'].get('pendingUpdate')
        assert pending and pending['id'] == package_id and pending['version'] == version
        if pending['canConfirm']:
            break
        assert not pending['recoveryRequired'], pending
        await asyncio.sleep(0.5)
    else:
        raise AssertionError('Updated plugin did not become ready for explicit confirmation')
    await product.page.get_by_role('button', name='Plugins', exact=True).click()
    await product.page.get_by_role('button', name='Keep this version', exact=True).click()
    from playwright.async_api import expect
    await expect(product.page.get_by_role('button', name='Keep this version', exact=True)).to_have_count(0, timeout=30000)
    result = await product.page.evaluate("window.amadeus.extensionCatalog({action:'get'})")
    assert not result['catalog'].get('pendingUpdate')


async def prepare_startup_fixture(product, script: str, *, timeout: float):
    import websockets

    def address():
        connection = http.client.HTTPConnection('127.0.0.1', product.main_debug_port, timeout=2)
        try:
            connection.request('GET', '/json/list')
            targets = json.loads(connection.getresponse().read())
            assert len(targets) == 1
            url = targets[0]['webSocketDebuggerUrl']
            assert url.startswith(f'ws://127.0.0.1:{product.main_debug_port}/')
            return url
        finally:
            connection.close()

    deadline = time.monotonic() + timeout
    while True:
        if product.process.poll() is not None:
            raise RuntimeError('Owned Electron exited before startup fixture setup')
        try:
            url = await asyncio.to_thread(address)
            break
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise TimeoutError('Owned main inspector did not open')
            await asyncio.sleep(0.1)

    async with websockets.connect(url, open_timeout=3) as socket:
        sequence = 0
        paused = False

        async def command(method, params=None):
            nonlocal sequence, paused
            sequence += 1
            await socket.send(json.dumps(dict(id=sequence, method=method, params=params or {})))
            while True:
                response = json.loads(await asyncio.wait_for(socket.recv(), 30))
                if response.get('method') == 'Debugger.paused':
                    paused = True
                if response.get('id') == sequence:
                    if 'error' in response:
                        raise RuntimeError(str(response['error']))
                    return response.get('result', {})

        await command('Runtime.enable')
        await command('Debugger.enable')
        await command('Runtime.runIfWaitingForDebugger')
        while not paused:
            response = json.loads(await asyncio.wait_for(socket.recv(), 30))
            paused = response.get('method') == 'Debugger.paused'
        expression = (
            f"(()=>{{if(process.pid!=={product.process.pid})throw Error('Unexpected process');"
            "const require=process.getBuiltinModule('module').createRequire(process.execPath);"
            "const app=require('electron').app;const ready=app.whenReady.bind(app);let fixture;"
            "app.whenReady=()=>fixture??=ready().then(async()=>{" + script + "});return true})()"
        )
        result = await command('Runtime.evaluate', dict(expression=expression, returnByValue=True))
        if result.get('exceptionDetails') or result.get('result', {}).get('value') is not True:
            raise RuntimeError('Could not install startup fixture: ' + str(result))
        await command('Debugger.resume')
        # Detach debugger instrumentation explicitly after resuming. Closing the
        # socket while startup callbacks are running can crash packaged Electron.
        await command('Debugger.disable')
        ready = await command('Runtime.evaluate', dict(
            expression="process.getBuiltinModule('module').createRequire(process.execPath)('electron').app.whenReady().then(()=>true)",
            awaitPromise=True, returnByValue=True))
        if ready.get('exceptionDetails') or ready.get('result', {}).get('value') is not True:
            raise RuntimeError('Startup fixture did not become ready: ' + str(ready))
