"""VN-only bridge from VN reactions to the existing TTS queue.

This module intentionally mirrors the main chat speech path:
- accept Japanese voice text directly when the VN lane already speaks Japanese
- fall back to streaming Chinese-to-Japanese translation for older callers
- split/aggregate text with the same latency-oriented knobs
- run VN-only Japanese -> Chinese subtitle translation side effects
- enqueue into pending_sentence_items so tts.pipeline keeps all playback/TTS optimizations

It is side-effect only. It does not write VN context and it does not participate
in the immediate reaction prompt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, AsyncIterator
from urllib import request

from config import settings
from tools.text_utils import _compute_text_sha1, parse_tags_and_clean
from tts.sentence_state import sentence_state_manager

logger = logging.getLogger(__name__)

_CLIENTS: dict[tuple[str, str], Any] = {}
_TASKS: set[asyncio.Task[Any]] = set()
_TASK_META: dict[asyncio.Task[Any], dict[str, Any]] = {}
_SUBTITLE_TASKS: set[asyncio.Task[Any]] = set()
_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_SUBTITLE_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_SENTENCE_META: dict[str, dict[str, Any]] = {}
_VN_SUBTITLE_CACHE: dict[str, dict[str, str]] = {}


def is_vn_sentence(sentence_id: str) -> bool:
    """Return True when the shared playback queue is handling a VN lane sentence."""
    return str(sentence_id or "") in _SENTENCE_META


def get_vn_sentence_metadata(sentence_id: str) -> dict[str, Any] | None:
    """Return a bounded copy of host playback identity for one queued line."""

    metadata = _SENTENCE_META.get(str(sentence_id or ""))
    return dict(metadata) if isinstance(metadata, dict) else None


def is_vn_tts_busy() -> bool:
    """Return True while VN-side speech jobs may still enqueue audio output."""
    return bool(_TASKS)


def cancel_pending_vn_tts(
    *,
    source: str,
    work_item_id: str = "",
    nonterminal_only: bool = False,
) -> int:
    """Cancel bridge jobs that have not finished enqueueing stale speech."""

    target_source = str(source or "").strip()
    target_work_item = str(work_item_id or "").strip()
    cancelled = 0
    for task, metadata in tuple(_TASK_META.items()):
        if target_source and str(metadata.get("source") or "") != target_source:
            continue
        if target_work_item and str(metadata.get("work_item_id") or "") != target_work_item:
            continue
        if nonterminal_only and metadata.get("terminal") is True:
            continue
        if not task.done():
            task.cancel()
            cancelled += 1
    return cancelled


async def get_vn_subtitle(sentence_id: str, japanese_text: str) -> dict[str, str] | None:
    """Return the VN-only subtitle cache entry for the currently playing sentence."""
    meta = _SENTENCE_META.get(str(sentence_id or ""), {})
    display_text = str(meta.get("display_text") or "").strip()
    if display_text and _is_completed_display_subtitle(display_text, meta.get("display_language")):
        return {"chinese": display_text, "status": "completed", "source": "vn_sentence_meta"}
    normalized = str(japanese_text or "").strip()
    if not normalized:
        return None
    cached = _VN_SUBTITLE_CACHE.get(normalized)
    if cached:
        return dict(cached)
    return None

_STRONG_ENDINGS = {"\u3002", "\uff01", "\uff1f", "!", "?", "\n"}
_WEAK_ENDINGS = {"\u3001", "\uff0c", ",", "\uff1b", ";", "\uff1a", ":"}
_KANA_RE = re.compile("[\u3040-\u30ff]")
_CJK_RE = re.compile("[\u4e00-\u9fff]")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def submit_vn_tts(
    payload: dict[str, Any],
    *,
    pending_sentence_items: asyncio.Queue | None,
    _enqueue_receipt: asyncio.Future | None = None,
) -> dict[str, Any]:
    """Start a non-blocking VN TTS job and return immediately."""
    display_text = _clean_text(payload.get("display_text") or payload.get("subtitle_text") or "")
    voice_text = _clean_text(
        payload.get("voice_text_ja")
        or payload.get("voice_text")
        or payload.get("tts_text")
        or payload.get("text")
        or ""
    )
    if not display_text and not voice_text:
        return {"status": "skipped", "reason": "empty_text"}
    if pending_sentence_items is None:
        return {"status": "skipped", "reason": "tts_queue_unavailable"}

    max_pending = max(1, int(os.environ.get("VN_TTS_MAX_PENDING", "3")))
    if len(_TASKS) >= max_pending:
        return {"status": "dropped", "reason": "vn_tts_queue_full", "pending": len(_TASKS)}

    delivery = (
        payload.get("_narration_delivery")
        if isinstance(payload.get("_narration_delivery"), dict)
        else {}
    )
    metadata = {
        "source": str(payload.get("source") or "").strip(),
        "display_language": _normalize_display_language(payload.get("display_language")),
        "overlay_url": str(payload.get("overlay_url") or "").strip(),
        "emotion": str(payload.get("emotion") or payload.get("emotion_intent") or "").strip(),
        "duration_ms": int(payload.get("duration_ms") or 6500),
        "line_id": str(payload.get("line_id") or "").strip(),
        "script_id": str(payload.get("script_id") or "").strip(),
        "action": str(payload.get("action") or "").strip(),
        "terminal": payload.get("terminal") is True,
        "work_item_id": str(payload.get("work_item_id") or "").strip(),
        "attempt_id": str(payload.get("attempt_id") or "").strip(),
        "narration_source_kind": str(delivery.get("source_kind") or "").strip(),
        "narration_source_id": str(delivery.get("source_id") or "").strip(),
        "narration_session_id": str(delivery.get("session_id") or "").strip(),
        "narration_request_id": str(delivery.get("request_id") or "").strip(),
        "narration_complete_turn": payload.get("complete_turn") is True,
    }
    try:
        from tts.pipeline import current_tts_epoch

        metadata["tts_epoch"] = current_tts_epoch()
    except Exception:
        # Older/headless TTS configurations keep the consumer-stamped path.
        metadata["tts_epoch"] = None
    task = asyncio.create_task(
        _run_vn_tts_job(
            display_text=display_text,
            voice_text=voice_text,
            pending_sentence_items=pending_sentence_items,
            metadata=metadata,
            enqueue_receipt=_enqueue_receipt,
        )
    )
    _TASKS.add(task)
    _TASK_META[task] = metadata

    def _forget(done: asyncio.Task[Any]) -> None:
        _TASKS.discard(done)
        _TASK_META.pop(done, None)

    task.add_done_callback(_forget)
    result = {
        "status": "queued",
        "pending": len(_TASKS),
        "mode": "direct" if voice_text else "translate_stream",
    }
    if _enqueue_receipt is not None:
        result["_task"] = task
    return result


async def submit_vn_tts_confirmed(
    payload: dict[str, Any],
    *,
    pending_sentence_items: asyncio.Queue | None,
) -> dict[str, Any]:
    """Return ``queued`` only after a speakable sentence enters TTS proper."""

    loop = asyncio.get_running_loop()
    receipt: asyncio.Future = loop.create_future()
    scheduled = submit_vn_tts(
        payload,
        pending_sentence_items=pending_sentence_items,
        _enqueue_receipt=receipt,
    )
    task = scheduled.pop("_task", None)
    if scheduled.get("status") != "queued" or task is None:
        return scheduled
    try:
        timeout_s = max(0.5, _env_float("VN_TTS_ENQUEUE_CONFIRM_TIMEOUT", 12.0))
        confirmed = await asyncio.wait_for(asyncio.shield(receipt), timeout=timeout_s)
        if not isinstance(confirmed, dict):
            return {"status": "error", "reason": "invalid_enqueue_receipt"}
        if payload.get("complete_turn") is True:
            # Direct host answers own a complete conversational turn.  Wait
            # only for the bridge to enqueue all of its logical sentences so
            # the caller can mark the real last sentence; audio playback stays
            # asynchronous as before.
            completed = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=timeout_s,
            )
            if isinstance(completed, dict):
                confirmed = {**confirmed, **completed}
        return {**scheduled, **confirmed}
    except asyncio.CancelledError:
        # The narration owner may supersede a progress utterance with a newer
        # terminal fact.  A shield protects queue receipt bookkeeping from a
        # timeout, but ownership cancellation must still stop the bridge from
        # enqueueing the rest of the stale utterance.
        task.cancel()
        raise
    except asyncio.TimeoutError:
        task.cancel()
        return {"status": "error", "reason": "tts_enqueue_confirmation_timeout"}


async def _run_vn_tts_job(
    *,
    display_text: str,
    voice_text: str,
    pending_sentence_items: asyncio.Queue,
    metadata: dict[str, Any],
    enqueue_receipt: asyncio.Future | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    sem = _get_loop_semaphore(loop)
    async with sem:
        dispatcher = _StreamingSentenceDispatcher(
            display_text=display_text,
            pending_sentence_items=pending_sentence_items,
            metadata=metadata,
            enqueue_receipt=enqueue_receipt,
        )
        try:
            if voice_text:
                await dispatcher.feed(voice_text, allow_early_cut=False)
            else:
                async for piece in _stream_translate_zh_to_ja(display_text):
                    await dispatcher.feed(piece)
            await dispatcher.flush()
            dispatcher.finish_receipt()
            return dispatcher.completion_result()
        except asyncio.CancelledError:
            if enqueue_receipt is not None and not enqueue_receipt.done():
                enqueue_receipt.set_result(
                    {"status": "error", "reason": "tts_enqueue_cancelled"}
                )
            raise
        except Exception as exc:
            if enqueue_receipt is not None and not enqueue_receipt.done():
                enqueue_receipt.set_result(
                    {
                        "status": "error",
                        "reason": f"bridge_job_failed:{exc.__class__.__name__}",
                    }
                )
            logger.exception("[VN TTS] bridge job failed")
            return {
                "status": "error",
                "reason": f"bridge_job_failed:{exc.__class__.__name__}",
            }


def _get_loop_semaphore(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    key = id(loop)
    sem = _SEMAPHORES.get(key)
    if sem is None:
        concurrency = max(1, int(os.environ.get("VN_TTS_TRANSLATE_CONCURRENCY", "1")))
        sem = asyncio.Semaphore(concurrency)
        _SEMAPHORES[key] = sem
    return sem


class _StreamingSentenceDispatcher:
    def __init__(
        self,
        *,
        display_text: str,
        pending_sentence_items: asyncio.Queue,
        metadata: dict[str, Any],
        enqueue_receipt: asyncio.Future | None = None,
    ) -> None:
        self.display_text = display_text
        self.pending_sentence_items = pending_sentence_items
        self.metadata = dict(metadata or {})
        self.current_sentence = ""
        self.is_first = True
        self.enqueue_receipt = enqueue_receipt
        self.last_enqueue_failure = "no_speakable_sentence"
        self.last_sentence_id = ""

    async def feed(self, text_piece: str, *, allow_early_cut: bool = True) -> None:
        if not text_piece:
            return
        for ch in str(text_piece):
            self.current_sentence += ch
            if ch in _STRONG_ENDINGS or ch in _WEAK_ENDINGS:
                if self._should_dispatch(ch):
                    await self._dispatch_current()
            elif allow_early_cut and self._should_early_cut():
                await self._dispatch_current()

    async def flush(self) -> None:
        if self.current_sentence.strip():
            await self._dispatch_current()

    def finish_receipt(self) -> None:
        if self.enqueue_receipt is not None and not self.enqueue_receipt.done():
            self.enqueue_receipt.set_result(
                {"status": "dropped", "reason": self.last_enqueue_failure}
            )

    def completion_result(self) -> dict[str, Any]:
        if not self.last_sentence_id:
            return {"status": "dropped", "reason": self.last_enqueue_failure}
        return {
            "status": "queued",
            "last_sentence_id": self.last_sentence_id,
        }

    def _should_dispatch(self, ch: str) -> bool:
        sentence_len = len(self.current_sentence.strip())
        min_chars = max(0, _env_int("VN_TTS_MIN_DISPATCH_CHARS", 14))
        if self.is_first:
            first_min_chars = max(0, _env_int("VN_TTS_FIRST_MIN_DISPATCH_CHARS", 18))
            if ch in _WEAK_ENDINGS and sentence_len < first_min_chars:
                return False
            if ch in _STRONG_ENDINGS and sentence_len < min(6, first_min_chars):
                return False
            return True

        if sentence_len < min_chars:
            return False
        return not (ch in _WEAK_ENDINGS and sentence_len < 5)

    def _should_early_cut(self) -> bool:
        sentence_len = len(self.current_sentence.strip())
        if self.is_first:
            early_cut = max(0, int(getattr(settings, "FIRST_SENTENCE_EARLY_CUT_CHARS", 11)))
            return early_cut > 0 and sentence_len >= early_cut
        max_chars = max(1, _env_int("VN_TTS_MAX_DISPATCH_CHARS", 80))
        return sentence_len >= max_chars

    async def _dispatch_current(self) -> None:
        text = _clean_text(self.current_sentence)
        self.current_sentence = ""
        if not text or _is_punctuation_only(text):
            return

        sentence_id = sentence_state_manager.create_sentence(text)
        _SENTENCE_META[sentence_id] = dict(self.metadata)
        display_text = self.display_text.strip()
        display_language = self.metadata.get("display_language")
        if display_text and _is_completed_display_subtitle(display_text, display_language):
            _SENTENCE_META[sentence_id]["display_text"] = display_text
            if _should_seed_display_translation(display_language):
                await _seed_display_translation(text, display_text)
        else:
            if display_text:
                _SENTENCE_META[sentence_id]["rejected_display_text"] = display_text[:160]
                logger.info(
                    "[VN subtitle] rejected untranslated display_text id=%s source=%s",
                    sentence_id,
                    self.metadata.get("source") or "",
                )
            _start_vn_subtitle_translation(sentence_id, text, self.metadata)
        from tts.contract import TTSRequest
        item = TTSRequest(
            sentence_id=sentence_id,
            text=text,
            is_first=self.is_first,
            stream_tts=True,
            source=str(self.metadata.get("source") or "vn"),
            turn_id=str(self.metadata.get("line_id") or ""),
            tts_epoch=(
                int(self.metadata["tts_epoch"])
                if self.metadata.get("tts_epoch") is not None
                else None
            ),
            metadata=dict(self.metadata),
        )
        try:
            put_timeout = max(0.1, _env_float("VN_TTS_QUEUE_PUT_TIMEOUT", 3.0))
            await asyncio.wait_for(self.pending_sentence_items.put(item), timeout=put_timeout)
            self.last_sentence_id = sentence_id
            logger.info(
                "[VN TTS] enqueue id=%s first=%s sha1=%s text='%s'",
                sentence_id,
                self.is_first,
                _compute_text_sha1(text),
                text[:60],
            )
            self.is_first = False
            if self.enqueue_receipt is not None and not self.enqueue_receipt.done():
                self.enqueue_receipt.set_result(
                    {"status": "queued", "sentence_id": sentence_id}
                )
        except asyncio.TimeoutError:
            self.last_enqueue_failure = "pending_sentence_queue_full"
            logger.warning("[VN TTS] pending_sentence_items full; dropped sentence id=%s", sentence_id)


async def _seed_display_translation(japanese_text: str, chinese_text: str) -> None:
    normalized = japanese_text.strip()
    if not normalized or not chinese_text.strip():
        return
    _VN_SUBTITLE_CACHE[normalized] = {
        "chinese": chinese_text.strip(),
        "status": "completed",
        "source": "vn_tts_bridge",
    }


def _is_completed_display_subtitle(text: str, display_language: object = None) -> bool:
    """Return True when display_text is safe to treat as an already translated subtitle."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    language = _normalize_display_language(display_language)
    if language == "japanese":
        # Japanese display_text is the role/source line.  Treating it as a
        # completed translated caption makes Chinese caption mode replay the
        # Japanese sentence and prevents the subtitle sidecar from running.
        return False
    return not _looks_like_untranslated_japanese(cleaned)


