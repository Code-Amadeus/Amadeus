"""The desktop memory switches and the explicit Flash formation boundary."""
import asyncio
from dataclasses import replace
import json
from time import time_ns

import eidema
import httpx
import pytest

from config.environment import EnvironmentReader
from config.memory import MemoryConfig
from core import session_manager as sessions
from server.handlers.memory_handler import MemoryHandler
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


@pytest.mark.parametrize("legacy", [
    {}, {"AMADEUS_MEMORY_ENABLED": "true"}, {"AMADEUS_MEMORY_ENABLED": "false"},
    {"AMADEUS_MEMORY_SOURCE_RUNTIME_ENABLED": "true"},
])
@pytest.mark.parametrize("use", [None, "false", "true"])
def test_only_explicit_use_setting_authorizes_role_lookup(legacy, use):
    values = legacy if use is None else {**legacy, USE: use}
    config = MemoryConfig.from_environment(EnvironmentReader(values))
    assert config.use_enabled is (use == "true")
    assert not config.extraction_enabled


def _completion(request, items):
    return httpx.Response(200, request=request, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"complete": True, "items": items}, ensure_ascii=False),
        }}],
        "usage": {"prompt_tokens": 90, "completion_tokens": 20, "total_tokens": 110},
    })


@pytest.mark.parametrize("cold", [False, True])
def test_background_transport_is_bound_to_the_disclosed_provider(monkeypatch, cold):
    observed = []

    def post(client, url, *, content, headers):
        assert not client.follow_redirects and not client.trust_env
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer synthetic"
        body = json.loads(content)
        assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "low" and body["max_tokens"] == 900
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON object" in body["messages"][0]["content"]
        source, = json.loads(body["messages"][1]["content"])["sources"]
        assert source == {"source_id": "s0", "speaker_role": "user", "recorded_at_ms": 120,
                          "input_role": "incremental", "text": "The test lamp is blue."}
        assert b"host-private-speaker-key" not in content
        observed.append(body)
        return _completion(httpx.Request("POST", url), [])

    monkeypatch.setattr(httpx.Client, "post", post)
    provider = FlashMemoryProvider(eidema, "synthetic", cold=cold)
    source = eidema.FormationSource("s0", "The test lamp is blue.", "user", "host-private-speaker-key", 120)
    request = provider.prepare((source,), max_output_tokens=900, timeout_ms=30_000)
    result = provider.generate(request.payload, max_output_tokens=900, timeout_ms=30_000)
    assert len(observed) == 1
    assert result.finish == "stop" and result.actual == eidema.Usage(90, 20, 1)


@pytest.mark.parametrize("forget_linked", [False, True])
async def test_formed_note_can_be_recalled_with_formation_off_and_retires_with_source(
    room, tmp_path, monkeypatch, forget_linked,
):
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
    options = replace(options, cold_generation_enabled=True, automatic_cold_context=True,
                      config=eidema.RuntimeConfig(enabled=True, quiet_ms=0, max_wait_ms=50, batch_events=1))
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
        if forget_linked:
            assert sessions.load_session(source_session)[0]
            context = host.context(source_session)
            origin = sessions.read_session_source(source_session, "source-1", revision=1)
            source = {key: origin[key] for key in ("session_id", "message_id", "revision", "projection_version")}
            saved = await host.request("memory.remember", {
                **context, "operation_id": "save-linked", "item_id": "note", "text": "Saved key location.",
                "source": {**source, "start_cp": 0, "end_cp": len(second)},
            })
            assert saved["ok"], saved
            note = saved["operation"]
            params = {**context, "memory_id": note["memory_id"], "expected_version": note["row_version"]}
            result = await host.request("memory.forget_preview", params)
            assert result["ok"], result
            preview = result["preview"]
            assert preview["targets"] == [{"memory_id": note["memory_id"], "row_version": note["row_version"]}]
            assert {row["memory_id"] for row in preview["memories"]} == {note["memory_id"], learned.memory_id}
            result = await host.request("memory.forget", {
                **params, "operation_id": "forget-linked", "item_id": "note", "confirmed_targets": preview["targets"],
            })
            assert result["ok"] and result["operation"]["status"] == "forgotten", result
            assert not host._store.list_memories(access, include_history=True)
        else:
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


