"""TurnCoordinator 状态机行为测试。

运行方式（不依赖 pytest）：
    .venv\\Scripts\\python.exe -X utf8 tests\\test_turn_coordinator.py
也兼容 pytest：
    pytest tests/test_turn_coordinator.py
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.turn_coordinator import (
    ASR_QWEN_HOT,
    ASR_SLEEPING,
    ASR_WAKE_LISTENING,
    OUT_IDLE,
    OUT_INTERRUPTED,
    OUT_LLM_STREAMING,
    OUT_PLAYING,
    TurnCoordinator,
)


def test_normal_voice_turn_flow():
    c = TurnCoordinator()
    snap = c.snapshot()
    assert snap["asr_mode"] == ASR_SLEEPING and snap["output_mode"] == OUT_IDLE
    assert not snap["initialized"]

    c.on_wake_listening(running=True)
    assert c.snapshot()["asr_mode"] == ASR_WAKE_LISTENING

    c.on_wake_detected()
    c.on_asr_listening(source="wake", hot_window=True)
    assert c.snapshot()["asr_mode"] == ASR_QWEN_HOT

    c.on_chat_turn_started(turn_id="t1", session_id="s1", chat_epoch=1, source="wake")
    snap = c.snapshot()
    assert snap["output_mode"] == OUT_LLM_STREAMING
    assert snap["active_turn_id"] == "t1" and snap["session_id"] == "s1"
    assert snap["epochs"]["chat"] == 1

    c.on_sentence_playback_started(sentence_id="sentence_1_x")
    assert c.snapshot()["output_mode"] == OUT_PLAYING

    c.on_turn_playback_complete()
    assert c.snapshot()["output_mode"] == OUT_IDLE

    c.on_chat_turn_finished(turn_id="t1", ok=True)
    snap = c.snapshot()
    assert snap["active_turn_id"] == ""
    assert snap["counters"]["turns_started"] == 1
    assert snap["counters"]["turns_completed"] == 1
    assert snap["counters"]["violations"] == 0

    # 热窗口结束回到 wake 监听
    c.on_asr_stopped(reason="awake_timeout")
    assert c.snapshot()["asr_mode"] == ASR_WAKE_LISTENING
    # wake 也停掉则休眠
    c.on_wake_listening(running=False)
    assert c.snapshot()["asr_mode"] == ASR_SLEEPING
    assert c.snapshot()["initialized"]


def test_observer_sentence_completion_releases_playing_without_a_chat_turn():
    c = TurnCoordinator()

    c.on_sentence_playback_started(sentence_id="observer_sentence")
    assert c.snapshot()["output_mode"] == OUT_PLAYING

    c.on_sentence_playback_complete(sentence_id="observer_sentence")
    snap = c.snapshot()
    assert snap["output_mode"] == OUT_IDLE
    assert snap["recent_transitions"][-1]["event"] == "sentence_playback_complete"


def test_sentence_completion_returns_to_streaming_for_an_active_chat_turn():
    c = TurnCoordinator()
    c.on_chat_turn_started(turn_id="t1", chat_epoch=1)
    c.on_sentence_playback_started(sentence_id="s1")

    c.on_sentence_playback_complete(sentence_id="s1")

    assert c.snapshot()["output_mode"] == OUT_LLM_STREAMING


def test_interrupt_flow_and_stale_playback_violation():
    c = TurnCoordinator()
    c.on_chat_turn_started(turn_id="t1", chat_epoch=1)
    c.on_sentence_playback_started(sentence_id="s1")
    assert c.snapshot()["output_mode"] == OUT_PLAYING

    c.on_tts_interrupted(tts_epoch=5, source="barge_in")
    c.on_playback_interrupted(playback_epoch=3)
    snap = c.snapshot()
    assert snap["output_mode"] == OUT_INTERRUPTED
    assert snap["epochs"]["tts"] == 5 and snap["epochs"]["playback"] == 3
    # interrupts 计数器只在 tts_interrupted / chat_aborted 增加；
    # playback_interrupted 是同一打断链条的下游，只记录不重复计数
    assert snap["counters"]["interrupts"] == 1
    assert snap["counters"]["violations"] == 0

    # 打断后旧音频开始播放 → 违规，且不获得模式所有权（保持 interrupted）
    c.on_sentence_playback_started(sentence_id="s2_stale")
    snap = c.snapshot()
    assert snap["counters"]["violations"] == 1
    assert snap["violations"][0]["rule"] == "playback_after_interrupt"
    assert snap["output_mode"] == OUT_INTERRUPTED

    # 新一轮开始后恢复正常
    c.on_chat_turn_started(turn_id="t2", chat_epoch=2)
    assert c.snapshot()["output_mode"] == OUT_LLM_STREAMING
    c.on_sentence_playback_started(sentence_id="s3")
    assert c.snapshot()["counters"]["violations"] == 1  # 不再新增


def test_overlapping_turns_violation():
    c = TurnCoordinator()
    c.on_chat_turn_started(turn_id="t1", chat_epoch=1)
    c.on_chat_turn_started(turn_id="t2", chat_epoch=2)
    snap = c.snapshot()
    assert snap["counters"]["violations"] == 1
    assert snap["violations"][0]["rule"] == "overlapping_turns"
    assert snap["active_turn_id"] == "t2"

    # 正常 finish + 再开新轮不违规
    c.on_chat_turn_finished(turn_id="t2", ok=True)
    c.on_chat_turn_started(turn_id="t3", chat_epoch=3)
    assert c.snapshot()["counters"]["violations"] == 1


def test_abort_and_stale_finish():
    c = TurnCoordinator()
    c.on_chat_turn_started(turn_id="t1", chat_epoch=1)
    c.on_chat_aborted(turn_id="t1")
    assert c.snapshot()["output_mode"] == OUT_INTERRUPTED
    assert c.snapshot()["counters"]["interrupts"] == 1

    # 迟到的旧轮 finish 不应清掉新轮状态
    c.on_chat_turn_started(turn_id="t2", chat_epoch=2)
    c.on_chat_turn_finished(turn_id="t1", ok=True)  # stale
    snap = c.snapshot()
    assert snap["active_turn_id"] == "t2"
    assert snap["output_mode"] == OUT_LLM_STREAMING


def test_stale_drop_counting_and_ring_buffer():
    c = TurnCoordinator()
    for _ in range(3):
        c.on_stale_dropped(kind="playlist")
    c.on_stale_dropped(kind="stream_chunk")
    snap = c.snapshot()
    assert snap["counters"]["stale_drops"] == 4
    # 环形缓冲有界
    for i in range(200):
        c.on_stale_dropped(kind="hammer")
    assert len(c.snapshot()["recent_transitions"]) <= 20


def test_notify_never_raises_and_thread_safe():
    c = TurnCoordinator()

    def hammer():
        for i in range(300):
            c.on_chat_turn_started(turn_id=f"t{i}", chat_epoch=i)
            c.on_sentence_playback_started(sentence_id=f"s{i}")
            c.on_tts_interrupted(tts_epoch=i)
            c.on_chat_turn_finished(turn_id=f"t{i}")
            c.on_stale_dropped(kind="x")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = c.snapshot()
    assert snap["counters"]["turns_started"] == 1200
    assert snap["counters"]["stale_drops"] == 1200


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all turn coordinator tests passed")


if __name__ == "__main__":
    _main()
