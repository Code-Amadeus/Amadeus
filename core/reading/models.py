"""Reading session and spoiler-boundary models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ReadingChunk:
    id: str
    chapter: str
    start_offset: int
    end_offset: int
    text: str
    page: int | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("chunk id is required")
        if self.start_offset < 0 or self.end_offset < self.start_offset:
            raise ValueError("invalid chunk offsets")
        if not self.chapter:
            raise ValueError("chunk chapter is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chapter": self.chapter,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text": self.text,
            "page": self.page,
        }

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ReadingChunk":
        return cls(
            id=str(value.get("id") or ""),
            chapter=str(value.get("chapter") or ""),
            start_offset=int(value.get("start_offset") or 0),
            end_offset=int(value.get("end_offset") or 0),
            text=str(value.get("text") or ""),
            page=None if value.get("page") is None else int(value["page"]),
        )


@dataclass(slots=True)
class ReadingContext:
    book_id: str
    kind: str = "novel"
    current_chapter: str = ""
    current_page: int | None = None
    cursor: int = 0
    spoiler_cursor: int = 0
    selected_start: int = 0
    selected_end: int = 0
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.book_id:
            raise ValueError("book_id is required")
        self.kind = str(self.kind or "novel")
        self.cursor = max(0, int(self.cursor))
        self.spoiler_cursor = max(0, int(self.spoiler_cursor or self.cursor))
        self.selected_start = max(0, int(self.selected_start))
        self.selected_end = max(self.selected_start, int(self.selected_end))
        self.updated_at = self.updated_at or utc_now_iso()

    @property
    def namespace(self) -> str:
        return f"reading:{self.book_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "kind": self.kind,
            "current_chapter": self.current_chapter,
            "current_page": self.current_page,
            "cursor": self.cursor,
            "spoiler_cursor": self.spoiler_cursor,
            "selected_start": self.selected_start,
            "selected_end": self.selected_end,
            "recent_turns": list(self.recent_turns),
            "updated_at": self.updated_at,
        }