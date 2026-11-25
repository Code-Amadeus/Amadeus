"""Committed source snapshots retain identity independently of live edits."""
from collections import OrderedDict
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core import session_manager as sm


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "_committed_sources", None)
    monkeypatch.setattr(sm, "_committed_recent", OrderedDict())
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("A")


def test_long_current_snapshot_is_detached_and_supports_exact_old_source(sessions):
    history = sm.conversation_history
    history.dialog = [{"role":"user", "content":f"source {index}",
        "message_id":f"message-{index}", "revision":1} for index in range(10_000)]
    assert sm.save_session("A")
    history.dialog[0].update(content="uncommitted replacement", revision=2)
    assert sm.read_committed_session_source("A", "message-0", revision=1)["text"] == "source 0"
    with pytest.raises(ValueError):
        sm.read_committed_session_source("A", "message-0", revision=2)
    rows = sm.committed_session_rows("A", limit=2)
    assert [row["content"] for row in rows] == ["source 9998", "source 9999"]
    rows[0]["content"] = "caller edit"
    assert sm.committed_session_rows("A", limit=2)[0]["content"] == "source 9998"


def test_failed_commit_never_publishes_new_source(sessions, monkeypatch):
    sm.conversation_history.add_user("committed", turn_id="turn", message_id="original")
    assert sm.save_session("A")
    sm.conversation_history.add_user("pending", turn_id="later", message_id="pending")
    monkeypatch.setattr(sm, "write_text", Mock(side_effect=OSError("synthetic commit failure")))
    assert not sm.save_session("A")
    assert sm.read_committed_session_source("A", "original", revision=1)["text"] == "committed"
    with pytest.raises(LookupError):
        sm.read_committed_session_source("A", "pending", revision=1)
    assert [row["content"] for row in sm.committed_session_recent("A")] == ["committed"]


def test_inactive_commit_keeps_current_owner_and_delete_invalidates_only_target(sessions):
    assert sm.append_session_message("A", role="user", content="A original", turn_id="A-turn", message_id="A-first")
    sm.create_session("B")
    assert sm.append_session_message("B", role="user", content="B original", turn_id="B-turn", message_id="B-first")
    active_snapshot = sm._committed_sources
    active_recent = sm._committed_recent["B"]
    assert sm.append_session_message("A", role="assistant", content="A late reply", turn_id="A-turn", message_id="A-late")
    assert sm._committed_sources is active_snapshot
    assert sm._committed_recent["B"] is active_recent
    assert sm.get_current_session_id() == "B"
    assert sm.read_committed_session_source("B", "B-first", revision=1)["text"] == "B original"
    with pytest.raises(LookupError):
        sm.read_committed_session_source("A", "A-first", revision=1)
    recent = sm.committed_session_recent("A")
    assert [row["content"] for row in recent] == ["A original", "A late reply"]
    recent[0]["content"] = "caller edit"
    assert sm.committed_session_recent("A")[0]["content"] == "A original"
    assert sm.delete_session("A")
    with pytest.raises(LookupError):
        sm.committed_session_recent("A")
    assert sm.read_committed_session_source("B", "B-first", revision=1)["text"] == "B original"
    assert sm.delete_session("B")
    with pytest.raises(LookupError):
        sm.committed_session_rows("B")


class CopyObservedRow(dict):
    deepcopies = 0

    def __deepcopy__(self, memo):
        type(self).deepcopies += 1
        return deepcopy(dict(self), memo)


