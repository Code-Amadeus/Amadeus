"""Real Session-file interleavings at the Chat admission boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core import session_manager as sm
from core.turn_coordinator import TurnCoordinator
from server.handlers.chat_handler import ChatHandler
from server.turn_admission import capture_turn_admission
from server.turn_decision_shadow import TurnDecisionShadowObserver


@pytest.fixture
def session_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm.conversation_history, "dialog", [])
    for sid, marker in (("origin-A", "ONLY_A"), ("active-B", "ONLY_B")):
        sm.create_session(sid)
        sm.conversation_history.add_user(marker)
        sm.save_session(sid, enable_conversation=True)
    assert sm.load_session("origin-A")[0]

    def read(sid):
        return json.loads(Path(sm._session_path(sid)).read_text(encoding="utf-8"))

    return read


def test_chat_session_switch_stops_when_previous_history_cannot_be_saved(session_files):
    async def run():
        handler = ChatHandler()
        model = AsyncMock()
        handler.configure(stream_llm_query=model, pending_sentence_items=None)
        before = sm.conversation_history.snapshot()
        with (
            patch.object(sm, "save_session", return_value=False),
            patch.object(handler, "_open_turn", side_effect=AssertionError("no new admission")) as admission,
        ):
            with pytest.raises(RuntimeError, match="save the active Session"):
                await handler._handle_send({"text": "hello", "turn_id": "new", "session_id": "active-B"})
        admission.assert_not_called()
        model.assert_not_called()
        assert sm.get_current_session_id() == "origin-A"
        assert sm.conversation_history.dialog == before.dialog

    asyncio.run(run())


def _admission(**kwargs):
    return capture_turn_admission(**{
        "utterance_id": "source-utterance", "turn_id": "source-turn",
        "session_id": "origin-A", "transcript": "hello", "chat_epoch": 7,
        **kwargs,
    })


def test_capture_and_observation_keep_time_alias_and_epoch_distinct():
    evidence = {"n_best_hashes": ["first"], "asr_confidence": 0.7, "raw_audio": "omit"}
    with (
        patch("server.turn_admission.time.time", return_value=100.0),
        patch("server.turn_admission.time.monotonic", return_value=10.0),
    ):
        first = _admission(source_evidence=evidence)
    evidence["n_best_hashes"].append("later mutation")
    assert first.source_evidence == {"n_best_hashes": ["first"], "asr_confidence": 0.7}
    observer = TurnDecisionShadowObserver(enabled=True)
    with patch("server.turn_admission.time.time", return_value=200.0):
        observer.observe_admission(first)
    retry = _admission(turn_id="retry-turn", chat_epoch=8)
    canonical = observer.observe_admission(retry)
    assert retry.root_id == first.root_id == canonical.root_id
    assert (retry.turn_id, retry.chat_epoch) == ("retry-turn", 8)
    assert (canonical.turn_id, canonical.chat_epoch) == ("source-turn", 7)
    row = observer.snapshot()["recent"][0]
    assert row["admission"]["admitted_at"] == 100.0
    assert row["admission"]["admitted_at_monotonic"] == 10.0
    assert row["events"][0]["arrived_at"] == 200.0
    assert observer.admission_for_turn("retry-turn").root_id == first.root_id
    assert _admission(utterance_id="repeat-words").root_id != first.root_id


def test_disabled_shadow_does_not_remove_host_capture():
    with patch("server.turn_decision_shadow.get_enabled_turn_decision_shadow_observer", return_value=None):
        captured = ChatHandler._capture_turn_admission(
            utterance_id="voice-u", turn_id="voice-t", session_id="origin-A",
            text="yes", source="wake", chat_epoch=3, pending=True,
            source_evidence={"tts_overlap": True},
            utterance_identity_source="explicit_utterance_id",
        )
        ChatHandler._observe_turn_admission(captured)
    assert captured is not None and captured.chat_epoch == 3 and captured.pending
    assert captured.source_evidence == {
        "tts_overlap": True, "utterance_identity_source": "explicit_utterance_id",
    }
    assert captured.authority_mode == "source_witness_v1"
    assert capture_turn_admission(utterance_id="", turn_id="", session_id="", transcript="host note") is None


def test_history_snapshot_is_deep_and_save_cannot_retarget_loaded_history(session_files):
    before_a = session_files("origin-A")
    sm.conversation_history.dialog[0]["nested"] = {"values": ["original"]}
    snapshot = sm.conversation_history.snapshot()
    sm.conversation_history.dialog[0]["nested"]["values"].append("changed")
    assert snapshot.dialog[0]["nested"]["values"] == ["original"]
    assert sm.load_session("active-B")[0]
    sm.conversation_history.add_user("new B message")
    assert sm.save_session("origin-A", enable_conversation=True) is False
    assert session_files("origin-A") == before_a
    assert sm.save_session("active-B", enable_conversation=True) is True
    assert session_files("active-B")["dialog"][-1]["content"] == "new B message"




@pytest.mark.parametrize("target", ["active-B", "new-C"])
def test_explicit_session_selection_loads_history_after_old_turn_interrupt(session_files, target):
    async def run():
        captured = []
        interrupted = []

        async def stream(_text, **kwargs):
            captured.append(kwargs)
            return ""

        async def interrupt():
            interrupted.append((sm.get_current_session_id(), sm.conversation_history.snapshot().dialog))

        handler = ChatHandler()
        handler.configure(stream_llm_query=stream, pending_sentence_items=None)
        handler._active_turn_id = "old-turn"
        handler._interrupt_superseded_turn = interrupt
        with (
            patch("core.turn_coordinator.get_turn_coordinator", return_value=TurnCoordinator()),
            patch("server.handlers.chat_handler.bus.emit", new=AsyncMock()),
            patch.object(handler, "_prepare_visual_context", new=AsyncMock(return_value=None)),
        ):
            await handler._handle_send({"text": "hello", "turn_id": "new-turn", "session_id": target})
            await handler._stream_task
        assert interrupted == [("origin-A", [{"role": "user", "content": "ONLY_A"}])]
        assert captured[0]["turn_admission"].session_id == target
        expected = [{"role": "user", "content": "ONLY_B"}] if target == "active-B" else []
        assert captured[0]["history_snapshot"].dialog == expected
        assert sm.get_current_session_id() == target
        assert session_files("origin-A")["dialog"] == [{"role": "user", "content": "ONLY_A"}]

    asyncio.run(run())
