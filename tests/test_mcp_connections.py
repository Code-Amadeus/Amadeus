from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_host.mcp_connections import (
    McpConnectionSpec,
    codex_mcp_config_overrides,
    load_mcp_connections,
    mcp_provider_environment,
)
from server.capability_composition import mcp_connection_capability_package
from server.handlers.mcp_connection_handler import McpConnectionHandler
from server.protocol import Method


def _stdio_connection(**overrides) -> McpConnectionSpec:
    values = {
        "connection_id": "amadeus_fixture",
        "display_name": "Fixture MCP",
        "transport": "stdio",
        "enabled": True,
        "provider_ids": ("codex",),
        "command": "python",
        "arguments": ("fixture.py",),
        "environment": {"FIXTURE_TOKEN": "not-public"},
    }
    values.update(overrides)
    return McpConnectionSpec(**values)


def test_registry_validation_and_public_projection_never_return_secret_values() -> None:
    raw = json.dumps(
        {
            "connections": [
                {
                    "id": "amadeus_fixture",
                    "name": "Fixture MCP",
                    "transport": "stdio",
                    "enabled": True,
                    "provider_ids": ["codex"],
                    "command": "python",
                    "arguments": ["fixture.py"],
                    "environment": {"FIXTURE_TOKEN": "  keep-spaces  "},
                }
            ]
        }
    )
    connection = load_mcp_connections(raw)[0]

    assert connection.environment["FIXTURE_TOKEN"] == "  keep-spaces  "
    assert connection.public_dict()["environment_keys"] == ["FIXTURE_TOKEN"]
    assert "keep-spaces" not in str(connection.public_dict())
    assert connection.public_dict()["main_chat_access"] is False


def test_enabled_connection_requires_a_provider_and_rejects_bad_http_urls() -> None:
    with pytest.raises(ValueError, match="compatible Work Provider"):
        _stdio_connection(provider_ids=())
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        McpConnectionSpec(
            connection_id="amadeus_http",
            display_name="HTTP MCP",
            transport="http",
            enabled=False,
            url="file:///private/server",
        )


def test_codex_projection_uses_config_without_copying_secrets_to_command_line() -> None:
    connection = _stdio_connection(cwd="C:/workspace")
    overrides = codex_mcp_config_overrides((connection,))

    assert 'mcp_servers.amadeus_fixture.command="python"' in overrides
    assert 'mcp_servers.amadeus_fixture.args=["fixture.py"]' in overrides
    assert 'mcp_servers.amadeus_fixture.cwd="C:/workspace"' in overrides
    assert all("not-public" not in item for item in overrides)
    assert mcp_provider_environment((connection,), "codex") == {
        "FIXTURE_TOKEN": "not-public"
    }
    assert mcp_provider_environment((connection,), "openclaw") == {}


def test_provider_environment_rejects_conflicting_values() -> None:
    left = _stdio_connection()
    right = _stdio_connection(
        connection_id="amadeus_other",
        display_name="Other MCP",
        environment={"FIXTURE_TOKEN": "different"},
    )
    with pytest.raises(ValueError, match="conflicting values"):
        mcp_provider_environment((left, right), "codex")


def test_capability_projection_is_provider_only_and_secret_free() -> None:
    package = mcp_connection_capability_package((_stdio_connection(),))
    assert package is not None
    contribution = package.contributions[0]

    assert contribution.kind == "mcp_server"
    assert contribution.bindings[0].surface == "work_execution"
    assert contribution.bindings[0].projection == "mcp_connection"
    assert contribution.metadata["provider_ids"] == ["codex"]
    assert contribution.metadata["main_chat_access"] is False
    assert "not-public" not in str(package.to_dict(include_disabled=True))


def test_connection_handler_lists_sanitized_state_and_only_discovers_tools() -> None:
    calls: list[str] = []

    class Client:
        async def list_tools(self, *, cursor=None):
            calls.append(f"list:{cursor or ''}")
            return SimpleNamespace(
                result_type="complete",
                tools=[SimpleNamespace(name="read_fixture")],
                next_cursor=None,
            )

    @asynccontextmanager
    async def open_fixture(_connection):
        calls.append("connect")
        yield Client()

    async def scenario() -> None:
        handler = McpConnectionHandler(
            (_stdio_connection(),),
            open_connection=open_fixture,
        )
        listed = await handler.handle(Method.MCP_CONNECTION_LIST, {})
        assert listed is not None
        assert listed["main_chat_access"] is False
        assert "not-public" not in str(listed)

        tested = await handler.handle(
            Method.MCP_CONNECTION_TEST,
            {"connection_id": "amadeus_fixture"},
        )
        assert tested == {
            "connection_id": "amadeus_fixture",
            "status": "connected",
            "tool_count": 1,
            "tools": ["read_fixture"],
            "main_chat_access": False,
        }
        assert calls == ["connect", "list:"]

    asyncio.run(scenario())


