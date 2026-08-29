from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.event_bus import bus
from server.protocol import Method
from server.work_observer import ObserverSession, WorkObserverCoordinator


async def _settle_narration(observer: WorkObserverCoordinator) -> None:
    """Wait on the Observer-owned flush boundary, not only fact ingestion."""

    while True:
        tasks = tuple(observer._narration_tasks.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _note(summary: str, keypoint: str, *, milestone: str = "", phase: str = "Work") -> dict:
    metadata = {"narration_keypoint": keypoint}
    if milestone:
        metadata["semantic_milestone"] = milestone
    return {
        "source": "work_ledger",
        "provider": "future_provider",
        "run_id": "run-priority",
        "session_id": "session-priority",
        "phase": phase,
        "title": summary,
        "summary": summary,
        "signals": [{"label": "report", "text": summary}],
        "importance": "important" if phase == "Result" else "normal",
        "metadata": metadata,
    }


def test_terminal_merge_keeps_three_semantic_milestones_over_mechanics() -> None:
    observer = WorkObserverCoordinator()
    session = ObserverSession(
        narration_id="run-priority",
        run_id="run-priority",
        session_id="session-priority",
        provider="future_provider",
        goal="Build the game",
    )
    notes = [
        _note("Edited game.py and styles.css.", "semantic_candidate"),
        _note("Chose explicit turn ownership to prevent input races.", "semantic_progress", milestone="design"),
        _note("Two players can now alternate turns and see the winner.", "semantic_progress", milestone="capability"),
        _note("Ran 18 tests: all passed; the earlier reset bug is fixed.", "semantic_progress", milestone="validation"),
        _note("Provider execution ended and the result is ready for review.", "terminal", phase="Result"),
    ]
    for note in notes:
        session.add_note(note)
        observer._narration_governor.observe(note, output_busy=True)

    merged = observer._merged_narration_note(session)
    summary = str(merged.get("summary") or "")
    assert "turn ownership" in summary
    assert "alternate turns" in summary
    assert "18 tests" in summary
    assert "earlier reset bug is fixed" in summary
    assert "ready for review" in summary
    assert "Edited game.py" not in summary


def test_same_permission_issue_speaks_once_until_state_changes() -> None:
    async def scenario() -> None:
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def decide(**kwargs) -> dict:
            note = kwargs["note"]
            return {
                "action": "speak",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": True,
                "display_text": str(note.get("summary") or ""),
                "main_chat_entry": "",
                "reason": "permission state changed",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]

        def permission(request_id: str, state: str) -> dict:
            keypoint = "permission_pending" if state == "pending" else "permission_blocked"
            return {
                "source": "work_ledger",
                "provider": "future_provider",
                "run_id": "run-permission-state",
                "session_id": "session-permission-state",
                "phase": "Checkpoint",
                "title": "Permission state",
                "summary": f"Workspace shell permission is {state}.",
                "signals": [{"label": "permission", "text": state}],
                "importance": "blocking",
                "metadata": {
                    "work_item_id": "work-permission-state",
                    "attempt_id": "attempt-permission-state",
                    "permission_request_id": request_id,
                    "permission_issue": {
                        "capability": "shell",
                        "action": "execute",
                        "scope": ["workspace"],
                    },
                    "permission_status": state,
                    "narration_keypoint": keypoint,
                },
            }

        try:
            await bus.emit(Method.CHAT_WORK_NOTE, permission("p1", "pending"))
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            await bus.emit(Method.CHAT_WORK_NOTE, permission("p2", "pending"))
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            await bus.emit(Method.CHAT_WORK_NOTE, permission("p3", "denied"))
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            assert len(spoken) == 2
            assert "pending" in spoken[0]["display_text"]
            assert "denied" in spoken[1]["display_text"]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_terminal_arriving_during_progress_decision_is_not_consumed() -> None:
    async def scenario() -> None:
        decision_started = asyncio.Event()
        release_progress = asyncio.Event()
        published: list[dict] = []

        async def narrate(payload: dict) -> dict:
            published.append(payload)
            return {"status": "queued"}

        async def decide(**kwargs) -> dict:
            note = kwargs["note"]
            terminal = str(note.get("phase") or "").lower() == "result"
            if not terminal:
                decision_started.set()
                await release_progress.wait()
            return {
                "action": "final_report" if terminal else "silent",
                "terminal": terminal,
                "append_to_main_chat": terminal,
                "speak": terminal,
                "display_text": str(note.get("summary") or "") if terminal else "",
                "main_chat_entry": str(note.get("summary") or "") if terminal else "",
                "reason": "terminal must survive an older decision",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        progress = _note(
            "The reset defect is fixed and validation is running.",
            "semantic_progress",
            milestone="validation",
        )
        terminal = _note(
            "The reset defect is fixed; 18 tests passed and the result is ready.",
            "terminal",
            phase="Result",
        )
        try:
            progress_emit = asyncio.create_task(
                bus.emit(Method.CHAT_WORK_NOTE, progress)
            )
            await asyncio.wait_for(decision_started.wait(), timeout=5)
            await bus.emit(Method.CHAT_WORK_NOTE, terminal)
            release_progress.set()
            await asyncio.wait_for(progress_emit, timeout=5)
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            for _ in range(100):
                if any(item.get("terminal") for item in published):
                    break
                await asyncio.sleep(0.02)
            terminal_reports = [item for item in published if item.get("terminal")]
            assert len(terminal_reports) == 1
            assert "18 tests passed" in terminal_reports[0]["display_text"]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_progress_arriving_during_progress_flush_needs_no_third_note() -> None:
    async def scenario() -> None:
        first_decision_started = asyncio.Event()
        release_first = asyncio.Event()
        spoken: list[dict] = []
        decision_count = 0

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def decide(**kwargs) -> dict:
            nonlocal decision_count
            decision_count += 1
            note = kwargs["note"]
            if decision_count == 1:
                first_decision_started.set()
                await release_first.wait()
            text = str(note.get("summary") or "")
            return {
                "action": "speak",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": True,
                "display_text": text,
                "main_chat_entry": "",
                "reason": "new milestone",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        first = _note(
            "The connection shape is selected.",
            "semantic_progress",
            milestone="design",
        )
        second = _note(
            "The participant action contract now validates.",
            "semantic_progress",
            milestone="validation",
        )
        try:
            await bus.emit(Method.CHAT_WORK_NOTE, first)
            await asyncio.wait_for(first_decision_started.wait(), timeout=5)
            await bus.emit(Method.CHAT_WORK_NOTE, second)
            release_first.set()
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await asyncio.wait_for(_settle_narration(observer), timeout=5)

            assert len(spoken) == 2
            assert "connection shape" in spoken[0]["display_text"]
            assert "participant action contract" in spoken[1]["display_text"]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_terminal_retires_queued_nonterminal_speech_for_same_work_item() -> None:
    async def scenario() -> None:
        observer = WorkObserverCoordinator()
        terminal = {
            **_note(
                "The game is complete and all checks passed.",
                "terminal",
                phase="Result",
            ),
            "metadata": {
                "narration_keypoint": "terminal",
                "work_item_id": "work-terminal-supersession",
                "attempt_id": "attempt-terminal-supersession",
            },
        }
        observer._ensure_narration_flush = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        with (
            patch(
                "server.vn_tts_bridge.cancel_pending_vn_tts",
                return_value=1,
            ) as cancel_bridge,
            patch("tts.pipeline.discard_pending_tts", return_value=1) as discard_queue,
        ):
            await observer._handle_note(terminal)

        expected = {
            "source": "work_observer",
            "work_item_id": "work-terminal-supersession",
            "nonterminal_only": True,
        }
        cancel_bridge.assert_called_once_with(**expected)
        discard_queue.assert_called_once_with(**expected)

    asyncio.run(scenario())


def test_semantic_decision_keeps_consumed_keypoint_provenance() -> None:
    async def scenario() -> None:
        decisions: list[dict] = []

        async def narrate(_payload: dict) -> dict:
            return {"status": "queued"}

        async def decide(**kwargs) -> dict:
            note = kwargs["note"]
            return {
                "action": "speak",
                "terminal": False,
                "append_to_main_chat": True,
                "speak": True,
                "display_text": str(note.get("summary") or ""),
                "main_chat_entry": str(note.get("summary") or ""),
                "reason": "factual milestone",
            }

        async def capture(_method: str, payload: dict) -> None:
            decisions.append(dict(payload))

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        bus.on(Method.CHAT_OBSERVER_DECISION, capture)
        try:
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note(
                    "Two-player scoring is now implemented.",
                    "semantic_progress",
                    milestone="capability",
                ),
            )
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            assert len(decisions) == 1
            assert decisions[0]["narration_keypoints"] == ["semantic_progress"]
            assert decisions[0]["narration_merged_count"] == 1
        finally:
            bus.off(Method.CHAT_OBSERVER_DECISION, capture)
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_first_concrete_direction_is_spoken_once_even_when_observer_defers() -> None:
    async def scenario() -> None:
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def defer_with_copy(**kwargs) -> dict:
            summary = str(kwargs["note"].get("summary") or "")
            return {
                "action": "silent",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": False,
                "display_text": summary,
                "main_chat_entry": "",
                "reason": "the canvas already shows the direction",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=defer_with_copy,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        first = _note(
            "I am mapping the existing controls, then I will verify standalone and connected behavior.",
            "directional_progress",
        )
        second = _note(
            "I am now reading one more interface reference.",
            "directional_progress",
        )
        try:
            await bus.emit(Method.CHAT_WORK_NOTE, first)
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            assert len(spoken) == 1
            assert "mapping the existing controls" in spoken[0]["display_text"]

            session = observer.get_session("run-priority")
            assert session is not None
            session.last_progress_decision_at = 0.0
            await bus.emit(Method.CHAT_WORK_NOTE, second)
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            assert len(spoken) == 1
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_exact_nonterminal_narration_is_delivered_once_per_attempt() -> None:
    async def scenario() -> None:
        spoken: list[dict] = []

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        async def same_copy(**_kwargs) -> dict:
            return {
                "action": "speak",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": True,
                "display_text": "I am checking the shared interface and staged bundle.",
                "main_chat_entry": "",
                "reason": "direction",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=same_copy,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        first = _note("Inspecting the compact interface.", "directional_progress")
        second = _note("Reading the manifest template.", "directional_progress")
        try:
            await bus.emit(Method.CHAT_WORK_NOTE, first)
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            await bus.emit(Method.CHAT_WORK_NOTE, second)
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)

            assert len(spoken) == 1
            session = observer.get_session("run-priority")
            assert session is not None
            assert session.decisions[-1]["speak"] is False
            assert "already delivered" in session.decisions[-1]["reason"]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_nonterminal_voice_keeps_only_new_sentence_units() -> None:
    observer = WorkObserverCoordinator()
    session = ObserverSession(
        narration_id="run-sentence-dedup",
        run_id="run-sentence-dedup",
        session_id="session-sentence-dedup",
        provider="future_provider",
    )
    session.add_decision(
        {
            "action": "speak",
            "speak": True,
            "speech_status": "queued",
            "display_text": "The board is ready. Validation is running.",
        }
    )

    partial = observer._deduplicate_spoken_voice(
        {
            "action": "speak",
            "append_to_main_chat": False,
            "speak": True,
            "display_text": "The board is ready. Connected mode also passed.",
            "main_chat_entry": "",
            "reason": "semantic update",
        },
        session,
        terminal=False,
    )
    repeated = observer._deduplicate_spoken_voice(
        {
            "action": "speak",
            "append_to_main_chat": False,
            "speak": True,
            "display_text": "The board is ready.",
            "main_chat_entry": "",
            "reason": "semantic update",
        },
        session,
        terminal=False,
    )

    assert partial["display_text"] == "Connected mode also passed."
    assert partial["speak"] is True
    assert repeated["action"] == "silent"
    assert repeated["speak"] is False
    assert repeated["display_text"] == ""


def test_keypoint_fallback_has_no_canned_progress_label() -> None:
    assert WorkObserverCoordinator._keypoint_summary(
        "五子棋のゲームをHTMLファイルとして新規作成する",
        "japanese",
    ) == "五子棋のゲームをHTMLファイルとして新規作成する。"
    assert WorkObserverCoordinator._keypoint_summary(
        "I am validating both launch modes",
        "english",
    ) == "I am validating both launch modes."


def test_liveness_speech_does_not_consume_first_substantive_update() -> None:
    session = ObserverSession(
        narration_id="run-liveness-first",
        run_id="run-liveness-first",
        session_id="session-liveness-first",
        provider="future_provider",
    )
    session.add_decision(
        {
            "speak": True,
            "speech_status": "queued",
            "narration_keypoints": ["quiet_monitoring"],
        }
    )
    assert session.has_spoken_keypoint(
        "directional_progress",
        "semantic_progress",
    ) is False
    session.add_decision(
        {
            "speak": True,
            "speech_status": "queued",
            "narration_keypoints": ["directional_progress"],
        }
    )
    assert session.has_spoken_keypoint(
        "directional_progress",
        "semantic_progress",
    ) is True


def test_terminal_narrator_receives_only_successfully_queued_prior_speech() -> None:
    async def scenario() -> None:
        calls: list[dict] = []

        async def decide(**kwargs) -> dict:
            calls.append(kwargs)
            terminal = str(kwargs["note"].get("phase") or "").lower() == "result"
            text = (
                "Validation passed."
                if terminal
                else "Two-player scoring is implemented."
            )
            return {
                "action": "final_report" if terminal else "speak",
                "terminal": terminal,
                "append_to_main_chat": terminal,
                "speak": True,
                "display_text": text,
                "main_chat_entry": text if terminal else "",
                "reason": "semantic milestone",
            }

        async def narrate(_payload: dict) -> dict:
            return {"status": "queued"}

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        async def terminal_idle() -> bool:
            return True

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        observer._wait_for_terminal_output_idle = terminal_idle  # type: ignore[method-assign]
        try:
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note(
                    "Two-player scoring is implemented.",
                    "semantic_progress",
                    milestone="capability",
                ),
            )
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note(
                    "The implementation is complete and validation passed.",
                    "terminal",
                    phase="Result",
                ),
            )
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)

            assert len(calls) == 2
            assert calls[0]["recent_spoken_updates"] == []
            assert calls[1]["recent_spoken_updates"] == [
                {
                    "action": "speak",
                    "text": "Two-player scoring is implemented.",
                    "terminal": False,
                }
            ]
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_terminal_voice_omits_exact_sentences_already_spoken_but_keeps_full_history() -> None:
    async def scenario() -> None:
        spoken: list[dict] = []
        history: list[dict] = []

        async def decide(**kwargs) -> dict:
            terminal = str(kwargs["note"].get("phase") or "").lower() == "result"
            text = (
                "Validation passed. 65/65 checks succeeded. Desktop export still needs approval."
                if terminal
                else "Validation passed. 65/65 checks succeeded."
            )
            return {
                "action": "final_report" if terminal else "speak",
                "terminal": terminal,
                "append_to_main_chat": terminal,
                "speak": True,
                "display_text": text,
                "main_chat_entry": text if terminal else "",
                "reason": "validation boundary",
            }

        async def narrate(payload: dict) -> dict:
            spoken.append(payload)
            return {"status": "queued"}

        observer = WorkObserverCoordinator()
        observer.configure(
            is_chat_busy=lambda: False,
            is_tts_busy=lambda: False,
            narrate=narrate,
            observer_llm=decide,
            append_to_main_chat=lambda decision: history.append(dict(decision)),
            display_language=lambda: "English",
            narration_min_interval_s=0,
        )

        async def no_wait() -> None:
            return None

        async def terminal_idle() -> bool:
            return True

        observer._wait_for_output_idle = no_wait  # type: ignore[method-assign]
        observer._wait_for_terminal_output_idle = terminal_idle  # type: ignore[method-assign]
        try:
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note(
                    "Validation passed. 65/65 checks succeeded.",
                    "semantic_progress",
                    milestone="validation",
                ),
            )
            assert observer._queue is not None
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)
            await bus.emit(
                Method.CHAT_WORK_NOTE,
                _note(
                    "Validation passed and Desktop export still needs approval.",
                    "terminal",
                    phase="Result",
                ),
            )
            await asyncio.wait_for(observer._queue.join(), timeout=5)
            await _settle_narration(observer)

            assert [item["display_text"].replace(" ", "") for item in spoken] == [
                "Validationpassed.65/65checkssucceeded.",
                "Desktopexportstillneedsapproval.",
            ]
            assert history[-1]["main_chat_entry"].replace(" ", "") == (
                "Validationpassed.65/65checkssucceeded."
                "Desktopexportstillneedsapproval."
            )
            assert history[-1]["display_text"] == "Desktop export still needs approval."
            assert observer.get_session("run-priority") is None
            assert observer._closed_runs["run-priority"] > 0
        finally:
            bus.off(Method.CHAT_WORK_NOTE, observer._on_work_note)
            if observer._worker is not None:
                observer._worker.cancel()
                await asyncio.gather(observer._worker, return_exceptions=True)

    asyncio.run(scenario())


