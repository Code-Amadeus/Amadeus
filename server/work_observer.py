"""Observer runtime for provider work sessions.

Provider runs are not normal chat turns. They are short-lived work sessions
that Kurisu can observe, summarize, and fold into the main conversation when a
user-visible conclusion is worth preserving.

This runtime is intentionally low priority:
- it never starts a main chat turn
- it only enqueues TTS after main chat and playback are idle
- it waits while the main chat is busy
- raw provider mechanics stay silent because canvas already carries them
- sparse execution directions may be spoken without becoming outcome facts
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from config import settings
from server.event_bus import bus
from server.protocol import Method
from server.assistant_language import text_matches_assistant_language
from server.outcome_verification import localize_outcome_verdict
from server.work_context import clear_work_run
from server.work_narration_governor import (
    MANDATORY_NARRATION_KEYPOINTS,
    WorkNarrationGovernor,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ObserverSession:
    narration_id: str
    run_id: str
    session_id: str
    provider: str
    work_item_id: str = ""
    attempt_id: str = ""
    goal: str = ""
    status: str = "running"
    notes: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    closed_at: float | None = None
    last_progress_decision_at: float = 0.0

    def add_note(self, note: dict[str, Any]) -> None:
        item = dict(note)
        item["created_at"] = float(item.get("created_at") or time.time())
        self.notes.append(item)
        self.notes = self.notes[-48:]
        self.updated_at = time.time()
        note_run_id = str(item.get("run_id") or "").strip()
        if note_run_id:
            self.run_id = note_run_id
        if not self.goal:
            self.goal = str(item.get("summary") or item.get("title") or "")
        if str(item.get("phase") or "").lower() == "result":
            self.status = "result"

    def should_decide(self, note: dict[str, Any]) -> bool:
        phase = str(note.get("phase") or "").lower()
        importance = str(note.get("importance") or "normal").lower()
        if phase in {"result", "checkpoint"} or importance in {"important", "blocking", "urgent", "error"}:
            return True
        if phase == "work" and importance == "normal" and self._has_report_signal(note):
            now = time.time()
            if now - self.last_progress_decision_at >= 18.0:
                self.last_progress_decision_at = now
                return True
        return False

    def close(self) -> None:
        self.status = "closed"
        self.closed_at = time.time()

    def recent_notes(self, limit: int = 12) -> list[dict[str, Any]]:
        return [dict(item) for item in self.notes[-max(1, limit):]]

    def add_decision(self, decision: dict[str, Any]) -> None:
        self.decisions.append(dict(decision))
        self.decisions = self.decisions[-12:]
        self.updated_at = time.time()

    def recent_spoken_updates(self, limit: int = 3) -> list[dict[str, Any]]:
        """Return bounded prose that this Attempt already sent to TTS.

        Provider facts say what happened.  This view says only what the
        narrator already told the person, so a later terminal report can add
        the final truth without repeating a just-spoken milestone.
        """

        updates: list[dict[str, Any]] = []
        for decision in reversed(self.decisions):
            if decision.get("speak") is not True:
                continue
            if str(decision.get("speech_status") or "").strip().lower() not in {
                "queued",
                "queued_legacy_sink",
            }:
                continue
            text = str(
                decision.get("display_text")
                or decision.get("main_chat_entry")
                or ""
            ).strip()
            if not text:
                continue
            updates.append(
                {
                    "action": str(decision.get("action") or "speak"),
                    "text": text,
                    "terminal": bool(decision.get("terminal")),
                }
            )
            if len(updates) >= max(1, int(limit)):
                break
        return list(reversed(updates))

    def has_spoken_keypoint(self, *keypoints: str) -> bool:
        expected = {str(value) for value in keypoints if str(value)}
        if not expected:
            return False
        return any(
            decision.get("speak") is True
            and str(decision.get("speech_status") or "").strip().lower()
            in {"queued", "queued_legacy_sink"}
            and expected.intersection(
                str(value)
                for value in decision.get("narration_keypoints") or []
            )
            for decision in self.decisions
        )

    @staticmethod
    def _has_report_signal(note: dict[str, Any]) -> bool:
        summary = " ".join(str(note.get("summary") or "").split())
        if len(summary) < 32:
            return False
        for signal in note.get("signals") or []:
            if not isinstance(signal, dict):
                continue
            if str(signal.get("label") or "").lower() == "report":
                text = " ".join(str(signal.get("text") or "").split())
                return len(text) >= 24
        return False


class WorkObserverCoordinator:
    """Runtime that owns short-lived observer sessions."""

    def __init__(self) -> None:
        self._subscribed = False
        self._closing = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task | None = None
        self._sessions: dict[str, ObserverSession] = {}
        self._is_chat_busy: Callable[[], bool] | None = None
        self._is_tts_busy: Callable[[], bool] | None = None
        self._append_to_main_chat: Callable[[dict[str, Any]], Any] | None = None
        self._narrate: Callable[[dict[str, Any]], Any] | None = None
        self._get_recent_chat: Callable[[str], list[dict[str, str]]] | None = None
        self._get_display_language: Callable[[], str] | None = None
        self._observer_llm = None
        self._release_work: Callable[[str], Any] | None = None
        self._closed_runs: dict[str, float] = {}
        self._delivered_terminal_ids: dict[str, float] = {}
        self._announced_permission_ids: dict[str, float] = {}
        self._permission_issue_states: dict[str, tuple[str, float]] = {}
        self._narration_tasks: dict[str, asyncio.Task] = {}
        # Urgent terminal facts replace an older per-Attempt flush. Keep the
        # cancelled predecessor owned until it actually finishes so shutdown
        # can join it even after the active-task map points at its replacement.
        self._owned_narration_tasks: set[asyncio.Task] = set()
        # Per-attempt cadence is independent, but speech is a single shared
        # character channel. Serialize decisions and enqueueing across tasks so
        # simultaneous background completions cannot talk over each other.
        self._narration_lock = asyncio.Lock()
        self._narration_governor = WorkNarrationGovernor(
            min_interval_s=float(getattr(settings, "WORK_NARRATION_MIN_INTERVAL_S", 20.0)),
            diagnostic_first_n=int(getattr(settings, "WORK_NARRATION_DIAGNOSTIC_FIRST_N", 5)),
            diagnostic_every_n=int(getattr(settings, "WORK_NARRATION_DIAGNOSTIC_EVERY_N", 25)),
        )
        self._terminal_max_wait_s = max(
            1.0,
            float(getattr(settings, "WORK_TERMINAL_NARRATION_MAX_WAIT_S", 20.0)),
        )

    def configure(
        self,
        *,
        is_chat_busy: Callable[[], bool] | None = None,
        is_tts_busy: Callable[[], bool] | None = None,
        append_to_main_chat: Callable[[dict[str, Any]], Any] | None = None,
        narrate: Callable[[dict[str, Any]], Any] | None = None,
        get_recent_chat: Callable[[str], list[dict[str, str]]] | None = None,
        display_language: Callable[[], str] | None = None,
        observer_llm=None,
        release_work: Callable[[str], Any] | None = None,
        narration_min_interval_s: float | None = None,
        narration_diagnostic_first_n: int | None = None,
        narration_diagnostic_every_n: int | None = None,
        terminal_max_wait_s: float | None = None,
    ) -> None:
        if self._closing or self._closed:
            raise RuntimeError("closed work observer cannot be configured")
        self._is_chat_busy = is_chat_busy
        self._is_tts_busy = is_tts_busy
        self._append_to_main_chat = append_to_main_chat
        self._narrate = narrate
        self._get_recent_chat = get_recent_chat
        self._get_display_language = display_language
        self._observer_llm = observer_llm
        self._release_work = release_work
        if terminal_max_wait_s is not None:
            self._terminal_max_wait_s = max(1.0, float(terminal_max_wait_s))
        if any(
            value is not None
            for value in (
                narration_min_interval_s,
                narration_diagnostic_first_n,
                narration_diagnostic_every_n,
            )
        ):
            self._narration_governor = WorkNarrationGovernor(
                min_interval_s=(
                    float(narration_min_interval_s)
                    if narration_min_interval_s is not None
                    else self._narration_governor.min_interval_s
                ),
                diagnostic_first_n=(
                    int(narration_diagnostic_first_n)
                    if narration_diagnostic_first_n is not None
                    else self._narration_governor.diagnostic_first_n
                ),
                diagnostic_every_n=(
                    int(narration_diagnostic_every_n)
                    if narration_diagnostic_every_n is not None
                    else self._narration_governor.diagnostic_every_n
                ),
            )
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=96)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_loop(), name="work-observer-runtime")
        if not self._subscribed:
            bus.on(Method.CHAT_WORK_NOTE, self._on_work_note)
            self._subscribed = True

    async def close(self) -> None:
        """Stop accepting work facts and await every owned background task.

        Work narration is deliberately asynchronous during normal operation,
        but those tasks still belong to this coordinator.  Closing first
        seals intake and task creation, then cancels and joins the worker and
        per-Attempt flushes so no coroutine survives backend shutdown.
        """

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            if self._subscribed:
                bus.off(Method.CHAT_WORK_NOTE, self._on_work_note)
                self._subscribed = False

            current = asyncio.current_task()
            owned_tasks = {
                task
                for task in (self._worker, *self._owned_narration_tasks)
                if task is not None and task is not current
            }
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await asyncio.gather(*owned_tasks, return_exceptions=True)

            self._worker = None
            self._narration_tasks.clear()
            self._owned_narration_tasks.clear()
            queue = self._queue
            if queue is not None:
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    else:
                        queue.task_done()
            self._queue = None
            for narration_id in tuple(self._sessions):
                self._narration_governor.drop(narration_id)
            self._sessions.clear()
            self._closed = True
            self._closing = False

    def get_session(self, run_id: str) -> ObserverSession | None:
        clean = str(run_id or "")
        direct = self._sessions.get(clean)
        if direct is not None:
            return direct
        return next(
            (session for session in self._sessions.values() if session.run_id == clean),
            None,
        )

    def supersede_for_status_query(self, work_item_id: str) -> int:
        """Consume already-known nonterminal narration for an explicit query.

        The ledger answer includes the latest semantic milestone and liveness
        fact, so replaying a pending background update immediately before or
        after it is stale.  Terminal truth is never consumed here.
        """

        return self._supersede_nonterminal(
            work_item_id,
            reason="status query",
        )

    def supersede_for_steer(self, work_item_id: str) -> int:
        """Retire nonterminal narration from the superseded instruction."""

        return self._supersede_nonterminal(
            work_item_id,
            reason="steer checkpoint",
        )

    def _supersede_nonterminal(self, work_item_id: str, *, reason: str) -> int:
        target = str(work_item_id or "").strip()
        if not target:
            return 0
        consumed = 0
        for narration_id, session in tuple(self._sessions.items()):
            if session.work_item_id != target:
                continue
            if self._narration_governor.pending_is_terminal(narration_id):
                continue
            if self._narration_governor.has_pending(narration_id):
                self._narration_governor.mark_consumed(narration_id)
                consumed += 1
            task = self._narration_tasks.pop(narration_id, None)
            if task is not None and not task.done():
                task.cancel()
        if consumed:
            logger.info(
                "[WORK-NARRATION] %s superseded %d pending update(s) for %s",
                reason,
                consumed,
                target,
            )
        self._supersede_pending_delivery(target)
        return consumed

    async def compose_status_query_reply(
        self,
        note: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Let the existing Narrator express one Host-resolved status fact.

        Task lookup owns identity and ledger truth before this method runs.
        This method owns only character wording; it does not enqueue the note,
        advance cadence, or mutate Work lifecycle state.
        """

        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        if metadata.get("status_query") is not True:
            raise ValueError("status narration requires a status_query note")
        session = self._status_query_session(note)
        notes = [*session.recent_notes(limit=9), dict(note)]
        model_decision = await self._decide_with_llm(session, note, notes)
        if not model_decision:
            return None
        decision = self._merge_decision_defaults(model_decision, session, note)
        display_text = str(
            decision.get("display_text") or decision.get("main_chat_entry") or ""
        ).strip()
        if not display_text:
            return None
        reason = str(decision.get("reason") or "").strip()
        return {
            **decision,
            "source": "work_status_narrator",
            "action": "speak",
            "terminal": False,
            "append_to_main_chat": True,
            "speak": True,
            "display_text": display_text,
            "main_chat_entry": display_text,
            "reason": (
                f"{reason}; " if reason else ""
            ) + "explicit status query was expressed by the Work Narrator",
        }

    def record_status_query_delivery(
        self,
        note: dict[str, Any],
        decision: dict[str, Any],
        delivery: dict[str, Any],
    ) -> None:
        """Retain an explicit spoken reply for later anti-repetition context."""

        session = self._matching_status_query_session(note)
        if session is None:
            return
        speech_status = str(delivery.get("speech_status") or "").strip().lower()
        session.add_decision(
            {
                **decision,
                "speak": speech_status in {"queued", "queued_legacy_sink"},
                "speech_status": speech_status,
            }
        )

    def _status_query_session(self, note: dict[str, Any]) -> ObserverSession:
        existing = self._matching_status_query_session(note)
        if existing is not None:
            return existing
        work_item_id, attempt_id = self._work_identity(note)
        return ObserverSession(
            narration_id=f"status-query:{work_item_id or time.time_ns()}",
            run_id=str(note.get("run_id") or ""),
            session_id=str(note.get("session_id") or ""),
            provider=str(note.get("provider") or "provider"),
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            goal=str(note.get("title") or note.get("summary") or ""),
        )

    def _matching_status_query_session(
        self,
        note: dict[str, Any],
    ) -> ObserverSession | None:
        work_item_id, attempt_id = self._work_identity(note)
        if not work_item_id:
            return None
        candidates = [
            session
            for session in self._sessions.values()
            if session.work_item_id == work_item_id
            and (not attempt_id or not session.attempt_id or session.attempt_id == attempt_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda session: session.updated_at)

    @staticmethod
    def _supersede_pending_delivery(work_item_id: str) -> int:
        """Retire queued nonterminal speech made stale by newer Work truth.

        The source policy lives here, not in the shared delivery/TTS layer.
        Active playback and terminal speech are deliberately left untouched.
        """

        target = str(work_item_id or "").strip()
        if not target:
            return 0
        try:
            from server.vn_tts_bridge import cancel_pending_vn_tts
            from tts.pipeline import discard_pending_tts

            cancelled = cancel_pending_vn_tts(
                source="work_observer",
                work_item_id=target,
                nonterminal_only=True,
            )
            discarded = discard_pending_tts(
                source="work_observer",
                work_item_id=target,
                nonterminal_only=True,
            )
            return int(cancelled or 0) + int(discarded or 0)
        except Exception:
            logger.exception(
                "failed to supersede queued nonterminal narration for %s",
                target,
            )
            return 0

    async def _on_work_note(self, _method: str, params: dict[str, Any]) -> None:
        if self._closing or self._closed or self._queue is None:
            return
        note = dict(params or {})
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        if str(metadata.get("steering_stage") or "").strip().lower() in {
            "steer_queued",
            "steer_applied",
        }:
            work_item_id, _attempt_id = self._work_identity(note)
            self.supersede_for_steer(work_item_id)
        if self._observer_policy(note) == "silent":
            self._trace_drop(note, "observer_policy_silent")
            return
        try:
            self._queue.put_nowait(note)
        except asyncio.QueueFull:
            keep_when_full = self._is_terminal_note(note) or self._is_actionable_export_permission(note)
            if keep_when_full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self._queue.put_nowait(note)
                    logger.warning(
                        "work observer queue full; dropped oldest note to keep %s note",
                        "permission" if self._is_actionable_export_permission(note) else "terminal",
                    )
                    return
                except asyncio.QueueEmpty:
                    pass
                except asyncio.QueueFull:
                    pass
            logger.warning("work observer queue full; dropping note")

    async def _run_loop(self) -> None:
        assert self._queue is not None
        while True:
            note = await self._queue.get()
            try:
                await self._handle_note(note)
            except Exception:
                logger.exception("work observer runtime failed")
            finally:
                self._queue.task_done()

    async def _handle_note(self, note: dict[str, Any]) -> None:
        if self._closing or self._closed:
            return
        run_id = str(note.get("run_id") or "")
        if not run_id:
            return
        narration_id = self._narration_identity(note)
        self._prune_closed_runs()
        actionable_export = self._is_actionable_export_permission(note)
        trusted_terminal = self._is_terminal_note(note)
        if trusted_terminal:
            work_item_id, _attempt_id = self._work_identity(note)
            self._supersede_pending_delivery(work_item_id)
        permission_issue_id, permission_state = self._permission_issue_state(note)
        if permission_issue_id:
            previous = self._permission_issue_states.get(permission_issue_id)
            if previous is not None and previous[0] == permission_state:
                self._trace_drop(note, "permission_state_unchanged")
                return
            self._permission_issue_states[permission_issue_id] = (
                permission_state,
                time.time(),
            )
        if actionable_export:
            metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
            request_id = str(metadata.get("permission_request_id") or "").strip()
            if request_id and request_id in self._announced_permission_ids:
                return
        if narration_id in self._closed_runs and not actionable_export:
            if trusted_terminal and narration_id not in self._delivered_terminal_ids:
                # Recover from a legacy/erroneous progress close.  A terminal
                # that has already been delivered remains idempotent, while a
                # real Result that was never delivered is allowed through.
                self._closed_runs.pop(narration_id, None)
                self._narration_governor.drop(narration_id)
                logger.warning(
                    "[WORK-NARRATION] reopening closed narration for an undelivered "
                    "trusted terminal run_id=%s",
                    narration_id,
                )
            else:
                self._trace_drop(
                    note,
                    "terminal_already_delivered"
                    if trusted_terminal
                    else "run_already_closed",
                )
                return
        if self._observer_policy(note) == "silent":
            return
        session = self._session_for(note, narration_id=narration_id)
        session.add_note(note)
        cadence_note = {**note, "run_id": narration_id}
        gate = self._narration_governor.observe(
            cadence_note,
            output_busy=self._output_is_busy(),
        )
        if not gate.keypoint:
            return
        # Fact ingestion must never wait on the Observer model or TTS.  Keeping
        # the queue free lets a terminal fact supersede a slower progress
        # decision instead of sitting behind stale "still checking" speech.
        # The flush worker already owns cadence, output-busy and serialization.
        self._ensure_narration_flush(narration_id, urgent=gate.terminal)

    def _session_for(
        self,
        note: dict[str, Any],
        *,
        narration_id: str,
    ) -> ObserverSession:
        run_id = str(note.get("run_id") or "")
        session = self._sessions.get(narration_id)
        if session is not None:
            return session
        work_item_id, attempt_id = self._work_identity(note)
        session = ObserverSession(
            narration_id=narration_id,
            run_id=run_id,
            session_id=str(note.get("session_id") or ""),
            provider=str(note.get("provider") or "provider"),
            work_item_id=work_item_id,
            attempt_id=attempt_id,
            goal=str(note.get("summary") or note.get("title") or ""),
        )
        self._sessions[narration_id] = session
        return session

    @classmethod
    def _narration_identity(cls, note: dict[str, Any]) -> str:
        work_item_id, attempt_id = cls._work_identity(note)
        if work_item_id and attempt_id:
            return f"work:{work_item_id}:attempt:{attempt_id}"
        return str(note.get("run_id") or "")

    @staticmethod
    def _work_identity(note: dict[str, Any]) -> tuple[str, str]:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        work_item_id = str(
            metadata.get("work_item_id")
            or metadata.get("workItemId")
            or work.get("work_item_id")
            or work.get("workItemId")
            or ""
        ).strip()
        attempt_id = str(
            metadata.get("attempt_id")
            or metadata.get("attemptId")
            or work.get("attempt_id")
            or work.get("attemptId")
            or ""
        ).strip()
        return work_item_id, attempt_id

    def _ensure_narration_flush(self, narration_id: str, *, urgent: bool = False) -> None:
        if self._closing or self._closed:
            return
        existing = self._narration_tasks.get(narration_id)
        if existing is not None and not existing.done():
            if not urgent:
                return
            existing.cancel()
        task = asyncio.create_task(
            self._run_narration_flush(narration_id),
            name=f"work-narration-flush-{narration_id[:32]}",
        )
        self._narration_tasks[narration_id] = task
        self._owned_narration_tasks.add(task)
        task.add_done_callback(self._owned_narration_tasks.discard)

    async def _run_narration_flush(self, narration_id: str) -> None:
        try:
            while self._narration_governor.has_pending(narration_id):
                terminal = self._narration_governor.pending_is_terminal(narration_id)
                delay = 0.0 if terminal else self._narration_governor.remaining_delay(narration_id)
                if delay > 0.0:
                    await asyncio.sleep(min(delay, 60.0))
                    continue
                # Progress may wait for the voice lane indefinitely; it is
                # optional.  A terminal is owed, so let the serial flush apply
                # its bounded voice wait and truthful text fallback instead of
                # starving behind a stale busy flag forever.
                if self._output_is_busy() and not terminal:
                    await asyncio.sleep(0.5)
                    continue
                if await self._flush_narration(narration_id):
                    # A newer keypoint may have arrived after this flush took
                    # its revision snapshot.  Keep draining the coalesced hold
                    # instead of waiting for an unrelated third note to create
                    # another task.
                    await asyncio.sleep(0)
                    continue
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("work narration flush failed for %s", narration_id)
        finally:
            try:
                current = asyncio.current_task()
            except RuntimeError:
                # The interpreter may finalize a never-awaited test coroutine
                # after its event loop has already closed.  There is no live
                # scheduler on which a replacement flush could run.
                current = None
            if current is not None:
                if self._narration_tasks.get(narration_id) is current:
                    self._narration_tasks.pop(narration_id, None)
                # Close the narrow race where a note observes this task just
                # before it leaves the loop, declines to create a duplicate
                # flush, and becomes pending immediately after the last
                # has_pending() check.
                if (
                    not self._closing
                    and not self._closed
                    and narration_id not in self._closed_runs
                    and self._narration_governor.has_pending(narration_id)
                    and narration_id not in self._narration_tasks
                ):
                    self._ensure_narration_flush(
                        narration_id,
                        urgent=self._narration_governor.pending_is_terminal(
                            narration_id
                        ),
                    )

    async def _flush_narration(self, narration_id: str) -> bool:
        async with self._narration_lock:
            return await self._flush_narration_serial(narration_id)

    async def _flush_narration_serial(self, narration_id: str) -> bool:
        if not self._narration_governor.has_pending(narration_id):
            return True
        terminal = self._narration_governor.pending_is_terminal(narration_id)
        if not terminal and (
            self._output_is_busy() or self._narration_governor.remaining_delay(narration_id) > 0.0
        ):
            return False
        session = self._sessions.get(narration_id)
        if session is None:
            self._narration_governor.drop(narration_id)
            return True
        flush_revision = self._narration_governor.pending_revision(narration_id)
        # A permission card is a request for the user to act, not a progress
        # report, so it is taken out of the merge before anything else looks at
        # it. Merging is what lost it in production on 2026-08-02: the card and
        # the task's terminal note arrived eight seconds apart, both keypoints
        # went pending together, and `_merged_narration_note` represents a
        # merge by its most recent member -- the terminal one. The card lost
        # its identity, this branch never matched, and the whole thing fell
        # through to the observer LLM's discretion, which said nothing. The
        # user heard neither, and a permission nobody hears is a task stalled
        # indefinitely on a person who does not know they are being waited on.
        pending_permission = next(
            (
                candidate
                for candidate in reversed(session.recent_notes(limit=12))
                if self._is_actionable_export_permission(candidate)
                and str(
                    (candidate.get("metadata") or {}).get("permission_request_id") or ""
                ).strip()
                not in self._announced_permission_ids
            ),
            None,
        )
        if pending_permission is not None:
            await self._announce_export_permission(pending_permission)
            if narration_id in self._closed_runs:
                self._sessions.pop(narration_id, None)
                return True
            if not self._narration_governor.pending_is_terminal(narration_id):
                # Nothing but the card was waiting, so the cadence window may
                # close on it as usual.
                self._narration_governor.mark_spoken(
                    narration_id,
                    through_revision=flush_revision,
                )
                return True
            # The card has been heard; the outcome is still owed. Falling
            # through delivers it in this same flush rather than deferring:
            # when a keypoint is already ready, `_handle_note` calls the flush
            # exactly once and discards what it returns, so there is no loop
            # here to come back for a second pass.

        note = self._merged_narration_note(session)
        decision = await self._decide(session, note)
        if not decision:
            self._trace_drop(note, "observer_declined")
            self._narration_governor.mark_consumed(
                narration_id,
                through_revision=flush_revision,
            )
            return True
        decision = self._apply_narration_keypoint_policy(decision, session, note)
        if decision.get("speak") is True:
            decision = self._deduplicate_spoken_voice(
                decision,
                session,
                terminal=terminal,
            )
        if not decision.get("speak"):
            # The observer is allowed to stay quiet, but a terminal that says
            # nothing is indistinguishable from a terminal that never arrived,
            # and the user only finds out by asking why no report came.
            logger.info(
                "[WORK-NARRATION] silent action=%s phase=%s reason=%r run_id=%s",
                str(decision.get("action") or ""),
                str(note.get("phase") or ""),
                str(decision.get("reason") or "")[:80],
                narration_id,
            )
        # The observer model owns wording and channel choice, never the run
        # lifecycle.  Only an actual Result note can close the session.
        terminal = terminal and self._session_has_terminal_note(session)
        spoke = False
        speech_status = "not_requested"
        terminal_output_ready = True
        if decision.get("speak"):
            if terminal:
                terminal_output_ready = (
                    await self._wait_for_terminal_output_idle()
                )
            elif self._output_is_busy():
                return False
            if terminal_output_ready:
                spoke, speech_status = await self._submit_narration(decision)
            else:
                speech_status = "output_busy_text_fallback"
                logger.warning(
                    "[WORK-NARRATION] terminal voice lane remained busy for %.1fs; "
                    "publishing text fallback run_id=%s",
                    self._terminal_max_wait_s,
                    narration_id,
                )
            decision = {
                **decision,
                "speak": spoke,
                "speech_status": speech_status,
            }
            if not spoke:
                logger.warning(
                    "[WORK-NARRATION] TTS did not accept decision; "
                    "publishing truthful text fallback status=%s run_id=%s",
                    speech_status,
                    narration_id,
                )
        session.add_decision(decision)
        notify = self._should_publish_decision(decision, terminal=terminal)
        if notify:
            if terminal:
                # Once voice was queued, give it the same bounded window to
                # finish before the durable receipt is emitted.  A wedged TTS
                # flag still cannot hide the visible report forever.
                if terminal_output_ready and spoke:
                    await self._wait_for_terminal_output_idle()
            else:
                await self._wait_for_output_idle()
            await bus.emit(Method.CHAT_OBSERVER_DECISION, decision)

        if decision.get("append_to_main_chat") and self._append_to_main_chat:
            result = self._append_to_main_chat(decision)
            if hasattr(result, "__await__"):
                await result
        if spoke and terminal and not notify:
            # Terminal decisions are normally always published above.  Keep
            # the exceptional voice-only path bounded by the same deadline;
            # an old unbounded wait here used to undo the text-fallback
            # guarantee after TTS had accepted a line but never became idle.
            await self._wait_for_terminal_output_idle()
        if spoke:
            self._narration_governor.mark_spoken(
                narration_id,
                through_revision=flush_revision,
            )
        else:
            self._narration_governor.mark_consumed(
                narration_id,
                through_revision=flush_revision,
            )

        if terminal:
            await self._ack_terminal_delivery(session)
            await self._release_character_runtime(session.run_id)
            session.close()
            clear_work_run(session.run_id)
            self._sessions.pop(narration_id, None)
            self._closed_runs[narration_id] = time.time()
            self._delivered_terminal_ids[narration_id] = time.time()
        return True

    def _deduplicate_spoken_voice(
        self,
        decision: dict[str, Any],
        session: ObserverSession,
        *,
        terminal: bool,
    ) -> dict[str, Any]:
        """Speak only clauses the same Attempt has not already said.

        This is a presentation postcondition, not a truth rewrite. Terminal
        history remains complete, while a nonterminal update is reduced to its
        novel sentence units or suppressed when it adds nothing.
        """

        full_display = str(decision.get("display_text") or "").strip()
        full_entry = str(
            decision.get("main_chat_entry") or full_display
        ).strip()
        spoken_units = {
            self._spoken_sentence_key(unit)
            for update in session.recent_spoken_updates(limit=12)
            for unit in self._spoken_sentence_units(str(update.get("text") or ""))
            if self._spoken_sentence_key(unit)
        }
        if not full_display or not spoken_units:
            return decision
        novel_units = [
            unit
            for unit in self._spoken_sentence_units(full_display)
            if self._spoken_sentence_key(unit) not in spoken_units
        ]
        novel_display = "".join(novel_units).strip()
        if novel_display == full_display:
            return decision
        reason = str(decision.get("reason") or "").strip()
        if not novel_display and not terminal:
            return {
                **decision,
                "action": "silent",
                "append_to_main_chat": False,
                "speak": False,
                "display_text": "",
                "main_chat_entry": "",
                "reason": (
                    f"{reason}; " if reason else ""
                ) + "nonterminal narration already delivered in this Attempt",
            }
        if not novel_display:
            novel_display = self._terminal_voice_closure(
                str(decision.get("display_language") or self._display_language())
            )
        return {
            **decision,
            "display_text": novel_display,
            "main_chat_entry": (
                full_entry
                if terminal
                else (novel_display if decision.get("append_to_main_chat") else "")
            ),
            "reason": (
                f"{reason}; " if reason else ""
            ) + "voice omitted sentences already delivered in this Attempt",
        }

    @staticmethod
    def _spoken_sentence_units(text: str) -> list[str]:
        value = str(text or "").strip()
        if not value:
            return []
        return [
            match.group(0).strip()
            for match in re.finditer(r".+?(?:[。．！？!?.]+|$)", value, flags=re.DOTALL)
            if match.group(0).strip()
        ]

    @staticmethod
    def _spoken_sentence_key(text: str) -> str:
        return "".join(str(text or "").split()).casefold()

    @staticmethod
    def _terminal_voice_closure(display_language: str) -> str:
        language = str(display_language or "").strip().lower().replace("-", "_")
        if language in {"en", "en_us", "english", "英文"}:
            return "That closes this task."
        if language in {
            "zh",
            "zh_cn",
            "chinese",
            "simplified_chinese",
            "中文",
            "简体中文",
        }:
            return "这轮任务到这里已经结束。"
        return "これで今回の作業は終わりよ。"

    async def _submit_narration(self, decision: dict[str, Any]) -> tuple[bool, str]:
        """Get a real enqueue receipt before publishing a decision as spoken."""

        if self._narrate is None:
            return False, "tts_unavailable"
        last_status = "enqueue_failed"
        for attempt in range(2):
            try:
                result = self._narrate(self._narration_payload(decision))
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, dict):
                    last_status = str(result.get("status") or "unknown").strip().lower()
                    if last_status == "queued":
                        return True, "queued"
                else:
                    # Backward-compatible sinks enqueue synchronously and did
                    # not historically return a receipt.
                    return True, "queued_legacy_sink"
            except Exception:
                last_status = "enqueue_error"
                logger.exception("work narration TTS enqueue failed")
            if attempt == 0:
                await asyncio.sleep(0.25)
        return False, last_status

    def _merged_narration_note(self, session: ObserverSession) -> dict[str, Any]:
        notes = session.recent_notes(limit=12)
        pending = self._narration_governor.pending_keypoints(session.narration_id)
        relevant = [
            item
            for item in notes
            if WorkNarrationGovernor.keypoint_for(item) in pending
        ]
        current = next(
            (
                item
                for item in reversed(relevant)
                if WorkNarrationGovernor.keypoint_for(item) in pending
            ),
            notes[-1] if notes else {},
        )
        candidates: list[tuple[int, int, str]] = []
        for index, item in enumerate(relevant):
            keypoint = WorkNarrationGovernor.keypoint_for(item)
            summary = self._trim(str(item.get("summary") or item.get("title") or ""), 180)
            # Intake and the first tool normally use the task as their fallback
            # summary.  It is useful on the visual work surface but is not a
            # progress fact and must never leak into a later merged utterance.
            if keypoint in {"run_started", "first_tool"} and summary == self._trim(
                session.goal,
                180,
            ):
                continue
            if summary:
                candidates.append((self._narration_note_priority(item), index, summary))
        selected: list[tuple[int, int, str]] = []
        seen_summaries: set[str] = set()
        for candidate in sorted(candidates, key=lambda value: (value[0], value[1]), reverse=True):
            if candidate[2] in seen_summaries:
                continue
            selected.append(candidate)
            seen_summaries.add(candidate[2])
            if len(selected) >= 4:
                break
        selected.sort(key=lambda value: value[1])
        merged_summary = " / ".join(value[2] for value in selected)
        metadata = dict(current.get("metadata") if isinstance(current.get("metadata"), dict) else {})
        metadata.update(
            {
                "narration_keypoint": pending[-1] if pending else "",
                "narration_keypoints": pending,
                "narration_merged_count": self._narration_governor.pending_count(session.narration_id),
            }
        )
        return {
            **dict(current),
            "summary": self._trim(merged_summary or str(current.get("summary") or ""), 420),
            "metadata": metadata,
        }

    @staticmethod
    def _narration_note_priority(note: dict[str, Any]) -> int:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        keypoint = WorkNarrationGovernor.keypoint_for(note)
        milestone = str(metadata.get("semantic_milestone") or "").strip().lower()
        if keypoint == "terminal":
            return 100
        if keypoint in {
            "permission_pending",
            "permission_blocked",
            "execution_blocked",
            "export_staged",
        }:
            return 90
        if milestone == "validation":
            return 80
        if milestone == "diagnostic":
            return 78
        if milestone == "capability":
            return 75
        if milestone == "design":
            return 70
        if keypoint == "semantic_progress":
            return 60
        if keypoint == "directional_progress":
            return 50
        if keypoint in {"stalled", "quiet_monitoring"}:
            return 30
        return 10

    def _apply_narration_keypoint_policy(
        self,
        decision: dict[str, Any],
        session: ObserverSession,
        note: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        keypoints = [str(value) for value in (metadata.get("narration_keypoints") or [])]
        annotated = {
            **decision,
            "note_count": len(session.notes),
            "narration_keypoints": keypoints,
            "narration_merged_count": int(
                metadata.get("narration_merged_count") or 0
            ),
        }
        # Intake snapshots and the first tool are useful visual/audit facts but
        # contain no Provider-authored direction or semantic result.
        if keypoints and set(keypoints).issubset({"run_started", "first_tool"}):
            return {
                **annotated,
                "action": "silent",
                "terminal": False,
                "append_to_main_chat": False,
                "speak": False,
                "display_text": "",
                "main_chat_entry": "",
                "reason": "mechanical provider activity stays on the work surface",
            }
        model_text = str(
            decision.get("display_text")
            or decision.get("main_chat_entry")
            or ""
        ).strip()
        source_summary = str(note.get("summary") or note.get("title") or "").strip()
        first_substantive = bool(
            {"directional_progress", "semantic_progress"}.intersection(keypoints)
            and not session.has_spoken_keypoint(
                "directional_progress",
                "semantic_progress",
            )
            and (
                model_text
                or text_matches_assistant_language(
                    source_summary,
                    self._display_language(),
                )
            )
        )
        forced = (
            any(value in MANDATORY_NARRATION_KEYPOINTS for value in keypoints)
            or first_substantive
        )
        if not forced:
            return annotated
        terminal = self._session_has_terminal_note(session)
        # Quiet monitoring is a liveness receipt, not creative narration.  Its
        # wording must not inherit the English diagnostic summary and produce
        # a mixed-language line such as Japanese framing around raw Host text.
        # Keep this policy in the existing Work Observer source layer; the
        # shared delivery/TTS boundary still receives only final prose.
        quiet_included = "quiet_monitoring" in keypoints
        display_text = model_text
        if quiet_included and not model_text:
            display_text = self._quiet_monitoring_summary(
                self._display_language(),
                # The intake gate opens from the newest useful semantic *or*
                # directional update. Use that same clock in user-facing
                # wording; the older verified-semantic clock may be much
                # larger immediately after a truthful directional report.
                silence_s=float(
                    metadata.get("useful_update_silence_s")
                    or metadata.get("semantic_silence_s")
                    or 0.0
                ),
            )
        if not display_text:
            display_text = self._keypoint_summary(
                str(note.get("summary") or note.get("title") or ""),
                self._display_language(),
            )
        action = "final_report" if terminal else str(decision.get("action") or "speak")
        if not terminal and action == "final_report":
            action = "speak"
        if action in {"", "silent", "canvas_update", "subtitle"}:
            action = "final_report" if terminal else "speak"
        return {
            **annotated,
            "action": action,
            "terminal": terminal,
            "speak": True,
            "display_text": display_text,
            "main_chat_entry": (
                str(decision.get("main_chat_entry") or display_text)
                if decision.get("append_to_main_chat") or terminal
                else ""
            ),
            "append_to_main_chat": bool(decision.get("append_to_main_chat")) or terminal,
        }

    @staticmethod
    def _keypoint_summary(summary: str, display_language: str) -> str:
        body = WorkObserverCoordinator._trim(summary, 240)
        if not body:
            return ""
        if re.search(r"[。．！？!?.]$", body):
            return body
        language = str(display_language or "").strip().lower()
        if language == "english":
            return f"{body}."
        return f"{body}。"

    @staticmethod
    def _quiet_monitoring_summary(
        display_language: str,
        *,
        silence_s: float = 0.0,
    ) -> str:
        language = str(display_language or "").strip().lower()
        total_seconds = max(0, round(float(silence_s or 0.0)))
        minutes, seconds = divmod(total_seconds, 60)
        if minutes and seconds:
            duration_ja = f"{minutes}分{seconds}秒間"
            duration_zh = f"{minutes}分{seconds}秒内"
            duration_en = f"for {minutes}m {seconds}s"
        elif minutes:
            duration_ja = f"{minutes}分間"
            duration_zh = f"{minutes}分钟内"
            duration_en = f"for {minutes}m"
        elif seconds:
            duration_ja = f"{seconds}秒間"
            duration_zh = f"{seconds}秒内"
            duration_en = f"for {seconds}s"
        else:
            duration_ja = duration_zh = duration_en = ""
        if language == "japanese":
            if duration_ja:
                return f"処理は続いているけど、確認できる新しい節目は{duration_ja}届いていないわ。"
            return "まだ処理は続いているわ。今のところ、確認できる新しい節目は届いていない。"
        if language == "english":
            if duration_en:
                return f"The work is still running. No new verified milestone has arrived {duration_en}."
            return "The work is still running. There is no new verified milestone yet."
        if duration_zh:
            return f"还在处理中，{duration_zh}没有新的可验证里程碑。"
        return "还在处理中，目前还没有新的可验证里程碑。"

    def _output_is_busy(self) -> bool:
        return self._chat_is_busy() or self._tts_is_busy()

    async def _wait_for_output_idle(self) -> None:
        await asyncio.sleep(0.8)
        while self._chat_is_busy() or self._tts_is_busy():
            await asyncio.sleep(1.0)

    async def _wait_for_terminal_output_idle(self) -> bool:
        try:
            await asyncio.wait_for(
                self._wait_for_output_idle(),
                timeout=self._terminal_max_wait_s,
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _ack_terminal_delivery(self, session: ObserverSession) -> None:
        """Acknowledge only after the terminal decision reached its output boundary.

        TTS acceptance is preferred, but a truthful text fallback also counts:
        voice is presentation while the terminal fact must remain recoverable.
        The ledger keeps the note pending until this receipt arrives.
        """

        terminal_note = None
        for note in reversed(session.notes):
            metadata = note.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if self._is_terminal_note(note) and str(
                metadata.get("delivery_id") or ""
            ).strip():
                terminal_note = note
                break
        if terminal_note is None:
            return
        metadata = (
            terminal_note.get("metadata")
            if isinstance(terminal_note.get("metadata"), dict)
            else {}
        )
        await bus.emit(
            Method.CHAT_WORK_NOTE_DELIVERED,
            {
                "delivery_id": str(metadata.get("delivery_id") or ""),
                "work_item_id": str(metadata.get("work_item_id") or session.work_item_id),
                "attempt_id": str(metadata.get("attempt_id") or session.attempt_id),
                "run_id": session.run_id,
                "session_id": session.session_id,
            },
        )

    async def _release_character_runtime(self, run_id: str) -> None:
        try:
            if self._release_work is not None:
                # The activity coordinator owns the shared work presentation and
                # skips the release while sibling runs are still active.
                result = self._release_work(run_id)
                if hasattr(result, "__await__"):
                    await result
                return
            await bus.emit(
                Method.WALLPAPER_ACTIVITY,
                {"activity": "", "reason": "work_observer_terminal", "run_id": run_id},
            )
            from server.character_presentation import coordinator as character_presentation

            await character_presentation.release(
                source_kind="work",
                source_id="active-work",
                scenario="computer-use",
                metadata={"reason": "work_observer_terminal", "run_id": run_id},
            )
        except Exception:
            logger.exception("failed to release SpriteForge runtime after work observer terminal")

    def _prune_closed_runs(self) -> None:
        now = time.time()
        for run_id, closed_at in list(self._closed_runs.items()):
            if now - closed_at > 600.0:
                self._closed_runs.pop(run_id, None)
                self._delivered_terminal_ids.pop(run_id, None)
                self._narration_governor.drop(run_id)
        for request_id, announced_at in list(self._announced_permission_ids.items()):
            if now - announced_at > 3600.0:
                self._announced_permission_ids.pop(request_id, None)
        for issue_id, (_state, observed_at) in list(self._permission_issue_states.items()):
            if now - observed_at > 3600.0:
                self._permission_issue_states.pop(issue_id, None)

    @staticmethod
    def _permission_issue_state(note: dict[str, Any]) -> tuple[str, str]:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        keypoint = WorkNarrationGovernor.keypoint_for(note)
        if keypoint not in {"permission_pending", "permission_blocked", "export_staged"}:
            return "", ""
        issue = metadata.get("permission_issue")
        issue = issue if isinstance(issue, dict) else {}
        material: dict[str, Any] = {
            "work_item_id": str(
                metadata.get("work_item_id") or metadata.get("workItemId") or ""
            ),
            "attempt_id": str(
                metadata.get("attempt_id") or metadata.get("attemptId") or ""
            ),
            "run_id": str(note.get("run_id") or "")
            if not (metadata.get("work_item_id") or metadata.get("workItemId"))
            else "",
            "kind": str(metadata.get("permission_kind") or "provider_permission"),
            "capability": str(issue.get("capability") or ""),
            "action": str(issue.get("action") or metadata.get("permission_action") or ""),
            "scope": [str(value) for value in (issue.get("scope") or []) if str(value)],
            "resources": [
                str(value)
                for value in (metadata.get("permission_filenames") or [])
                if str(value)
            ],
        }
        if not any(
            (
                material["capability"],
                material["action"],
                material["scope"],
                material["resources"],
            )
        ):
            material["request"] = str(metadata.get("permission_request_id") or "")
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True)
        issue_id = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:20]
        state = str(metadata.get("permission_status") or keypoint).strip().lower()
        return issue_id, state

    @staticmethod
    def _trace_drop(note: dict[str, Any], reason: str) -> None:
        """Say out loud that a note will not be spoken, and why.

        Every suppression point here used to return in silence, so a note that
        never became speech left no trace anywhere -- not in the log, not in
        the ledger. Diagnosing the 2026-08-02 sessions meant inferring from a
        sampled cadence counter and getting it wrong three times: a closed run,
        then a frozen keypoint count, then an API timeout mistaken for the
        model declining to route. A dropped note is now a fact you can grep,
        which is the difference between reading the log and guessing at it.
        """

        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        logger.info(
            "[WORK-NARRATION] dropped reason=%s phase=%s keypoint=%s speak=%s run_id=%s title=%r",
            reason,
            str(note.get("phase") or ""),
            str(metadata.get("narration_keypoint") or ""),
            bool(note.get("speak")),
            str(note.get("run_id") or ""),
            str(note.get("title") or "")[:60],
        )

    @staticmethod
    def _is_actionable_export_permission(note: dict[str, Any]) -> bool:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        return bool(
            note.get("speak") is True
            and str(note.get("source") or "").strip().lower() == "work_ledger"
            and metadata.get("permission_actionable") is True
            and str(metadata.get("permission_kind") or "").strip().lower()
            == "desktop_export"
            and str(metadata.get("permission_action") or "").strip().lower()
            == "allow_once"
            and str(metadata.get("permission_request_id") or "").strip()
        )

    async def _announce_export_permission(self, note: dict[str, Any]) -> None:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        request_id = str(metadata.get("permission_request_id") or "").strip()
        self._prune_closed_runs()
        if not request_id or request_id in self._announced_permission_ids:
            return
        decision = self._export_permission_decision(note)
        await self._wait_for_output_idle()
        spoke, speech_status = await self._submit_narration(decision)
        decision = {
            **decision,
            "speak": spoke,
            "speech_status": speech_status,
        }
        await bus.emit(Method.CHAT_OBSERVER_DECISION, decision)
        if not spoke:
            logger.warning(
                "Desktop export permission narration fell back to card only: "
                "request_id=%s status=%s",
                request_id,
                speech_status,
            )
        self._announced_permission_ids[request_id] = time.time()

    def _export_permission_decision(self, note: dict[str, Any]) -> dict[str, Any]:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        filenames_value = metadata.get("permission_filenames")
        filenames = (
            [str(value).strip() for value in filenames_value if str(value).strip()]
            if isinstance(filenames_value, list)
            else []
        )
        language = self._display_language()
        label = ", ".join(filenames[:3]) or "The Desktop file"
        if len(filenames) > 3:
            remaining = len(filenames) - 3
            if str(language or "").strip().lower() == "japanese":
                label = f"{label} ほか{remaining}ファイル"
            elif str(language or "").strip().lower() == "english":
                label = f"{label} and {remaining} more files"
            else:
                label = f"{label} 等另外 {remaining} 个文件"
        display_text = self._export_permission_prompt(label, language)
        return {
            "source": "work_observer_runtime",
            "run_id": str(note.get("run_id") or ""),
            "session_id": str(note.get("session_id") or ""),
            "provider": str(note.get("provider") or "provider"),
            "display_language": language,
            "action": "ask_user",
            "terminal": False,
            "append_to_main_chat": False,
            "speak": True,
            "display_text": display_text,
            "main_chat_entry": "",
            "reason": "actionable Desktop export permission is visible",
            "note_count": 1,
            "permission_request_id": str(metadata.get("permission_request_id") or ""),
        }

    @staticmethod
    def _export_permission_prompt(label: str, display_language: str) -> str:
        language = str(display_language or "").strip().lower()
        if language == "japanese":
            return f"{label} の準備ができました。デスクトップへコピーする前に、権限カードで承認してください。"
        if language == "english":
            return f"{label} is ready. Please approve the permission card before I copy it to your Desktop."
        return f"{label} 已经准备好了。请在权限卡片中确认，我才能把它复制到桌面。"

    @staticmethod
    def _observer_policy(note: dict[str, Any]) -> str:
        return str(note.get("observer_policy") or "auto").strip().lower()

    @staticmethod
    def _is_terminal_note(note: dict[str, Any]) -> bool:
        # Importance controls delivery priority, not lifecycle.  Blocking and
        # error checkpoints still need a later Result and stay open.
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        if metadata.get("execution_started") is False:
            return False
        phase = str(note.get("phase") or "").strip().lower()
        return phase == "result"

    @classmethod
    def _session_has_terminal_note(
        cls,
        session: ObserverSession,
    ) -> bool:
        return any(cls._is_terminal_note(item) for item in session.notes)

    @staticmethod
    def _should_publish_decision(decision: dict[str, Any], *, terminal: bool) -> bool:
        if terminal:
            return True
        if decision.get("append_to_main_chat") or decision.get("speak"):
            return True
        action = str(decision.get("action") or "").strip().lower()
        return action not in {"", "silent"}

    async def _decide(self, session: ObserverSession, note: dict[str, Any]) -> dict[str, Any] | None:
        notes = session.recent_notes(limit=12)
        llm_decision = await self._decide_with_llm(session, note, notes)
        if llm_decision:
            return self._merge_decision_defaults(llm_decision, session, note)
        return self._fallback_decision(session, note, notes)

    async def _decide_with_llm(
        self,
        session: ObserverSession,
        note: dict[str, Any],
        notes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if self._observer_llm is None:
            return None
        recent_chat: list[dict[str, str]] = []
        if self._get_recent_chat is not None:
            try:
                recent_chat = self._get_recent_chat(session.session_id)
            except Exception:
                logger.exception("failed to collect recent chat for observer")
        if self._is_terminal_note(note):
            # Terminal facts are Attempt-scoped.  Prior chat can contain a
            # predecessor Attempt's truthful result, which becomes poisonous
            # evidence after an amendment (the model may fluently repeat the
            # old deliverable).  Voice style already comes from the observer
            # contract; factual content must come only from this Attempt's
            # notes and current terminal record.
            recent_chat = []
        result = self._observer_llm(
            note=note,
            notes=notes,
            recent_chat=recent_chat,
            recent_spoken_updates=session.recent_spoken_updates(),
        )
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, dict) else None

    def _fallback_decision(
        self,
        session: ObserverSession,
        note: dict[str, Any],
        notes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        phase = str(note.get("phase") or "").lower()
        importance = str(note.get("importance") or "normal").lower()
        summary = self._trim(str(note.get("summary") or note.get("title") or ""), 360)
        display_language = self._display_language()

        if phase == "result":
            text = self._guarded_terminal_outcome_summary(note, display_language)
            if not text:
                text = self._terminal_summary(session.provider, summary, notes, display_language)
            return self._decision(
                session,
                action="final_report",
                terminal=True,
                append=True,
                speak=True,
                display_text=text,
                reason="provider result reached; observer session can fold into main chat",
                note_count=len(notes),
            )

        if importance in {"blocking", "error"}:
            text = self._blocking_summary(summary, display_language)
            return self._decision(
                session,
                action="ask_user",
                terminal=False,
                append=True,
                speak=False,
                display_text=text,
                reason="blocking provider note",
                note_count=len(notes),
            )

        return self._decision(
            session,
            action="silent",
            terminal=False,
            append=False,
            speak=False,
            display_text="",
            reason="normal provider progress; canvas already carries the visible detail",
            note_count=len(notes),
        )

    def _decision(
        self,
        session: ObserverSession,
        *,
        action: str,
        terminal: bool,
        append: bool,
        speak: bool,
        display_text: str,
        reason: str,
        note_count: int,
    ) -> dict[str, Any]:
        bounded_text = self._bounded_role_line(display_text)
        return {
            "source": "work_observer_runtime",
            "run_id": session.run_id,
            "session_id": session.session_id,
            "provider": session.provider,
            "work_item_id": session.work_item_id,
            "attempt_id": session.attempt_id,
            "display_language": self._display_language(),
            "action": action,
            "terminal": terminal,
            "append_to_main_chat": append,
            "speak": speak,
            "display_text": bounded_text,
            "main_chat_entry": bounded_text if append else "",
            "reason": reason,
            "note_count": note_count,
        }

    def _merge_decision_defaults(
        self,
        decision: dict[str, Any],
        session: ObserverSession,
        note: dict[str, Any],
    ) -> dict[str, Any]:
        action = str(decision.get("action") or "silent").strip().lower()
        phase = str(note.get("phase") or "").lower()
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        status_query = metadata.get("status_query") is True
        terminal = self._is_terminal_note(note)
        model_claimed_terminal = bool(decision.get("terminal")) or action == "final_report"
        if terminal:
            action = "final_report"
        elif action == "final_report":
            action = "speak" if decision.get("speak") else "subtitle"
        append = bool(decision.get("append_to_main_chat"))
        if model_claimed_terminal and not terminal:
            append = False
        display_text = str(decision.get("display_text") or "")
        main_chat_entry = str(decision.get("main_chat_entry") or display_text)
        if phase == "result" or status_query:
            truth_summary = self._guarded_terminal_outcome_summary(
                note,
                str(decision.get("display_language") or self._display_language()),
            )
            if truth_summary:
                # The LLM may choose the channel and character cadence, but an
                # unverified provider outcome is a host fact. Replace any
                # optimistic wording instead of asking a prompt to refrain
                # from turning process exit into task success.
                display_text = truth_summary
                main_chat_entry = truth_summary
                decision = {
                    **decision,
                    "reason": "unverified provider outcome is narrated from structured host facts",
                }
            elif phase == "result" and not display_text and not main_chat_entry:
                display_text = self._terminal_summary(
                    session.provider,
                    self._trim(str(note.get("summary") or note.get("title") or ""), 360),
                    session.recent_notes(limit=12),
                    str(decision.get("display_language") or self._display_language()),
                )
                main_chat_entry = display_text
            if phase == "result":
                append = True
        if status_query:
            # The Host already established that this is an explicit read-only
            # question. The Narrator owns the words, not whether the person is
            # owed an answer or whether a checkpoint ends the Work lifecycle.
            action = "speak"
            terminal = False
            append = True
        display_language = str(
            decision.get("display_language") or self._display_language()
        )
        display_valid = text_matches_assistant_language(
            display_text,
            display_language,
        )
        main_valid = text_matches_assistant_language(
            main_chat_entry,
            display_language,
        )
        if display_valid and not main_valid:
            main_chat_entry = display_text
            main_valid = True
        elif main_valid and not display_valid:
            display_text = main_chat_entry
            display_valid = True
        elif not display_valid and not main_valid:
            fallback = ""
            if terminal:
                fallback = self._terminal_summary(
                    session.provider,
                    self._trim(str(note.get("summary") or note.get("title") or ""), 360),
                    session.recent_notes(limit=12),
                    display_language,
                )
            if text_matches_assistant_language(fallback, display_language):
                display_text = fallback
                main_chat_entry = fallback
            else:
                # A wrong-language progress line is less useful than silence;
                # the Canvas still retains the provider-neutral facts.
                display_text = ""
                main_chat_entry = ""
                append = False
                decision = {**decision, "speak": False}
                if not terminal:
                    action = "silent"
        display_text = self._bounded_role_line(display_text)
        main_chat_entry = self._bounded_role_line(main_chat_entry, limit=300)
        return {
            **decision,
            "source": str(decision.get("source") or "work_observer_llm"),
            "run_id": str(decision.get("run_id") or session.run_id),
            "session_id": str(decision.get("session_id") or session.session_id),
            "provider": str(decision.get("provider") or session.provider),
            "work_item_id": str(decision.get("work_item_id") or session.work_item_id),
            "attempt_id": str(decision.get("attempt_id") or session.attempt_id),
            "display_language": display_language,
            "action": action,
            "terminal": terminal,
            "append_to_main_chat": append,
            "speak": (bool(decision.get("speak")) or phase == "result")
            and bool(display_text),
            "display_text": display_text,
            "main_chat_entry": main_chat_entry if append else "",
            "note_count": int(decision.get("note_count") or len(session.notes)),
        }

    @staticmethod
    def _guarded_terminal_outcome_summary(
        note: dict[str, Any],
        display_language: str,
    ) -> str:
        """Localize an unverified provider outcome without model inference."""

        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        truth = metadata.get("outcome_verdict")
        truth = truth if isinstance(truth, dict) else {}
        if not truth:
            return ""
        if (
            str(truth.get("completeness") or "") == "complete"
            and truth.get("provider_report_allowed") is True
        ):
            return ""
        execution_status = str(metadata.get("execution_status") or "failed")
        return localize_outcome_verdict(
            truth,
            execution_status=execution_status,
            display_language=display_language,
        )

    def _chat_is_busy(self) -> bool:
        if self._is_chat_busy is None:
            return False
        try:
            return bool(self._is_chat_busy())
        except Exception:
            logger.exception("chat busy probe failed")
            return True

    def _tts_is_busy(self) -> bool:
        if self._is_tts_busy is None:
            return False
        try:
            return bool(self._is_tts_busy())
        except Exception:
            logger.exception("TTS busy probe failed")
            return True

    def _narration_payload(self, decision: dict[str, Any]) -> dict[str, Any]:
        text = str(decision.get("display_text") or decision.get("main_chat_entry") or "").strip()
        run_id = str(decision.get("run_id") or int(time.time()))
        action = str(decision.get("action") or "note").strip().lower() or "note"
        note_count = int(decision.get("note_count") or 0)
        display_language = str(
            decision.get("display_language") or self._display_language()
        )
        line_id = f"work-observer-{run_id}-{action}-{note_count}"
        payload = {
            "display_text": text,
            "display_language": display_language,
            "emotion": "thinking" if decision.get("action") != "final_report" else "happy",
            "duration_ms": 5600,
            "line_id": line_id,
            # Each Observer narration is a complete, standalone voice
            # utterance.  The shared playback layer must close it just like a
            # normal chat reply so ASR and animation do not remain in a
            # speaking turn after the last sentence ends.
            "turn_id": line_id,
            "complete_turn": True,
            "source": "work_observer",
            "action": action,
            "terminal": bool(decision.get("terminal")),
            "work_item_id": str(decision.get("work_item_id") or ""),
            "attempt_id": str(decision.get("attempt_id") or ""),
        }
        if self._is_japanese_voice_text(text, display_language):
            # Observer decisions are already final character prose.  Sending
            # Japanese through the zh->ja streaming bridge makes its 11-char
            # latency cut split Katakana words (エンドレスア/ーケード).  The
            # direct voice contract disables that early cut and preserves the
            # exact sentence the observer chose.
            payload["voice_text_ja"] = text
        return payload

    @staticmethod
    def _private_path_label(path: str) -> str:
        """Keep an artifact name, never a private runtime location."""

        normalized = str(path or "").strip().strip("\"'`").replace("\\", "/")
        leaf = normalized.rstrip("/").rsplit("/", 1)[-1]
        leaf = leaf.rstrip(".,;:!?)]}，；：。！？）】")
        if not leaf or leaf in {".", ".."}:
            return ""
        suffix = leaf.rsplit(".", 1)[-1].casefold() if "." in leaf else ""
        if suffix in {
            "exe",
            "com",
            "cmd",
            "bat",
            "ps1",
            "sh",
            "dll",
            "so",
            "dylib",
        }:
            return ""
        # A basename with an extension can be a useful user-visible artifact.
        # Directory names and extensionless executable locations are omitted.
        if "." in leaf and not leaf.startswith("."):
            return leaf
        return ""

    @staticmethod
    def _strip_private_locations(text: str) -> str:
        """Remove absolute filesystem locations from user-facing narration."""

        value = str(text or "")

        def replace(match: re.Match[str]) -> str:
            return WorkObserverCoordinator._private_path_label(match.group("path"))

        # File URLs are filesystem locations too. Handle them before the bare
        # drive pattern so a replacement cannot leave a misleading file:///.
        value = re.sub(
            r"(?i)\bfile:///(?P<path>(?:[a-z]:/|(?:Users|home|usr|var|opt|tmp|private|mnt|Volumes)/)[^\s\"'`|<>，；;。！？!?（）()\[\]{}]+)",
            replace,
            value,
        )
        value = re.sub(
            r"(?P<quote>[\"'`])(?P<path>(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|usr|var|opt|tmp|private|mnt|Volumes)/)[^\"'`\r\n]*?)(?P=quote)",
            replace,
            value,
        )
        value = re.sub(
            r"(?<![\w:])(?P<path>[A-Za-z]:[\\/][^\s\"'`|<>，；;。！？!?（）()\[\]{}]+)",
            replace,
            value,
        )
        value = re.sub(
            r"(?<![\\\w])(?P<path>\\\\[^\s\"'`|<>，；;。！？!?（）()\[\]{}]+)",
            replace,
            value,
        )
        value = re.sub(
            r"(?<![\w:])(?P<path>/(?:Users|home|usr|var|opt|tmp|private|mnt|Volumes)(?:/[^\s\"'`|<>，；;。！？!?（）()\[\]{}]+)+)",
            replace,
            value,
        )
        value = re.sub(r"([\"'`])\s*\1", "", value)
        # Observer notes use spaced slashes as list separators. Removing path
        # segments can leave empty entries; URLs contain no spaced separators.
        segments = [segment.strip() for segment in re.split(r"\s+/\s+", value)]
        return " / ".join(segment for segment in segments if segment)

    @staticmethod
    def _bounded_role_line(text: str, *, limit: int = 240) -> str:
        """Keep one background update conversational instead of queue-shaped."""

        cleaned = " ".join(
            WorkObserverCoordinator._strip_private_locations(text).split()
        )
        if not cleaned:
            return ""
        # A full stop is a sentence boundary only before whitespace/end.  The
        # earlier character-class split treated the dot in ``index.html`` or
        # ``chess_game.py`` as a sentence, so the three-sentence budget could
        # delete the actual outcome while retaining fragments of filenames.
        pieces = re.findall(
            r".*?(?:[。！？!?]+|\.(?=\s|$)|$)",
            cleaned,
        )
        pieces = [piece for piece in pieces if piece]
        compact = "".join(piece.strip() for piece in pieces[:3]).strip()
        return WorkObserverCoordinator._trim(compact or cleaned, limit)

    @staticmethod
    def _is_japanese_voice_text(text: str, display_language: str) -> bool:
        language = str(display_language or "").strip().lower().replace("-", "_")
        if language in {"ja", "ja_jp", "japanese", "日本語"}:
            return bool(str(text or "").strip())
        return any(
            "\u3040" <= char <= "\u30ff" or "\u31f0" <= char <= "\u31ff"
            for char in str(text or "")
        )

    def _display_language(self) -> str:
        if self._get_display_language is None:
            return "simplified_chinese"
        try:
            return str(self._get_display_language() or "simplified_chinese")
        except Exception:
            logger.exception("observer display language probe failed")
            return "simplified_chinese"

    def _terminal_summary(self, provider: str, summary: str, notes: list[dict[str, Any]], display_language: str) -> str:
        body = self._trim(summary, 180)
        language = str(display_language or "").strip().lower()
        if language == "japanese":
            if body and not text_matches_assistant_language(body, language):
                body = ""
            if not body:
                return "こちらで確認したわ。この作業は終わっている。"
            return f"こちらで確認したわ。この作業は終わった。概要は「{body}」。詳しい根拠はカードに残してある。"
        if language == "english":
            if body and not text_matches_assistant_language(body, language):
                body = ""
            if not body:
                return "I checked it. This background task is finished."
            return f"I checked it. The task is finished. Briefly: {body}. I kept the details on the card."
        if not body:
            return "我这边确认好了，这轮后台工作已经结束。"
        return f"我这边确认好了，这轮后台工作已经结束。简要结果是：{body}。详细来源我保留在卡片里。"

    @staticmethod
    def _blocking_summary(summary: str, display_language: str) -> str:
        language = str(display_language or "").strip().lower()
        if language == "japanese":
            return f"ここで止まっているわ：{summary}。先に少し確認したほうがよさそう。"
        if language == "english":
            return f"This got blocked: {summary}. You may need to check it first."
        return f"这边卡住了：{summary}。你可能需要先看一下。"

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)].rstrip() + "..."