def test_legacy_manual_stdio_migrates_with_real_probe_and_tool_call(tmp_path, monkeypatch):
    from pathlib import Path
    import sys
    from agent_host.mcp_connections import open_mcp_connection
    record = tmp_path / 'child.json'
    peer = Path(__file__).parent / 'fixtures/manual_mcp_environment.py'
    monkeypatch.setenv('FIXTURE_TOKEN', 'synthetic-explicit-token')
    monkeypatch.setenv('UNRELATED_HOST_CREDENTIAL', 'synthetic-host-secret')
    # Original registry shape, with no extension metadata or explicit bindings.
    raw = {'version': 1, 'connections': [{'id': 'legacy', 'name': 'Legacy manual MCP',
        'transport': 'stdio', 'command': sys.executable,
        'arguments': ['-I', str(peer), str(record)], 'cwd': str(tmp_path),
        'enabled': True, 'provider_ids': ['codex']}]}
    async def scenario():
        legacy = load_mcp_connections(json.dumps(raw))[0]
        failed = await McpConnectionHandler((legacy,)).handle(Method.MCP_CONNECTION_TEST, {'connection_id': 'legacy'})
        assert failed['status'] == 'error'
        assert 'TaskGroup' not in failed['detail']
        assert 'Encrypted environment values' in failed['detail']
        assert 'save and restart' in failed['detail']
        assert 'synthetic-explicit-token' not in failed['detail']
        facts = json.loads(record.read_text())
        assert not facts['token_present'] and not facts['host_secret_present']
        raw['connections'][0]['environment'] = {'FIXTURE_TOKEN': 'synthetic-explicit-token'}
        migrated = load_mcp_connections(json.dumps(raw))[0]
        tested = await McpConnectionHandler((migrated,)).handle(Method.MCP_CONNECTION_TEST, {'connection_id': 'legacy'})
        assert tested['status'] == 'connected' and tested['tools'] == ['echo']
        async with open_mcp_connection(migrated) as client:
            result = await client.call_tool('echo', {'text': 'Migrated manual connection'})
            assert not result.is_error and result.content[0].text == 'Migrated manual connection'
            facts = json.loads(record.read_text())
            assert facts['token_present'] and not facts['host_secret_present']
            assert Path(facts['cwd']) == tmp_path
        assert 'synthetic-explicit-token' not in str(tested)
    asyncio.run(scenario())


@pytest.mark.parametrize('transport', ['stdio', 'http'])
def test_probe_unwraps_nested_errors_redacts_secrets_and_limits_migration_hint_to_stdio(transport):
    connection = (_stdio_connection() if transport == 'stdio' else McpConnectionSpec(
        'http_fixture', 'HTTP fixture', 'http', url='https://example.invalid/mcp',
        environment={'FIXTURE_TOKEN': 'not-public'}))

    @asynccontextmanager
    async def failed_connection(_connection):
        raise ExceptionGroup('unhandled errors in a TaskGroup', [
            RuntimeError('Cannot initialize with not-public'),
            ExceptionGroup('nested transport', [PermissionError('Cannot open fixture resource')]),
        ])
        yield  # pragma: no cover -- defines the async context manager shape

    async def scenario():
        result = await McpConnectionHandler((connection,), open_connection=failed_connection).handle(
            Method.MCP_CONNECTION_TEST, {'connection_id': connection.connection_id})
        assert result['code'] == 'connection_failed'
        assert 'Cannot initialize with [redacted]' in result['detail']
        assert 'Cannot open fixture resource' in result['detail']
        assert 'TaskGroup' not in result['detail'] and 'not-public' not in result['detail']
        assert ('Encrypted environment values' in result['detail']) == (transport == 'stdio')
        assert len(result['detail']) <= 500

    asyncio.run(scenario())


def test_long_probe_error_retains_migration_action_after_redaction():
    from server.handlers.mcp_connection_handler import _safe_error
    detail = _safe_error(RuntimeError('not-public ' * 100), _stdio_connection())
    assert 'not-public' not in detail
    assert 'Encrypted environment values' in detail and 'save and restart' in detail
    assert len(detail) <= 500


def test_nested_http_error_redacts_resolved_bearer_token(monkeypatch):
    from server.handlers.mcp_connection_handler import _safe_error
    monkeypatch.setenv('FIXTURE_HTTP_TOKEN', 'synthetic-http-token')
    connection = McpConnectionSpec('http_fixture', 'HTTP fixture', 'http',
        url='https://example.invalid/mcp', bearer_token_env_var='FIXTURE_HTTP_TOKEN',
        environment={'OTHER_TOKEN': 'synthetic-http'})
    detail = _safe_error(ExceptionGroup('transport', [
        RuntimeError('Rejected synthetic-http-token')]), connection)
    assert detail == 'Rejected [redacted]'
