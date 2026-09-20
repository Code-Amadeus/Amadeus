from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.continuity.clock import RealityClock
from ._support import FakeClock


def test_clock_persists_real_gap_across_restart(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc), 100.0)
    first = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    first.mark_application_started()
    first.mark_user_turn(session_id="session-a")
    fake.advance(minutes=2)
    first.mark_assistant_completed(session_id="session-a")

    fake.advance(days=2, hours=4)
    restarted = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    restarted.mark_application_started()
    snapshot = restarted.snapshot()

    assert snapshot.last_session_id == "session-a"
    assert snapshot.clock_adjusted is False
    assert snapshot.elapsed_since_last_successful_chat_seconds == 2 * 86400 + 4 * 3600


def test_process_elapsed_uses_monotonic_time(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 50.0)
    clock = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    clock.mark_application_started()
    fake.advance(seconds=17)
    assert clock.snapshot().process_elapsed_seconds == 17.0


def test_clock_rollback_is_detected_and_elapsed_is_withheld(continuity_store) -> None:
    fake = FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc), 1.0)
    clock = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
        rollback_tolerance_seconds=60,
    )
    clock.mark_application_started()
    clock.mark_assistant_completed(session_id="session-a")
    fake.advance(hours=2)
    clock.snapshot()

    # Move the wall clock three hours backwards from the last observation.
    fake.set_wall(fake.current - timedelta(hours=3))
    snapshot = clock.snapshot()

    assert snapshot.clock_adjusted is True
    assert snapshot.elapsed_since_last_successful_chat_seconds is None

    # A second observation at the same incorrect wall time must remain unsafe.
    still_adjusted = clock.snapshot()
    assert still_adjusted.clock_adjusted is True
    assert still_adjusted.elapsed_since_last_successful_chat_seconds is None

    # Reliability returns only after wall time catches the previous high-water mark.
    fake.advance(hours=3)
    recovered = clock.snapshot()
    assert recovered.clock_adjusted is False
    assert recovered.elapsed_since_last_successful_chat_seconds is not None


def test_timezone_and_local_date_are_host_observed(continuity_store) -> None:
    tz = timezone(timedelta(hours=9))
    fake = FakeClock(datetime(2026, 9, 13, 0, 30, tzinfo=tz), 5.0)
    clock = RealityClock(
        continuity_store,
        now_provider=fake.now,
        monotonic_provider=fake.monotonic,
    )
    clock.mark_application_started()
    snapshot = clock.snapshot()

    assert snapshot.local_date == "2026-09-13"
    assert snapshot.utc_offset_minutes == 540
    state = continuity_store.get_clock_state()
    assert state.last_observed_local_date == "2026-09-13"
    assert state.last_observed_utc_offset_minutes == 540
