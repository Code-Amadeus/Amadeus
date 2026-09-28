"""Control application uncertainty is not a no-execution receipt.

The real dispatcher schedules a fake Host handler; no Provider/model is invoked.
Failures are injected on either side of that actual scheduling boundary.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.chat_runtime import ChatRuntime, _TurnState
from server import host_action_dispatcher as dispatcher


async def _application_failure(stage, auip_decision=None, *, report_only=False):
    from server import app

    attrs = {"provider": "codex", "intent": "execute", "task": "create the requested game"}
    if report_only:
        attrs = {"provider": "codex", "intent": "report", "subject": "work_item"}
    evidence = SimpleNamespace(decision_status="ok", outcome="agree", canonical_actions=(attrs,), notes=(), reason="")
    owner = ChatRuntime()
    owner.configure(
        control_proposal_observer=SimpleNamespace(capture=lambda _batch: evidence),
        control_proposal_authority=True,
        control_authority_block_callback=app._announce_control_authority_block,
    )
    state = _TurnState(gui_callback=None, turn_id="application-truth", question=attrs.get("task", "report progress"), session_id="application-session")
    handler_calls, batches, notes, app_calls = [], [], [], []
    if auip_decision is not None:
        owner.configure(auip_control_callback=lambda attrs, **_kwargs: app_calls.append(dict(attrs)))
        state.auip_decision_result = auip_decision
        state.auip_decision_task = asyncio.create_task(asyncio.sleep(0))

    async def handler(task, _attrs):
        handler_calls.append(task)
        return "fake Host handler completed"

    def record(actions, **kwargs):
        if stage == "unavailable":
            raise dispatcher.HostDispatchUnavailable("fake unconfigured Host")
        batch = dispatcher.record_actions(actions, delegate_handler=handler, **kwargs)
        batches.append(batch)
        if stage == "handoff_exception":
            raise RuntimeError("lost callback acknowledgement after scheduling")
        return batch

    with (
        patch("core.chat_runtime.record_actions", side_effect=record),
        patch.object(owner, "_start_auip_decision_for_work"),
        patch.object(owner, "_remember_taskless_focus", side_effect=None if stage == "success" else RuntimeError("post-handoff bookkeeping failed")),
        patch("server.work_context.add_work_note", side_effect=notes.append),
        patch("server.event_bus.bus.emit", new=AsyncMock()),
    ):
        owner._consume_stream_chunk(state, '[DELEGATE provider="codex" intent="execute" task="create the requested game"]')
        await owner._wait_for_control_authority(state)
        await asyncio.gather(*batches)
        if auip_decision is not None:
            await owner._wait_for_auip_controls(state)
    return state, handler_calls, notes, app_calls


@pytest.mark.parametrize("stage", ["after_handoff", "handoff_exception"])
def test_scheduled_dispatch_is_not_relabelled_as_nothing_started(stage):
    state, calls, notes, _ = asyncio.run(_application_failure(stage))
    assert calls == ["create the requested game"]
    assert len(notes) == 1
    note = notes[0]
    assert note["metadata"].get("execution_uncertain") is True
    assert "execution_started" not in note["metadata"]
    assert note["metadata"]["decision_outcome"] == "application_uncertain"
    assert "no Provider work" not in note["summary"]
    assert "Nothing was started" not in str(note["signals"])
    assert len(state.control_effective_actions) == 1
    assert "[DELEGATE" in state.history_response


def test_known_pre_handoff_unavailability_still_proves_not_started():
    state, calls, notes, _ = asyncio.run(_application_failure("unavailable"))
    assert calls == []
    assert len(notes) == 1
    assert notes[0]["metadata"]["execution_started"] is False
    assert not notes[0]["metadata"].get("execution_uncertain")
    assert state.control_effective_actions == []
    # The recorded request remains historical evidence, not an acceptance.
    assert "[DELEGATE" in state.history_response


@pytest.mark.parametrize("stage", ["after_handoff", "handoff_exception"])
def test_uncertain_work_cannot_fall_through_to_another_auip_preparation(stage):
    from server.auip_control_decision import AuipControlDecision

    decision = AuipControlDecision(status="ok", action="prepare", preparation_work_item_id="work_game")
    state, calls, notes, app_calls = asyncio.run(_application_failure(stage, decision))
    assert len(calls) == 1
    assert notes[0]["metadata"]["execution_uncertain"] is True
    assert app_calls == []
    assert state.auip_decision_dispatched is False


def test_confirmed_scheduling_keeps_existing_turn_bound_auip_continuation():
    from server.auip_control_decision import AuipControlDecision

    decision = AuipControlDecision(status="ok", action="prepare", preparation_work_item_id="work_game")
    state, calls, notes, app_calls = asyncio.run(_application_failure("success", decision))
    assert len(calls) == 1
    assert notes == []
    assert len(app_calls) == 1
    assert app_calls[0]["action"] == "launch"
    assert app_calls[0]["after"] == "work"
    assert app_calls[0]["_host_work_binding"] == "turn"
    assert app_calls[0]["_host_work_item_id"] == "work_game"
    assert state.auip_decision_dispatched is True


def test_uncertain_work_does_not_cancel_an_independent_app_step():
    from server.auip_control_decision import AuipControlDecision

    decision = AuipControlDecision(status="ok", action="step", work_relation="independent", app_session_id="app_other")
    _, calls, notes, app_calls = asyncio.run(_application_failure("handoff_exception", decision))
    assert len(calls) == 1 and len(notes) == 1
    assert len(app_calls) == 1
    assert app_calls[0]["action"] == "step"


@pytest.mark.parametrize("attrs", [
    {"action": "prepare", "_host_preparation_work_item_id": "work_game"},
    {"action": "PREPARE", "_host_preparation_work_item_id": "work_game"},
    {"action": " prepare ", "_host_preparation_work_item_id": "work_game"},
    {"action": "launch", "after": "work", "_host_work_binding": "turn"},
    {"action": "launch", "after": "WORK", "_host_work_binding": "turn"},
    {"action": "launch", "after": " work ", "_host_work_binding": "turn"},
])
def test_inline_fallback_cannot_bypass_the_unknown_work_boundary(attrs):
    async def run():
        from llm.stream_parser import StreamTagParser

        owner = ChatRuntime()
        callback = AsyncMock()
        owner.configure(auip_control_callback=callback)
        state = _TurnState(gui_callback=None, turn_id="unknown-inline", session_id="test")
        state.work_handoff_uncertain = True
        state.control_effective_actions = [{"type": "DELEGATE", "attrs": {"intent": "execute", "task": "build"}}]
        state.auip_decision_result = SimpleNamespace(status="unavailable")
        raw = "[AUIP " + " ".join(f'{key}="{value}"' for key, value in attrs.items()) + "]"
        _, actions = StreamTagParser().process_chunk(raw)
        assert len(actions) == 1
        state.auip_inline_fallback = actions[0]
        await owner._wait_for_auip_controls(state)
        callback.assert_not_called()
        assert state.auip_decision_dispatched is False
    asyncio.run(run())


def test_uncertain_non_work_control_does_not_block_independent_preparation():
    from server.auip_control_decision import AuipControlDecision

    decision = AuipControlDecision(status="ok", action="prepare", preparation_work_item_id="work_other")
    state, calls, notes, app_calls = asyncio.run(_application_failure("handoff_exception", decision, report_only=True))
    assert len(calls) == 1
    assert notes[0]["metadata"]["execution_uncertain"] is True
    assert state.work_handoff_uncertain is False
    assert len(app_calls) == 1 and app_calls[0]["action"] == "prepare"


def test_application_uncertainty_keeps_the_observer_failure_latch_after_ring_eviction():
    from core.chat_control_authority import observe_control_resolution
    from server.control_authority import resolve_control_authority
    from server.turn_decision_shadow import TurnDecisionShadowObserver

    shadow = TurnDecisionShadowObserver(enabled=True)
    shadow.admit_turn(utterance_id="input", turn_id="application-trace", session_id="test", transcript="build", dialogue_source_scope="chat:test", chat_epoch=1)
    resolution = resolve_control_authority(decision_status="invalid", decision_outcome="application_uncertain")
    with patch("server.turn_decision_shadow.get_enabled_turn_decision_shadow_observer", return_value=shadow):
        observe_control_resolution("application-trace", resolution)
    event = shadow.snapshot()["recent"][0]["events"][-1]
    assert event["origin_kind"] == "host_action_dispatcher"
    assert event["payload"]["execution_uncertain"] is True
    for i in range(110):
        shadow.record_event("application-trace", stage="bounded_noise", origin_kind="test", origin_id=str(i))
    assert not any(item["stage"] == "control_authority_resolved" for item in shadow.snapshot()["recent"][0]["events"])
    decision = shadow.observe_settlement("application-trace", effective_actions=({"type": "DELEGATE", "attrs": {"intent": "execute", "task": "build"}},))
    assert decision.status == "failed_closed"
    assert len(decision.effects) == 1  # Known request retained, never a no-effect receipt.
