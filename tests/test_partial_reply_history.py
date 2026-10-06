"""Delivered partial replies retain exact Session, turn and branch ownership."""
import asyncio
import ast
from contextlib import asynccontextmanager
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core import session_manager as sm
import core.chat_history_projection as hp
import core.turn_coordinator as tc
from server.cooperative_context_store import CooperativeContextStore
from server.control_ledger import ControlLedgerStore
from server.handlers.chat_handler import ChatHandler
from server.protocol import Method
from test_chat_role_delivery import streaming_role as streaming_role, role_loop


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    history = sm.ConversationHistory()
    for module in (sm, hp):
        monkeypatch.setattr(module, "conversation_history", history)
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr(ChatHandler, "_turn_allows_visible_emit", AsyncMock(return_value=True))
    sm.create_session("A")


def assistant_rows(sid="A"):
    history, _ = sm._read_session_history(sid)
    return [row for row in history.dialog if row["role"] == "assistant"]


@asynccontextmanager
async def durable_loop(host, query, tmp_path):
    host.delivery.record_display = sm.append_session_message
    loop = role_loop(query, host.delivery)
    ledger = ControlLedgerStore(tmp_path / "host.sqlite3")
    loop.attach_state(CooperativeContextStore(ledger, "A"))
    try:
        yield loop
    finally:
        await loop.close()
        ledger.close()


@pytest.mark.parametrize("failure", [False, True], ids=["normal_publish", "stream_failure"])
async def test_visible_stream_retained_once(streaming_role, tmp_path, failure):
    host = streaming_role
    raw = '{"action":null,"say":"Visible reply."}'

    async def query(messages, *, on_text=None):
        await on_text(raw)
        assert host.emitted and host.queue.qsize() > 0
        if failure:
            raise RuntimeError("synthetic transport failure")
        return raw

    async with durable_loop(host, query, tmp_path) as loop:
        if failure:
            with pytest.raises(Exception, match="synthetic transport failure"):
                await loop.submit("hello", turn_id="reply")
        else:
            await loop.submit("hello", turn_id="reply")
    rows = assistant_rows()
    assert [(r["turn_id"], r["content"]) for r in rows] == [("reply", "Visible reply.")]


async def test_cancel_before_first_delivery_leaves_no_assistant(streaming_role, tmp_path):
    host = streaming_role
    waiting = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        async def acquire(self):
            waiting.set()
            return await super().acquire()

    async def query(messages, *, on_text=None):
        await on_text('{"action":null,"say":"Never delivered."}')
        raise AssertionError("cancelled query resumed")

    async with durable_loop(host, query, tmp_path) as loop:
        lock = ObservedLock()
        await lock.acquire()
        waiting.clear()
        loop._delivery_lock = lock
        task = asyncio.create_task(loop.submit("hello", input_id="input", turn_id="unseen"))
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            loop._inputs["input"][1].cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            lock.release()
        assert host.emitted == [] and host.queue.empty()
    assert assistant_rows() == [], "No text reached UI or audio; decoder output is not delivery evidence"


@pytest.mark.parametrize("origin_failure", ["none", "corrupt_json", "write_denied"])
async def test_stream_failure_after_switch_never_mutates_other_session(
    streaming_role, tmp_path, monkeypatch, origin_failure,
):
    host = streaming_role
    before = None

    async def query(messages, *, on_text=None):
        nonlocal before
        await on_text('{"action":null,"say":"Only for A."}')
        assert host.emitted
        sm.create_session("B")
        sm.conversation_history.add_user("Only B user")
        sm.save_session("B")
        before = deepcopy(sm.conversation_history.dialog)
        if origin_failure == "corrupt_json":
            Path(sm._session_path("A")).write_text("invalid JSON", encoding="utf-8")
        if origin_failure == "write_denied":
            original_replace = sm.os.replace
            origin = Path(sm._session_path("A")).resolve()

            def deny_origin_write(source, destination):
                if Path(destination).resolve() == origin:
                    raise PermissionError("synthetic write denial for A only")
                return original_replace(source, destination)

            monkeypatch.setattr(sm.os, "replace", deny_origin_write)
        raise RuntimeError("synthetic transport failure after session switch")

    async with durable_loop(host, query, tmp_path) as loop:
        with pytest.raises(Exception, match="synthetic transport failure"):
            await loop.submit("hello", turn_id="a-reply")
    assert sm.get_current_session_id() == "B"
    assert sm.save_session("B")
    observed = {"loaded_B": sm.conversation_history.dialog, "persisted_B_assistants": assistant_rows("B")}
    assert observed == {"loaded_B": before, "persisted_B_assistants": []}, "A's failure must not contaminate B"
    if origin_failure == "none":
        assert [row["content"] for row in assistant_rows("A")] == ["Only for A."]


def interrupt_callback(monkeypatch):
    # Same AST-extraction technique as the repository's production_query tests.
    # Execute the unchanged nested callback without starting the server/audio.
    import server.character_presentation as presentation
    monkeypatch.setattr(presentation, "playback_bridge", SimpleNamespace(release_all=Mock()))
    scope = {"logger": logging.getLogger("partial-reply-history"), "_expr_ctrl": Mock(),
             "_render_signal_bridge": Mock(), "bus": SimpleNamespace(emit=AsyncMock(),
             subscriber_count=Mock(return_value=0)), "Method": Method}
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definition = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_tts_interrupt")
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), scope)
    return scope["_handle_tts_interrupt"]


