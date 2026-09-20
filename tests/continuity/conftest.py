from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.continuity import ContinuityStore
# Keep this import package-relative: a dependency that ships a top-level
# `tests` package must not shadow this suite's own helpers.
from ._support import FakeClock


@pytest.fixture
def continuity_store(tmp_path):
    store = ContinuityStore(tmp_path / "continuity.sqlite3")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def fake_clock():
    return FakeClock(datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))
