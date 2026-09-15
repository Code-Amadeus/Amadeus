"""Host Chat epoch failure propagation; no live model/Provider calls."""

import asyncio
import ast
from pathlib import Path
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core import session_manager as sm
import core.turn_coordinator as tc
from core.turn_coordinator import TurnAuthorityError
from server.handlers.chat_handler import ChatHandler
from server.interrupt_flow import MainTurnInterruptFlow
from server.speculative_turn import SpeculativeTurnLauncher


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    coordinator = tc.TurnCoordinator()
    monkeypatch.setattr(tc, "coordinator", coordinator)
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    return coordinator


@pytest.mark.parametrize("operation", ["open", "advance"])
def test_core_owner_failure_does_not_return_a_grant(context, monkeypatch, operation):
    before = context.snapshot()
    monkeypatch.setattr(context, "_issue_epoch", Mock(side_effect=RuntimeError("owner failed")))
    with pytest.raises(TurnAuthorityError):
        if operation == "open":
            context.open_turn(turn_id="new", local_next_epoch=1, pending=True)
        else:
            context.advance_chat_epoch(local_next=1)
    assert context.snapshot()["epochs"] == before["epochs"]
    assert context.snapshot()["active_turn_id"] == before["active_turn_id"]


@pytest.mark.parametrize("operation", ["open", "advance"])
def test_missing_owner_does_not_become_handler_local_authority(context, monkeypatch, operation):
    handler = ChatHandler()
    handler._chat_epoch = 4
    monkeypatch.setattr(tc, "get_turn_coordinator", Mock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(TurnAuthorityError):
        if operation == "open":
            handler._open_turn(turn_id="new", session_id="", source="test", pending=True)
        else:
            handler._advance_chat_epoch()
    assert handler._chat_epoch == 4


@pytest.mark.parametrize("pending", [False, True])
def test_failed_grant_never_starts_a_model(context, monkeypatch, pending):
    async def run():
        handler = ChatHandler()
        model = AsyncMock()
        handler.configure(stream_llm_query=model, pending_sentence_items=None)
        monkeypatch.setattr(context, "_issue_epoch", Mock(side_effect=RuntimeError("owner failed")))
        with pytest.raises(TurnAuthorityError):
            await handler._handle_send({"text": "input", "turn_id": "new", "pending": pending})
        assert handler._stream_task is None
        assert handler._active_turn_id == ""
        assert handler._chat_epoch == 0
        model.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["resolve", "stale", "abandon", "launch"])
def test_speculative_discard_failure_does_not_erase_slot_or_start_replacement(context, monkeypatch, operation):
    async def run():
        context.open_turn(turn_id="old", local_next_epoch=1, pending=True)
        handler = ChatHandler()
        handler._active_turn_id = "old"
        handler._chat_epoch = 1
        handler._stream_task = Mock()
        handler._stream_task.done.return_value = False
        handler._stream_task.cancelling.return_value = 0
        launcher = SpeculativeTurnLauncher()
        send = AsyncMock()
        launcher.configure(send_pending=send, confirm=handler.confirm_pending_turn, discard=handler.discard_pending_turn)
        launcher._slot_turn_id = "old"
        launcher._slot_text = "original"
        launcher._slot_at = 0 if operation == "stale" else time.monotonic()
        monkeypatch.setattr(launcher, "_policy_blocked_reason", lambda _: "")
        monkeypatch.setattr(context, "_issue_epoch", Mock(side_effect=RuntimeError("owner failed")))
        if operation == "launch":
            assert await launcher.launch("replacement") is False
        else:
            with pytest.raises(TurnAuthorityError):
                if operation in {"resolve", "stale"}:
                    await launcher.resolve("correction")
                else:
                    await launcher.abandon("test")
        assert launcher._slot_turn_id == "old"
        send.assert_not_called()
        handler._stream_task.cancel.assert_called_once()
    asyncio.run(run())


def test_abort_failure_stops_local_generation_but_does_not_acknowledge_fencing(context, monkeypatch):
    async def run():
        handler = ChatHandler()
        handler._active_turn_id = "old"
        handler._stream_task = Mock()
        handler._stream_task.done.return_value = False
        handler._stream_task.cancelling.return_value = 0
        monkeypatch.setattr(context, "_issue_epoch", Mock(side_effect=RuntimeError("owner failed")))
        with pytest.raises(TurnAuthorityError):
            await handler._handle_abort({})
        handler._stream_task.cancel.assert_called_once()
        assert handler._active_turn_id == ""
        assert handler._chat_epoch == 0
    asyncio.run(run())


