"""投机 LLM 启动器（切片 D2）测试。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_speculative_turn.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.speculative_turn import SpeculativeTurnLauncher


class Recorder:
    def __init__(self):
        self.sent: list[dict] = []
        self.confirmed: list[str] = []
        self.discarded: list[tuple[str, str]] = []
        self.confirm_result = True

    async def send_pending(self, text, **kw):
        self.sent.append({"text": text, **kw})
        return {"status": "ok", "turn_id": kw.get("turn_id", "")}

    async def confirm(self, turn_id, *, reason=""):
        self.confirmed.append(turn_id)
        return self.confirm_result

    async def discard(self, turn_id, *, reason=""):
        self.discarded.append((turn_id, reason))
        return True


def _make_launcher(rec: Recorder, *, provider="hybrid", source="wake", busy=False):
    lc = SpeculativeTurnLauncher()

    async def _allowed():
        return True

    lc.configure(
        send_pending=rec.send_pending,
        confirm=rec.confirm,
        discard=rec.discard,
        provider_getter=lambda: provider,
        asr_source_getter=lambda: source,
        chat_busy_fn=lambda: busy,
        voice_allowed_fn=_allowed,
        session_id_factory=lambda: "sess_test",
    )
    return lc


def test_launch_and_confirm_on_match():
    async def run():
        rec = Recorder()
        lc = _make_launcher(rec)
        assert await lc.launch("今日の天気は？") is True
        assert len(rec.sent) == 1
        assert rec.sent[0]["session_id"] == "sess_test"
        assert rec.sent[0]["source"] == "wake"
        assert lc.has_pending

        # 正式文本一致 → 确认，调用方不再发送
        assert await lc.resolve("今日の天気は？") is True
        assert rec.confirmed == [rec.sent[0]["turn_id"]]
        assert rec.discarded == []
        assert not lc.has_pending

    asyncio.run(run())


def test_mismatch_discards_and_falls_through():
    async def run():
        rec = Recorder()
        lc = _make_launcher(rec)
        await lc.launch("今日の天気")
        assert await lc.resolve("今日の天気はどうですか") is False
        assert rec.confirmed == []
        assert rec.discarded[0][1] == "text_mismatch"

    asyncio.run(run())


def test_policy_gates():
    async def run():
        # provider 非 hybrid → 不发（用户要求：只对 hybrid 生效）
        rec = Recorder()
        lc = _make_launcher(rec, provider="deepseek")
        assert await lc.launch("テスト") is False
        assert rec.sent == []
        # bedrock / local 同样不发
        for p in ("bedrock", "local", "openai", ""):
            lc2 = _make_launcher(Recorder(), provider=p)
            assert await lc2.launch("テスト") is False
        # hybrid2 / hybrid3 放行
        for p in ("hybrid2", "hybrid3"):
            rec_ok = Recorder()
            assert await _make_launcher(rec_ok, provider=p).launch("テスト") is True
        # 非 wake 来源不发
        rec3 = Recorder()
        assert await _make_launcher(rec3, source="vn_player").launch("テスト") is False
        # chat 忙不发
        rec4 = Recorder()
        assert await _make_launcher(rec4, busy=True).launch("テスト") is False
        # 空文本不发
        rec5 = Recorder()
        assert await _make_launcher(rec5).launch("   ") is False

    asyncio.run(run())


def test_exact_stop_never_starts_speculative_pending():
    async def run():
        rec = Recorder()
        lc = _make_launcher(rec)

        assert await lc.launch("结束对话。") is False
        assert rec.sent == []
        assert not lc.has_pending

        assert await lc.launch("请解释如何结束对话") is True
        assert [item["text"] for item in rec.sent] == ["请解释如何结束对话"]

    asyncio.run(run())


def test_supersede_and_abandon():
    async def run():
        rec = Recorder()
        lc = _make_launcher(rec)
        await lc.launch("最初の発話")
        first_turn = rec.sent[0]["turn_id"]
        # 新投机抢占旧槽：旧轮作废
        await lc.launch("言い直した発話")
        assert rec.discarded[0] == (first_turn, "superseded")
        assert len(rec.sent) == 2
        # abandon 清理当前槽
        await lc.abandon("listen_stopped:test")
        assert rec.discarded[1][0] == rec.sent[1]["turn_id"]
        assert not lc.has_pending
        # 空槽 abandon / resolve 均为 no-op
        await lc.abandon()
        assert await lc.resolve("なにか") is False
        assert len(rec.discarded) == 2

    asyncio.run(run())


def test_confirm_noop_falls_back_to_normal_send():
    """轮已被作废（打断/门控超时）时 confirm 返回 False → 正常发送。"""
    async def run():
        rec = Recorder()
        rec.confirm_result = False
        lc = _make_launcher(rec)
        await lc.launch("テスト")
        assert await lc.resolve("テスト") is False  # 未确认 → 调用方重发

    asyncio.run(run())


def test_stale_slot_discarded():
    async def run():
        rec = Recorder()
        lc = _make_launcher(rec)
        await lc.launch("古い発話")
        lc._slot_at = time.monotonic() - 60  # 伪造超龄槽
        assert await lc.resolve("古い発話") is False
        assert rec.discarded[0][1] == "stale_slot"

    asyncio.run(run())


def test_manager_on_result_callback():
    """_SpeculativeTranscription：完成且有效才回调；作废后不回调。"""
    import pytest

    pytest.importorskip("torch", reason="local-model tier (torch) is not installed")
    import numpy as np
    from asr.manager import _SpeculativeTranscription

    class SlowBackend:
        def __init__(self, delay=0.05, text="結果"):
            self.delay, self.text = delay, text

        def transcribe(self, audio, sr, context=""):
            time.sleep(self.delay)
            return self.text

    audio = np.zeros(16000, dtype=np.float32)

    results: list[str] = []
    spec = _SpeculativeTranscription(SlowBackend(), "", 16000, on_result=results.append)
    spec.submit(audio)
    time.sleep(0.2)
    assert results == ["結果"], results

    # 作废后完成 → 不回调
    results2: list[str] = []
    spec2 = _SpeculativeTranscription(SlowBackend(delay=0.15), "", 16000, on_result=results2.append)
    spec2.submit(audio)
    spec2.invalidate()
    time.sleep(0.3)
    assert results2 == [], results2


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all speculative turn tests passed")


if __name__ == "__main__":
    _main()