async def test_waiting_for_foreground_does_not_tick_and_suggestions_can_be_reviewed(room, tmp_path, monkeypatch):
    source_session, next_session = "suggestion-source", "suggestion-reader"
    text = "I keep my return tag in the blue tray during rehearsal."
    statement = "During rehearsal, the return tag is in the blue tray."
    received, probes = [], []
    ready = False

    def resources_ready():
        probes.append(ready)
        return ready

    def post(_client, url, *, content, headers):
        body = json.loads(content)
        assert "comparison_kind" not in body["messages"][0]["content"]
        source, = json.loads(body["messages"][1]["content"])["sources"]
        assert source["text"] == text
        received.append(source["source_id"])
        evidence = [{"source_id": source["source_id"], "quote": text}]
        return _completion(httpx.Request("POST", url), [
            {"statement": statement, "kind": "fact", "support": evidence},
            {"statement": "A return tag exists.", "kind": "fact", "support": evidence},
            {"statement": "A tag may have been moved.", "kind": "hypothesis", "support": evidence},
        ])

    monkeypatch.setattr(httpx.Client, "post", post)
    config = MemoryConfig(db_path=tmp_path / "review.sqlite3", user_id=USER,
        semantic_enabled=False, source_runtime_enabled=True, extraction_enabled=True)
    host = HostMemoryService(config, formation_for_session=lambda sid: sid == source_session)
    host.start()
    try:
        options = replace(configured_maintenance(host, "synthetic"),
                          config=eidema.RuntimeConfig(enabled=True, quiet_ms=0, max_wait_ms=50, batch_events=1))
        binding = await options.bind(host, resource_ready=resources_ready)
        sessions.create_session(source_session)
        assert sessions.append_session_message(source_session, role="user", content=text,
                                               turn_id="turn", message_id="tag")
        waiting = await wait_for(binding.status, lambda state: state["state"] == "waiting_resource")
        assert not config.db_path.with_suffix(".maintenance.sqlite3").exists()
        assert eidema.MaintenanceRuntime.bound_lane_keys(host._store, binding.runtime_id)
        prior_probes = len(probes)
        binding.resume()  # Explicit notices also cannot reenter SQLite while busy.
        await wait_for(lambda: len(probes), lambda count: count >= prior_probes + 3)
        assert binding.status()["ticks"] == waiting["ticks"] and received == []

        ready = True  # The desktop probe has no push notification.
        access = eidema.AccessScope(USER, sessions.conversation_history.character_id)
        candidates = await wait_for(lambda: eidema.Candidates(host._store).list(access), lambda rows: len(rows) == 3)
        assert len(received) == 1 and all(lane.key.startswith("warm:") for lane in binding.lanes)
        assert not host._store.list_memories(access)
        assert not host._store.recall(access, statement, now_ms=0).candidates

        sessions.create_session(next_session)
        handler = MemoryHandler(host)
        context = host.context(next_session)

        async def request(method, **params):
            response = None
            async with asyncio.timeout(4):
                while response is None or response.get("error") == "busy":
                    response = await handler.handle(method, {
                        "session_id": next_session, "context_token": context["context_token"], **params,
                    })
                    if response.get("error") == "busy":
                        await asyncio.sleep(.01)
            return response

        summaries = (await request("memory.candidates"))["candidates"]
        assert len(summaries) == 3 and all("text" not in item for item in summaries)
        main = next(item for item in candidates if item.text == statement)
        other = next(item for item in candidates if item.text == "A return tag exists.")
        hypothesis = next(item for item in candidates if item.kind == "hypothesis")

        async def detail(candidate):
            return await request("memory.candidate", candidate_id=candidate.candidate_id,
                                 expected_version=candidate.row_version)

        async def decide(candidate, decision, kind="fact"):
            return await request("memory.review_candidate", candidate_id=candidate.candidate_id,
                expected_version=candidate.row_version, decision=decision, kind=kind,
                operation_id=f"{decision}-{candidate.candidate_id}", item_id="review")

        reviewed = (await detail(main))["candidate"]
        assert reviewed["text"] == statement
        assert reviewed["quotes"] == [{"speaker_role": "user", "role": "support", "text": text}]
        assert (await decide(hypothesis, "save"))["error"] == "invalid_request"
        assert (await decide(hypothesis, "dismiss"))["operation"]["status"] == "rejected"
        accepted = (await decide(main, "save"))["operation"]
        assert accepted["status"] == "accepted"
        assert (await decide(main, "save"))["operation"]["replayed"]
        note, = host._store.list_memories(access)
        assert (note.memory_id, note.text, note.retention) == (
            accepted["memory_id"], statement, "explicit")
        question = "Where is the tag?"
        assert sessions.append_session_message(next_session, role="user", content=question,
                                               turn_id="lookup", message_id="question")
        reference = await host.lookup_for_session(next_session, "return tag blue tray",
                                                  input_id="question", turn_id="lookup", question=question)
        evidence = await host.validate_lookup(reference)
        assert any(item.memory_id == note.memory_id for item in evidence.candidates)

        # The UI saw this version. Remove its original before the SQLite
        # receiver applies the withdrawal. Native authority still rejects it.
        assert (await detail(other))["ok"]
        with monkeypatch.context() as delayed_delivery:
            delayed_delivery.setattr(host._sources, "_enqueue", lambda path: None)
            assert sessions.delete_session(source_session)
            assert eidema.Candidates(host._store).detail(access, other.candidate_id).state == "pending"
            assert (await decide(other, "save"))["error"] == "conflict"
        host._sources._enqueue(room.with_name(source_session + ".json"))
        await wait_for(lambda: eidema.Candidates(host._store).list(access), lambda rows: not rows)
        assert [item.memory_id for item in host._store.list_memories(access)] == [note.memory_id]
        assert len(received) == 1
    finally:
        await host.close()
        await host.wait_closed()