@pytest.mark.parametrize("tts_fails", [False, True])
def test_failed_supersede_cleans_audio_then_stops_without_retry_or_new_turn(context, monkeypatch, tts_fails):
    async def run():
        handler = ChatHandler()
        model = AsyncMock()
        handler.configure(stream_llm_query=model, pending_sentence_items=None)
        handler._active_turn_id = "old"
        old_task = Mock()
        old_task.done.return_value = False
        old_task.cancelling.return_value = 0
        handler._stream_task = old_task
        issue = Mock(side_effect=[RuntimeError("one failure"), 2])
        monkeypatch.setattr(context, "_issue_epoch", issue)
        tts = Mock()
        tts.handle = AsyncMock(return_value={"status": "interrupted"})
        if tts_fails:
            tts.handle.side_effect = RuntimeError("audio cleanup also failed")
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=handler, tts_handler=tts)
        with patch("server.interrupt_flow.get_interrupt_flow", return_value=flow):
            with pytest.raises(TurnAuthorityError):
                await handler._handle_send({"text": "new input", "turn_id": "new"})
        issue.assert_called_once()
        old_task.cancel.assert_called_once()
        tts.handle.assert_awaited_once()
        assert handler._stream_task is old_task
        assert context.snapshot()["recent_transitions"][-1]["event"] == "interrupt_end"
        model.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["confirm", "discard"])
def test_pending_authority_failure_is_not_a_normal_noop(context, monkeypatch, operation):
    async def run():
        handler = ChatHandler()
        failure = TurnAuthorityError("decision owner failed")
        monkeypatch.setattr(context, operation + "_turn", Mock(side_effect=failure))
        with pytest.raises(TurnAuthorityError) as error:
            await getattr(handler, operation + "_pending_turn")("pending")
        assert error.value is failure
    asyncio.run(run())


def test_failed_pending_decision_still_cleans_its_active_local_stream(context, monkeypatch):
    async def run():
        handler = ChatHandler()
        handler._active_turn_id = "pending"
        handler._stream_task = Mock()
        handler._stream_task.done.return_value = False
        handler._stream_task.cancelling.return_value = 0
        monkeypatch.setattr(context, "discard_turn", Mock(side_effect=TurnAuthorityError("refused")))
        advance = Mock()
        monkeypatch.setattr(handler, "_advance_chat_epoch", advance)
        with pytest.raises(TurnAuthorityError):
            await handler.discard_pending_turn("pending")
        handler._stream_task.cancel.assert_called_once()
        assert handler._active_turn_id == ""
        assert handler._chat_epoch == 0
        advance.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["resolve", "abandon"])
def test_delayed_retirement_cannot_clear_a_newer_speculative_slot(monkeypatch, operation):
    async def run():
        launcher = SpeculativeTurnLauncher()
        started = asyncio.Event()
        released = asyncio.Event()
        calls = 0

        async def discard(turn_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await released.wait()
            return True

        launcher.configure(
            send_pending=AsyncMock(side_effect=lambda text, **kw: {"status": "ok", "turn_id": kw["turn_id"]}),
            confirm=AsyncMock(), discard=discard,
        )
        monkeypatch.setattr(launcher, "_policy_blocked_reason", lambda _: "")
        launcher._slot_turn_id = "old"
        launcher._slot_text = "original"
        launcher._slot_at = time.monotonic()
        retiring = asyncio.create_task(launcher.resolve("correction") if operation == "resolve" else launcher.abandon())
        await asyncio.wait_for(started.wait(), 1)
        assert await launcher.launch("replacement")
        replacement = launcher._slot_turn_id
        assert replacement and replacement != "old"
        released.set()
        await retiring
        assert launcher._slot_turn_id == replacement
        assert launcher._slot_text == "replacement"
    asyncio.run(run())


@pytest.mark.parametrize("resolution", ["failure", "confirmed", "noop"])
def test_actual_wake_callback_does_not_resend_after_authority_failure(monkeypatch, resolution):
    # Execute the exact nested callback from app.py, supplying its closure
    # dependencies without starting servers, microphones or Provider adapters.
    # This is source-backed callback integration, not a full bootstrap/ASR test.
    path = Path(__file__).resolve().parents[1] / "server" / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bootstrap = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap")
    callback = next(node for node in bootstrap.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_wake_text")
    module = ast.Module(body=[callback], type_ignores=[])
    launcher = Mock()
    launcher.resolve = AsyncMock(return_value=resolution == "confirmed")
    if resolution == "failure":
        launcher.resolve.side_effect = TurnAuthorityError("cannot retire pending input")
    monkeypatch.setattr("server.speculative_turn.get_speculative_launcher", lambda: launcher)
    monkeypatch.setattr("asr.text_filter.is_asr_prompt_leak", lambda *args, **kwargs: False)
    chat = Mock()
    chat.send_text = AsyncMock()
    session_factory = Mock(return_value="voice")
    namespace = {
        "WAKE_AUTO_SEND_TO_CHAT": True, "_main_voice_allowed_now": AsyncMock(return_value=True),
        "logger": Mock(), "protected_text": lambda value: value, "LLM_PROVIDER": "local",
        "_current_or_create_session_id": session_factory, "chat_h": chat,
    }
    exec(compile(module, str(path), "exec"), namespace)
    if resolution == "failure":
        with pytest.raises(TurnAuthorityError):
            asyncio.run(namespace["_send_wake_text"]("final input"))
    else:
        asyncio.run(namespace["_send_wake_text"]("final input"))
    assert chat.send_text.await_count == (1 if resolution == "noop" else 0)
    assert session_factory.call_count == (1 if resolution == "noop" else 0)
