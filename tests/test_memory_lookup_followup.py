"""Actual Chat handles one bounded supplemental read with exact source order."""

import json
from unittest.mock import Mock

import eidema as memory
import pytest

from core import session_manager as sessions
from core import session_source_journal as journal
from test_cooperative_pending_turn import pending_host as pending_host
from test_memory_source_delivery import wait_for
from test_memory_source_lookup import USER, send, wire


MATCH = "lantern"
ANCHOR = "ランタンの鍵に lantern と書いたよ。"
ENDPOINT = "印は lantern で間違いない。"
CONTINUATION = "当番が終わったら、鍵は灰色の棚へ戻す。"
OTHER_EVENT = "それとは別件だけど、倉庫の鍵は受付に返す。"
FIRST_DRAFT = "鍵の返却先は今の資料では分からないわ。"
FINAL = "当番の鍵を返すのは灰色の棚ね。"


def add(sid, ident, body, *, role="user"):
    assert sessions.append_session_message(sid, role=role, content=body, message_id=ident, turn_id=ident)


def span(sid, ident):
    return memory.SourceSpan(**journal.coordinate(sid, ident, 1))


def hide(sid, ident):
    sessions.mark_memory_control_sources(sid, [("user", journal.coordinate(sid, ident, 1))])


async def indexed(host, sid, term, expected):
    access = host._access(host.context(sid))
    retrieval = memory.Retrieval(host._store)
    await wait_for(lambda: {row.text for row in retrieval.lookup_sources(access, term).items},
                   lambda rows: set(expected) <= rows)
    return retrieval, access


def read_frame(messages):
    return json.loads(messages[-1]["content"])


def proposal(query):
    return json.dumps({"say":"", "action":{"op":"memory_lookup", "query":query}})


@pytest.mark.parametrize(("direct", "bridge", "withheld"), [
    (True, CONTINUATION, False),
    (False, CONTINUATION, False),
    (True, OTHER_EVENT, False),
    (True, CONTINUATION, True),
])
async def test_one_neighbor_read_uses_the_real_continuation(pending_host, tmp_path, monkeypatch,
                                                           direct, bridge, withheld):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    first_and_final = []
    status = "鍵がどの場面のことか確認できないわ。"
    # Even if the final generation asks again, it has no remaining read slot.
    illegal_second = '[DELEGATE op=memory_lookup query="other"]'

    async def role(messages, **_kwargs):
        frame = read_frame(messages)
        if frame["source_kind"] == "user":
            return proposal(MATCH)
        current = frame["current"]
        first_and_final.append(current)
        records = current["memory_reference"]["records"]
        if current["additional_lookup_remaining"] == 1:
            assert [record["text"] for record in records] == [ANCHOR]
            return f'{FIRST_DRAFT}[DELEGATE op=memory_lookup record="{records[0]["source_id"]}" direction="next"]'
        assert current["additional_lookup_remaining"] == 0
        assert not ctx.publications and not ctx.spoken
        assert [record["text"] for record in records] == [ANCHOR, bridge]
        assert current["memory_reference"]["source_order"] == [["s0", "s1", 1]]
        return (FINAL if bridge == CONTINUATION else status) + illegal_second
    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        add("earlier", "anchor", ANCHOR, role="assistant")
        add("earlier", "bridge", bridge)
        add("earlier", "beyond", "ここを飛び越えてはいけない。")
        if withheld:
            hide("earlier", "bridge")
        await indexed(host, ctx.session_id, MATCH, (ANCHOR,))
        observed = Mock(wraps=host.supplement_lookup)
        monkeypatch.setattr(host, "supplement_lookup", observed)
        await send(ctx, direct=direct, question=MATCH)
        expected = FIRST_DRAFT if withheld else FINAL if bridge == CONTINUATION else status
        observed.assert_called_once()
        assert len(first_and_final) == (1 if withheld else 2)
        assert [event["text"] for event in ctx.publications] == [expected]
        assert illegal_second not in json.dumps(ctx.spoken, ensure_ascii=False)
        assert ctx.host.adapter.calls == 0 and not ctx.host.work.list_work_items()
    finally:
        await host.close()