async def test_paused_cold_binding_reopens_without_scheduling_comparisons(room, tmp_path, monkeypatch):
    calls = []

    def post(_client, url, *, content, headers):
        body = json.loads(content)
        calls.append("cold" if "comparison_kind" in body["messages"][0]["content"] else "warm")
        return _completion(httpx.Request("POST", url), [])

    monkeypatch.setattr(httpx.Client, "post", post)
    config = MemoryConfig(db_path=tmp_path / "old-binding.sqlite3", user_id=USER,
        semantic_enabled=False, source_runtime_enabled=True, extraction_enabled=True)
    for enabled, sid in ((True, "existing-stream"), (False, "new-stream")):
        cold_before_pause = calls.count("cold")
        host = HostMemoryService(config, formation_for_session=lambda session_id: True)
        host.start()
        try:
            options = replace(configured_maintenance(host, "synthetic"), cold_generation_enabled=enabled,
                              config=eidema.RuntimeConfig(enabled=True, quiet_ms=0, max_wait_ms=50, batch_events=1))
            binding = await options.bind(host, resource_ready=lambda: True)
            sessions.create_session(sid)
            assert sessions.append_session_message(sid, role="user", content=f"A durable report from {sid}.",
                                                   turn_id="turn", message_id="report")
            if enabled:
                await wait_for(lambda: binding.lanes, lambda lanes: any(lane.key.startswith("cold:") for lane in lanes))
                legacy_cold, = (lane.key for lane in binding.lanes if lane.key.startswith("cold:"))
                await wait_for(lambda: calls.count("warm"), lambda count: count == 1)
                await wait_for(binding.status, lambda status: status["state"] in {"waiting_time", "waiting_event"})
            else:
                await wait_for(lambda: calls.count("warm"), lambda count: count == 2)
                await wait_for(binding.status, lambda status: status["state"] in {"waiting_time", "waiting_event"})
                assert [lane.key for lane in binding.lanes if lane.key.startswith("cold:")] == [legacy_cold]
                assert binding.status()["error"] is None
                assert calls.count("cold") == cold_before_pause
        finally:
            await host.close()
            await host.wait_closed()


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
