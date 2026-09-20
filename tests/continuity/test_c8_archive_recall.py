from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from core.continuity import (
    ContinuityService,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    TurnEvidence,
)
from core.continuity.clock import RealityClock
from core import session_manager as sm
from server.handlers.continuity_handler import ContinuityHandler
from server.protocol import Method
from ._support import FakeClock


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _write_memory(
    store,
    *,
    session_id: str,
    turn_id: str,
    summary: str,
    key: str,
    created_at: str,
    kind: MemoryKind = MemoryKind.EPISODIC,
):
    observed = _ts(created_at)
    records = store.apply_memory_candidates(
        TurnEvidence(
            session_id=session_id,
            turn_id=turn_id,
            user_text=summary,
            assistant_text="ack",
            user_created_at=created_at,
            assistant_created_at=created_at,
            observed_at=observed,
        ),
        (
            MemoryCandidate(
                memory_key=key,
                kind=kind,
                summary=summary,
                importance=0.55,
                confidence=1.0,
            ),
        ),
        resolver=MemoryResolver(),
        complete_turn=False,
    )
    assert len(records) == 1
    return records[0]


def _write_session(
    session_dir: Path,
    *,
    session_id: str,
    turn_id: str,
    created_at: str,
    user_text: str,
    assistant_text: str = "",
) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    dialog = [
        {
            "role": "user",
            "content": user_text,
            "turn_id": turn_id,
            "created_at": created_at,
        }
    ]
    if assistant_text:
        dialog.append(
            {
                "role": "assistant",
                "content": assistant_text,
                "turn_id": turn_id,
                "created_at": created_at,
            }
        )
    (session_dir / f"{session_id}.json").write_text(
        json.dumps({"session_id": session_id, "dialog": dialog}, ensure_ascii=False),
        encoding="utf-8",
    )


