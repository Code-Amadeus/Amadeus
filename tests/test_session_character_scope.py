"""Startup role ownership confines chat history, while retained Work stays shared."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import session_manager as sm
from llm import character_prompts
from server.handlers.session_handler import SessionHandler
from test_cooperative_pending_turn import pending_host as pending_host
from test_session_activation import context as context, sessions as sessions, state, write_session


def boot_as(monkeypatch, character_id):
    # Tests simulate separate backend startups without adding a runtime switch.
    monkeypatch.setattr(character_prompts, "active_character_id", lambda: character_id)


@pytest.mark.parametrize("character_id", ["kurisu", "testchar"])
def test_character_identity_survives_snapshot_append_and_interruption(sessions, monkeypatch, character_id):
    boot_as(monkeypatch, character_id)
    sm.create_session("owned")
    assert sm.conversation_history.character_id == character_id
    assert sm.conversation_history.snapshot().character_id == character_id
    with pytest.raises(AttributeError):
        sm.conversation_history.character_id = "different"
    assert sm.append_session_message("owned", role="user", content="Question", turn_id="owned-turn")
    assert sm.persist_interrupted_assistant_turn("owned", turn_id="owned-turn", heard_content="Heard")
    assert sm.save_session("owned")
    sm.create_session("other")
    assert sm.append_session_message("owned", role="assistant", content="Later", turn_id="later-turn")
    history, _ = sm._read_session_history("owned")
    assert history.character_id == character_id
    assert history.dialog[-2]["content"] == "Heard [interrupted by user]"
    assert sm.load_session("owned")[0]
    assert sm.conversation_history.character_id == character_id
    assert json.loads(Path(sm._session_path("owned")).read_text(encoding="utf-8"))["character_id"] == character_id


def test_legacy_history_keeps_kurisu_identity_after_other_character_startup(sessions, monkeypatch):
    write_session("legacy")
    boot_as(monkeypatch, "testchar")
    history, enable = sm._read_session_history("legacy")
    assert history.character_id == sm.get_session_character_id("legacy") == "kurisu"
    sm._persist_history("legacy", history, enable_conversation=enable)
    assert json.loads(Path(sm._session_path("legacy")).read_text(encoding="utf-8"))["character_id"] == "kurisu"
    assert not sm.load_session("legacy")[0]


@pytest.mark.parametrize("character_id", [None, "", 1, "Kurisu", "../../kurisu"])
def test_explicit_invalid_identity_is_never_repaired_as_current_character(sessions, character_id):
    write_session("bad", character_id=character_id)
    with pytest.raises(ValueError, match="character id"):
        sm._read_session_history("bad")
    before = Path(sm._session_path("bad")).read_bytes()
    with pytest.raises(ValueError):
        sm._persist_history("bad", sm.conversation_history.snapshot(), enable_conversation=True)
    assert Path(sm._session_path("bad")).read_bytes() == before


def test_save_refuses_changed_or_unreadable_persisted_identity(sessions):
    path = Path(sm._session_path("A"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["character_id"] = "testchar"
    path.write_text(json.dumps(data), encoding="utf-8")
    for content in [path.read_bytes(), b"{invalid json"]:
        path.write_bytes(content)
        assert not sm.save_session("A")
        assert path.read_bytes() == content


def test_foreign_load_preserves_current_history_fence_and_project_context(context):
    write_session("foreign", character_id="testchar")
    before = state()
    persisted = Path(sm._session_path("A")).read_bytes()
    binding = deepcopy(context.coordinator.conversation_binding("A"))
    guard = Mock()
    sm.configure_activation_guard(guard)
    response = context.handler._load({"session_id": "foreign"})
    assert response["ok"] is False
    assert "Restart" in response["error"] and "AMADEUS_CHARACTER_ID=testchar" in response["error"]
    assert "messages" not in response
    assert not sm.load_session("foreign")[0]
    with pytest.raises(sm.SessionCharacterMismatch):
        sm.set_current_session_id("foreign")
    assert state() == before
    assert Path(sm._session_path("A")).read_bytes() == persisted
    assert context.coordinator.conversation_binding("A") == binding
    guard.assert_not_called()
    context.projection.assert_not_called()
    listed = context.handler._list()["sessions"]
    assert next(row for row in listed if row["id"] == "foreign")["character_id"] == "testchar"
    assert all("messages" not in row for row in listed)
    with pytest.raises(sm.SessionCharacterMismatch):
        context.handler._session_payload("foreign")


def test_missing_foreign_pack_keeps_identity_and_restart_error(sessions, monkeypatch):
    write_session("foreign", character_id="removed-pack")
    forbidden = Mock(side_effect=AssertionError("history ownership must not load a pack"))
    monkeypatch.setattr(character_prompts, "load", forbidden)
    assert sm._read_session_history("foreign")[0].character_id == "removed-pack"
    response = SessionHandler()._load({"session_id": "foreign"})
    assert response["ok"] is False and "AMADEUS_CHARACTER_ID=removed-pack" in response["error"]
    forbidden.assert_not_called()


def test_same_character_switch_keeps_normal_activation(context):
    write_session("same", character_id="kurisu")
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    response = context.handler._load({"session_id": "same"})
    assert response["ok"] is True
    assert response["session"]["character_id"] == "kurisu"
    assert response["messages"][0]["content"] == "ONLY_same"
    guard.assert_called_once_with("A", "same")
    context.projection.assert_called_once_with("session.loaded")


@pytest.mark.parametrize("already_created", [False, True])
async def test_cooperative_loop_creation_and_reuse_reject_foreign_owner(sessions, already_created):
    from server.cooperative_chat_ingress import CooperativeChatManager

    write_session("foreign", character_id="testchar")
    cached = object()
    manager = SimpleNamespace(ingresses={"foreign": cached} if already_created else {})
    with pytest.raises(sm.SessionCharacterMismatch):
        await CooperativeChatManager._ingress_for(manager, "foreign")
    assert manager.ingresses == ({"foreign": cached} if already_created else {})
    manager.ingresses["A"] = cached
    assert await CooperativeChatManager._ingress_for(manager, "A") is cached


async def test_direct_cooperative_continue_refuses_foreign_owner_before_provider_selection(pending_host):
    context = pending_host
    write_session("foreign", character_id="testchar")
    select = Mock()
    context.manager.role_provider_selector = select
    before = state()
    with pytest.raises(sm.SessionCharacterMismatch):
        await context.manager.run("Continue", turn_admission=SimpleNamespace(session_id="foreign"), provider="other")
    assert state() == before
    assert "foreign" not in context.manager.ingresses
    assert context.host.adapter.calls == 0 and context.frames == []
    select.assert_not_called()


def test_work_observer_does_not_copy_foreign_narration_into_either_conversation(sessions, monkeypatch):
    from server.app import _append_work_observer_to_history

    original = Path(sm._session_path("A")).read_bytes()
    boot_as(monkeypatch, "testchar")
    sm.set_current_session_id(None)
    sm.create_session("current")
    before = state()
    _append_work_observer_to_history({"session_id": "A", "main_chat_entry": "Old Work finished"})
    assert Path(sm._session_path("A")).read_bytes() == original and state() == before
    _append_work_observer_to_history({"session_id": "current", "main_chat_entry": "Current Work finished"})
    assert sm.conversation_history.dialog[-1]["content"] == "[WORK_OBSERVER]\nCurrent Work finished"


def test_production_work_resolver_checks_persisted_source_before_first_acceptance(sessions, monkeypatch):
    from server.app import _source_session_role_identity
    from server.work_control import WorkControl
    from test_work_source_proof import _admission, _stores, _v3_payload

    sm.create_session("session-source")
    ledger, work, project = _stores(sessions)
    control = WorkControl(ledger, work, source_role_identity_resolver=_source_session_role_identity)
    source = "Create a board."
    admission = _admission(source)
    payload = _v3_payload(project.project_id, source)
    try:
        control.admit(admission, fence_scope="source-character")
        expected = character_prompts.character_identity("kurisu")
        assert _source_session_role_identity("session-source") == expected
        boot_as(monkeypatch, "testchar")
        with pytest.raises(sm.SessionCharacterMismatch):
            control.seal(admission, payload)
        assert ledger.get_admission(admission.root_id)["plan_id"] is None
        boot_as(monkeypatch, "kurisu")
        control.seal(admission, payload)
        evidence = json.loads(ledger.get_admission(admission.root_id)["plan_json"])["evidence"]
        assert evidence["role_identity"] == expected
        boot_as(monkeypatch, "testchar")
        control.seal(admission, payload)  # Replay relies on accepted identity only.
    finally:
        ledger.close()
        work.close()


async def test_shared_context_opens_same_character_chat_without_reading_old_history(context, monkeypatch):
    boot_as(monkeypatch, "testchar")
    sm.set_current_session_id(None)
    sm.create_session("current")
    original = Path(sm._session_path("A")).read_bytes()
    result = await context.handler._open_context({"project_id": context.project.project_id}, source="slice")
    assert result["ok"] is True and result["session"]["id"] != "A"
    assert result["session"]["character_id"] == "testchar"
    assert result["messages"] == []
    assert result["session"]["context"]["projectId"] == context.project.project_id
    assert Path(sm._session_path("A")).read_bytes() == original
    before = set(sm.list_sessions())
    repeated = await context.handler._open_context({"project_id": context.project.project_id}, source="slice")
    assert repeated["session"]["id"] == result["session"]["id"]
    assert set(sm.list_sessions()) == before


def test_other_character_file_lookup_preserves_draft_and_retained_project_boundary(sessions, monkeypatch):
    from agent_host.work_ledger_store import WorkLedgerStore
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    scratch = sessions / "scratch"
    workspace = scratch / "draft"
    workspace.mkdir(parents=True)
    monkeypatch.setattr("config.settings.WORK_SCRATCH_ROOT", str(scratch))
    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry", lambda _: True)
    with WorkLedgerStore(sessions / "shared.sqlite3") as store:
        coordinator = WorkLedgerCoordinator(store)
        project = store.create_or_get_project(scratch, name="Scratch", metadata={"scratch": True})
        item = store.create_work_item(project.project_id, title="Board", workspace_path=workspace)
        attempt = store.create_attempt(item.work_item_id, provider="synthetic", task="Build board",
                                       metadata={"session_id": "A"})
        store.update_attempt(attempt.attempt_id, execution_status="succeeded")
        file = workspace / "board.html"
        file.write_text("<title>Board</title>", encoding="utf-8")
        artifact = store.register_artifact(item.work_item_id, attempt_id=attempt.attempt_id,
                                           kind="business.file", path=file, status="registered")
        boot_as(monkeypatch, "testchar")
        sm.set_current_session_id(None)
        sm.create_session("current")
        try:
            before = coordinator.conversation_work_items_by_file("current", "board.html", include_kept_projects=True)
            assert before == []
            assert store.get_work_item(item.work_item_id).work_item_id == item.work_item_id
            assert store.get_artifact(artifact.artifact_id).path == str(file)
            promoted = coordinator.promote_work_item_to_project(item.work_item_id)
            after = coordinator.conversation_work_items_by_file("current", "board.html", include_kept_projects=True)
            assert after[0]["work_item_id"] == item.work_item_id
            retained = coordinator.project_work_items_for_resolution("current", promoted["projectId"])
            assert retained["items"][0]["work_item_id"] == item.work_item_id
            assert store.get_artifact(artifact.artifact_id).path == str(file)
            assert file.read_text(encoding="utf-8") == "<title>Board</title>"
            assert not sm.load_session("A")[0]
        finally:
            coordinator.close()