def test_current_persistence_shares_old_rows_without_deepcopy_or_history_reread(sessions, monkeypatch):
    history = sm.conversation_history
    history.dialog = [CopyObservedRow(role="user", content=f"source {index}",
        turn_id=f"turn-{index}", **sm.new_message_metadata(message_id=f"message-{index}"))
        for index in range(10_000)]
    history.dialog.append({"role":"assistant", "content":"Heard. Unheard tail. [EMO happy]",
        "turn_id":"voice", **sm.new_message_metadata(message_id="voice-reply")})
    assert sm.save_session("A", enable_conversation=True)
    old_rows = tuple(history.dialog)
    committed_rows = sm._committed_sources[1]
    CopyObservedRow.deepcopies = 0
    copied = Mock(wraps=sm.deepcopy)
    parse = Mock(side_effect=AssertionError("unchanged current history was reparsed"))
    monkeypatch.setattr(sm, "deepcopy", copied)
    monkeypatch.setattr(sm.json, "load", parse)
    assert sm.save_session("A", enable_conversation=True)
    assert copied.call_count == 0
    assert all(current is old for current, old in zip(sm._committed_sources[1], committed_rows))
    assert sm.append_session_message("A", role="user", content="Next question", turn_id="next",
        message_id="next-user")
    assert copied.call_count == 1
    assert sm.persist_interrupted_assistant_turn("A", turn_id="voice", heard_content="Heard.")
    assert copied.call_count == 2
    assert sm.persist_interrupted_assistant_turn("A", turn_id="next", heard_content="Delivered prefix.")
    assert copied.call_count == 3
    assert not sm.persist_interrupted_assistant_turn("A", turn_id="next", heard_content="Delivered prefix.")
    assert copied.call_count == 3
    assert CopyObservedRow.deepcopies == 0
    sources = sm._committed_sources[1]
    assert all(current is old for current, old in zip(sources[:10_000], committed_rows))
    assert sources[10_000] is not committed_rows[10_000]
    assert committed_rows[10_000]["content"] == "Heard. Unheard tail. [EMO happy]"
    assert all(source is not active for source, active in zip(sources, history.dialog))
    parse.assert_not_called()
    assert all(current is old for current, old in zip(history.dialog[:10_000], old_rows))
    assert history.dialog[10_000] is not old_rows[10_000]
    assert old_rows[10_000]["content"] == "Heard. Unheard tail. [EMO happy]"
    assert old_rows[10_000]["revision"] == 1
    source = sm.read_committed_session_source("A", "voice-reply", revision=2)
    assert source["text"] == "Heard. [interrupted by user]"
    with pytest.raises(ValueError):
        sm.read_committed_session_source("A", "voice-reply", revision=1)
    assert [row["content"] for row in history.dialog[-2:]] == [
        "Next question", "Delivered prefix. [interrupted by user]"]
    data = json.loads(Path(sm._session_path("A")).read_text(encoding="utf-8"))
    assert data["dialog"] == history.dialog
    assert data["enable_conversation"] is True


def test_public_snapshot_detaches_nested_state_after_serial_persistence(sessions):
    history = sm.conversation_history
    history.add_user("Original", message_id="original")
    history.dialog[0]["nested"] = {"values":["original"]}
    assert sm.save_session("A")
    detached = history.snapshot()
    history.dialog[0]["nested"]["values"].append("active edit")
    history.add_assistant("Later", turn_id="later")
    assert detached.dialog[0]["nested"]["values"] == ["original"]
    detached.dialog[0]["nested"]["values"].append("snapshot edit")
    assert history.dialog[0]["nested"]["values"] == ["original", "active edit"]
    assert len(detached.dialog) == 1


