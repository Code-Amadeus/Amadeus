"""Accepted names and independent startup names reach native adapter requests."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from agent_host.provider_identity import execution_role_name, request_main_role_name
from agent_host.provider_types import ProviderRunRequest
from llm import character_prompts as characters
from openclaw import client as openclaw_client


HISTORICAL = "Makise Kurisu (牧瀬紅莉栖)"
NAMES = (None, HISTORICAL, "Mira", "Mira before rename ${literal}")


@pytest.fixture
def mira_startup_without_old_pack(monkeypatch):
    values = dict(characters.active_character().values)
    values.update({key: "Mira after rename" for key in characters.NAME_KEYS - {"character_id", "work_title"}})
    values.update(character_id="mira", work_title="")
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", characters.CharacterPrompts("mira", values))
    forbidden = Mock(side_effect=AssertionError("accepted request must not load or read a character pack"))
    monkeypatch.setattr(characters, "load", forbidden)
    startup_reads = Mock(wraps=characters.text)
    monkeypatch.setattr("agent_host.provider_authoring.text", startup_reads)
    monkeypatch.setattr(openclaw_client, "text", startup_reads)
    return startup_reads


def metadata(tmp_path, name):
    return {"auip_authoring_skill_path": str(tmp_path / "skill.md"),
            **({"main_role_name": name} if name is not None else {})}


def assert_coding_prompt(prompt, name):
    expected = "Mira after rename" if name is None else "Kurisu" if name == HISTORICAL else name
    assert f"Amadeus/{expected} to watch" in prompt
    if name is not None:
        assert "Mira after rename" not in prompt
        assert f'main role is "{name}"' in prompt
    else:
        assert "[Amadeus role-reference context]" not in prompt


def assert_startup_name_reads(startup_reads, name):
    if name is None:
        startup_reads.assert_called_once_with("short_name")
    else:
        startup_reads.assert_not_called()


@pytest.mark.parametrize("name", NAMES)
async def test_acp_native_prompt_uses_accepted_role_name(tmp_path, mira_startup_without_old_pack, name):
    from test_acp_provider import adapter, records

    provider = adapter(tmp_path)
    result = await provider.run(ProviderRunRequest(provider.provider_id, "Read state.", cwd=str(tmp_path),
        metadata=metadata(tmp_path, name)), "accepted-acp", lambda _event: asyncio.sleep(0))
    assert result.status == "done", result.error
    prompt = next(row["text"] for row in records(tmp_path) if row["kind"] == "prompt")
    assert_coding_prompt(prompt, name)
    assert_startup_name_reads(mira_startup_without_old_pack, name)


@pytest.mark.parametrize("name", NAMES)
async def test_direct_codex_stdin_uses_accepted_role_name(tmp_path, mira_startup_without_old_pack, monkeypatch, name):
    from agent_host.adapters.direct_codex import DirectCodexAdapter
    from test_direct_codex_adapter import _fake_cli, _request

    provider = DirectCodexAdapter(cli_path=sys.executable, prefix_args=(_fake_cli(tmp_path),), timeout_s=5)
    prompts = []
    write = provider._write_prompt
    async def capture(process, prompt):
        prompts.append(prompt)
        await write(process, prompt)
    monkeypatch.setattr(provider, "_write_prompt", capture)
    request = _request(tmp_path, "Read state.", write=False)
    request.metadata.update(metadata(tmp_path, name))
    result = await provider.run(request, "accepted-direct", lambda _event: asyncio.sleep(0))
    assert result.status == "done", result.error
    assert_coding_prompt(prompts[0], name)
    assert_startup_name_reads(mira_startup_without_old_pack, name)


@pytest.mark.parametrize("name", NAMES)
async def test_codex_app_server_native_developer_context_uses_accepted_role_name(tmp_path, mira_startup_without_old_pack, name):
    from agent_host.adapters.codex_app_server import CodexAppServerAdapter
    from test_codex_app_server_adapter import _FakeCodex, _FakeThread, _FakeTurn, _request, _success_events

    turn = _FakeTurn("accepted-turn", _success_events("accepted-turn"))
    sdk = _FakeCodex([_FakeThread("accepted-thread", turn)])
    provider = CodexAppServerAdapter(codex=sdk)
    request = _request(tmp_path, "Read state.", write=False)
    request.metadata.update(metadata(tmp_path, name))
    result = await provider.run(request, "accepted-app-server", lambda _event: asyncio.sleep(0))
    assert result.status == "done", result.error
    prompt = sdk.injected_items[0][1][0]["content"][0]["text"]
    assert_coding_prompt(prompt, name)
    assert_startup_name_reads(mira_startup_without_old_pack, name)


@pytest.mark.parametrize("name", NAMES)
async def test_openclaw_gateway_native_message_uses_accepted_role_name(mira_startup_without_old_pack, name):
    from agent_host.adapters.openclaw import OpenClawAdapter
    from test_openclaw_adapter import _FakeGatewayClient

    _FakeGatewayClient.configure("Done.")
    request_metadata = {"timeout": 3, **({"main_role_name": name} if name is not None else {})}
    result = await OpenClawAdapter(gateway_client_factory=_FakeGatewayClient).run(
        ProviderRunRequest("openclaw", "Read state.", metadata=request_metadata),
        "accepted-gateway", lambda _event: asyncio.sleep(0))
    assert result.status == "done", result.error
    message = next(params["message"] for method, params in _FakeGatewayClient.instances[0].requests
                   if method == "sessions.send")
    expected = "Mira after rename" if name is None else "Kurisu" if name == HISTORICAL else name
    assert f"Do not roleplay as Amadeus or {expected}." in message
    if name is not None:
        assert "Mira after rename" not in message
        assert f'main role is "{name}"' in message
    else:
        assert "[Amadeus role-reference context]" not in message
    assert_startup_name_reads(mira_startup_without_old_pack, name)


@pytest.mark.parametrize("metadata", [None, {}, {"unrelated": "value"}])
def test_absent_request_identity_has_no_conversation_name(metadata):
    assert request_main_role_name(metadata) is None


@pytest.mark.parametrize("name", [None, "", "  ", False, 3])
def test_explicit_invalid_request_identity_is_not_a_legacy_default(name):
    with pytest.raises(ValueError):
        request_main_role_name({"main_role_name": name})


def test_historical_alias_is_exact_and_other_names_remain_literal():
    assert execution_role_name(HISTORICAL) == "Kurisu"
    for name in ["Kurisu", HISTORICAL + " renamed", "Mira ${name}", "Mira  before rename"]:
        assert execution_role_name(name) == name


async def test_openclaw_single_call_uses_explicit_name_without_pack_lookup(mira_startup_without_old_pack, monkeypatch):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Done."))])
    monkeypatch.setattr(openclaw_client, "_get_openclaw_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    assert await openclaw_client.ask_openclaw("Read state.", main_role_name="Mira") == "Done."
    assert "Amadeus or Mira." in requests[0]["messages"][0]["content"]
    mira_startup_without_old_pack.assert_not_called()


async def test_openclaw_sse_call_uses_explicit_name_without_pack_lookup(mira_startup_without_old_pack, monkeypatch):
    bodies = []
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def raise_for_status(self): return None
        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"Done."}}]}'
            yield 'data: [DONE]'
    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def stream(self, *_args, **kwargs):
            bodies.append(kwargs["json"])
            return Response()
    monkeypatch.setattr("httpx.AsyncClient", Client)
    assert await openclaw_client.ask_openclaw_stream("Read state.", main_role_name="Mira") == "Done."
    assert "Amadeus or Mira." in bodies[0]["messages"][0]["content"]
    mira_startup_without_old_pack.assert_not_called()
