"""Production cooperative turns read the originating shared Session history."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock
from types import SimpleNamespace

from agent_host.provider_runtime import ProviderRuntime
from core import session_manager as sm
from server.cooperative_provider_loop import CooperativeProviderLoop
from test_cooperative_pending_turn import pending_host as pending_host


async def _capture_next_frame(context, text, turn_id):
    ingress = await context.manager._ingress_for(context.session_id)
    captured = []
    original_query = ingress.loop.query

    async def query(messages, **kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            captured.append(frame)
        return await original_query(messages, **kwargs)

    ingress.loop.query = query
    await context.handler.send_text(text, session_id=context.session_id, turn_id=turn_id)
    await context.finish()
    frame, = captured
    return ingress, frame


async def test_existing_ingress_reads_later_saved_observer_and_revised_history(pending_host):
    context = pending_host
    await context.manager._ingress_for(context.session_id)
    observer = "[WORK_OBSERVER]\n便签ページが完成したわ。全部クリアも完成したわ。"
    sm.conversation_history.add_assistant(observer)
    sm.conversation_history.add_assistant("取消前の古い説明。", turn_id="interrupt-result")
    assert sm.conversation_history.mark_last_assistant_interrupted(
        "取消後に聞こえた説明。", turn_id="interrupt-result")
    assert sm.save_session(context.session_id, enable_conversation=True) is True

    ingress, frame = await _capture_next_frame(
        context, "thanks", "after-shared-history")
    history_text = [row["text"] for row in frame["history"]]
    assert history_text.count(observer) == 1
    assert "取消後に聞こえた説明。 [interrupted by user]" in history_text
    assert "取消前の古い説明。" not in history_text
    assert all(row["text"] != "thanks" for row in frame["history"])
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []
    assert all(row["text"] != observer for row in context.publications)
    assert sum(row.get("source") == "user"
        and row.get("turn_id") == "after-shared-history"
        for row in ingress.loop.history) == 1


async def test_auip_role_and_work_planner_share_one_frozen_public_dialogue(pending_host):
    from server.work_planner import RuntimeWorkPlanner

    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    prior, late = "前の仕事は取り消されたわ。", "今回の判断中に届いた別の通知。"
    assert sm.append_session_message(context.session_id, role="assistant", content=prior, turn_id="prior")
    source, frames, app_histories, plans = Mock(wraps=ingress.loop.history_source), [], [], []
    ingress.loop.history_source = source

    def capture(**kwargs):
        app_histories.append(kwargs["prior_messages"])
        sm.append_session_message(context.session_id, role="assistant", content=late, turn_id="late")
        return None

    context.manager.auip_decider = SimpleNamespace(capture=capture)
    context.manager.auip_router = lambda *_args:None
    context.manager.auip_entry_context = None

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        sm.append_session_message(context.session_id, role="assistant",
            content="EARLY CURRENT ROLE", turn_id="shared-owners")
        return '{"action":{"op":"work"},"say":"調べてみるわ。"}'

    async def plan(messages):
        plans.append(messages)
        return '{"decisions":[]}'

    ingress.loop.query = role
    ingress.loop.work_proposals_only = True
    context.manager.work_planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=plan, provider=context.manager.provider)
    await context.handler.send_text("じゃあ、別の資料を調べて。", session_id=context.session_id,
        turn_id="shared-owners")
    await context.finish()
    assert source.call_count == 1 and len(app_histories) == len(plans) == 1
    for rendered in (json.dumps(app_histories, ensure_ascii=False),
            json.dumps(frames[0]["history"], ensure_ascii=False), json.dumps(plans, ensure_ascii=False)):
        assert prior in rendered
        assert late not in rendered and "EARLY CURRENT ROLE" not in rendered
    assert context.host.adapter.calls == 0


async def test_early_role_and_later_rejection_survive_history_and_next_turn(pending_host):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    delivery = ingress.loop.publish
    early = {"source":"kurisu", "cause":"one-turn", "text":"作ってみるわ。"}
    rejected = {"source":"kurisu", "cause":"one-turn", "text":"処理で問題が起きて、まだ着手できていないわ。"}
    assert await delivery(early)
    assert await delivery(rejected)
    assert await delivery(rejected)  # The same delivered message is idempotent.
    saved, _ = sm._read_session_history(context.session_id)
    rows = [row for row in saved.dialog if row.get("turn_id") == "one-turn"]
    assert [row["content"] for row in rows] == [early["text"], rejected["text"]]
    assert len({row["message_id"] for row in rows}) == 2
    _, frame = await _capture_next_frame(context, "thanks", "after-rejection")
    text = [row["text"] for row in frame["history"]]
    assert text.count(early["text"]) == text.count(rejected["text"]) == 1


async def test_ordinary_role_receives_shared_app_facts_without_an_action_query(pending_host, monkeypatch):
    from server.auip_runtime import AuipRuntime
    from test_auip_runtime import _manifest

    context = pending_host
    runtime = AuipRuntime()
    monkeypatch.setattr("server.auip_runtime.runtime", runtime)
    app = runtime.register(manifest=_manifest(), conversation_id=context.session_id)
    runtime.publish_state(app_session_id=app["app_session_id"], bridge_token=app["bridge_token"],
        revision=1, state={"moveCount":3})
    expected = context.manager.role_app_context_for_session(context.session_id)
    assert context.manager.role_app_context_for_session(
        context.session_id, app_session_id=app["app_session_id"]
    ) == expected
    assert context.manager.role_app_context_for_session(
        context.session_id, app_session_id="app-from-another-focus"
    ) == ""
    transition = context.manager.role_app_context_for_session(
        context.session_id, app_session_id=app["app_session_id"],
        transition_action="observe")
    assert transition.startswith("[Current AUIP transition state]\n")
    assert '"status":"active"' in transition
    assert '"engagement_mode":"observe"' in transition
    assert '"controller":{"status":"idle","reason":""}' in transition
    assert "moveCount" not in transition
    assert "latest_verified_self_action" not in transition
    assert "AUIP Interaction Briefing" not in transition
    assert context.manager.role_app_context_for_session(
        context.session_id, app_session_id="app-from-another-focus",
        transition_action="leave") == ""
    _, frame = await _capture_next_frame(context, "thanks", "ordinary-app-comment")
    assert frame["app_context"] == expected
    assert "Gomoku" in expected and "status=active" in expected
    assert '"moveCount":3' in expected
    assert "[AUIP Interaction Briefing]" in expected
    assert "auip_context" not in frame  # No current action proposal was invented.
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []

    runtime.host_leave(app_session_id=app["app_session_id"], reason="user_left")
    _, closed = await _capture_next_frame(context, "thanks", "after-app-left")
    assert "[AUIP Interaction Briefing]" not in closed.get("app_context", "")
    assert "status=active" not in closed.get("app_context", "")

    runtime.register(manifest=_manifest(), conversation_id="another-session")
    _, isolated = await _capture_next_frame(context, "thanks", "foreign-app")
    assert "status=active" not in isolated.get("app_context", "")


async def test_inactive_originating_session_does_not_read_current_other_session(pending_host):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    origin_line = "元のセッションだけの完了通知。"
    sm.conversation_history.add_assistant(origin_line)
    assert sm.save_session(context.session_id, enable_conversation=True) is True

    other = sm.create_session("other-session")
    assert other == "other-session"
    foreign_line = "別セッションだけの会話。"
    sm.conversation_history.add_assistant(foreign_line)
    assert sm.save_session("other-session", enable_conversation=True) is True

    # The normal send path activates its destination Session. Exercise the
    # read-only inactive source before that legitimate activation occurs.
    inactive_history = ingress.loop.history_source("inactive-origin-turn")
    assert origin_line in [row["text"] for row in inactive_history]
    assert foreign_line not in [row["text"] for row in inactive_history]
    assert sm.get_current_session_id() == "other-session"

    _, frame = await _capture_next_frame(
        context, "thanks", "inactive-origin-turn")
    history_text = [row["text"] for row in frame["history"]]
    assert origin_line in history_text
    assert foreign_line not in history_text
    assert all(row["text"] != "thanks" for row in frame["history"])
    assert context.host.adapter.calls == 0
    assert sm.get_current_session_id() == context.session_id


async def test_shared_history_keeps_repeated_prior_text_and_excludes_only_current_turn(pending_host):
    context = pending_host
    await context.manager._ingress_for(context.session_id)
    for turn_id in ("prior-thanks-a", "prior-thanks-b"):
        assert sm.append_session_message(context.session_id, role="user",
            content="thanks", turn_id=turn_id)
    _, frame = await _capture_next_frame(context, "thanks", "current-thanks")
    matching = [row for row in frame["history"] if row["source"] == "user"
        and row["text"] == "thanks"]
    assert [row["input_id"] for row in matching] == ["prior-thanks-a", "prior-thanks-b"]
    assert frame["current"]["turn_id"] == "current-thanks"
    assert context.host.adapter.calls == 0


async def test_one_shared_snapshot_feeds_role_and_provider_parent_context(pending_host):
    context = pending_host
    prior = "最初から共有されている依頼。"
    sm.conversation_history.add_assistant(prior, turn_id="prior-shared")
    assert sm.save_session(context.session_id, enable_conversation=True) is True
    ingress = await context.manager._ingress_for(context.session_id)
    captured = []
    entered, release = asyncio.Event(), asyncio.Event()
    source_calls = 0
    history_source = ingress.loop.history_source

    def counted_history_source(turn_id):
        nonlocal source_calls
        source_calls += 1
        return history_source(turn_id)

    ingress.loop.history_source = counted_history_source

    async def query(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "user":
            captured.append(frame)
            entered.set()
            await release.wait()
            return json.dumps({"say":"進めるわ。",
                "action":{"op":"work", "intent":"execute"}}, ensure_ascii=False)
        return "完了したわ。"

    ingress.loop.query = query
    try:
        await context.handler.send_text("Build the page", session_id=context.session_id,
            turn_id="frozen-shared-history")
        await asyncio.wait_for(entered.wait(), 2)
        late = "モデル待機中に追加された後発メッセージ。"
        sm.conversation_history.add_assistant(late, turn_id="late-shared")
        assert sm.save_session(context.session_id, enable_conversation=True) is True
        release.set()
        await context.finish()
        assert source_calls == 1
        frame, = captured
        assert prior in [row["text"] for row in frame["history"]]
        assert late not in [row["text"] for row in frame["history"]]
        assert all(row["text"] != "Build the page" for row in frame["history"])
        request = context.host.adapter.requests[0]["request"]
        assert prior in request.metadata["source_user_context"]
        assert late not in request.metadata["source_user_context"]
    finally:
        release.set()
        context.host.adapter.release.set()
        await context.finish()


async def test_standalone_loop_keeps_its_native_protocol_history():
    captured = []

    async def query(messages):
        captured.append(json.loads(messages[-1]["content"]))
        return json.dumps({"action":None, "say":"続けるわ。"}, ensure_ascii=False)

    loop = CooperativeProviderLoop(ProviderRuntime(), query,
        Mock(side_effect=AssertionError("no allocation")), provider="unavailable",
        context_requirements={}, owns_runtime=False)
    loop.history.extend([
        {"source":"user", "text":"standalone prior", "input_id":"prior"},
        {"source":"host_receipt", "state":"not_accepted", "text":"private fact"},
    ])
    try:
        receipt = await loop.submit("next", turn_id="standalone-next")
        assert receipt["state"] == "no_action"
        assert captured[0]["history"] == loop.history[:2]
    finally:
        await loop.close()
