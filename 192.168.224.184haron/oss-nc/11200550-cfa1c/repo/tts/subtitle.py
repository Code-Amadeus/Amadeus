"""
字幕显示函数（日文 / 中文 / 清空）

使用 configure() 在启动时注入运行时依赖（字幕窗口、PlaybackManager），
避免直接引用 main.py 全局变量导致的循环依赖。
"""
from __future__ import annotations

import logging
from typing import Any

from config.log_privacy import protected_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 运行时依赖注册表（由 main.py 在初始化完成后调用 configure() 注入）
# ---------------------------------------------------------------------------
_subtitle_window: Any = None
_subtitle_available: bool = False
_playback_manager: Any = None


def configure(
    subtitle_window,
    subtitle_available: bool,
    playback_manager,
) -> None:
    """
    注入字幕运行时依赖。
    在 subtitle_window_instance 和 playback_manager 初始化完成后调用一次。
    """
    global _subtitle_window, _subtitle_available, _playback_manager
    _subtitle_window = subtitle_window
    _subtitle_available = subtitle_available
    _playback_manager = playback_manager


# ---------------------------------------------------------------------------
# 字幕显示函数
# ---------------------------------------------------------------------------
async def display_japanese_subtitle(sentence_id: str, japanese_text: str) -> None:
    """显示日文字幕，中文翻译区域留空。"""
    try:
        if _subtitle_available and _subtitle_window:
            _subtitle_window.update_display_simple(japanese_text, "")
            logger.info(f"[Subtitle] displayed Japanese subtitle: {sentence_id}")
    except Exception as e:
        logger.error(f"displaying Japanese subtitle failed: {e}")


async def display_chinese_subtitle_with_text(
    sentence_id: str, japanese_text: str, chinese_text: str
) -> None:
    """
    显示中文字幕（统一更新入口）。
    仅当 PlaybackManager 当前播放 ID 与 sentence_id 匹配时才更新，防止乱序覆盖。
    """
    try:
        logger.info(
            f"[Subtitle] attempting translation display: {sentence_id} -> "
            f"'{japanese_text[:30]}...' -> '{chinese_text[:30]}...'"
        )
        if _subtitle_available and _subtitle_window and chinese_text:
            current_id = getattr(_playback_manager, "current_playing_id", None)
            if current_id != sentence_id:
                logger.info(
                    f"[Subtitle] skipped translation display; sentence id mismatch: "
                    f"playing={current_id}, incoming={sentence_id}"
                )
                return
            _subtitle_window.update_display_simple(japanese_text, chinese_text)
            logger.info(
                "[Subtitle] translation displayed: %s",
                protected_text(chinese_text, limit=30),
            )
        else:
            logger.warning("[Subtitle] display conditions not met")
    except Exception as e:
        logger.error(f"displaying Chinese subtitle failed: {e}")


async def display_chinese_subtitle(sentence_id: str, chinese_text: str) -> None:
    """显示中文字幕（当前已禁用）。"""
    pass


async def clear_subtitle_after_sentence(sentence_id: str) -> None:
    """句子播放结束后清空字幕。"""
    try:
        if _subtitle_available and _subtitle_window:
            _subtitle_window.update_display_simple("", "")
            logger.info(f"[Subtitle] cleared: {sentence_id}")
    except Exception as e:
        logger.error(f"clearing subtitle failed: {e}")
