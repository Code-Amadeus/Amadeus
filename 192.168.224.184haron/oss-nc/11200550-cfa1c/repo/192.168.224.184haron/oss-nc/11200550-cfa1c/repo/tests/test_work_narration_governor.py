"""Synthetic cadence checks for provider-work narration."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.event_bus import bus
from server.protocol import Method
from server.work_narration_governor import WorkNarrationGovernor
from server.work_observer import WorkObserverCoordinator


def _note(run_id: str, *, keypoint: str = "", summary: str = "progress") -> dict:
    metadata = {"narration_keypoint": keypoint} if keypoint else {}
    return {
        "source": "provider",
        "provider": "locus",
        "run_id": run_id,
        "session_id": "session-cadence",
        "phase": "Result" if keypoint == "terminal" else "Work",
        "title": summary,
        "summary": summary,
        "signals": [],
        "importance": "important" if keypoint else "normal",
        "observer_policy": "auto",
        "metadata": metadata,
    }


def test_dense_delta_notes_do_not_open_narration() -> None:
    governor = WorkNarrationGovernor(min_interval_s=20, diagnostic_first_n=0)
    for index in range(50):
        gate = governor.observe(
            _note("dense-run", summary=f"delta {index}"),
            output_busy=False,
        )
        assert gate.keypoint == ""
        assert gate.ready is False
    assert governor.counts == {
        "note_seen": 50,
        "keypoint": 0,
        "spoken": 0,
        "merged": 0,
    }
    assert governor.has_pending("dense-run") is False


def test_reported_direction_opens_a_bounded_narration_window() -> None:
    governor = WorkNarrationGovernor(min_interval_s=20, diagnostic_first_n=0)
    gate = governor.observe(
        _note(
            "direction-run",
            keypoint="directional_progress",
            summary="Mapping the existing board into shared state before validation.",
        ),
        output_busy=False,
    )
    assert gate.keypoint == "directional_progress"
    assert gate.ready is True
    governor.mark_spoken("direction-run")
    assert governor.has_pending("direction-run") is False


def test_keypoints_inside_interval_hold_and_merge() -> None:
    now = [100.0]
    governor = WorkNarrationGovernor(
        min_interval_s=20,
        diagnostic_first_n=0,
        clock=lambda: now[0],
    )
    started = governor.observe(
        _note("merge-run", keypoint="run_started", summary="run began"),
        output_busy=False,
    )
    assert started.ready is True
    governor.mark_spoken("merge-run")

    now[0] += 3
    first_tool = governor.observe(
        _note("merge-run", keypoint="first_tool", summary="first tool"),
        output_busy=False,
    )
    now[0] += 4
    artifact = governor.observe(
        _note("merge-run", keypoint="artifact_registered", summary="artifact ready"),
        output_busy=False,
    )
    assert first_tool.ready is False
    assert artifact.ready is False
    assert governor.pending_keypoints("merge-run") == ["first_tool", "artifact_registered"]
    assert governor.pending_count("merge-run") == 2
    assert governor.counts["merged"] == 2

    now[0] += 13
    assert governor.remaining_delay("merge-run") == 0
    governor.mark_spoken("merge-run")
    assert governor.has_pending("merge-run") is False
    assert governor.counts["spoken"] == 2


def test_terminal_speaks_after_busy_hold() -> None:
    async def run() -> None:
        busy = [True]
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def decide(**kwargs) -> dict:
            note = kwargs["note"]
            return {
                "action": "silent",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": False,
                "display_text": "",
                "main_chat_entry": "",
                "reason": str(note.get("summary") or ""),
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: busy[0],
            narrate=narrate,
            display_language=lambda: "English",
            observer_llm=decide,
            narration_min_interval_s=20,
            narration_diagnostic_first_n=0,
        )
        try:
            await bus.emit(Method.CHAT_WORK_NOTE, _note("terminal-run", keypoint="run_started"))
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note("terminal-run", keypoint="terminal", summary="all checks passed"),
            )
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5.0)
            assert spoken == []
            assert observer._narration_governor.pending_is_terminal("terminal-run") is True

            busy[0] = False
            for _ in range(20):
                if spoken:
                    break
                await asyncio.sleep(0.05)
            assert len(spoken) == 1
            assert spoken[0]["terminal"] is True
            assert "all checks passed" in spoken[0]["display_text"]
            assert spoken[0]["source"] == "work_observer"
        finally:
            await observer.close()

    asyncio.run(run())


def test_close_joins_terminal_flush_and_seals_intake() -> None:
    async def run() -> None:
        baseline_subscribers = bus.subscriber_count(Method.CHAT_WORK_NOTE)

        async def decide(**_kwargs) -> dict:
            return {
                "action": "speak",
                "terminal": True,
                "append_to_main_chat": True,
                "speak": True,
                "display_text": "The work finished.",
                "main_chat_entry": "The work finished.",
                "reason": "verified terminal",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: True,
            narrate=lambda _payload: {"status": "queued"},
            display_language=lambda: "English",
            observer_llm=decide,
            narration_min_interval_s=0,
            narration_diagnostic_first_n=0,
            terminal_max_wait_s=10,
        )
        assert bus.subscriber_count(Method.CHAT_WORK_NOTE) == baseline_subscribers + 1

        await bus.emit(
            Method.CHAT_WORK_NOTE,
            _note("terminal-run", keypoint="terminal", summary="all checks passed"),
        )
        assert observer._queue is not None
        await asyncio.wait_for(observer._queue.join(), timeout=2.0)
        await asyncio.sleep(0)
        flushes = tuple(observer._narration_tasks.values())
        assert len(flushes) == 1
        assert flushes[0].get_name() == "work-narration-flush-terminal-run"
        assert not flushes[0].done()

        await observer.close()
        await observer.close()

        assert bus.subscriber_count(Method.CHAT_WORK_NOTE) == baseline_subscribers
        assert observer._worker is None
        assert observer._narration_tasks == {}
        assert observer._owned_narration_tasks == set()
        assert all(task.done() for task in flushes)
        assert not any(
            task.get_name() == "work-narration-flush-terminal-run"
            for task in asyncio.all_tasks()
        )

        await bus.emit(
            Method.CHAT_WORK_NOTE,
            _note("after-close", keypoint="terminal", summary="must stay ignored"),
        )
        assert observer.get_session("after-close") is None

    asyncio.run(run())


if __name__ == "__main__":
    test_dense_delta_notes_do_not_open_narration()
    test_reported_direction_opens_a_bounded_narration_window()
    test_keypoints_inside_interval_hold_and_merge()
    test_terminal_speaks_after_busy_hold()
    print("ok: work narration keypoints are rate-limited, merged, and terminal-safe")
