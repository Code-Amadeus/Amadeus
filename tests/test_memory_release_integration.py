"""The packaged SDK keeps scoped selection behind explicit Host admission."""

import json

import pytest

from core import session_manager as sessions
from core import session_source_journal as journal
from test_cooperative_pending_turn import pending_host as pending_host
from test_memory_lookup_followup import add
from test_memory_source_delivery import wait_for
from test_memory_source_lookup import USER, send, wire


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
    monkeypatch.setenv("AMADEUS_MVP_SCOPED_RECALL", "1" if enabled else "0")
    host = await wire(ctx, tmp_path, monkeypatch)
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
            reads = host._store._output_direct_reads(USER, output)
            assert {host._api.SourceSpan(**journal.coordinate("earlier", f"record-{i}", 1))
                    for i in range(len(originals))} <= {pin.source for pin in reads.sources}
        if revoke:
            assert not any(packet.get("records") for packet in answer_packets)
            assert "Selected original is current." not in [row["text"] for row in ctx.publications]
        assert ctx.host.adapter.calls == 0 and not ctx.host.work.list_work_items()
        assert host._maintenance is None  # Selection grants no background execution.
    finally:
        await host.close()
