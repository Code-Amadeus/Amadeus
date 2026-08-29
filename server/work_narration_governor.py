"""Bounded cadence policy for spoken provider-work narration.

The work ledger and observer session keep every note.  This governor only
controls when the observer is allowed to turn those notes into speech.  It
keeps one coalesced pending state per run; it never builds a queue of prepared
utterances.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

NARRATION_KEYPOINTS = frozenset(
    {
        "run_started",
        "first_tool",
        "artifact_registered",
        "quiet_monitoring",
        "stalled",
        "permission_pending",
        "permission_blocked",
        "execution_blocked",
        "export_staged",
        "terminal",
    }
)
# These events open an observer decision window.  They do not all force the
# character to speak: first-tool facts are useful on the Slice but contain no
# new task knowledge on their own. New intake producers classify the explicit
# Host-dispatched goal as directional progress instead of a mechanical event.
_NARRATION_TRIGGERS = NARRATION_KEYPOINTS | {
    "directional_progress",
    "semantic_progress",
}

# Silence is not an acceptable decision for facts the user must hear.  Keep
# this narrower than NARRATION_KEYPOINTS so mechanical lifecycle events cannot
# turn into a spoken repetition of the original task.
MANDATORY_NARRATION_KEYPOINTS = frozenset(
    {
        "quiet_monitoring",
        "stalled",
        "permission_pending",
        "permission_blocked",
        "execution_blocked",
        "export_staged",
        "terminal",
    }
)


@dataclass(slots=True)
class NarrationGate:
    run_id: str
    keypoint: str = ""
    terminal: bool = False
    ready: bool = False
    delay_s: float = 0.0


@dataclass(slots=True)
class _RunCadence:
    pending_keypoints: list[str] = field(default_factory=list)
    pending_count: int = 0
    pending_events: list[tuple[int, str]] = field(default_factory=list)
    next_revision: int = 0
    last_spoken_at: float = 0.0


class WorkNarrationGovernor:
    """Rate-limit keypoint narration while retaining a single merged hold."""

    def __init__(
        self,
        *,
        min_interval_s: float = 20.0,
        diagnostic_first_n: int = 5,
        diagnostic_every_n: int = 25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.diagnostic_first_n = max(0, int(diagnostic_first_n))
        self.diagnostic_every_n = max(0, int(diagnostic_every_n))
        self._clock = clock
        self._runs: dict[str, _RunCadence] = {}
        self.counts = {"note_seen": 0, "keypoint": 0, "spoken": 0, "merged": 0}
        self._diagnostic_events = 0

    def observe(self, note: dict[str, Any], *, output_busy: bool) -> NarrationGate:
        run_id = str(note.get("run_id") or "").strip()
        self._record("note_seen", run_id=run_id)
        keypoint = self.keypoint_for(note)
        if not run_id or not keypoint:
            return NarrationGate(run_id=run_id)

        if keypoint in NARRATION_KEYPOINTS:
            self._record("keypoint", run_id=run_id, keypoint=keypoint)
        state = self._runs.setdefault(run_id, _RunCadence())
        had_pending = bool(state.pending_count)
        state.next_revision += 1
        state.pending_events.append((state.next_revision, keypoint))
        state.pending_events = state.pending_events[-64:]
        self._sync_pending(state)

        terminal = keypoint == "terminal"
        remaining = self.remaining_delay(run_id)
        ready = terminal or (not output_busy and remaining <= 0.0)
        if had_pending or not ready:
            self._record("merged", run_id=run_id, keypoint=keypoint)
        return NarrationGate(
            run_id=run_id,
            keypoint=keypoint,
            terminal=terminal,
            ready=ready,
            delay_s=0.0 if terminal else remaining,
        )

    def remaining_delay(self, run_id: str) -> float:
        state = self._runs.get(str(run_id or ""))
        if state is None or state.last_spoken_at <= 0.0:
            return 0.0
        elapsed = self._clock() - state.last_spoken_at
        return max(0.0, self.min_interval_s - elapsed)

    def has_pending(self, run_id: str) -> bool:
        state = self._runs.get(str(run_id or ""))
        return bool(state and state.pending_count)

    def pending_is_terminal(self, run_id: str) -> bool:
        state = self._runs.get(str(run_id or ""))
        return bool(state and "terminal" in state.pending_keypoints)

    def pending_keypoints(self, run_id: str) -> list[str]:
        state = self._runs.get(str(run_id or ""))
        return list(state.pending_keypoints) if state else []

    def pending_count(self, run_id: str) -> int:
        state = self._runs.get(str(run_id or ""))
        return int(state.pending_count) if state else 0

    def pending_revision(self, run_id: str) -> int:
        state = self._runs.get(str(run_id or ""))
        if state is None or not state.pending_events:
            return 0
        return int(state.pending_events[-1][0])

    def mark_spoken(self, run_id: str, *, through_revision: int | None = None) -> None:
        key = str(run_id or "")
        state = self._runs.setdefault(key, _RunCadence())
        state.last_spoken_at = self._clock()
        self._consume(state, through_revision=through_revision)
        self._record("spoken", run_id=key)

    def mark_consumed(self, run_id: str, *, through_revision: int | None = None) -> None:
        """Clear one coalesced hold when policy deliberately stays silent."""
        state = self._runs.get(str(run_id or ""))
        if state is None:
            return
        self._consume(state, through_revision=through_revision)

    def drop(self, run_id: str) -> None:
        self._runs.pop(str(run_id or ""), None)

    @staticmethod
    def _sync_pending(state: _RunCadence) -> None:
        state.pending_count = len(state.pending_events)
        keypoints: list[str] = []
        for _revision, keypoint in state.pending_events:
            if keypoint not in keypoints:
                keypoints.append(keypoint)
        state.pending_keypoints = keypoints[-len(_NARRATION_TRIGGERS) :]

    @classmethod
    def _consume(
        cls,
        state: _RunCadence,
        *,
        through_revision: int | None,
    ) -> None:
        if through_revision is None:
            state.pending_events.clear()
        else:
            boundary = max(0, int(through_revision))
            state.pending_events = [
                item for item in state.pending_events if int(item[0]) > boundary
            ]
        cls._sync_pending(state)

    @staticmethod
    def keypoint_for(note: dict[str, Any]) -> str:
        metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
        explicit = str(metadata.get("narration_keypoint") or "").strip().lower()
        # Legacy snapshots called unverified assistant prose a semantic
        # candidate and deliberately kept it silent. New producers distinguish
        # a user-facing execution direction explicitly instead of granting the
        # prose semantic/result authority.
        if explicit == "semantic_candidate":
            return ""
        if explicit in _NARRATION_TRIGGERS:
            return explicit
        # A Host correction that explicitly says no execution began is an
        # actionable blocking fact, never a Provider terminal.  Keep this
        # defensive inference in the cadence owner so a future producer cannot
        # accidentally turn "nothing started" into "the task finished" merely
        # by choosing the wrong presentation phase.
        if metadata.get("execution_started") is False:
            return "execution_blocked"
        phase = str(note.get("phase") or "").strip().lower()
        if phase == "result":
            return "terminal"
        if metadata.get("permission_actionable") is True:
            return "export_staged"
        if str(metadata.get("attention") or "").strip().lower() == "permission":
            return "permission_pending"
        for signal in note.get("signals") or []:
            if not isinstance(signal, dict):
                continue
            if str(signal.get("label") or "").strip().lower() == "report":
                return "semantic_progress"
        return ""

    def _record(self, name: str, *, run_id: str, keypoint: str = "") -> None:
        self.counts[name] += 1
        self._diagnostic_events += 1
        index = self._diagnostic_events
        if not (
            index <= self.diagnostic_first_n
            or (self.diagnostic_every_n > 0 and index % self.diagnostic_every_n == 0)
        ):
            return
        logger.info(
            "work narration cadence note_seen=%d keypoint=%d spoken=%d merged=%d run_id=%s latest=%s",
            self.counts["note_seen"],
            self.counts["keypoint"],
            self.counts["spoken"],
            self.counts["merged"],
            run_id,
            keypoint or name,
        )
