"""Remove the sensitive Electron startup snapshot before eager runtime imports."""
from __future__ import annotations

import json
import os
from typing import Any

SNAPSHOT_ENV = "AMADEUS_EXTENSIONS"


def pop_snapshot_environment() -> str:
    return os.environ.pop(SNAPSHOT_ENV, "")


def parse_snapshot(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("extension startup snapshot exceeds 2 MiB")
    records = json.loads(raw)
    if not isinstance(records, list) or len(records) > 64:
        raise ValueError("extension startup snapshot must be an array of at most 64 records")
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("invalid extension startup record")
    return records
