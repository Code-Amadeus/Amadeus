"""Both admitted Chat entries consume the same current, versioned originals."""

import asyncio
import json
from datetime import datetime
from unittest.mock import Mock

import eidema as memory
import pytest

from config.memory import MemoryConfig
from core import session_manager as sessions
from core import session_source_journal as journal
from core.chat_history_projection import project_role_history
from server.cooperative_chat_ingress import _shared_role_history
from server.memory_service import HostMemoryService, MemoryLookupEvidence
from server.protocol import Method
from test_cooperative_pending_turn import pending_host as pending_host
from test_memory_recall import SyntheticMultilingualEmbedder
from test_memory_source_delivery import wait_for


USER = "synthetic-user"
ORIGINAL = "抹茶の注文は前回の練習が終わったときよ。"
SAVED = "抹茶は自分でも買っておく予定です。"
QUESTION = "前に話した抹茶を覚えている？"
ANSWER = "練習のあとに抹茶を注文したわ。"


async def wire(context, tmp_path, monkeypatch, *, embedder=None, scoped_recall_enabled=True, speech=False):
    monkeypatch.setenv("AMADEUS_SESSION_JOURNAL_ENABLED", "0")
    config = MemoryConfig(semantic_enabled=embedder is not None, source_runtime_enabled=True,
                          user_id=USER, db_path=tmp_path / "memory.sqlite3",
                          scoped_recall_enabled=scoped_recall_enabled)
    host = HostMemoryService(config, embedder_factory=(lambda: embedder) if embedder else None)
    if speech:
        from server.memory_output_speech import install
        install(host)
    host.start()
    await host._startup
    assert host._source_lookup is not None
    context.manager.memory = host
    monkeypatch.setattr(sessions, "_model_history_projection", host.project_history)
    return host


def old_session():
    assert sessions.create_session("earlier", activate=False) == "earlier"
    assert sessions.append_session_message("earlier", role="user", content="練習が終わった。", turn_id="old")
    assert sessions.append_session_message("earlier", role="assistant", content=ORIGINAL,
                                          turn_id="old", message_id="previous")


async def save_note(host, sid):
    ctx = host.context(sid)
    return await host.request(Method.MEMORY_REMEMBER, {
        "session_id": sid, "context_token": ctx["context_token"], "operation_id": "note", "item_id": "note",
        "text": SAVED, "scope": "character",
    })


async def send(ctx, *, direct, turn="current", question=QUESTION):
    assert await ctx.handler.handle(Method.CHAT_SEND, {
        "text": question, "session_id": ctx.session_id, "turn_id": turn, "memory_lookup": direct,
    }) == {"status": "ok", "turn_id": turn}
    await ctx.finish()


