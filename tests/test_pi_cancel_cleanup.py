"""真实 ProviderRuntime 取消不能截断 Pi/MCP 资源 owner 的 finally。"""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import sys

import psutil
import pytest

from agent_host.mcp_connections import McpConnectionSpec
from agent_host.pi_rpc import PiRpcClient
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunRequest
from test_pi_runtime import native_pi as native_pi, pytestmark as pytestmark


PEER = Path(__file__).parent / 'fixtures/pi_mcp_lifetime_server.py'


def lock_held(path):
    with path.open('r+b') as stream:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@pytest.fixture
async def cancel_scene(native_pi, tmp_path):
    adapter, model_requests, selected, _ = native_pi
    root = tmp_path / 'lifetime'
    root.mkdir()
    adapter.mcp_connections = (McpConnectionSpec(connection_id='fixture', display_name='Lifetime fixture',
        enabled=True, transport='stdio', provider_ids=('pi',), command=sys.executable,
        arguments=('-B', '-X', 'utf8', str(PEER), str(root))),)
    selected.update(tool='mcp__fixture__inspect_fixture', args={})
    permission = asyncio.Event()
    expiry_entered = asyncio.Event()
    release_expiry = asyncio.Event()
    closing_entered = asyncio.Event()
    release_closing = asyncio.Event()
    state = {'publish': 'normal', 'pause_close': False, 'close_calls': 0}
    clients, records = [], []

    class ObservedClient(PiRpcClient):
        async def close(self):
            state['close_calls'] += 1
            closing_entered.set()
            if state['pause_close']:
                await release_closing.wait()
            await super().close()

    def client_factory(*args, **kwargs):
        client = ObservedClient(*args, **kwargs)
        clients.append(client)
        return client

    adapter.client_factory = client_factory

    class Runtime(ProviderRuntime):
        async def _emit(self, record, event):
            await super()._emit(record, event)
            if event.type == 'permission.requested':
                permission.set()
            if event.type == 'permission.expired':
                expiry_entered.set()
                if state['publish'] == 'blocked':
                    await release_expiry.wait()
                elif state['publish'] == 'error':
                    raise RuntimeError('synthetic permission publication failed')

    runtime = Runtime()
    runtime.register(adapter)

    async def start():
        record = await runtime.start(ProviderRunRequest(provider='pi', task='Inspect the synthetic fixture once.',
            cwd=str(tmp_path), requirements=ProviderRequirements(workspace_access='none', workspace_ownership='caller')))
        records.append(record)
        await asyncio.wait_for(permission.wait(), 15)
        run = adapter._runs[record.run_id]
        assert run.permissions and not (root / 'called').exists()
        assert lock_held(root / 'exclusive.lock')
        return record, run

    async def settled(record):
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(record.task_handle), 6)
        assert clients[0].process.returncode is not None
        assert not psutil.pid_exists(json.loads((root / 'started.json').read_text(encoding='utf-8'))['pid'])
        assert (root / 'stdin-eof').is_file()
        assert json.loads((root / 'finally.json').read_text(encoding='utf-8')) == {'stdin_eof': True, 'lock_released': True}
        assert not lock_held(root / 'exclusive.lock')
        assert (root / 'exclusive.lock').is_file()
        assert not (root / 'called').exists()
        assert record.run_id not in adapter._runs
        assert not adapter._attached
        assert all(task.done() for task in clients[0]._tasks)
        assert sum(message['role'] == 'user' and 'Inspect the synthetic fixture once.' in str(message['content'])
            for message in model_requests[0]['messages']) == 1

    try:
        yield state, runtime, start, settled, expiry_entered, closing_entered, release_closing, clients
    finally:
        release_expiry.set()
        release_closing.set()
        # 首败也正常关闭本测试的原连接；不重发任务，锁文件不删除。
        for client in clients:
            await PiRpcClient.close(client)
        for record in records:
            if record.task_handle and not record.task_handle.done():
                record.task_handle.cancel()
            with suppress(asyncio.CancelledError):
                await record.task_handle


@pytest.mark.parametrize('publish', ['blocked', 'error'])
async def test_runtime_cancel_closes_transport_despite_expiry_publication(cancel_scene, publish):
    state, runtime, start, settled, expiry, _, _, _ = cancel_scene
    state['publish'] = publish
    record, run = await start()
    await asyncio.wait_for(runtime.cancel(record.run_id), 6)
    assert expiry.is_set()
    await settled(record)
    assert run.done.is_set()
    if publish == 'error':
        assert record.status == 'error'
        assert record.error == 'synthetic permission publication failed'
    else:
        assert record.status == 'cancelled'


async def test_runtime_cancel_retains_and_awaits_one_close_through_repeated_cancel(cancel_scene):
    state, runtime, start, settled, _, closing, release, clients = cancel_scene
    state['pause_close'] = True
    record, run = await start()
    await asyncio.wait_for(runtime.cancel(record.run_id), 6)
    await asyncio.wait_for(closing.wait(), 3)
    record.task_handle.cancel()
    await asyncio.sleep(0)
    assert not record.task_handle.done()
    assert clients[0].process.returncode is None
    assert state['close_calls'] == 1
    release.set()
    await settled(record)
    assert run.done.is_set() and record.status == 'cancelled'
