"""The desktop memory switches and the explicit Flash formation boundary."""
from dataclasses import replace
import json
from time import time_ns

import eidema
import httpx
import pytest

from config.environment import EnvironmentReader
from config.memory import MemoryConfig
from core import session_manager as sessions
from server.memory_preview import FlashMemoryProvider, configured_maintenance
from server.memory_service import HostMemoryService, MemoryRequestError
from test_memory_source_delivery import wait_for
from test_session_source_lifecycle import USER, room as room


USE = "AMADEUS_MEMORY_USE_ENABLED"
EXTRACT = "AMADEUS_MEMORY_EXTRACTION_ENABLED"


@pytest.mark.parametrize(("use", "extract"), [(False, False), (True, False), (False, True), (True, True)])
def test_memory_controls_are_independent(use, extract):
    config = MemoryConfig.from_environment(EnvironmentReader({USE: str(use), EXTRACT: str(extract)}))
    assert config.enabled
    assert config.use_enabled is use and config.extraction_enabled is extract
    assert config.capture_sources is extract
    assert config.source_runtime_enabled is (use or extract)


def _completion(request, items):
    return httpx.Response(200, request=request, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"complete": True, "items": items}, ensure_ascii=False),
        }}],
        "usage": {"prompt_tokens": 90, "completion_tokens": 20, "total_tokens": 110},
    })


def test_background_transport_is_bound_to_the_disclosed_provider(monkeypatch):
    observed = []

    def post(client, url, *, content, headers):
        assert not client.follow_redirects and not client.trust_env
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer synthetic"
        body = json.loads(content)
        assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "low" and body["max_tokens"] == 900
        source, = json.loads(body["messages"][1]["content"])["sources"]
        assert source == {"source_id": "s0", "speaker_role": "user", "recorded_at_ms": 120,
                          "input_role": "incremental", "text": "The test lamp is blue."}
        assert b"host-private-speaker-key" not in content
        observed.append(body)
        return _completion(httpx.Request("POST", url), [])

    monkeypatch.setattr(httpx.Client, "post", post)
    provider = FlashMemoryProvider(eidema, "synthetic")
    source = eidema.FormationSource("s0", "The test lamp is blue.", "user", "host-private-speaker-key", 120)
    request = provider.prepare((source,), max_output_tokens=900, timeout_ms=30_000)
    result = provider.generate(request.payload, max_output_tokens=900, timeout_ms=30_000)
    assert len(observed) == 1
    assert result.finish == "stop" and result.actual == eidema.Usage(90, 20, 1)


