"""Inactive application entry reuses the AUIP owner behind the foreground reply."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auip_control_decision import AuipControlDecisionResolver
from server.auip_launch import AuipLaunchCoordinator
from server.chat_role_delivery import ChatRoleDelivery
from server.cooperative_delivery import CooperativeHostDelivery
from core import session_manager as sm
from server.protocol import Method
from test_auip_control_decision import _Runtime
from test_auip_launch import _seed_app
from test_cooperative_pending_turn import pending_host as pending_host
from test_cooperative_planned_work import planned
from test_chat_role_delivery import streaming_role as streaming_role, role_loop


@pytest.fixture
def entry_host(pending_host, tmp_path, request):
    context = pending_host
    state = SimpleNamespace(role_action={"op":"auip"}, target="2048", source_action="engage",
        relation="subsumed", events=[], frames=[], captures=[], queried=asyncio.Event(),
        release=asyncio.Event(), cancelled=asyncio.Event(), block=False, change_artifact=False,
        block_route=False, route_started=asyncio.Event(), release_route=asyncio.Event(), route_failure=False)
    state.prepare = AsyncMock()
    state.release.set()
    parameter = getattr(request, "param", True)
    item, attempt, artifact = _seed_app(context.host.work, context.host.project, tmp_path / "scratch" / "apps",
        title="2048", turn_id="existing-game", with_manifest=parameter is not False)
    if parameter != "foreign":
        context.host.work.update_attempt(attempt.attempt_id, metadata={"session_id":context.session_id})
    coordinator = context.host.executor.coordinator
    coordinator.bind_session_context(context.session_id, context.host.project.project_id,
        work_item_id=item.work_item_id)

    async def emit(method, payload):
        state.events.append((method, payload))

    launch = AuipLaunchCoordinator(artifacts=context.host.work, work_roster=coordinator,
        attention=context.manager.attention, emit=emit)

    async def source_query(messages):
        if '"references"' in messages[0]["content"]:
            return json.dumps({"references":["work_item:" + item.work_item_id]
                if state.target == "2048" else []})
        state.captures.append(messages)
        if "换个话题吧。" in messages[-1]["content"]:
            return '{"action":"none"}'
        state.queried.set()
        try:
            if state.block:
                await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled.set()
            raise
        if state.source_action == "none":
            return '{"action":"none"}'
        if state.source_action == "launch":
            return json.dumps({"action":"launch", "timing":"now",
                "mode":"collaborate", "target":state.target,
                "work_relation":state.relation})
        return json.dumps({"action":"engage", "timing":"now", "mode":"collaborate",
            "target":state.target, "work_relation":state.relation})

    async def query(messages, *, on_text=None):
        frame = json.loads(messages[-1]["content"])
        state.frames.append(frame)
        if frame["source_kind"] != "user":
            return "起動の受領状態を確認したわ。"
        assert "[AUIP launchable applications]" in messages[0]["content"]
        action = None if frame["current"]["text"] == "换个话题吧。" else state.role_action
        raw = json.dumps({"action":action, "say":"いいわ、確認するわね。"}, ensure_ascii=False)
        if on_text is not None:
            await on_text(raw)
        return raw

    async def route(attrs, *, session_id, turn_id, **_kwargs):
        if state.change_artifact:
            from pathlib import Path
            Path(item.workspace_path, "auip.manifest.json").write_text("{}", encoding="utf8")
        result = await launch.route_control(attrs, session_id=session_id, turn_id=turn_id,
            prepare_work=state.prepare)
        state.route_started.set()
        if state.block_route:
            await state.release_route.wait()
        if state.route_failure:
            raise RuntimeError("response lost after launch request")
        return result

    publisher = ChatRoleDelivery()
    async def display(event):
        accepted = await publisher.publish(event)
        if accepted:
            context.publications.append(event)
        return accepted
    context.manager.publish_factory = lambda session:CooperativeHostDelivery(session_id=session,
        display=display, record_display=sm.append_session_message,
        narration_sink=lambda payload:context.spoken.append(payload) or {"status":"queued"})

    context.manager.query = query
    context.manager.configure_auip(AuipControlDecisionResolver(query=source_query,
        app_runtime=_Runtime(), launch_catalog=launch), AsyncMock(side_effect=route),
        entry_context=lambda session:launch.render_prompt_context(session,
            language="ja", include_control_contract=False))
    assert len(launch.candidates(context.session_id)) == int(parameter is not False)
    return context, state, launch, item, attempt, artifact


@pytest.mark.parametrize("role_action", [{"op":"auip"}, {"op":"work","intent":"execute"},
    {"op":"send"}, {"op":"launch","target":"role-invented-target"}, None])
async def test_natural_reopen_uses_one_exact_existing_artifact(entry_host, role_action):
    context, state, _launch, item, attempt, artifact = entry_host
    state.role_action = role_action
    text = "刚才那个 2048 再打开吧，咱们接着玩。"
    await context.handler.send_text(text, session_id=context.session_id, turn_id="reopen")
    await context.finish()
    ingress = context.manager.ingresses[context.session_id]
    assert ingress.receipts["reopen"]["state"] == "auip_entry_pending"
    assert context.host.adapter.calls == 0
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(item.work_item_id)) == 1
    event, = [p for method, p in state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert event["artifact_id"] == artifact.artifact_id
    assert event["work_item_id"] == item.work_item_id and event["mode"] == "collaborate"
    assert len(state.captures) == 1
    assert len(context.publications) == len(context.spoken) == 1
    assert context.publications[0]["cause"] == "reopen"
    assert [row["source"] for row in ingress.loop.history] == ["user", "kurisu"]
    assert (await context.handler.send_text(text, session_id=context.session_id, turn_id="reopen"))["status"] == "replayed"
    assert len(state.events) == 1 and context.host.adapter.calls == 0


@pytest.mark.parametrize("failure", ["no_action", "missing_target", "changed_artifact"])
async def test_role_entry_proposal_does_not_authorize_launch(entry_host, failure):
    context, state, *_ = entry_host
    if failure == "no_action":
        state.source_action = "none"
    elif failure == "missing_target":
        state.target = "Missing app"
    elif failure == "changed_artifact":
        state.change_artifact = True
    await context.handler.send_text("把刚才那个再打开吧。", session_id=context.session_id, turn_id="not-admitted")
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts["not-admitted"]
    assert receipt["state"] == "auip_rejected"
    assert not [p for m,p in state.events if m == Method.AUIP_LAUNCH_REQUESTED]
    assert context.host.adapter.calls == 0


async def test_independent_inactive_launch_and_work_settle_through_existing_owners(
        entry_host):
    context, state, _launch, item, _attempt, artifact = entry_host
    state.source_action = "launch"
    state.relation = "independent"
    state.role_action = {"op":"work"}
    work_clause = "再做个便签页。"
    text = work_clause + "另外把2048打开。"
    context.manager.work_planner = lambda *_args:planned(
        context.manager.provider, text, work_clause, "execute", one_off=True)
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="independent-launch-work")
        await asyncio.wait_for(context.handler._stream_task, 4)
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["independent-launch-work"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == "work_started"
        assert receipt["auip"]["state"] == "auip_entry_pending"
        request = context.host.adapter.requests[0]["request"]
        assert request.task == work_clause
        assert request.metadata["source_user_text"] == text
        event, = [payload for method, payload in state.events
            if method == Method.AUIP_LAUNCH_REQUESTED]
        assert event["artifact_id"] == artifact.artifact_id
        assert event["work_item_id"] == item.work_item_id
        assert len(context.host.work.list_work_items()) == 2
        assert [row["text"] for row in ingress.loop.history
            if row["source"] == "user"] == [text]
    finally:
        context.host.adapter.release.set()
        await context.finish()


async def test_independent_inactive_launch_keeps_ordinary_chat_and_opens_once(
        entry_host):
    context, state, _launch, item, *_ = entry_host
    state.source_action = "launch"
    state.relation = "independent"
    state.role_action = None
    text = "2048打开吧，顺便陪我聊两句。"
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="independent-launch-chat")
    await context.finish()
    ingress = context.manager.ingresses[context.session_id]
    receipt = ingress.receipts["independent-launch-chat"]
    assert receipt["state"] == "work_auip_independent"
    assert receipt["work"]["state"] == "no_action"
    assert receipt["auip"]["state"] == "auip_entry_pending"
    assert len([payload for method, payload in state.events
        if method == Method.AUIP_LAUNCH_REQUESTED]) == 1
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.publications) == 1
    assert [row["text"] for row in ingress.loop.history
        if row["source"] == "user"] == [text]


async def test_pending_voice_entry_waits_for_existing_confirmation(entry_host):
    context, state, *_ = entry_host
    text = "刚才那个再打开吧。"
    assert await context.launcher.launch(text)
    await asyncio.wait_for(state.queried.wait(), 3)
    assert state.events == [] and context.publications == [] and context.spoken == []
    assert await context.launcher.resolve(text)
    await context.finish()
    assert len([p for m,p in state.events if m == Method.AUIP_LAUNCH_REQUESTED]) == 1
    assert context.host.adapter.calls == 0


async def test_denied_pending_voice_cannot_launch_independent_inactive_app(
        entry_host):
    context, state, *_ = entry_host
    state.source_action = "launch"
    state.relation = "independent"
    state.block = True
    state.release.clear()
    text = "把2048打开吧。"
    assert await context.launcher.launch(text)
    await asyncio.wait_for(state.queried.wait(), 3)
    await context.launcher.abandon("voice_permission_denied")
    state.release.set()
    await context.finish()
    assert state.events == []
    assert context.publications == context.spoken == []
    assert context.host.adapter.calls == 0


@pytest.mark.parametrize(("source_action", "relation"), [
    ("engage", "subsumed"), ("launch", "independent")])
async def test_superseded_entry_does_not_hold_input_lock_or_launch_late(
        entry_host, source_action, relation):
    context, state, *_ = entry_host
    state.source_action = source_action
    state.relation = relation
    state.block = True
    state.release.clear()
    await context.handler.send_text("刚才那个再打开吧。", session_id=context.session_id, turn_id="old-entry")
    await asyncio.wait_for(state.queried.wait(), 3)
    loop = context.manager.ingresses[context.session_id].loop
    assert not loop._foreground.locked()
    await context.handler.send_text("换个话题吧。", session_id=context.session_id, turn_id="new-chat")
    await asyncio.wait_for(context.handler._stream_task, 3)
    await asyncio.wait_for(state.cancelled.wait(), 3)
    assert state.events == [] and context.host.adapter.calls == 0
    assert [p["cause"] for p in context.publications] == ["new-chat"]
    state.release.set()
    await context.finish()
    assert state.events == []


async def test_accepted_entry_finishes_after_new_chat_without_stale_speech(entry_host):
    context, state, *_ = entry_host
    state.block_route = True
    await context.handler.send_text("刚才那个再打开吧。", session_id=context.session_id, turn_id="accepted-entry")
    await asyncio.wait_for(state.route_started.wait(), 3)
    assert len(state.events) == 1
    await context.handler.send_text("换个话题吧。", session_id=context.session_id, turn_id="new-chat")
    await asyncio.wait_for(context.handler._stream_task, 3)
    assert [p["cause"] for p in context.publications] == ["new-chat"]
    state.release_route.set()
    ingress = context.manager.ingresses[context.session_id]
    await asyncio.gather(*tuple(ingress.loop._monitors))
    assert ingress.receipts["accepted-entry"]["state"] == "auip_entry_pending"
    assert len(state.events) == 1
    assert [p["cause"] for p in context.publications] == ["new-chat"]


async def test_uncertain_launch_handoff_keeps_one_request_without_retry(entry_host):
    context, state, *_ = entry_host
    state.route_failure = True
    text = "刚才那个再打开吧。"
    await context.handler.send_text(text, session_id=context.session_id, turn_id="unknown-entry")
    await context.finish()
    assert context.manager.ingresses[context.session_id].receipts["unknown-entry"]["state"] == "auip_unknown"
    assert len(state.events) == 1
    assert (await context.handler.send_text(text, session_id=context.session_id, turn_id="unknown-entry"))["status"] == "replayed"
    assert len(state.events) == 1


@pytest.mark.parametrize("entry_host", [False], indirect=True)
@pytest.mark.parametrize("relation", ["subsumed", "independent"])
async def test_preparation_keeps_exact_work_and_independent_request_boundary(entry_host, relation):
    context, state, _launch, item, *_ = entry_host
    state.relation = relation
    text = "刚才那个再打开吧。"
    await context.handler.send_text(text, session_id=context.session_id, turn_id="prepare-entry")
    await context.finish()
    receipt = context.manager.ingresses[context.session_id].receipts["prepare-entry"]
    assert receipt["state"] == ("auip_entry_pending"
        if relation == "subsumed" else "work_auip_independent")
    assert state.prepare.await_count == int(relation == "subsumed")
    if relation == "subsumed":
        candidate, mode = state.prepare.await_args.args
        assert candidate.work_item_id == item.work_item_id and mode == "collaborate"
        assert receipt["outcome"]["preparing"] is True
    else:
        assert receipt["work"]["state"] == "no_action"
        assert receipt["auip"]["state"] == "auip_rejected"
        assert receipt["auip"]["outcome"]["error"] == (
            "independent_prepare_requires_compound_work_authority")
    assert not [p for m,p in state.events if m == Method.AUIP_LAUNCH_REQUESTED]
    assert context.host.adapter.calls == 0  # This fixture observes the domain preparation handoff only.


@pytest.mark.parametrize("entry_host", [False], indirect=True)
async def test_independent_prepare_rejects_app_side_without_losing_main_work(
        entry_host):
    context, state, *_ = entry_host
    state.relation = "independent"
    state.role_action = {"op":"work"}
    work_clause = "再做个便签页。"
    text = work_clause + "另外把2048接入并打开。"
    context.manager.work_planner = lambda *_args:planned(
        context.manager.provider, text, work_clause, "execute", one_off=True)
    try:
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id="independent-prepare-work")
        await asyncio.wait_for(context.handler._stream_task, 4)
        ingress = context.manager.ingresses[context.session_id]
        receipt = ingress.receipts["independent-prepare-work"]
        assert receipt["state"] == "work_auip_independent"
        assert receipt["work"]["state"] == "work_started"
        assert receipt["auip"]["state"] == "auip_rejected"
        assert receipt["auip"]["outcome"]["error"] == (
            "independent_prepare_requires_compound_work_authority")
        assert state.prepare.await_count == 0
        assert context.host.adapter.calls == 1
        assert len(context.host.work.list_work_items()) == 2
        request = context.host.adapter.requests[0]["request"]
        assert request.task == work_clause
        assert request.metadata["source_user_text"] == text
        assert not [payload for method, payload in state.events
            if method == Method.AUIP_LAUNCH_REQUESTED]
    finally:
        context.host.adapter.release.set()
        await context.finish()


@pytest.mark.parametrize("entry_host", ["foreign"], indirect=True)
async def test_recent_draft_shelf_is_shared_without_changing_work_ownership(entry_host, tmp_path):
    from agent_host.work_ledger_store import WorkLedgerStore
    from server.work_ledger_coordinator import WorkLedgerCoordinator
    context, _state, launch, game, game_attempt, artifact = entry_host
    creator = context.host.work.get_attempt(game_attempt.attempt_id).metadata["session_id"]
    assert creator != context.session_id
    other, _, _ = _seed_app(context.host.work, context.host.project, tmp_path / "scratch" / "apps",
        title="Unrelated", turn_id="unrelated")
    memo, memo_attempt, _ = _seed_app(context.host.work, context.host.project, tmp_path / "scratch" / "apps",
        title="Memo", turn_id="memo", with_manifest=False)
    context.host.work.update_attempt(memo_attempt.attempt_id, metadata={"session_id":context.session_id})
    context.host.work.set_session_active_work_item(context.session_id, memo.work_item_id,
        metadata={"source":"provider_intake", "explicit_context_binding":False})
    assert {c.work_item_id for c in launch.candidates(context.session_id)} == {game.work_item_id, other.work_item_id}
    assert context.host.work.get_attempt(game_attempt.attempt_id).metadata["session_id"] == creator
    # The public recent shelf is independent from the receiving Work pointer.
    context.host.work.clear_session_active_work_item(context.session_id)
    with WorkLedgerStore(context.host.work.db_path) as reopened:
        cold = WorkLedgerCoordinator(reopened)
        shelf = {r["workItemId"] for r in cold.draft_apps(limit=5)["apps"]}
        assert {game.work_item_id, other.work_item_id} <= shelf
        assert memo.work_item_id not in shelf  # It is preparable, not a verified app yet.
    text = "刚才那个 2048 再打开吧。"
    await context.handler.send_text(text, session_id=context.session_id, turn_id="retained-entry")
    await context.finish()
    event, = [p for method, p in _state.events if method == Method.AUIP_LAUNCH_REQUESTED]
    assert event["artifact_id"] == artifact.artifact_id


async def test_recent_draft_lookup_keeps_five_without_deleting_older_files(entry_host, tmp_path, monkeypatch):
    from pathlib import Path
    context, _state, launch, old_game, _, old_artifact = entry_host
    now = context.host.work._clock()
    created = []
    for index in range(6):
        monkeypatch.setattr(context.host.work, "_clock", lambda index=index:now + index + 1)
        item, _, _ = _seed_app(context.host.work, context.host.project, tmp_path / "scratch" / "apps",
            title=f"Recent {index}", turn_id=f"recent-{index}")
        created.append(item.work_item_id)
    assert {c.work_item_id for c in launch.candidates(context.session_id)} == set(created[-5:])
    assert context.host.work.get_work_item(old_game.work_item_id) is not None
    assert Path(old_artifact.path).is_file()
    assert context.host.work.get_attempt(context.host.work.list_attempts(old_game.work_item_id)[-1].attempt_id).metadata["session_id"] == context.session_id


@pytest.mark.parametrize("current_hint", [True, False])
async def test_running_work_does_not_put_inactive_capture_before_chat(entry_host, current_hint):
    context, state, _launch, item, attempt, _artifact = entry_host
    attempt = context.host.work.create_attempt(item.work_item_id, provider="locus", task="Add a timer")
    context.host.work.update_attempt(attempt.attempt_id, execution_status="running")
    context.manager.active_work_for_recipient = lambda *_a, **_k: {
        "work_item_id":item.work_item_id, "attempt_id":attempt.attempt_id,
        "status":"running", "runtime_attached":True} if current_hint else None
    context.manager.auip_decider._has_active_work = lambda _session:(attempt.attempt_id,)
    state.role_action = None
    state.source_action = "none"
    state.block = True
    state.release.clear()
    try:
        await context.handler.send_text("今天有点累，陪我聊会儿。", session_id=context.session_id, turn_id="ordinary-chat")
        await asyncio.wait_for(state.queried.wait(), 3)
        # The real ingress must reach the role and publish without waiting for
        # the inactive-app query, even while a Work is running.
        assert any(frame["source_kind"] == "user" for frame in state.frames)
        assert any(event["cause"] == "ordinary-chat" for event in context.publications)
        assert not context.manager.ingresses[context.session_id].loop._foreground.locked()
        assert context.host.adapter.calls == 0
    finally:
        state.release.set()
        await context.finish()


async def test_explicit_browser_url_does_not_wait_for_inactive_app_capture(
        entry_host, monkeypatch):
    context, state, *_ = entry_host
    text = "打开 https://example.test/wiki。"
    state.role_action = {"op":"browser", "intent":"open",
        "target":"https://example.test/wiki"}
    state.block = True
    state.release.clear()
    planner = AsyncMock(side_effect=AssertionError(
        "an exact Browser address must not enter Work planning"))
    context.manager.work_planner = planner
    monkeypatch.setattr(context.handler, "_capture_interaction_branch_routing_lease",
        lambda session_id:{"state":"absent", "parent_session_id":session_id})

    async def start_browser(**_kwargs):
        # Ensure the inactive query has really reached its blocked await. The
        # Browser receipt must still settle without waiting for its result.
        await state.queried.wait()
        return None

    context.manager.browser_owner = SimpleNamespace(start_from_turn=start_browser)

    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="explicit-browser-url")
    await asyncio.wait_for(state.queried.wait(), 3)
    await asyncio.wait_for(context.handler._stream_task, 3)
    await asyncio.wait_for(state.cancelled.wait(), 3)

    receipt = context.manager.ingresses[context.session_id].receipts[
        "explicit-browser-url"]
    assert receipt["state"] == "browser_rejected"
    assert receipt["reason"] == "browser_entry_scope_stale"
    assert len(state.captures) == 1
    planner.assert_not_awaited()
    assert context.host.adapter.calls == 0
    state.release.set()


async def test_inactive_capture_does_not_gate_shared_first_sentence(streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role
    release, finish_capture = asyncio.Event(), asyncio.Event()
    async def capture():
        await release.wait()
        await finish_capture.wait()
        return None
    task = asyncio.create_task(capture())
    async def query(_messages, *, on_text=None):
        raw = '{"action":null,"say":"少し休もうか。ゆっくり話して。"}'
        prefix = '{"action":null,"say":"'
        await on_text(prefix)
        assert not release.is_set() and host.queue.empty()
        await on_text("少し休もうか")
        assert not release.is_set() and host.queue.empty()
        await on_text("。")
        assert release.is_set() and host.queue.qsize() == 1
        await on_text('ゆっくり話して。"}')
        assert host.queue.qsize() >= 1
        assert not task.done()
        return raw
    loop = role_loop(query, host.delivery)
    try:
        result = await loop.submit("今日は疲れた。", turn_id="reply", auip_entry={
            "prompt":"既存の起動候補はあります。", "pending":task, "release":release, "owns":lambda _:False})
        assert result["state"] == "no_action" and not task.done()
        assert host.queue.qsize() == 2
        assert not loop._foreground.locked()
    finally:
        finish_capture.set()
        await task
        await loop.close()


async def test_focused_pending_control_does_not_gate_published_first_sentence(
        streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role
    control_started, release_control = asyncio.Event(), asyncio.Event()
    first_sentence, release_role = asyncio.Event(), asyncio.Event()

    async def control():
        control_started.set()
        await release_control.wait()
        return SimpleNamespace(status="ok", action="step",
            app_session_id="focused-app", read_facets=())

    pending = asyncio.create_task(control())
    release = asyncio.Event()

    async def query(_messages, *, on_text=None):
        raw = '{"action":null,"say":"先に話し始めるわ。続きも考えている。"}'
        await on_text('{"action":null,"say":"')
        await on_text("先に話し始めるわ。")
        first_sentence.set()
        await release_role.wait()
        await on_text('続きも考えている。"}')
        return raw

    loop = role_loop(query, host.delivery)
    submission = asyncio.create_task(loop.submit("focused", turn_id="focused-pending",
        auip_entry={"prompt":"", "pending":pending, "release":release,
            "owns":lambda _decision:True, "focused_pending":True}))
    try:
        await asyncio.wait_for(control_started.wait(), 2)
        await asyncio.wait_for(first_sentence.wait(), 2)
        assert not pending.done() and not submission.done()
        assert release.is_set()
        assert host.queue.qsize() == 1
        assert host.queue.get_nowait().text == "先に話し始めるわ。"
        release_role.set()
        assert (await submission)["state"] == "no_action"
        assert not pending.done()
    finally:
        release_role.set()
        release_control.set()
        await asyncio.gather(pending, submission, return_exceptions=True)
        await loop.close()


async def test_inactive_capture_waits_through_unpunctuated_stream_then_finally_releases(
        streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role
    release = asyncio.Event()

    async def query(_messages, *, on_text=None):
        raw = '{"action":null,"say":"まだ句点がない"}'
        await on_text(raw)
        assert not release.is_set() and host.queue.empty()
        return raw

    loop = role_loop(query, host.delivery)
    try:
        result = await loop.submit("話して。", turn_id="unpunctuated",
            auip_entry={"prompt":"candidate", "release":release})
        assert result["state"] == "no_action"
        assert release.is_set()
        assert host.queue.get_nowait().text == "まだ句点がない"
    finally:
        await loop.close()


async def test_nonstreaming_empty_and_cancelled_entry_queries_always_release(
        streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role

    nonstream_release = asyncio.Event()

    async def nonstream_query(_messages):
        return '{"action":null,"say":"完了。"}'

    nonstream = role_loop(nonstream_query, host.delivery)
    try:
        assert (await nonstream.submit("nonstream", turn_id="nonstream",
            auip_entry={"prompt":"candidate", "release":nonstream_release}))["state"] == "no_action"
        assert nonstream_release.is_set()
    finally:
        await nonstream.close()

    empty_release = asyncio.Event()

    async def empty_query(_messages, *, on_text=None):
        raw = '{"action":null,"say":""}'
        await on_text(raw)
        assert not empty_release.is_set()
        return raw

    empty = role_loop(empty_query, host.delivery)
    try:
        assert (await empty.submit("empty", turn_id="empty",
            auip_entry={"prompt":"candidate", "release":empty_release}))["state"] == "no_action"
        assert empty_release.is_set()
    finally:
        await empty.close()

    cancelled_release = asyncio.Event()

    async def cancelled_query(_messages, *, on_text=None):
        await on_text('{"action":null,"say":"途中')
        assert not cancelled_release.is_set()
        raise asyncio.CancelledError

    cancelled = role_loop(cancelled_query, host.delivery)
    try:
        with pytest.raises(asyncio.CancelledError):
            await cancelled.submit("cancel", turn_id="cancel",
                auip_entry={"prompt":"candidate", "release":cancelled_release})
        assert cancelled_release.is_set()
    finally:
        await cancelled.close()


async def test_no_speech_role_stream_does_not_wait_for_a_queue_signal(
        streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role
    release = asyncio.Event()
    delivery = CooperativeHostDelivery(session_id="A",
        display=host.display.publish, partial_display=host.display.publish_partial,
        allows=host.display.allows,
        role_stream_factory=lambda cause, gui_callback=None,
            auip_background_capture_release=None:host.runtime.begin_role_text_stream(
                turn_id=cause, speech=False, gui_callback=gui_callback,
                auip_background_capture_release=auip_background_capture_release))

    async def query(_messages, *, on_text=None):
        raw = '{"action":null,"say":"声には出さない。"}'
        await on_text(raw)
        assert release.is_set()
        return raw

    loop = role_loop(query, delivery)
    try:
        assert (await loop.submit("silent", turn_id="silent",
            auip_entry={"prompt":"candidate", "release":release}))["state"] == "no_action"
        assert host.queue.empty()
    finally:
        await loop.close()


async def test_unsupported_stream_prefix_releases_at_query_end_and_uses_completed_delivery(
        streaming_role, monkeypatch):
    from server.handlers.chat_handler import ChatHandler
    monkeypatch.setattr("server.chat_role_delivery.sm.get_current_session_id", lambda:"A")
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    host = streaming_role
    release = asyncio.Event()

    async def query(_messages, *, on_text=None):
        raw = '{"say":"順序が違う。","action":null}'
        await on_text(raw)
        assert not release.is_set() and host.queue.empty()
        return raw

    loop = role_loop(query, host.delivery)
    try:
        assert (await loop.submit("fallback", turn_id="unsupported-prefix",
            auip_entry={"prompt":"candidate", "release":release}))["state"] == "no_action"
        assert release.is_set()
        assert host.queue.get_nowait().text == "順序が違う。"
        host.narration.assert_awaited_once()
    finally:
        await loop.close()
