from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from core.continuity import ContinuityStore


@dataclass
class FakeClock:
    current: datetime
    monotonic_value: float = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.monotonic_value

    def advance(self, **delta) -> None:
        step = timedelta(**delta)
        self.current = self.current + step
        self.monotonic_value += step.total_seconds()

    def set_wall(self, value: datetime) -> None:
        self.current = value


def open_test_store(root: Path) -> ContinuityStore:
    return ContinuityStore(root / "runtime" / "continuity.sqlite3")
