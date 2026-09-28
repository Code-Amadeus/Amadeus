"""An oral go-ahead retains the existing source and bounded dialogue transport."""
import asyncio
import json

from agent_host.provider_identity import with_parent_conversation_context
from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host
from test_runtime_work_planner import decision


async def test_go_ahead_keeps_prior_goal_in_existing_provider_context(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider,))
    prior = "我想做个海洋生物科普页，蓝色背景，手机上也能看。先聊聊怎么做。"
    confirmation = "好，就按刚才说的做吧。"
    suggestion = "青い背景で、生き物ごとのカードを並べましょう。スマホにも対応できるわ。"
    planning_requests = []

    async def query(messages):
        planning_requests.append(messages)
        raw = json.loads(decision(confirmation, context.manager.provider, "execute"))
        raw["decisions"][0]["payload_continuity"] = "confirmed_prior_request"
        return json.dumps(raw, ensure_ascii=False)

    async def role(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "確認したわ。"
        is_confirmation = frame["current"]["text"] == confirmation
        return json.dumps({"action": {"op": "work"} if is_confirmation else None,
            "say": "それじゃ、作り始めるわ。" if is_confirmation else suggestion},
            ensure_ascii=False)

    context.manager.query = role
    context.manager.work_planner = RuntimeWorkPlanner(
        coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    for text, turn_id in ((prior, "discuss"), (confirmation, "confirm")):
        await context.handler.send_text(text, session_id=context.session_id,
            turn_id=turn_id)
        await asyncio.wait_for(context.handler._stream_task, 5)
        if turn_id == "discuss":
            assert not planning_requests and context.host.adapter.calls == 0

    ingress = context.manager.ingresses[context.session_id]
    receipt = ingress.receipts["confirm"]
    assert receipt["state"] == "work_started"
    await asyncio.wait_for(context.host.adapter.started.wait(), 5)
    assert len(planning_requests) == context.host.adapter.calls == 1
    rendered = json.dumps(planning_requests[0], ensure_ascii=False)
    assert prior in rendered and suggestion in rendered
    request = context.host.adapter.requests[0]["request"]
    assert request.task == request.metadata["source_user_text"] == confirmation
    assert prior in request.metadata["source_user_context"]
    assert suggestion in request.metadata["source_user_context"]
    assert "それじゃ、作り始めるわ。" not in request.metadata["source_user_context"]
    assert request.metadata["turn_id"] == "confirm"
    rendered_handoff = with_parent_conversation_context(request.task,
        metadata=request.metadata, execution_provider=request.provider)
    assert confirmation in rendered_handoff
    assert prior in rendered_handoff and suggestion in rendered_handoff
    replay = await context.handler.send_text(confirmation,
        session_id=context.session_id, turn_id="confirm")
    assert replay["status"] == "replayed"
    assert len(planning_requests) == context.host.adapter.calls == 1
    await context.finish()