@pytest.mark.parametrize("direct", [True, False])
async def test_shared_cross_session_lookup_through_chat_wire(pending_host, tmp_path, monkeypatch, direct):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    calls = []
    async def model(messages, **_options):
        frame = json.loads(messages[-1]["content"])
        calls.append(frame)
        if frame["source_kind"] == "user":
            return json.dumps({"say": "", "action": {"op": "memory_lookup", "query": QUESTION}})
        return ANSWER

    ctx.manager.query = model
    try:
        old_session()
        assert (await save_note(host, ctx.session_id))["ok"]
        access = host._access(host.context(ctx.session_id))
        retrieval = memory.Retrieval(host._store)
        await wait_for(lambda: retrieval.lookup_sources(access, "抹茶").items, bool)
        rank = host._source_lookup.rank
        async def force_index_before_read(session_id, query, **ownership):
            # Positive control for self-matching: wait until the very same
            # accepted question could be returned by the native index.
            current = memory.SourceSpan(**journal.coordinate(session_id, ownership["input_id"], 1,
                                        host.context(session_id).get("incarnation")))
            await wait_for(lambda: retrieval.lookup_sources(access, "抹茶").items,
                           lambda items: any(item.source == current for item in items))
            return await original_search(session_id, query, **ownership)
        original_search = host.lookup_for_session
        ranking = Mock(wraps=rank)
        monkeypatch.setattr(host._source_lookup, "rank", ranking)
        monkeypatch.setattr(host, "lookup_for_session", force_index_before_read)
        await send(ctx, direct=direct)
        assert len(calls) == (1 if direct else 2), (calls, ctx.visible)
        frame = calls[-1]["current"]
        assert frame["saved_memories_only"] is False
        clock = ranking.call_args.kwargs["question_time"]
        assert clock["status"] == "known"  # Canonical Session clock stays exact.
        shown = datetime.fromisoformat(frame["question_time"].replace("Z", "+00:00"))
        accepted = datetime.fromisoformat(clock["utc"].replace("Z", "+00:00"))
        assert shown == accepted.replace(second=0, microsecond=0)
        packet = frame["memory_reference"]
        assert ORIGINAL in [row["text"] for row in packet["records"]]
        assert QUESTION not in [row["text"] for row in packet["records"]]
        assert SAVED in [entry["text"] for entry in packet["entries"]]
        assert ranking.call_count == 1 and len(packet["groups"]) <= 8
        output = memory.SourceSpan(**journal.coordinate(ctx.session_id, ctx.publications[-1]["message_id"], 1))
        reads = host._store.output_read_set(access, output)
        assert len(reads.memories) == 1 and ORIGINAL in json.dumps(packet, ensure_ascii=False)
        assert memory.SourceSpan(**journal.coordinate("earlier", "previous", 1)) in [
            pin.source for pin in reads.sources
        ]
        assert ranking.call_args.kwargs["current"] not in [pin.source for pin in reads.sources]
        assert ctx.publications[-1]["text"] == ANSWER
        assert ctx.host.adapter.calls == 0 and ctx.host.work.list_work_items() == []
        assert sessions.delete_session("earlier")
        await wait_for(lambda: project_role_history(_shared_role_history(ctx.session_id, "later")),
                       lambda rows: ANSWER not in [row["text"] for row in rows])
        assert host._store.list_memories(access)[0].text == SAVED
    finally:
        await host.close()


@pytest.mark.parametrize("boundary", ["before_read", "before_publish"])
@pytest.mark.parametrize("mutation", ["delete", "revise"])
async def test_source_change_blocks_stale_index_and_in_flight_answer(pending_host, tmp_path, monkeypatch, boundary, mutation):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    frames = []
    def change_source():
        if mutation == "delete":
            assert sessions.delete_session("earlier")
        else:
            assert sessions.persist_interrupted_assistant_turn(
                "earlier", turn_id="old", heard_content="注文はまだ確定していません。")

    async def model(messages, **_options):
        frame = json.loads(messages[-1]["content"])["current"]
        frames.append(frame)
        if boundary == "before_publish":
            assert ORIGINAL in [record["text"] for record in frame["memory_reference"]["records"]]
            change_source()
            return ANSWER
        return "保存済みのメモだけが見つかったわ。"
    ctx.manager.query = model
    try:
        old_session()
        assert (await save_note(host, ctx.session_id))["ok"]
        access = host._access(host.context(ctx.session_id))
        retrieval = memory.Retrieval(host._store)
        await wait_for(lambda: retrieval.lookup_sources(access, "抹茶").items, bool)
        # Keep the accepted withdrawal in the Session while the SDK is behind.
        host._sources._task.cancel()
        await asyncio.gather(host._sources._task, return_exceptions=True)
        search = host.lookup_for_session
        async def read(session_id, query, **ownership):
            ranked = await search(session_id, query, **ownership)
            if boundary == "before_read":
                change_source()
            return ranked
        monkeypatch.setattr(host, "lookup_for_session", read)
        await send(ctx, direct=True)
        assert ORIGINAL in [row.text for row in retrieval.lookup_sources(access, "抹茶").items]
        assert len(frames) == 1
        if boundary == "before_read":
            package = frames[0]["memory_reference"]
            assert package["records"] == []
            assert package["entries"][0]["text"] == SAVED
        assert ANSWER not in [event["text"] for event in ctx.publications]
        assert ANSWER not in json.dumps(ctx.spoken, ensure_ascii=False)
    finally:
        await host.close()


