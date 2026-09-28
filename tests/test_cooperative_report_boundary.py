"""Ledger reports stay independent of the receiving context's execution scope."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import json

import pytest

from agent_host.provider_contract import ProviderRequirements
from agent_host.work_ledger_store import WorkLedgerStore
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_provider_loop import ChildConversation, ContextBinding, CooperativeProviderLoop
from server.reference_catalog import candidate_catalog_from_coordinator
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.attention_request import AttentionRequestCoordinator
import asyncio


@pytest.fixture
def report_host(tmp_path, monkeypatch):
    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry", lambda _:True)
    store = WorkLedgerStore(tmp_path / "work.sqlite3")
    coordinator = WorkLedgerCoordinator(store, current_session_id=lambda:"reports")
    items = []
    for name in ("alpha", "beta"):
        path = tmp_path / name
        path.mkdir()
        project = store.create_or_get_project(path, name=name)
        item = store.create_work_item(project.project_id, title=name, workspace_path=path)
        _, attempt = store.create_operation_attempt(item.work_item_id, intent="execute",
            instruction=name, provider="fake", task=name, attempt_metadata={"session_id":"reports"})
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        items.append(item)
    query = AsyncMock(return_value='{"references":[]}')
    runtime = SimpleNamespace(get_manifest=lambda _:object(), get_run=lambda _:None)
    loop = CooperativeProviderLoop(runtime, query, Mock(side_effect=AssertionError("no allocation")),
        provider="fake", context_requirements={}, owns_runtime=False, publish=lambda _:True)
    loop._effects = SimpleNamespace(accept_no_effect=Mock())
    ingress = SimpleNamespace(loop=loop, session_id="reports", receipts=loop.receipts)
    manager = CooperativeChatManager.__new__(CooperativeChatManager)
    manager.work_control = Mock()
    manager.work_executor = SimpleNamespace(coordinator=coordinator)
    manager.work_report = AsyncMock(return_value="canonical report")
    manager.query = query
    manager.ingresses = {"reports":ingress}
    manager.attention = AttentionRequestCoordinator()
    work_calls = []
    original_handle = manager.handle_work_action

    async def handle(ingress, turn_id, receipt, admission):
        if receipt["state"] == "work_required":
            work_calls.append(receipt)
            return {"state":"work_started", "work_item_id":receipt.get("work_item_id") or "new-work"}
        return await original_handle(ingress, turn_id, receipt, admission)

    manager.handle_work_action = handle
    host = SimpleNamespace(store=store, coordinator=coordinator, items=items, loop=loop,
        manager=manager, ingress=ingress, query=query, work_calls=work_calls)
    candidates, complete, _ = candidate_catalog_from_coordinator(coordinator, "reports")
    assert complete and len([row for row in candidates if row.kind == "work_item"]) == 2
    try:
        yield host
    finally:
        coordinator.close()
        store.close()


def bind(host, *, access="write"):
    item = host.items[0]
    child = ChildConversation("bound", "Execution A", item.workspace_path, "fake",
        ProviderRequirements(workspace_access=access, workspace_ownership="caller"),
        workspace_route={"projectId":item.project_id})
    host.loop.children[child.child_id] = child
    host.loop._binding = ContextBinding(child.child_id, "bound-token")
    return child


async def test_report_resolves_user_named_timer_over_role_known_memo_alias(
        report_host, tmp_path, monkeypatch):
    host = report_host
    from server import cooperative_chat_ingress as cooperative_ingress
    original_resolver = cooperative_ingress.resolve_typed_reference
    resolver_phrases = []

    async def track_resolver(phrase, *args, **kwargs):
        resolver_phrases.append(phrase)
        return await original_resolver(phrase, *args, **kwargs)

    monkeypatch.setattr(cooperative_ingress, "resolve_typed_reference", track_resolver)

    def seed(name):
        path = tmp_path / name.lower()
        path.mkdir()
        project = host.store.create_or_get_project(path, name=name)
        item = host.store.create_work_item(project.project_id, title=name,
            workspace_path=path)
        _, attempt = host.store.create_operation_attempt(item.work_item_id,
            intent="execute", instruction=name, provider="fake", task=name,
            attempt_metadata={"session_id":"reports"})
        host.store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        return item

    memo, timer = seed("Memo"), seed("Timer")
    before = [row.to_dict() for row in host.store.list_work_items()]
    host.query.side_effect = [
        '{"action":{"op":"report","target":"Memo"},"say":"確認するわ。"}',
        json.dumps({"references":["work_item:" + timer.work_item_id]}),
    ]
    admission = SimpleNamespace(utterance_id="timer-report", pending=False)
    receipt = await host.loop.submit("Timer 那个现在怎么样？",
        input_id=admission.utterance_id, turn_admission=admission)
    result = await host.manager.handle_work_action(
        host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_reported"
    assert result["report_work_item_id"] == timer.work_item_id
    assert host.manager.work_report.call_args.args[0] == "Timer 那个现在怎么样？"
    assert host.manager.work_report.call_args.args[1]["workspace_ref"] == timer.work_item_id
    assert host.query.await_count == 2
    assert resolver_phrases == ["Timer 那个现在怎么样？"]
    assert host.work_calls == []
    assert [row.to_dict() for row in host.store.list_work_items()] == before
    assert host.ingress.receipts[admission.utterance_id] == result
    host.manager.work_report.assert_awaited_once()
    assert memo.work_item_id != timer.work_item_id


async def test_report_exact_admitted_reference_keeps_fast_path(report_host):
    host = report_host
    host.query.return_value = (
        '{"action":{"op":"report","target":"alpha"},"say":"確認するわ。"}')
    admission = SimpleNamespace(utterance_id="exact-report", pending=False)
    receipt = await host.loop.submit("beta", input_id=admission.utterance_id,
        turn_admission=admission)
    result = await host.manager.handle_work_action(
        host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_reported"
    assert result["report_work_item_id"] == host.items[1].work_item_id
    assert host.manager.work_report.call_args.args[0] == "beta"
    assert host.query.await_count == 1
    assert host.work_calls == []


async def test_report_token_plus_natural_text_uses_typed_resolver(report_host):
    host = report_host
    token = "work_item:" + host.items[1].work_item_id
    text = token + " 现在怎么样？"
    host.query.side_effect = [
        '{"action":{"op":"report","target":"alpha"},"say":"確認するわ。"}',
        json.dumps({"references":[token]}),
    ]
    admission = SimpleNamespace(utterance_id="token-natural-report", pending=False)
    receipt = await host.loop.submit(text, input_id=admission.utterance_id,
        turn_admission=admission)
    result = await host.manager.handle_work_action(
        host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_reported"
    assert result["report_work_item_id"] == host.items[1].work_item_id
    assert host.manager.work_report.call_args.args[0] == text
    assert host.query.await_count == 2
    assert host.work_calls == []


@pytest.mark.parametrize("visible", [True, False])
async def test_real_foreground_report_uses_role_delivery_while_handler_is_busy(report_host, monkeypatch, visible):
    from server import app
    from server.handlers.chat_handler import ChatHandler
    from server.cooperative_delivery import CooperativeHostDelivery
    from core.chat_runtime import ChatRuntime

    host = report_host
    handler = ChatHandler()
    handler._active_turn_id = "foreground-report"
    handler._stream_task = asyncio.current_task()
    assert handler.is_busy()
    monkeypatch.setattr(app, "output_idle_probe", lambda: not handler.is_busy())
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator",
        lambda: host.coordinator)
    narrator = SimpleNamespace(supersede_for_status_query=Mock(),
        compose_status_query_reply=AsyncMock(return_value={
            "display_text":"まだ実行中よ。", "display_language":"japanese"}),
        record_status_query_delivery=Mock())
    monkeypatch.setattr(app, "work_status_narrator", narrator)
    monkeypatch.setattr("core.chat_runtime._pre_translation_enabled", lambda:False)
    queue = asyncio.Queue()
    runtime = ChatRuntime()
    runtime.configure(pending_sentence_items=queue,
        playback_manager=SimpleNamespace(mark_turn_last_sentence=Mock()))
    display, history = AsyncMock(return_value=visible), Mock(return_value=True)

    async def speech(payload):
        return await runtime.enqueue_completed_role_text(payload["voice_text_ja"],
            turn_id=payload["turn_id"])

    delivery = CooperativeHostDelivery(session_id="reports", display=display,
        narration_sink=speech, record_display=history)
    host.loop.publish = delivery
    host.manager.work_report = app._answer_report_from_ledger
    host.query.side_effect = [
        '{"action":{"op":"report","target":"beta"},"say":"確認するわ。"}',
        json.dumps({"references":["work_item:" + host.items[1].work_item_id]}),
    ]
    admission = SimpleNamespace(utterance_id="foreground-report", pending=False)
    receipt = await host.loop.submit("现在怎么样？", input_id=admission.utterance_id,
        turn_admission=admission)
    result = await asyncio.wait_for(host.manager.handle_work_action(
        host.ingress, admission.utterance_id, receipt, admission), timeout=0.5)
    assert result["state"] == "work_reported"
    assert handler.is_busy()  # The report did not wait for its own handler to become idle.
    narrator.compose_status_query_reply.assert_awaited_once()
    assert host.query.await_count == 2  # Coordination plus typed reference; no role re-expression.
    display.assert_awaited_once()
    narrator.record_status_query_delivery.assert_called_once()
    assert narrator.record_status_query_delivery.call_args.args[2]["speech_status"] == (
        "foreground_published" if visible else "suppressed")
    if not visible:
        history.assert_not_called()
        assert queue.empty()
        assert result["report_result"] == "[report] answer pass unavailable"
        return
    history.assert_called_once()
    assert queue.qsize() == 1
    assert queue.get_nowait().turn_id == "foreground-report"
    assert delivery.receipts[0]["narration"]["accepted"] is True
    assert [row["text"] for row in host.loop.history if row["source"] == "kurisu"] == ["まだ実行中よ。"]
    assert host.work_calls == []


async def test_real_batch_report_keeps_existing_nonblocking_text_delivery(report_host, monkeypatch):
    from server import app
    from server.protocol import Method

    host = report_host
    monkeypatch.setattr("server.work_ledger_coordinator.get_work_ledger_coordinator", lambda:host.coordinator)
    monkeypatch.setattr("core.session_manager.get_current_session_id", lambda:"reports")
    history = Mock()
    monkeypatch.setattr("core.session_manager.conversation_history", history)
    narrator = SimpleNamespace(supersede_for_status_query=Mock(),
        compose_status_query_reply=AsyncMock(return_value={
            "display_text":"まだ実行中よ。", "display_language":"japanese"}),
        record_status_query_delivery=Mock())
    monkeypatch.setattr(app, "work_status_narrator", narrator)
    idle, speech, emitted = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(app, "_wait_for_output_idle", idle)
    monkeypatch.setattr(app, "host_readonly_voice_sink", speech)
    monkeypatch.setattr("server.event_bus.bus.emit", emitted)
    reply = await app._answer_report_from_ledger("betaの進捗は？",
        {"intent":"report", "subject":"work_item", "lookup_session_id":"reports",
            "workspace_ref":host.items[1].work_item_id, "_host_nonblocking_report":True})
    assert reply == "[report] answered from canonical ledger identity"
    idle.assert_not_called()
    speech.assert_not_called()
    narrator.compose_status_query_reply.assert_awaited_once()
    history.add_assistant.assert_called_once()
    emitted.assert_awaited_once()
    assert emitted.call_args.args[0] == Method.CHAT_OBSERVER_DECISION
    assert emitted.call_args.args[1]["speech_status"] == "text_only_nonblocking"


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("available", [False, True])
async def test_standalone_report_reads_outside_execution_scope_without_work(report_host, bound, available):
    host = report_host
    host.loop.runtime.get_manifest = lambda _: object() if available else None
    if bound:
        bind(host, access="read")
    before = [row.to_dict() for row in host.store.list_work_items()]
    host.query.side_effect = [
        '{"action":{"op":"report","target":"beta"},"say":"確認するわ。"}',
        json.dumps({"references":["work_item:" + host.items[1].work_item_id]}),
    ]
    admission = SimpleNamespace(utterance_id="report-input", pending=False)
    receipt = await host.loop.submit("刚才那个怎么样了？", input_id=admission.utterance_id,
        turn_admission=admission)
    assert receipt["state"] == "work_report_required"
    result = await host.manager.handle_work_action(host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_reported"
    assert result["report_work_item_id"] == host.items[1].work_item_id
    host.manager.work_report.assert_awaited_once()
    assert host.manager.work_report.call_args.args[0] == "刚才那个怎么样了？"
    assert host.manager.work_report.call_args.args[1]["workspace_ref"] == host.items[1].work_item_id
    assert "_host_nonblocking_report" not in host.manager.work_report.call_args.args[1]
    assert host.work_calls == []
    assert [row.to_dict() for row in host.store.list_work_items()] == before
    host.loop._effects.accept_no_effect.assert_called_once()
    assert host.query.await_count == 2


async def test_report_uses_typed_history_resolution_without_rewriting_source(report_host):
    host = report_host
    bind(host, access="read")
    host.loop.history.append({"source":"kurisu", "text":"betaの進捗を次に確認するわ。"})
    host.query.side_effect = [
        '{"action":{"op":"report","target":"刚才提到的那个"},"say":"確認するわ。"}',
        json.dumps({"references":["work_item:" + host.items[1].work_item_id]}),
    ]
    admission = SimpleNamespace(utterance_id="history-report", pending=False)
    receipt = await host.loop.submit("刚才那个怎么样了？", input_id=admission.utterance_id,
        turn_admission=admission)
    result = await host.manager.handle_work_action(host.ingress, admission.utterance_id, receipt, admission)
    assert result["report_work_item_id"] == host.items[1].work_item_id
    assert host.query.await_count == 2
    assert any(message["content"] == "betaの進捗を次に確認するわ。"
        for message in host.query.call_args.args[0])
    assert host.manager.work_report.call_args.args[0] == "刚才那个怎么样了？"
    assert host.work_calls == []


async def test_unknown_report_target_does_not_default_to_current_work(report_host):
    host = report_host
    bind(host)
    host.query.side_effect = [
        '{"action":{"op":"report","target":"unknown report"},"say":"確認するわ。"}',
        '{"references":[]}',
        "対象を特定できなかったわ。",
    ]
    admission = SimpleNamespace(utterance_id="unknown-report", pending=False)
    receipt = await host.loop.submit("その進捗は？", input_id=admission.utterance_id, turn_admission=admission)
    result = await host.manager.handle_work_action(host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "rejected" and result["reason"] == "work_report_target_none"
    host.manager.work_report.assert_not_called()
    assert host.work_calls == []


async def test_ambiguous_report_uses_existing_attention_selection_once(report_host):
    host = report_host
    bind(host, access="read")
    host.query.side_effect = [
        '{"action":{"op":"report","target":"那个报告"},"say":"確認するわ。"}',
        json.dumps({"references":["work_item:" + item.work_item_id for item in host.items]}),
    ]
    admission = SimpleNamespace(utterance_id="ambiguous-report", pending=False)
    receipt = await host.loop.submit("现在怎么样了？", input_id=admission.utterance_id, turn_admission=admission)
    result = await host.manager.handle_work_action(host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_report_selection_required"
    # Ingress replaces its original pending receipt while the Attention card waits.
    receipt.clear()
    receipt.update(result)
    host.manager.work_report.assert_not_called()
    pending, = host.manager.attention.list_pending("reports")
    option = next(row for row in pending["options"] if "beta" in row["label"])
    resolved = await host.manager.attention.resolve(session_id="reports",
        request_id=pending["id"], option_id=option["id"])
    assert resolved["ok"] is True
    assert host.manager.work_report.call_args.args[1]["workspace_ref"] == host.items[1].work_item_id
    assert host.manager.work_report.call_args.args[0] == "现在怎么样了？"
    await host.manager.attention.resolve(session_id="reports",
        request_id=pending["id"], option_id=option["id"])
    host.manager.work_report.assert_awaited_once()
    assert host.work_calls == []


async def test_bound_batch_resolves_each_source_clause_independently(report_host, monkeypatch):
    host = report_host
    from server import cooperative_chat_ingress as cooperative_ingress
    original_resolver = cooperative_ingress.resolve_typed_reference
    resolver_phrases = []

    async def track_resolver(phrase, *args, **kwargs):
        resolver_phrases.append(phrase)
        return await original_resolver(phrase, *args, **kwargs)

    monkeypatch.setattr(cooperative_ingress, "resolve_typed_reference", track_resolver)
    child = bind(host)
    action = {"op":"batch", "actions":[
        {"op":"work", "intent":"amend", "target":"alpha", "source":"给 beta 加个按钮"},
        {"op":"report", "target":"beta", "source":"告诉我 alpha 的状态"}]}
    host.query.side_effect = [json.dumps({"action":action,"say":"確認するわ。"}),
        json.dumps({"references":["work_item:" + host.items[1].work_item_id]}),
        json.dumps({"references":["work_item:" + host.items[0].work_item_id]})]
    admission = SimpleNamespace(utterance_id="outside-write", pending=False)
    receipt = await host.loop.submit("给 beta 加个按钮；告诉我 alpha 的状态", input_id=admission.utterance_id,
        turn_admission=admission)
    result = await host.manager.resolve_work_report_batch(host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_report_batch_started"
    work, = host.work_calls
    assert work["work_item_id"] == host.items[1].work_item_id
    assert work["child_id"] == child.child_id and work["context_revision"] == child.revision
    assert work["text"] == "给 beta 加个按钮" and work["source_user_text"] == "给 beta 加个按钮；告诉我 alpha 的状态"
    assert work["source_start"] == 0 and work["source_end"] == 11
    host.manager.work_report.assert_awaited_once()
    assert host.manager.work_report.call_args.args[0] == "告诉我 alpha 的状态"
    assert host.manager.work_report.call_args.args[1]["workspace_ref"] == host.items[0].work_item_id
    assert host.loop._binding.child_id == child.child_id
    assert host.query.await_count == 3
    assert resolver_phrases == ["给 beta 加个按钮", "告诉我 alpha 的状态"]


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("intent", ["execute", "amend"])
async def test_batch_preserves_execution_context_but_reports_another_workspace(report_host, bound, intent):
    host = report_host
    child = bind(host) if bound else None
    first = {"op":"work", "intent":intent, "source":"改好它"}
    if intent == "amend":
        first["target"] = "alpha"
    action = {"op":"batch", "actions":[first,
        {"op":"report", "target":"beta", "source":"再告诉我另一个怎么样"}]}
    coordination = json.dumps({"action":action,"say":"確認するわ。"})
    if intent == "amend":
        # The Work clause is natural, so the typed owner resolves it even when
        # the role happened to propose a known target label.
        host.query.side_effect = [coordination,
            json.dumps({"references":["work_item:" + host.items[0].work_item_id]}),
            json.dumps({"references":["work_item:" + host.items[1].work_item_id]})]
    else:
        host.query.side_effect = [coordination,
            json.dumps({"references":["work_item:" + host.items[1].work_item_id]})]
    admission = SimpleNamespace(utterance_id="batch-input", pending=False)
    receipt = await host.loop.submit("改好它；再告诉我另一个怎么样", input_id=admission.utterance_id,
        turn_admission=admission)
    result = await host.manager.resolve_work_report_batch(host.ingress, admission.utterance_id, receipt, admission)
    assert result["state"] == "work_report_batch_started"
    assert len(host.work_calls) == 1
    work = host.work_calls[0]
    assert work["child_id"] == (child.child_id if child else "")
    assert work["context_revision"] == (child.revision if child else -1)
    assert work["text"] == "改好它"
    assert work["source_user_text"] == "改好它；再告诉我另一个怎么样"
    assert work["source_start"] == 0 and work["source_end"] == 3
    assert work["work_item_id"] == (host.items[0].work_item_id if intent == "amend" else "")
    host.manager.work_report.assert_awaited_once()
    assert host.manager.work_report.call_args.args[0] == "再告诉我另一个怎么样"
    assert host.manager.work_report.call_args.args[1]["workspace_ref"] == host.items[1].work_item_id
    assert host.manager.work_report.call_args.args[1]["_host_nonblocking_report"] is True
    assert host.query.await_count == (3 if intent == "amend" else 2)
