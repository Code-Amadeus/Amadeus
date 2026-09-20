"""Deterministic Host policy for applying extracted memory candidates.

The extractor may suggest structured facts, but it never decides whether a
fact becomes durable, reinforces an existing record, supersedes it, or is
blocked by an explicit-forget tombstone.  Those decisions live here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from core.continuity.models import MemoryCandidate, MemoryRecord, MemoryTombstone


class MemoryResolutionAction(StrEnum):
    CREATE = "create"
    REINFORCE = "reinforce"
    SUPERSEDE = "supersede"
    SUPPRESS = "suppress"
    CLEAR_TOMBSTONE_AND_CREATE = "clear_tombstone_and_create"


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    action: MemoryResolutionAction
    reason: str


def normalize_memory_text(value: str) -> str:
    """Stable comparison form; never used as the user-visible summary."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip("。.!！?？")
    return text.strip()


def candidate_identity(candidate: MemoryCandidate) -> str:
    if str(candidate.object_text or "").strip():
        return normalize_memory_text(candidate.object_text)
    return normalize_memory_text(candidate.summary)


def record_identity(record: MemoryRecord) -> str:
    if str(record.object_text or "").strip():
        return normalize_memory_text(record.object_text)
    return normalize_memory_text(record.summary)


class MemoryResolver:
    """Pure resolution policy used inside the Store's SQLite transaction."""

    def resolve(
        self,
        *,
        candidate: MemoryCandidate,
        active: MemoryRecord | None,
        tombstone: MemoryTombstone | None,
        observed_at: float,
    ) -> MemoryResolution:
        if tombstone is not None:
            if candidate.explicit_keep and observed_at >= tombstone.created_at:
                return MemoryResolution(
                    MemoryResolutionAction.CLEAR_TOMBSTONE_AND_CREATE,
                    "newer explicit user re-introduction overrides tombstone",
                )
            return MemoryResolution(
                MemoryResolutionAction.SUPPRESS,
                "active explicit-forget tombstone or stale remember evidence",
            )

        if active is None:
            return MemoryResolution(MemoryResolutionAction.CREATE, "no active memory")

        if candidate_identity(candidate) == record_identity(active):
            return MemoryResolution(
                MemoryResolutionAction.REINFORCE,
                "same logical value for active memory key",
            )

        active_since = active.valid_from if active.valid_from is not None else active.created_at
        if observed_at < float(active_since):
            return MemoryResolution(
                MemoryResolutionAction.SUPPRESS,
                "stale conflicting evidence predates the active value",
            )

        return MemoryResolution(
            MemoryResolutionAction.SUPERSEDE,
            "same memory key now carries a different logical value",
        )
