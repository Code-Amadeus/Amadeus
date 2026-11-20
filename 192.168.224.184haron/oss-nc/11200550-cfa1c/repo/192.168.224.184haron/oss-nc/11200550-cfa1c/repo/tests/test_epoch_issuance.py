"""Epoch 发放权迁移（切片 B）测试。

覆盖：发放语义（seed/连续/分歧取 max）、三个所有者与账本的同步、
账本不可用时的回退行为。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_epoch_issuance.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.turn_coordinator as tc_mod
from core.turn_coordinator import TurnCoordinator


def _fresh_coordinator() -> TurnCoordinator:
    c = TurnCoordinator()
    tc_mod.coordinator = c
    return c


def test_issuance_semantics():
    c = TurnCoordinator()
    # 初始化对齐：账本未观察过 → 采纳 local_next
    assert c.advance_tts_epoch(local_next=1) == 1
    # 连续推进
    assert c.advance_tts_epoch(local_next=2) == 2
    assert c.snapshot()["counters"]["violations"] == 0

    # 分歧（本地落后）：发放 max(local_next, mirror+1)=3，记违规
    assert c.advance_tts_epoch(local_next=1) == 3
    snap = c.snapshot()
    assert snap["counters"]["violations"] == 1
    assert snap["violations"][0]["rule"] == "epoch_divergence"

    # 分歧（本地超前）：取大保安全方向
    assert c.advance_tts_epoch(local_next=9) == 9
    assert c.snapshot()["counters"]["violations"] == 2

    # 三种 epoch 相互独立
    assert c.advance_chat_epoch(local_next=1) == 1
    assert c.advance_playback_epoch(local_next=1) == 1
    assert c.snapshot()["epochs"] == {"chat": 1, "tts": 9, "playback": 1}


def test_pipeline_interrupt_syncs_ledger():
    coord = _fresh_coordinator()
    import tts.pipeline as pipeline

    saved = pipeline._tts_interrupt_epoch
    try:
        pipeline._tts_interrupt_epoch = 0
        e1 = pipeline.interrupt_pending_tts()
        e2 = pipeline.interrupt_pending_tts()
        assert (e1, e2) == (1, 2), (e1, e2)
        snap = coord.snapshot()
        assert snap["epochs"]["tts"] == 2
        assert snap["counters"]["violations"] == 0
        # 缓存与账本一致
        assert pipeline._tts_interrupt_epoch == 2
    finally:
        pipeline._tts_interrupt_epoch = saved


def test_playback_interrupt_syncs_ledger():
    async def run():
        coord = _fresh_coordinator()
        from tts.playback import PlaybackManager

        class FakePlayer:
            def stop(self):
                pass

        pm = PlaybackManager(FakePlayer())
        await pm.interrupt()
        await pm.interrupt()
        assert pm.playback_epoch == 2
        snap = coord.snapshot()
        assert snap["epochs"]["playback"] == 2
        assert snap["counters"]["violations"] == 0

    asyncio.run(run())


def test_chat_abort_syncs_ledger():
    async def run():
        coord = _fresh_coordinator()
        from server.handlers.chat_handler import ChatHandler

        h = ChatHandler()
        await h._handle_abort({})
        await h._handle_abort({})
        assert h._chat_epoch == 2
        snap = coord.snapshot()
        assert snap["epochs"]["chat"] == 2
        assert snap["counters"]["violations"] == 0

    asyncio.run(run())


def test_ledger_unavailable_falls_back_to_local():
    import tts.pipeline as pipeline

    saved_epoch = pipeline._tts_interrupt_epoch
    saved_getter = tc_mod.get_turn_coordinator

    def _boom():
        raise RuntimeError("ledger down")

    try:
        pipeline._tts_interrupt_epoch = 5
        tc_mod.get_turn_coordinator = _boom
        e = pipeline.interrupt_pending_tts()
        assert e == 6, e  # 旧行为：本地自增
        assert pipeline._tts_interrupt_epoch == 6
    finally:
        tc_mod.get_turn_coordinator = saved_getter
        pipeline._tts_interrupt_epoch = saved_epoch


def test_composite_interrupt_end_to_end_ledger_state():
    """模拟一次完整复合打断后账本的终态。"""
    async def run():
        coord = _fresh_coordinator()
        import tts.pipeline as pipeline
        from tts.playback import PlaybackManager
        from server.handlers.chat_handler import ChatHandler

        saved = pipeline._tts_interrupt_epoch
        try:
            pipeline._tts_interrupt_epoch = 0

            class FakePlayer:
                def stop(self):
                    pass

            pm = PlaybackManager(FakePlayer())
            h = ChatHandler()

            coord.on_interrupt_begin(source="barge_in")
            await h._handle_abort({})
            pipeline.interrupt_pending_tts()
            await pm.interrupt()
            coord.on_interrupt_end(source="barge_in")

            snap = coord.snapshot()
            assert snap["epochs"] == {"chat": 1, "tts": 1, "playback": 1}, snap["epochs"]
            assert snap["counters"]["interrupts"] == 1
            assert snap["counters"]["violations"] == 0
            assert snap["output_mode"] == "interrupted"
        finally:
            pipeline._tts_interrupt_epoch = saved

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all epoch issuance tests passed")


if __name__ == "__main__":
    _main()
