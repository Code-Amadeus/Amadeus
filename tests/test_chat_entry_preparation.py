"""One utterance may overlap read-only entry preparation with ASR, never authority."""
import ast
import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.cooperative_chat_ingress import (
    CooperativeChatManager,
    _prepared_auip_entry,
    prepared_auip_entry_scope,
)


def entry_manager(context):
    manager = CooperativeChatManager.__new__(CooperativeChatManager)
    manager.auip_entry_context = context
    manager.auip_decider = SimpleNamespace(capture=Mock(return_value=None))
    manager.auip_router = Mock()
    manager.work_planner = None
    return manager


async def entry_prompt(manager, session="A"):
    loop = SimpleNamespace(_binding=None, prior_messages=lambda _:[], _monitors=set(), trace=[])
    result = await manager.handle_auip_action(
        SimpleNamespace(session_id=session, loop=loop), "turn", "hello", None, lambda:None)
    entry = result["entry"]
    entry["pending"].cancel()
    await asyncio.gather(entry["pending"], return_exceptions=True)
    return entry["prompt"]


async def test_voice_reuses_complete_information_without_repeating_query():
    context = Mock(return_value="[applications] Board, Clock [/applications]")
    manager = entry_manager(context)
    prepared = manager.prefetch_auip_entry_context("A")
    # Recognition may continue while the context is already available.
    await prepared[2]
    manager.auip_decider.capture.assert_not_called()
    manager.auip_router.assert_not_called()
    with prepared_auip_entry_scope(prepared):
        prompt = await asyncio.create_task(entry_prompt(manager))
    assert prompt == context.return_value
    context.assert_called_once_with("A")
    # A later typed turn has no inherited preparation and gets fresh information.
    context.return_value = "[applications] New Board [/applications]"
    assert await entry_prompt(manager) == context.return_value
    assert context.call_count == 2


@pytest.mark.parametrize("prefetch", [False, True])
async def test_information_is_preserved_while_preparation_is_pending(prefetch):
    started, finish = threading.Event(), threading.Event()

    def context(session):
        started.set()
        assert finish.wait(3)
        return "complete information for " + session

    manager = entry_manager(context)
    prepared = manager.prefetch_auip_entry_context("A") if prefetch else None
    with prepared_auip_entry_scope(prepared):
        pending = asyncio.create_task(entry_prompt(manager))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert not pending.done()
        finish.set()
        assert await pending == "complete information for A"
    finally:
        finish.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_switch_session_or_manager_cannot_reuse_another_preparation():
    context = Mock(side_effect=lambda session:"information for " + session)
    manager = entry_manager(context)
    prepared = manager.prefetch_auip_entry_context("A")
    await prepared[2]
    with prepared_auip_entry_scope(prepared):
        assert await entry_prompt(manager, "B") == "information for B"
        other = entry_manager(Mock(return_value="other host"))
        assert await entry_prompt(other) == "other host"
    assert context.call_args_list == [(("A",),), (("B",),)]
    assert manager.prefetch_auip_entry_context("") is None


async def test_cancelled_speculation_does_not_cancel_final_turn_preparation():
    started, finish = threading.Event(), threading.Event()

    def context(_session):
        started.set()
        assert finish.wait(3)
        return "applications"

    manager = entry_manager(context)
    prepared = manager.prefetch_auip_entry_context("A")
    with prepared_auip_entry_scope(prepared):
        pending = asyncio.create_task(entry_prompt(manager))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        assert not prepared[2].cancelled()
        finish.set()
        with prepared_auip_entry_scope(prepared):
            assert await entry_prompt(manager) == "applications"
    finally:
        finish.set()
        await prepared[2]


def production_callbacks(namespace):
    module = ast.parse((Path(__file__).resolve().parents[1] / "server/app.py").read_text(encoding="utf-8"))
    bootstrap = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "bootstrap")
    names = {"_prepare_asr_entry", "_on_asr_speech_start", "_launch_prepared_speculative_text",
        "_handle_asr_recognized"}
    factory = ast.parse("def callbacks():\n    asr_entry_preparation = None\n").body[0]
    factory.body.extend(node for node in bootstrap.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names)
    factory.body.append(ast.parse("return locals()").body[0])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
        "<production-asr-entry-callbacks>", "exec"), namespace)
    return namespace["callbacks"]()


@pytest.mark.parametrize("source,active,session,auto_send,expected", [
    ("wake", True, "A", True, True),
    ("wake", True, "", True, False),
    ("wake", False, "A", True, False),
    ("vn_player", True, "A", True, False),
    ("", True, "A", True, False),
    ("wake", True, "A", False, False),
])
async def test_production_asr_callbacks_scope_only_the_recognized_utterance(
        monkeypatch, source, active, session, auto_send, expected):
    manager = entry_manager(Mock(return_value="applications"))
    observed = []

    async def send(*_args, **_kwargs):
        async def turn():
            observed.append(_prepared_auip_entry.get())
        await asyncio.create_task(turn())

    monkeypatch.setattr("core.session_manager.get_current_session_id", lambda:session)
    create_session = Mock(side_effect=AssertionError("speech must not create a session"))
    monkeypatch.setattr("core.session_manager.create_session", create_session)
    launcher = SimpleNamespace(launch=AsyncMock(side_effect=send))
    monkeypatch.setattr("server.speculative_turn.get_speculative_launcher", lambda:launcher)
    callbacks = production_callbacks({"cooperative_chat":manager,
        "asr_h":SimpleNamespace(listening_state=lambda:{"active":active, "source":source}),
        "server_loop":asyncio.get_running_loop(), "WAKE_AUTO_SEND_TO_CHAT":auto_send,
        "prepared_auip_entry_scope":prepared_auip_entry_scope,
        "_send_wake_text":send, "_handle_vn_player_asr_recognized":AsyncMock()})
    await asyncio.to_thread(callbacks["_on_asr_speech_start"])
    await callbacks["_launch_prepared_speculative_text"]("synthetic speech")
    await callbacks["_handle_asr_recognized"]({"source":"wake", "text":"synthetic speech"})
    assert (observed[0] is not None) == expected
    assert observed[1] is observed[0]
    if expected:
        await observed[0][2]
        manager.auip_entry_context.assert_called_once_with("A")
    else:
        manager.auip_entry_context.assert_not_called()
    assert _prepared_auip_entry.get() is None
    # Inline commands / subsequent recognition cannot inherit a finished utterance.
    await callbacks["_handle_asr_recognized"]({"source":"wake", "text":"next"})
    assert observed[-1] is None
    create_session.assert_not_called()
