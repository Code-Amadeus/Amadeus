"""open_turn 申领模式（切片 C）测试。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_open_turn.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.turn_coordinator as tc_mod
from core.turn_coordinator import OUT_LLM_STREAMING, TurnCoordinator


def _fresh_coordinator() -> TurnCoordinator:
    c = TurnCoordinator()
    tc_mod.coordinator = c
    return c


def test_open_turn_atomic_grant():
    c = TurnCoordinator()
    grant = c.open_turn(turn_id="t1", local_next_epoch=1, session_id="s1", source="wake")
    assert grant == {"turn_id": "t1", "chat_epoch": 1, "pending": False}
    snap = c.snapshot()
    assert snap["active_turn_id"] == "t1"
    assert snap["session_id"] == "s1"
    assert snap["turn_source"] == "wake"
    assert snap["output_mode"] == OUT_LLM_STREAMING
    assert snap["epochs"]["chat"] == 1
    assert snap["counters"]["turns_started"] == 1
    assert snap["recent_transitions"][-1]["event"] == "turn_opened"
    assert snap["counters"]["violations"] == 0


def test_epoch_continuity_across_send_abort_send():
    """send(open) → abort(advance) → send(open) 的 epoch 链必须连续无分歧。"""
    c = TurnCoordinator()
    g1 = c.open_turn(turn_id="t1", local_next_epoch=1)
    assert g1["chat_epoch"] == 1
    # abort 路径：advance_chat_epoch（chat_handler._handle_abort 的行为）
    assert c.advance_chat_epoch(local_next=2, source="chat_handler") == 2
    c.on_chat_aborted(turn_id="t1")
    g2 = c.open_turn(turn_id="t2", local_next_epoch=3)
    assert g2["chat_epoch"] == 3
    assert c.snapshot()["counters"]["violations"] == 0


def test_open_turn_overlap_violation():
    c = TurnCoordinator()
    c.open_turn(turn_id="t1", local_next_epoch=1)
    c.open_turn(turn_id="t2", local_next_epoch=2)  # t1 仍在 llm_streaming
    snap = c.snapshot()
    assert snap["counters"]["violations"] == 1
    assert snap["violations"][0]["rule"] == "overlapping_turns"
    assert snap["active_turn_id"] == "t2"
    # 正常 finish 后再开不违规
    c.on_chat_turn_finished(turn_id="t2", ok=True)
    c.open_turn(turn_id="t3", local_next_epoch=3)
    assert c.snapshot()["counters"]["violations"] == 1


def test_chat_handler_send_claims_turn_from_ledger():
    async def run():
        coord = _fresh_coordinator()
        from server.handlers.chat_handler import ChatHandler

        async def fake_stream(text, gui_callback=None, provider=None,
                              visual_context=None, turn_id="", **kw):
            if gui_callback:
                gui_callback("答えの途中")
            return "答えの全部"

        h = ChatHandler()
        h.configure(
            stream_llm_query=fake_stream,
            pending_sentence_items=None,
        )
        result = await h._handle_send({"text": "テスト", "turn_id": "turn_c1", "source": "test"})
        assert result["status"] == "ok"

        # 申领已生效（在流任务完成前就应可见）
        snap = coord.snapshot()
        assert snap["epochs"]["chat"] == 1
        assert snap["counters"]["turns_started"] == 1
        assert h._chat_epoch == 1

        await h._stream_task  # 等流任务完成
        snap = coord.snapshot()
        assert snap["counters"]["turns_completed"] == 1
        assert snap["active_turn_id"] == ""
        assert snap["counters"]["violations"] == 0
        events = [t["event"] for t in snap["recent_transitions"]]
        assert "turn_opened" in events and "chat_turn_finished" in events

    asyncio.run(run())


def test_new_confirmed_chat_turn_interrupts_the_previous_turn_before_opening() -> None:
    async def run() -> None:
        coord = _fresh_coordinator()
        from server.handlers.chat_handler import ChatHandler

        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()

        async def stream(text, gui_callback=None, provider=None,
                         visual_context=None, turn_id="", **kw):
            if text == "first":
                first_started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            return "second answer"

        class _UnconfiguredFlow:
            configured = False

        handler = ChatHandler()
        handler.configure(stream_llm_query=stream, pending_sentence_items=None)
        with patch(
            "server.interrupt_flow.get_interrupt_flow",
            return_value=_UnconfiguredFlow(),
        ):
            await handler._handle_send({"text": "first", "turn_id": "turn_first"})
            first_task = handler._stream_task
            await asyncio.wait_for(first_started.wait(), timeout=2)
            await handler._handle_send({"text": "second", "turn_id": "turn_second"})
            second_task = handler._stream_task
            assert first_task is not None and second_task is not None
            await asyncio.wait_for(first_cancelled.wait(), timeout=2)
            await asyncio.wait_for(second_task, timeout=2)
            await asyncio.gather(first_task, return_exceptions=True)

        snapshot = coord.snapshot()
        assert snapshot["counters"]["violations"] == 0
        assert snapshot["counters"]["turns_started"] == 2
        assert snapshot["counters"]["turns_completed"] == 1
        assert snapshot["active_turn_id"] == ""

    asyncio.run(run())


def test_new_idle_chat_turn_quiesces_background_presentation_before_opening() -> None:
    async def run() -> None:
        coord = _fresh_coordinator()
        from server.handlers.chat_handler import ChatHandler

        events: list[str] = []

        async def interrupt_presentation() -> None:
            assert coord.snapshot()["active_turn_id"] == ""
            events.append("presentation_interrupted")

        async def interrupt_interaction() -> None:
            assert coord.snapshot()["active_turn_id"] == ""
            events.append("interaction_interrupted")

        async def stream(_text, **_kwargs):
            events.append("stream_started")
            return "done"

        handler = ChatHandler()
        handler.configure(
            stream_llm_query=stream,
            pending_sentence_items=None,
            presentation_interrupt=interrupt_presentation,
            background_interaction_interrupt=interrupt_interaction,
        )
        await handler._handle_send({"text": "next", "turn_id": "turn_next"})
        assert coord.snapshot()["active_turn_id"] == "turn_next"
        await asyncio.wait_for(handler._stream_task, timeout=2)
        assert events == [
            "presentation_interrupted",
            "interaction_interrupted",
            "stream_started",
        ]

    asyncio.run(run())


def test_open_turn_ledger_failure_fallback():
    async def run():
        from server.handlers.chat_handler import ChatHandler

        saved = tc_mod.get_turn_coordinator

        def _boom():
            raise RuntimeError("ledger down")

        try:
            tc_mod.get_turn_coordinator = _boom
            h = ChatHandler()
            h._chat_epoch = 4
            grant = h._open_turn(turn_id="tx", session_id="", source="test")
            assert grant["chat_epoch"] == 5  # 本地回退
        finally:
            tc_mod.get_turn_coordinator = saved

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all open_turn tests passed")


if __name__ == "__main__":
    _main()
