"""Different Session generations may reuse a message ID and revision."""

import json
from unittest.mock import Mock

import eidema as memory
import pytest

from config.memory import MemoryConfig
from core import session_manager as sessions
from core import session_source_journal as journal
from core.chat_history_projection import project_role_history
from server.cooperative_chat_ingress import _shared_role_history
from server.handlers.memory_handler import MemoryHandler
from server.memory_policy_recovery import PolicyRecoveryError, recover_source_policy
from server.memory_service import HostMemoryService, MemoryLookupEvidence
from server.protocol import Method
from test_session_source_lifecycle import MODE, USER, access, append, room as room


def select_source(row):
    return {
        **{
            key: row[key]
            for key in ("session_id", "message_id", "revision", "projection_version", "incarnation")
            if key in row
        },
        "start_cp": 0,
        "end_cp": len(row["text"]),
    }


def shared():
    return _shared_role_history("A", "next-turn")


def visible(rows):
    return [item["text"] for item in project_role_history(rows)]


async def test_real_history_and_mutations_keep_generation_while_reusing_session_and_message_ids(
    room, tmp_path, monkeypatch
):
    old_body = "An earlier private detail."
    new_body = "A new independent answer."
    unrelated = "Another current source."
    append("same", old_body)
    # Body payloads cannot supply the identity that stamps a role snapshot.
    sessions.conversation_history.dialog[0]["incarnation"] = "untrusted-row-metadata"
    old_capture = shared()
    assert "incarnation" not in old_capture[0]["_source"]
    path = tmp_path / "memory.sqlite3"
    config = MemoryConfig(db_path=path, user_id=USER, semantic_enabled=False)
    service = HostMemoryService(config)
    monkeypatch.setattr(sessions, "_model_history_projection", service.project_history)

    async def request(method, *, token=None, **values):
        token = token or service.context("A")["context_token"]
        return await MemoryHandler(service).handle(
            method, {"session_id": "A", "context_token": token, **values}
        )

    async def remember(key, source):
        return await request(
            Method.MEMORY_REMEMBER,
            text="Synthetic saved note.",
            scope="character",
            source=source,
            operation_id=key,
            item_id=key,
        )

    async def forget(key, receipt):
        memory_id = receipt["operation"]["memory_id"]
        preview = await request(
            Method.MEMORY_FORGET_PREVIEW, memory_id=memory_id, expected_version=1
        )
        assert preview["ok"], preview
        result = await request(
            Method.MEMORY_FORGET,
            memory_id=memory_id,
            expected_version=1,
            confirmed_targets=preview["preview"]["targets"],
            operation_id=key,
            item_id=key,
        )
        assert result["ok"], result
        return preview["preview"]["source_exclusions"]

    try:
        await service._open()
        before = await request(Method.MEMORY_SOURCES)
        assert before["ok"] and before["sources"][0]["text"] == old_body
        old_source = select_source(before["sources"][0])
        old_token = before["context"]["context_token"]
        old_note = await remember("original", old_source)
        assert old_note["ok"], old_note
        assert "incarnation" not in (await forget("first-forget", old_note))[0]
        assert old_body not in visible(old_capture)

        monkeypatch.setenv(MODE, "0")
        assert sessions.delete_session("A") and sessions.create_session("A") == "A"
        append("same", new_body)
        append("independent", unrelated)
        new_capture = shared()
        selection = await request(Method.MEMORY_SOURCES)
        current = select_source(
            next(row for row in selection["sources"] if row["message_id"] == "same")
        )
        generation = current["incarnation"]
        assert new_capture[0]["_source"]["incarnation"] == generation
        assert new_body in visible(new_capture) and old_body not in visible(old_capture)
        assert new_body in [row["content"] for row in sessions.conversation_history.model_recent()]
        assert all(
            "_source" not in row and "incarnation" not in row
            for row in project_role_history(new_capture)
        )
        assert (await request(Method.MEMORY_SOURCES, token=old_token))["error"] == "context_changed"
        assert (await remember("old-selection", old_source))["error"] == "conflict"
        assert (await remember("bad-generation", {**current, "incarnation": "wrong-generation"}))[
            "error"
        ] == "conflict"

        # Inactive role history is captured from the cached Session and keeps
        # the same generation. Switching away cannot relabel its contents.
        assert sessions.create_session("B") == "B"
        assert visible(shared()) == [new_body, unrelated]
        assert sessions.load_session("A")[0]
        new_note = await remember("new-provenance", current)
        assert new_note["ok"], new_note
        memory_id = new_note["operation"]["memory_id"]
        detail = await request(Method.MEMORY_GET, memory_id=memory_id)
        assert detail["memory"]["source"]["incarnation"] == generation

        evidence = MemoryLookupEvidence(
            service.context("A"), (memory.RecallReference(memory_id, 1),), {}
        )
        derived_body = "The exact saved-note reply."
        async with service.lookup_delivery(evidence, message_id="derived") as published:
            append("derived", derived_body, role="assistant")
            published()
        bound = memory.SourceSpan(**journal.coordinate("A", "derived", 1, generation))
        assert bound in service._store.registered_output_sources(access())
        dependencies, _ = await service.history_dependencies("A", shared())
        assert dependencies.candidates == evidence.candidates
        assert derived_body in visible(shared())

        assert (await forget("second-forget", new_note))[0]["incarnation"] == generation
        assert unrelated in visible(shared())
        assert all(text not in visible(shared()) for text in (old_body, new_body, derived_body))
        assert new_body not in visible(new_capture)
        assert old_body not in visible(old_capture)
    finally:
        await service.close()

    # Rebuild from the real core with both coordinate formats, then reopen Host
    # with the SDK import unavailable and verify the same history protection.
    service._policy.path.unlink()
    recover_source_policy(path, USER)
    monkeypatch.setattr(
        "server.memory_service.importlib.import_module", Mock(side_effect=ImportError("synthetic"))
    )
    degraded = HostMemoryService(config)
    try:
        await degraded._open()
        assert degraded._store is None and degraded._exclusions_ready
        sessions.configure_model_history_projection(degraded.project_history)
        assert unrelated in visible(shared())
        assert all(text not in visible(shared()) for text in (old_body, new_body, derived_body))
    finally:
        await degraded.close()


def test_recovery_refuses_malformed_v2_coordinates(tmp_path):
    path = tmp_path / "memory.sqlite3"
    store = memory.MemoryStore(path)
    caller = memory.AccessScope(USER, "kurisu")
    try:
        # The generic SDK can store opaque coordinates. The Host owns the
        # interpretation of its named source formats.
        malformed = memory.SourceSpan(
            journal.V2, json.dumps(["A", "message", journal.VERSION]), 1, 0, 1
        )
        info = store.remember(
            caller,
            memory.MemoryScope("character", USER, "kurisu"),
            source=malformed,
            text="Synthetic malformed Host source.",
            operation_id="r",
            item_id="r",
            now_ms=1,
        )
        preview = store.preview_forget(caller, info.memory_id, 1)
        store.forget(
            caller,
            info.memory_id,
            1,
            confirmed_targets=preview.targets,
            operation_id="f",
            item_id="f",
            now_ms=2,
        )
    finally:
        store.close()
    before = path.read_bytes()
    with pytest.raises(PolicyRecoveryError, match="invalid_coordinates"):
        recover_source_policy(path, USER)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".source-policy.sqlite3").exists()
