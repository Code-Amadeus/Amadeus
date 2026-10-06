import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from aiohttp import web

from test_chat_role_delivery import (
    host_role_ownership as host_role_ownership,
    role_loop,
    streaming_role as streaming_role,
)


@pytest.mark.parametrize("chunks,expected", [
    (["実験の対照群か、[EM", "O thinking]整理して考えるわ。続きは出さない。"],
     "実験の対照群か、[EMO thinking]整理して考えるわ。"),
    (["That topic, hmm; ", "let me think.[EMO thinking] A second sentence."],
     "That topic, hmm; let me think."),
    (["その話；少し考える", "わ！次の文。"], "その話；少し考えるわ！"),
])
async def test_head_keeps_a_complete_sentence_and_closes_its_producer(monkeypatch, chunks, expected):
    from llm import hybrid_stream

    closed = asyncio.Event()

    async def tokens(_messages):
        try:
            for chunk in chunks:
                yield chunk
            raise AssertionError("head read past its complete first sentence")
        finally:
            closed.set()

    monkeypatch.setattr(hybrid_stream, "local_head_tokens", tokens)
    assert "".join([part async for part in hybrid_stream._first_local_sentence([])]) == expected
    assert closed.is_set()


@pytest.mark.parametrize("mode", ["streamed", "completed", "action", "professional"])
async def test_head_settles_at_the_user_turn_boundary(streaming_role, mode):
    host = streaming_role
    visible = asyncio.Event()
    head_closed = asyncio.Event()

    async def head():
        try:
            yield "首句。"
            visible.set()
        finally:
            head_closed.set()

    async def reference(text):
        # Head generation is not serialized behind role retrieval.
        await asyncio.wait_for(visible.wait(), 2)
        assert not loop._foreground.locked()
        return ""

    async def query(messages, *, on_text=None):
        await asyncio.wait_for(visible.wait(), 2)
        assert host.queue.qsize() == 1
        assert loop._delivery_lock.locked()
        if mode == "professional":
            await on_text('遠端。[DELEGATE op=work]')
            raise AssertionError("professional handoff did not stop the producer")
        if mode == "streamed":
            raw = '{"action":null,"say":"遠端。"}'
        elif mode == "completed":
            raw = '{"say":"遠端。","action":null}'
        else:
            raw = '{"action":{"op":"work","intent":"execute"},"say":"受理。"}'
        await on_text(raw)
        return raw

    loop = role_loop(query, host.delivery)
    loop.hybrid_head = lambda text, visual: head()
    loop.role_reference = reference
    loop.work_proposals_only = mode == "professional"
    try:
        receipt = await asyncio.wait_for(loop.submit("request", turn_id="reply"), 3)
        assert head_closed.is_set() and not loop._delivery_lock.locked()
        if mode == "action":
            assert receipt["state"] == "work_required"
            assert host.history.call_args.kwargs["content"] == "首句。"
            assert host.history.call_args.kwargs["message_id"] == "cooperative-head:reply"
            assert not receipt.get("_host_coordination_delivered")
            # Ingress may now wait for a choice; an unrelated receipt remains deliverable.
            await asyncio.wait_for(loop._deliver("受理。", cause="reply"), 1)
            assert [call.kwargs["content"] for call in host.history.call_args_list] == ["首句。", "受理。"]
            assert len({call.kwargs["message_id"] for call in host.history.call_args_list}) == 2
            finals = [p for method, p in host.emitted if method == "chat.role_message"]
            assert [p["message_id"] for p in finals] == ["cooperative-head:reply", "reply"]
            assert finals[0]["turn_id"] == "reply"
        else:
            host.history.assert_called_once_with("A", role="assistant", content="首句。遠端。",
                turn_id="reply", message_id=ANY)
            assert len([row for row in loop.history if row["source"] == "kurisu"]) == 1
            if mode == "professional":
                assert receipt["state"] == "work_plan_required"
                assert receipt["_host_coordination_delivered"] is True
            items = [host.queue.get_nowait() for _ in range(host.queue.qsize())]
            assert [item.text for item in items] == ["首句。", "遠端。"]
            assert [item.stream_tts for item in items] == [True, True]
        assert not loop._delivery_lock.locked()
        assert {"kind":"hybrid_head", "turn_id":"reply", "outcome":"presented"} in loop.trace
    finally:
        await loop.close()


@pytest.mark.parametrize("failure", ["cancel", "remote_error"])
async def test_head_interruption_records_only_visible_prefix_and_releases_lock(streaming_role, failure):
    host = streaming_role
    ready, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def head():
        try:
            yield "見えた部分"
            ready.set()
            if failure == "cancel":
                await release.wait()
                yield "未提示の尾"
        finally:
            closed.set()

    async def query(messages, *, on_text=None):
        await ready.wait()
        if failure == "remote_error":
            raise RuntimeError("remote unavailable")
        await release.wait()

    loop = role_loop(query, host.delivery)
    loop.hybrid_head = lambda text, visual: head()
    task = asyncio.create_task(loop.submit("request", input_id="input", turn_id="reply"))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        if failure == "cancel":
            loop._inputs["input"][1].cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await asyncio.wait_for(task, 2)
        assert closed.is_set() and not loop._delivery_lock.locked()
        host.history.assert_called_once_with("A", role="assistant", content="見えた部分", turn_id="reply")
        assert host.queue.empty()
    finally:
        release.set()
        await loop.close()


