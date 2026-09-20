"""C5 relationship-state policy loading.

Relationship tuning is intentionally centralized and versioned.  The policy is
Host-owned; model output may propose events but cannot choose durable bounds,
decay, replay, or projection behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


_RELATIONSHIP_DIMS = ("familiarity", "trust", "warmth", "respect", "closeness")
_AFFECT_DIMS = ("irritation", "embarrassment", "tension", "playfulness")


def _default_event_caps() -> dict[str, float]:
    return {
        "familiarity": 0.03,
        "trust": 0.05,
        "warmth": 0.05,
        "respect": 0.04,
        "closeness": 0.04,
        "irritation": 0.25,
        "embarrassment": 0.20,
        "tension": 0.25,
        "playfulness": 0.20,
    }


def _default_window_caps() -> dict[str, float]:
    return {
        "familiarity": 0.05,
        "trust": 0.12,
        "warmth": 0.12,
        "respect": 0.10,
        "closeness": 0.10,
        "irritation": 0.55,
        "embarrassment": 0.45,
        "tension": 0.55,
        "playfulness": 0.45,
    }


def _default_affect_half_lives() -> dict[str, float]:
    return {
        "irritation": 2.0,
        "embarrassment": 3.0,
        "tension": 2.0,
        "playfulness": 4.0,
    }


@dataclass(frozen=True, slots=True)
class RelationshipPolicy:
    policy_version: str = "c5-v1"
    minimum_confidence: float = 0.80
    long_term_baseline: float = 0.50
    affect_baseline: float = 0.0
    window_hours: float = 24.0
    event_delta_caps: Mapping[str, float] = field(default_factory=_default_event_caps)
    window_delta_caps: Mapping[str, float] = field(default_factory=_default_window_caps)
    affect_half_life_hours: Mapping[str, float] = field(default_factory=_default_affect_half_lives)
    projection_max_chars: int = 520
    projection_max_lines: int = 4
    relationship_dimensions: tuple[str, ...] = _RELATIONSHIP_DIMS
    affect_dimensions: tuple[str, ...] = _AFFECT_DIMS

    @classmethod
    def load(cls, path: str | Path | None) -> "RelationshipPolicy":
        if path is None:
            return cls()
        policy_path = Path(path)
        if not policy_path.is_file():
            return cls()
        try:
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid continuity policy JSON: {policy_path}") from exc
        section = raw.get("relationship") if isinstance(raw, dict) else {}
        if not isinstance(section, dict):
            section = {}
        defaults = cls()
        event_caps = dict(defaults.event_delta_caps)
        window_caps = dict(defaults.window_delta_caps)
        affect_halves = dict(defaults.affect_half_life_hours)
        for key, value in (section.get("event_delta_caps") or {}).items():
            if key in event_caps:
                event_caps[key] = float(value)
        for key, value in (section.get("window_delta_caps") or {}).items():
            if key in window_caps:
                window_caps[key] = float(value)
        for key, value in (section.get("affect_half_life_hours") or {}).items():
            if key in affect_halves:
                affect_halves[key] = float(value)
        result = cls(
            policy_version=str(section.get("policy_version", defaults.policy_version) or defaults.policy_version),
            minimum_confidence=float(section.get("minimum_confidence", defaults.minimum_confidence)),
            long_term_baseline=float(section.get("long_term_baseline", defaults.long_term_baseline)),
            affect_baseline=float(section.get("affect_baseline", defaults.affect_baseline)),
            window_hours=float(section.get("window_hours", defaults.window_hours)),
            event_delta_caps=event_caps,
            window_delta_caps=window_caps,
            affect_half_life_hours=affect_halves,
            projection_max_chars=int(section.get("projection_max_chars", defaults.projection_max_chars)),
            projection_max_lines=int(section.get("projection_max_lines", defaults.projection_max_lines)),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("relationship policy_version is required")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValueError("relationship minimum_confidence must be 0..1")
        if not 0.0 <= self.long_term_baseline <= 1.0:
            raise ValueError("relationship long_term_baseline must be 0..1")
        if not 0.0 <= self.affect_baseline <= 1.0:
            raise ValueError("relationship affect_baseline must be 0..1")
        if self.window_hours <= 0:
            raise ValueError("relationship window_hours must be positive")
        if not 128 <= self.projection_max_chars <= 2000:
            raise ValueError("relationship projection_max_chars must be 128..2000")
        if not 1 <= self.projection_max_lines <= 8:
            raise ValueError("relationship projection_max_lines must be 1..8")
        expected = set(self.relationship_dimensions) | set(self.affect_dimensions)
        if set(self.event_delta_caps) != expected or set(self.window_delta_caps) != expected:
            raise ValueError("relationship delta-cap policy must cover every dimension exactly")
        if set(self.affect_half_life_hours) != set(self.affect_dimensions):
            raise ValueError("relationship affect half-life policy must cover affect dimensions exactly")
        for name, value in self.event_delta_caps.items():
            if not 0.0 < float(value) <= 1.0:
                raise ValueError(f"invalid relationship event cap for {name}")
        for name, value in self.window_delta_caps.items():
            if not 0.0 < float(value) <= 1.0:
                raise ValueError(f"invalid relationship window cap for {name}")
        for name, value in self.affect_half_life_hours.items():
            if float(value) <= 0.0:
                raise ValueError(f"invalid relationship affect half-life for {name}")

    def event_cap(self, dimension: str) -> float:
        return float(self.event_delta_caps[str(dimension)])

    def window_cap(self, dimension: str) -> float:
        return float(self.window_delta_caps[str(dimension)])

    def affect_half_life(self, dimension: str) -> float:
        return float(self.affect_half_life_hours[str(dimension)])


__all__ = ["RelationshipPolicy"]
