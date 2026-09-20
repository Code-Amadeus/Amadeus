"""C6 Character Life policy loading.

Character Life is Host-owned simulated derived state. The policy bounds catch-up,
projection, and thread progress; it never grants fact or execution authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LifePolicy:
    policy_version: str = "c6-v1"
    character_id: str = "kurisu"
    profile_relative_path: str = "character_continuity/kurisu.json"
    projection_max_chars: int = 620
    projection_max_lines: int = 5
    detailed_catchup_hours: float = 24.0
    daily_summary_days: int = 3
    max_today_outcomes: int = 4
    max_previous_day_outcomes: int = 3
    thread_progress_step: float = 0.12

    @classmethod
    def load(cls, path: str | Path) -> "LifePolicy":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        data = raw.get("life") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        return cls(
            policy_version=str(data.get("policy_version") or defaults.policy_version),
            character_id=str(data.get("character_id") or defaults.character_id),
            profile_relative_path=str(data.get("profile_relative_path") or defaults.profile_relative_path),
            projection_max_chars=max(128, int(data.get("projection_max_chars", defaults.projection_max_chars))),
            projection_max_lines=max(1, int(data.get("projection_max_lines", defaults.projection_max_lines))),
            detailed_catchup_hours=max(0.0, float(data.get("detailed_catchup_hours", defaults.detailed_catchup_hours))),
            daily_summary_days=max(1, int(data.get("daily_summary_days", defaults.daily_summary_days))),
            max_today_outcomes=max(0, int(data.get("max_today_outcomes", defaults.max_today_outcomes))),
            max_previous_day_outcomes=max(0, int(data.get("max_previous_day_outcomes", defaults.max_previous_day_outcomes))),
            thread_progress_step=max(0.0, min(1.0, float(data.get("thread_progress_step", defaults.thread_progress_step)))),
        )
