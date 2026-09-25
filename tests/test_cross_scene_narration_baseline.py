"""Current narration payload contracts at the shared delivery boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from server.work_observer import WorkObserverCoordinator
from vn_player.runtime import VNPlayerRuntime


def test_work_observer_payload_preserves_run_identity_and_delivery_fields() -> None:
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
        "run_id": "run-1",
        "action": "final_report",
        "terminal": True,
        "work_item_id": "work-1",
        "attempt_id": "attempt-1",
        "voice_text_ja": "検証が終わったわ。",
    }


def test_vn_director_payload_shape_is_stable_before_delivery_extraction(tmp_path: Path) -> None:
    async def run() -> None:
        captured: list[dict] = []

        async def speak(payload: dict) -> dict:
            captured.append(payload)
            return {"status": "queued", "sentence_id": "sentence-1"}

        runtime = VNPlayerRuntime(tmp_path, speak_callback=speak)
        await runtime.start({
            "session_id": "vn-session-1",
            "game_id": "game-1",
            "game_title": "Game",
            "prompt_pack": "base",
            "script_path": "",
            "output_language": "ja",
            "overlay_url": "http://127.0.0.1:8788/reaction",
        })
        request = {
            "text": "そこ、少し怪しいわね。",
            "priority": "normal",
            "emotion_intent": "thinking",
        }
        line = {"line_id": "line-7", "script_id": "script-7"}
        await runtime._speak(request, line)
        await runtime.stop()

        # Playback grouping adds a Host-owned identity; the authored narration
        # and existing line/session attribution remain unchanged.
        assert captured[0].pop("vn_speech_id")
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

        # A stopped session must reject both comments and player-requested speech.
        for player_requested in (False, True):
            await runtime._speak(request, line, player_requested=player_requested)
            assert len(captured) == 1

    asyncio.run(run())


if __name__ == "__main__":
    test_work_observer_payload_preserves_run_identity_and_delivery_fields()
    with TemporaryDirectory() as directory:
        test_vn_director_payload_shape_is_stable_before_delivery_extraction(Path(directory))
    print("ok: Work and VN narration payload baselines are explicit")
