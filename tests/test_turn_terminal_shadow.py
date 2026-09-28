"""Terminal shadow is evidence, never a new action-existence authority."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.turn_decision_shadow import TurnDecisionShadowObserver


def _observer():
    observer = TurnDecisionShadowObserver(enabled=True)
    observer.admit_turn(
        utterance_id="voice-1", turn_id="turn-1", session_id="session-1",
        transcript="continue", input_source="voice",
    )
    return observer


def test_completion_without_settlement_is_not_fabricated_no_effect():
    observer = _observer()
    observer.mark_lifecycle("turn-1", "completed")
    row = observer.snapshot()["recent"][0]
    assert row["decision"] is None
    assert row["terminal"]["disposition"] == "missing_settlement"
    assert row["terminal"]["observed_effect_count"] is None
    assert observer.snapshot()["counters"]["terminal_missing_settlement"] == 1


def test_settled_no_effect_is_explicit_and_terminal_is_immutable():
    observer = _observer()
    observer.observe_settlement("turn-1")
    observer.mark_lifecycle("turn-1", "completed")
    original = observer.snapshot()["recent"][0]
    assert original["terminal"]["disposition"] == "observed_no_effect"
    assert original["terminal"]["observed_effect_count"] == 0
    observer.mark_lifecycle("turn-1", "completed")
    observer.mark_lifecycle("turn-1", "superseded", reason="later audio abort")
    observer.observe_settlement("turn-1", effective_actions=(
        {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "late"}},
    ))
    latest = observer.snapshot()["recent"][0]
    assert latest["terminal"] == original["terminal"]
    assert latest["decision"] == original["decision"]
    assert observer.snapshot()["counters"]["terminal_dispositions"] == 1
    assert observer.snapshot()["counters"]["late_settlements"] == 1


def test_failure_and_cancel_have_terminal_evidence_without_invented_effects():
    for status in ("failed", "cancelled", "superseded", "discarded", "expired"):
        observer = _observer()
        observer.mark_lifecycle("turn-1", status, reason="test")
        row = observer.snapshot()["recent"][0]
        assert row["terminal"]["disposition"] == status
        assert row["terminal"]["decision_id"] is None
        assert row["decision"] is None


def test_milestones_are_first_wins_monotonic_and_survive_event_eviction():
    with patch("server.turn_decision_shadow.time.monotonic", return_value=10.0):
        observer = _observer()
    with patch("server.turn_decision_shadow.time.monotonic", return_value=10.025):
        observer.record_event("turn-1", stage="first_sentence_enqueued", origin_kind="role")
    with patch("server.turn_decision_shadow.time.monotonic", return_value=10.100):
        observer.record_event("turn-1", stage="first_sentence_enqueued", origin_kind="role")
        for _ in range(110):
            observer.record_event("turn-1", stage="noise", origin_kind="test")
    timing = observer.snapshot()["recent"][0]["timing"]
    assert abs(timing["elapsed_ms"]["first_sentence_enqueued"] - 25.0) < 0.001
    assert timing["elapsed_ms"]["first_audio_write_completed"] is None
    assert timing["elapsed_ms"]["plan_frozen"] is None
    assert timing["plan_freeze_supported"] is False


def test_eviction_exposes_unclosed_observation_instead_of_silent_loss():
    observer = TurnDecisionShadowObserver(enabled=True, root_cap=1)
    for index in range(2):
        observer.admit_turn(utterance_id=f"u-{index}", turn_id=f"t-{index}",
                            session_id="session", transcript="hello")
    assert observer.snapshot()["counters"]["evicted_without_terminal"] == 1


def test_disabled_terminal_and_timing_have_no_observation_side_effects():
    observer = TurnDecisionShadowObserver(enabled=False)
    observer.mark_lifecycle("absent", "completed")
    observer.record_event("absent", stage="first_sentence_enqueued", origin_kind="test")
    assert observer.snapshot()["counters"]["terminal_dispositions"] == 0
    assert observer.snapshot()["recent"] == []


def test_late_first_settlement_and_direct_branch_do_not_repair_missing_evidence():
    for direct in (False, True):
        observer = _observer()
        observer.mark_lifecycle("turn-1", "completed", reason="\ud800")
        original = observer.snapshot()["recent"][0]["terminal"]
        if direct:
            observer.observe_direct_branch("turn-1", {"provider": "browser", "branch_id": "b-1"})
        else:
            observer.observe_settlement("turn-1")
        row = observer.snapshot()["recent"][0]
        assert row["terminal"] == original
        assert row["decision"] is None
        assert row["timing"]["elapsed_ms"]["decision_settled"] is None
        json.dumps(observer.snapshot(), ensure_ascii=False).encode("utf-8")


def test_audio_timing_joins_exact_original_turn_and_survives_later_active_turn():
    from core.turn_coordinator import TurnCoordinator

    coordinator = TurnCoordinator()
    with patch("server.turn_decision_shadow.time.monotonic", return_value=10.0):
        observer = _observer()
    coordinator.on_first_sentence_enqueued(turn_id="turn-1", sentence_id="sentence-original")
    coordinator.open_turn(turn_id="turn-new", local_next_epoch=3)
    with patch("core.turn_coordinator.time.monotonic", return_value=10.2):
        coordinator.on_sentence_audio_written(sentence_id="sentence-original")
    with patch("core.turn_coordinator.time.monotonic", return_value=11.0):
        coordinator.on_sentence_audio_written(sentence_id="sentence-original")
        coordinator.on_sentence_audio_written(sentence_id="unmapped")
    with patch("core.turn_coordinator.get_turn_coordinator", return_value=coordinator):
        timing = observer.snapshot()["recent"][0]["timing"]["elapsed_ms"]
        for index in range(64):
            sentence = f"new-sentence-{index}"
            coordinator.on_first_sentence_enqueued(turn_id=f"t-{index}", sentence_id=sentence)
            coordinator.on_sentence_audio_written(sentence_id=sentence)
        assert "turn-1" not in coordinator.first_audio_write_times()
        assert observer.snapshot()["recent"][0]["timing"]["elapsed_ms"] == timing
    assert timing["first_audio_write_completed"] == 200.0


def test_direct_blocker_is_not_fabricated_as_an_accepted_browser_effect():
    for uncertain in (False, True):
        observer = _observer()
        observer.observe_direct_branch("turn-1", {
            "provider": "browser", "branch_id": "b-1",
            "route_kind": "browser_continuation_blocked",
            "execution_uncertain": uncertain,
        })
        observer.mark_lifecycle("turn-1", "completed")
        row = observer.snapshot()["recent"][0]
        assert row["decision"]["effects"] == []
        assert row["terminal"]["disposition"] == (
            "failed_closed" if uncertain else "observed_no_effect"
        )


def test_runtime_normal_error_and_cancellation_all_close_observation():
    from core.chat_runtime import ChatRuntime

    async def run(status):
        observer = TurnDecisionShadowObserver(enabled=True)
        runtime = ChatRuntime()
        runtime.configure(pending_sentence_items=asyncio.Queue(), playback_manager=None, provider="local")
        runtime._ensure_clients = lambda _provider: None

        async def model(*_args, **_kwargs):
            if status == "failed":
                raise RuntimeError("model unavailable")
            if status == "cancelled":
                raise asyncio.CancelledError()

        runtime._run_local = model
        with patch("server.turn_decision_shadow.observer", observer):
            try:
                result = await runtime.stream_llm_query("hello", preserve_emotion=True, turn_id="runtime-turn")
                assert status != "cancelled"
                assert ("LLM API Error" in result) == (status == "failed")
            except asyncio.CancelledError:
                assert status == "cancelled"
            # The Handler sees a returned error string as a completed stream.
            # That generic signal must not erase the more specific runtime failure.
            from server.handlers.chat_handler import ChatHandler

            ChatHandler._notify_coordinator_finished("runtime-turn", ok=True)
        row = observer.snapshot()["recent"][0]
        assert row["terminal"]["lifecycle"] == status
        assert row["terminal"]["disposition"] == (
            "observed_no_effect" if status == "completed" else status
        )

    for status in ("completed", "failed", "cancelled"):
        asyncio.run(run(status))


def test_handler_cancellation_before_coroutine_start_still_closes_admission():
    from core.turn_coordinator import TurnCoordinator
    from server.handlers.chat_handler import ChatHandler

    async def run():
        observer = TurnDecisionShadowObserver(enabled=True)
        handler = ChatHandler()
        model = AsyncMock(return_value="unused")
        handler.configure(stream_llm_query=model, pending_sentence_items=None)
        create_task = asyncio.create_task
        def cancel_before_start(coro, *args, **kwargs):
            task = create_task(coro, *args, **kwargs)
            if coro.cr_code.co_name == "_run_stream":
                task.cancel()
            return task
        with (
            patch("server.turn_decision_shadow.observer", observer),
            patch("core.turn_coordinator.get_turn_coordinator", return_value=TurnCoordinator()),
            patch("core.session_manager.get_current_session_id", return_value=""),
            patch("asyncio.create_task", side_effect=cancel_before_start),
        ):
            await handler._handle_send({"text": "hello", "turn_id": "never-started"})
            await asyncio.gather(handler._stream_task, return_exceptions=True)
            await asyncio.sleep(0)
        model.assert_not_awaited()
        row = observer.snapshot()["recent"][0]
        assert row["terminal"]["lifecycle"] == "cancelled"
        assert row["terminal"]["decision_id"] is None

    asyncio.run(run())


def test_audio_writer_never_waits_on_coordinator_logging_lock():
    from core.turn_coordinator import TurnCoordinator

    coordinator = TurnCoordinator()
    coordinator.on_first_sentence_enqueued(turn_id="voice-turn", sentence_id="sentence")
    completed = threading.Event()

    def writer():
        coordinator.on_sentence_audio_written(sentence_id="sentence")
        completed.set()

    with coordinator._lock:
        thread = threading.Thread(target=writer)
        thread.start()
        finished_while_general_lock_held = completed.wait(timeout=2)
    thread.join(timeout=2)
    assert finished_while_general_lock_held
    assert set(coordinator.first_audio_write_times()) == {"voice-turn"}


def test_live_j5_failed_closed_fact_survives_zero_effects_and_ring_eviction():
    fixture_path = Path(__file__).parent / "fixtures/turn_decision/j5_authority_failure_live_v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    observer = _observer()
    for event in fixture["events"]:
        observer.record_event("turn-1", **event)
    for _ in range(110):
        observer.record_event("turn-1", stage="unrelated_progress", origin_kind="test")
    observer.observe_settlement("turn-1")
    observer.mark_lifecycle("turn-1", "completed")
    row = observer.snapshot()["recent"][0]
    assert row["terminal"]["disposition"] == fixture["expected_terminal"]
    assert row["terminal"]["observed_effect_count"] == 0
    assert row["decision"]["status"] == "failed_closed"
    assert not any(event["stage"] == "control_authority_resolved" for event in row["events"])


def test_control_resolution_observation_preserves_failure_not_empty_action_heuristic():
    from core.chat_control_authority import observe_control_resolution
    from server.control_authority import ControlAuthorityResolution

    for disposition in ("accepted", "suppressed", "failed_closed"):
        observer = _observer()
        with patch("server.turn_decision_shadow.observer", observer):
            observe_control_resolution("turn-1", ControlAuthorityResolution(disposition=disposition))
        observer.observe_settlement("turn-1")
        observer.mark_lifecycle("turn-1", "completed")
        row = observer.snapshot()["recent"][0]
        assert row["terminal"]["disposition"] == (
            "failed_closed" if disposition == "failed_closed" else "observed_no_effect"
        )
    observer = _observer()
    with patch("server.turn_decision_shadow.observer", observer):
        observe_control_resolution("turn-1", ControlAuthorityResolution(disposition="failed_closed"))
    observer.observe_settlement("turn-1", effective_actions=(
        {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "already accepted witness"}},
    ))
    observer.mark_lifecycle("turn-1", "completed")
    terminal = observer.snapshot()["recent"][0]["terminal"]
    assert terminal["disposition"] == "failed_closed"
    assert terminal["observed_effect_count"] == 1
    with patch("server.turn_decision_shadow.get_enabled_turn_decision_shadow_observer", return_value=None):
        # Disabled telemetry must not require an observation-compatible object.
        observe_control_resolution("turn-1", SimpleNamespace())
