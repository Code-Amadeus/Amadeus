"""pending-turn 机制（切片 D1）测试。

覆盖：轮次状态机、TTS 出队门控、打断作废 pending 轮、
chat_handler 确认/作废、历史写入防护、状态表淘汰兜底。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_pending_turn.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.turn_coordinator as tc_mod
from core.turn_coordinator import (
    GATE_DROP,
    GATE_PROCEED,
    GATE_WAIT,
    TurnCoordinator,
)


def test_all_pending_gates_share_settings_timeout():
    from config.settings import PENDING_TURN_GATE_TIMEOUT_S
    from core.chat_runtime import PENDING_TURN_GATE_TIMEOUT_S as history_timeout
    from core.turn_coordinator import PENDING_TURN_GATE_TIMEOUT_S as coordinator_timeout
    from server.handlers.chat_handler import PENDING_TURN_GATE_TIMEOUT_S as visible_timeout
    from tts.pipeline import _PENDING_TURN_GATE_TIMEOUT_S as tts_timeout

    assert PENDING_TURN_GATE_TIMEOUT_S == 8.0
    assert coordinator_timeout == PENDING_TURN_GATE_TIMEOUT_S
    assert tts_timeout == PENDING_TURN_GATE_TIMEOUT_S
    assert visible_timeout == PENDING_TURN_GATE_TIMEOUT_S
    assert history_timeout == PENDING_TURN_GATE_TIMEOUT_S


def _fresh_coordinator() -> TurnCoordinator:
    c = TurnCoordinator()
    tc_mod.coordinator = c
    return c


def test_turn_state_machine_and_gate():
    c = TurnCoordinator()
    # 普通轮：开即 confirmed，门控放行
    c.open_turn(turn_id="t_norm", local_next_epoch=1)
    assert c.turn_gate("t_norm") == GATE_PROCEED
    # 未知 / 空 turn（VN、live、legacy）一律放行
    assert c.turn_gate("") == GATE_PROCEED
    assert c.turn_gate("t_unknown") == GATE_PROCEED

    # pending 轮：门控扣住
    c.on_chat_turn_finished(turn_id="t_norm", ok=True)
    grant = c.open_turn(turn_id="t_spec", local_next_epoch=2, pending=True)
    assert grant["pending"] is True
    assert c.turn_gate("t_spec") == GATE_WAIT

    # 确认 → 放行
    assert c.confirm_turn("t_spec") is True
    assert c.turn_gate("t_spec") == GATE_PROCEED
    # 决议幂等：重复确认 / 对已决议轮作废均为 no-op
    assert c.confirm_turn("t_spec") is False
    assert c.discard_turn("t_spec") is False
    assert c.turn_gate("t_spec") == GATE_PROCEED


def test_discard_and_wait_semantics():
    c = TurnCoordinator()
    c.open_turn(turn_id="t1", local_next_epoch=1, pending=True)

    # 后台线程 150ms 后作废，wait_turn_decided 应及时返回 drop
    def _later():
        time.sleep(0.15)
        c.discard_turn("t1", reason="test")

    threading.Thread(target=_later, daemon=True).start()
    t0 = time.monotonic()
    gate = c.wait_turn_decided("t1", timeout=5.0)
    waited = time.monotonic() - t0
    assert gate == GATE_DROP
    assert waited < 1.0, f"decision event not honored: waited {waited:.2f}s"
    snap = c.snapshot()
    assert snap["counters"]["turns_discarded"] == 1
    assert snap["active_turn_id"] == ""  # 作废清空活跃轮

    # 超时未决议 → 返回 wait，由调用方决策
    c.open_turn(turn_id="t2", local_next_epoch=2, pending=True)
    assert c.wait_turn_decided("t2", timeout=0.1) == GATE_WAIT


def test_interrupt_discards_all_pending_turns():
    c = TurnCoordinator()
    c.open_turn(turn_id="p1", local_next_epoch=1, pending=True)
    # 复合打断括号作废 pending 轮
    c.on_interrupt_begin(source="barge_in")
    c.on_interrupt_end(source="barge_in")
    assert c.turn_gate("p1") == GATE_DROP

    # 独立 TTS 打断同样作废
    c.open_turn(turn_id="p2", local_next_epoch=2, pending=True)
    c.on_tts_interrupted(tts_epoch=1)
    assert c.turn_gate("p2") == GATE_DROP
    assert c.snapshot()["counters"]["turns_discarded"] == 2


def test_state_cap_eviction_never_wedges_waiters():
    c = TurnCoordinator()
    c.open_turn(turn_id="old_pending", local_next_epoch=1, pending=True)
    # 塞满状态表触发淘汰
    for i in range(70):
        c.open_turn(turn_id=f"filler_{i}", local_next_epoch=i + 2)
    # 被淘汰的 pending 轮：事件已置位（不悬死），门控按 proceed 对待
    assert c.turn_gate("old_pending") == GATE_PROCEED
    assert c.wait_turn_decided("old_pending", timeout=0.1) == GATE_PROCEED


def test_pipeline_gate_helper():
    async def run():
        coord = _fresh_coordinator()
        from tts.pipeline import _gate_job_turn

        # 无轮次 → 放行
        assert await _gate_job_turn("") == "proceed"
        # confirmed → 放行
        coord.open_turn(turn_id="g1", local_next_epoch=1)
        assert await _gate_job_turn("g1") == "proceed"
        # discarded → 丢弃
        coord.on_chat_turn_finished(turn_id="g1", ok=True)
        coord.open_turn(turn_id="g2", local_next_epoch=2, pending=True)
        coord.discard_turn("g2")
        assert await _gate_job_turn("g2") == "drop"
        # pending → 等待决议后放行
        coord.open_turn(turn_id="g3", local_next_epoch=3, pending=True)

        async def _confirm_soon():
            await asyncio.sleep(0.1)
            coord.confirm_turn("g3")

        confirm_task = asyncio.create_task(_confirm_soon())
        t0 = time.monotonic()
        gate = await _gate_job_turn("g3")
        assert gate == "proceed" and time.monotonic() - t0 < 2.0
        await confirm_task

    asyncio.run(run())


def test_chat_handler_pending_lifecycle():
    async def run():
        coord = _fresh_coordinator()
        from server.handlers.chat_handler import ChatHandler

        stream_started = asyncio.Event()
        stream_cancelled = asyncio.Event()

        async def slow_stream(text, gui_callback=None, provider=None,
                              visual_context=None, turn_id="", **kw):
            stream_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stream_cancelled.set()
                raise
            return "never"

        h = ChatHandler()
        h.configure(stream_llm_query=slow_stream, pending_sentence_items=None)
        await h._handle_send({"text": "テスト", "turn_id": "spec_1",
                              "source": "asr_spec", "pending": True})
        await asyncio.wait_for(stream_started.wait(), timeout=5)
        assert coord.turn_gate("spec_1") == GATE_WAIT

        # 作废：门控变 drop、流任务被取消、epoch 推进使迟到回调失效
        epoch_before = h._chat_epoch
        ok = await h.discard_pending_turn("spec_1", reason="asr_mismatch")
        assert ok is True
        assert coord.turn_gate("spec_1") == GATE_DROP
        await asyncio.wait_for(stream_cancelled.wait(), timeout=5)
        assert h._chat_epoch == epoch_before + 1
        assert h._active_turn_id == ""

        # 确认路径
        await h._handle_send({"text": "テスト2", "turn_id": "spec_2",
                              "source": "asr_spec", "pending": True})
        await asyncio.wait_for(stream_started.wait(), timeout=5)
        assert await h.confirm_pending_turn("spec_2") is True
        assert coord.turn_gate("spec_2") == GATE_PROCEED
        h._stream_task.cancel()  # 清理慢流任务

    asyncio.run(run())


def test_pending_chat_visible_only_after_confirmed():
    async def run():
        _fresh_coordinator()
        from server.event_bus import bus
        from server.handlers.chat_handler import ChatHandler
        from server.protocol import Method

        first_token_sent = asyncio.Event()
        confirmed = asyncio.Event()
        events: list[tuple[str, dict]] = []

        async def on_event(method, params):
            events.append((method, dict(params)))

        async def stream(text, gui_callback=None, provider=None,
                         visual_context=None, turn_id="", **kw):
            gui_callback("draft")
            first_token_sent.set()
            await confirmed.wait()
            gui_callback("draft final")
            return "draft final"

        bus.on(Method.CHAT_TOKEN, on_event)
        bus.on(Method.CHAT_COMPLETE, on_event)
        try:
            h = ChatHandler()
            h.configure(stream_llm_query=stream, pending_sentence_items=None)
            await h._handle_send({
                "text": "テスト",
                "turn_id": "spec_visible",
                "source": "asr_spec",
                "pending": True,
            })
            await asyncio.wait_for(first_token_sent.wait(), timeout=5)
            await asyncio.sleep(0.05)
            assert events == []

            assert await h.confirm_pending_turn("spec_visible") is True
            confirmed.set()
            await asyncio.wait_for(h._stream_task, timeout=5)

            assert events == [
                (
                    Method.CHAT_COMPLETE,
                    {"turn_id": "spec_visible", "full_text": "draft final"},
                )
            ]
        finally:
            bus.off(Method.CHAT_TOKEN, on_event)
            bus.off(Method.CHAT_COMPLETE, on_event)

    asyncio.run(run())


def test_confirmed_chat_turn_emits_the_authoritative_user_message():
    async def run():
        _fresh_coordinator()
        from server.event_bus import bus
        from server.handlers.chat_handler import ChatHandler
        from server.protocol import Method

        events: list[dict] = []

        async def capture(_method, params):
            events.append(dict(params))

        async def stream(_text, **_kwargs):
            return ""

        bus.on(Method.CHAT_USER, capture)
        try:
            handler = ChatHandler()
            handler.configure(stream_llm_query=stream, pending_sentence_items=None)
            await handler.send_text(
                "type on the desk",
                turn_id="wallpaper-turn",
                session_id="wallpaper-session",
                source="wallpaper_keyboard",
            )
            assert events == [{
                "turn_id": "wallpaper-turn",
                "text": "type on the desk",
                "session_id": "wallpaper-session",
                "source": "wallpaper_keyboard",
            }]
            assert handler._stream_task is not None
            await asyncio.wait_for(handler._stream_task, timeout=5)
        finally:
            bus.off(Method.CHAT_USER, capture)

    asyncio.run(run())


def test_history_guard():
    async def run():
        coord = _fresh_coordinator()
        from core.chat_runtime import ChatRuntime

        # 作废轮 → 拒绝写历史
        coord.open_turn(turn_id="h1", local_next_epoch=1, pending=True)
        coord.discard_turn("h1")
        assert await ChatRuntime._turn_allows_history("h1") is False
        # 确认轮 / 无轮次 → 放行
        coord.open_turn(turn_id="h2", local_next_epoch=2, pending=True)
        coord.confirm_turn("h2")
        assert await ChatRuntime._turn_allows_history("h2") is True
        assert await ChatRuntime._turn_allows_history("") is True
        # 未决议：等待期内决议 → 按决议放行/拒绝
        coord.open_turn(turn_id="h3", local_next_epoch=3, pending=True)

        async def _discard_soon():
            await asyncio.sleep(0.1)
            coord.discard_turn("h3")

        task = asyncio.create_task(_discard_soon())
        assert await ChatRuntime._turn_allows_history("h3") is False
        await task

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all pending turn tests passed")


if __name__ == "__main__":
    _main()
