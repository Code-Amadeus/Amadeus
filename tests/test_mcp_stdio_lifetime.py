"""Real synthetic MCP pipes and process trees; no remote services or credentials."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import anyio
import psutil
import pytest
from mcp import Client
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCRequest

from agent_host.mcp_connections import McpConnectionSpec
from agent_host.mcp_stdio import MAX_LINE_BYTES, McpStdioError, managed_mcp_stdio

ROOT = Path(__file__).resolve().parents[1]
PEER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
mode, record = sys.argv[1:3]
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'],
                         stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
record_path = Path(record)
temporary = record_path.with_suffix('.tmp')
temporary.write_text(json.dumps({'server': os.getpid(), 'child': child.pid,
    'package_token': os.environ.get('PACKAGE_TOKEN'), 'host_token': os.environ.get('HOST_ONLY_TOKEN'),
    'registry': os.environ.get('AMADEUS_EXTENSIONS'), 'pythonpath': os.environ.get('PYTHONPATH')}))
os.replace(temporary, record_path)
sys.stderr.write(os.environ.get('PACKAGE_TOKEN', '') + '\n'); sys.stderr.flush()
if mode == 'malformed':
    sys.stdout.write('invalid ' + os.environ.get('PACKAGE_TOKEN', '') + '\n'); sys.stdout.flush(); time.sleep(120)
if mode == 'oversized':
    sys.stdout.write('x' * (8 * 1024 * 1024 + 1)); sys.stdout.flush(); time.sleep(120)
if mode == 'ignore':
    time.sleep(120)
if mode == 'eof':
    child.terminate(); child.wait(timeout=5); sys.exit(0)
def send(request, result):
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        send(request, {'protocolVersion': request['params']['protocolVersion'],
                      'serverInfo': {'name': 'synthetic-local-mcp', 'version': '1.0.0'},
                      'capabilities': {'tools': {}}})
    elif method == 'tools/list':
        send(request, {'tools': [{'name': 'echo', 'description': 'Synthetic echo',
            'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}}]})
    elif method == 'tools/call':
        if request['params']['name'] == 'hang':
            Path(record + '.pending').write_text('waiting'); time.sleep(120)
        send(request, {'content': [{'type': 'text', 'text': request['params']['arguments']['text']}], 'isError': False})
    else:
        # MCP2 defaults to an auto discover probe. A legacy peer must answer
        # unknown methods, leaving protocol negotiation to the official SDK.
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                          'error': {'code': -32601, 'message': 'Synthetic method not found'}}), flush=True)
'''


def wait_until(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic process lifetime condition did not complete")
        time.sleep(0.01)


async def async_wait_until(condition, timeout=8):
    with anyio.fail_after(timeout):
        while not condition():
            await anyio.sleep(0.01)


def alive(pid):
    if not psutil.pid_exists(pid):
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def cleanup_record(record):
    if record.exists():
        for pid in json.loads(record.read_text()).values():
            if isinstance(pid, int):
                with suppress(psutil.NoSuchProcess):
                    psutil.Process(pid).kill()


@pytest.fixture
def peer(tmp_path):
    script = tmp_path / 'peer.py'
    script.write_text(PEER, encoding='utf-8')
    records = []

    def connection(mode='normal'):
        record = tmp_path / f'{mode}-{len(records)}.json'
        records.append(record)
        return McpConnectionSpec(connection_id='synthetic', display_name='Synthetic MCP', transport='stdio',
            command=str(Path(sys.executable).resolve()), arguments=('-I', str(script), mode, str(record)), cwd=str(tmp_path),
            environment={'PACKAGE_TOKEN': 'synthetic-package-secret', 'PYTHONUNBUFFERED': '1'}), record

    yield connection
    for record in records:
        cleanup_record(record)


async def test_real_official_client_handshake_list_tool_and_normal_tree_cleanup(peer, monkeypatch, capsys):
    monkeypatch.setenv('HOST_ONLY_TOKEN', 'synthetic-host-secret')
    monkeypatch.setenv('AMADEUS_EXTENSIONS', 'synthetic-aggregate-snapshot')
    monkeypatch.setenv('PYTHONPATH', 'synthetic-unsafe-import-path')
    connection, record = peer()
    with anyio.fail_after(8):
        async with Client(managed_mcp_stdio(connection), raise_exceptions=False) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == ['echo']
            result = await client.call_tool('echo', {'text': 'Hello · 日本語'})
            assert result.is_error is False
            assert result.content[0].text == 'Hello · 日本語'
            facts = json.loads(record.read_text())
            assert facts['package_token'] == 'synthetic-package-secret'
            assert facts['host_token'] is facts['registry'] is facts['pythonpath'] is None
            assert alive(facts['server']) and alive(facts['child'])
    wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))
    captured = capsys.readouterr()
    assert 'synthetic-package-secret' not in captured.out + captured.err


async def test_native_task_cancellation_kills_server_and_descendant(peer):
    connection, record = peer()

    async def request():
        async with Client(managed_mcp_stdio(connection), raise_exceptions=False) as client:
            await client.call_tool('hang', {'text': 'Synthetic pending request'})

    task = asyncio.create_task(request())
    await async_wait_until(lambda: Path(str(record) + '.pending').exists())
    facts = json.loads(record.read_text())
    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 6)
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))
    assert time.monotonic() - start < 6


async def test_anyio_cancellation_during_handshake_is_bounded_and_kills_tree(peer):
    connection, record = peer('ignore')
    with anyio.move_on_after(0.3) as scope:
        async with Client(managed_mcp_stdio(connection), raise_exceptions=False):
            pytest.fail('Silent synthetic server must not initialize')
    assert scope.cancelled_caught
    await async_wait_until(record.exists)
    facts = json.loads(record.read_text())
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))


@pytest.mark.parametrize('mode,diagnostic', [('malformed', 'invalid JSON-RPC'), ('oversized', 'exceeds 8 MiB')])
async def test_invalid_stdout_is_bounded_and_diagnostic_contains_no_child_content(peer, mode, diagnostic):
    connection, record = peer(mode)
    with anyio.fail_after(8):
        async with managed_mcp_stdio(connection) as (read, _write):
            error = await read.receive()
            assert isinstance(error, McpStdioError)
            assert diagnostic in str(error)
            assert 'synthetic-package-secret' not in str(error)
            with pytest.raises(anyio.EndOfStream):
                await read.receive()
    facts = json.loads(record.read_text())
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))


async def test_eof_closes_official_read_stream(peer):
    connection, record = peer('eof')
    with anyio.fail_after(8):
        async with managed_mcp_stdio(connection) as (read, _write):
            with pytest.raises(anyio.EndOfStream):
                await read.receive()
    facts = json.loads(record.read_text())
    assert not alive(facts['server']) and not alive(facts['child'])


async def test_context_exit_interrupts_blocked_pipe_write(peer):
    connection, record = peer('ignore')
    start = time.monotonic()
    with anyio.fail_after(8):
        async with managed_mcp_stdio(connection) as (_read, write):
            await async_wait_until(record.exists)
            await write.send(SessionMessage(JSONRPCRequest(jsonrpc='2.0', id=1, method='synthetic', params={'text': 'x' * (2 * 1024 * 1024)})))
            await anyio.sleep(0.05)
    facts = json.loads(record.read_text())
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))
    assert time.monotonic() - start < 6


async def test_outbound_line_limit_closes_transport_without_sending_unbounded_data(peer):
    connection, record = peer('ignore')
    with anyio.fail_after(8):
        async with managed_mcp_stdio(connection) as (read, write):
            await async_wait_until(record.exists)
            await write.send(SessionMessage(JSONRPCRequest(jsonrpc='2.0', id=1, method='synthetic', params={'text': 'x' * MAX_LINE_BYTES})))
            error = await read.receive()
            assert isinstance(error, McpStdioError) and 'exceeds 8 MiB' in str(error)
            with pytest.raises(anyio.EndOfStream):
                await read.receive()
    facts = json.loads(record.read_text())
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))


def test_windows_abrupt_host_exit_kills_server_and_descendant(peer, tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows Job kill-on-close guarantee')
    connection, record = peer()
    ready = tmp_path / 'host-ready'
    driver = tmp_path / 'host.py'
    driver.write_text('''
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_host.mcp_connections import McpConnectionSpec
from agent_host.mcp_stdio import managed_mcp_stdio
from mcp import Client
async def run():
    connection = McpConnectionSpec(connection_id='synthetic', display_name='Synthetic MCP', transport='stdio',
        command=sys.executable, arguments=('-I', sys.argv[2], 'normal', sys.argv[3]),
        environment={'PYTHONUNBUFFERED':'1'})
    async with Client(managed_mcp_stdio(connection), raise_exceptions=False) as client:
        await client.list_tools()
        Path(sys.argv[4]).write_text('ready')
        await asyncio.sleep(120)
asyncio.run(run())
''', encoding='utf-8')
    host = subprocess.Popen([sys.executable, '-I', str(driver), str(ROOT), connection.arguments[1], str(record), str(ready)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(ready.exists, timeout=10)
        facts = json.loads(record.read_text())
        assert alive(facts['server']) and alive(facts['child'])
        host.kill()
        host.wait(timeout=5)
        wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)


async def test_executable_resolution_fails_closed_without_process_launch(tmp_path):
    connection = McpConnectionSpec(connection_id='synthetic', display_name='Synthetic MCP', transport='stdio', command=str(tmp_path / 'missing.exe'))
    with pytest.raises(McpStdioError, match='executable is unavailable'):
        async with managed_mcp_stdio(connection):
            pytest.fail('Unavailable executable must never launch')


@pytest.mark.parametrize('path_key', ['PATH', pytest.param('Path', marks=pytest.mark.skipif(os.name != 'nt', reason='Windows case-insensitive environment'))])
async def test_bare_executable_resolves_only_from_explicit_child_path(peer, path_key):
    original, record = peer()
    connection = McpConnectionSpec(connection_id='synthetic', display_name='Synthetic MCP', transport='stdio',
        command=Path(sys.executable).name, arguments=original.arguments,
        environment={**original.environment, path_key: str(Path(sys.executable).parent)})
    with anyio.fail_after(8):
        async with Client(managed_mcp_stdio(connection), raise_exceptions=False) as client:
            assert [tool.name for tool in (await client.list_tools()).tools] == ['echo']
    facts = json.loads(record.read_text())
    await async_wait_until(lambda: not alive(facts['server']) and not alive(facts['child']))


async def test_windows_batch_script_is_rejected_without_shell_execution(tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows batch shell boundary')
    marker = tmp_path / 'must-not-exist'
    command = tmp_path / 'unsafe.cmd'
    command.write_text(f'@echo launched > "{marker}"', encoding='utf-8')
    connection = McpConnectionSpec(connection_id='synthetic', display_name='Synthetic MCP', transport='stdio', command=str(command))
    with pytest.raises(McpStdioError, match='native executable'):
        async with managed_mcp_stdio(connection):
            pytest.fail('Batch command must never run')
    assert not marker.exists()
