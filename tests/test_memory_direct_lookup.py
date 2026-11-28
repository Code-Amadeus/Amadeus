"""The click wire uses the real Host/core with synthetic stores and role output."""
import asyncio
from dataclasses import replace
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from core import session_manager as sm
from core.chat_history_projection import project_role_history
from core.turn_coordinator import TurnAuthorityError
from server.cooperative_chat_ingress import _shared_role_history
from server.cooperative_provider_loop import LoopConflict
from server.event_bus import bus
from server.memory_service import HostMemoryService, MemoryRequestError
from server.protocol import Method
from test_cooperative_pending_turn import pending_host as pending_host
from test_memory_explicit_lookup import lookup_loop, proposed
from test_memory_recall import semantic_service, SyntheticMultilingualEmbedder
from test_memory_service import forget, host as host, remember


def observe(loop):
    events = []
    def status(turn_id, value):
        events.append((turn_id, value))
    loop.memory_lookup_status = status
    return events


async def test_click_skips_model_decision_and_auto_recall_and_reads_exact_user_text_once(host, monkeypatch):
    await semantic_service(host, threshold=None)
    await remember(host, "私は抹茶ラテが好きです。")
    await host.service._index_task
    sm.create_session("B")
    sm.append_session_message("B", role="user", content="HISTORY_DO_NOT_EXPAND_PRONOUN", message_id="history", turn_id="history")
    frames = []
    async def model(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        assert frame["source_kind"] == "host_receipt" and "history" not in frame
        assert frame["current"]["query"] == "我偏爱哪种绿色饮品？"
        return "保存した飲み物は抹茶ラテよ。"
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    auto = AsyncMock(side_effect=AssertionError("direct click must not auto-recall"))
    loop.memory_reference = auto
    recall = Mock(wraps=host.service._store.recall)
    monkeypatch.setattr(host.service._store, "recall", recall)
    try:
        result = await loop.submit("我偏爱哪种绿色饮品？", input_id="click", turn_id="turn", memory_lookup=True)
        assert result == {"state":"memory_lookup", "input_id":"click"}
        assert len(frames) == len(published) == recall.call_count == 1
        auto.assert_not_awaited()
        assert recall.call_args.args[1] == "我偏爱哪种绿色饮品？"
        assert events == [("turn", "searching"), ("turn", "found")]
        # The answer is a durable derived source, rather than a new memory.
        assert host.service.history_is_derived(_shared_role_history("B", "later"))
        assert len(host.service._store.list_memories(host.service._access(host.service.context("B")))) == 1
    finally:
        await loop.close()


@pytest.mark.parametrize("complete,has_result,terminal", [(True, True, "found"),
    (True, False, "empty"), (False, True, "partial"), (False, False, "partial")])
async def test_real_coverage_and_final_registration_own_the_receipt(host, monkeypatch, complete, has_result, terminal):
    if complete:
        await semantic_service(host)
    if has_result:
        await remember(host, "抹茶")
        if host.service._index_task is not None:
            await host.service._index_task
    loop, published = lookup_loop(host, AsyncMock(return_value="保存した範囲の回答。"))
    events = observe(loop)
    register = Mock(wraps=host.service._store.register_recall_use)
    monkeypatch.setattr(host.service._store, "register_recall_use", register)
    original = loop.publish
    # Preserve protected delivery while inspecting first publication.
    publish_display = original.display
    async def display(event):
        assert events == [("turn", "searching"), ("turn", terminal)]
        assert register.call_count == int(has_result)
        value = publish_display(event)
        if hasattr(value, "__await__"):
            value = await value
        return value
    original.display = display
    try:
        await loop.submit("抹茶", input_id="click", turn_id="turn", memory_lookup=True)
        assert events == [("turn", "searching"), ("turn", terminal)]
        assert len(published) == 1
    finally:
        await loop.close()


@pytest.mark.parametrize("coverage", [{"semantic":"partial"}, {"scan_limited":True},
    {"pending_memories":1}, {"failed_memories":1}, {"budget_omitted":1}])
async def test_partial_coverage_never_claims_complete_found(host, monkeypatch, coverage):
    await semantic_service(host)
    await remember(host, "抹茶")
    await host.service._index_task
    validate = host.service.validate_lookup
    async def limited(reference):
        evidence = await validate(reference)
        return replace(evidence, payload={**evidence.payload,
            "coverage":{**evidence.payload["coverage"], **coverage}})
    monkeypatch.setattr(host.service, "validate_lookup", limited)
    loop, _published = lookup_loop(host, AsyncMock(return_value="限られた範囲の結果。"))
    events = observe(loop)
    try:
        await loop.submit("抹茶", input_id="one", memory_lookup=True)
        assert events[-1] == ("one", "partial")
    finally:
        await loop.close()


@pytest.mark.parametrize("code", ["disabled", "busy", "unavailable"])
async def test_direct_unavailable_uses_one_result_expression_without_fallback_query(host, monkeypatch, code):
    frames = []
    async def model(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        assert frame["source_kind"] == "host_receipt"
        assert frame["current"]["status"] == code
        assert "query" not in frame["current"] and "memory_reference" not in frame["current"]
        return "今は保存記憶を確認できないわ。"
    monkeypatch.setattr(host.service, "lookup_for_session", AsyncMock(side_effect=MemoryRequestError(code)))
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    try:
        await loop.submit("PRIVATE_QUERY", input_id="one", memory_lookup=True)
        assert len(frames) == len(published) == 1
        assert events == [("one", "searching"), ("one", "unavailable")]
    finally:
        await loop.close()


@pytest.mark.parametrize("stage", ["before_model", "during_model"])
@pytest.mark.parametrize("mutation", ["forget", "correct", "scope"])
async def test_direct_final_revalidation_blocks_withdrawn_facts(host, monkeypatch, stage, mutation):
    saved = await remember(host, "PRIVATE_OLD_FACT 抹茶")
    async def invalidate():
        if mutation == "forget":
            await forget(host, saved["memory_id"])
        elif mutation == "correct":
            result = await host.request(Method.MEMORY_CORRECT, memory_id=saved["memory_id"], expected_version=1,
                operation_id="correct", item_id="correct", text="NEW_FACT", mode="error")
            assert result["ok"]
        else:
            host.project["id"] = "other"
    if stage == "before_model":
        lookup = host.service.lookup_for_session
        async def changed(*args):
            reference = await lookup(*args)
            await invalidate()
            return reference
        monkeypatch.setattr(host.service, "lookup_for_session", changed)
    frames = []
    async def model(messages):
        frames.append(json.loads(messages[-1]["content"]))
        await invalidate()
        return "PRIVATE_OLD_FACT"
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    try:
        await loop.submit("抹茶", input_id="one", memory_lookup=True)
        assert len(frames) == int(stage == "during_model")
        assert len(published) == 1 and "PRIVATE" not in published[0]["text"]
        assert events == ([("one", "searching")] +
            ([("one", "partial")] if stage == "during_model" else []) + [("one", "invalidated")])
    finally:
        await loop.close()


@pytest.mark.parametrize("boundary", ["cancel", "binding", "model", "register", "display"])
async def test_direct_interruption_or_failure_has_no_success_receipt(host, monkeypatch, boundary):
    await remember(host, "PRIVATE_FACT 抹茶")
    started, release = asyncio.Event(), asyncio.Event()
    async def model(messages):
        started.set()
        await release.wait()
        if boundary == "model":
            raise RuntimeError("synthetic transport failed")
        return "PRIVATE_FACT"
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    if boundary == "register":
        monkeypatch.setattr(host.service._store, "register_recall_use", Mock(side_effect=OSError("failed")))
    elif boundary == "display":
        loop.publish.display = lambda event:False
    task = asyncio.create_task(loop.submit("抹茶", input_id="one", memory_lookup=True))
    try:
        await asyncio.wait_for(started.wait(), 3)
        if boundary == "cancel":
            loop._inputs["one"][1].cancel()
        elif boundary == "binding":
            loop._binding = replace(loop._binding, token="changed")
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        expected = [("one", "searching"), ("one", "partial")]
        if boundary in {"cancel", "binding"}:
            expected.append(("one", "invalidated"))
        assert events == expected
        assert not any("PRIVATE_FACT" in item["text"] for item in published)
        if boundary in {"cancel", "binding", "model"}:
            assert isinstance(result[0], BaseException)
    finally:
        release.set()
        await loop.close()


async def test_direct_duplicates_share_one_read_and_expression_and_reject_changed_mode_or_text(host, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def model(messages):
        started.set()
        await release.wait()
        return "結果。"
    lookup = AsyncMock(wraps=host.service.lookup_for_session)
    monkeypatch.setattr(host.service, "lookup_for_session", lookup)
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    try:
        first = asyncio.create_task(loop.submit("原文", input_id="same", turn_id="turn", memory_lookup=True))
        await started.wait()
        duplicate = asyncio.create_task(loop.submit("原文", input_id="same", turn_id="turn", memory_lookup=True))
        with pytest.raises(LoopConflict, match="different memory lookup mode"):
            await loop.submit("原文", input_id="same", turn_id="turn")
        with pytest.raises(LoopConflict, match="different text"):
            await loop.submit("別の質問", input_id="same", turn_id="turn", memory_lookup=True)
        release.set()
        assert await first == await duplicate
        assert (await loop.submit("原文", input_id="same", turn_id="turn", memory_lookup=True))["state"] == "memory_lookup"
        assert lookup.await_count == len(published) == 1 and len(events) == 2
    finally:
        release.set()
        await loop.close()


async def test_ordinary_no_action_and_unfulfilled_promise_never_emit_read_receipts(host):
    model = AsyncMock(return_value=json.dumps({"action":None, "say":"保存した記憶を探してみるわ。"}))
    loop, _published = lookup_loop(host, model)
    events = observe(loop)
    loop.memory_reference = AsyncMock(return_value=None)
    lookup = AsyncMock(side_effect=AssertionError("no read authorized"))
    loop.memory_lookup = lookup
    try:
        assert (await loop.submit("ordinary", input_id="one"))["state"] == "no_action"
        with pytest.raises(LoopConflict, match="different memory lookup mode"):
            await loop.submit("ordinary", input_id="one", memory_lookup=True)
        lookup.assert_not_awaited()
        model.assert_awaited_once()
        assert events == []
    finally:
        await loop.close()


async def wire_memory(context, tmp_path, monkeypatch, *, semantic=False):
    from config.memory import MemoryConfig
    service = HostMemoryService(MemoryConfig(semantic_enabled=semantic, user_id="synthetic-user",
        db_path=tmp_path / "memory.sqlite3"), embedder_factory=SyntheticMultilingualEmbedder)
    service.start()
    await service._startup
    context.manager.memory = service
    monkeypatch.setattr(sm, "_model_history_projection", service.project_history)
    bound = service.context(context.session_id)
    await service.request(Method.MEMORY_REMEMBER, {"session_id":context.session_id,
        "context_token":bound["context_token"], "operation_id":"save", "item_id":"save",
        "text":"私は抹茶が好きです。", "scope":"character"})
    if service._index_task is not None:
        await service._index_task
    return service


async def test_chat_send_wire_reaches_real_host_without_work_planning_and_replay_has_no_new_lookup(pending_host, tmp_path, monkeypatch):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch)
    frames, events = [], []
    async def model(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        assert frame["source_kind"] == "host_receipt"
        return "抹茶の保存記憶を見つけたわ。"
    context.manager.query = model
    context.manager.handle_auip_action = AsyncMock(side_effect=AssertionError("click must not route AUIP"))
    async def capture(method, payload):
        events.append(dict(payload))
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    recall = Mock(wraps=service._store.recall)
    monkeypatch.setattr(service._store, "recall", recall)
    visual = AsyncMock(side_effect=AssertionError("click must not capture visual context"))
    monkeypatch.setattr(context.handler, "_prepare_visual_context", visual)
    params = {"text":"抹茶", "session_id":context.session_id, "turn_id":"wire", "memory_lookup":True}
    try:
        assert await context.handler.handle(Method.CHAT_SEND, params) == {"status":"ok", "turn_id":"wire"}
        await context.finish()
        assert recall.call_count == len(frames) == len(context.publications) == 1
        assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []
        assert [event["status"] for event in events] == ["searching", "partial"]
        assert all(set(event) == {"session_id", "turn_id", "status"} for event in events)
        assert all(event["session_id"] == context.session_id and event["turn_id"] == "wire" for event in events)
        for mode in [True, False]:
            replay = await context.handler.handle(Method.CHAT_SEND, {**params, "memory_lookup":mode})
            assert replay["status"] == "replayed" and replay["turn_id"] == "wire"
        with pytest.raises(TurnAuthorityError, match="replay changed"):
            await context.handler.handle(Method.CHAT_SEND, {**params, "text":"different query"})
        assert recall.call_count == len(frames) == 1 and len(events) == 2
        visual.assert_not_awaited()
        # Durable assistant identity is retained, and forget masks the answer.
        assert service.history_is_derived(_shared_role_history(context.session_id, "later"))
        memory = service._store.list_memories(service._access(service.context(context.session_id)))[0]
        preview = await service.request(Method.MEMORY_FORGET_PREVIEW,
            {"session_id":context.session_id, "context_token":bound_token(service, context.session_id),
             "memory_id":memory.memory_id, "expected_version":memory.row_version})
        await service.request(Method.MEMORY_FORGET,
            {"session_id":context.session_id, "context_token":bound_token(service, context.session_id),
             "memory_id":memory.memory_id, "expected_version":memory.row_version,
             "operation_id":"forget", "item_id":"forget", "confirmed_targets":preview["preview"]["targets"]})
        assert "抹茶の保存記憶" not in json.dumps(project_role_history(_shared_role_history(context.session_id, "later")), ensure_ascii=False)
    finally:
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


def bound_token(service, session_id):
    return service.context(session_id)["context_token"]


async def test_wire_ordinary_replayed_as_click_is_distinguishable_and_does_not_read(pending_host, tmp_path, monkeypatch):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch)
    recall = Mock(wraps=service._store.recall)
    monkeypatch.setattr(service._store, "recall", recall)
    events = []
    async def capture(method, payload):
        events.append(payload)
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        params = {"text":"thanks", "turn_id":"ordinary", "session_id":context.session_id}
        assert (await context.handler.handle(Method.CHAT_SEND, params))["status"] == "ok"
        await context.finish()
        assert (await context.handler.handle(Method.CHAT_SEND, {**params, "memory_lookup":True}))["status"] == "replayed"
        assert recall.call_count == 0 and events == []
    finally:
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


@pytest.mark.parametrize("extra", [{"memory_lookup":1}, {"memory_lookup":"true"}, {"memory_lookup":None},
    {"memory_lookup":[]}, {"text":""}, {"text":" "}, {"text":"x" * 2001}, {"text":7},
    {"text":["query"]}, {"text":{"text":"query"}}, {"visual":{"attachment":{"path":"fake.png"}}},
    {"visual":{}}, {"text":"\ud800"}])
async def test_wire_invalid_input_rejected_before_turn_admission_or_history(pending_host, extra):
    context = pending_host
    try:
        with pytest.raises(TurnAuthorityError):
            await context.handler.handle(Method.CHAT_SEND, {"text":"query", "session_id":context.session_id,
                "turn_id":"invalid", "memory_lookup":True, **extra})
        assert context.handler._stream_task is None
        assert sm.conversation_history.dialog == []
        assert context.manager.ingresses == {}
        assert context.host.work.list_work_items() == []
    finally:
        await context.finish()


async def test_click_without_capability_fails_truthfully_without_normal_chat(pending_host):
    context = pending_host
    events = []
    async def capture(method, payload):
        events.append(payload["status"])
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        await context.handler.handle(Method.CHAT_SEND, {"text":"query", "session_id":context.session_id,
            "turn_id":"unavailable", "memory_lookup":True})
        await context.finish()
        assert context.frames == context.publications == []
        assert events == ["unavailable"]
        assert any(method == Method.CHAT_ERROR and "saved-memory lookup unavailable" in payload["error"]
            for method, payload in context.visible)
        assert context.host.adapter.calls == 0
    finally:
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)


async def test_found_receipt_is_synchronous_and_does_not_repeat_validation_for_expression(host, monkeypatch):
    await semantic_service(host)
    await remember(host, "抹茶")
    await host.service._index_task
    validate = AsyncMock(wraps=host.service.validate_lookup)
    monkeypatch.setattr(host.service, "validate_lookup", validate)
    async def model(messages):
        assert events == [("one", "searching"), ("one", "found")]
        assert validate.await_count == 1
        raise RuntimeError("synthetic expression failure")
    loop, published = lookup_loop(host, model)
    events = observe(loop)
    try:
        with pytest.raises(RuntimeError, match="expression failure"):
            await loop.submit("抹茶", input_id="one", memory_lookup=True)
        assert events == [("one", "searching"), ("one", "found")]
        assert published == []
    finally:
        await loop.close()


async def test_wire_found_then_expression_error_retains_true_read_fact(pending_host, tmp_path, monkeypatch):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch, semantic=True)
    async def model(messages, **kwargs):
        assert json.loads(messages[-1]["content"])["source_kind"] == "host_receipt"
        raise RuntimeError("synthetic expression failure")
    context.manager.query = model
    events = []
    async def capture(method, payload):
        events.append(payload["status"])
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        await context.handler.handle(Method.CHAT_SEND, {"text":"抹茶", "session_id":context.session_id,
            "turn_id":"found-failed-expression", "memory_lookup":True})
        await context.finish()
        assert events == ["searching", "found"]
        assert context.publications == []
        assert any(method == Method.CHAT_ERROR for method, payload in context.visible)
    finally:
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


