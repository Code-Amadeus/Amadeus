"""Long provider silence gets one bounded, truthful spoken checkpoint."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method
from server.work_observer import ObserverSession, WorkObserverCoordinator


def test_quiet_notice_has_early_threshold_and_repeat_backoff() -> None:
    async def run() -> None:
        notes: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            notes.append(payload)

        activity = WorkActivityCoordinator()
        state = activity._run_state(
            {
                "provider": "locus",
                "run_id": "locus-quiet-backoff",
                "task": "Build an endless game",
            }
        )
        state["status"] = "running"
        state["last_semantic_progress_at"] = 10.0
        state["last_provider_activity_at"] = 95.0
        bus.on(Method.CHAT_WORK_NOTE, capture)
        try:
            with (
                patch.object(
                    activity,
                    "_quiet_notice_after",
                    return_value=90.0,
                ),
                patch.object(
                    activity,
                    "_quiet_notice_repeat",
                    return_value=300.0,
                ),
            ):
                await activity._maybe_emit_quiet_notice(state, now=99.9)
                await activity._maybe_emit_quiet_notice(state, now=100.0)
                await activity._maybe_emit_quiet_notice(state, now=250.0)
                await activity._maybe_emit_quiet_notice(state, now=400.0)
        finally:
            bus.off(Method.CHAT_WORK_NOTE, capture)

        assert len(notes) == 2
        assert notes[0]["metadata"]["narration_keypoint"] == "quiet_monitoring"
        assert notes[0]["metadata"]["user_action_required"] is False
        assert notes[0]["summary"].startswith(
            "No new meaningful progress has been reported for 1m 30s"
        )
        assert notes[0]["metadata"]["semantic_silence_s"] == 90.0
        assert notes[0]["metadata"]["provider_silence_s"] == 5.0
        assert notes[1]["summary"].startswith(
            "No new meaningful progress has been reported for 6m 30s"
        )

    asyncio.run(run())


def test_recent_direction_delays_empty_monitoring_and_is_retained_in_the_notice() -> None:
    async def run() -> None:
        notes: list[dict] = []

        async def capture(_method: str, payload: dict) -> None:
            notes.append(payload)

        activity = WorkActivityCoordinator()
        state = activity._run_state(
            {
                "provider": "future_provider",
                "run_id": "direction-aware-quiet",
                "task": "Connect the existing app",
            }
        )
        state["status"] = "running"
        state["last_semantic_progress_at"] = 10.0
        state["last_directional_progress_at"] = 50.0
        state["semantic_candidate_text"] = (
            "Mapping the existing state first, then validating both operating modes."
        )
        state["semantic_candidate_source"] = "future_provider_assistant_update"
        state["last_provider_activity_at"] = 95.0
        bus.on(Method.CHAT_WORK_NOTE, capture)
        try:
            with patch.object(activity, "_quiet_notice_after", return_value=90.0):
                await activity._maybe_emit_quiet_notice(state, now=100.0)
                assert notes == []
                await activity._maybe_emit_quiet_notice(state, now=140.0)
        finally:
            bus.off(Method.CHAT_WORK_NOTE, capture)

        assert len(notes) == 1
        assert notes[0]["metadata"]["semantic_silence_s"] == 130.0
        assert notes[0]["metadata"]["useful_update_silence_s"] == 90.0
        assert notes[0]["metadata"]["directional_summary"].startswith(
            "Mapping the existing state"
        )

    asyncio.run(run())


def test_quiet_keypoint_is_spoken_even_when_observer_model_selects_silence() -> None:
    async def run() -> None:
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def choose_silence(**_kwargs) -> dict:
            return {
                "action": "silent",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": False,
                "display_text": "",
                "main_chat_entry": "",
                "reason": "model preferred silence",
            }

        async def no_wait() -> None:
            return None

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            display_language=lambda: "japanese",
            observer_llm=choose_silence,
            narration_min_interval_s=0,
        )
        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        note = {
            "provider": "locus",
            "run_id": "locus-quiet-spoken",
            "session_id": "quiet-session",
            "phase": "Work",
            "title": "Locus has no new milestone yet",
            "summary": (
                "No new meaningful progress has been reported for 1m 30s. "
                "Provider events are still arriving; Amadeus is monitoring the run."
            ),
            "importance": "normal",
            "metadata": {
                "narration_keypoint": "quiet_monitoring",
                "semantic_silence_s": 585.0,
                "useful_update_silence_s": 90.0,
            },
        }
        try:
            await observer._on_work_note(Method.CHAT_WORK_NOTE, note)
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=3.0)
            assert len(spoken) == 1
            assert spoken[0]["action"] == "speak"
            assert spoken[0]["terminal"] is False
            assert spoken[0]["voice_text_ja"] == spoken[0]["display_text"]
            assert spoken[0]["display_text"] == (
                "処理は続いているけど、確認できる新しい節目は"
                "1分30秒間届いていないわ。"
            )
            assert "No new meaningful progress" not in spoken[0]["display_text"]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(run())


def test_merged_quiet_fallback_never_wraps_raw_direction_in_japanese() -> None:
    observer = WorkObserverCoordinator()
    observer._get_display_language = lambda: "japanese"
    session = observer._sessions.setdefault(
        "merged-quiet",
        ObserverSession(
            narration_id="merged-quiet",
            run_id="merged-quiet",
            session_id="quiet-session",
            provider="future-provider",
        ),
    )
    note = {
        "phase": "Work",
        "summary": "Mapping the existing state before connected validation.",
        "metadata": {
            "narration_keypoints": ["directional_progress", "quiet_monitoring"],
            "narration_merged_count": 2,
            "semantic_silence_s": 115.0,
        },
    }
    decision = observer._apply_narration_keypoint_policy(
        {
            "action": "silent",
            "speak": False,
            "display_text": "",
            "main_chat_entry": "",
        },
        session,
        note,
    )
    assert decision["speak"] is True
    assert "1分55秒間" in decision["display_text"]
    assert "Mapping the existing state" not in decision["display_text"]


def test_japanese_export_permission_count_is_localized() -> None:
    observer = WorkObserverCoordinator()
    observer._get_display_language = lambda: "japanese"
    decision = observer._export_permission_decision(
        {
            "metadata": {
                "permission_request_id": "permission-bundle",
                "permission_filenames": ["a.js", "b.json", "c.html", "d.js", "e.js"],
            }
        }
    )
    assert "ほか2ファイル" in decision["display_text"]
    assert "more files" not in decision["display_text"]


if __name__ == "__main__":
    test_quiet_notice_has_early_threshold_and_repeat_backoff()
    test_recent_direction_delays_empty_monitoring_and_is_retained_in_the_notice()
    test_quiet_keypoint_is_spoken_even_when_observer_model_selects_silence()
    print("ok: long provider silence has bounded spoken narration")