@pytest.mark.parametrize("scope", ["pending", "independent_auip"])
async def test_head_uses_existing_stream_eligibility(streaming_role, scope):
    async def query(messages, *, on_text=None):
        assert on_text is None
        return '{"action":null,"say":"完了。"}'

    loop = role_loop(query, streaming_role.delivery)
    loop.hybrid_head = lambda *_args: pytest.fail("ineligible head started")

    async def accept():
        return SimpleNamespace(pending=False)

    try:
        await loop.submit("request", turn_id="reply", **(
            {"turn_admission": SimpleNamespace(pending=True), "acceptance_check": accept}
            if scope == "pending" else {"auip_context": {}}))
    finally:
        await loop.close()


async def test_real_http_head_is_independent_of_cli_and_stops_at_first_sentence(monkeypatch):
    from llm import client, hybrid_stream

    seen = []

    async def handle(request):
        seen.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for part in ('[DELE', 'GATE task="ignored"][EMO normal]首句。余計な尾。'):
            await response.write(('data: ' + json.dumps({"choices": [{"delta": {"content": part}}]}) + '\n\n').encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(hybrid_stream, "HYBRID_LOCAL_LLM_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(client, "LOCAL_LLM_TYPE", "cli")
    monkeypatch.setattr(client, "LLM_PROVIDER", "hybrid2")
    try:
        text = "".join([part async for part in hybrid_stream.hybrid_local_head("hello")])
        assert text == "[EMO normal]首句。"
        assert len(seen) == 1 and seen[0]["stream"] is True
        assert seen[0]["max_tokens"] == 80
        assert [row["role"] for row in seen[0]["messages"]] == ["system", "user"]
        monkeypatch.setattr(client, "LLM_PROVIDER", "deepseek")
        assert hybrid_stream.hybrid_local_head("hello") is None
        assert len(seen) == 1
    finally:
        await runner.cleanup()


async def test_empty_head_leaves_one_remote_reply_without_retry(streaming_role, caplog):
    calls = []
    ended = asyncio.Event()

    async def head():
        ended.set()
        if False:
            yield ""

    async def query(messages, *, on_text=None):
        calls.append(messages)
        await ended.wait()
        raw = '{"action":null,"say":"遠端。"}'
        await on_text(raw)
        return raw

    loop = role_loop(query, streaming_role.delivery)
    loop.hybrid_head = lambda text, visual: head()
    try:
        await loop.submit("request", turn_id="reply")
        assert len(calls) == 1 and not loop._delivery_lock.locked()
        assert streaming_role.queue.qsize() == 1
        streaming_role.history.assert_called_once_with("A", role="assistant", content="遠端。",
            turn_id="reply", message_id=ANY)
        assert "local head produced no visible text" in caplog.text
    finally:
        await loop.close()


@pytest.mark.parametrize("mode", ["streamed", "completed", "action", "professional"])
async def test_remote_ready_cancels_unstarted_head_without_waiting_for_endpoint(streaming_role, mode, caplog):
    started, closed = asyncio.Event(), asyncio.Event()
    calls = []

    async def head():
        started.set()
        try:
            await asyncio.Event().wait()
            yield "never shown"
        finally:
            closed.set()

    async def query(messages, *, on_text=None):
        calls.append(messages)
        await started.wait()
        if mode == "professional":
            await on_text('[DELEGATE op=work]')
            raise AssertionError("professional handoff must stop generation")
        raw = {
            "streamed": '{"action":null,"say":"遠端。"}',
            "completed": '{"say":"遠端。","action":null}',
            "action": '{"action":{"op":"work","intent":"execute"},"say":"受理。"}',
        }[mode]
        await on_text(raw)
        return raw

    loop = role_loop(query, streaming_role.delivery)
    loop.hybrid_head = lambda *_: head()
    loop.work_proposals_only = mode == "professional"
    try:
        await asyncio.wait_for(loop.submit("request", turn_id="remote-first"), 2)
        assert closed.is_set() and not loop._delivery_lock.locked()
        assert len(calls) == 1
        assert "separate local voice" in calls[0][0]["content"]
        assert loop.trace[-1] == {"kind":"hybrid_head", "turn_id":"remote-first",
            "outcome":"skipped_remote_ready"}
        assert "local head produced no visible text" not in caplog.text
        if mode in {"streamed", "completed"}:
            streaming_role.history.assert_called_once_with("A", role="assistant", content="遠端。",
                turn_id="remote-first", message_id=ANY)
        else:
            streaming_role.history.assert_not_called()
    finally:
        await loop.close()


async def test_normal_provider_prompt_has_no_hybrid_presentation_instruction(streaming_role):
    async def query(messages, *, on_text=None):
        assert "separate local voice" not in messages[0]["content"]
        return '{"action":null,"say":"遠端。"}'

    loop = role_loop(query, streaming_role.delivery)
    loop.hybrid_head = lambda *_: None
    try:
        await loop.submit("request", turn_id="ordinary")
        assert not any(row.get("kind") == "hybrid_head" for row in loop.trace)
    finally:
        await loop.close()


@pytest.mark.parametrize("provider", ["hybrid", "hybrid2", "hybrid3"])
async def test_visual_head_uses_acknowledgement_prompt_and_no_image_bytes(monkeypatch, provider):
    from llm import client, hybrid_stream
    from llm.visual_context import local_visual_ack_text
    from test_llm_message_query_backends import _visual_context

    captured = []

    async def tokens(messages):
        captured.append(messages)
        yield "はい。"

    monkeypatch.setattr(client, "LLM_PROVIDER", provider)
    monkeypatch.setattr(hybrid_stream, "local_head_tokens", tokens)
    visual = _visual_context()
    assert "".join([part async for part in hybrid_stream.hybrid_local_head("question", visual)]) == "はい。"
    assert local_visual_ack_text("question", visual) in captured[0][-1]["content"]
    assert visual["frame"]["dataBase64"] not in json.dumps(captured)
