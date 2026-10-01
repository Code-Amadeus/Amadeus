"""Canonical memory record types.

The JSONL snapshot log is the source of truth. SQLite and Markdown are
rebuildable projections of these records.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import re
import uuid
from typing import Any, Mapping


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPACTED = "compacted"
    REVOKED = "revoked"


_ALLOWED_STATUSES = {item.value for item in MemoryStatus}
_WHITESPACE = re.compile(r"\s+")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def normalize_iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def content_hash(*, scope: str, namespace: str, kind: str, text: str) -> str:
    canonical = "\x1f".join((scope, namespace, kind, text)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _unique_strings(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        raise TypeError("expected a string or iterable of strings")
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    text: str
    kind: str
    scope: str
    namespace: str
    tags: tuple[str, ...] = ()
    importance: int = 3
    confidence: float = 0.8
    source: str = "conversation"
    source_ids: tuple[str, ...] = ()
    content_hash: str = ""
    created_at: str = ""
    updated_at: str = ""
    valid_from: str = ""
    valid_to: str | None = None
    status: str = MemoryStatus.ACTIVE.value
    supersedes: tuple[str, ...] = ()
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", normalize_text(self.text))
        object.__setattr__(self, "kind", normalize_text(self.kind))
        object.__setattr__(self, "scope", normalize_text(self.scope))
        object.__setattr__(self, "namespace", normalize_text(self.namespace))
        object.__setattr__(self, "tags", _unique_strings(self.tags))
        object.__setattr__(self, "source_ids", _unique_strings(self.source_ids))
        object.__setattr__(self, "supersedes", _unique_strings(self.supersedes))
        if not self.id:
            raise ValueError("memory id is required")
        if not all((self.text, self.kind, self.scope, self.namespace)):
            raise ValueError("text, kind, scope and namespace are required")
        if self.status not in _ALLOWED_STATUSES:
            raise ValueError(f"unsupported memory status: {self.status}")
        if not 1 <= int(self.importance) <= 5:
            raise ValueError("importance must be between 1 and 5")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        now = utc_now_iso()
        created_at = normalize_iso(self.created_at or now)
        updated_at = normalize_iso(self.updated_at or created_at)
        valid_from = normalize_iso(self.valid_from or created_at)
        valid_to = normalize_iso(self.valid_to) if self.valid_to else None
        if valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to cannot be earlier than valid_from")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        expected_hash = content_hash(
            scope=self.scope, namespace=self.namespace, kind=self.kind, text=self.text
        )
        object.__setattr__(self, "content_hash", self.content_hash or expected_hash)
        object.__setattr__(self, "status", self.status)

    @classmethod
    def create(
        cls,
        *,
        text: str,
        kind: str,
        scope: str,
        namespace: str,
        tags: tuple[str, ...] | list[str] = (),
        importance: int = 3,
        confidence: float = 0.8,
        source: str = "conversation",
        source_ids: tuple[str, ...] | list[str] = (),
        created_at: str | None = None,
        valid_from: str | None = None,
        memory_id: str | None = None,
    ) -> "MemoryRecord":
        created = created_at or utc_now_iso()
        return cls(
            id=memory_id or f"m_{uuid.uuid4().hex}",
            text=text,
            kind=kind,
            scope=scope,
            namespace=namespace,
            tags=tuple(tags),
            importance=int(importance),
            confidence=float(confidence),
            source=source,
            source_ids=tuple(source_ids),
            created_at=created,
            updated_at=created,
            valid_from=valid_from or created,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MemoryRecord":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown memory fields: {', '.join(unknown)}")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "scope": self.scope,
            "namespace": self.namespace,
            "tags": list(self.tags),
            "importance": self.importance,
            "confidence": self.confidence,
            "source": self.source,
            "source_ids": list(self.source_ids),
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "status": self.status,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
        }

    def evolve(self, **changes: Any) -> "MemoryRecord":
        return replace(self, **changes)