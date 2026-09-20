from __future__ import annotations

import json

from core.continuity import (
    ContinuityStore,
    MemoryCandidate,
    MemoryKind,
    MemoryResolver,
    RelationshipProposal,
    RelationshipStateClass,
    TurnEvidence,
)
from server.session_state_reaper import reap_session_state, run_startup_reap


def _seed_memory(store: ContinuityStore, session_id: str, key: str, summary: str) -> None:
    evidence = TurnEvidence(
        session_id=session_id, turn_id=f"t-{key}", user_text=summary, observed_at=100.0
    )
    store.apply_memory_candidates(
        evidence,
        (MemoryCandidate(memory_key=key, kind=MemoryKind.USER_FACT, summary=summary),),
        resolver=MemoryResolver(),
        complete_turn=False,
    )


def test_reap_purges_scopes_without_live_or_backup_transcript(tmp_path) -> None:
    db = tmp_path / "runtime" / "continuity.sqlite3"
    sessions = tmp_path / "sessions"
    mirror = tmp_path / "runtime" / "backup" / "sessions"
    sessions.mkdir(parents=True)
    mirror.mkdir(parents=True)

    store = ContinuityStore(db)
    _seed_memory(store, "live-session", "user.fact.live", "keep me")
    _seed_memory(store, "backed-session", "user.fact.backed", "keep me too")
    _seed_memory(store, "orphan-session", "user.fact.orphan", "purge me")
    _seed_memory(store, "orphan-session", "user.fact.orphan_two", "purge me too")
    _seed_memory(store, "", "user.fact.unsessioned", "exempt")
    store.add_topic_mute("旧话题", scope="orphan-session")
    store.forget_memory("user.fact.orphan", scope="orphan-session")
    store.apply_relationship_proposals(
        TurnEvidence(
            session_id="orphan-session", turn_id="rel", user_text="我信任你", observed_at=100.0
        ),
        (
            RelationshipProposal(
                event_type="trust_test",
                state_class=RelationshipStateClass.RELATIONSHIP,
                dimension="trust",
                delta=0.04,
                confidence=1.0,
                occurred_at=100.0,
            ),
        ),
        as_of=100.0,
    )
    store.close()

    # Only "live-session" has a live transcript; "backed-session" survives via
    # the backup mirror; "orphan-session" has neither.
    (sessions / "live-session.json").write_text(
        json.dumps({"session_id": "live-session", "dialog": []}), encoding="utf-8"
    )
    (mirror / "backed-session.json").write_text(
        json.dumps({"session_id": "backed-session", "dialog": []}), encoding="utf-8"
    )

    purged = reap_session_state(
        continuity_db=db, sessions_dir=sessions, backup_sessions_dir=mirror
    )

    assert set(purged) == {"orphan-session"}
    assert purged["orphan-session"]["memories"] == 1
    assert purged["orphan-session"]["tombstones"] == 1
    assert purged["orphan-session"]["relationship_events"] == 1
    assert purged["orphan-session"]["mutes"] == 1

    reopened = ContinuityStore(db)
    try:
        assert reopened.get_active_memory("user.fact.live", scope="live-session") is not None
        assert reopened.get_active_memory("user.fact.backed", scope="backed-session") is not None
        assert reopened.get_active_memory("user.fact.orphan", scope="orphan-session") is None
        assert reopened.get_active_memory("user.fact.orphan_two", scope="orphan-session") is None
        assert (
            reopened.get_active_memory("user.fact.unsessioned", scope="__unsessioned__")
            is not None
        )
        assert reopened.get_active_tombstone("user.fact.orphan", scope="orphan-session") is None
        assert reopened.list_topic_mutes(scope="orphan-session") == []
        diagnostics = reopened.relationship_diagnostics(scope="orphan-session", now=200.0)
        assert diagnostics["active_event_count"] == 0
    finally:
        reopened.close()

    # Idempotent: nothing is left for a second run to reap.
    assert (
        reap_session_state(continuity_db=db, sessions_dir=sessions, backup_sessions_dir=mirror)
        == {}
    )


def test_reap_ignores_a_missing_database(tmp_path) -> None:
    assert (
        reap_session_state(
            continuity_db=tmp_path / "missing.sqlite3",
            sessions_dir=tmp_path / "sessions",
            backup_sessions_dir=tmp_path / "backup",
        )
        == {}
    )


def test_run_startup_reap_opts_out_for_isolated_layouts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AMADEUS_CONTINUITY_DB_PATH", str(tmp_path / "isolated.sqlite3"))

    assert run_startup_reap(tmp_path / "project") == {}
    assert not (tmp_path / "project").exists()