async def test_barge_in_preserves_prior_turn_and_adds_current(monkeypatch):
    sm.append_session_message("A", role="assistant", content="Prior reply", turn_id="prior")
    sm.append_session_message("A", role="user", content="Next question", turn_id="current")
    callback = interrupt_callback(monkeypatch)
    payload = {"session_id": "A", "turn_id": "current", "completed_text": "Heard prefix.",
               "accumulated_text": "Heard prefix. Unheard tail.", "source": "review"}
    await callback(payload)
    await callback(payload)
    assert [(r["turn_id"], r["content"]) for r in assistant_rows()] == [
        ("prior", "Prior reply"), ("current", "Heard prefix. [interrupted by user]")]


@pytest.mark.parametrize("interrupt_first", [False, True])
async def test_partial_cleanup_never_restores_interrupted_tail(streaming_role, interrupt_first):
    host = streaming_role
    host.delivery.record_display = sm.append_session_message
    # Cooperative ingress records the accepted user row before role output.
    sm.append_session_message("A", role="user", content="Question", turn_id="interrupted")
    stream = host.delivery.begin_stream("interrupted")
    await stream.feed("Heard prefix. Unheard tail.")

    def interrupt():
        assert sm.persist_interrupted_assistant_turn("A", turn_id="interrupted",
            heard_content="Heard prefix.")

    if interrupt_first:
        interrupt()
    stream.abort()
    stream.abort()  # _check and the turn's finally may both clean up.
    if not interrupt_first:
        interrupt()
    assert [(row["turn_id"], row["content"]) for row in assistant_rows()] == [
        ("interrupted", "Heard prefix. [interrupted by user]")]


@pytest.mark.parametrize("accepted", [True, False])
async def test_gui_receipt_survives_cancel_during_sentence_dispatch(streaming_role, monkeypatch, accepted):
    host = streaming_role
    host.delivery.record_display = sm.append_session_message
    callback = Mock(return_value=accepted)
    stream = host.delivery.begin_stream("gui-interrupt", gui_callback=callback)
    monkeypatch.setattr(host.runtime, "_append_and_dispatch",
        AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await stream.feed("Shown before audio dispatch.")
    stream.abort()
    callback.assert_called_once_with("Shown before audio dispatch.")
    assert [row["content"] for row in assistant_rows()] == (
        ["Shown before audio dispatch."] if accepted else [])


async def test_final_publication_rejected_retains_only_prior_partial(streaming_role, monkeypatch):
    host = streaming_role
    host.delivery.record_display = sm.append_session_message
    stream = host.delivery.begin_stream("final-rejected")
    await stream.feed("Visible prefix.")
    monkeypatch.setattr(host.delivery, "display", AsyncMock(return_value=False))
    assert await stream.finish({"cause":"final-rejected", "text":"Undelivered final."}) is False
    stream.abort()
    assert [row["content"] for row in assistant_rows()] == ["Visible prefix."]


async def test_rejected_partial_display_does_not_create_history(streaming_role, monkeypatch):
    host = streaming_role
    host.delivery.record_display = sm.append_session_message
    monkeypatch.setattr(host.delivery, "partial_display", AsyncMock(return_value=False))
    stream = host.delivery.begin_stream("partial-rejected")
    await stream.feed("A buffered fragment")
    stream.abort()
    assert assistant_rows() == []


async def test_aborted_json_stream_persists_decoded_say_not_envelope(streaming_role, tmp_path):
    import json

    host = streaming_role
    text = 'Visible "quote" and C:\\sample\\file.'
    raw = json.dumps({"action":None, "say":text})

    async def query(messages, *, on_text=None):
        # Decode every escape across transport boundaries; never finish the JSON.
        for character in raw[:-2]:
            await on_text(character)
        raise RuntimeError("synthetic truncated JSON stream")

    async with durable_loop(host, query, tmp_path) as loop:
        with pytest.raises(Exception, match="synthetic truncated JSON"):
            await loop.submit("hello", turn_id="escaped")
    assert [row["content"] for row in assistant_rows()] == [text]


async def test_tts_interrupt_retains_origin_when_playback_drain_switches_session(monkeypatch):
    from server.handlers.tts_handler import TtsHandler

    sm.append_session_message("A", role="assistant", content="Full original reply.", turn_id="voice")

    async def drain():
        sm.create_session("B")
        sm.conversation_history.add_user("Only B")
        assert sm.save_session("B")

    playback = SimpleNamespace(get_completed_turn_text=lambda:"Heard prefix.",
        interrupt=drain, clear_turn_tracking=Mock())
    handler = TtsHandler(sentence_sequence_manager=Mock())
    handler.configure(playback, Mock(), on_interrupt=interrupt_callback(monkeypatch))
    monkeypatch.setattr("tts.pipeline.interrupt_pending_tts", Mock())
    await handler.handle(Method.TTS_INTERRUPT, {"annotate_history":True,
        "turn_id":"voice", "accumulated_text":"Full original reply.", "session_id":"untrusted"})
    assert sm.get_current_session_id() == "B"
    assert sm.conversation_history.dialog == [{"role":"user", "content":"Only B"}]
    assert assistant_rows("B") == []
    assert assistant_rows("A")[0]["content"] == "Heard prefix. [interrupted by user]"