def test_terminal_narrator_cannot_reuse_a_predecessor_attempt_from_chat() -> None:
    async def scenario() -> None:
        captured: list[dict] = []

        async def decide(**kwargs) -> dict:
            captured.append(kwargs)
            return {
                "action": "final_report",
                "terminal": True,
                "append_to_main_chat": True,
                "speak": True,
                "display_text": "The current revision is complete.",
                "main_chat_entry": "The current revision is complete.",
                "reason": "current attempt only",
            }

        observer = WorkObserverCoordinator()
        observer.configure(
            observer_llm=decide,
            get_recent_chat=lambda _session_id: [
                {
                    "role": "assistant",
                    "content": "The obsolete built-in AI version is complete.",
                }
            ],
            display_language=lambda: "English",
        )
        note = {
            **_note(
                "The amended main-assistant integration passed validation.",
                "terminal",
                phase="Result",
            ),
            "metadata": {
                "narration_keypoint": "terminal",
                "work_item_id": "work-amended",
                "attempt_id": "attempt-current",
            },
        }
        session = ObserverSession(
            narration_id="work:work-amended:attempt:attempt-current",
            run_id="run-current",
            session_id="session-current",
            provider="future_provider",
            work_item_id="work-amended",
            attempt_id="attempt-current",
        )
        decision = await observer._decide_with_llm(session, note, [note])
        assert decision is not None
        assert captured[0]["recent_chat"] == []
        assert captured[0]["notes"] == [note]

    asyncio.run(scenario())


