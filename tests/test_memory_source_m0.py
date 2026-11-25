"""Synthetic contracts for durable message sources and their optional recall boundary."""
import asyncio
from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from config import settings
from core import character_rag as rag
from core import chat_history_projection as hp
from core import session_manager as sm
from server.cooperative_delivery import CooperativeHostDelivery
from server.cooperative_provider_loop import RoleDecisionUnavailable
from test_cooperative_character_rag import loop_for


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "_active_assistant_replies", {})
    history = sm.ConversationHistory()
    monkeypatch.setattr(sm, "conversation_history", history)
    sm.create_session("synthetic")
    return Path(sm._session_path("synthetic"))


def test_identity_replay_is_independent_of_body_and_turn_parts(session):
    for identity in ("host-first", "host-second", "host-second"):
        assert sm.append_session_message("synthetic", role="assistant", content="same text",
            turn_id="turn", message_id=identity)
    before = deepcopy(sm.conversation_history.dialog)
    assert len(before) == 2 and all(row["revision"] == 1 for row in before)
    assert not sm.append_session_message("synthetic", role="assistant", content="changed",
        turn_id="turn", message_id="host-first")
    assert sm.conversation_history.dialog == before
    assert sm.conversation_history.build_deepseek_messages("", "next") == [
        {"role":"assistant", "content":"same text"},
        {"role":"assistant", "content":"same text"}, {"role":"user", "content":"next"}]
    assert sm.read_session_sources("synthetic")[0]["message_id"] == "host-first"


def test_append_success_includes_durability_for_an_unsaved_matching_row(session, monkeypatch):
    sm.conversation_history.add_user("accepted", turn_id="turn", message_id="utterance")
    disk_before, memory_before = session.read_bytes(), deepcopy(sm.conversation_history.dialog)
    real_write = sm.write_text
    monkeypatch.setattr(sm, "write_text", Mock(side_effect=OSError("synthetic write failure")))
    assert not sm.append_session_message("synthetic", role="user", content="accepted",
        turn_id="turn", message_id="utterance")
    assert session.read_bytes() == disk_before and sm.conversation_history.dialog == memory_before
    monkeypatch.setattr(sm, "write_text", real_write)
    assert sm.append_session_message("synthetic", role="user", content="accepted",
        turn_id="turn", message_id="utterance")
    assert sm.read_session_sources("synthetic")[0]["message_id"] == "utterance"


def test_legacy_migration_commits_once_and_does_not_invent_original_time(session):
    retired = "cooperative-role:" + "a" * 64
    payload = {"session_id":"synthetic", "timestamp":123, "title":"Synthetic title", "dialog":[
        {"role":"user", "content":"literal [EMO preset=normal]"},
        {"role":"assistant", "content":"reply", "message_id":retired, "turn_id":"old-turn"}]}
    session.write_text(json.dumps(payload), encoding="utf-8")
    first = sm.read_session_sources("synthetic")
    assert all(row["created_at_ms"] is None and row["utc_offset_minutes"] is None for row in first)
    assert first[0]["turn_id"] is None and first[1]["message_id"] != retired
    persisted = json.loads(session.read_text(encoding="utf-8"))
    assert persisted["timestamp"] == 123 and persisted["title"] == "Synthetic title"
    assert sm.load_session("synthetic")[0]
    assert sm.read_session_sources("synthetic") == first
    assert not sm.append_session_message("synthetic", role="assistant", content="reply",
        turn_id="old-turn", message_id=retired)


def test_failed_migration_exposes_no_allocated_sources_or_partial_active_state(session, monkeypatch):
    session.write_text(json.dumps({"dialog":[{"role":"user", "content":"legacy"}]}), encoding="utf-8")
    disk_before, memory_before = session.read_bytes(), deepcopy(sm.conversation_history.dialog)
    monkeypatch.setattr(sm, "write_text", Mock(side_effect=OSError("synthetic failure")))
    with pytest.raises(OSError):
        sm.read_session_sources("synthetic")
    assert sm.load_session("synthetic") == (False, False)
    assert session.read_bytes() == disk_before and sm.conversation_history.dialog == memory_before


def test_source_revision_is_exact_and_interruption_is_idempotent(session):
    assert sm.append_session_message("synthetic", role="assistant", content="heard unheard",
        turn_id="turn", message_id="reply")
    original = sm.read_session_source("synthetic", "reply", revision=1)
    assert sm.persist_interrupted_assistant_turn("synthetic", turn_id="turn", heard_content="heard")
    revised = sm.read_session_source("synthetic", "reply", revision=2)
    assert revised["created_at_ms"] == original["created_at_ms"]
    assert revised["utc_offset_minutes"] == original["utc_offset_minutes"]
    assert not sm.persist_interrupted_assistant_turn("synthetic", turn_id="turn", heard_content="heard")
    with pytest.raises(ValueError):
        sm.read_session_source("synthetic", "reply", revision=1)
    assert not sm.append_session_message("synthetic", role="assistant", content="heard unheard",
        turn_id="turn", message_id="reply")
    assert sm.read_session_source("synthetic", "reply", revision=2) == revised


def test_canonical_projection_preserves_unicode_and_user_literals(session):
    literal = '<think>user words</think>[EMO preset="normal"]e\u0301😀 [a, b]'
    sm.conversation_history.add_user(literal, turn_id="turn", message_id="user")
    raw = '<think>hidden[AUIP action="launch"]</think>A[EMO preset="smile"]e\u0301😀\n[1, 2][CONTROL delegate="false"] Z'
    sm.conversation_history.add_assistant(raw, turn_id="turn", message_id="assistant")
    assert sm.save_session("synthetic")
    user, assistant = sm.read_session_sources("synthetic")
    assert user["text"] == literal
    assert assistant["projection_version"] == hp.MEMORY_TEXT_PROJECTION_VERSION
    assert assistant["text"] == 'Ae\u0301😀\n[1, 2] Z'
    assert assistant["text"][1:4] == 'e\u0301😀'
    assert sm.conversation_history.dialog[-1]["content"] == raw


