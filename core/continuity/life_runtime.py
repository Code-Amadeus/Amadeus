"""C6 Character Life runtime.

This subsystem owns only SIMULATED_LIFE derived state. It can describe a bounded
character-day simulation, but it cannot write Character RAG, redefine user facts,
mutate C5 relationship evidence, or acquire Work/Provider/AUIP/permission authority.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from core.continuity.life_policy import LifePolicy
from core.continuity.life_scheduler import CharacterLifeProfile, DeterministicLifeScheduler
from core.continuity.models import LifeScheduleStatus, LifeSnapshot

if TYPE_CHECKING:  # pragma: no cover
    from core.continuity.store import ContinuityStore


def _identity_hash(session_id: str, turn_id: str) -> str:
    return hashlib.sha256(f"{session_id}\x1f{turn_id}".encode("utf-8")).hexdigest()


class CharacterLifeRuntime:
    def __init__(
        self,
        store: "ContinuityStore",
        *,
        profile: CharacterLifeProfile,
        policy: LifePolicy | None = None,
    ) -> None:
        self.store = store
        self.profile = profile
        self.policy = policy or LifePolicy(character_id=profile.character_id)
        if self.policy.character_id != self.profile.character_id:
            raise ValueError("life policy character_id must match profile character_id")
        self.scheduler = DeterministicLifeScheduler(profile)

    @property
    def character_id(self) -> str:
        return self.profile.character_id

    def _ensure_schedule(self, now: datetime):
        local_date = now.date().isoformat()
        existing = self.store.get_life_schedule(self.character_id, local_date)
        if existing is not None:
            return existing
        active_threads = self.store.list_life_threads(self.character_id, status="active")
        plans = self.scheduler.generate(local_date=local_date, tzinfo=now.tzinfo, active_threads=active_threads)
        return self.store.ensure_life_schedule(
            character_id=self.character_id,
            local_date=local_date,
            profile_version=self.profile.profile_version,
            seed_hash=self.scheduler.seed_digest(local_date),
            generated_at=now.timestamp(),
            items=plans,
        )

    def _complete_item(self, item, *, occurred_at: float) -> None:
        result, feeling, follow_up = self.scheduler.outcome(category=item.category, item_id=item.item_id)
        self.store.complete_life_schedule_item(
            item.item_id,
            occurred_at=occurred_at,
            summary=result,
            feeling=feeling,
            follow_up=follow_up,
            policy=self.policy,
        )

    def _finalize_previous_day(self, local_date: str) -> None:
        schedule = self.store.get_life_schedule(self.character_id, local_date)
        if schedule is None:
            return
        items = self.store.list_life_schedule_items(schedule.schedule_id)
        completed = 0
        for item in items:
            if item.status in {LifeScheduleStatus.COMPLETED, LifeScheduleStatus.SKIPPED}:
                continue
            if completed >= self.policy.max_previous_day_outcomes:
                self.store.set_life_schedule_item_status(item.item_id, LifeScheduleStatus.SKIPPED, observed_at=item.ends_at)
                continue
            self._complete_item(item, occurred_at=item.ends_at)
            completed += 1

    def _bounded_offline_catchup(self, now: datetime) -> None:
        latest = self.store.get_latest_life_schedule(self.character_id, before_local_date=now.date().isoformat())
        if latest is None:
            return
        previous = datetime.fromisoformat(latest.local_date).date()
        gap_days = (now.date() - previous).days
        if gap_days <= 0:
            return
        elapsed_hours = max(0.0, (now.timestamp() - float(latest.generated_at)) / 3600.0)
        if gap_days == 1 and elapsed_hours <= self.policy.detailed_catchup_hours:
            self._finalize_previous_day(latest.local_date)
            return
        if gap_days <= self.policy.daily_summary_days:
            # If the only missing day is the already-existing previous schedule,
            # record one coarse marker instead of inventing detailed outcomes.
            start_offset = 0 if gap_days == 1 else 1
            for offset in range(start_offset, gap_days):
                missing = previous + timedelta(days=offset)
                self.store.record_life_event(
                    character_id=self.character_id,
                    local_date=missing.isoformat(),
                    event_type="catchup_day_summary",
                    summary=self.profile.offline_daily_summary,
                    occurred_at=datetime.combine(missing, datetime.min.time(), tzinfo=now.tzinfo).timestamp(),
                    policy=self.policy,
                    fingerprint_material=f"daily-summary:{self.character_id}:{missing.isoformat()}",
                )
            return
        self.store.record_life_event(
            character_id=self.character_id,
            local_date=now.date().isoformat(),
            event_type="catchup_gap_summary",
            summary=self.profile.offline_gap_summary,
            occurred_at=now.timestamp(),
            policy=self.policy,
            fingerprint_material=f"gap-summary:{self.character_id}:{previous.isoformat()}:{now.date().isoformat()}",
        )

    def _advance_today(self, now: datetime) -> None:
        schedule = self.store.get_life_schedule(self.character_id, now.date().isoformat())
        if schedule is None:
            return
        items = self.store.list_life_schedule_items(schedule.schedule_id)
        generated_outcomes = 0
        now_ts = now.timestamp()
        for item in items:
            if item.status == LifeScheduleStatus.PAUSED:
                continue
            if now_ts >= item.ends_at:
                if item.status not in {LifeScheduleStatus.COMPLETED, LifeScheduleStatus.SKIPPED}:
                    if generated_outcomes < self.policy.max_today_outcomes:
                        self._complete_item(item, occurred_at=item.ends_at)
                        generated_outcomes += 1
                    else:
                        self.store.set_life_schedule_item_status(item.item_id, LifeScheduleStatus.SKIPPED, observed_at=now_ts)
            elif item.starts_at <= now_ts < item.ends_at and item.status == LifeScheduleStatus.PLANNED:
                self.store.set_life_schedule_item_status(item.item_id, LifeScheduleStatus.RUNNING, observed_at=now_ts)

    def ensure_today(self, now: datetime) -> LifeSnapshot:
        self._bounded_offline_catchup(now)
        self._ensure_schedule(now)
        self._advance_today(now)
        return self.snapshot(now)

    def snapshot(self, now: datetime) -> LifeSnapshot:
        return self.store.get_life_snapshot(
            self.character_id,
            local_date=now.date().isoformat(),
            now=now.timestamp(),
        )

    def interrupt_for_chat(self, *, now: datetime, session_id: str, turn_id: str) -> None:
        self.ensure_today(now)
        self.store.pause_current_life_item_for_chat(
            self.character_id,
            local_date=now.date().isoformat(),
            observed_at=now.timestamp(),
            session_id=session_id,
            turn_id=turn_id,
            source_hash=_identity_hash(session_id, turn_id),
            policy=self.policy,
        )

    def resume_after_chat(self, *, now: datetime, session_id: str, turn_id: str) -> None:
        item = self.store.resume_life_item_after_chat(
            self.character_id,
            local_date=now.date().isoformat(),
            observed_at=now.timestamp(),
            session_id=session_id,
            turn_id=turn_id,
            source_hash=_identity_hash(session_id, turn_id),
            policy=self.policy,
        )
        if item is not None and item.status == LifeScheduleStatus.COMPLETED:
            self._complete_item(item, occurred_at=now.timestamp())

    def render_projection(self, *, now: datetime) -> str:
        snapshot = self.snapshot(now)
        return render_life_projection(snapshot, policy=self.policy)


def render_life_projection(snapshot: LifeSnapshot, *, policy: LifePolicy | None = None) -> str:
    """Render bounded qualitative C6-B context without internal IDs or authority leakage."""

    policy = policy or LifePolicy(character_id=snapshot.character_id or "kurisu")
    lines: list[str] = []
    current = snapshot.current_item
    if current is not None:
        if current.status == LifeScheduleStatus.PAUSED:
            lines.append(f"- The current simulated-life activity is paused for this conversation: {current.title}.")
        else:
            lines.append(f"- Current simulated-life activity: {current.title}.")
    elif snapshot.next_item is not None:
        lines.append(f"- Next simulated-life activity later today: {snapshot.next_item.title}.")

    if snapshot.recent_events:
        recent = snapshot.recent_events[0]
        if recent.event_type in {"activity_outcome", "catchup_day_summary", "catchup_gap_summary"} and recent.summary:
            lines.append(f"- Recent simulated-life continuity: {recent.summary}")
    if snapshot.active_threads:
        thread = snapshot.active_threads[0]
        lines.append(f"- Ongoing simulated-life thread: {thread.title}.")

    if not lines:
        return ""
    prefix = (
        "SIMULATED_LIFE context only. It may shape light conversational continuity, but it is not Canon, "
        "a user fact, Host/external verification, permission, or Work state. Do not claim unlisted details."
    )
    selected = lines[: policy.projection_max_lines]
    text = "\n".join((prefix, *selected))
    if len(text) > policy.projection_max_chars:
        text = text[: policy.projection_max_chars].rstrip()
    return text


__all__ = ["CharacterLifeRuntime", "render_life_projection"]