async def test_ordinary_chat_does_not_search_source_index(pending_host, tmp_path, monkeypatch):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    frames = []
    async def model(messages, **_options):
        frames.append(json.loads(messages[-1]["content"]))
        return json.dumps({"say": "そうね。", "action": None})
    ctx.manager.query = model
    search = Mock(side_effect=AssertionError("ordinary Chat did not admit an explicit read"))
    monkeypatch.setattr(host._source_lookup, "rank", search)
    try:
        await send(ctx, direct=False, question="今日は空がきれい。")
        search.assert_not_called()
        assert len(frames) == 1 and "memory_reference" not in frames[0]
        assert ctx.publications[-1]["text"] == "そうね。"
    finally:
        await host.close()


async def test_query_encoding_is_shared_by_notes_and_cross_language_sources(pending_host, tmp_path, monkeypatch):
    ctx = pending_host
    embedder = SyntheticMultilingualEmbedder()
    host = await wire(ctx, tmp_path, monkeypatch, embedder=embedder)
    try:
        old_session()
        assert (await save_note(host, ctx.session_id))["ok"]
        async with asyncio.timeout(5):
            while True:
                if (host._sources.state == "idle" and host._semantic_state == "ready"
                        and host._index_task is not None and host._index_task.done()):
                    break
                await asyncio.sleep(.01)
        query = Mock(wraps=embedder.embed_query)
        monkeypatch.setattr(embedder, "embed_query", query)
        reference = await host.lookup_for_session(ctx.session_id, "绿色饮品")
        evidence = await host.validate_lookup(reference)
        query.assert_called_once()
        assert ORIGINAL in [row["text"] for row in evidence.payload["records"]]
        assert SAVED in [row["text"] for row in evidence.payload["entries"]]
    finally:
        await host.close()


async def test_withdrawn_original_invalidates_derived_text_before_replay(pending_host, tmp_path, monkeypatch):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    derivative = "先ほどの抹茶の記録を確認しました。"
    legacy = "保存した抹茶メモの説明です。"
    try:
        old_session()
        note = await save_note(host, ctx.session_id)
        assert note["ok"]
        context = host.context(ctx.session_id)
        access = host._access(context)
        retrieval = memory.Retrieval(host._store)
        await wait_for(lambda: retrieval.lookup_sources(access, "抹茶").items, bool)
        result = await host.lookup_for_session(ctx.session_id, "抹茶")
        evidence = await host.validate_lookup(result)
        saved = MemoryLookupEvidence(context, evidence.candidates, {})
        for value, mid, text in ((saved, "saved-answer", legacy), (evidence, "raw-answer", derivative)):
            async with host.lookup_delivery(value, message_id=mid) as published:
                assert sessions.append_session_message(ctx.session_id, role="assistant", turn_id=mid,
                                                       content=text, message_id=mid)
                published()
        await wait_for(lambda: retrieval.lookup_sources(access, "抹茶").items,
                       lambda items: any(item.text == derivative for item in items))
        rows = _shared_role_history(ctx.session_id, "later")
        dependencies, retained = await host.history_dependencies(ctx.session_id, rows)
        assert dependencies.candidates == saved.candidates
        visible = [row["text"] for row in project_role_history(retained)]
        assert legacy in visible and derivative not in visible

        host._sources._task.cancel()
        await asyncio.gather(host._sources._task, return_exceptions=True)
        assert sessions.delete_session("earlier")
        assert derivative in [item.text for item in retrieval.lookup_sources(access, "抹茶").items]
        latest = await host.validate_lookup(await host.lookup_for_session(ctx.session_id, "抹茶"))
        records = [entry["text"] for entry in latest.payload["records"]]
        assert legacy in records and ORIGINAL not in records and derivative not in records
        assert latest.payload["entries"][0]["text"] == SAVED
    finally:
        await host.close()