async def test_same_publication_replay_keeps_identity_but_identical_new_publication_is_distinct(session):
    delivery = CooperativeHostDelivery(session_id="synthetic", display=lambda event:True,
        record_display=sm.append_session_message)
    first = {"cause":"turn", "text":"identical"}
    second = {"cause":"turn", "text":"identical"}
    assert await delivery(first) and await delivery(first) and await delivery(second)
    sources = sm.read_session_sources("synthetic")
    assert len(sources) == 2 and sources[0]["message_id"] == first["message_id"]
    assert sources[0]["created_at_ms"] == first["created_at_ms"]
    assert first["message_id"] != second["message_id"]


def test_imported_branch_text_does_not_invent_original_message_time():
    from server.provider_branch import apply_branch_merge_to_history
    history = sm.ConversationHistory()
    apply_branch_merge_to_history({"visible_messages":[
        {"role":"user", "content":"old input", "created_at":123},
        {"role":"assistant", "content":"old answer", "created_at":124}],
        "provider":"synthetic", "branch_id":"branch", "final_report":"New report"}, history)
    assert all(row["created_at_ms"] is None and row["utc_offset_minutes"] is None for row in history.dialog[:2])
    assert history.dialog[-1]["created_at_ms"] is not None


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_optional_reference_failure_keeps_real_role_query(failure, caplog):
    async def reference(_text):
        if failure == "exception":
            raise RuntimeError("PRIVATE_EXCEPTION_BODY")
        await asyncio.Event().wait()
    query = AsyncMock(return_value='{"action":null,"say":"ok"}')
    loop = loop_for(query, reference)
    loop.role_reference_timeout_s = .01
    try:
        with caplog.at_level(logging.INFO):
            result = await asyncio.wait_for(loop.submit("PRIVATE_USER_BODY", input_id="input"), 1)
        assert result["state"] == "no_action"
        query.assert_awaited_once()
        assert "PRIVATE_EXCEPTION_BODY" not in caplog.text and "PRIVATE_USER_BODY" not in caplog.text
    finally:
        await loop.close()


async def test_real_query_failure_still_fails_and_reference_cancellation_stops_query():
    query = AsyncMock(side_effect=RuntimeError("synthetic model failure"))
    loop = loop_for(query, AsyncMock(return_value=""))
    try:
        with pytest.raises(RoleDecisionUnavailable):
            await loop.submit("synthetic", input_id="input")
    finally:
        await loop.close()
    query = AsyncMock()
    loop = loop_for(query, AsyncMock(side_effect=asyncio.CancelledError))
    try:
        with pytest.raises(asyncio.CancelledError):
            await loop.submit("synthetic", input_id="input")
        query.assert_not_called()
    finally:
        await loop.close()


async def test_reference_deadlines_do_not_queue_threads_or_reuse_late_text(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLED", True)
    service = rag.CharacterRAG()
    started, release = threading.Event(), threading.Event()
    calls = []
    def reference(query):
        calls.append(query)
        started.set()
        assert release.wait(3)
        return query + "_REFERENCE"
    monkeypatch.setattr(service, "reference", reference)
    first = asyncio.create_task(service.reference_async("STALE"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for index in range(10):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(service.reference_async(str(index)), .001)
        assert calls == ["STALE"]
        release.set()
        result = await asyncio.wait_for(service.reference_async("CURRENT"), 1)
        assert result == "CURRENT_REFERENCE" and calls == ["STALE", "CURRENT"]
    finally:
        release.set()
        if service._reference_task is not None:
            await service._reference_task


async def test_decision_logs_protect_history_current_reference_and_raw_output(caplog, monkeypatch):
    monkeypatch.setattr(settings, "LOG_USER_CONTENT", False)
    loop = loop_for(AsyncMock(return_value='{"action":null,"say":"PRIVATE_RAW"}'),
        AsyncMock(return_value="PRIVATE_REFERENCE"))
    loop.history.append({"source":"user", "text":"PRIVATE_HISTORY"})
    try:
        with caplog.at_level(logging.INFO):
            await loop.submit("PRIVATE_CURRENT", input_id="input")
        assert "COOPERATIVE-DECISION" in caplog.text
        assert all(value not in caplog.text for value in (
            "PRIVATE_RAW", "PRIVATE_REFERENCE", "PRIVATE_HISTORY", "PRIVATE_CURRENT"))
        assert any(row["kind"] == "decision" and "PRIVATE_RAW" in row["raw"] for row in loop.trace)
    finally:
        await loop.close()


async def test_source_metadata_does_not_change_standalone_role_model_history():
    frames, publications = [], []
    async def query(messages):
        frames.append(json.loads(messages[1]["content"]))
        return '{"action":null,"say":"Same visible reply."}'
    loop = loop_for(query, None)
    loop.publish = lambda event: publications.append(event) or True
    try:
        await loop.submit("first", input_id="one", turn_id="first-turn")
        await loop.submit("second", input_id="two", turn_id="second-turn")
        assert publications[0]["message_id"] and publications[0]["created_at_ms"] is not None
        role_row = next(row for row in frames[1]["history"] if row["source"] == "kurisu")
        assert role_row == {"source":"kurisu", "text":"Same visible reply.", "cause":"first-turn"}
    finally:
        await loop.close()