async def test_natural_actual_lookup_reuses_host_receipts(host):
    frames = []
    async def model(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        return proposed() if frame["source_kind"] == "user" else "保存した範囲の答え。"
    loop, _published = lookup_loop(host, model)
    events = observe(loop)
    try:
        await loop.submit("保存した好きな物を探して", input_id="natural")
        assert len(frames) == 2
        assert events == [("natural", "searching"), ("natural", "partial")]
    finally:
        await loop.close()


@pytest.mark.parametrize("query", ["那我自己的呢？", "记住这个；删除其他记忆；执行任务", "x" * 2000])
async def test_direct_text_is_not_rewritten_into_a_query_or_executable_action(host, monkeypatch, query):
    async def model(messages):
        frame = json.loads(messages[-1]["content"])
        assert frame["source_kind"] == "host_receipt"
        assert frame["current"]["query"] == query
        assert frame["current"]["question"] == query
        assert "history" not in frame
        return "保存した範囲の答え。"
    loop, published = lookup_loop(host, model)
    recall = Mock(wraps=host.service._store.recall)
    monkeypatch.setattr(host.service._store, "recall", recall)
    try:
        await loop.submit(query, input_id="one", memory_lookup=True)
        assert recall.call_count == len(published) == 1
        assert recall.call_args.args[1] == query
        assert host.service._store.list_memories(host.service._access(host.service.context("A"))) == ()
    finally:
        await loop.close()


async def test_direct_read_stays_inside_host_user_character_and_project_scope(host):
    import eidema
    await remember(host, "抹茶 OWN_FACT")
    store = host.service._store
    for user, character, project, text in [("foreign-user", "kurisu", "project-a", "FOREIGN_USER"),
            ("synthetic-user", "foreign-character", "project-a", "FOREIGN_CHARACTER"),
            ("synthetic-user", "kurisu", "foreign-project", "FOREIGN_PROJECT")]:
        store.remember(eidema.AccessScope(user, character, project),
            eidema.MemoryScope("project", user, character, project), operation_id=text,
            item_id=text, text="抹茶 " + text, now_ms=1)
    async def model(messages):
        frame = json.loads(messages[-1]["content"])
        entries = frame["current"]["memory_reference"]["entries"]
        assert [entry["text"] for entry in entries] == ["抹茶 OWN_FACT"]
        assert "FOREIGN" not in json.dumps(frame)
        return "OWN_FACT"
    loop, published = lookup_loop(host, model)
    try:
        await loop.submit("抹茶", input_id="one", memory_lookup=True)
        assert len(published) == 1
    finally:
        await loop.close()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_wire_pending_click_uses_existing_confirmation_gate(pending_host, tmp_path, monkeypatch, confirmed):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch)
    context.manager.query = AsyncMock(return_value="保存した範囲の答え。")
    lookup = AsyncMock(wraps=service.lookup_for_session)
    monkeypatch.setattr(service, "lookup_for_session", lookup)
    events = []
    async def capture(method, payload):
        events.append(payload["status"])
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        await context.handler.handle(Method.CHAT_SEND, {"text":"抹茶", "session_id":context.session_id,
            "turn_id":"pending-click", "pending":True, "memory_lookup":True})
        assert lookup.await_count == 0 and events == [] and context.publications == []
        if confirmed:
            assert await context.handler.confirm_pending_turn("pending-click")
        else:
            assert await context.handler.discard_pending_turn("pending-click")
        await context.finish()
        assert lookup.await_count == int(confirmed)
        assert len(context.publications) == int(confirmed)
        assert events == (["searching", "partial"] if confirmed else [])
        assert context.host.adapter.calls == 0 and context.host.work.list_work_items() == []
    finally:
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


