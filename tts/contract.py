"""TTS 请求契约（runtime convergence plan, Phase 5）。

所有进入 pending_sentence_items 队列的条目统一为 TTSRequest。
历史上四条生产路径各用各的元组形状：

    chat      (sentence_id, text, is_first, stream_tts)
    chat CLI  (sentence_id, text, is_first)
    LIVE      (sentence_id, text, is_first)
    VN        (sentence_id, text, is_first, True)

统一后的契约携带：
- 轮次身份 turn_id —— pending-turn 语义（投机 LLM 启动的确认前缓冲/整体废弃）
  依赖它；chat 路径由 chat_handler 注入，其余路径为空。
- source —— 观测与调试用，标记条目来自哪条链路。
- tts_epoch / playback_epoch —— 可选的入队所有权戳。后台/VN 生产者在
  开始异步生成语音时领取 tts_epoch，防止主聊天抢占后迟到的旧语音
  误领新 epoch；尚未迁移的生产者仍由消费侧在出队时盖章。

过渡兼容：from_queue_item 接受旧元组，未迁移的生产者不会崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TTSRequest:
    sentence_id: str
    text: str
    is_first: bool = False
    # None = 由消费侧按 is_first 推断（与旧 3 元组行为一致）
    stream_tts: bool | None = None
    # chat | chat_cli | live | vn | test | legacy
    source: str = "chat"
    # chat 轮次身份；空字符串表示该路径尚无轮次概念
    turn_id: str = ""
    # 后台生产者在异步工作开始时领取；None 保持旧消费侧盖章行为
    tts_epoch: int | None = None
    playback_epoch: int | None = None
    # 预留：语音风格/情绪
    emotion: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_queue_item(cls, item: Any) -> "TTSRequest":
        """将队列条目规范化为 TTSRequest；兼容旧元组形状。"""
        if isinstance(item, TTSRequest):
            return item
        if isinstance(item, (tuple, list)) and len(item) >= 3:
            return cls(
                sentence_id=str(item[0]),
                text=str(item[1]),
                is_first=bool(item[2]),
                stream_tts=item[3] if len(item) > 3 else None,
                source="legacy",
            )
        raise TypeError(f"unsupported TTS queue item: {type(item)!r}")
