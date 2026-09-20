"""Characterize the two existing narration payloads before extraction."""

from __future__ import annotations

import asyncio
from pathlib import Path

from server.work_observer import WorkObserverCoordinator
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import VNProfile


ROOT = Path(__file__).resolve().parents[1]


def test_work_observer_payload_shape_is_stable_before_delivery_extraction() -> None:
    async def run() -> dict:
        observer = WorkObserverCoordinator()
        observer.configure(display_language=lambda: "japanese")
        try:
            return observer._narration_payload(
                {
                    "display_text": "検証が終わったわ。",
                    "display_language": "japanese",
                    "run_id": "run-1",
                    "work_item_id": "work-1",
                    "attempt_id": "attempt-1",
                    "action": "final_report",
                    "terminal": True,
                    "note_count": 4,
                }
            )
        finally:
            worker = observer._worker
            if worker is not None and not worker.done():
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

    payload = asyncio.run(run())

    assert payload == {
        "display_text": "検証が終わったわ。",
        "display_language": "japanese",
        "emotion": "happy",
        "duration_ms": 5600,
        "line_id": "work-observer-run-1-final_report-4",
        "turn_id": "work-observer-run-1-final_report-4",
        "complete_turn": True,
        "source": "work_observer",
        "action": "final_report",
        "terminal": True,
        "work_item_id": "work-1",
        "attempt_id": "attempt-1",
        "voice_text_ja": "検証が終わったわ。",
    }


def test_vn_director_payload_shape_is_stable_before_delivery_extraction() -> None:
    async def run() -> None:
        captured: list[dict] = []

        async def speak(payload: dict) -> dict:
            captured.append(payload)
            return {"status": "queued", "sentence_id": "sentence-1"}

        runtime = VNPlayerRuntime(ROOT, speak_callback=speak)
        runtime.profile = VNProfile(
            session_id="vn-session-1",
            game_id="game-1",
            game_title="Game",
            output_language="ja",
            overlay_url="http://127.0.0.1:8788/reaction",
        )
        await runtime._speak(
            {
                "text": "そこ、少し怪しいわね。",
                "priority": "normal",
                "emotion_intent": "thinking",
            },
            {"line_id": "line-7", "script_id": "script-7"},
        )

        assert captured == [
            {
                "text": "そこ、少し怪しいわね。",
                "priority": "normal",
                "emotion_intent": "thinking",
                "line": {
                    "seq": None,
                    "line_id": "line-7",
                    "script_id": "script-7",
                    "speaker": "",
                    "text_hash": "",
                },
                "session_id": "vn-session-1",
                "overlay_url": "http://127.0.0.1:8788/reaction",
            }
        ]

    asyncio.run(run())


if __name__ == "__main__":
    test_work_observer_payload_shape_is_stable_before_delivery_extraction()
    test_vn_director_payload_shape_is_stable_before_delivery_extraction()
    print("ok: Work and VN narration payload baselines are explicit")
