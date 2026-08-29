"""A permission card must be heard, and the outcome must still be reported.

Real session, 2026-08-02: a chess task finished, wrote its file to the Desktop
behind an export authorization, and the user heard nothing at all -- neither
"I need your approval" nor "it's done". They only learned the card existed by
looking at the screen, and then asked out loud why no report had come.

Both notes had been built and emitted; the ledger's own claim markers prove it
(`desktop_export_permission_notice_id` and `terminal_work_notice_ids` were both
set on the attempt). They died in the observer, and the mechanism was merging:
the card and the terminal note arrived eight seconds apart, both keypoints went
pending together, and a merge is represented by its most recent member -- the
terminal one. The card lost its identity, the branch that speaks cards
unconditionally never matched, and the merged note fell through to the observer
LLM's discretion, which chose silence.

Why the card may never be merged away: it is not a progress report, it is a
request for the user to act. Silence there does not cost a sentence, it stalls
the task indefinitely on a person who has gone off to do something else and has
no idea they are being waited on.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.protocol import Method
from server.event_bus import bus
from server.work_observer import WorkObserverCoordinator

RUN_ID = "work:work_chess:attempt:attempt_chess"


def _permission_note() -> dict:
    """The shape work_ledger_coordinator claims for an actionable export."""

    return {
        "source": "work_ledger",
        "provider": "locus",
        "run_id": RUN_ID,
        "session_id": "s-live",
        "phase": "Checkpoint",
        "title": "Desktop export needs your approval",
        "summary": "chess_game.py is staged for Desktop; approve it once to copy.",
        "importance": "blocking",
        "speak": True,
        "metadata": {
            "permission_actionable": True,
            "permission_kind": "desktop_export",
            "permission_action": "allow_once",
            "permission_request_id": "permission_960276ae",
            "permission_filenames": ["chess_game.py"],
        },
    }


def _terminal_note() -> dict:
    """The shape claimed once the approved export has been copied."""

    return {
        "source": "work_ledger",
        "provider": "locus",
        "run_id": RUN_ID,
        "session_id": "s-live",
        "phase": "Result",
        "title": "Desktop export complete",
        "summary": "chess_game.py was copied to Desktop successfully.",
        "importance": "important",
        "speak": True,
        "metadata": {
            "work_event": "work.accepted",
            "attention": "none",
            "work_item_id": "work_chess",
            "attempt_id": "attempt_chess",
            "delivery_id": "attempt:attempt_chess:provider_result",
        },
    }


def _progress_note() -> dict:
    """An ordinary keypoint whose narration opens the cadence window.

    Without one of these the very first note flushes instantly, nothing is ever
    held, and no merge can form -- which is how the first version of this test
    passed against the unfixed code and proved nothing.
    """

    return {
        "source": "work_ledger",
        "provider": "locus",
        "run_id": RUN_ID,
        "session_id": "s-live",
        "phase": "Work",
        "title": "Working",
        "summary": "Writing the board renderer.",
        "importance": "normal",
        "speak": True,
        # This sentence is a content update, not the mechanical fact that the
        # provider invoked its first tool.  Use the semantic contract so the
        # test opens the same cadence window as production progress does.
        "metadata": {"narration_keypoint": "semantic_progress"},
    }


def _observer(spoken: list[dict], decisions: list[dict]) -> WorkObserverCoordinator:
    async def narrate(payload: dict) -> dict:
        spoken.append(payload)
        return {"status": "queued"}

    async def decide(**kwargs) -> dict:
        note = kwargs["note"]
        decisions.append(note)
        # Terminal only for a result, as the real observer decides. Returning
        # True for everything closes the run after the first narration, and a
        # closed run drops every later note that is not an actionable export --
        # which silently turned the first draft of this test into a check of
        # its own stub.
        terminal = str(note.get("phase") or "").strip().lower() == "result"
        return {
            "action": "speak",
            "terminal": terminal,
            "append_to_main_chat": False,
            "speak": True,
            "display_text": f"Outcome: {note.get('summary', '')}",
            "main_chat_entry": "",
            "reason": "terminal" if terminal else "progress",
        }

    observer = WorkObserverCoordinator()
    observer.configure(
        is_chat_busy=lambda: False,
        is_tts_busy=lambda: False,
        narrate=narrate,
        display_language=lambda: "English",
        observer_llm=decide,
        # Production's own cadence window. Zero here is what made the first
        # version of this test unable to fail: with no window nothing is ever
        # held, so the card and the outcome never coexist as pending keypoints
        # and the merge that lost the card in production cannot form.
        narration_min_interval_s=20.0,
    )

    async def no_wait() -> None:
        return None

    observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
    return observer


def test_the_card_is_announced_and_the_outcome_still_follows() -> None:
    async def run() -> None:
        spoken: list[dict] = []
        decisions: list[dict] = []
        observer = _observer(spoken, decisions)
        try:
            # Reproduces the live sequence: one narration opens the cadence
            # window, the card lands inside it and is held, then the terminal
            # note arrives and makes the flush ready. Both keypoints are now
            # pending together -- the condition that erased the card.
            await observer._on_work_note(Method.CHAT_WORK_NOTE, _progress_note())
            for _ in range(20):
                await asyncio.sleep(0.05)
                if spoken:
                    break
            assert spoken, "the cadence window never opened"
            spoken.clear()

            await observer._on_work_note(Method.CHAT_WORK_NOTE, _permission_note())
            await asyncio.sleep(0.1)
            await observer._on_work_note(Method.CHAT_WORK_NOTE, _terminal_note())
            for _ in range(60):
                await asyncio.sleep(0.05)
                if len(spoken) >= 2:
                    break
        finally:
            observer._narration_governor.drop(RUN_ID)

        said = " | ".join(str(item.get("display_text") or "") for item in spoken)
        assert len(spoken) >= 2, f"expected the card and the outcome, heard: {said!r}"
        # The card first: it is what blocks, and the user may walk away.
        assert "chess_game.py" in str(spoken[0].get("display_text") or ""), said
        assert "approve" in said.lower() or "approval" in said.lower(), said
        # And the outcome still gets said, rather than being consumed with it.
        assert any(
            "copied to Desktop" in str(item.get("display_text") or "") for item in spoken
        ), said
        # The card never reaches the observer LLM: its delivery is not a
        # judgement call, so nothing may decide to stay quiet about it.
        assert all(
            str(note.get("title") or "") != "Desktop export needs your approval"
            for note in decisions
        ), "the permission card was put to the observer's discretion"

    asyncio.run(run())


def test_a_card_is_announced_once_however_often_its_note_repeats() -> None:
    """Re-delivery must not turn a blocking card into a nag."""

    async def run() -> None:
        spoken: list[dict] = []
        decisions: list[dict] = []
        observer = _observer(spoken, decisions)
        try:
            for _ in range(3):
                await observer._on_work_note(Method.CHAT_WORK_NOTE, _permission_note())
                await asyncio.sleep(0.05)
            for _ in range(20):
                await asyncio.sleep(0.05)
                if spoken:
                    break
            await asyncio.sleep(0.3)
        finally:
            observer._narration_governor.drop(RUN_ID)

        cards = [
            item
            for item in spoken
            if "approv" in str(item.get("display_text") or "").lower()
        ]
        assert len(cards) == 1, f"card announced {len(cards)} times: {spoken}"

    asyncio.run(run())


def test_permission_is_published_as_spoken_only_after_tts_accepts_it() -> None:
    async def run() -> None:
        order: list[str] = []
        emitted: list[dict] = []
        attempts = 0

        async def narrate(_payload: dict) -> dict:
            nonlocal attempts
            attempts += 1
            status = "dropped" if attempts == 1 else "queued"
            order.append(f"tts:{status}")
            return {"status": status, "reason": "queue_full" if attempts == 1 else ""}

        async def capture(_method: str, payload: dict) -> None:
            order.append("decision")
            emitted.append(payload)

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            display_language=lambda: "English",
        )
        observer._wait_for_output_idle = _no_wait  # type: ignore[method-assign]
        bus.on(Method.CHAT_OBSERVER_DECISION, capture)
        try:
            await observer._announce_export_permission(_permission_note())
        finally:
            bus.off(Method.CHAT_OBSERVER_DECISION, capture)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

        assert order == ["tts:dropped", "tts:queued", "decision"]
        assert emitted[0]["speak"] is True
        assert emitted[0]["speech_status"] == "queued"

    async def _no_wait() -> None:
        return None

    asyncio.run(run())


def test_rejected_tts_enqueue_is_a_truthful_text_fallback() -> None:
    async def run() -> None:
        emitted: list[dict] = []

        async def narrate(_payload: dict) -> dict:
            return {"status": "dropped", "reason": "queue_full"}

        async def capture(_method: str, payload: dict) -> None:
            emitted.append(payload)

        async def no_wait() -> None:
            return None

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            display_language=lambda: "English",
        )
        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        bus.on(Method.CHAT_OBSERVER_DECISION, capture)
        try:
            await observer._announce_export_permission(_permission_note())
        finally:
            bus.off(Method.CHAT_OBSERVER_DECISION, capture)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

        assert emitted[0]["speak"] is False
        assert emitted[0]["speech_status"] == "dropped"

    asyncio.run(run())


def test_terminal_busy_timeout_publishes_text_and_acknowledges_delivery() -> None:
    async def run() -> None:
        decisions: list[dict] = []
        receipts: list[dict] = []
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def capture_decision(_method: str, payload: dict) -> None:
            decisions.append(payload)

        async def capture_receipt(_method: str, payload: dict) -> None:
            receipts.append(payload)

        async def terminal_lane_never_clears() -> bool:
            return False

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: True,
            is_tts_busy=lambda: False,
            narrate=narrate,
            display_language=lambda: "English",
            terminal_max_wait_s=1.0,
        )
        observer._wait_for_terminal_output_idle = (  # type: ignore[method-assign]
            terminal_lane_never_clears
        )
        bus.on(Method.CHAT_OBSERVER_DECISION, capture_decision)
        bus.on(Method.CHAT_WORK_NOTE_DELIVERED, capture_receipt)
        try:
            await observer._on_work_note(Method.CHAT_WORK_NOTE, _terminal_note())
            for _ in range(40):
                await asyncio.sleep(0.05)
                if receipts:
                    break
        finally:
            bus.off(Method.CHAT_OBSERVER_DECISION, capture_decision)
            bus.off(Method.CHAT_WORK_NOTE_DELIVERED, capture_receipt)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

        assert not spoken
        assert decisions and decisions[0]["speak"] is False
        assert decisions[0]["speech_status"] == "output_busy_text_fallback"
        assert receipts == [
            {
                "delivery_id": "attempt:attempt_chess:provider_result",
                "work_item_id": "work_chess",
                "attempt_id": "attempt_chess",
                "run_id": RUN_ID,
                "session_id": "s-live",
            }
        ]

    asyncio.run(run())


if __name__ == "__main__":
    test_the_card_is_announced_and_the_outcome_still_follows()
    print("ok: the card is announced and the outcome still follows")
    test_a_card_is_announced_once_however_often_its_note_repeats()
    print("ok: a card is announced once however often its note repeats")
    test_permission_is_published_as_spoken_only_after_tts_accepts_it()
    print("ok: permission publication follows a real TTS enqueue receipt")
    test_rejected_tts_enqueue_is_a_truthful_text_fallback()
    print("ok: rejected TTS enqueue becomes a truthful text fallback")
    test_terminal_busy_timeout_publishes_text_and_acknowledges_delivery()
    print("ok: terminal busy timeout publishes text and delivery receipt")
    print("all permission narration tests passed")