@pytest.mark.asyncio
async def test_formed_note_can_be_recalled_with_formation_off_and_retires_with_source(room, tmp_path, monkeypatch):
    source_session, denied_session = "formation-source", "ordinary-source"
    first = "The blue storage cabinet is beside the window."
    second = "工具室の予備の鍵は青い棚に入れました。"
    calls = []

    def post(_client, url, *, content, headers):
        assert url == "https://api.deepseek.com/chat/completions" and headers["Authorization"] == "Bearer synthetic"
        body = json.loads(content)
        rows = json.loads(body["messages"][1]["content"])["sources"]
        assert {row["text"] for row in rows} == {first, second}
        by_text = {row["text"]: {"source_id": row["source_id"], "quote": row["text"]} for row in rows}
        cold = "comparison_kind" in body["messages"][0]["content"]
        calls.append("cold" if cold else "warm")
        if cold:
            items = [{"statement": "The sources do not establish a shared object.",
                      "comparison_kind": "unresolved", "left": by_text[first], "right": by_text[second]}]
        else:
            items = [
                {"statement": second, "kind": "fact", "support": [by_text[second]]},
                {"statement": "There are two keys in the cabinet.", "kind": "fact", "support": [by_text[first]]},
            ]
        return _completion(httpx.Request("POST", url), items)

    monkeypatch.setattr(httpx.Client, "post", post)
    access = eidema.AccessScope(USER, sessions.conversation_history.character_id)
    config = MemoryConfig(db_path=tmp_path / "preview.sqlite3", user_id=USER,
        semantic_enabled=False, source_runtime_enabled=True, use_enabled=True, extraction_enabled=True)
    allowed = lambda session_id: session_id == source_session

    async def close(host):
        await host.close()
        await host.wait_closed()

    def note_rows(host):
        return host._store.list_memories(access)

    host = HostMemoryService(config, formation_for_session=allowed)
    host.start()
    options = configured_maintenance(host, "synthetic")
    options = replace(options, config=eidema.RuntimeConfig(enabled=True, quiet_ms=0, max_wait_ms=50, batch_events=1))
    try:
        await host._startup
        binding = await options.bind(host, resource_ready=lambda: True)
        binding.set_foreground_busy(True)
        now = time_ns() // 1_000_000
        for sid, texts in ((source_session, (first, second)), (denied_session, ("Outside formation scope.",))):
            sessions.create_session(sid)
            for index, text in enumerate(texts):
                assert sessions.append_session_message(sid, role="user", content=text, turn_id=f"turn-{index}",
                    message_id=f"source-{index}", created_at_ms=now + index)
        await wait_for(host._sources.accepted_streams, lambda rows: len(rows) >= 2)
        assert calls == []
        binding.set_foreground_busy(False)
        await wait_for(lambda: calls, lambda rows: sorted(rows) == ["cold", "warm"])
        await wait_for(lambda: note_rows(host), lambda notes: {row.text for row in notes} == {second})
        pending = await wait_for(lambda: eidema.Candidates(host._store).list(access),
                                 lambda rows: any(row.comparison for row in rows))
        assert any(row.comparison and row.comparison.kind == "unresolved" for row in pending)
        assert any(row.text == "There are two keys in the cabinet." and row.state == "pending" for row in pending)
        learned, = note_rows(host)
        assert learned.retention == "dependent" and learned.source_candidate_id
    finally:
        await close(host)

    host = HostMemoryService(replace(config, extraction_enabled=False, capture_sources=False))
    host.start()
    try:
        await host._startup
        assert configured_maintenance(host, "synthetic") is None
        assert host.runtime_status()["use_enabled"] and not host.runtime_status()["extraction_enabled"]
        sessions.create_session("reader")
        assert sessions.append_session_message("reader", role="user", content="What did I write about the key?",
            turn_id="lookup-turn", message_id="lookup-user", created_at_ms=time_ns() // 1_000_000)
        reference = await host.lookup_for_session("reader", "工具室 予備 鍵",
            input_id="lookup-user", turn_id="lookup-turn", question="What did I write about the key?")
        evidence = await host.validate_lookup(reference)
        assert any(item.memory_id == learned.memory_id for item in evidence.candidates)
        assert not any("reader" in stream for _, stream in host._sources.accepted_streams())
        assert sessions.delete_session(source_session)
        await wait_for(lambda: note_rows(host), lambda notes: not notes)
        assert sorted(calls) == ["cold", "warm"]
    finally:
        await close(host)

    host = HostMemoryService(replace(config, use_enabled=False, extraction_enabled=False, capture_sources=False))
    host.start()
    try:
        await host._startup
        with pytest.raises(MemoryRequestError, match="disabled"):
            await host.lookup_for_session("reader", "工具室")
        status = await host.request("memory.status", {})
        assert status["runtime"]["sdk_version"] and not status["runtime"]["use_enabled"]
    finally:
        await close(host)


async def test_exhausted_token_budget_schedules_the_next_window(room, tmp_path, monkeypatch):
    calls = []

    def post(_client, url, *, content, headers):
        calls.append(json.loads(content))
        return _completion(httpx.Request("POST", url), [])

    monkeypatch.setattr(httpx.Client, "post", post)
    first_now = time_ns() // 1_000_000
    next_window = first_now + 60_000
    now = first_now

    def budget(at_ms):
        start = first_now if at_ms < next_window else next_window
        return eidema.BudgetWindow(f"window-{start}", start, start + 60_000, 10,
                                  eidema.Usage(32_768, 4096, 10))

    session = "rollover-source"
    config = MemoryConfig(db_path=tmp_path / "rollover.sqlite3", user_id=USER,
        semantic_enabled=False, source_runtime_enabled=True, extraction_enabled=True)
    host = HostMemoryService(config, formation_for_session=lambda sid: sid == session)
    host.start()
    options = replace(configured_maintenance(host, "synthetic"), clock=lambda: now, budget=budget,
        cold_provider=None, config=eidema.RuntimeConfig(enabled=True, quiet_ms=0, max_wait_ms=50, batch_events=1))
    try:
        binding = await options.bind(host, resource_ready=lambda: True)
        sessions.create_session(session)
        assert sessions.append_session_message(session, role="user", content="First observation.",
            turn_id="first", message_id="first", created_at_ms=now)
        maintenance = eidema.Maintenance(host._store)
        first_budget = budget(now).window_id
        await wait_for(lambda: maintenance.budget_status(first_budget).usage.output_tokens,
                       lambda output: output == 20)

        assert sessions.append_session_message(session, role="user", content="Second observation.",
            turn_id="second", message_id="second", created_at_ms=now + 1)
        status = await wait_for(binding.status,
            lambda value: (value["last_report"] or {}).get("code") == "budget_exhausted")
        assert status["last_report"]["step"]["stage"] == "blocked"
        assert status["next_wake_at_ms"] == next_window and len(calls) == 1

        now = next_window
        binding.resume()
        await wait_for(lambda: len(calls), lambda count: count == 2)
    finally:
        await host.close()
        await host.wait_closed()
