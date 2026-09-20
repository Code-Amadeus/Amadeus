from __future__ import annotations

from datetime import datetime, timezone

from core.continuity import ContinuityService
from core.continuity.clock import RealityClock
from ._support import FakeClock


def _service(store, tmp_path) -> ContinuityService:
    fake = FakeClock(datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc), 0.0)
    return ContinuityService(
        store,
        clock=RealityClock(store, now_provider=fake.now, monotonic_provider=fake.monotonic),
        relationship_enabled=True,
        relationship_live_enabled=True,
        session_dir=tmp_path / "sessions",
    )


async def _turn(service: ContinuityService, session_id: str, turn_id: str, text: str) -> None:
    await service._on_chat_user(
        "chat.user", {"session_id": session_id, "turn_id": turn_id, "text": text}
    )
    await service._on_chat_complete(
        "chat.complete", {"session_id": session_id, "turn_id": turn_id, "full_text": "嗯。"}
    )
    await service.drain()


async def test_memories_do_not_cross_dialogues(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    service.start()
    await _turn(service, "session-a", "a1", "我的生日是1月2日")

    in_a = service.grounding_for_turn("我的生日是什么？", session_id="session-a", turn_id="a2")
    assert "1月2日" in in_a.text

    in_b = service.grounding_for_turn("我的生日是什么？", session_id="session-b", turn_id="b1")
    assert "1月2日" not in in_b.text
    assert in_b.memory_count == 0

    # A fresh dialogue starts from zero and only knows its own facts.
    await _turn(service, "session-b", "b2", "我的职业是研究员")
    in_b2 = service.grounding_for_turn("我的职业是什么？", session_id="session-b", turn_id="b3")
    assert "研究员" in in_b2.text
    in_a2 = service.grounding_for_turn("我的职业是什么？", session_id="session-a", turn_id="a3")
    assert "研究员" not in in_a2.text
    await service.aclose(graceful=False)


async def test_forget_and_mute_are_session_scoped(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    service.start()
    await _turn(service, "session-a", "a1", "我的生日是1月2日")
    await _turn(service, "session-b", "b1", "我的生日是3月4日")

    # Forget in A hard-deletes only A's row; B's fact survives untouched.
    await _turn(service, "session-a", "a2", "忘记我的生日")
    assert continuity_store.get_active_memory("user.fact.birth_date", scope="session-a") is None
    survivor = continuity_store.get_active_memory("user.fact.birth_date", scope="session-b")
    assert survivor is not None and survivor.object_text == "3月4日"

    # A topic mute in A never silences B.
    await _turn(service, "session-a", "a3", "不要再提京都那家甜点店了")
    assert [mute.topic for mute in continuity_store.list_topic_mutes(scope="session-a")] == [
        "京都那家甜点店"
    ]
    assert continuity_store.list_topic_mutes(scope="session-b") == []
    await service.aclose(graceful=False)


async def test_relationship_state_is_per_dialogue(continuity_store, tmp_path) -> None:
    service = _service(continuity_store, tmp_path)
    service.start()
    await _turn(service, "session-a", "a1", "我信任你，谢谢你")

    a_snapshot = continuity_store.get_relationship_snapshot(scope="session-a", now=1.0)
    b_snapshot = continuity_store.get_relationship_snapshot(scope="session-b", now=1.0)
    assert a_snapshot.relationship_value("trust") > 0.5
    assert b_snapshot.relationship_value("trust") == 0.5
    assert b_snapshot.relationship[0].event_count == 0

    grounding_a = service.grounding_for_turn("今天过得怎么样", session_id="session-a", turn_id="a2")
    grounding_b = service.grounding_for_turn("今天过得怎么样", session_id="session-b", turn_id="b1")
    assert "Relationship context:" in grounding_a.text
    assert "Relationship context:" not in grounding_b.text
    await service.aclose(graceful=False)
