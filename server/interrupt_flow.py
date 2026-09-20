"""主语音轮复合打断编排器（所有权迁移·切片 A）。

历史上"打断一轮对话"的复合序列（chat abort → TTS/playback 打断）只存在于
barge-in 回调内部。本模块把这段编排提为唯一入口，供 barge-in 及后续调用方
（pending-turn 作废、provider 抢占等）复用，并在 TurnCoordinator 上以
interrupt_begin/end 括号记为一次原子打断事件。

刻意不改变的行为（打断机制精细，序列语义即正确性）：

1. 步骤顺序固定：先 chat abort（取消 LLM 流任务，停止新句子产生），
   再 TTS interrupt（epoch 递增、排干队列、毒杀活跃流、重建信号量、
   停播放器、可选历史标注）。顺序颠倒会让仍在跑的流在排干之后继续入队。
2. 每一步独立 try/except——chat abort 失败不阻止 TTS 打断（与原
   barge-in 内联代码逐行一致）。
3. completed_text 采集、annotate_history、epoch 递增全部仍由
   TtsHandler._interrupt / tts.pipeline / PlaybackManager 内部完成，
   本模块只编排调用，不搬运任何内部状态逻辑。

调用方自己负责的语境性动作不在此处：barge-in 的防抖、探测器停止、
VN 门控、ASR 释放与重臂——它们属于 barge-in 语境，不属于打断本身。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("interrupt_flow")


class MainTurnInterruptFlow:
    def __init__(self) -> None:
        self._chat_handler = None
        self._tts_handler = None

    def configure(self, *, chat_handler=None, tts_handler=None) -> None:
        if chat_handler is not None:
            self._chat_handler = chat_handler
        if tts_handler is not None:
            self._tts_handler = tts_handler

    @property
    def configured(self) -> bool:
        return self._chat_handler is not None and self._tts_handler is not None

    async def interrupt(
        self,
        *,
        source: str,
        annotate_history: bool = True,
    ) -> dict[str, Any]:
        """执行复合打断，返回 {turn_id, accumulated_text}。

        任何一步失败都继续后续步骤（与原 barge-in 内联序列一致）；
        本方法自身绝不抛异常。
        """
        from core.turn_coordinator import get_turn_coordinator
        from server.protocol import Method

        aborted_turn_id = ""
        aborted_text = ""

        try:
            get_turn_coordinator().on_interrupt_begin(source=source)
        except Exception:
            logger.debug("coordinator interrupt-begin notify failed", exc_info=True)

        try:
            # 步骤 1：chat abort —— 递增 chat epoch、取消 LLM 流任务，
            # 保证后续 TTS 排干期间不再有新句子入队。
            if self._chat_handler is not None:
                try:
                    abort_result = await self._chat_handler.handle(Method.CHAT_ABORT, {})
                    aborted_turn_id = str((abort_result or {}).get("turn_id") or "")
                    aborted_text = str((abort_result or {}).get("accumulated_text") or "")
                except Exception:
                    logger.exception("%s chat abort failed", source)

            # 步骤 2：TTS/playback 打断 —— completed_text 采集、双 epoch 递增、
            # 队列排干、历史标注均在 TtsHandler._interrupt 内部按原有顺序完成。
            if self._tts_handler is not None:
                try:
                    await self._tts_handler.handle(
                        Method.TTS_INTERRUPT,
                        {
                            "annotate_history": annotate_history,
                            "source": source,
                            "turn_id": aborted_turn_id,
                            "accumulated_text": aborted_text,
                        },
                    )
                except Exception:
                    logger.exception("%s TTS interrupt failed", source)
        finally:
            try:
                get_turn_coordinator().on_interrupt_end(source=source)
            except Exception:
                logger.debug("coordinator interrupt-end notify failed", exc_info=True)

        return {"turn_id": aborted_turn_id, "accumulated_text": aborted_text}


# 进程级单例（bootstrap 时 configure）
interrupt_flow = MainTurnInterruptFlow()


def get_interrupt_flow() -> MainTurnInterruptFlow:
    return interrupt_flow
