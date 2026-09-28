"""Natural amendments address the existing live Work input owner."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_host.provider_types import ProviderInputDelivery
from core import session_manager as sm
import core.turn_coordinator as tc
from server.attention_request import AttentionRequestCoordinator
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_delivery import CooperativeHostDelivery
from server.cooperative_provider_loop import LoopConflict
from server.handlers.chat_handler import ChatHandler
from server.handlers.work_ledger_handler import WorkLedgerHandler
from server.work_destination_service import WorkDestinationService
from server.reference_catalog import TypedReferenceCandidate
from test_work_effect_executor import _admission, _host, _payload


@pytest.mark.parametrize("target_kind", [
    "current_id", "history_reference", "unknown", "unattached", "other", "ambiguous",
    "wrong_known", "wrong_known_missing", "source_exact",
])
async def test_live_amend_uses_existing_input_owner(tmp_path, monkeypatch, target_kind):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    scratch = tmp_path / "drafts"
    scratch.mkdir()
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    session_id = "session-active-amend"
    sm.create_session(session_id)
    create_text = "创建 acceptance-note.md，写标题，然后等待再结束。"
    text = (create_text if target_kind == "source_exact" else
        "给海战那个加个按钮。" if target_kind == "wrong_known_missing" else
        "给 Timer 加个按钮。" if target_kind == "wrong_known" else
        "再加一行“追加要求已收到”，其他步骤照旧。")
    target_id = ""
    previous_id = ""
    reference_queries = []
    publications = []
    inputs = []
    attention = AttentionRequestCoordinator()

    async with _host(tmp_path) as host:
        host.adapter.manifest = replace(host.adapter.manifest,
            capabilities=replace(host.adapter.manifest.capabilities, append_input=True))

        async def append_input(run_id, message):
            inputs.append((run_id, message))
            return ProviderInputDelivery("delivered")

        host.adapter.append_input = append_input
        host.runtime.register(host.adapter)
        work_handler = WorkLedgerHandler(host.coordinator,
            provider_input=host.runtime.append_input)

        async def query(messages, **_kwargs):
            try:
                frame = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                reference_queries.append(messages)
                candidates, complete, _ = manager.work_candidates_for_context(session_id, "")
                assert complete
                known = {candidate.entity_id:candidate.token for candidate in candidates}
                assert target_id in known
                ids = ([target_id] if target_kind in {
                        "current_id", "history_reference", "unattached", "wrong_known"}
                    else [previous_id] if target_kind == "other"
                    else [target_id, previous_id] if target_kind == "ambiguous" else [])
                return json.dumps({"references":[known[key] for key in ids]})
            if frame["source_kind"] != "user":
                return "対象の状態を確認する必要があるわ。"
            action = {"op":"work", "intent":"execute"}
            if target_id and frame["current"]["text"] == text:
                proposed_target = ("作成済みの古いメモ。" if target_kind == "wrong_known" else
                    target_id if target_kind in {
                        "current_id", "wrong_known_missing", "source_exact"} else
                    "先ほどのメモ")
                action = {"op":"work", "intent":"amend", "target":proposed_target}
            return json.dumps({"action":action, "say":"メモに反映するわ。"}, ensure_ascii=False)

        handler = ChatHandler()
        manager = CooperativeChatManager(handler, ledger=host.control_store,
            fence_scope="cooperative:active-amend", provider=host.adapter.provider_id,
            runtime=host.runtime, context_requirements={host.adapter.provider_id:
                _payload(host.project.project_id, host.adapter.provider_id).requirements},
            allocate=Mock(side_effect=AssertionError("no independent Provider context")),
            query=query, publish_factory=lambda session:CooperativeHostDelivery(
                session_id=session, display=lambda event:publications.append(event) or True),
            destination=WorkDestinationService(host.work, registry_check=lambda _path:True,
                scratch_root_provider=lambda:scratch), attention=attention)
        manager.configure_work(host.control, host.executor, input_request=work_handler.submit_input)
        manager.install()

        async def send(message, key, *, replay=False):
            accepted = await handler._handle_send({"text":message, "turn_id":key,
                "utterance_id":key, "session_id":session_id})
            assert accepted["status"] == ("replayed" if replay else "ok")
            if handler._stream_task is not None:
                await asyncio.wait_for(handler._stream_task, 5)
            return manager.ingresses[session_id].receipts[key]

        async def finish():
            if manager._work_tasks:
                await asyncio.gather(*tuple(manager._work_tasks))

        try:
            if target_kind in {"other", "ambiguous", "wrong_known"}:
                previous = await send("作成済みの古いメモ。", "previous")
                previous_id = previous["work_item_id"]
                await finish()
            host.adapter.release.clear()
            host.adapter.started.clear()
            first = await send(create_text, "create")
            assert first["state"] == "work_started"
            target_id = first["work_item_id"]
            await asyncio.wait_for(host.adapter.started.wait(), 5)
            await host.coordinator.drain_provider_facts()
            before_calls = host.adapter.calls
            if target_kind == "other":
                host.adapter.started.clear()
            candidates, complete, _ = manager.work_candidates_for_context(session_id, "")
            assert complete and any(candidate.entity_id == target_id
                and candidate.execution == "running" for candidate in candidates)
            if target_kind == "unattached":
                # Lose only the process attachment; durable exact Work identity remains.
                with monkeypatch.context() as unavailable:
                    unavailable.setattr(host.runtime, "get_run", lambda _run_id:None)
                    result = await send(text, "amend")
            else:
                result = await send(text, "amend")
            await work_handler.drain_inputs()
            if target_kind in {"current_id", "history_reference", "wrong_known", "source_exact"}:
                assert result["state"] == "work_input_accepted"
                assert result["work_item_id"] == target_id
                assert result["attempt_id"] == first["attempt_id"]
                assert result["input"]["text"] == text
                assert inputs == [(first["run_id"], text)]
                replayed = await send(text, "amend", replay=True)
                await work_handler.drain_inputs()
                assert replayed == result and inputs == [(first["run_id"], text)]
                owner_replay = await work_handler.submit_input({"input_id":"amend",
                    "work_item_id":target_id, "run_id":first["run_id"], "text":text})
                await work_handler.drain_inputs()
                assert owner_replay["replayed"] and inputs == [(first["run_id"], text)]
                assert len([event for event in publications if event["cause"] == "amend"]) == 1
                assert len([row for row in manager.ingresses[session_id].loop.history
                    if row.get("source") == "user" and row.get("input_id") == "amend"]) == 1
            elif target_kind == "other":
                assert result["state"] == "work_started"
                assert result["work_item_id"] == previous_id
                assert result["run_id"] != first["run_id"]
                assert host.runtime.get_run(first["run_id"]).status == "running"
                assert inputs == []
            else:
                expected = {"unknown":("rejected", "work_amend_target_none"),
                    "wrong_known_missing":("rejected", "work_amend_target_none"),
                    "unattached":("unknown", "work_runtime_owner_unavailable"),
                    "ambiguous":("work_amend_selection_required", None)}[target_kind]
                assert result["state"] == expected[0]
                if expected[1]:
                    assert result["reason"] == expected[1]
                if target_kind == "ambiguous":
                    assert len(attention.list_pending(session_id)[0]["options"]) == 2
                assert inputs == []
            if target_kind == "other":
                await asyncio.wait_for(host.adapter.started.wait(), 5)
            assert host.adapter.calls == before_calls + int(target_kind == "other")
            assert len(host.work.list_work_items()) == (2 if previous_id else 1)
            assert all(len(host.work.list_attempts(item.work_item_id)) ==
                (2 if target_kind == "other" and item.work_item_id == previous_id else 1)
                for item in host.work.list_work_items())
            assert manager.ingresses[session_id].loop.children == {}
            # Only an exact reference in the admitted source bypasses the typed
            # resolver. A known ID proposed by the role still needs resolution.
            assert len(reference_queries) == (0 if target_kind == "source_exact" else 1)
            if target_kind == "ambiguous":
                request = attention.list_pending(session_id)[0]
                # The resolver's first typed candidate was the active Work.
                chosen = await attention.resolve(session_id=session_id,
                    request_id=request["id"], option_id=request["options"][0]["id"])
                await work_handler.drain_inputs()
                assert chosen["ok"]
                assert manager.ingresses[session_id].receipts["amend"]["state"] == "work_input_accepted"
                assert inputs == [(first["run_id"], text)]
                assert host.adapter.calls == before_calls
                assert len(host.work.list_attempts(target_id)) == 1
        finally:
            attention.reset_for_tests()
            host.adapter.release.set()
            await finish()
            await work_handler.drain_inputs()
            await handler.close()
            await manager.close()


@pytest.mark.parametrize("change", ["binding", "run", "source", "catalog"])
async def test_active_target_resolution_rechecks_frozen_authority(change):
    text = "再加一行“追加要求已收到”，其他步骤照旧。"
    admission = _admission(suffix="active-race", text=text)
    active = {"work_item_id":"work-active", "attempt_id":"attempt-active",
        "run_id":"run-active", "effect_id":"effect-active", "runtime_attached":True}
    candidate = TypedReferenceCandidate(kind="work_item", entity_id="work-active",
        label="Original Work", scope="session_draft", execution="running")
    manager = object.__new__(CooperativeChatManager)
    manager.work_candidates_for_context = Mock(return_value=((candidate,), True, ""))
    manager.work_for_recipient = Mock(return_value={"work_item_id":"work-active"})
    manager.active_work_for_recipient = Mock(return_value=active)
    manager.work_input = AsyncMock()
    loop = SimpleNamespace(children={}, history=[], prior_messages=lambda _turn:[], trace=[], _foreground=asyncio.Lock(),
        _binding=SimpleNamespace(child_id="", token="source-token"),
        active_work=lambda context:manager.active_work_for_recipient(
            ingress.session_id, context) or {},
        _effects=SimpleNamespace(accept_no_effect=Mock()),
        _deliver=AsyncMock())
    ingress = SimpleNamespace(session_id="session-c2", loop=loop, receipts={})

    async def query(*_args, **_kwargs):
        if change == "binding":
            loop._binding.token = "new-token"
        elif change == "run":
            manager.active_work_for_recipient.return_value = {**active, "run_id":"new-run"}
        elif change == "catalog":
            manager.work_candidates_for_context.return_value = (
                (replace(candidate, execution="succeeded"),), True, "")
        return json.dumps({"references":[candidate.token]})

    manager.query = query
    receipt = {"state":"work_amend_resolution_required", "target":"先ほどのメモ",
        "source_binding_token":"source-token", "text":text + ("改写" if change == "source" else "")}
    if change == "run":
        result = await manager.resolve_work_amend_target(ingress, admission.turn_id, receipt, admission)
        assert result["state"] == "rejected" and result["reason"] == "work_run_not_active"
    else:
        with pytest.raises(ValueError if change == "source" else LoopConflict):
            await manager.resolve_work_amend_target(ingress, admission.turn_id, receipt, admission)
        loop._effects.accept_no_effect.assert_not_called()
    manager.work_input.assert_not_awaited()
    loop._deliver.assert_not_awaited()
