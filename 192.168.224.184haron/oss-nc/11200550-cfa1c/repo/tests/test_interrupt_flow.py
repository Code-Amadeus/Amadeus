"""MainTurnInterruptFlow 编排语义测试。

验证的都是"打断机制的精细处"：步骤顺序、参数透传、每步异常隔离、
账本括号原子计数。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_interrupt_flow.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.turn_coordinator as tc_mod
from core.turn_coordinator import TurnCoordinator
from server.interrupt_flow import MainTurnInterruptFlow
from server.protocol import Method


class FakeChatHandler:
    def __init__(self, *, fail: bool = False):
        self.calls: list = []
        self.fail = fail

    async def handle(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail:
            raise RuntimeError("chat boom")
        return {"status": "aborted", "turn_id": "turn_9", "accumulated_text": "途中の返事"}


class FakeTtsHandler:
    def __init__(self, *, fail: bool = False):
        self.calls: list = []
        self.fail = fail

    async def handle(self, method, params):
        self.calls.append((method, dict(params)))
        if self.fail:
            raise RuntimeError("tts boom")
        return {"status": "interrupted"}


def _fresh_coordinator() -> TurnCoordinator:
    """替换进程单例，让 flow 内的 get_turn_coordinator() 拿到干净账本。"""
    c = TurnCoordinator()
    tc_mod.coordinator = c
    return c


def test_sequence_order_and_param_passthrough():
    async def run():
        coord = _fresh_coordinator()
        chat, tts = FakeChatHandler(), FakeTtsHandler()
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=chat, tts_handler=tts)

        result = await flow.interrupt(source="barge_in", annotate_history=True)

        # 顺序：chat abort 必须先于 TTS interrupt
        assert chat.calls[0][0] == Method.CHAT_ABORT
        assert tts.calls[0][0] == Method.TTS_INTERRUPT
        # abort 的 turn_id / accumulated_text 透传进 TTS 打断参数（历史标注依赖）
        tts_params = tts.calls[0][1]
        assert tts_params["turn_id"] == "turn_9"
        assert tts_params["accumulated_text"] == "途中の返事"
        assert tts_params["annotate_history"] is True
        assert tts_params["source"] == "barge_in"
        assert result == {"turn_id": "turn_9", "accumulated_text": "途中の返事"}

        # 账本：括号一次原子打断
        snap = coord.snapshot()
        assert snap["counters"]["interrupts"] == 1
        events = [t["event"] for t in snap["recent_transitions"]]
        assert events[0] == "interrupt_begin" and events[-1] == "interrupt_end"
        assert snap["output_mode"] == "interrupted"

    asyncio.run(run())


def test_chat_abort_failure_does_not_block_tts_interrupt():
    async def run():
        coord = _fresh_coordinator()
        chat, tts = FakeChatHandler(fail=True), FakeTtsHandler()
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=chat, tts_handler=tts)

        result = await flow.interrupt(source="barge_in")

        # chat 失败，TTS 打断仍然执行（turn_id 为空——与原内联行为一致）
        assert len(tts.calls) == 1
        assert tts.calls[0][1]["turn_id"] == ""
        assert result["turn_id"] == ""
        # 括号仍然闭合
        events = [t["event"] for t in coord.snapshot()["recent_transitions"]]
        assert events[-1] == "interrupt_end"

    asyncio.run(run())


def test_tts_failure_still_closes_bracket_and_never_raises():
    async def run():
        coord = _fresh_coordinator()
        flow = MainTurnInterruptFlow()
        flow.configure(chat_handler=FakeChatHandler(), tts_handler=FakeTtsHandler(fail=True))

        result = await flow.interrupt(source="test")
        assert result["turn_id"] == "turn_9"  # abort 成功的结果仍返回
        events = [t["event"] for t in coord.snapshot()["recent_transitions"]]
        assert events[-1] == "interrupt_end"
        assert coord.snapshot()["counters"]["interrupts"] == 1

    asyncio.run(run())


def test_bracket_folds_subevents_into_one_interrupt():
    coord = _fresh_coordinator()
    # 模拟真实链路：括号内 chat_aborted / tts_interrupted / playback_interrupted
    coord.on_interrupt_begin(source="barge_in")
    coord.on_chat_aborted(turn_id="t1")
    coord.on_tts_interrupted(tts_epoch=2, source="barge_in")
    coord.on_playback_interrupted(playback_epoch=2)
    coord.on_interrupt_end(source="barge_in")
    snap = coord.snapshot()
    assert snap["counters"]["interrupts"] == 1, snap["counters"]
    assert snap["epochs"]["tts"] == 2 and snap["epochs"]["playback"] == 2

    # 括号外的独立打断仍逐次计数（WS tts.interrupt 单独调用的路径）
    coord.on_tts_interrupted(tts_epoch=3)
    assert coord.snapshot()["counters"]["interrupts"] == 2


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all interrupt flow tests passed")


if __name__ == "__main__":
    _main()
