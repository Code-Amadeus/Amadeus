"""Source capture can pause independently of an enrolled Session's lifetime."""

from collections import OrderedDict
import json
import os
from pathlib import Path

import eidema as memory
import pytest

from core import session_manager as sessions
from core import session_source_journal as journal


MODE = "AMADEUS_SESSION_JOURNAL_ENABLED"
USER = "journal-fixture"


@pytest.fixture
def room(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setattr(sessions, "_SESSION_DIR", str(directory))
    monkeypatch.setattr(sessions, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sessions, "_activation_guard", None)
    monkeypatch.setattr(sessions, "_committed_sources", None)
    monkeypatch.setattr(sessions, "_committed_recent", OrderedDict())
    monkeypatch.setattr(sessions, "_active_assistant_replies", {})
    monkeypatch.setattr(sessions, "conversation_history", sessions.ConversationHistory())
    monkeypatch.setattr(journal, "session_scope", None)
    monkeypatch.setenv(MODE, "1")
    monkeypatch.setenv("AMADEUS_SESSION_JOURNAL_USER", USER)
    monkeypatch.setenv(
        "AMADEUS_SESSION_JOURNAL_CHARACTER", sessions.conversation_history.character_id
    )
    monkeypatch.delenv("AMADEUS_SESSION_JOURNAL_PROJECT", raising=False)
    monkeypatch.setenv("AMADEUS_JOURNAL_LOOKUP_ALLOWED", "1")
    monkeypatch.setenv("AMADEUS_JOURNAL_FORMATION_ALLOWED", "1")
    monkeypatch.setenv("AMADEUS_JOURNAL_ORDINARY_ALLOWED", "0")
    monkeypatch.setenv("AMADEUS_JOURNAL_DERIVED_LOOKUP_ALLOWED", "1")
    monkeypatch.setenv("AMADEUS_JOURNAL_POLICY_REVISION", "fixture-review")
    assert sessions.create_session("A") == "A"
    return directory / "A.json"


def append(mid, body, *, role="user", turn=None):
    assert sessions.append_session_message(
        "A", role=role, content=body, turn_id=turn or mid, message_id=mid
    )


def access():
    return memory.AccessScope(USER, sessions.conversation_history.character_id)


def span(mid, revision=1, incarnation=None):
    return memory.SourceSpan(**journal.coordinate("A", mid, revision, incarnation))


def latest_events(path, generation=-1):
    return journal.read(path)[journal.FIELD]["generations"][generation]["events"]


def feed(maintenance, caller, path, stream="m0:A", after=0):
    result = journal.page(path, stream, caller, after=after, limit=100)
    maintenance.register_stream(caller, stream, memory.MemoryScope(**result["scope"]))
    changes = [
        memory.SourceChange(
            **{
                **event,
                "source": memory.SourceSpan(**event["source"]),
                "formation_allowed": journal.allows_formation(event),
            }
        )
        for event in result["events"]
    ]
    assert maintenance.append_changes(caller, stream, changes) == result["high_water"]
    return result


def test_paused_session_revisions_replay_into_actual_sdk_without_recapture(
    room, monkeypatch, tmp_path
):
    append("anchor", "Independent old source.")
    append("reply", "Original interrupted reply.", role="assistant", turn="voice-turn")
    caller = access()
    store = memory.MemoryStore(tmp_path / "memory.sqlite3")
    try:
        maintenance = memory.Maintenance(store)
        start = feed(maintenance, caller, room)["high_water"]
        independent = maintenance.lookup_input(caller, "m0:A", span("anchor"))
        old_reply = maintenance.lookup_input(caller, "m0:A", span("reply"))
        assert old_reply.source.revision == 1
        for saved_input, output in ((old_reply, "dependent-output"), (independent, "independent-output")):
            reads = store.capture_read_set(caller, sources=(saved_input,), now_ms=1)
            store.register_output_use(caller, reads, source=span(output), now_ms=1)
        detail = store.remember(
            caller,
            memory.MemoryScope("character", caller.user_id, caller.character_id),
            operation_id="explicit-save",
            item_id="independent-copy",
            text="User-confirmed independent note.",
            source=span("reply"),
            now_ms=1,
        )

        monkeypatch.setenv(MODE, "0")
        monkeypatch.delenv("AMADEUS_SESSION_JOURNAL_USER")
        assert sessions.load_session("A")[0]
        assert sessions.persist_interrupted_assistant_turn(
            "A", turn_id="voice-turn", heard_content="Heard prefix."
        )
        assert "Heard prefix." in sessions.read_session_source("A", "reply", revision=2)["text"]
        append("paused", "Written during the pause.")
        assert (
            sessions.read_committed_session_source("A", "paused", revision=1)["text"]
            == "Written during the pause."
        )
        with pytest.raises(PermissionError, match="journal_delivery_disabled"):
            journal.page(room, "m0:A", caller, after=start, limit=100)
        paused = latest_events(room)[start:]
        assert [(event["action"], event["source"]) for event in paused] == [
            ("withdraw", journal.coordinate("A", "reply", 1))
        ]

        monkeypatch.setenv("AMADEUS_SESSION_JOURNAL_USER", USER)
        monkeypatch.setenv(MODE, "1")
        append("resumed", "New note after resuming.")
        end = feed(maintenance, caller, room, after=start)["high_water"]
        assert end - start == 2
        assert maintenance.lookup_input(caller, "m0:A", span("anchor")) == independent
        assert maintenance.lookup_input(caller, "m0:A", span("resumed")).source.revision == 1
        for unavailable in (span("reply"), span("reply", 2), span("paused")):
            with pytest.raises(memory.MemoryNotFound):
                maintenance.lookup_input(caller, "m0:A", unavailable)
        with pytest.raises(memory.MemoryConflict):
            store.capture_read_set(caller, outputs=(span("dependent-output"),), now_ms=2)
        assert store.capture_read_set(caller, outputs=(span("independent-output"),), now_ms=2).outputs
        assert store.detail(caller, detail.memory_id).memory.text == "User-confirmed independent note."
        assert feed(maintenance, caller, room)["high_water"] == end

        assert sessions.persist_interrupted_assistant_turn(
            "A", turn_id="voice-turn", heard_content="New authorized revision."
        )
        assert feed(maintenance, caller, room, after=end)["high_water"] == end + 1
        assert maintenance.lookup_input(caller, "m0:A", span("reply", 3)).source.revision == 3
    finally:
        store.close()


def test_paused_delete_and_reuse_keep_old_stream_and_never_restore_its_body(room, monkeypatch):
    append("same", "First generation body.")
    caller = access()
    monkeypatch.setenv(MODE, "0")
    assert sessions.delete_session("A")
    assert "A" not in sessions.list_sessions() and not sessions.load_session("A")[0]
    tombstone = room.read_text(encoding="utf-8")
    assert "First generation body." not in tombstone
    assert "dialog" not in json.loads(tombstone)
    retired_events = latest_events(room)
    assert [event["action"] for event in retired_events] == ["upsert", "withdraw"]
    assert retired_events[0]["source"] == retired_events[1]["source"]

    assert sessions.create_session("A") == "A"
    append("same", "Second generation body.")
    current = sessions.read_session_source("A", "same", revision=1)
    assert (
        current["incarnation"]
        == sessions.read_committed_session_source("A", "same", revision=1)["incarnation"]
    )
    assert latest_events(room, generation=0) == retired_events and latest_events(room) == []
    monkeypatch.setenv(MODE, "1")
    append("after", "Selected when source capture resumes.")
    new_stream = f"m0:A:inc:{current['incarnation']}"
    new = journal.page(room, new_stream, caller, after=0, limit=100)
    assert [(row["action"], row["source"]) for row in new["events"]] == [
        ("upsert", journal.coordinate("A", "after", 1, current["incarnation"])),
    ]
    assert journal.page(room, "m0:A", caller, after=0, limit=100)["retired"]


def test_control_restriction_on_paused_version_survives_resumption(room, monkeypatch, tmp_path):
    append("control", "Synthetic request to manage memory.")
    append("unrelated", "Independent usable material.")
    monkeypatch.setenv(MODE, "0")
    sessions.mark_memory_control_sources("A", (("user", journal.coordinate("A", "control", 1)),))
    assert latest_events(room)[-1]["evidence_only"]
    monkeypatch.setenv(MODE, "1")
    assert sessions.save_session("A")
    caller = access()
    store = memory.MemoryStore(tmp_path / "memory.sqlite3")
    try:
        maintenance = memory.Maintenance(store)
        feed(maintenance, caller, room)
        with pytest.raises(memory.MemoryNotFound):
            maintenance.lookup_input(caller, "m0:A", span("control"), discoverable=True)
        assert maintenance.lookup_input(
            caller, "m0:A", span("unrelated"), discoverable=True
        ).source == span("unrelated")
    finally:
        store.close()


def test_paused_mode_leaves_unregistered_session_in_legacy_format(room, monkeypatch):
    monkeypatch.setenv(MODE, "0")
    assert sessions.create_session("legacy") == "legacy"
    assert sessions.append_session_message(
        "legacy", role="user", content="Ordinary Session.", turn_id="t", message_id="u"
    )
    path = room.with_name("legacy.json")
    assert journal.FIELD not in json.loads(path.read_bytes())
    assert sessions.load_session("legacy")[0] and sessions.delete_session("legacy")
    assert not path.exists()


def test_paused_publish_failure_keeps_file_and_committed_source(room, monkeypatch):
    append("old", "Stable content.")
    before = room.read_bytes()
    monkeypatch.setenv(MODE, "0")

    def blocked_publish(*_args):
        raise OSError("synthetic pre-commit failure")

    monkeypatch.setattr(journal, "_replace", blocked_publish)
    assert not sessions.append_session_message(
        "A", role="assistant", content="Not committed.", turn_id="t", message_id="new"
    )
    assert room.read_bytes() == before
    assert not tuple(room.parent.glob(".A.journal-*.tmp"))
    assert (
        sessions.read_committed_session_source("A", "old", revision=1)["text"] == "Stable content."
    )
    with pytest.raises(LookupError):
        sessions.read_committed_session_source("A", "new", revision=1)


def test_changed_active_journal_owner_cannot_erase_durable_generation(room, monkeypatch):
    append("old", "Stable content.")
    before = room.read_bytes()
    monkeypatch.setenv("AMADEUS_SESSION_JOURNAL_USER", "another-owner")
    assert not sessions.save_session("A") and room.read_bytes() == before
    with pytest.raises(PermissionError, match="journal_stream_scope_mismatch"):
        journal.page(
            room,
            "m0:A",
            memory.AccessScope("another-owner", access().character_id),
            after=0,
            limit=100,
        )


class PosixBranch:
    name = "posix"

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.mark.parametrize("use_posix_branch", [False, True], ids=["native", "posix-branch"])
def test_exclusive_install_preserves_existing_file(tmp_path, monkeypatch, use_posix_branch):
    if use_posix_branch:
        monkeypatch.setattr(journal, "os", PosixBranch())
    target = tmp_path / "exclusive.json"
    journal.publish(target, {"first": True}, create=True)
    with pytest.raises(FileExistsError):
        journal.publish(target, {"other": True}, create=True)
    assert json.loads(target.read_bytes()) == {"first": True}
    assert list(tmp_path.iterdir()) == [Path(target)]