@pytest.mark.parametrize("operation", ["save", "append", "interrupt_existing", "interrupt_new"])
@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_failed_preparation_keeps_active_rows_cache_and_committed_sources(
    sessions, monkeypatch, operation, failure,
):
    assert sm.append_session_message("A", role="user", content="Question", turn_id="reply",
        message_id="question")
    assert sm.append_session_message("A", role="assistant", content="Heard. Unheard tail.",
        turn_id="reply", message_id="reply")
    assert sm.append_session_message("A", role="user", content="Unanswered", turn_id="new",
        message_id="new-question")
    history = sm.conversation_history
    legacy = {"role":"user", "content":"Uncommitted legacy row"}
    history.dialog.append(legacy)
    before = deepcopy(history.dialog)
    active_rows = tuple(history.dialog)
    file_state, sources, recent = history._file_state, sm._committed_sources, sm._committed_recent["A"]
    path = Path(sm._session_path("A"))
    persisted = path.read_bytes()
    if failure == "serialize":
        monkeypatch.setattr(sm.json, "dumps", Mock(side_effect=TypeError("synthetic serialization failure")))
    else:
        monkeypatch.setattr(sm.os, "replace", Mock(side_effect=OSError("synthetic write failure")))
    if operation == "save":
        result = sm.save_session("A", enable_conversation=True)
    elif operation == "append":
        result = sm.append_session_message("A", role="assistant", content="Pending publication",
            turn_id="new", message_id="pending")
    else:
        result = sm.persist_interrupted_assistant_turn("A",
            turn_id="reply" if operation == "interrupt_existing" else "new", heard_content="Heard.")
    assert result is False
    assert history.dialog == before
    assert all(current is old for current, old in zip(history.dialog, active_rows))
    assert history.dialog[-1] is legacy and "message_id" not in legacy
    assert history._file_state is file_state
    assert sm._committed_sources is sources
    assert sm._committed_recent["A"] is recent
    assert path.read_bytes() == persisted
    assert sm.read_committed_session_source("A", "reply", revision=1)["text"] == "Heard. Unheard tail."
    with pytest.raises(ValueError):
        sm.read_committed_session_source("A", "reply", revision=2)
    with pytest.raises(LookupError):
        sm.read_committed_session_source("A", "pending", revision=1)


