"""
句子状态与预翻译缓存
- SentenceState：单句 TTS/翻译/播放状态跟踪
- SentenceStateManager：全局句子注册表（线程安全）
- PreTranslationCache：异步预翻译缓存（通过 translate_fn 依赖注入，避免循环导入）
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from threading import Lock
from typing import Callable, Coroutine, Any

from config.log_privacy import protected_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SentenceState
# ---------------------------------------------------------------------------
class SentenceState:
    """单句 TTS 和翻译任务的进度跟踪器。"""

    def __init__(self, sentence_id: str, japanese_text: str):
        self.sentence_id = sentence_id
        self.japanese_text = japanese_text
        self.chinese_text = ""

        self.tts_completed = False
        self.translation_completed = False
        self.audio_playing = False

        self.tts_task = None
        self.translation_task = None

        self.created_time = time.time()
        self.tts_start_time = None
        self.tts_end_time = None
        self.translation_start_time = None
        self.translation_end_time = None

        self.subtitle_displayed = False
        self.translation_displayed = False

    def start_tts(self, tts_task) -> None:
        self.tts_task = tts_task
        self.tts_start_time = time.time()
        logger.info(f"TTS task started: {self.sentence_id}")

    def complete_tts(self) -> None:
        self.tts_completed = True
        self.tts_end_time = time.time()
        logger.info(
            f"TTS task completed: {self.sentence_id}, elapsed={self.tts_end_time - self.tts_start_time:.2f}s"
        )

    def start_translation(self, translation_task) -> None:
        self.translation_task = translation_task
        self.translation_start_time = time.time()
        logger.info(f"translation task started: {self.sentence_id}")

    def complete_translation(self, chinese_text: str) -> None:
        self.translation_completed = True
        self.chinese_text = chinese_text
        self.translation_end_time = time.time()
        logger.info(
            f"translation task completed: {self.sentence_id}, elapsed="
            f"{self.translation_end_time - self.translation_start_time:.2f}s"
        )

    def start_audio_playback(self) -> None:
        self.audio_playing = True
        logger.info(f"audio playback started: {self.sentence_id}")

    def is_ready_for_playback(self) -> bool:
        return self.tts_completed and not self.audio_playing

    def is_translation_ready(self) -> bool:
        return self.translation_completed


# ---------------------------------------------------------------------------
# SentenceStateManager
# ---------------------------------------------------------------------------
class SentenceStateManager:
    """管理所有句子状态的注册表（线程安全）。"""

    def __init__(self):
        self.sentences: dict[str, SentenceState] = {}
        self.sentence_counter = 0
        self.lock = Lock()

    def create_sentence(self, japanese_text: str) -> str:
        with self.lock:
            self.sentence_counter += 1
            timestamp_ms = int(time.time() * 1000)
            sentence_id = f"sentence_{self.sentence_counter}_{timestamp_ms}_{uuid.uuid4().hex[:8]}"
            self.sentences[sentence_id] = SentenceState(sentence_id, japanese_text)
            logger.info(f"sentence state created: {sentence_id}")
            return sentence_id

    def begin_turn(self) -> int:
        """Reset the per-presentation sequence and return its first number.

        Sentence ids and PlaybackManager's ``next_seq_to_play`` form one turn
        contract.  Resetting the counter by raw assignment from a caller can
        leave direct presentation lanes producing sequence 6 while playback
        waits forever for sequence 1, so the allocator owns this boundary.
        Existing sentence records are retained for their normal lifecycle.
        """

        with self.lock:
            self.sentence_counter = 0
            return 1

    def get_sentence(self, sentence_id: str) -> SentenceState | None:
        with self.lock:
            return self.sentences.get(sentence_id)

    def remove_sentence(self, sentence_id: str) -> None:
        with self.lock:
            if sentence_id in self.sentences:
                del self.sentences[sentence_id]
                logger.info(f"sentence state removed: {sentence_id}")

    def get_all_sentences(self) -> list[SentenceState]:
        with self.lock:
            return list(self.sentences.values())

    def cleanup_old_sentences(self, max_age_seconds: int = 300) -> None:
        current_time = time.time()
        with self.lock:
            to_remove = [
                sid
                for sid, state in self.sentences.items()
                if current_time - state.created_time > max_age_seconds
            ]
            for sid in to_remove:
                del self.sentences[sid]
                logger.info(f"expired sentence state cleaned up: {sid}")


# ---------------------------------------------------------------------------
# PreTranslationCache
# ---------------------------------------------------------------------------
TranslateFn = Callable[[str], Coroutine[Any, Any, str]]


class PreTranslationCache:
    """
    异步预翻译缓存。

    参数：
        translate_fn: async (japanese_text: str) -> str
            调用方注入的翻译函数，避免循环导入。
            若为 None，翻译任务将直接跳过。
    """

    def __init__(self, translate_fn: TranslateFn | None = None):
        self.cache: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self._translate_fn = translate_fn

    def set_translate_fn(self, translate_fn: TranslateFn) -> None:
        """在事件循环启动后注入翻译函数。"""
        self._translate_fn = translate_fn

    async def start_translation(self, sentence_id: str, japanese_text: str) -> None:
        normalized = japanese_text.strip()
        async with self.lock:
            self.cache[normalized] = {"chinese": "", "status": "translating"}
        asyncio.create_task(self._translate_task(normalized))
        logger.info(
            "[PreTranslationCache] started: %s -> %s",
            sentence_id,
            protected_text(normalized, limit=30),
        )

    async def _translate_task(self, japanese_text: str) -> None:
        if self._translate_fn is None:
            logger.warning("[PreTranslationCache] translate_fn is not injected; translation skipped")
            async with self.lock:
                if japanese_text in self.cache:
                    self.cache[japanese_text]["status"] = "failed"
            return
        try:
            logger.info(
                "[PreTranslationCache] translating: %s",
                protected_text(japanese_text, limit=30),
            )
            try:
                chinese_text = await asyncio.wait_for(
                    self._translate_fn(japanese_text), timeout=12.0
                )
            except asyncio.TimeoutError:
                async with self.lock:
                    if japanese_text in self.cache:
                        self.cache[japanese_text]["status"] = "translating"
                return
            async with self.lock:
                if japanese_text in self.cache:
                    self.cache[japanese_text]["chinese"] = chinese_text
                    self.cache[japanese_text]["status"] = "completed"
            logger.info(
                f"[PreTranslationCache] completed: '{japanese_text[:30]}...' -> '{chinese_text[:30]}...'"
            )
        except Exception as e:
            async with self.lock:
                if japanese_text in self.cache:
                    self.cache[japanese_text]["chinese"] = "translation failed"
                    self.cache[japanese_text]["status"] = "failed"
            logger.error(
                "[PreTranslationCache] failed: %s, %s",
                protected_text(japanese_text, limit=30),
                e,
            )

    async def get_translation(self, japanese_text: str) -> dict | None:
        normalized = japanese_text.strip()
        async with self.lock:
            return self.cache.get(normalized)

    async def remove_translation(self, japanese_text: str) -> None:
        normalized = japanese_text.strip()
        async with self.lock:
            self.cache.pop(normalized, None)


# ---------------------------------------------------------------------------
# 模块级单例（main.py 注入 translate_fn 后使用）
# ---------------------------------------------------------------------------
sentence_state_manager = SentenceStateManager()
pre_translation_cache = PreTranslationCache()  # translate_fn 由 main.py 在初始化时注入