def test_status_query_consumes_progress_but_never_terminal_truth() -> None:
    observer = WorkObserverCoordinator()
    progress_session = ObserverSession(
        narration_id="run-status-progress",
        run_id="run-status-progress",
        session_id="session-status",
        provider="future_provider",
        work_item_id="work-status",
    )
    observer._sessions[progress_session.narration_id] = progress_session
    observer._narration_governor.observe(
        {
            **_note("Validation is still running.", "semantic_progress"),
            "run_id": progress_session.run_id,
        },
        output_busy=True,
    )
    assert observer.supersede_for_status_query("work-status") == 1
    assert not observer._narration_governor.has_pending(progress_session.narration_id)

    terminal_session = ObserverSession(
        narration_id="run-status-terminal",
        run_id="run-status-terminal",
        session_id="session-status",
        provider="future_provider",
        work_item_id="work-status",
    )
    observer._sessions[terminal_session.narration_id] = terminal_session
    observer._narration_governor.observe(
        {
            **_note("All validation passed.", "terminal", phase="Result"),
            "run_id": terminal_session.run_id,
        },
        output_busy=True,
    )
    assert observer.supersede_for_status_query("work-status") == 0
    assert observer._narration_governor.pending_is_terminal(
        terminal_session.narration_id
    )


def test_explicit_status_query_reuses_the_existing_narrator_and_attempt_context() -> None:
    async def scenario() -> None:
        from server import task_lookup

        captured: list[dict] = []

        async def narrator(**kwargs) -> dict:
            captured.append(kwargs)
            return {
                # The model may choose a passive channel for an ordinary
                # background note; an explicit question is still owed a reply.
                "action": "subtitle",
                "display_text": "棋盘启动失败的原因已经定位，正在完成验证。",
                "main_chat_entry": "",
                "append_to_main_chat": False,
                "speak": False,
            }

        observer = WorkObserverCoordinator()
        observer._observer_llm = narrator
        observer._get_display_language = lambda: "simplified_chinese"
        session = ObserverSession(
            narration_id="work:work-status:attempt:attempt-status",
            run_id="run-status",
            session_id="session-status",
            provider="future_provider",
            work_item_id="work-status",
            attempt_id="attempt-status",
        )
        session.add_decision(
            {
                "action": "speak",
                "display_text": "我已经开始检查接入问题。",
                "speak": True,
                "speech_status": "queued",
            }
        )
        observer._sessions[session.narration_id] = session
        note = task_lookup.status_query_narration_note(
            {
                "work_item_id": "work-status",
                "attempt_id": "attempt-status",
                "title": "Adapt the board to AUIP",
                "execution": "running",
                "activity_phase": "working",
                "completion": "unknown",
                "attention": "none",
                "activity_milestones": {
                    "diagnostic": {
                        "summary": (
                            "The board failed because the initial snapshot ran "
                            "before state initialization."
                        ),
                        "observedAt": 5,
                    }
                },
            },
            session_id="session-status",
        )

        decision = await observer.compose_status_query_reply(note)

        assert decision is not None
        assert decision["action"] == "speak"
        assert decision["speak"] is True
        assert decision["append_to_main_chat"] is True
        assert decision["main_chat_entry"] == decision["display_text"]
        assert "initial snapshot" in captured[0]["note"]["summary"]
        assert captured[0]["recent_spoken_updates"] == [
            {
                "action": "speak",
                "text": "我已经开始检查接入问题。",
                "terminal": False,
            }
        ]

        observer.record_status_query_delivery(
            note,
            decision,
            {"speech_status": "queued"},
        )
        assert session.recent_spoken_updates()[-1]["text"] == decision["display_text"]

    asyncio.run(scenario())


if __name__ == "__main__":
    test_terminal_merge_keeps_three_semantic_milestones_over_mechanics()
    test_same_permission_issue_speaks_once_until_state_changes()
    test_terminal_arriving_during_progress_decision_is_not_consumed()
    test_terminal_retires_queued_nonterminal_speech_for_same_work_item()
    test_semantic_decision_keeps_consumed_keypoint_provenance()
    test_terminal_narrator_receives_only_successfully_queued_prior_speech()
    test_terminal_voice_omits_exact_sentences_already_spoken_but_keeps_full_history()
    test_terminal_narrator_cannot_reuse_a_predecessor_attempt_from_chat()
    test_status_query_consumes_progress_but_never_terminal_truth()
    test_explicit_status_query_reuses_the_existing_narrator_and_attempt_context()
    print("ok: narration prioritises semantic milestones and permission state changes")
