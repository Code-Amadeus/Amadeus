"""Real-file Session preparation and synchronous activation-boundary contracts."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_host.work_ledger_store import WorkLedgerStore
from core import session_manager as sm
from server.handlers.session_handler import SessionHandler
from server.work_ledger_coordinator import WorkLedgerCoordinator


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("A")
    sm.conversation_history.add_user("ONLY_A")
    sm.conversation_history.last_summary = "SUMMARY_A"
    assert sm.save_session("A")
    return tmp_path


def state():
    return sm.get_current_session_id(), deepcopy(sm.conversation_history.__dict__)


def write_session(sid, **changes):
    data = {"session_id": sid, "dialog": [{"role": "user", "content": f"ONLY_{sid}"}]}
    data.update(changes)
    Path(sm._session_path(sid)).write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def context(sessions, monkeypatch):
    project_root = sessions / "project"
    project_root.mkdir()
    store = WorkLedgerStore(sessions / "ledger.sqlite3")
    project = store.create_or_get_project(project_root)
    coordinator = WorkLedgerCoordinator(store)
    monkeypatch.setattr("server.work_ledger_coordinator.cwd_in_project_registry", lambda _: True)
    coordinator.bind_session_context("A", project.project_id, source="test")
    handler = SessionHandler()
    handler.configure(work_coordinator=coordinator, is_chat_busy=lambda: False)
    monkeypatch.setattr(handler, "_enable_conversation", lambda: None)
    projection = Mock()
    monkeypatch.setattr(handler, "_publish_context_projection_now", projection)
    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.session_handler.bus.emit", emit)
    try:
        yield SimpleNamespace(
            handler=handler, coordinator=coordinator, store=store, project=project,
            projection=projection, emit=emit,
        )
    finally:
        coordinator.close()


@pytest.mark.parametrize("field", ["max_rounds", "summary_token_threshold"])
def test_bad_numeric_setting_does_not_install_another_sessions_history(sessions, field):
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    write_session("B", last_summary="SUMMARY_B", **{field: "not-an-int"})
    assert sm.load_session("B") == (False, False)
    assert state() == before
    guard.assert_not_called()


def test_new_session_does_not_inherit_the_previous_summary(sessions):
    sm.create_session("B")
    assert sm.conversation_history.last_summary == ""
    assert sm.conversation_history.dialog == []
    assert json.loads(Path(sm._session_path("B")).read_text(encoding="utf-8"))["last_summary"] == ""


def test_failed_new_session_persistence_does_not_activate_it(sessions, monkeypatch):
    before = state()
    not_directory = sessions / "not-a-directory"
    not_directory.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(sm, "_SESSION_DIR", str(not_directory))
    with pytest.raises(OSError):
        sm.create_session("B")
    assert state() == before


def test_failed_serialization_preserves_the_last_durable_session_file(sessions):
    path = Path(sm._session_path("A"))
    previous = path.read_bytes()
    sm.conversation_history.dialog.append({"role": "user", "content": object()})
    assert sm.save_session("A") is False
    assert path.read_bytes() == previous


def test_file_payload_cannot_relabel_the_requested_session(sessions):
    before = state()
    write_session("B", session_id="C")
    assert sm.load_session("B") == (False, False)
    assert state() == before


def test_correlated_append_targets_original_session_without_selecting_it(sessions):
    sm.create_session("B")
    sm.conversation_history.add_user("UNSAVED_B")
    before = state()
    revision = sm.get_session_selection_revision()
    assert sm.append_session_message("A", role="assistant", content="A displayed reply", turn_id="a-run")
    assert state() == before and sm.get_session_selection_revision() == revision
    data = json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8"))
    assert data["dialog"][-1] == {"role":"assistant", "content":"A displayed reply", "turn_id":"a-run"}
    assert all(row["content"] != "UNSAVED_B" for row in data["dialog"])
    assert sm.save_session("B")
    assert sm.load_session("A")[0]
    assert sm.conversation_history.dialog[-1]["content"] == "A displayed reply"


def test_correlated_append_preserves_loaded_edits_and_replay_identity(sessions):
    sm.conversation_history.add_user("UNSAVED_A")
    sm.conversation_history.add_assistant("UNSAVED_REPLY", turn_id="already-in-memory")
    assert sm.append_session_message("A", role="assistant", content="UNSAVED_REPLY", turn_id="already-in-memory")
    persisted = json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8"))
    assert persisted["dialog"][-1]["content"] == "UNSAVED_REPLY"
    revision = sm.get_session_selection_revision()
    assert sm.append_session_message("A", role="user", content="new input", turn_id="input")
    assert sm.append_session_message("A", role="assistant", content="displayed", turn_id="input")
    before = state()
    assert sm.append_session_message("A", role="assistant", content="displayed", turn_id="input")
    assert not sm.append_session_message("A", role="assistant", content="changed", turn_id="input")
    assert state() == before and sm.get_session_selection_revision() == revision
    data = json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8"))
    assert data["dialog"] == sm.conversation_history.dialog
    assert any(row["content"] == "UNSAVED_A" for row in data["dialog"])


def test_correlated_append_cannot_recreate_or_relabel_a_session(sessions):
    assert sm.delete_session("A")
    assert not sm.append_session_message("A", role="assistant", content="late", turn_id="old")
    assert not Path(sm._session_path("A")).exists()
    write_session("B", session_id="C")
    before = Path(sm._session_path("B")).read_bytes()
    assert not sm.append_session_message("B", role="assistant", content="late", turn_id="old")
    assert Path(sm._session_path("B")).read_bytes() == before


def test_failed_correlated_append_leaves_memory_and_file_unchanged(sessions, monkeypatch):
    before, persisted = state(), Path(sm._session_path("A")).read_bytes()
    monkeypatch.setattr(sm.os, "replace", Mock(side_effect=OSError("write failed")))
    assert not sm.append_session_message("A", role="assistant", content="visible", turn_id="run")
    assert state() == before and Path(sm._session_path("A")).read_bytes() == persisted


def test_async_display_receipt_persists_to_its_origin_after_selection_changes(sessions):
    from server.cooperative_delivery import CooperativeHostDelivery

    async def scenario():
        async def displayed_then_switched(event):
            assert event["session_id"] == sm.get_current_session_id() == "A"
            sm.create_session("B")  # display happened in A before acknowledgement returns
            sm.conversation_history.add_user("B_ONLY")
            return True
        publication = CooperativeHostDelivery(session_id="A", display=displayed_then_switched,
            record_display=sm.append_session_message)
        assert await publication({"source":"kurisu", "cause":"provider-run-A", "text":"A question"})
        assert sm.get_current_session_id() == "B"
        assert sm.conversation_history.dialog == [{"role":"user", "content":"B_ONLY"}]
        assert publication.receipts[0]["published"] and publication.receipts[0]["history_recorded"]
        data = json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8"))
        assert data["dialog"][-1]["content"] == "A question"

        unseen = CooperativeHostDelivery(session_id="A", display=lambda event:False,
            record_display=sm.append_session_message)
        assert not await unseen({"source":"kurisu", "cause":"late-A", "text":"Not displayed"})
        assert json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8")) == data
    asyncio.run(scenario())


def test_legacy_history_keeps_configuration_metadata_title_and_singleton(sessions):
    path = Path(sm._session_path("B"))
    dialog = [{"role": "assistant", "content": "legacy", "turn_id": "t", "extra": 3}]
    path.write_text(json.dumps({
        "dialog": dialog, "last_summary": "summary", "max_rounds": "7",
        "summary_token_threshold": "200", "title": "Existing title", "enable_conversation": True,
    }), encoding="utf-8")
    singleton = sm.conversation_history
    before = state()
    seen = []

    def guard(old, new):
        seen.append((old, new, state()))

    sm.configure_activation_guard(guard)
    assert sm.load_session("B") == (True, True)
    assert seen == [("A", "B", before)]
    assert sm.conversation_history is singleton
    assert sm.conversation_history.dialog == dialog
    assert sm.conversation_history.max_rounds == 7
    assert sm.conversation_history.summary_token_threshold == 200
    assert sm.save_session("B")
    assert sm.get_session_title("B") == "Existing title"


def test_prepared_session_is_not_an_activation(sessions):
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    assert sm.create_session("B", activate=False) == "B"
    assert state() == before
    assert "B" in sm.list_sessions()
    guard.assert_not_called()


def test_only_successful_independent_selection_advances_queued_input_revision(sessions):
    initial = sm.get_session_selection_revision()
    sm.create_session("B", activate=False)
    assert sm.get_session_selection_revision() == initial
    assert sm.load_session("B", expected_selection_revision=initial)[0]
    assert sm.get_session_selection_revision() == initial
    assert sm.load_session("A", expected_selection_revision=initial)[0]
    assert sm.get_session_selection_revision() == initial
    assert sm.load_session("A")[0]  # same-id independent reload is a new selection
    assert sm.get_session_selection_revision() == initial + 1
    assert sm.load_session("B", expected_selection_revision=initial) == (False, False)
    assert sm.get_current_session_id() == "A"
    assert sm.delete_session("B")  # unrelated inactive cleanup is not a selection
    assert sm.get_session_selection_revision() == initial + 1
    assert sm.delete_session("A")
    assert sm.get_session_selection_revision() == initial + 2


@pytest.mark.parametrize("derived", [False, True])
def test_refused_selection_keeps_revision_and_derived_install_still_requires_guard(sessions, derived):
    sm.create_session("B", activate=False)
    initial = sm.get_session_selection_revision()
    guard = Mock(side_effect=RuntimeError("cannot fence"))
    sm.configure_activation_guard(guard)
    options = {"expected_selection_revision": initial} if derived else {}
    assert sm.load_session("B", **options) == (False, False)
    guard.assert_called_once_with("A", "B")
    assert sm.get_session_selection_revision() == initial
    assert sm.get_current_session_id() == "A"


@pytest.mark.parametrize("operation", ["load", "create"])
def test_guard_refusal_preserves_old_context_and_cleans_only_new_creation(sessions, operation):
    before = state()
    original = Path(sm._session_path("A")).read_bytes()
    sm.configure_activation_guard(Mock(side_effect=RuntimeError("fence unavailable")))
    if operation == "load":
        write_session("B")
        assert sm.load_session("B") == (False, False)
        assert Path(sm._session_path("B")).exists()
    else:
        with pytest.raises(RuntimeError, match="fence unavailable"):
            sm.create_session("B")
        assert not Path(sm._session_path("B")).exists()
    assert state() == before
    assert Path(sm._session_path("A")).read_bytes() == original


@pytest.mark.parametrize("changes", [
    {"dialog": {}}, {"dialog": ["text"]}, {"dialog": [{"role": "user", "content": 3}]},
    {"last_summary": []},
])
def test_invalid_history_never_reaches_activation(sessions, changes):
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    write_session("B", **changes)
    assert sm.load_session("B") == (False, False)
    assert state() == before
    guard.assert_not_called()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_guard_must_complete_synchronously_and_return_none(sessions, asynchronous):
    async def unfinished(old, new):
        raise AssertionError("a coroutine must not be treated as acceptance")

    before = state()
    write_session("B")
    sm.configure_activation_guard(unfinished if asynchronous else lambda old, new: False)
    assert sm.load_session("B") == (False, False)
    assert state() == before


def test_duplicate_create_does_not_overwrite_or_delete_existing_session(sessions):
    before = state()
    original = Path(sm._session_path("A")).read_bytes()
    with pytest.raises(FileExistsError):
        sm.create_session("A")
    assert state() == before
    assert Path(sm._session_path("A")).read_bytes() == original


@pytest.mark.parametrize("sid", ["review space", "review.space", "review/space", "中文", "A?"])
def test_new_identity_must_round_trip_without_sanitized_aliases(sessions, sid):
    before = state()
    with pytest.raises(ValueError, match="Session id"):
        sm.create_session(sid)
    assert sm.list_sessions() == ["A"]
    assert state() == before


def test_supported_new_identity_round_trips_through_index(sessions):
    sid = sm.create_session("new-session_123")
    assert sid in sm.list_sessions()
    assert sm.load_session("A")[0]
    assert sm.load_session(sid)[0]
    assert sm.get_current_session_id() == sid


def test_handler_reports_invalid_new_identity_without_switching(context):
    before = state()
    result = context.handler._create({"session_id": "review space"})
    assert result["ok"] is False
    assert "Session id" in result["error"]
    assert state() == before
    assert sm.list_sessions() == ["A"]


def test_delete_alias_cannot_remove_active_file_without_activation_check(sessions):
    sm.create_session("A_")
    guard = Mock(side_effect=RuntimeError("refuse"))
    sm.configure_activation_guard(guard)
    assert sm.delete_session("A?") is False
    assert Path(sm._session_path("A_")).exists()
    assert sm.get_current_session_id() == "A_"
    guard.assert_not_called()


def test_case_alias_cannot_delete_a_differently_named_session_on_windows(sessions):
    alias = Path(sm._SESSION_DIR) / "a.json"
    if not alias.exists():
        # A case-sensitive filesystem has two different keys, so no alias.
        assert sm.delete_session("a") is False
        return
    before = state()
    assert sm.delete_session("a") is False
    assert Path(sm._session_path("A")).exists()
    assert state() == before


def test_failed_replace_keeps_last_good_file_and_removes_owned_temporary(sessions, monkeypatch):
    path = Path(sm._session_path("A"))
    original = path.read_bytes()
    sm.conversation_history.add_user("unsaved")
    monkeypatch.setattr(sm.os, "replace", Mock(side_effect=OSError("replacement denied")))
    assert sm.save_session("A") is False
    assert path.read_bytes() == original
    assert list(path.parent.glob(".session-*.tmp")) == []


def test_denied_active_delete_preserves_file_context_binding_and_projection(context):
    before = state()
    path = Path(sm._session_path("A"))
    original = path.read_bytes()
    binding = context.store.get_conversation_binding("A")
    guard = Mock(side_effect=RuntimeError("fence unavailable"))
    sm.configure_activation_guard(guard)
    result = context.handler._delete({"session_id": "A"})
    assert result["ok"] is False
    assert state() == before
    assert path.read_bytes() == original
    assert context.store.get_conversation_binding("A") == binding
    context.projection.assert_not_called()
    guard.assert_called_once_with("A", None)


def test_failed_active_file_delete_does_not_clear_context_or_report_success(context, monkeypatch):
    before = state()
    binding = context.store.get_conversation_binding("A")
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    monkeypatch.setattr(sm.os, "remove", Mock(side_effect=OSError("file busy")))
    assert context.handler._delete({"session_id": "A"})["ok"] is False
    assert state() == before
    assert context.store.get_conversation_binding("A") == binding
    assert Path(sm._session_path("A")).exists()
    context.projection.assert_not_called()
    guard.assert_called_once_with("A", None)


def test_successful_active_delete_checks_once_before_removing_file(context):
    seen = []

    def guard(old, new):
        seen.append((old, new, Path(sm._session_path("A")).exists(), state()))

    before = state()
    sm.configure_activation_guard(guard)
    assert context.handler._delete({"session_id": "A"})["ok"] is True
    assert seen == [("A", None, True, before)]
    assert sm.get_current_session_id() is None
    assert sm.conversation_history.dialog == []
    assert sm.conversation_history.last_summary == ""
    assert context.store.get_conversation_binding("A") is None
    context.projection.assert_called_once_with("session.deleted")


def test_inactive_delete_does_not_change_or_guard_active_context(sessions):
    sm.create_session("B", activate=False)
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    assert sm.delete_session("B")
    assert state() == before
    guard.assert_not_called()


def test_failed_existing_context_load_does_not_publish_session_changed(context, monkeypatch):
    before = state()
    monkeypatch.setattr(context.handler, "_matching_session", lambda *args: "missing")
    result = asyncio.run(context.handler._open_context(
        {"project_id": context.project.project_id}, source="test",
    ))
    assert result["ok"] is False
    assert "openedContext" not in result
    assert state() == before
    context.emit.assert_not_called()
    context.projection.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "open"])
def test_failed_project_binding_never_activates_prepared_context(context, monkeypatch, operation):
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    monkeypatch.setattr(context.handler, "_matching_session", lambda *args: "")
    monkeypatch.setattr(context.coordinator, "bind_session_context", Mock(side_effect=ValueError("bad project")))
    params = {"session_id": "B", "project_id": context.project.project_id}
    result = context.handler._create(params) if operation == "create" else asyncio.run(
        context.handler._open_context(params, source="test"),
    )
    assert result["ok"] is False
    assert state() == before
    assert sm.list_sessions() == ["A"]
    guard.assert_not_called()
    context.emit.assert_not_called()
    context.projection.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "open"])
def test_project_binding_is_prepared_before_activation_guard(context, monkeypatch, operation):
    before = state()
    seen = []
    monkeypatch.setattr(context.handler, "_matching_session", lambda *args: "")

    def guard(old, new):
        seen.append((old, new, state(), context.coordinator.session_project(new)))

    sm.configure_activation_guard(guard)
    params = {"session_id": "B", "project_id": context.project.project_id}
    result = context.handler._create(params) if operation == "create" else asyncio.run(
        context.handler._open_context(params, source="test"),
    )
    assert result["ok"] is True
    assert seen == [("A", result["current_session_id"], before, context.project.project_id)]


@pytest.mark.parametrize("operation", ["create", "load", "open"])
def test_previous_save_failure_refuses_switch_before_guard(context, monkeypatch, operation):
    before = state()
    guard = Mock(return_value=None)
    sm.configure_activation_guard(guard)
    monkeypatch.setattr(sm, "save_session", lambda *args, **kwargs: False)
    monkeypatch.setattr(context.handler, "_matching_session", lambda *args: "")
    params = {"session_id": "B", "project_id": context.project.project_id}
    if operation == "open":
        result = asyncio.run(context.handler._open_context(params, source="test"))
    else:
        result = getattr(context.handler, "_" + operation)(params)
    assert result["ok"] is False
    assert state() == before
    assert sm.list_sessions() == ["A"]
    guard.assert_not_called()
    context.emit.assert_not_called()
    context.projection.assert_not_called()
