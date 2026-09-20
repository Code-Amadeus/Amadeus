"""Deterministic C6 daily Character Life scheduling.

The scheduler is intentionally model-free. A local date plus a character seed
produces the same plan across windows, sessions, and restarts. The resulting
state is SIMULATED_LIFE, never Canon or Host-verified fact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

from core.continuity.models import LifeScheduleItemPlan, LifeThread


@dataclass(frozen=True, slots=True)
class CharacterLifeProfile:
    character_id: str
    profile_version: str
    seed: str
    time_slots: tuple[dict[str, str], ...]
    activity_pools: dict[str, tuple[str, ...]]
    thread_templates: tuple[dict[str, object], ...]
    outcomes: dict[str, tuple[dict[str, str], ...]]
    offline_daily_summary: str
    offline_gap_summary: str

    @classmethod
    def load(cls, path: str | Path) -> "CharacterLifeProfile":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if str(raw.get("fact_source") or "").upper() != "SIMULATED_LIFE":
            raise ValueError("Character Life profile must declare SIMULATED_LIFE fact_source")
        pools = {
            str(name): tuple(str(item) for item in values if str(item).strip())
            for name, values in dict(raw.get("activity_pools") or {}).items()
        }
        outcomes = {
            str(name): tuple(dict(item) for item in values if isinstance(item, dict))
            for name, values in dict(raw.get("outcomes") or {}).items()
        }
        offline = dict(raw.get("offline_summaries") or {})
        profile = cls(
            character_id=str(raw.get("character_id") or "").strip(),
            profile_version=str(raw.get("profile_version") or "").strip(),
            seed=str(raw.get("seed") or "").strip(),
            time_slots=tuple(dict(item) for item in raw.get("time_slots") or () if isinstance(item, dict)),
            activity_pools=pools,
            thread_templates=tuple(dict(item) for item in raw.get("thread_templates") or () if isinstance(item, dict)),
            outcomes=outcomes,
            offline_daily_summary=str(offline.get("daily") or "A coarse offline continuity marker was kept without detailed reconstruction."),
            offline_gap_summary=str(offline.get("gap") or "A longer offline gap passed without detailed reconstruction."),
        )
        if not profile.character_id or not profile.profile_version or not profile.seed:
            raise ValueError("Character Life profile requires character_id, profile_version, and seed")
        if not profile.time_slots:
            raise ValueError("Character Life profile requires at least one time slot")
        return profile


class DeterministicLifeScheduler:
    def __init__(self, profile: CharacterLifeProfile) -> None:
        self.profile = profile

    def seed_digest(self, local_date: str) -> str:
        material = f"{self.profile.character_id}\x1f{local_date}".encode("utf-8")
        return hmac.new(self.profile.seed.encode("utf-8"), material, hashlib.sha256).hexdigest()

    def _rng(self, local_date: str, *, suffix: str = "schedule") -> random.Random:
        digest = hmac.new(
            self.profile.seed.encode("utf-8"),
            f"{self.profile.character_id}\x1f{local_date}\x1f{suffix}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return random.Random(int.from_bytes(digest[:8], "big", signed=False))

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return time(hour=hour, minute=minute)

    def _thread_choice(self, local_date: str, active_threads: Iterable[LifeThread]) -> tuple[str, str, tuple[str, ...]]:
        active = sorted(
            (thread for thread in active_threads if thread.status.value == "active"),
            key=lambda item: (item.created_at, item.thread_key),
        )
        templates = {str(item.get("key") or ""): item for item in self.profile.thread_templates}
        if active:
            selected = active[0]
            template = templates.get(selected.thread_key, {})
            titles = tuple(str(v) for v in template.get("activity_titles") or () if str(v).strip())
            return selected.thread_key, selected.title, titles
        candidates = [item for item in self.profile.thread_templates if str(item.get("key") or "").strip()]
        if not candidates:
            return "", "", ()
        item = self._rng(local_date, suffix="thread").choice(candidates)
        key = str(item.get("key") or "")
        title = str(item.get("title") or key)
        titles = tuple(str(v) for v in item.get("activity_titles") or () if str(v).strip())
        return key, title, titles

    def generate(self, *, local_date: str, tzinfo, active_threads: Iterable[LifeThread] = ()) -> tuple[LifeScheduleItemPlan, ...]:
        parsed_date = date.fromisoformat(local_date)
        rng = self._rng(local_date)
        thread_key, thread_title, thread_titles = self._thread_choice(local_date, active_threads)
        plans: list[LifeScheduleItemPlan] = []
        threaded = False
        for ordinal, slot in enumerate(self.profile.time_slots):
            category = str(slot.get("category") or "daily_life")
            pool_name = str(slot.get("pool") or category)
            pool = self.profile.activity_pools.get(pool_name) or ("Keep the simulated day intentionally light.",)
            title = rng.choice(pool)
            use_thread = category in {"research", "analysis"} and thread_key and not threaded
            if use_thread:
                if thread_titles:
                    title = rng.choice(thread_titles)
                threaded = True
            starts = datetime.combine(parsed_date, self._parse_hhmm(str(slot.get("start") or "09:00")), tzinfo=tzinfo)
            ends = datetime.combine(parsed_date, self._parse_hhmm(str(slot.get("end") or "10:00")), tzinfo=tzinfo)
            if ends <= starts:
                raise ValueError(f"life slot end must be after start: {slot!r}")
            plans.append(
                LifeScheduleItemPlan(
                    ordinal=ordinal,
                    category=category,
                    title=title,
                    starts_at=starts.timestamp(),
                    ends_at=ends.timestamp(),
                    thread_key=thread_key if use_thread else "",
                    thread_title=thread_title if use_thread else "",
                )
            )
        return tuple(plans)

    def outcome(self, *, category: str, item_id: str) -> tuple[str, str, str]:
        options = self.profile.outcomes.get(category) or ()
        if not options:
            return (
                "The simulated activity ended without a detailed reconstructed result.",
                "neutral",
                "No additional durable claim is created.",
            )
        digest = hmac.new(self.profile.seed.encode("utf-8"), item_id.encode("utf-8"), hashlib.sha256).digest()
        chosen = options[int.from_bytes(digest[:4], "big") % len(options)]
        return (
            str(chosen.get("result") or ""),
            str(chosen.get("feeling") or ""),
            str(chosen.get("follow_up") or ""),
        )
