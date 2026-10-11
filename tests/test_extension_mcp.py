from __future__ import annotations

import copy
import json

import pytest

from agent_host.extension_mcp import project_extension_mcp_connections
from agent_host import mcp_connections


def package():
    return {
        "enabled": True, "providers": ["codex"], "python_path": "C:/env/python.exe",
        "package_path": "C:/packages/notes", "data_path": "C:/data/notes",
        "config": {"limit": 4}, "secrets": {"api_key": "own-key"},
        "manifest": {
            "id": "com.example.notes", "display_name": {"en": "Notes"},
            "config": {"limit": {"type": "integer"}, "api_key": {"type": "string", "secret": True}},
            "contributes": {"mcp_server": [{
                "id": "notes", "transport": "stdio", "command": "${python}",
                "args": ["-m", "notes", "--limit", "${config.limit}", "${data}"],
                "env": {"SERVICE_KEY": "${config.api_key}"},
            }]},
        },
    }


def test_package_projection_keeps_native_provider_boundary_and_stable_identity():
    entry = package()
    item, = project_extension_mcp_connections(entry)
    assert item.command == entry["python_path"]
    assert item.arguments == ("-m", "notes", "--limit", "4", entry["data_path"])
    assert item.cwd == ""
    assert item.environment == {"SERVICE_KEY": "own-key"}
    assert item.supports_provider("codex") and not item.supports_provider("other")
    assert "own-key" not in str(item.public_dict())
    assert item.public_dict()["main_chat_access"] is False
    other = copy.deepcopy(entry)
    other["manifest"]["id"] = "com.other.notes"
    assert project_extension_mcp_connections(other)[0].connection_id != item.connection_id
    assert project_extension_mcp_connections(entry)[0].connection_id == item.connection_id


@pytest.mark.parametrize("field,value", [("command", "${config.api_key}"),
                                        ("args", ["${config.api_key}"])])
def test_credentials_cannot_escape_into_process_arguments(field, value):
    entry = package()
    entry["manifest"]["contributes"]["mcp_server"][0][field] = value
    with pytest.raises(ValueError, match="secrets cannot appear"):
        project_extension_mcp_connections(entry)


def test_http_credentials_are_namespaced_and_do_not_fall_back_to_host(monkeypatch):
    entry = package()
    entry["manifest"]["contributes"]["mcp_server"] = [{
        "id": "notes", "transport": "http", "url": "https://notes.invalid/mcp",
        "bearer_token": "${config.api_key}",
    }]
    item, = project_extension_mcp_connections(entry)
    assert item.environment[item.bearer_token_env_var] == "own-key"
    entry["secrets"].clear()
    monkeypatch.setenv("api_key", "unrelated-host-value")
    with pytest.raises(ValueError, match="credential is unavailable"):
        project_extension_mcp_connections(entry)


def test_disabled_package_has_no_projection_and_unbound_package_is_not_executable():
    entry = package()
    entry["enabled"] = False
    assert project_extension_mcp_connections(entry) == ()
    entry["enabled"] = True
    entry["providers"] = []
    assert project_extension_mcp_connections(entry)[0].enabled is False


def test_startup_consumes_raw_registry_and_rebuild_after_removal_drops_package(monkeypatch):
    monkeypatch.setattr(mcp_connections, "_PROCESS_CONNECTIONS", None)
    raw = [{"id": "manual", "name": "Manual", "transport": "stdio", "command": "python",
            "environment": {"MANUAL_SECRET": "manual-key"}}]
    monkeypatch.setenv(mcp_connections.MCP_CONNECTIONS_ENV, json.dumps(raw))
    extra = project_extension_mcp_connections(package())
    loaded = mcp_connections.configure_mcp_connections(extra_connections=extra)
    assert len(loaded) == 2
    assert mcp_connections.MCP_CONNECTIONS_ENV not in __import__("os").environ
    assert mcp_connections.load_mcp_connections() == loaded
    from server.capability_composition import mcp_connection_capability_package
    projected = mcp_connection_capability_package(loaded)
    assert [item.contribution_id for item in projected.contributions] == ['manual']
    assert mcp_connection_capability_package(extra) is None
    monkeypatch.setenv(mcp_connections.MCP_CONNECTIONS_ENV, json.dumps(raw))
    assert len(mcp_connections.configure_mcp_connections()) == 1


async def test_manual_mcp_keeps_sdk_command_handling_without_host_secret_inheritance(monkeypatch):
    import mcp
    observed = []

    class Client:
        def __init__(self, transport, **kwargs):
            observed.append(transport)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(mcp, 'Client', Client)
    monkeypatch.setenv('HOST_ONLY_SECRET', 'must-not-forward')
    connection = mcp_connections.McpConnectionSpec(
        connection_id='manual', display_name='Manual', transport='stdio',
        command='npx.cmd', arguments=('--yes', 'synthetic-mcp'),
        environment={'OWN_KEY': 'own-secret'})
    async with mcp_connections.open_mcp_connection(connection):
        pass
    transport, = observed
    assert isinstance(transport, mcp.StdioServerParameters)
    assert transport.command == 'npx.cmd'
    assert transport.args == ['--yes', 'synthetic-mcp']
    assert transport.env['OWN_KEY'] == 'own-secret'
    assert 'HOST_ONLY_SECRET' not in transport.env