def _service(store, session_dir: Path, now: datetime) -> ContinuityService:
    fake = FakeClock(now)
    return ContinuityService(
        store,
        clock=RealityClock(store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        memory_enabled=True,
        relationship_enabled=False,
        life_enabled=False,
        archive_recall_enabled=True,
        session_dir=session_dir,
    )


def test_low_confidence_historical_question_uses_high_fidelity_session_fallback(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    created = "2025-05-12T18:30:00+00:00"
    _write_memory(
        continuity_store,
        session_id="kyoto-2025",
        turn_id="turn-dessert",
        summary="去年在京都聊过一家甜点店",
        key="user.episode.kyoto-dessert",
        created_at=created,
    )
    _write_session(
        session_dir,
        session_id="kyoto-2025",
        turn_id="turn-dessert",
        created_at=created,
        user_text="去年在京都那家店我点的是焙茶巴菲，还坐在靠窗第二桌。",
        assistant_text="我记得你当时很喜欢那份焙茶巴菲。",
    )
    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )

    grounding = service.grounding_for_turn(
        "你还记得去年在京都那家店我点的具体是什么吗？",
        session_id="current",
        turn_id="current-turn",
    )

    assert grounding.archive_used is True
    assert grounding.archive_hit_count >= 1
    assert "焙茶巴菲" in grounding.text
    assert "Historical Session evidence" in grounding.text
    trace = service.c8_retrieval_traces(limit=1)[0]
    assert trace["archive_attempted"] is True
    assert trace["archive_session_hit_count"] >= 1
    assert trace["temporal_filter"] == "year"
    assert "焙茶巴菲" not in repr(trace)


def test_ordinary_or_confident_fast_recall_never_scans_session_archive(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    _write_memory(
        continuity_store,
        session_id="facts",
        turn_id="birthday",
        summary="我的生日是1月2日",
        key="user.fact.birth_date",
        created_at="2025-01-02T10:00:00+00:00",
        kind=MemoryKind.USER_FACT,
    )
    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )
    search = Mock(wraps=service.archive_searcher.search)
    service.archive_searcher.search = search

    direct = service.grounding_for_turn("我的生日是什么？", turn_id="now-1")
    assert direct.memory_count == 1
    assert search.call_count == 0

    historical_but_strong = service.grounding_for_turn("你还记得我的生日吗？", turn_id="now-2")
    assert historical_but_strong.memory_count == 1
    assert search.call_count == 0
    assert service.c8_retrieval_traces(limit=1)[0]["archive_gate"] == "fast_confident"


def test_archive_tier_memory_is_only_used_after_historical_low_confidence_gate(continuity_store, tmp_path) -> None:
    record = _write_memory(
        continuity_store,
        session_id="archive-session",
        turn_id="archive-turn",
        summary="很久以前我们约定代码审查暗号是蓝鲸",
        key="user.episode.review-codeword",
        created_at="2024-03-01T12:00:00+00:00",
    )
    continuity_store._connection.execute(
        "UPDATE memory_items SET retention_tier = 'archive' WHERE id = ?",
        (record.id,),
    )
    service = _service(
        continuity_store,
        tmp_path / "missing-sessions",
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )

    ordinary = service.grounding_for_turn("代码审查暗号是什么？", turn_id="ordinary")
    assert ordinary.memory_count == 0

    historical = service.grounding_for_turn("你还记得很久以前代码审查暗号是什么吗？", turn_id="historical")
    assert historical.archive_used is True
    assert "蓝鲸" in historical.text
    trace = service.c8_retrieval_traces(limit=1)[0]
    assert trace["archive_memory_hit_count"] >= 1
    assert trace["archive_result"] == "session_dir_missing"


def test_explicit_forget_records_source_guard_and_session_fallback_cannot_resurrect(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    created = "2025-02-10T08:00:00+00:00"
    record = _write_memory(
        continuity_store,
        session_id="secret-session",
        turn_id="secret-turn",
        summary="以前聊过一个私人纪念日",
        key="user.fact.private-anniversary",
        created_at=created,
        kind=MemoryKind.USER_FACT,
    )
    _write_session(
        session_dir,
        session_id="secret-session",
        turn_id="secret-turn",
        created_at=created,
        user_text="那个私人纪念日是2月10日，请记住。",
    )

    deleted = continuity_store.forget_memory(
        record.memory_key,
        session_id="current",
        turn_id="forget-turn",
        observed_at=_ts("2026-09-18T09:00:00+00:00"),
    )
    assert deleted == 1
    assert continuity_store.archive_forget_guard_ready() is True
    assert continuity_store.is_archive_source_blocked("secret-session", "secret-turn") is True
    # Clearing the key tombstone later may authorize a new fact with the same
    # key, but it must not re-authorize the old forgotten source transcript.
    continuity_store._connection.execute(
        "UPDATE memory_tombstones SET cleared_at = ? WHERE memory_key = ?",
        (_ts("2026-09-18T09:01:00+00:00"), record.memory_key),
    )
    assert continuity_store.is_archive_source_blocked("secret-session", "secret-turn") is True

    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 5, tzinfo=timezone.utc),
    )
    grounding = service.grounding_for_turn(
        "你还记得以前那个私人纪念日具体是哪天吗？",
        turn_id="ask-after-forget",
    )
    assert "2月10日" not in grounding.text
    assert grounding.archive_hit_count == 0


def test_legacy_unmapped_tombstone_fails_closed_for_session_archive(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    created = "2025-04-01T08:00:00+00:00"
    _write_memory(
        continuity_store,
        session_id="safe-session",
        turn_id="safe-turn",
        summary="去年聊过旧相机",
        key="user.episode.camera",
        created_at=created,
    )
    _write_session(
        session_dir,
        session_id="safe-session",
        turn_id="safe-turn",
        created_at=created,
        user_text="去年那台旧相机是银色FM2。",
    )
    continuity_store._connection.execute(
        """
        INSERT INTO memory_tombstones(
            id, scope, memory_key, kind, created_at,
            source_session_id, source_turn_id, reason, cleared_at, archive_guard_complete
        ) VALUES ('legacy-tombstone', 'global', 'legacy.unknown', NULL, 1.0, '', '', 'explicit_forget', NULL, 0)
        """
    )
    # Repeating forget cannot pretend a pre-C8 tombstone has complete
    # source closure when its original source turn is unknowable.
    continuity_store.forget_memory("legacy.unknown", observed_at=2.0)
    assert continuity_store.archive_forget_guard_ready() is False
    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )
    grounding = service.grounding_for_turn("你还记得去年那台旧相机具体是什么吗？", turn_id="now")
    assert "银色FM2" not in grounding.text
    trace = service.c8_retrieval_traces(limit=1)[0]
    assert trace["archive_attempted"] is True
    assert trace["archive_guard_ready"] is False
    assert trace["archive_result"] == "legacy_tombstone_guard"


