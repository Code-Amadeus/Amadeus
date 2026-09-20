from __future__ import annotations

import pytest

from core.continuity import ContinuityService, MemoryCandidate, MemoryKind, MemoryResolver, TurnEvidence
from core.continuity.store import ContinuityStore
from server.handlers.continuity_handler import ContinuityHandler
from server.protocol import Method


@pytest.mark.asyncio
async def test_c7_handler_memory_controls_and_clear_feedback(tmp_path) -> None:
    store = ContinuityStore(tmp_path / "continuity.sqlite3")
    try:
        records = store.apply_memory_candidates(
            TurnEvidence(session_id="s", turn_id="t", user_text="remember tea", observed_at=1000.0),
            (MemoryCandidate(memory_key="user.preference.tea", kind=MemoryKind.PREFERENCE, summary="Likes tea"),),
            resolver=MemoryResolver(),
            complete_turn=False,
        )
        service = ContinuityService(store, memory_enabled=False, relationship_enabled=False, life_enabled=False)
        handler = ContinuityHandler(service)
        listing = await handler.handle(Method.CONTINUITY_MEMORY_LIST, {"limit": 20})
        assert listing and listing["ok"] is True
        assert listing["memories"][0]["id"] == records[0].id

        pinned = await handler.handle(
            Method.CONTINUITY_MEMORY_PIN,
            {"memory_id": records[0].id, "pinned": True},
        )
        assert pinned and pinned["ok"] is True and pinned["memory"]["pinned"] is True

        forgotten = await handler.handle(
            Method.CONTINUITY_MEMORY_FORGET,
            {"memory_id": records[0].id},
        )
        assert forgotten and forgotten["ok"] is True and forgotten["deleted"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_c7_handler_validates_date_and_boolean(tmp_path) -> None:
    store = ContinuityStore(tmp_path / "continuity.sqlite3")
    try:
        handler = ContinuityHandler(
            ContinuityService(store, memory_enabled=False, relationship_enabled=False, life_enabled=False)
        )
        with pytest.raises(ValueError, match="pinned must be a boolean"):
            await handler.handle(
                Method.CONTINUITY_MEMORY_PIN,
                {"memory_id": "x", "pinned": "yes"},
            )
        with pytest.raises(ValueError, match="local_date must use YYYY-MM-DD"):
            await handler.handle(
                Method.CONTINUITY_LIFE_SCHEDULE,
                {"local_date": "09/18/2026"},
            )
    finally:
        store.close()