def _should_seed_display_translation(display_language: object = None) -> bool:
    language = _normalize_display_language(display_language)
    return language in {"simplified_chinese", "bilingual"}


def _normalize_display_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "": "simplified_chinese",
        "zh": "simplified_chinese",
        "zh_cn": "simplified_chinese",
        "chinese": "simplified_chinese",
        "simplified_chinese": "simplified_chinese",
        "both": "bilingual",
        "bilingual": "bilingual",
        "ja": "japanese",
        "jp": "japanese",
        "ja_jp": "japanese",
        "japanese": "japanese",
        "en": "english",
        "en_us": "english",
        "english": "english",
    }
    return aliases.get(raw, "simplified_chinese")


def _looks_like_untranslated_japanese(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if not compact:
        return False
    kana_count = len(_KANA_RE.findall(compact))
    if kana_count < 3:
        return False
    cjk_count = len(_CJK_RE.findall(compact))
    # Chinese subtitles may quote a short Japanese title/name. Only reject when
    # kana is substantial enough that the whole display line is probably still
    # Japanese original text rather than a Chinese sentence with a quoted term.
    kana_ratio = kana_count / max(1, len(compact))
    return kana_count > cjk_count or kana_ratio >= 0.27 or (kana_count >= 8 and kana_ratio >= 0.20)


def _start_vn_subtitle_translation(sentence_id: str, japanese_text: str, metadata: dict[str, Any]) -> None:
    task = asyncio.create_task(
        _translate_and_publish_vn_subtitle(
            sentence_id=str(sentence_id or ""),
            japanese_text=str(japanese_text or ""),
            metadata=dict(metadata or {}),
        )
    )
    _SUBTITLE_TASKS.add(task)
    task.add_done_callback(_SUBTITLE_TASKS.discard)


async def _translate_and_publish_vn_subtitle(
    *,
    sentence_id: str,
    japanese_text: str,
    metadata: dict[str, Any],
) -> None:
    normalized = japanese_text.strip()
    if not normalized:
        return

    cached = _VN_SUBTITLE_CACHE.get(normalized)
    if cached and cached.get("status") == "completed" and cached.get("chinese"):
        return

    _VN_SUBTITLE_CACHE[normalized] = {
        "chinese": "",
        "status": "translating",
        "source": "vn_subtitle_translate",
    }
    try:
        sem = _get_subtitle_translate_semaphore(asyncio.get_running_loop())
        async with sem:
            chinese_text = await _translate_ja_to_zh(normalized)
        chinese_text = _clean_subtitle_translation(chinese_text)
        if not chinese_text:
            raise RuntimeError("empty subtitle translation")
        _VN_SUBTITLE_CACHE[normalized] = {
            "chinese": chinese_text,
            "status": "completed",
            "source": "vn_subtitle_translate",
        }
        logger.info(
            "[VN subtitle] translated id=%s sha1=%s ja='%s' zh='%s'",
            sentence_id,
            _compute_text_sha1(normalized),
            normalized[:40],
            chinese_text[:40],
        )
    except Exception as exc:
        _VN_SUBTITLE_CACHE[normalized] = {
            "chinese": "",
            "status": "failed",
            "source": "vn_subtitle_translate",
        }
        logger.warning("[VN subtitle] translation failed id=%s: %s", sentence_id, exc)


def _get_subtitle_translate_semaphore(loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
    key = id(loop)
    sem = _SUBTITLE_SEMAPHORES.get(key)
    if sem is None:
        concurrency = max(1, int(os.environ.get("VN_SUBTITLE_TRANSLATE_CONCURRENCY", "2")))
        sem = asyncio.Semaphore(concurrency)
        _SUBTITLE_SEMAPHORES[key] = sem
    return sem


async def publish_overlay_subtitle(sentence_id: str, japanese_text: str, chinese_text: str) -> None:
    meta = _SENTENCE_META.get(str(sentence_id or ""), {})
    await _publish_overlay(
        meta,
        display_text=str(chinese_text or "").strip(),
        raw_text=str(chinese_text or japanese_text or "").strip(),
        source="vn_pretranslation",
    )


async def _publish_overlay(
    meta: dict[str, Any],
    *,
    display_text: str,
    raw_text: str,
    source: str,
) -> None:
    url = str((meta or {}).get("overlay_url") or "").strip()
    if not url:
        return
    payload = {
        "text": raw_text,
        "display_text": display_text,
        "emotion": str((meta or {}).get("emotion") or "thinking"),
        "duration_ms": int((meta or {}).get("duration_ms") or 6500),
        "line_id": str((meta or {}).get("line_id") or ""),
        "script_id": str((meta or {}).get("script_id") or ""),
        "source": source,
    }
    try:
        await asyncio.to_thread(_post_json, url, payload, 0.25)
    except Exception:
        logger.debug("[VN TTS] overlay publish failed: %s", url, exc_info=True)


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> None:
    raw = __import__("json").dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        resp.read()


async def _translate_ja_to_zh(japanese_text: str) -> str:
    provider = os.environ.get("VN_SUBTITLE_TRANSLATE_PROVIDER", "deepseek").strip().lower()
    if provider not in {"deepseek", "openai"}:
        provider = "deepseek"

    if provider == "openai":
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        base_url = os.environ.get("VN_SUBTITLE_TRANSLATE_BASE_URL") or getattr(settings, "OPENAI_BASE_URL", "")
        model = os.environ.get("VN_SUBTITLE_TRANSLATE_MODEL") or getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.4-mini")
    else:
        api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("VN_SUBTITLE_TRANSLATE_BASE_URL") or getattr(settings, "DEEPSEEK_BASE_URL", "")
        model = os.environ.get("VN_SUBTITLE_TRANSLATE_MODEL") or getattr(settings, "DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError(f"{provider} API key is not configured")

    client = _get_client(provider, api_key, base_url)
    system = (
        "You are a narrow VN subtitle translation sidecar. Translate Japanese game "
        "dialogue into concise natural Simplified Chinese. Return only Chinese text. "
        "No markdown, no JSON, no quotes, no explanations, and no added facts."
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": japanese_text},
        ],
        "temperature": 0.1,
        "max_tokens": max(80, int(os.environ.get("VN_SUBTITLE_TRANSLATE_MAX_TOKENS", "220"))),
        "stream": False,
        "timeout": max(4.0, float(os.environ.get("VN_SUBTITLE_TRANSLATE_TIMEOUT", "12"))),
    }
    if provider == "deepseek":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    response = await asyncio.to_thread(lambda: client.chat.completions.create(**kwargs))
    try:
        return str(response.choices[0].message.content or "").strip()
    except Exception:
        return ""


async def _stream_translate_zh_to_ja(chinese_text: str) -> AsyncIterator[str]:
    provider = os.environ.get("VN_TTS_TRANSLATE_PROVIDER", "deepseek").strip().lower()
    if provider not in {"deepseek", "openai"}:
        provider = "deepseek"

    if provider == "openai":
        api_key = getattr(settings, "OPENAI_API_KEY", "")
        base_url = os.environ.get("VN_TTS_TRANSLATE_BASE_URL") or getattr(settings, "OPENAI_BASE_URL", "")
        model = os.environ.get("VN_TTS_TRANSLATE_MODEL") or getattr(settings, "OPENAI_MODEL_NAME", "gpt-5.4-mini")
    else:
        api_key = getattr(settings, "DEEPSEEK_API_KEY", "")
        base_url = os.environ.get("VN_TTS_TRANSLATE_BASE_URL") or getattr(settings, "DEEPSEEK_BASE_URL", "")
        model = os.environ.get("VN_TTS_TRANSLATE_MODEL") or getattr(settings, "DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError(f"{provider} API key is not configured")

    client = _get_client(provider, api_key, base_url)
    system = (
        "You are a narrow VN TTS translation sidecar. Translate the Chinese Kurisu "
        "Makise reaction into natural Japanese for speech synthesis. Return only "
        "Japanese text. No Chinese, no markdown, no JSON, no quotes, no control tags, "
        "no stage directions, and no new facts. Keep Kurisu's concise skeptical tone."
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": chinese_text},
        ],
        "temperature": 0.15,
        "max_tokens": max(80, int(os.environ.get("VN_TTS_TRANSLATE_MAX_TOKENS", "180"))),
        "stream": True,
        "timeout": max(4.0, float(os.environ.get("VN_TTS_TRANSLATE_TIMEOUT", "12"))),
    }
    if provider == "deepseek":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    stream = await asyncio.to_thread(lambda: client.chat.completions.create(**kwargs))
    async for chunk in _aiter_sync_iter(stream):
        try:
            piece = chunk.choices[0].delta.content or ""
        except Exception:
            piece = ""
        piece = _strip_translation_noise(piece)
        if piece:
            yield piece


def _get_client(provider: str, api_key: str, base_url: str):
    key = (provider, base_url)
    cached = _CLIENTS.get(key)
    if cached is not None:
        return cached
    import httpx
    from openai import OpenAI

    http_client = httpx.Client(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=60.0),
        timeout=httpx.Timeout(30.0),
        http2=False,
    )
    client = OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
    _CLIENTS[key] = client
    return client


async def _aiter_sync_iter(sync_iterable) -> AsyncIterator[Any]:
    iterator = iter(sync_iterable)
    while True:
        has_item, item = await asyncio.to_thread(_next_or_sentinel, iterator)
        if not has_item:
            break
        yield item


def _next_or_sentinel(iterator) -> tuple[bool, Any]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None


def _clean_text(text: Any) -> str:
    value = str(text or "")
    try:
        value, _actions = parse_tags_and_clean(value)
    except Exception:
        value = re.sub(r"\[(?:PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE)(?:[^\]]*)\]", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _strip_translation_noise(text: str) -> str:
    value = str(text or "")
    value = value.replace("```", "")
    value = re.sub(r"\[(?:PARAM|EXPR|HOTKEY|EMO|ANIM|DELEGATE)(?:[^\]]*)\]", "", value, flags=re.IGNORECASE)
    return value


def _clean_subtitle_translation(text: Any) -> str:
    value = str(text or "").strip()
    value = value.replace("```", "").strip()
    value = re.sub(r"^\s*(?:json|text|zh|cn|chinese|translation)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    value = value.strip().strip('"').strip("'").strip()
    return re.sub(r"\s+", " ", value).strip()


def _is_punctuation_only(text: str) -> bool:
    return bool(re.fullmatch(r"[\s。！？!?、，,；;：:…・\-ー~「」『』（）()\[\]]+", str(text or "")))
