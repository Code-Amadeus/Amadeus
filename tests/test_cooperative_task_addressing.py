"""Natural continuation preserves the selected task's native context after restart."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import session_manager as sm
from server.attention_request import AttentionRequestCoordinator
from server.cooperative_chat_ingress import CooperativeChatManager
from server.reference_clarification import TypedReferenceResolution
from server.turn_admission import capture_turn_admission
from test_cooperative_context_recovery import host_factory as host_factory
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned
from test_cooperative_work_conversation import work_conversation_host as work_conversation_host


def admitted_address(host, text, turn_id):
    admission = capture_turn_admission(utterance_id=turn_id, turn_id=turn_id,
        session_id=host.ingress.session_id, transcript=text,
        input_source="text", authority_mode="turn_decision")
    assert admission is not None
    opened = host.ledger.open_admission(root_id=admission.root_id,
        source_scope=admission.dialogue_source_scope,
        fence_scope="foreground-chat", utterance_id=admission.utterance_id,
        authority_mode="turn_decision",
        transcript_hash=admission.transcript_hash)
    return replace(admission, chat_epoch=opened["admission"]["chat_epoch"])


@pytest.mark.parametrize("mode", ["known", "cold", "default", "ambiguous", "unknown",
    "same_context_runs", "missing_source", "unknown_token", "active_work", "work_catalog_unavailable",
    "implicit_send", "original_goal", "active_work_send", "wrong_known", "wrong_known_missing", "source_exact"])
async def test_task_address_resumes_exact_context_after_restart(host_factory, mode, monkeypatch):
    host = host_factory()
    source_a, source_b = "检查当前目录。", "检查 Python 版本后等待 180 秒。"
    if mode == "original_goal":
        shared = ("我想先把这台电脑的开发环境理一下，省得过两天安装东西时又发现版本对不上。"
            "你先帮我看看就好，别升级，也别卸载什么。以前装的那些项目还要继续用，"
            "我不想因为这次检查把它们弄坏。要是你看到有几个版本同时装着，就把你实际检查到的"
            "那个告诉我，没看到的别猜。结果不用写成报告，也不用给我生成文件，直接在这儿说一下就行。"
            "慢一点没关系，别为了这次检查改动我现有的设置。")
        source_a, source_b = shared + "现在先看看 Node 是什么版本。", shared + "现在先看看 Python 是什么版本。"
        assert source_a[:160] == source_b[:160] and len(source_b) < 400
    try:
        first = await host.send(source_a, "directory")
        await host.loop.wait()
        other = host.loop._create_context("Another context", host.adapter.provider_id)
        host.loop.bind_context(other.child_id)
        if mode in {"same_context_runs", "original_goal"}:
            await host.send(source_b, "version-earlier")
            await host.loop.wait()
            # This is an explicit continuation, not a new unlinked execution
            # that happens to reuse the same native context or source wording.
            seed_manager = object.__new__(CooperativeChatManager)
            seed_manager.ledger, seed_manager.runtime, seed_manager.work_executor = host.ledger, host.runtime, None
            host.ingress.work_request = seed_manager.handle_work_action
            host.loop.task_contexts = lambda:seed_manager.task_context_candidates(host.ingress)
            candidates, _, _, targets = seed_manager.task_context_candidates(host.ingress)
            task_token = next(candidate.token for candidate in candidates
                if targets[candidate.token]["child_id"] == other.child_id)
            async def seed_query(messages):
                try:
                    frame = json.loads(messages[-1]["content"])
                except json.JSONDecodeError:
                    assert "再帮我看一眼吧。" in messages[-1]["content"]
                    return json.dumps({"references":[task_token]})
                return (json.dumps({"action":{"op":"send_to", "target":task_token}, "say":"好。"})
                    if frame["source_kind"] == "user" else "好。")
            seed_manager.query = host.loop.query = seed_query
        host.adapter.release.clear()
        host.adapter.started.clear()
        second = await host.send("再帮我看一眼吧。" if mode == "original_goal" else source_b, "version")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        await host.runtime.cancel(second["run_id"])
        await host.loop.wait()
        original = host.loop.get_context(second["child_id"])
        native, workspace = original.native_session, original.workspace
        host.loop.bind_context(second["child_id"] if mode in {"default", "active_work_send"} else first["child_id"])
        if mode == "cold":
            preview_id = host.loop._create_context("Unrelated", host.adapter.provider_id).child_id
        binding = (host.loop.bound_context_id, host.loop._binding.token)
        ids = {row["context_id"] for row in host.loop.context_catalog()}
    finally:
        await host.close()


    host = host_factory(allow_allocate=False)
    manager = object.__new__(CooperativeChatManager)
    manager.ledger, manager.runtime, manager.work_executor = host.ledger, host.runtime, None
    manager.attention = AttentionRequestCoordinator()
    if mode == "work_catalog_unavailable":
        # Provider-context lookup has no dependency on the unrelated Work catalog.
        manager.work_executor = SimpleNamespace()
    host.ingress.work_request = manager.handle_work_action
    host.loop.task_contexts = lambda:manager.task_context_candidates(host.ingress)
    reference_queries, frames = [], []
    text = "刚才的 Python 版本检查继续，查完不用再等 180 秒，直接告诉我结果。目录检查保持原样。"
    if mode == "wrong_known_missing":
        text = "海战那个接着弄吧。"
    elif mode == "unknown_token":
        text = "execution:missing"
    elif mode == "source_exact":
        text = source_b

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            candidates, _, _, targets = manager.task_context_candidates(host.ingress)
            selected = [candidate for candidate in candidates
                if targets[candidate.token]["child_id"] == second["child_id"]]
            if mode == "ambiguous":
                selected = list(candidates)
            elif mode in {"unknown", "wrong_known_missing"}:
                selected = []
            assert text in messages[-1]["content"]
            return json.dumps({"references":[candidate.token for candidate in selected]})
        frames.append(frame)
        if frame["source_kind"] != "user":
            return "確認したわ。"
        candidates, _, _, targets = manager.task_context_candidates(host.ingress)
        token = next((candidate.token for candidate in candidates
            if targets[candidate.token]["child_id"] == second["child_id"]), "")
        action = {"op":"send_to", "target":token if mode == "known" else
            "execution:missing" if mode == "unknown_token" else "刚才的 Python 版本检查"}
        if mode == "implicit_send":
            action = {"op":"send"}
        elif mode in {"wrong_known", "wrong_known_missing"}:
            # Real model failure: a valid default-task token was proposed for
            # another requested task, including a task absent from the catalog.
            action = {"op":"send_to", "target":next(candidate.token for candidate in candidates
                if targets[candidate.token]["child_id"] == first["child_id"])}
        elif mode == "active_work_send":
            action = {"op":"send", "target":token}
        return json.dumps({"action":action, "say":"バージョン確認を続けるわ。"})

    manager.query = host.loop.query = query
    if mode in {"active_work", "active_work_send"}:
        host.loop.active_work = lambda context_id:({"work_item_id":"work-B", "run_id":"work-run-B"}
            if context_id == second["child_id"] else None)
        manager._handle_work_input = AsyncMock(return_value={"state":"work_input_accepted"})
    try:
        assert second["child_id"] not in host.loop.children
        if mode == "original_goal":
            # The bounded dialogue can lose the initial request while the Host
            # still retains its admitted source in Session storage.
            host.loop.history.clear()
        if mode == "cold":
            host.loop._idle_context_budget = 1
            # An unrelated retained context may occupy the bounded preview.
            host.loop.get_context(preview_id)
        if mode == "missing_source":
            host.loop.history[:] = [item for item in host.loop.history if item.get("input_id") != "version"]
            history, changed = sm._read_session_history(host.ingress.session_id)
            history.dialog[:] = [item for item in history.dialog if item.get("turn_id") != "version"]
            monkeypatch.setattr(sm, "_read_session_history", lambda _session:(history, changed))
        result = await host.send(text, "continue-version")
        if mode == "ambiguous":
            assert result["state"] == "task_address_selection_required"
            assert host.adapter.requests == []
            request, = manager.attention.list_pending(host.ingress.session_id)
            option = next(option for option in request["options"] if option["label"] == source_b)
            selected = await manager.attention.resolve(session_id=host.ingress.session_id,
                request_id=request["id"], option_id=option["id"])
            assert selected["ok"]
            result = host.ingress.receipts["continue-version"]
        if mode in {"unknown", "missing_source", "unknown_token", "wrong_known_missing"}:
            assert result["state"] == "rejected"
            assert result["reason"] == "task_address_target_" + ("incomplete" if mode == "missing_source" else "none")
            assert host.adapter.requests == []
        elif mode in {"active_work", "active_work_send"}:
            assert result["state"] == "rejected" and result["reason"] == "addressed_task_busy"
            manager._handle_work_input.assert_not_awaited()
            assert host.adapter.requests == []
        else:
            assert result["state"] == "started" and result["child_id"] == second["child_id"]
            await host.loop.wait()
            assert len(host.adapter.requests) == 1
            assert host.adapter.requests[0].task == text
            assert host.adapter.requests[0].session == native
            assert host.adapter.requests[0].cwd == workspace
            assert (await host.send(text, "continue-version"))["status"] == "replayed"
            assert len(host.adapter.requests) == 1
        assert (host.loop.bound_context_id, host.loop._binding.token) == binding
        assert {row["context_id"] for row in host.loop._state.load_catalog()[1]} == ids
        assert len(reference_queries) == (0 if mode in {"source_exact", "missing_source", "unknown_token"} else 1)
        frame = next(frame for frame in frames if frame["source_kind"] == "user")
        references = [item["task"] for item in [frame["context"], *frame["retained_contexts"]]
            if item.get("task")]
        assert "task_contexts" not in frame and "task_contexts_complete" not in frame
        if mode == "cold":
            assert frame["retained_contexts_complete"] is False
            assert not any(row["goal"] == source_b for row in references)
        if mode == "known":
            assert any(row["goal"] == source_b for row in references)
        if mode == "original_goal":
            assert {row["goal"] for row in references} == {source_a, source_b}
            candidates = manager.task_context_candidates(host.ingress)[0]
            assert len({item.label for item in candidates}) == 1
    finally:
        await host.close()


async def test_preresolved_unique_task_skips_query_and_revalidates_identity(
        host_factory):
    host = host_factory()
    try:
        first = await host.send("检查目录。", "pre-first")
        await host.loop.wait()
        second = await host.send("检查版本。", "pre-second")
        await host.loop.wait()
        manager = host.ingress.work_request.__self__
        manager.query = AsyncMock(side_effect=AssertionError(
            "pre-resolved address must not query again"))
        catalog = manager.task_context_candidates(host.ingress)
        candidates, complete, _, targets = catalog
        assert complete
        candidate = next(row for row in candidates
            if targets[row.token]["child_id"] == first["child_id"])
        text, turn_id = "目录检查用了什么命令？", "pre-resolved-unique"
        admission = admitted_address(host, text, turn_id)
        receipt = {"state":"task_address_resolution_required", "text":text,
            "input_id":turn_id, "parent_context":"",
            "coordination_say":"確認するわ。",
            "source_binding_context_id":host.loop.bound_context_id,
            "source_binding_token":host.loop._binding.token}
        before = len(host.adapter.requests)

        result = await manager._continue_resolved_task_address(
            host.ingress, turn_id, receipt, admission,
            TypedReferenceResolution(status="unique", candidates=(candidate,)),
            catalog=catalog)
        await host.loop.wait()

        assert result["state"] == "started"
        assert result["child_id"] == first["child_id"] == second["child_id"]
        assert len({row.token for row in candidates}) >= 2
        assert len(host.adapter.requests) == before + 1
        assert host.adapter.requests[-1].task == text
        manager.query.assert_not_awaited()
    finally:
        await host.close()


async def test_preresolved_ambiguity_uses_attention_and_missing_target_fails_closed(
        host_factory):
    host = host_factory()
    try:
        first = await host.send("检查目录。", "amb-first")
        await host.loop.wait()
        second = await host.send("检查版本。", "amb-second")
        await host.loop.wait()
        manager = host.ingress.work_request.__self__
        manager.query = AsyncMock(side_effect=AssertionError(
            "pre-resolved address must not query again"))
        catalog = manager.task_context_candidates(host.ingress)
        candidates, complete, _, targets = catalog
        assert complete
        selected = tuple(row for row in candidates
            if targets[row.token]["child_id"] in {first["child_id"], second["child_id"]})
        assert len(selected) == 2
        text, turn_id = "刚才两个检查都解释一下。", "pre-resolved-ambiguous"
        admission = admitted_address(host, text, turn_id)
        receipt = {"state":"task_address_resolution_required", "text":text,
            "input_id":turn_id, "parent_context":"",
            "source_binding_context_id":host.loop.bound_context_id,
            "source_binding_token":host.loop._binding.token}
        before = len(host.adapter.requests)

        ambiguous = await manager._continue_resolved_task_address(
            host.ingress, turn_id, receipt, admission,
            TypedReferenceResolution(status="ambiguous", candidates=selected),
            catalog=catalog)
        assert ambiguous["state"] == "task_address_selection_required"
        request, = manager.attention.list_pending(host.ingress.session_id)
        assert request["id"] == ambiguous["attention_request_id"]
        assert len(request["options"]) == 2
        assert len(host.adapter.requests) == before

        none_text, none_turn = "存在しない対象に送って。", "pre-resolved-none"
        none_admission = admitted_address(host, none_text, none_turn)
        none_receipt = {**receipt, "text":none_text, "input_id":none_turn}
        none = await manager._continue_resolved_task_address(
            host.ingress, none_turn, none_receipt, none_admission,
            TypedReferenceResolution(status="none"),
            catalog=((), True, "", {}))
        assert none["state"] == "rejected"
        assert none["reason"] == "task_address_target_none"

        missing_text, missing_turn = "消えた対象に送って。", "pre-resolved-missing"
        missing_admission = admitted_address(host, missing_text, missing_turn)
        missing_receipt = {**receipt, "text":missing_text, "input_id":missing_turn}
        child = host.loop.get_context(first["child_id"])
        host.loop._save_child(child, closed=True)
        missing = await manager._continue_resolved_task_address(
            host.ingress, missing_turn, missing_receipt, missing_admission,
            TypedReferenceResolution(status="unique", candidates=(selected[0],)),
            catalog=catalog)
        assert missing["state"] == "rejected"
        assert missing["reason"] == "addressed_context_unavailable"
        assert len(host.adapter.requests) == before
        manager.query.assert_not_awaited()
    finally:
        await host.close()


async def test_default_resolution_rejects_provider_identity_changed_during_query(
        host_factory):
    host = host_factory()
    try:
        first = await host.send("检查目录。", "identity-first")
        await host.loop.wait()
        second = await host.send("检查版本。", "identity-second")
        await host.loop.wait()
        manager = host.ingress.work_request.__self__
        candidates, complete, _, targets = manager.task_context_candidates(host.ingress)
        assert complete
        candidate = candidates[0]
        text = "把目录检查的结论再解释一下。"

        async def query(messages):
            try:
                frame = json.loads(messages[-1]["content"])
            except json.JSONDecodeError:
                child = host.loop.get_context(targets[candidate.token]["child_id"])
                child.provider = "changed-provider"
                return json.dumps({"references":[candidate.token]})
            if frame["source_kind"] == "user":
                return json.dumps({"action":{"op":"send_to", "target":"目录检查"},
                    "say":"確認するわ。"}, ensure_ascii=False)
            return "確認できなかったわ。"

        host.loop.query = manager.query = query
        before = len(host.adapter.requests)
        result = await host.send(text, "identity-changed")
        assert result["state"] == "rejected"
        assert result["reason"] == "addressed_context_unavailable"
        assert len(host.adapter.requests) == before
        assert first["child_id"] == second["child_id"]
    finally:
        await host.close()


async def test_preresolved_terminal_work_question_reuses_native_without_new_work(
        work_conversation_host):
    context, native = work_conversation_host
    text = "做一份终态报告。"

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"action":{"op":"work"}, "say":"作るわ。"},
            ensure_ascii=False) if frame["source_kind"] == "user" else "できたわ。")

    context.manager.query = query
    context.manager.work_planner = lambda _ingress, _turn, receipt, _admission: planned(
        context.manager.provider, receipt["text"], receipt["text"],
        "execute", one_off=True)
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="terminal-work-create")
    await asyncio.wait_for(context.handler._stream_task, 4)
    await context.finish()
    ingress = context.manager.ingresses[context.session_id]
    work_id = ingress.receipts["terminal-work-create"]["work_item_id"]
    catalog = context.manager.task_context_candidates(ingress)
    candidate = next(row for row in catalog[0]
        if row.kind == "work_item" and row.entity_id == work_id)
    question, turn_id = "这份报告用了什么方法？", "pre-resolved-work-question"
    admission = capture_turn_admission(utterance_id=turn_id, turn_id=turn_id,
        session_id=context.session_id, transcript=question,
        input_source="text", authority_mode="turn_decision")
    assert admission is not None
    opened = context.manager.ledger.open_admission(root_id=admission.root_id,
        source_scope=admission.dialogue_source_scope,
        fence_scope="foreground-chat", utterance_id=turn_id,
        authority_mode="turn_decision", transcript_hash=admission.transcript_hash)
    admission = replace(admission,
        chat_epoch=opened["admission"]["chat_epoch"])
    receipt = {"state":"task_address_resolution_required", "text":question,
        "input_id":turn_id, "parent_context":"",
        "source_binding_context_id":ingress.loop.bound_context_id,
        "source_binding_token":ingress.loop._binding.token}
    before_items = len(context.host.work.list_work_items())
    before_attempts = len(context.host.work.list_attempts(work_id))
    before_requests = len(native.requests)
    context.manager.query = AsyncMock(side_effect=AssertionError(
        "pre-resolved Work address must not query again"))

    result = await context.manager._continue_resolved_task_address(
        ingress, turn_id, receipt, admission,
        TypedReferenceResolution(status="unique", candidates=(candidate,)),
        catalog=catalog)
    await ingress.loop.wait()

    assert result["state"] == "started"
    assert len(native.requests) == before_requests + 1
    assert native.requests[-1].metadata["cooperative_work_item_id"] == work_id
    assert len(context.host.work.list_work_items()) == before_items
    assert len(context.host.work.list_attempts(work_id)) == before_attempts
    context.manager.query.assert_not_awaited()
