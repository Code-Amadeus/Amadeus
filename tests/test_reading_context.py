from __future__ import annotations

from pathlib import Path

import pytest

from core.reading import ReadingChunk, ReadingContext, ReadingSessionStore, SpoilerGuard


def test_session_round_trip_and_recent_turns(tmp_path: Path) -> None:
    store = ReadingSessionStore.open(tmp_path)
    context = ReadingContext(
        book_id="book-1",
        kind="epub",
        current_chapter="第三章",
        current_page=42,
        cursor=1200,
        spoiler_cursor=1200,
        selected_start=200,
        selected_end=1200,
    )
    store.save_context(context)
    store.append_turn(
        "book-1",
        user_message="总结这两章",
        assistant_message="这是总结",
        selected_excerpt="第三章内容",
        referenced_chunks=["c1", "c2"],
    )

    restored = store.get_context("book-1")
    assert restored is not None
    assert restored.namespace == "reading:book-1"
    assert restored.current_page == 42
    assert restored.recent_turns[0]["user_message"] == "总结这两章"
    assert restored.recent_turns[0]["referenced_chunks"] == ["c1", "c2"]


def test_update_from_event_allows_cursor_to_move_back(tmp_path: Path) -> None:
    store = ReadingSessionStore.open(tmp_path)
    store.update_from_event(
        {
            "book_id": "book-1",
            "chapter": "第四章",
            "cursor": 5000,
            "page": 80,
        }
    )
    context = store.update_from_event(
        {
            "book_id": "book-1",
            "chapter": "第三章",
            "cursor": 1200,
            "page": 42,
        }
    )

    assert context.cursor == 1200
    assert context.spoiler_cursor == 5000
    assert context.current_chapter == "第三章"


def test_spoiler_guard_allows_only_read_chunks() -> None:
    context = ReadingContext(book_id="book-1", cursor=100, spoiler_cursor=100)
    chunks = [
        ReadingChunk("c1", "第一章", 0, 100, "已经读到的内容"),
        ReadingChunk("c2", "第二章", 100, 200, "还没读到的关键秘密"),
    ]
    guard = SpoilerGuard()

    allowed = guard.prepare_chunks(context, chunks)
    assert [chunk.id for chunk in allowed] == ["c1"]
    assert [chunk.id for chunk in guard.forbidden_chunks(context, chunks)] == ["c2"]


def test_spoiler_guard_detects_verbatim_future_text() -> None:
    context = ReadingContext(book_id="book-1", cursor=100, spoiler_cursor=100)
    future = ReadingChunk("c2", "第二章", 100, 200, "凶手其实是那个戴帽子的男人")
    guard = SpoilerGuard(minimum_overlap=6)

    decision = guard.check_response("根据后面的内容，凶手其实是那个戴帽子的男人。", context, [future])

    assert decision.allowed is False
    assert decision.offending_chunk_ids == ("c2",)


def test_spoiler_guard_accepts_safe_response() -> None:
    context = ReadingContext(book_id="book-1", cursor=100, spoiler_cursor=100)
    future = ReadingChunk("c2", "第二章", 100, 200, "凶手其实是那个戴帽子的男人")
    guard = SpoilerGuard(minimum_overlap=6)

    decision = guard.check_response("目前还不能确定凶手是谁。", context, [future])

    assert decision.allowed is True


def test_prepare_chunks_fails_without_readable_content() -> None:
    context = ReadingContext(book_id="book-1", cursor=0, spoiler_cursor=0)
    guard = SpoilerGuard()

    with pytest.raises(PermissionError):
        guard.prepare_chunks(context, [ReadingChunk("c1", "第一章", 1, 10, "内容")])

def test_update_from_event_persists_selected_text_chunk(tmp_path: Path) -> None:
    store = ReadingSessionStore.open(tmp_path)
    store.update_from_event(
        {
            "book_id": "book-1",
            "chapter": "第三章",
            "cursor": 1200,
            "selected_start": 200,
            "selected_end": 1200,
            "text": "这是用户选中的一两章内容。",
        }
    )

    chunks = store.list_chunks("book-1")
    assert len(chunks) == 1
    assert chunks[0].text == "这是用户选中的一两章内容。"
    assert chunks[0].end_offset == 1200