@pytest.mark.parametrize("scope", ["allowed", "withheld", "paused"])
async def test_order_edge_and_dependency_have_the_same_authority(pending_host, tmp_path, monkeypatch, scope):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    pack = []
    async def role(messages, **_):
        pack.append(read_frame(messages)["current"]["memory_reference"])
        return FIRST_DRAFT
    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        add("earlier", "anchor", ANCHOR, role="assistant")
        with monkeypatch.context() as capture:
            if scope == "paused":
                capture.setattr(journal, "session_scope", lambda sid: sid != "earlier")
            add("earlier", "bridge", "補助になるかもしれない記録。")
        add("earlier", "end", ENDPOINT, role="assistant")
        if scope == "withheld":
            hide("earlier", "bridge")
        await indexed(host, ctx.session_id, MATCH, (ANCHOR, ENDPOINT))
        await send(ctx, direct=True, question=MATCH)
        assert len(pack) == 1
        records = {record["text"]: record["source_id"] for record in pack[0]["records"]}
        assert set(records) == {ANCHOR, ENDPOINT}
        assert pack[0].get("source_order", []) == (
            [[records[ANCHOR], records[ENDPOINT], 2]] if scope == "allowed" else [])
        output = span(ctx.session_id, ctx.publications[0]["message_id"])
        pins = host._store._output_direct_reads(USER, output).sources
        assert (span("earlier", "bridge") in {pin.source for pin in pins}) == (scope == "allowed")
    finally:
        await host.close()


@pytest.mark.parametrize("releases", [False, True])
async def test_semantic_followup_shares_the_budget_and_keeps_actual_first_reads(pending_host, tmp_path,
                                                                               monkeypatch, releases):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    originals = [f"lantern candidate {index}" for index in range(8)]
    extra = "harbor return information"
    frames = []
    removed = []
    async def role(messages, **_):
        frame = read_frame(messages)["current"]
        frames.append(frame)
        package = frame["memory_reference"]
        assert len(package["groups"]) == 8
        if len(frames) > 1:
            assert extra in {record["text"] for record in package["records"]}
            return FINAL
        assert extra not in {record["text"] for record in package["records"]}
        removal = package["groups"][-1]
        identifier, = removal["sources"]
        removed.append(next(row["text"] for row in package["records"] if row["source_id"] == identifier))
        attr = f' release_group="{removal["group_id"]}"' if releases else ""
        return FIRST_DRAFT + f'[DELEGATE op=memory_lookup query="harbor"{attr}]'

    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        for index, body in enumerate(originals):
            add("earlier", f"record-{index}", body)
        add("earlier", "additional", extra)
        await indexed(host, ctx.session_id, MATCH, originals)
        await indexed(host, ctx.session_id, "harbor", (extra,))
        await send(ctx, direct=True, question=MATCH)
        assert len(frames) == (2 if releases else 1)
        assert [event["text"] for event in ctx.publications] == [FINAL if releases else FIRST_DRAFT]
        output = span(ctx.session_id, ctx.publications[0]["message_id"])
        pins = host._store._output_direct_reads(USER, output).sources
        keys = {pin.source for pin in pins}
        assert span("earlier", f"record-{originals.index(removed[0])}") in keys
        if releases:
            assert removed[0] not in {row["text"] for row in frames[-1]["memory_reference"]["records"]}
        assert (span("earlier", "additional") in keys) == releases
    finally:
        await host.close()


async def test_proof_revocation_after_model_read_blocks_publication(pending_host, tmp_path, monkeypatch):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    async def role(messages, **_):
        assert read_frame(messages)["current"]["memory_reference"]["source_order"]
        hide("earlier", "bridge")
        return FINAL
    ctx.manager.query = role
    try:
        sessions.create_session("earlier", activate=False)
        add("earlier", "anchor", ANCHOR, role="assistant")
        add("earlier", "bridge", "前の二つを結ぶかもしれない記録。")
        add("earlier", "end", ENDPOINT, role="assistant")
        await indexed(host, ctx.session_id, MATCH, (ANCHOR, ENDPOINT))
        await send(ctx, direct=True, question=MATCH)
        assert ctx.publications and FINAL not in [event["text"] for event in ctx.publications]
        assert FINAL not in json.dumps(ctx.spoken, ensure_ascii=False)
    finally:
        await host.close()


@pytest.mark.parametrize("tag", [
    '[DELEGATE op=memory_lookup record="s99" direction="next"]',
    '[DELEGATE op=memory_lookup query="lantern" scope="all"]',
    '[DELEGATE op=memory_lookup query="lantern"][DELEGATE op=memory_lookup query="cabinet"]',
])
async def test_malformed_followup_cannot_request_a_read(pending_host, tmp_path, monkeypatch, tag):
    ctx = pending_host
    host = await wire(ctx, tmp_path, monkeypatch)
    async def role(_messages, **_):
        return FIRST_DRAFT + tag
    ctx.manager.query = role
    search = Mock(side_effect=AssertionError("only one visible, scoped request is authorized"))
    monkeypatch.setattr(host, "supplement_lookup", search)
    try:
        sessions.create_session("earlier", activate=False)
        add("earlier", "anchor", ANCHOR, role="assistant")
        await indexed(host, ctx.session_id, MATCH, (ANCHOR,))
        await send(ctx, direct=True, question=MATCH)
        search.assert_not_called()
        assert [event["text"] for event in ctx.publications] == [FIRST_DRAFT]
    finally:
        await host.close()
