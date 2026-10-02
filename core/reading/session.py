"""Persistent reading session and turn store."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator, Mapping, Sequence

from .models import ReadingChunk, ReadingContext, utc_now_iso


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_sessions (
    book_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    current_chapter TEXT NOT NULL,
    current_page INTEGER,
    cursor INTEGER NOT NULL,
    spoiler_cursor INTEGER NOT NULL,
    selected_start INTEGER NOT NULL,
    selected_end INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reading_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT NOT NULL,
    selected_excerpt TEXT NOT NULL,
    referenced_chunks TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reading_turns_book_idx
    ON reading_turns(book_id, id DESC);
CREATE TABLE IF NOT EXISTS reading_chunks (
    book_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    chapter TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page INTEGER,
    text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS reading_chunks_book_idx
    ON reading_chunks(book_id, end_offset, start_offset);
"""


class ReadingSessionStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "reading.sqlite3"
        self._lock = threading.RLock()

    @classmethod
    def open(cls, root: str | Path) -> "ReadingSessionStore":
        store = cls(root)
        store.initialize()
        return store

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    def save_context(self, context: ReadingContext) -> ReadingContext:
        context.updated_at = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reading_sessions (
                    book_id, kind, current_chapter, current_page, cursor,
                    spoiler_cursor, selected_start, selected_end, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id) DO UPDATE SET
                    kind=excluded.kind,
                    current_chapter=excluded.current_chapter,
                    current_page=excluded.current_page,
                    cursor=excluded.cursor,
                    spoiler_cursor=excluded.spoiler_cursor,
                    selected_start=excluded.selected_start,
                    selected_end=excluded.selected_end,
                    updated_at=excluded.updated_at
                """,
                (
                    context.book_id,
                    context.kind,
                    context.current_chapter,
                    context.current_page,
                    context.cursor,
                    context.spoiler_cursor,
                    context.selected_start,
                    context.selected_end,
                    context.updated_at,
                ),
            )
            connection.commit()
        return context

    def get_context(self, book_id: str) -> ReadingContext | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reading_sessions WHERE book_id = ?", (book_id,)
            ).fetchone()
        if row is None:
            return None
        context = ReadingContext(
            book_id=row["book_id"],
            kind=row["kind"],
            current_chapter=row["current_chapter"],
            current_page=row["current_page"],
            cursor=int(row["cursor"]),
            spoiler_cursor=int(row["spoiler_cursor"]),
            selected_start=int(row["selected_start"]),
            selected_end=int(row["selected_end"]),
            updated_at=row["updated_at"],
        )
        context.recent_turns = self.recent_turns(book_id, limit=10)
        return context

    def append_turn(
        self,
        book_id: str,
        *,
        user_message: str,
        assistant_message: str,
        selected_excerpt: str = "",
        referenced_chunks: Sequence[str] = (),
        created_at: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reading_turns (
                    book_id, user_message, assistant_message, selected_excerpt,
                    referenced_chunks, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(book_id),
                    str(user_message),
                    str(assistant_message),
                    str(selected_excerpt),
                    json.dumps(list(referenced_chunks), ensure_ascii=False),
                    created_at or utc_now_iso(),
                ),
            )
            connection.commit()

    def recent_turns(self, book_id: str, *, limit: int = 10) -> list[dict[str, object]]:
        clean_limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_message, assistant_message, selected_excerpt,
                       referenced_chunks, created_at
                FROM reading_turns WHERE book_id = ? ORDER BY id DESC LIMIT ?
                """,
                (book_id, clean_limit),
            ).fetchall()
        return [
            {
                "user_message": row["user_message"],
                "assistant_message": row["assistant_message"],
                "selected_excerpt": row["selected_excerpt"],
                "referenced_chunks": json.loads(row["referenced_chunks"]),
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def save_chunks(self, book_id: str, chunks: Sequence[ReadingChunk | Mapping[str, object]]) -> None:
        normalized = [
            chunk if isinstance(chunk, ReadingChunk) else ReadingChunk.from_mapping(dict(chunk))
            for chunk in chunks
        ]
        if not normalized:
            return
        timestamp = utc_now_iso()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO reading_chunks (
                    book_id, chunk_id, chapter, start_offset, end_offset,
                    page, text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, chunk_id) DO UPDATE SET
                    chapter=excluded.chapter,
                    start_offset=excluded.start_offset,
                    end_offset=excluded.end_offset,
                    page=excluded.page,
                    text=excluded.text,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        str(book_id), chunk.id, chunk.chapter, chunk.start_offset,
                        chunk.end_offset, chunk.page, chunk.text, timestamp,
                    )
                    for chunk in normalized
                ],
            )
            connection.commit()

    def list_chunks(self, book_id: str) -> list[ReadingChunk]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk_id, chapter, start_offset, end_offset, page, text
                FROM reading_chunks WHERE book_id = ?
                ORDER BY end_offset ASC, start_offset ASC, chunk_id ASC
                """,
                (book_id,),
            ).fetchall()
        return [
            ReadingChunk(
                id=row["chunk_id"],
                chapter=row["chapter"],
                start_offset=int(row["start_offset"]),
                end_offset=int(row["end_offset"]),
                page=row["page"],
                text=row["text"],
            )
            for row in rows
        ]

    def latest_context(self) -> ReadingContext | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT book_id FROM reading_sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return self.get_context(str(row["book_id"])) if row else None

    def update_from_event(self, event: Mapping[str, object]) -> ReadingContext:
        book_id = str(event.get("book_id") or "").strip()
        if not book_id:
            raise ValueError("reading event requires book_id")
        existing = self.get_context(book_id)
        cursor = int(event.get("cursor") or (existing.cursor if existing else 0))
        context = existing or ReadingContext(book_id=book_id)
        context.kind = str(event.get("kind") or context.kind)
        context.current_chapter = str(event.get("chapter") or context.current_chapter)
        page_value = event.get("page")
        context.current_page = None if page_value is None else int(page_value)
        context.cursor = cursor
        context.spoiler_cursor = max(context.spoiler_cursor, int(event.get("spoiler_cursor") or cursor))
        context.selected_start = max(0, int(event.get("selected_start") or context.selected_start))
        context.selected_end = max(context.selected_start, int(event.get("selected_end") or context.selected_end))
        saved = self.save_context(context)
        raw_chunks = event.get("chunks")
        if isinstance(raw_chunks, list) and raw_chunks:
            self.save_chunks(book_id, raw_chunks)
        else:
            selected_text = str(event.get("text") or "").strip()
            if selected_text:
                chunk_id = "selection-" + hashlib.sha256(
                    f"{book_id}\x1f{context.selected_start}\x1f{context.selected_end}\x1f{selected_text}".encode("utf-8")
                ).hexdigest()[:16]
                self.save_chunks(
                    book_id,
                    [ReadingChunk(
                        id=chunk_id,
                        chapter=context.current_chapter or "selection",
                        start_offset=context.selected_start,
                        end_offset=max(context.selected_end, context.spoiler_cursor),
                        page=context.current_page,
                        text=selected_text,
                    )],
                )
        return saved