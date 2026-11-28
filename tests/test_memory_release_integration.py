"""The packaged SDK keeps scoped selection behind explicit Host admission."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import session_manager as sessions
from core import session_source_journal as journal
from server.memory_service import MemoryLookupEvidence
from test_cooperative_pending_turn import pending_host as pending_host
from test_memory_lookup_followup import add
from test_memory_recall import SyntheticMultilingualEmbedder
from test_memory_source_delivery import wait_for
from test_memory_source_lookup import send, wire
from tts.contract import TTSRequest


@pytest.mark.parametrize(("enabled", "direct", "revoke"), [
    (False, True, False),
    (True, False, False),
    (True, True, False),
    (True, True, True),
])
async def test_scoped_choice_needs_explicit_opt_in_and_retains_selector_reads(
    pending_host, tmp_path, monkeypatch, enabled, direct, revoke,
):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch, scoped_recall_enabled=enabled)
    originals = [f"lantern record {index}: " + "bounded witness text " * 55 for index in range(6)]
    selector_calls, answer_packets = [], []

    async def role(messages, **options):
        frame = json.loads(messages[-1]["content"])
        if "sources" in frame:
            selector_calls.append(frame)
            assert options["json_output"] is True and options["selection_turn_id"] == "current"
            assert {row["text"] for row in frame["sources"]} == set(originals)
            chosen = next(row["id"] for row in frame["sources"] if row["text"] == originals[-1])
            if revoke:
                assert sessions.delete_session("earlier")
            return json.dumps({"groups": [[chosen]]})
        if frame["source_kind"] == "user":
            return json.dumps({"say": "", "action": {"op": "memory_lookup", "query": "lantern"}})
        packet = frame["current"].get("memory_reference", {})
        answer_packets.append(packet)
        return "Selected original is current." if packet.get("records") else "No current evidence."

    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        for index, body in enumerate(originals):
            add("earlier", f"record-{index}", body)
        access = host._access(host.context(ctx.session_id))
        retrieval = host._api.Retrieval(host._store)
        await wait_for(lambda: {row.text for row in retrieval.lookup_sources(
            access, "lantern", limit=16, candidate_limit=32, max_chars=131072).items},
            lambda rows: set(originals) <= rows)
        await send(ctx, direct=direct, question="lantern")
        selected = enabled and direct
        assert len(selector_calls) == int(selected)
        if selected and not revoke:
            assert [row["text"] for row in answer_packets[-1]["records"]] == [originals[-1]]
            output = host._api.SourceSpan(**journal.coordinate(
                ctx.session_id, ctx.publications[-1]["message_id"], 1))
            reads = host._store.output_read_set(access, output)
            assert {host._api.SourceSpan(**journal.coordinate("earlier", f"record-{i}", 1))
                    for i in range(len(originals))} <= {pin.source for pin in reads.sources}
        if revoke:
            assert not any(packet.get("records") for packet in answer_packets)
            assert "Selected original is current." not in [row["text"] for row in ctx.publications]
        assert ctx.host.adapter.calls == 0 and not ctx.host.work.list_work_items()
        assert host._maintenance is None  # Selection grants no background execution.
    finally:
        await host.close()


async def test_wide_scoped_choice_uses_matched_splits_and_seals_selector_reads(pending_host, tmp_path, monkeypatch):
    class SplitEmbedder(SyntheticMultilingualEmbedder):
        def split_passage(self, text):
            return [text[index:index + 120] for index in range(0, len(text), 120)]

    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch, embedder=SplitEmbedder())
    originals = [f"lantern record {index}: " + "bounded witness text " * 41 for index in range(18)]
    selector_rows, delivered = [], []

    async def role(messages, **options):
        frame = json.loads(messages[-1]["content"])
        if "sources" in frame:
            selector_rows.extend(frame["sources"])
            target = next(row for row in frame["sources"] if originals[-1].startswith(row["text"]))
            return json.dumps({"groups": [[target["id"]]]})
        delivered.extend(row["text"] for row in frame["current"]["memory_reference"]["records"])
        return "Selected the complete source."

    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        for index, body in enumerate(originals):
            add("earlier", f"record-{index}", body)
        access = host._access(host.context(ctx.session_id))
        retrieval = host._api.Retrieval(host._store)
        await wait_for(lambda: retrieval.coverage(access, target_kind="source"),
                       lambda coverage: coverage.indexed_documents == len(originals))
        seal = Mock(wraps=host._store.register_output_use)
        monkeypatch.setattr(host._store, "register_output_use", seal)
        await send(ctx, direct=True, question="lantern")
        assert len(selector_rows) == len(originals)
        assert all(row["excerpt"] == [0, len(row["text"])] and len(row["text"].encode("utf8")) <= 720
                   for row in selector_rows)
        assert delivered == [originals[-1]]
        seal.assert_called_once()
        assert len(seal.call_args.args[1].sources) == len(originals)
    finally:
        await host.close()


@pytest.mark.parametrize("change", ["correct", "forget", "retire"])
async def test_speech_guard_drops_revoked_memory_and_keeps_synthesis_available(
    pending_host, tmp_path, monkeypatch, change,
):
    from server import memory_output_speech as speech
    from tts import pipeline
    from tts.utterance_scheduler import TTSUtteranceScheduler

    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch, speech=True)
    registry = host.output_speech
    queue = speech.queue(registry)
    playback = SimpleNamespace(add_to_playlist=AsyncMock())
    backend = AsyncMock()
    monkeypatch.setattr(pipeline, "select_synthesis", lambda *_args, **_options: ("test", backend))
    monkeypatch.setattr(pipeline, "_utterance_scheduler", TTSUtteranceScheduler(logger=pipeline.logger))
    monkeypatch.setattr(pipeline, "_synthesis_guard", None)
    speech.bind_pipeline(pipeline, playback, registry)
    context = host.context(ctx.session_id)
    access = host._access(context)
    original = "The blue rehearsal token goes in the left drawer."
    try:
        if change == "retire":
            sessions.create_session("earlier", activate=False)
            add("earlier", "token", original)
            lookup = await wait_for(lambda: host._source_lookup.search_originals(access, "rehearsal token"), bool)
            value = host._sources.view(access).pin(lookup[0])
            reads = host._store.capture_read_set(access, sources=(value,), now_ms=1)
            evidence = MemoryLookupEvidence(context, (), {}, read_set=reads, pack=object())
        else:
            save = await host.request("memory.remember", {
                **context, "operation_id": "save", "item_id": "token", "text": original,
            })
            item = save["operation"]
            candidates = host._store.recall(access, "rehearsal token", now_ms=1).candidates
            evidence = MemoryLookupEvidence(context, candidates, {})
        async with host.lookup_delivery(evidence, message_id="queued-answer") as started:
            event = {"message_id": "queued-answer", "revision": 1}
            with registry.sentence_scope(registry.selected(ctx.session_id, event)):
                await queue.put(TTSRequest("queued-answer", original, is_first=True))
            started()
        await queue.put(TTSRequest("independent", "We can still talk.", is_first=True))
        request, independent = await queue.get(), await queue.get()
        assert registry.current_ticket(registry.ticket_for_sentence(request.sentence_id))
        if change == "retire":
            assert sessions.delete_session("earlier")
        else:
            result = await host.request("memory." + change, {
                **context, "operation_id": change, "item_id": "token",
                "memory_id": item["memory_id"], "expected_version": item["row_version"],
                **({"text": "The blue rehearsal token moved."} if change == "correct" else {}),
            })
            assert result["ok"]
        assert not registry.current_ticket(registry.ticket_for_sentence(request.sentence_id))

        # Exercise the actual injected segment and synthesis boundaries.
        def synthesis_for(item):
            segment = pipeline._utterance_scheduler._to_segment(item)
            return pipeline._synthesis_guard(SimpleNamespace(segments=(segment,)))[1]

        permit = asyncio.BoundedSemaphore(1)
        await permit.acquire()
        await synthesis_for(request)(request.text, request.sentence_id, True, task_semaphore=permit)
        backend.assert_not_called()
        assert playback.add_to_playlist.await_args.args[0].size == 0 and not permit.locked()
        await synthesis_for(independent)(independent.text, independent.sentence_id, True)
        backend.assert_awaited_once()
    finally:
        await host.close()