async def test_wire_abort_keeps_shared_read_owner_and_blocks_stale_output(pending_host, tmp_path, monkeypatch):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = service._store.recall
    def blocked(*args, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return original(*args, **kwargs)
        finally:
            finished.set()
    recall = Mock(side_effect=blocked)
    monkeypatch.setattr(service._store, "recall", recall)
    events = []
    async def capture(method, payload):
        events.append(dict(payload))
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        await context.handler.handle(Method.CHAT_SEND, {"text":"抹茶", "session_id":context.session_id,
            "turn_id":"abort-click", "memory_lookup":True})
        assert await asyncio.to_thread(started.wait, 2)
        await context.handler.handle(Method.CHAT_ABORT, {"turn_id":"abort-click", "stop_execution":False})
        await context.finish()
        ingress = context.manager.ingresses[context.session_id]
        shared = ingress.loop._inputs["abort-click"][1]
        assert not shared.done() and service._query._future is not None
        assert context.frames == context.publications == []
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(shared, return_exceptions=True)
        await asyncio.sleep(0)
        assert [event["status"] for event in events] == ["searching", "invalidated"]
        assert recall.call_count == 1 and context.frames == context.publications == []
    finally:
        release.set()
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


async def test_wire_session_switch_invalidates_origin_turn_without_old_answer(pending_host, tmp_path, monkeypatch):
    context = pending_host
    service = await wire_memory(context, tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    async def model(messages, **kwargs):
        started.set()
        await release.wait()
        return "PRIVATE_OLD_ANSWER"
    context.manager.query = model
    events = []
    async def capture(method, payload):
        events.append(dict(payload))
    bus.on(Method.CHAT_MEMORY_LOOKUP, capture)
    try:
        await context.handler.handle(Method.CHAT_SEND, {"text":"抹茶", "session_id":context.session_id,
            "turn_id":"switch-click", "memory_lookup":True})
        await asyncio.wait_for(started.wait(), 2)
        sm.create_session("other-synthetic-session")
        await context.finish()
        ingress = context.manager.ingresses[context.session_id]
        shared = ingress.loop._inputs["switch-click"][1]
        assert not shared.done()
        release.set()
        await asyncio.gather(shared, return_exceptions=True)
        await asyncio.sleep(0)
        assert [event["status"] for event in events] == ["searching", "partial", "invalidated"]
        assert all(event["session_id"] == context.session_id for event in events)
        assert context.publications == []
        assert "PRIVATE_OLD" not in json.dumps(sm.conversation_history.dialog)
    finally:
        release.set()
        bus.off(Method.CHAT_MEMORY_LOOKUP, capture)
        await service.close()


async def test_memory_lookup_event_reaches_websocket_without_query_or_memory_body():
    from server.ws_handler import ConnectionManager
    from test_ws_handler_disconnect import _ConcurrentWriteGuard
    manager, socket = ConnectionManager(), _ConcurrentWriteGuard()
    serving = asyncio.create_task(manager.handle_connection(socket))
    try:
        for _ in range(20):
            if manager._connections:
                break
            await asyncio.sleep(0)
        payload = {"session_id":"synthetic-session", "turn_id":"synthetic-turn", "status":"found"}
        await bus.emit(Method.CHAT_MEMORY_LOOKUP, payload)
        assert len(socket.payloads) == 1
        assert socket.payloads[0]["method"] == Method.CHAT_MEMORY_LOOKUP
        assert socket.payloads[0]["params"] == payload
    finally:
        socket.release.set()
        await serving
