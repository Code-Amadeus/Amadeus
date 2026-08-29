"""Per-turn 播放完成账本测试。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_turn_playback_complete.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.turn_coordinator import OUT_LLM_STREAMING, TurnCoordinator


def test_stale_turn_completion_releases_only_its_own_waiter():
    c = TurnCoordinator()
    c.open_turn(turn_id="old_turn", local_next_epoch=1)
    c.open_turn(turn_id="new_turn", local_next_epoch=2, pending=True)

    c.on_turn_playback_complete("old_turn")

    assert c.wait_turn_playback_complete("old_turn", timeout=0.0) is True
    assert c.wait_turn_playback_complete("new_turn", timeout=0.0) is False
    snapshot = c.snapshot()
    assert snapshot["active_turn_id"] == "new_turn"
    assert snapshot["output_mode"] == OUT_LLM_STREAMING


def test_missing_turn_id_falls_back_to_active_turn():
    c = TurnCoordinator()
    c.open_turn(turn_id="legacy_active", local_next_epoch=1)

    c.on_turn_playback_complete()

    assert c.wait_turn_playback_complete("legacy_active", timeout=0.0) is True


def test_wait_returns_after_playback_complete():
    c = TurnCoordinator()
    c.open_turn(turn_id="t_done", local_next_epoch=1)
    c.on_turn_playback_complete()
    assert c.wait_turn_playback_complete("t_done", timeout=0.0) is True


def test_wait_blocks_until_complete_event():
    c = TurnCoordinator()
    c.open_turn(turn_id="t_block", local_next_epoch=1)

    def _complete_later():
        time.sleep(0.2)
        c.on_turn_playback_complete()

    threading.Thread(target=_complete_later, daemon=True).start()
    t0 = time.monotonic()
    assert c.wait_turn_playback_complete("t_block", timeout=5.0) is True
    waited = time.monotonic() - t0
    assert 0.15 <= waited < 1.0, f"playback event not honored: waited {waited:.2f}s"


def test_tts_interrupt_releases_playback_waiter():
    c = TurnCoordinator()
    c.open_turn(turn_id="t_interrupt", local_next_epoch=1)
    c.on_tts_interrupted(tts_epoch=1, source="test")
    assert c.wait_turn_playback_complete("t_interrupt", timeout=0.0) is True


def test_discard_releases_playback_waiter():
    c = TurnCoordinator()
    c.open_turn(turn_id="t_discard", local_next_epoch=1, pending=True)
    assert c.discard_turn("t_discard", reason="test") is True
    assert c.wait_turn_playback_complete("t_discard", timeout=0.0) is True


def test_eviction_never_wedges_playback_waiter():
    c = TurnCoordinator()
    c.open_turn(turn_id="old_playback", local_next_epoch=1)
    c.on_chat_turn_finished(turn_id="old_playback", ok=True)
    for i in range(70):
        c.open_turn(turn_id=f"filler_{i}", local_next_epoch=i + 2)
        c.on_chat_turn_finished(turn_id=f"filler_{i}", ok=True)
    assert c.wait_turn_playback_complete("old_playback", timeout=0.1) is True


def test_chat_runtime_skips_playback_wait_when_no_sentence():
    async def run():
        from core.chat_runtime import ChatRuntime

        rt = ChatRuntime()
        rt.configure(
            pending_sentence_items=asyncio.Queue(),
            playback_manager=None,
            provider="local",
        )
        rt._ensure_clients = lambda _provider: None

        async def _run_no_output(st, question, visual_context, enable_conv, llm_provider):
            return None

        rt._run_local = _run_no_output
        result = await rt.stream_llm_query(
            "no output",
            preserve_emotion=True,
            turn_id="empty_turn",
        )
        assert result == ""

    asyncio.run(run())


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all turn playback complete tests passed")


if __name__ == "__main__":
    _main()