@pytest.mark.parametrize("operation", ["save", "append", "interrupt"])
def test_external_header_edit_is_revalidated_once_and_preserved(sessions, monkeypatch, operation):
    assert sm.append_session_message("A", role="user", content="Question", turn_id="reply")
    path = Path(sm._session_path("A"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(title="Externally renamed", enable_conversation=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    parse = Mock(wraps=json.load)
    monkeypatch.setattr(sm.json, "load", parse)
    if operation == "save":
        assert sm.save_session("A", enable_conversation=True)
    elif operation == "append":
        assert sm.append_session_message("A", role="user", content="Next", turn_id="next")
    else:
        assert sm.persist_interrupted_assistant_turn("A", turn_id="reply", heard_content="Heard.")
    assert parse.call_count == 1
    observed = json.loads(path.read_text(encoding="utf-8"))
    assert observed["title"] == "Externally renamed"
    assert observed["enable_conversation"] is True
    assert sm.save_session("A", enable_conversation=True)
    assert parse.call_count == 1


@pytest.mark.parametrize("operation", ["save", "append", "interrupt"])
def test_external_file_replacement_with_same_size_and_mtime_rechecks_owner(sessions, monkeypatch, operation):
    assert sm.append_session_message("A", role="user", content="Question", turn_id="reply")
    path = Path(sm._session_path("A"))
    signature = path.stat()
    original = path.read_text(encoding="utf-8")
    changed = original.replace('"kurisu"', '"otherx"')
    assert len(changed.encode("utf-8")) == len(original.encode("utf-8"))
    replacement = path.with_suffix(".replacement")
    replacement.write_text(changed, encoding="utf-8")
    sm.os.utime(replacement, ns=(signature.st_atime_ns, signature.st_mtime_ns))
    sm.os.replace(replacement, path)
    rows, file_state, sources = deepcopy(sm.conversation_history.dialog), sm.conversation_history._file_state, sm._committed_sources
    if operation == "save":
        result = sm.save_session("A")
    elif operation == "append":
        result = sm.append_session_message("A", role="user", content="Next", turn_id="next")
    else:
        result = sm.persist_interrupted_assistant_turn("A", turn_id="reply", heard_content="Heard.")
    assert result is False
    assert path.read_text(encoding="utf-8") == changed
    assert sm.conversation_history.dialog == rows
    assert sm.conversation_history._file_state is file_state
    assert sm._committed_sources is sources


@pytest.mark.parametrize("operation", ["save", "append", "interrupt"])
def test_post_commit_stat_failure_keeps_success_and_publishes_sources(sessions, monkeypatch, operation):
    assert sm.append_session_message("A", role="user", content="Question", turn_id="reply",
        message_id="question")
    history = sm.conversation_history
    if operation == "save":
        history.add_assistant("Saved", turn_id="reply", message_id="reply")
    signature = sm._session_file_signature
    committed = False
    write = sm.write_text

    def commit(path, content):
        nonlocal committed
        write(path, content)
        committed = True

    def post_commit_stat(path):
        if committed:
            raise OSError("synthetic post-commit stat failure")
        return signature(path)

    monkeypatch.setattr(sm, "write_text", commit)
    monkeypatch.setattr(sm, "_session_file_signature", post_commit_stat)
    if operation == "save":
        result = sm.save_session("A")
        message_id, content = "reply", "Saved"
    elif operation == "append":
        result = sm.append_session_message("A", role="assistant", content="Appended", turn_id="reply",
            message_id="reply")
        message_id, content = "reply", "Appended"
    else:
        result = sm.persist_interrupted_assistant_turn("A", turn_id="reply", heard_content="Heard.")
        message_id, content = sm.assistant_turn_message_id("reply"), "Heard. [interrupted by user]"
    assert result is True and committed
    assert history._file_state is None
    assert history.dialog[-1]["content"] == content
    assert sm.read_committed_session_source("A", message_id, revision=1)["text"] == content
    path = Path(sm._session_path("A"))
    assert json.loads(path.read_text(encoding="utf-8"))["dialog"] == history.dialog
    monkeypatch.setattr(sm, "_session_file_signature", signature)
    assert sm.save_session("A")
    assert history._file_state is not None



def test_changed_committed_rows_detach_only_after_successful_save(sessions, monkeypatch):
    history = sm.conversation_history
    history.add_user("Original", message_id="original")
    history.dialog[0]["nested"] = {"values":["original"]}
    history.add_user("Unchanged", message_id="unchanged")
    assert sm.save_session("A")
    previous = sm._committed_sources
    original, unchanged = previous[1]
    assert original["nested"] is not history.dialog[0]["nested"]
    history.dialog[0].update(content="Replacement", revision=2)
    history.dialog[0]["nested"]["values"].append("pending edit")
    history.add_user("New", message_id="new")
    history.dialog[-1]["nested"] = {"values":["new"]}
    assert sm.read_committed_session_source("A", "original", revision=1)["text"] == "Original"
    assert original["nested"]["values"] == ["original"]
    write = sm.write_text
    copied = Mock(wraps=sm.deepcopy)
    monkeypatch.setattr(sm, "deepcopy", copied)
    monkeypatch.setattr(sm, "write_text", Mock(side_effect=OSError("synthetic write failure")))
    assert not sm.save_session("A")
    assert copied.call_count == 0
    assert sm._committed_sources is previous
    assert original["content"] == "Original" and original["revision"] == 1
    assert original["nested"]["values"] == ["original"]
    monkeypatch.setattr(sm, "write_text", write)
    assert sm.save_session("A")
    assert copied.call_count == 2
    replacement, reused, new = sm._committed_sources[1]
    assert replacement is not original and replacement is not history.dialog[0]
    assert reused is unchanged
    assert new is not history.dialog[-1]
    assert replacement["nested"]["values"] == ["original", "pending edit"]
    assert sm.read_committed_session_source("A", "original", revision=2)["text"] == "Replacement"
    with pytest.raises(ValueError):
        sm.read_committed_session_source("A", "original", revision=1)
    history.dialog[0]["nested"]["values"].append("later edit")
    history.dialog[-1]["nested"]["values"].append("later new edit")
    assert replacement["nested"]["values"] == ["original", "pending edit"]
    assert new["nested"]["values"] == ["new"]
    assert original["content"] == "Original" and original["nested"]["values"] == ["original"]
