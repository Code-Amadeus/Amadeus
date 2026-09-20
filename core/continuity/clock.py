"""Reality-clock owner for cross-restart conversational continuity."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from core.continuity.models import ConversationClockState, RealitySnapshot
from core.continuity.store import ContinuityStore


NowProvider = Callable[[], datetime]
MonotonicProvider = Callable[[], float]


def system_local_now() -> datetime:
    return datetime.now().astimezone()


def _aware_local(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.astimezone()
    return value


def _utc_offset_minutes(value: datetime) -> int:
    offset = value.utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else 0


def _from_timestamp(value: float | None, *, tzinfo) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=tzinfo)


class RealityClock:
    """Tracks real local time without asking the language model to infer it."""

    def __init__(
        self,
        store: ContinuityStore,
        *,
        now_provider: NowProvider = system_local_now,
        monotonic_provider: MonotonicProvider = time.monotonic,
        rollback_tolerance_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._now = now_provider
        self._monotonic = monotonic_provider
        self._rollback_tolerance_seconds = max(0.0, float(rollback_tolerance_seconds))
        self._process_started_monotonic: float | None = None

    def _current(self) -> datetime:
        return _aware_local(self._now())

    def current_time(self) -> datetime:
        """Capture the Host wall-clock time at an event boundary."""

        return self._current()

    def _observation_changes(
        self,
        previous: ConversationClockState,
        now: datetime,
    ) -> dict[str, object]:
        wall = float(now.timestamp())
        high_water = previous.wall_clock_high_water_at
        if high_water is None:
            high_water = previous.last_observed_wall_at
        adjusted = bool(
            high_water is not None
            and wall + self._rollback_tolerance_seconds < float(high_water)
        )
        next_high_water = wall if high_water is None else max(float(high_water), wall)
        return {
            "last_observed_wall_at": wall,
            "wall_clock_high_water_at": next_high_water,
            "last_observed_local_date": now.date().isoformat(),
            "last_observed_utc_offset_minutes": _utc_offset_minutes(now),
            "clock_adjusted": adjusted,
            "updated_at": wall,
        }

    def mark_application_started(self) -> ConversationClockState:
        now = self._current()
        self._process_started_monotonic = float(self._monotonic())
        previous = self._store.get_clock_state()
        changes = self._observation_changes(previous, now)
        changes["last_app_started_at"] = float(now.timestamp())
        return self._store.update_clock_state(**changes)

    def mark_user_turn(
        self,
        *,
        session_id: str = "",
        observed_at: datetime | None = None,
    ) -> ConversationClockState:
        now = _aware_local(observed_at) if observed_at is not None else self._current()
        previous = self._store.get_clock_state()
        changes = self._observation_changes(previous, now)
        changes["last_user_turn_at"] = float(now.timestamp())
        if session_id:
            changes["last_session_id"] = str(session_id)
        return self._store.update_clock_state(**changes)

    def mark_assistant_completed(
        self,
        *,
        session_id: str = "",
        successful: bool = True,
        observed_at: datetime | None = None,
    ) -> ConversationClockState:
        now = _aware_local(observed_at) if observed_at is not None else self._current()
        previous = self._store.get_clock_state()
        changes = self._observation_changes(previous, now)
        wall = float(now.timestamp())
        changes["last_assistant_completed_at"] = wall
        if successful:
            changes["last_successful_chat_at"] = wall
        if session_id:
            changes["last_session_id"] = str(session_id)
        return self._store.update_clock_state(**changes)

    def mark_graceful_shutdown(self) -> ConversationClockState:
        now = self._current()
        previous = self._store.get_clock_state()
        changes = self._observation_changes(previous, now)
        changes["last_graceful_shutdown_at"] = float(now.timestamp())
        return self._store.update_clock_state(**changes)

    def snapshot(self) -> RealitySnapshot:
        now = self._current()
        previous = self._store.get_clock_state()
        changes = self._observation_changes(previous, now)
        state = self._store.update_clock_state(**changes)
        now_ts = float(now.timestamp())

        elapsed: float | None = None
        if not state.clock_adjusted and state.last_successful_chat_at is not None:
            delta = now_ts - float(state.last_successful_chat_at)
            if delta >= 0:
                elapsed = delta

        process_elapsed: float | None = None
        if self._process_started_monotonic is not None:
            process_elapsed = max(
                0.0,
                float(self._monotonic()) - self._process_started_monotonic,
            )

        return RealitySnapshot(
            now=now,
            last_user_turn_at=_from_timestamp(state.last_user_turn_at, tzinfo=now.tzinfo),
            last_assistant_completed_at=_from_timestamp(
                state.last_assistant_completed_at,
                tzinfo=now.tzinfo,
            ),
            last_successful_chat_at=_from_timestamp(
                state.last_successful_chat_at,
                tzinfo=now.tzinfo,
            ),
            last_app_started_at=_from_timestamp(state.last_app_started_at, tzinfo=now.tzinfo),
            last_graceful_shutdown_at=_from_timestamp(
                state.last_graceful_shutdown_at,
                tzinfo=now.tzinfo,
            ),
            elapsed_since_last_successful_chat_seconds=elapsed,
            process_elapsed_seconds=process_elapsed,
            clock_adjusted=state.clock_adjusted,
            local_date=now.date().isoformat(),
            utc_offset_minutes=_utc_offset_minutes(now),
            last_session_id=state.last_session_id,
        )