def test_long_session_transcript_keeps_archived_turns_recallable(continuity_store, tmp_path) -> None:
    """A long running Session must not trim away the turn its memory anchors."""

    session_dir = tmp_path / "sessions"
    old_dir = sm._SESSION_DIR
    old_session_id = sm._CURRENT_SESSION_ID
    old_dialog = list(sm.conversation_history.dialog)
    old_rounds = sm.conversation_history.max_rounds
    session_id = "long-live-session"
    try:
        sm._SESSION_DIR = str(session_dir)
        sm._CURRENT_SESSION_ID = session_id
        sm.conversation_history.reset()
        sm.conversation_history.max_rounds = 1
        sm.conversation_history.add_user(
            "去年在京都那家店我点的是焙茶巴菲，还坐在靠窗第二桌。",
            turn_id="turn-dessert",
            created_at="2025-05-12T18:30:00+00:00",
        )
        sm.conversation_history.add_assistant(
            "我记得你当时很喜欢那份焙茶巴菲。",
            turn_id="turn-dessert",
            created_at="2025-05-12T18:30:00+00:00",
        )
        for index in range(4):
            sm.conversation_history.add_user(f"后来的闲聊 {index}", turn_id=f"turn-{index}")
            sm.conversation_history.add_assistant(f"回应 {index}", turn_id=f"turn-{index}")
        sm.save_session(session_id, enable_conversation=True)
    finally:
        sm._SESSION_DIR = old_dir
        sm._CURRENT_SESSION_ID = old_session_id
        sm.conversation_history.reset()
        sm.conversation_history.dialog[:] = old_dialog
        sm.conversation_history.max_rounds = old_rounds

    persisted = json.loads(
        (session_dir / f"{session_id}.json").read_text(encoding="utf-8")
    )
    persisted_turns = {str(message.get("turn_id") or "") for message in persisted["dialog"]}
    assert "turn-dessert" in persisted_turns

    _write_memory(
        continuity_store,
        session_id=session_id,
        turn_id="turn-dessert",
        summary="去年在京都聊过一家甜点店",
        key="user.episode.kyoto-dessert",
        created_at="2025-05-12T18:30:00+00:00",
    )
    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )
    grounding = service.grounding_for_turn(
        "你还记得去年在京都那家店我点的具体是什么吗？",
        session_id="current",
        turn_id="current-turn",
    )
    assert grounding.archive_used is True
    assert "焙茶巴菲" in grounding.text


def test_time_aware_archive_search_prefers_requested_year(continuity_store, tmp_path) -> None:
    session_dir = tmp_path / "sessions"
    old = "2025-06-01T12:00:00+00:00"
    recent = "2026-06-01T12:00:00+00:00"
    _write_memory(
        continuity_store,
        session_id="dessert-2025",
        turn_id="dessert-old",
        summary="京都甜点讨论",
        key="user.episode.dessert.2025",
        created_at=old,
    )
    _write_memory(
        continuity_store,
        session_id="dessert-2026",
        turn_id="dessert-new",
        summary="京都甜点讨论",
        key="user.episode.dessert.2026",
        created_at=recent,
    )
    _write_session(
        session_dir,
        session_id="dessert-2025",
        turn_id="dessert-old",
        created_at=old,
        user_text="京都甜点那次我点了抹茶羊羹。",
    )
    _write_session(
        session_dir,
        session_id="dessert-2026",
        turn_id="dessert-new",
        created_at=recent,
        user_text="京都甜点这次我点了栗子蒙布朗。",
    )
    service = _service(
        continuity_store,
        session_dir,
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )
    grounding = service.grounding_for_turn("你还记得去年京都甜点具体点了什么吗？", turn_id="now")
    assert "抹茶羊羹" in grounding.text
    assert "栗子蒙布朗" not in grounding.text


async def _call_trace_handler(service: ContinuityService):
    handler = ContinuityHandler(service)
    return await handler.handle(Method.CONTINUITY_RETRIEVAL_TRACE, {"limit": 5})


def test_retrieval_trace_handler_exposes_content_free_diagnostics(continuity_store, tmp_path) -> None:
    import asyncio

    service = _service(
        continuity_store,
        tmp_path / "sessions",
        datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc),
    )
    service.grounding_for_turn("普通问题不应扫描历史 Session", turn_id="now")
    result = asyncio.run(_call_trace_handler(service))
    assert result and result["ok"] is True
    assert len(result["traces"]) == 1
    trace = result["traces"][0]
    assert len(trace["query_fingerprint"]) == 16
    assert "普通问题" not in repr(trace)
    assert trace["archive_gate"] == "no_historical_cue"
