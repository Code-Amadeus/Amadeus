"""An app result handoff addresses Work, not the current recipient hint."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from server.auip_launch import AuipLaunchCoordinator
from server.auip_control_decision import AuipControlDecisionResolver
from server.protocol import Method
from test_auip_launch import _seed_app
from test_auip_control_decision import _Runtime
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize(("selection", "current_hint"), [
    (selection, hint) for selection in
    ("first", "ambiguous", "missing", "finished", "replaced", "new_work", "uncertain")
    for hint in (True, False)] + [(selection, False) for selection in
    ("voice_confirm", "voice_discard", "superseded")])
async def test_after_work_resolves_original_request_before_binding_operation(pending_host, tmp_path, selection, current_hint):
    context = pending_host
    items = [_seed_app(context.host.work, context.host.project, tmp_path / "scratch",
        title=name, turn_id="build-" + name, terminal=False) for name in ("Memo", "Timer")]
    for _, attempt, _ in items:
        context.host.work.update_attempt(attempt.attempt_id, execution_status="running",
            metadata={"session_id":context.session_id})
    events, reference_queries, role_frames = [], [], []
    first, latest = items
    context.manager.active_work_for_recipient = lambda *_a, **_k: {
        "work_item_id":latest[0].work_item_id, "attempt_id":latest[1].attempt_id,
        "status":"running", "runtime_attached":True} if current_hint else None

    async def emit(method, payload):
        events.append((method, payload))

    launch = AuipLaunchCoordinator(artifacts=context.host.work,
        work_roster=context.host.executor.coordinator, attention=context.manager.attention, emit=emit)
    source_started, reference_started, reference_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def source_query(messages):
        source_started.set()
        if "换个话题吧。" in messages[-1]["content"]:
            return '{"action":"none"}'
        return json.dumps({"action":"engage", "timing":"after_work", "target":"", "mode":"collaborate",
            "work_relation":"independent" if selection == "new_work" else "subsumed"})
    context.manager.auip_decider = AuipControlDecisionResolver(query=source_query,
        app_runtime=_Runtime(), launch_catalog=launch,
        has_active_work=lambda _session:tuple(row[1].attempt_id for row in items))
    context.manager.auip_entry_context = lambda session:launch.render_prompt_context(session,
        language="ja", include_control_contract=False)
    async def route(attrs, **kwargs):
        kwargs.pop("user_text")
        result = await launch.route_control(attrs, **kwargs)
        if selection == "uncertain":
            raise RuntimeError("reservation acknowledgement lost")
        return result
    context.manager.auip_router = AsyncMock(side_effect=route)
    context.manager.auip_cancel_deferred = launch.cancel_deferred
    text = "便签做好了就打开吧，咱们试试。" if selection != "ambiguous" else "那个做好了就打开吧。"
    if selection == "new_work":
        text = "再做个倒计时，做好了打开让我看看。"

    async def query(messages, **_kwargs):
        try:
            frame = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            reference_queries.append(messages)
            assert text in messages[-1]["content"]
            assert text not in "\n".join(message["content"] for message in messages[:-1])
            if selection == "superseded":
                reference_started.set()
                await reference_release.wait()
            if selection in {"finished", "replaced"}:
                context.host.work.update_attempt(first[1].attempt_id, execution_status="succeeded")
            if selection == "replaced":
                context.host.work.create_attempt(first[0].work_item_id, provider="locus", task="A new operation")
            selected = [] if selection == "missing" else items if selection == "ambiguous" else [first]
            return json.dumps({"references":["work_item:" + row[0].work_item_id for row in selected]})
        role_frames.append(frame)
        if frame["source_kind"] == "user":
            if selection == "new_work":
                return json.dumps({"action":{"op":"batch", "actions":[
                    {"op":"work", "intent":"execute", "source":"再做个倒计时"},
                    {"op":"auip_after_work", "mode":"observe", "source":"做好了打开让我看看。"}]},
                    "say":"作ってから開くわ。"})
            return json.dumps({"action":None, "say":"確認するわ。"})
        return "指定された成果の受領状態を確認したわ。"
    context.manager.query = query
    if selection in {"voice_confirm", "voice_discard"}:
        assert await context.launcher.launch(text)
        turn_id = context.launcher._slot_turn_id
        await asyncio.wait_for(source_started.wait(), 3)
        context.manager.auip_router.assert_not_awaited()
        assert not launch._deferred and not reference_queries
        if selection == "voice_discard":
            await context.launcher.abandon("discarded speech")
            await context.finish()
            context.manager.auip_router.assert_not_awaited()
            assert not launch._deferred and not reference_queries
            return
        assert await context.launcher.resolve(text)
    else:
        turn_id = "open-after"
        await context.handler.send_text(text, session_id=context.session_id, turn_id=turn_id)
    if selection == "superseded":
        await asyncio.wait_for(reference_started.wait(), 3)
        ingress = context.manager.ingresses[context.session_id]
        assert not ingress.loop._foreground.locked()
        await context.handler.send_text("换个话题吧。", session_id=context.session_id, turn_id="new-chat")
        reference_release.set()
        await context.finish()
        if ingress.loop._monitors:
            await asyncio.gather(*tuple(ingress.loop._monitors), return_exceptions=True)
        context.manager.auip_router.assert_not_awaited()
        assert not launch._deferred
        assert context.host.adapter.calls == 0
        return
    await context.finish()
    if selection == "new_work":
        result = context.manager.ingresses[context.session_id].receipts["open-after"]
        assert result["state"] == "work_auip_batch_started"
        assert result["work"]["work_item_id"] not in {row[0].work_item_id for row in items}
        assert context.host.adapter.calls == 1 and reference_queries == []
        assert context.manager.auip_router.call_args.args[0]["_host_work_binding"] == "turn"
        return
    assert len(reference_queries) == 1
    assert context.host.adapter.calls == 0
    if selection in {"first", "finished", "voice_confirm"}:
        assert [frame["source_kind"] for frame in role_frames] == ["user"]
        history = context.manager.ingresses[context.session_id].loop.history
        assert [row["source"] for row in history] == ["user", "kurisu"]
    if selection == "uncertain":
        result = context.manager.ingresses[context.session_id].receipts["open-after"]
        assert result["state"] == "auip_unknown"
        reservation, = launch._deferred.values()
        assert reservation.work_item_id == first[0].work_item_id
        assert (await context.handler.send_text(text, session_id=context.session_id,
            turn_id="open-after"))["status"] == "replayed"
        context.manager.auip_router.assert_awaited_once()
        return
    if selection in {"missing", "replaced"}:
        context.manager.auip_router.assert_not_awaited()
        assert not launch._deferred and not context.manager.attention.list_pending(context.session_id)
        return
    if selection == "ambiguous":
        choice, = context.manager.attention.list_pending(context.session_id)
        chosen = next(option for option in choice["options"] if option["label"] == "Memo")
        await context.manager.attention.resolve(session_id=context.session_id,
            request_id=choice["id"], option_id=chosen["id"])
    if selection != "finished":
        reservation, = launch._deferred.values()
        assert reservation.work_item_id == first[0].work_item_id
        assert reservation.operation_id == first[1].operation_id
        assert not [p for m,p in events if m == Method.AUIP_LAUNCH_REQUESTED]
        context.host.work.update_attempt(first[1].attempt_id, execution_status="succeeded")
        await launch.on_work_updated(Method.WORK_UPDATED, {"reason":"provider.result"})
    opened_events = [p for m,p in events if m == Method.AUIP_LAUNCH_REQUESTED]
    assert len(opened_events) == 1, (events, launch.candidates(context.session_id), launch._deferred)
    opened, = opened_events
    assert opened["artifact_id"] == first[2].artifact_id
    assert context.host.work.get_attempt(latest[1].attempt_id).execution_status == "running"
    assert (await context.handler.send_text(text, session_id=context.session_id,
        turn_id=turn_id))["status"] == "replayed"
    context.manager.auip_router.assert_awaited_once()
