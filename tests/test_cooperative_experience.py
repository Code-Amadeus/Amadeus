"""Shared product contracts around source-bound cooperative Work."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict
from core import session_manager as sm
import core.turn_coordinator as tc
from server.cooperative_chat_ingress import CooperativeChatManager
from server.cooperative_delivery import CooperativeHostDelivery
from server.attention_request import AttentionRequestCoordinator
from server.handlers.chat_handler import ChatHandler
from server.work_control import WorkAmendPayloadV4, WorkEffectPayloadV3
from server.work_destination_service import WorkDestinationService
from test_work_effect_executor import _host, _admission, _payload


@pytest.mark.parametrize("resolution_kind", ["allow", "deny", "drift"])
async def test_accepted_desktop_work_uses_existing_export_and_inherits_approved_files(
        tmp_path, resolution_kind):
    async with _host(tmp_path) as host:
        desktop = tmp_path / "desktop"
        desktop.mkdir()
        host.coordinator.export_service.desktop_path = desktop
        host.adapter.manifest = replace(host.adapter.manifest,
            capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
        host.runtime.register(host.adapter)
        original_run = host.adapter.run
        staged_contents = []

        async def run(request, run_id, emit):
            result = await original_run(request, run_id, emit)
            plan = request.metadata["export_plan"]
            staged = Path(plan["staging_root"]) / "index.html"
            staged_contents.append(staged.read_text(encoding="utf-8") if staged.exists() else "")
            staged.write_text("<h1>Kurisu</h1>" + ("<p>avatar</p>" if len(staged_contents) > 1 else ""),
                encoding="utf-8")
            result.session = request.session or ProviderSessionHandle(
                provider=host.adapter.provider_id, session_id=run_id, scope="work_item")
            return result

        host.adapter.run = run
        manager = object.__new__(CooperativeChatManager)
        manager.ledger, manager.work_control, manager.work_executor = (
            host.control_store, host.control, host.executor)
        manager.runtime, manager.provider = host.runtime, host.adapter.provider_id
        manager.destination = object()  # Amend preserves the selected Work destination.
        manager.context_requirements = {host.adapter.provider_id:
            _payload(host.project.project_id, host.adapter.provider_id).requirements}
        manager._work_dispatches, manager._work_tasks = {}, set()
        loop = SimpleNamespace(_foreground=asyncio.Lock(),
            _binding=SimpleNamespace(child_id="", token="unbound-token"),
            _express_and_deliver=AsyncMock(), children={}, trace=[], history=[], prior_messages=lambda _turn:[])
        ingress = SimpleNamespace(session_id="session-c2", loop=loop, receipts={})
        manager.ingresses = {ingress.session_id:ingress}
        work_id = None
        permissions = []

        async def reference_query(messages):
            assert "Add an avatar to it." in messages[-1]["content"]
            candidates, complete, _ = manager.work_candidates_for_context(ingress.session_id, "")
            assert complete
            selected = [candidate.token for candidate in candidates if candidate.entity_id == work_id]
            assert len(selected) == 1
            return json.dumps({"references":selected})

        manager.query = reference_query
        for index, text in enumerate(("Build index.html on Desktop.", "Add an avatar to it."), 1):
            suffix = f"desktop-{index}"
            admission = _admission(suffix=suffix, epoch=index+1, text=text)
            host.control.admit(admission, fence_scope="foreground-chat")
            base = replace(_payload(host.project.project_id, host.adapter.provider_id,
                suffix=suffix, source=text, task=text), external_export_target="desktop")
            assert WorkEffectPayloadV3.from_payload(base.to_payload()) == base
            if work_id is None:
                accepted = host.control.seal(admission, base)
                dispatch = await host.executor.dispatch(accepted["effect_id"])
                await asyncio.shield(dispatch.record.task_handle)
                await host.coordinator.drain_provider_facts()
                work_id = dispatch.binding["work_item_id"]
            else:
                receipt = await manager.handle_work_action(ingress, admission.turn_id,
                    {"state":"work_amend_resolution_required", "text":text, "target":work_id,
                        "source_binding_token":"unbound-token"}, admission)
                assert receipt["work_item_id"] == work_id
                sealed = json.loads(host.control_store.get_effect(receipt["effect_id"])["payload_json"])
                assert WorkAmendPayloadV4.from_payload(sealed).external_export_target == "desktop"
                await asyncio.gather(*manager._work_tasks)
                request = host.adapter.requests[-1]["request"]
                assert request.metadata["external_export"] == {"target":"desktop"}
                assert request.cwd == str(host.workspace)
                assert request.session.session_id == host.adapter.requests[0]["run_id"]
                assert loop.children == {}
            pending = host.work.list_permission_requests(work_id, status="pending")
            assert len(pending) == 1
            permissions.append(pending[0])
            if index == 1:
                assert not (desktop / "index.html").exists()
            else:
                assert (desktop / "index.html").read_text(encoding="utf-8") == "<h1>Kurisu</h1>"
            resolve = host.coordinator.resolve_permission
            identity = {"work_item_id":work_id, "attempt_id":pending[0].attempt_id}
            if index == 2 and resolution_kind == "drift":
                (desktop / "index.html").write_text("User revision", encoding="utf-8")
                with pytest.raises(WorkLedgerConflict):
                    await resolve(pending[0].request_id, allow=True, **identity)
            else:
                allowed = index == 1 or resolution_kind == "allow"
                resolution = await resolve(pending[0].request_id, allow=allowed, **identity)
                assert resolution["permission"]["status"] == ("allowed" if allowed else "denied")
                assert resolution["exportedPaths"] == ([str(desktop / "index.html")] if allowed else [])
        assert staged_contents == ["", "<h1>Kurisu</h1>"]
        assert len(host.work.list_work_items()) == 1
        assert len(host.work.list_operations(work_id)) == 2
        assert permissions[0].request_id != permissions[1].request_id
        assert host.work.get_permission_request(permissions[0].request_id).status == "allowed"
        assert (desktop / "index.html").read_text(encoding="utf-8") == {
            "allow":"<h1>Kurisu</h1><p>avatar</p>",
            "deny":"<h1>Kurisu</h1>", "drift":"User revision"}[resolution_kind]


async def test_receiving_work_survives_terminal_without_a_new_context(tmp_path):
    async with _host(tmp_path, clock=lambda:1000.0) as host:
        finished = await host.executor.execute(host.effect_id)
        manager = object.__new__(CooperativeChatManager)
        manager.ledger = host.control_store
        manager.work_control = host.control
        manager.work_executor = host.executor
        manager.ingresses = {}
        current = manager.work_for_recipient("session-c2", "")
        assert current["work_item_id"] == finished["binding"]["work_item_id"]
        assert current["execution_status"] == "succeeded"
        assessment = host.work.latest_completion(current["work_item_id"])
        assert current["completeness"] == assessment.completeness
        assert current["attention"] == assessment.attention
        assert current["work_state"] == host.work.get_work_item(current["work_item_id"]).state
        assert manager.work_for_recipient("another-session", "") is None
        assert manager.work_for_recipient("session-c2", "another-context") is None
        second_admission = _admission(suffix="second", epoch=2)
        host.control.admit(second_admission, fence_scope="foreground-chat")
        second = host.control.seal(second_admission,
            _payload(host.project.project_id, host.adapter.provider_id, suffix="second"))
        assert manager.work_for_recipient("session-c2", "")["work_item_id"] == current["work_item_id"]
        completed = await host.executor.execute(second["effect_id"])
        assert completed["binding"]["work_item_id"] != current["work_item_id"]
        assert manager.work_for_recipient("session-c2", "")["work_item_id"] == (
            completed["binding"]["work_item_id"])
        # A later UI focus change does not change the accepted receiving address.
        host.coordinator.set_focus(mode="pinned", work_item_id=current["work_item_id"])
        assert manager.work_for_recipient("session-c2", "")["work_item_id"] == (
            completed["binding"]["work_item_id"])


@pytest.mark.parametrize(("export_target", "mutation"), [
    ("", {"target":"desktop"}), ("desktop", None),
    ("desktop", {"target":"elsewhere"}), ("desktop", {"target":"desktop", "filename":"injected.html"}),
])
async def test_desktop_delivery_request_must_match_accepted_effect(tmp_path, export_target, mutation):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="export-projection", epoch=2)
        host.control.admit(admission, fence_scope="foreground-chat")
        payload = replace(_payload(host.project.project_id, host.adapter.provider_id,
            suffix="export-projection"), external_export_target=export_target)
        raw = payload.to_payload()
        assert ("external_export_target" in raw) == bool(export_target)
        effect = host.control.seal(admission, payload)
        authority = ProviderRunIntakeAuthority(effect["effect_id"])
        request = host.control.provider_request(effect["effect_id"])
        assert host.control.validate_runtime_request(authority, request) == payload
        if mutation is None:
            request.metadata.pop("external_export")
        else:
            request.metadata["external_export"] = mutation
        with pytest.raises(WorkLedgerConflict):
            host.control.validate_runtime_request(authority, request)
        assert host.control.binding(effect["effect_id"]) is None
        assert host.adapter.calls == 0


async def test_named_unknown_deliverable_is_not_forced_to_current_work(tmp_path):
    async with _host(tmp_path) as host:
        first = await host.executor.execute(host.effect_id)
        manager = object.__new__(CooperativeChatManager)
        manager.ledger, manager.work_control, manager.work_executor = (
            host.control_store, host.control, host.executor)
        manager.query = AsyncMock(return_value='{"references":[]}')
        manager.handle_work_action = AsyncMock(side_effect=AssertionError("unknown target must not dispatch"))
        loop = SimpleNamespace(children={}, history=[], prior_messages=lambda _turn:[], trace=[],
            _effects=SimpleNamespace(accept_no_effect=Mock()),
            _express_and_deliver=AsyncMock())
        ingress = SimpleNamespace(session_id="session-c2", loop=loop, receipts={})
        manager.ingresses = {ingress.session_id:ingress}
        current = manager.work_for_recipient(ingress.session_id, "")
        assert current["work_item_id"] == first["binding"]["work_item_id"]
        admission = _admission(suffix="unknown", epoch=2, text="Update unrelated.html")
        result = await manager.resolve_work_amend_target(ingress, admission.turn_id,
            {"target":"unrelated.html", "child_id":"", "source_binding_token":""}, admission)
        assert result["state"] == "rejected" and result["reason"] == "work_amend_target_none"
        manager.query.assert_awaited_once()
        manager.handle_work_action.assert_not_awaited()
        assert len(host.work.list_attempts(current["work_item_id"])) == 1


async def test_amendment_proposal_leaves_target_identity_to_work_owner(loop_host):
    loop, adapter, controls, frames, _ = loop_host
    loop.recipient_work = lambda _context: {
        "work_item_id":"work-original", "goal":"Build the profile page", "execution_status":"succeeded"}
    controls["加一个头像。"] = {"op":"work", "intent":"amend", "target":"work-original"}
    receipt = await loop.submit("加一个头像。")
    assert receipt["state"] == "work_amend_resolution_required"
    assert frames[-1]["context"]["current_work"]["work_item_id"] == "work-original"
    assert adapter.requests == [] and loop.children == {}
    controls["改一下别的。"] = {"op":"work", "intent":"amend", "target":"work-invented"}
    unresolved = await loop.submit("改一下别的。")
    assert unresolved["state"] == "work_amend_resolution_required"
    assert unresolved["target"] == "work-invented"
    assert adapter.requests == [] and loop.children == {}


@pytest.mark.parametrize("target_kind", ["current_id", "history_reference", "unknown", "ambiguous"])
async def test_installed_manager_chat_first_work_and_terminal_amend_share_one_owner(tmp_path, monkeypatch, target_kind):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    scratch = tmp_path / "drafts"
    scratch.mkdir()
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    session_id = "session-experience"
    sm.create_session(session_id)

    async with _host(tmp_path) as host:
        host.adapter.manifest = replace(host.adapter.manifest,
            capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
        host.runtime.register(host.adapter)
        original_run = host.adapter.run
        before_amend = []

        async def run(request, run_id, emit):
            result = await original_run(request, run_id, emit)
            page = Path(request.cwd) / "profile.html"
            if request.metadata["intent"] == "amend":
                before_amend.append(page.read_text(encoding="utf-8"))
            page.write_text("<h1>Kurisu</h1>" + ("<p>avatar</p>" if before_amend else ""),
                encoding="utf-8")
            result.session = request.session or ProviderSessionHandle(
                provider=host.adapter.provider_id, session_id=run_id, scope="work_item")
            return result

        host.adapter.run = run
        frames, publications, spoken, reference_queries = [], [], [], []
        attention = AttentionRequestCoordinator()
        original_work_id = ""
        amendment_text = "加一个头像，简单的 K 字母图标就好。"

        def natural_reply(text):
            return "自己紹介ページを作り始めるわ。" if text == "做一个自我介绍网页。" else "同次自然回复：" + text

        async def query(messages, **_kwargs):
            try:
                frame = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                # The existing typed-reference query uses its own catalog prompt.
                # Even a role-proposed current ID must be grounded in the admitted source.
                reference_queries.append(messages)
                assert amendment_text in messages[-1]["content"]
                assert any("自己紹介ページ" in row["content"] for row in messages[1:-1]
                    if row["role"] == "assistant")
                candidates, complete, _ = manager.work_candidates_for_context(session_id, "")
                assert complete
                known = {candidate.entity_id:candidate.token for candidate in candidates}
                assert original_work_id in known
                references = ([known[original_work_id]] if target_kind in {"current_id", "history_reference"}
                    else list(known.values()) if target_kind == "ambiguous" else [])
                return json.dumps({"references":references})
            frames.append(frame)
            if frame["source_kind"] != "user":
                assert target_kind in {"unknown", "ambiguous"}
                assert frame["current"]["state"] in {"rejected", "work_amend_selection_required"}
                return "変更する対象を確認する必要があるわ。"
            action = None
            if frame["source_kind"] == "user":
                text = frame["current"]["text"]
                if text == "做一个自我介绍网页。":
                    assert frame["context"].get("current_work") is None
                    action = {"op":"work", "intent":"execute"}
                elif text == "做另一份自我介绍网页。":
                    action = {"op":"work", "intent":"execute"}
                elif text == amendment_text:
                    current = frame["context"]["current_work"]
                    assert current["execution_status"] == "succeeded"
                    target = (current["work_item_id"] if target_kind == "current_id"
                        else "存在しない別のページ" if target_kind == "unknown" else "自己紹介ページ")
                    assert target not in text
                    action = {"op":"work", "intent":"amend", "target":target}
            return json.dumps({"say":natural_reply(frame["current"]["text"]), "action":action}, ensure_ascii=False)

        def display(event):
            if event["cause"] == "first-page" or (event["cause"] == "amend-page"
                    and target_kind in {"current_id", "history_reference"}):
                items = host.work.list_work_items()
                assert len(items) == 1
                assert len(host.work.list_attempts(items[0].work_item_id)) == (
                    1 if event["cause"] == "first-page" else 2)
            publications.append(event)
            return True

        async def voice(payload):
            spoken.append(payload)
            return {"status":"queued"}

        handler = ChatHandler()
        manager = CooperativeChatManager(handler, ledger=host.control_store,
            fence_scope="cooperative:continuity-experience", provider=host.adapter.provider_id,
            runtime=host.runtime, context_requirements={host.adapter.provider_id:
                _payload(host.project.project_id, host.adapter.provider_id).requirements},
            allocate=Mock(side_effect=AssertionError("this journey must not allocate a Provider context")),
            query=query, publish_factory=lambda session:CooperativeHostDelivery(
                session_id=session, display=display, narration_sink=voice,
                record_display=sm.append_session_message),
            destination=WorkDestinationService(host.work, registry_check=lambda _path:True,
                scratch_root_provider=lambda:scratch), attention=attention)
        manager.configure_work(host.control, host.executor)
        manager.install()

        async def send(text, key):
            accepted = await handler._handle_send({"text":text, "turn_id":key,
                "utterance_id":key, "session_id":session_id})
            assert accepted["status"] == "ok"
            await asyncio.wait_for(handler._stream_task, 5)
            return manager.ingresses[session_id].receipts[key]

        async def finish_work():
            if manager._work_tasks:
                await asyncio.gather(*tuple(manager._work_tasks))

        try:
            greeting = await send("你好。", "greeting")
            assert greeting["state"] == "no_action" and host.adapter.calls == 0
            host.adapter.release.clear()
            first = await send("做一个自我介绍网页。", "first-page")
            assert first["state"] == "work_started"
            await asyncio.wait_for(host.adapter.started.wait(), 5)
            during = await send("准备怎么做？", "during-work")
            assert during["state"] == "no_action" and host.adapter.calls == 1
            host.adapter.release.set()
            await finish_work()
            original = host.work.get_work_item(first["work_item_id"])
            original_work_id = original.work_item_id
            if target_kind == "ambiguous":
                other = await send("做另一份自我介绍网页。", "other-page")
                await finish_work()
                assert other["work_item_id"] != original_work_id
            before_calls = host.adapter.calls
            second = await send(amendment_text, "amend-page")
            if target_kind in {"unknown", "ambiguous"}:
                assert second["state"] == ("rejected" if target_kind == "unknown" else "work_amend_selection_required")
                if target_kind == "unknown":
                    assert second["reason"] == "work_amend_target_none"
                    assert attention.list_pending(session_id) == []
                else:
                    requests = attention.list_pending(session_id)
                    assert len(requests) == 1 and len(requests[0]["options"]) == 2
                assert host.adapter.calls == before_calls
                assert all(len(host.work.list_attempts(item.work_item_id)) == 1 for item in host.work.list_work_items())
                admission = host.control_store.find_admission("chat:" + session_id, "amend-page")
                with host.control_store._lock:
                    assert host.control_store._db.execute(
                        "SELECT COUNT(*) FROM control_effect_outbox WHERE root_id=? AND kind='work'",
                        (admission["root_id"],)).fetchone()[0] == 0
                assert len(reference_queries) == 1
                assert before_amend == []
                replies = [event for event in publications if event["cause"] == "amend-page"]
                assert len(replies) == 1 and replies[0]["text"] != natural_reply(amendment_text)
                assert [row["text"] for row in manager.ingresses[session_id].loop.history
                    if row.get("source") == "user" and row.get("input_id") == "amend-page"] == [amendment_text]
                assert (Path(original.workspace_path) / "profile.html").read_text(encoding="utf-8") == "<h1>Kurisu</h1>"
                return
            assert second["state"] == "work_started"
            await finish_work()
            assert second["work_item_id"] == first["work_item_id"]
            assert second["attempt_id"] != first["attempt_id"]
            assert len(host.work.list_work_items()) == 1
            assert len(host.work.list_operations(original.work_item_id)) == 2
            assert host.adapter.requests[1]["request"].session.session_id == first["run_id"]
            assert host.adapter.requests[1]["request"].cwd == original.workspace_path
            request = host.adapter.requests[1]["request"]
            assert request.task == request.metadata["source_user_text"] == amendment_text
            sealed = json.loads(host.control_store.get_effect(second["effect_id"])["payload_json"])
            payload = WorkAmendPayloadV4.from_payload(sealed)
            assert payload.task == payload.source_user_text == amendment_text
            assert payload.source_proof.selected_text(amendment_text) == amendment_text
            assert payload.work_item_id == original_work_id
            assert len(reference_queries) == 1
            assert before_amend == ["<h1>Kurisu</h1>"]
            assert (Path(original.workspace_path) / "profile.html").read_text(encoding="utf-8") == (
                "<h1>Kurisu</h1><p>avatar</p>")
            assert manager.ingresses[session_id].loop.children == {}
            assert len(frames) == 4
            for turn_id, text in (("first-page", "做一个自我介绍网页。"), ("amend-page", amendment_text)):
                replies = [event for event in publications if event["cause"] == turn_id]
                assert len(replies) == 1 and replies[0]["text"] == natural_reply(text)
                voices = [payload for payload in spoken if payload["turn_id"] == turn_id]
                assert len(voices) == 1 and voices[0]["display_text"] == replies[0]["text"]
            assert all(frame["source_kind"] != "user" or frame["context"]["id"] is None
                for frame in frames)
        finally:
            attention.reset_for_tests()
            host.adapter.release.set()
            await finish_work()
            await handler.close()
            await manager.close()


from test_cooperative_provider_loop import loop_host as loop_host
