"""tts/pipeline.py — TTS 合成 + 播放调度管线

负责：
  - TTS 参数选择（get_sovits_params）
  - 各种 speak_* 合成策略（改进版 / 流式 / Graph串行 / 增强异步队列）
  - 句子调度工作线程（play_sentence_worker）
  - CUDA Graph 管线预热（warmup_graph_pipeline）

运行时状态（模块级，供 main.py 读写）：
  exp_tts_semaphore, exp_play_condition, graph_tts_lock

依赖注入（通过 configure() 在 async main() 完成初始化后调用）：
  tts_runtime, tts_executor, playback_manager, player,
  pending_sentence_items, llm_warmup_fn
"""

import asyncio
import logging
import os
import re
import sys
import time
import traceback

import numpy as np

from config.log_privacy import protected_text
from config.settings import (
    PENDING_TURN_GATE_TIMEOUT_S,
    USE_EXPERIMENTAL_TTS_STREAM,
    EXP_TTS_MAX_CONCURRENCY,
    TTS_OUTPUT_LANGUAGE,
    TTS_REF_AUDIO_JA,
    TTS_REF_TEXT_JA,
    TTS_REF_AUDIO_EN,
    TTS_REF_TEXT_EN,
    TTS_RTF_INITIAL,
)
from tools.text_utils import (
    _compute_text_sha1,
    _parse_sentence_seq,
    async_generator_from_sync,
)
from tools.tts_text_processor import correct_pronunciation_for_tts
from tts.contract import TTSRequest
from tts.first_sentence_audio_cache import get_first_sentence_audio_cache
from tts.latency_clock import log_latency_marker
from tts.synthesis_backend import SynthesisBackends, select_synthesis
from tts.utterance_scheduler import TTSUtteranceScheduler

logger = logging.getLogger(__name__)
_utterance_scheduler = TTSUtteranceScheduler(logger=logger)

# ===== 运行时状态（供外部模块读写）=====
exp_tts_semaphore = None
exp_play_condition = None
graph_tts_lock = asyncio.Lock()

# ===== 依赖注入 —— configure() 填充这些 =====
_tts_runtime = None
_tts_executor = None
_playback_manager = None
_player = None
_pending_sentence_items = None
_llm_warmup_fn = None   # remote_llm_query，供 warmup_graph_pipeline 使用
_exp_tts_semaphore = None  # 并发合成信号量，由 main.py 创建后注入
_exp_tts_concurrency = EXP_TTS_MAX_CONCURRENCY
_RTF_EMA_ALPHA = 0.3
_rtf_ema = max(0.001, float(TTS_RTF_INITIAL))
_tts_interrupt_epoch: int = 0
_active_stream_queues: set[asyncio.Queue] = set()


def current_tts_epoch() -> int:
    """Return the current ownership epoch for asynchronous speech producers."""

    return int(_tts_interrupt_epoch)


def get_rtf_estimate() -> float:
    return max(0.001, float(_rtf_ema))


def _update_rtf_ema(elapsed: float, samples: int, sample_rate: int | float | None, sentence_id: str = "") -> None:
    global _rtf_ema
    try:
        rate = float(sample_rate or 0)
        duration = float(samples) / rate if rate > 0 else 0.0
        if elapsed <= 0 or duration <= 0:
            return
        rtf = float(elapsed) / duration
        if rtf <= 0:
            return
        _rtf_ema = (_rtf_ema * (1.0 - _RTF_EMA_ALPHA)) + (rtf * _RTF_EMA_ALPHA)
        logger.debug(
            "[TTS-RTF] updated ema=%.3f sample=%.3f id=%s elapsed=%.3fs audio=%.3fs",
            _rtf_ema,
            rtf,
            sentence_id,
            elapsed,
            duration,
        )
    except Exception:
        logger.debug("failed to update TTS RTF estimate", exc_info=True)


_TTS_FULLWIDTH_PAREN_RE = re.compile(r"\uFF08[^\uFF08\uFF09]*\uFF09")


def _strip_tts_fullwidth_parentheses(text: str) -> str:
    """Remove full-width Japanese/Chinese parenthetical hints for speech only."""
    if not text:
        return text
    stripped = str(text)
    while True:
        next_text = _TTS_FULLWIDTH_PAREN_RE.sub("", stripped)
        if next_text == stripped:
            break
        stripped = next_text
    stripped = re.sub(r"[ \t]{2,}", " ", stripped).strip()
    return stripped


def _detect_text_language(text: str) -> str:
    """根据字符组成自动判断文本语言（'日文' 或 '英文'）。
    日文/中文字符占比 > 15% 则判定为日文，否则为英文。
    """
    if not text:
        return TTS_OUTPUT_LANGUAGE
    jp_count = sum(
        1 for c in text
        if '぀' <= c <= 'ヿ'   # 平假名/片假名
        or '一' <= c <= '鿿'   # CJK 汉字
        or '＀' <= c <= '￯'   # 全角符号
    )
    return "日文" if jp_count / max(len(text), 1) > 0.15 else "英文"


def reconfigure_tts_language(lang: str) -> None:
    """热切换 TTS 输出语言（'日文' 或 '英文'），无需重启。
    同步更新 local_tts_infer 模块的运行时变量（若已加载）。
    """
    if lang not in {"日文", "英文"}:
        raise ValueError(f"unsupported TTS language: {lang!r}")
    global TTS_OUTPUT_LANGUAGE
    TTS_OUTPUT_LANGUAGE = lang
    try:
        from config import settings as config_settings

        config_settings.TTS_OUTPUT_LANGUAGE = lang
    except Exception:
        pass
    try:
        _lti = sys.modules.get("local_tts_infer")
        if _lti is not None:
            _lti._TTS_OUTPUT_LANGUAGE = lang
    except Exception:
        pass
    logger.info(f"[TTS Lang] switched to: {lang}")


def current_tts_language_code() -> str:
    return "en" if TTS_OUTPUT_LANGUAGE == "英文" else "ja"


def reconfigure_tts_language_code(value: str) -> str:
    code = str(value or "").strip().lower()
    aliases = {
        "ja": "日文",
        "jp": "日文",
        "japanese": "日文",
        "日文": "日文",
        "en": "英文",
        "english": "英文",
        "英文": "英文",
    }
    language = aliases.get(code)
    if language is None:
        raise ValueError(f"unsupported TTS language code: {value!r}")
    reconfigure_tts_language(language)
    return current_tts_language_code()


def reconfigure_tts_mode(cuda_graph: bool, concurrency: int) -> None:
    """热更新 TTS 推理模式（GUI 切换时调用，当前无任务时最安全）。

    cuda_graph=True  + concurrency=1 → CUDA Graph 串行模式（首句更快）
    cuda_graph=False + concurrency=2 → 并行模式（吞吐量优先）
    """
    global _exp_tts_semaphore, _exp_tts_concurrency
    os.environ['ENABLE_CUDA_GRAPH'] = '1' if cuda_graph else '0'
    _exp_tts_concurrency = max(1, int(concurrency))
    _exp_tts_semaphore = asyncio.Semaphore(concurrency)
    logger.info(
        f"[TTS Mode] switched -> CUDA_Graph={'ON' if cuda_graph else 'OFF'}, "
        f"semaphore={concurrency}"
    )


def current_tts_mode() -> str:
    return "cuda_graph" if os.environ.get("ENABLE_CUDA_GRAPH", "0") == "1" else "parallel"


def reconfigure_tts_mode_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "cuda_graph": "cuda_graph",
        "cuda graph ×1": "cuda_graph",
        "graph": "cuda_graph",
        "parallel": "parallel",
        "parallel ×2": "parallel",
    }
    mode = aliases.get(raw)
    if mode is None:
        raise ValueError(f"unsupported TTS mode: {value!r}")
    if mode == "cuda_graph":
        reconfigure_tts_mode(cuda_graph=True, concurrency=1)
    else:
        reconfigure_tts_mode(cuda_graph=False, concurrency=2)
    return mode


def _is_interrupted(epoch: int) -> bool:
    return epoch != _tts_interrupt_epoch


def _release_task_semaphore(task_semaphore, sentence_id: str) -> None:
    if task_semaphore is None:
        return
    try:
        task_semaphore.release()
        logger.debug("[Semaphore] release stale TTS permit: %s", sentence_id)
    except Exception:
        logger.exception("[Semaphore] failed to release stale TTS permit: %s", sentence_id)


async def _wait_producer_after_interrupt(
    queue_obj: asyncio.Queue,
    producer_future,
    sentence_id: str,
    *,
    force_graph: bool,
) -> None:
    """Keep CUDA Graph/static-KV ownership until an already-started producer exits."""
    if producer_future is None or not force_graph:
        return
    logger.info("[TTS-INTERRUPT] waiting stale graph producer to finish: %s", sentence_id)
    while True:
        if producer_future.done() and queue_obj.empty():
            break
        try:
            item = await asyncio.wait_for(queue_obj.get(), timeout=0.1)
        except asyncio.TimeoutError:
            if producer_future.done():
                break
            continue
        if isinstance(item, tuple) and isinstance(item[0], str) and item[0].startswith("__"):
            signal, _data = item
            if signal in {"__DONE__", "__ERROR__"}:
                break
    try:
        await producer_future
    except Exception:
        logger.debug("[TTS-INTERRUPT] stale graph producer ended with error: %s", sentence_id, exc_info=True)


def _drain_queue_nowait(queue_obj) -> int:
    if queue_obj is None:
        return 0
    drained = 0
    while True:
        try:
            queue_obj.get_nowait()
        except asyncio.QueueEmpty:
            break
        except Exception:
            break
        drained += 1
        try:
            queue_obj.task_done()
        except ValueError:
            pass
        except Exception:
            pass
    return drained


# Backward-compatible module alias; the authoritative value lives in settings.
_PENDING_TURN_GATE_TIMEOUT_S = PENDING_TURN_GATE_TIMEOUT_S


async def _gate_job_turn(turn_id: str) -> str:
    """TTS 出队门控（pending-turn 语义，切片 D1）。

    返回 "proceed" / "drop"。pending 轮经 to_thread 阻塞等待决议
    （不占事件循环）；超时仍未决议按 drop 处理并记违规——
    宁可丢掉一轮投机语音，不让 TTS worker 永久卡死。
    任何异常一律放行（门控失效退化为旧管线行为）。
    """
    try:
        from core.turn_coordinator import get_turn_coordinator

        coordinator = get_turn_coordinator()
        gate = coordinator.turn_gate(turn_id)
        if gate == "wait":
            gate = await asyncio.to_thread(
                coordinator.wait_turn_decided, turn_id, _PENDING_TURN_GATE_TIMEOUT_S
            )
            if gate == "wait":
                logger.warning(
                    "[TTS-GATE] pending turn undecided after %.1fs; dropping turn=%s",
                    _PENDING_TURN_GATE_TIMEOUT_S,
                    turn_id,
                )
                coordinator.discard_turn(turn_id, reason="gate_timeout")
                return "drop"
        return gate
    except Exception:
        return "proceed"


def _next_tts_epoch() -> int:
    """向 TurnCoordinator 账本申领下一 TTS epoch（所有权迁移·切片 B）。

    发放权在账本；模块级 _tts_interrupt_epoch 仅作热路径只读缓存
    （_is_interrupted 每 chunk 检查）。账本不可用时回退本地自增（旧行为）。
    """
    try:
        from core.turn_coordinator import get_turn_coordinator

        return get_turn_coordinator().advance_tts_epoch(
            local_next=_tts_interrupt_epoch + 1, source="tts.pipeline"
        )
    except Exception:
        return _tts_interrupt_epoch + 1


def interrupt_pending_tts() -> int:
    """Invalidate queued/running TTS work and close active stream queues."""
    global _tts_interrupt_epoch, _exp_tts_semaphore
    _tts_interrupt_epoch = _next_tts_epoch()
    drained = _drain_queue_nowait(_pending_sentence_items)
    scheduler_drained = _utterance_scheduler.clear()
    # Scheduler lookahead has already called Queue.get() for every buffered
    # item. Clearing that private buffer transfers completion ownership back
    # here; without the matching task_done(), Queue.join() can never release
    # even though qsize() is zero after a barge-in.
    if _pending_sentence_items is not None:
        for _ in range(scheduler_drained):
            try:
                _pending_sentence_items.task_done()
            except ValueError:
                logger.warning(
                    "[TTS-INTERRUPT] scheduler buffer exceeded queue task ownership"
                )
                break
    for stream_queue in list(_active_stream_queues):
        try:
            stream_queue.put_nowait(None)
        except Exception:
            pass
    old_semaphore = _exp_tts_semaphore
    if old_semaphore is not None:
        for _ in range(max(1, int(_exp_tts_concurrency))):
            try:
                old_semaphore.release()
            except Exception:
                break
        _exp_tts_semaphore = asyncio.Semaphore(max(1, int(_exp_tts_concurrency)))
    logger.info(
        "[TTS-INTERRUPT] epoch=%s drained_pending=%s drained_scheduler=%s active_streams=%s",
        _tts_interrupt_epoch,
        drained,
        scheduler_drained,
        len(_active_stream_queues),
    )
    try:
        from core.turn_coordinator import get_turn_coordinator

        get_turn_coordinator().on_tts_interrupted(tts_epoch=_tts_interrupt_epoch)
    except Exception:
        pass
    return _tts_interrupt_epoch


def discard_pending_tts(
    *,
    source: str,
    work_item_id: str = "",
    nonterminal_only: bool = False,
) -> int:
    """Drop queued, not-yet-playing speech made stale by a newer user read.

    This is deliberately selective: it does not advance the global TTS epoch,
    stop active playback, or touch normal role speech.
    """

    target_source = str(source or "").strip()
    target_work_item = str(work_item_id or "").strip()

    def matches(request) -> bool:
        if target_source and str(request.source or "") != target_source:
            return False
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        if target_work_item and str(metadata.get("work_item_id") or "") != target_work_item:
            return False
        if nonterminal_only and metadata.get("terminal") is True:
            return False
        return True

    kept = []
    discarded = 0
    queue_obj = _pending_sentence_items
    if queue_obj is not None:
        while True:
            try:
                item = queue_obj.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                request = TTSRequest.from_queue_item(item)
                if matches(request):
                    discarded += 1
                else:
                    kept.append(item)
            except TypeError:
                kept.append(item)
            finally:
                queue_obj.task_done()
        for item in kept:
            queue_obj.put_nowait(item)
    discarded += _utterance_scheduler.discard(matches)
    if discarded:
        logger.info(
            "[TTS-SUPERSEDE] discarded=%d source=%s work_item_id=%s",
            discarded,
            target_source,
            target_work_item,
        )
    return discarded


def configure(
    tts_runtime=None,
    tts_executor=None,
    playback_manager=None,
    player=None,
    pending_sentence_items=None,
    llm_warmup_fn=None,
    exp_tts_semaphore=None,
):
    """在 async main() 完成运行时初始化后调用，注入所有运行时依赖。"""
    global _tts_runtime, _tts_executor, _playback_manager, _player
    global _pending_sentence_items, _llm_warmup_fn, _exp_tts_semaphore
    global _exp_tts_concurrency
    if tts_runtime is not None:
        _tts_runtime = tts_runtime
    if tts_executor is not None:
        _tts_executor = tts_executor
    if playback_manager is not None:
        _playback_manager = playback_manager
        _utterance_scheduler.configure_deadline(
            cover_seconds_getter=lambda: (
                _playback_manager.estimate_cover_seconds()
                if _playback_manager is not None
                and hasattr(_playback_manager, "estimate_cover_seconds")
                else None
            ),
            rtf_getter=get_rtf_estimate,
        )
    if player is not None:
        _player = player
    if pending_sentence_items is not None:
        _pending_sentence_items = pending_sentence_items
    if llm_warmup_fn is not None:
        _llm_warmup_fn = llm_warmup_fn
    if exp_tts_semaphore is not None:
        _exp_tts_semaphore = exp_tts_semaphore
        try:
            _exp_tts_concurrency = max(1, int(os.environ.get("EXP_TTS_MAX_CONCURRENCY", str(EXP_TTS_MAX_CONCURRENCY))))
        except Exception:
            _exp_tts_concurrency = EXP_TTS_MAX_CONCURRENCY


# =============================================================================
# TTS 参数选择
# =============================================================================

def get_sovits_params(text: str, is_first_sentence: bool = False):
    """根据文本长度和是否为首句返回合适的推理参数。

    CUDA Graph 开关仅由环境变量 ENABLE_CUDA_GRAPH 控制，静态 KV Cache 始终开启。
    """
    length = len(text.strip())
    cuda_graph_enabled = os.environ.get("ENABLE_CUDA_GRAPH", "0") == "1"

    if is_first_sentence:
        max_sec_override = max(3.5, min(8.0, length * 0.25 or 3.5))
        return {
            "text_language": TTS_OUTPUT_LANGUAGE,
            "prompt_language": TTS_OUTPUT_LANGUAGE,
            "top_k": 5,
            "top_p": 1,
            "temperature": 0.6,
            "sample_steps": 4,
            "if_sr": False,
            "how_to_cut": "不切",
            "speed": 1.1,
            "pause_second": 0.05,
            "if_freeze": False,
            "enable_cuda_graph": cuda_graph_enabled,
            "enable_static_kv": True,
            "max_sec_override": max_sec_override,
        }

    if length < 45:
        return {
            "text_language": TTS_OUTPUT_LANGUAGE,
            "prompt_language": TTS_OUTPUT_LANGUAGE,
            "top_k": 5,
            "top_p": 1,
            "temperature": 0.6,
            "sample_steps": 16,
            "if_sr": False,
            "how_to_cut": "不切",
            "speed": 1,
            "pause_second": 0.05,
            "if_freeze": False,
            "enable_cuda_graph": cuda_graph_enabled,
            "enable_static_kv": True,
        }

    return {
        "text_language": TTS_OUTPUT_LANGUAGE,
        "prompt_language": TTS_OUTPUT_LANGUAGE,
        "top_k": 5,
        "top_p": 1,
        "temperature": 0.6,
        "sample_steps": 32,
        "if_sr": False,
        "how_to_cut": "凑四句一切",
        "speed": 1,
        "pause_second": 0.12,
        "if_freeze": False,
        "enable_cuda_graph": cuda_graph_enabled,
        "enable_static_kv": True,
    }


# =============================================================================
# 合成策略
# =============================================================================

# 参考音频 / 文本：始终跟随全局 TTS_OUTPUT_LANGUAGE（由用户设置按钮控制）
def _get_ref_audio(text: str = "") -> str:
    return TTS_REF_AUDIO_EN if TTS_OUTPUT_LANGUAGE == "英文" else TTS_REF_AUDIO_JA

def _get_ref_text(text: str = "") -> str:
    return TTS_REF_TEXT_EN if TTS_OUTPUT_LANGUAGE == "英文" else TTS_REF_TEXT_JA

# 向后兼容：模块级常量保留但指向当前语言的默认值（warmup 等地方直接引用时使用）
_REF_AUDIO = TTS_REF_AUDIO_JA
_REF_TEXT  = TTS_REF_TEXT_JA


_FIRST_SENTENCE_STREAM_MIN_CHUNK_SEC = 0.35
_FIRST_SENTENCE_STREAM_MAX_CHUNK_SEC = 0.80
_FIRST_SENTENCE_STREAM_TARGET_CHUNKS = 3


def _compute_first_sentence_stream_chunk_seconds(text: str, params: dict) -> float:
    """按首句长度估算一个更稳的 chunk 时长，目标约 3 段，避免切得过碎。"""
    stripped = (text or "").strip()
    length = len(stripped)
    speed = float(params.get("speed", 1.0) or 1.0)

    # 这里不用 max_sec_override，它对首句有 3.5s 下限，会让极短句估计严重失真。
    estimated_sec = max(0.9, min(3.0, (length * 0.11 + 0.25) / max(speed, 0.6)))
    estimated_sec = max(0.9, min(3.0, (length * 0.11 + 0.25) / max(speed, 0.6)))
    if length <= 8:
        return 0.40
    target_chunks = 2.5 if length <= 16 else float(_FIRST_SENTENCE_STREAM_TARGET_CHUNKS)
    chunk_sec = estimated_sec / target_chunks
    return max(
        _FIRST_SENTENCE_STREAM_MIN_CHUNK_SEC,
        min(_FIRST_SENTENCE_STREAM_MAX_CHUNK_SEC, chunk_sec),
    )


_PUNCT_ONLY_CHARS = set(" \t\r\n.,!?;:。！？、，…~～-—_・\"'`()[]{}<>《》〈〉「」『』【】")


def _is_punctuation_only_sentence(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(stripped) and all(ch in _PUNCT_ONLY_CHARS for ch in stripped)


async def speak_stream_graph_serial(
    text,
    sentence_id,
    is_first_sentence=False,
    stream_tts=None,
    segments=None,
    interrupt_epoch: int | None = None,
    task_semaphore=None,
):
    """Graph 模式专用：全局锁确保串行推理，合成完即释放锁让下一句并行合成。
    stream_tts: 若显式传入 bool，覆盖 is_first_sentence 对 stream_to_player 的默认推断。
    """
    global graph_tts_lock
    interrupt_epoch = _tts_interrupt_epoch if interrupt_epoch is None else interrupt_epoch
    if _is_interrupted(interrupt_epoch):
        logger.info("[TTS-INTERRUPT] skip stale job before graph lock: %s", sentence_id)
        _release_task_semaphore(task_semaphore, sentence_id)
        return
    if graph_tts_lock is None:
        graph_tts_lock = asyncio.Lock()
    logger.info(f"[Graph Serial] waiting for serial inference lock: {sentence_id}")
    await graph_tts_lock.acquire()
    start_time = time.time()
    if _is_interrupted(interrupt_epoch):
        logger.info("[TTS-INTERRUPT] skip stale job after graph lock: %s", sentence_id)
        graph_tts_lock.release()
        _release_task_semaphore(task_semaphore, sentence_id)
        return
    logger.info(f"[Graph Serial] lock acquired, starting serial inference: {sentence_id}")
    if is_first_sentence:
        log_latency_marker(logger, "first_graph_lock", id=sentence_id)

    _lock_released = False
    def _release_lock():
        nonlocal _lock_released
        if _lock_released:
            return
        _lock_released = True
        elapsed = time.time() - start_time
        logger.info(f"[Graph Serial] released serial inference lock: {sentence_id}, TTS elapsed {elapsed:.2f}s")
        graph_tts_lock.release()

    _stream_to_player = stream_tts if stream_tts is not None else is_first_sentence
    try:
        await speak_stream_enhanced_asyncio_queue(
            text,
            sentence_id,
            is_first_sentence=is_first_sentence,
            force_graph=True,
            on_synthesis_done=_release_lock,
            chunk_size_seconds=None,
            stream_to_player=_stream_to_player,
            segments=segments,
            interrupt_epoch=interrupt_epoch,
            task_semaphore=task_semaphore,
        )
    finally:
        _release_lock()  # 兜底：异常时也确保锁被释放


async def speak_stream_enhanced(
    text,
    sentence_id,
    is_first_sentence=False,
    chunk_size_seconds=None,
    segments=None,
    interrupt_epoch: int | None = None,
    task_semaphore=None,
):
    """增强的流式语音处理，支持状态管理和首句优化，第一句使用真正的流式播放。"""
    interrupt_epoch = _tts_interrupt_epoch if interrupt_epoch is None else interrupt_epoch
    if _is_interrupted(interrupt_epoch):
        logger.info("[TTS-INTERRUPT] skip stale job before TTS function: %s", sentence_id)
        _release_task_semaphore(task_semaphore, sentence_id)
        return
    try:
        _sha = _compute_text_sha1(text)
    except Exception:
        _sha = "sha_err"
    logger.info(f"[TTS-FUNC-ENTER] func=speak_stream_enhanced id={sentence_id} sha1={_sha} first={is_first_sentence}")

    if _tts_runtime is None:
        logger.error("TTS backend is not initialized; cannot generate speech")
        return

    overall_start = time.time()
    if text and text[-1] not in {',', '.', ',', '.', '?', '!', '?', '!', '。', '！', '？', '、', '，'}:
        text += "。"
    tts_text = _strip_tts_fullwidth_parentheses(text)
    if tts_text != text:
        logger.info(f"[TTS-TEXT-FILTER] removed full-width parentheses: {sentence_id}")
    tts_text = correct_pronunciation_for_tts(tts_text)
    processed_text = tts_text

    logger.info(
        "starting streaming text processing: %s (first_sentence=%s)",
        protected_text(text, limit=50),
        is_first_sentence,
    )
    params = get_sovits_params(tts_text, is_first_sentence)
    params['ref_audio_path'] = _get_ref_audio(tts_text)
    params['prompt_text'] = _get_ref_text(tts_text)

    try:
        first_chunk = True
        audio_chunks = []
        chunk_count = 0
        total_samples = 0
        observed_sample_rate = None
        tracking_streaming = chunk_size_seconds is not None and chunk_size_seconds > 0
        chunk_flush_threshold = 1 if tracking_streaming else 2

        def create_stream_generator():
            return _tts_runtime.infer_stream(
                text=processed_text,
                ref_audio_path=params['ref_audio_path'],
                prompt_text=params['prompt_text'],
                text_language=params["text_language"],
                prompt_language=params["prompt_language"],
                how_to_cut=params["how_to_cut"],
                top_p=params["top_p"],
                top_k=params.get("top_k", 20),
                temperature=params["temperature"],
                sample_steps=params["sample_steps"],
                speed=params["speed"],
                if_sr=params["if_sr"],
                pause_second=params["pause_second"],
                chunk_size_seconds=chunk_size_seconds,
                max_sec_override=params.get("max_sec_override"),
            )

        async for sr, audio_chunk, text_item in async_generator_from_sync(create_stream_generator):
            if _is_interrupted(interrupt_epoch):
                logger.info("[TTS-INTERRUPT] drop streaming synthesis result: %s", sentence_id)
                return
            if first_chunk:
                first_chunk = False
                logger.info(f"[StreamingPlayback] starting streaming synthesis for first sentence: {sentence_id}")
            if audio_chunk is not None and len(audio_chunk) > 0:
                audio_chunks.append(audio_chunk)
                total_samples += len(audio_chunk)
                observed_sample_rate = sr
                chunk_count += 1
                logger.debug(f"[StreamingSynthesis] collected audio chunk: {len(audio_chunk)} samples")
                if len(audio_chunks) >= chunk_flush_threshold and _playback_manager is not None:
                    partial_audio = np.concatenate(audio_chunks)
                    audio_chunks = []
                    payload_text = text if chunk_count <= 2 else ""
                    await _playback_manager.add_streaming_chunk(
                        partial_audio, sr, sentence_id, payload_text,
                        is_first_chunk=(chunk_count <= 2),
                        is_last_chunk=False,
                        playback_epoch=interrupt_epoch,
                    )

        if audio_chunks and _playback_manager is not None:
            if _is_interrupted(interrupt_epoch):
                logger.info("[TTS-INTERRUPT] drop remaining streaming audio: %s", sentence_id)
                return
            remaining_audio = np.concatenate(audio_chunks)
            payload_text = text if chunk_count <= 2 else ""
            await _playback_manager.add_streaming_chunk(
                remaining_audio, sr, sentence_id, payload_text,
                is_first_chunk=(chunk_count <= 2),
                is_last_chunk=True,
                playback_epoch=interrupt_epoch,
            )
            logger.info("[StreamingPlayback] playing remaining audio chunks")

        _update_rtf_ema(
            time.time() - overall_start,
            total_samples,
            observed_sample_rate,
            sentence_id,
        )
        logger.info("[StreamingPlayback] first-sentence streaming synthesis completed; playback handled by PlaybackManager")
    except Exception as e:
        logger.error(f"streaming processing failed: {e}\n{traceback.format_exc()}")
    logger.info(f"streaming processing completed, total time: {time.time() - overall_start:.2f}s")


async def speak_stream_enhanced_asyncio_queue(
    text, sentence_id, is_first_sentence=False, *, force_graph: bool = False,
    on_synthesis_done=None, stream_to_player: bool = False,
    chunk_size_seconds: float = None,
    segments=None,
    interrupt_epoch: int | None = None,
    task_semaphore=None,
):
    """增强版异步队列 TTS，支持首句流式播放和 Graph 串行释放。"""
    interrupt_epoch = _tts_interrupt_epoch if interrupt_epoch is None else interrupt_epoch
    if _is_interrupted(interrupt_epoch):
        logger.info("[TTS-INTERRUPT] skip stale job before TTS function: %s", sentence_id)
        _release_task_semaphore(task_semaphore, sentence_id)
        return
    try:
        _sha = _compute_text_sha1(text)
    except Exception:
        _sha = "sha_err"
    logger.info(
        f"[TTS-FUNC-ENTER] func=speak_stream_enhanced_asyncio_queue "
        f"id={sentence_id} sha1={_sha} first={is_first_sentence} stream={stream_to_player}"
    )
    if is_first_sentence:
        log_latency_marker(logger, "first_tts_enter", id=sentence_id, stream=int(bool(stream_to_player)))

    if _tts_runtime is None:
        logger.error("TTS backend is not initialized; cannot generate speech")
        return

    def tts_producer(loop, queue, producer_text, producer_params, producer_epoch):
        stream = None
        try:
            if _is_interrupted(producer_epoch):
                logger.info("[TTS-INTERRUPT] producer skipped before infer_stream: %s", sentence_id)
                return
            logger.info(f"[WorkerThread] TTS producer started: {sentence_id}")
            stream = _tts_runtime.infer_stream(text=producer_text, **producer_params)
            for item in stream:
                if _is_interrupted(producer_epoch):
                    logger.info("[TTS-INTERRUPT] producer stopped after interrupt: %s", sentence_id)
                    break
                loop.call_soon_threadsafe(queue.put_nowait, item)
            logger.info(f"[WorkerThread] TTS producer completed: {sentence_id}")
        except Exception as e:
            logger.error(f"[WorkerThread] TTS producer failed ({sentence_id}): {e}")
            logger.error(f"--- worker thread traceback ---\n{traceback.format_exc()}")
            loop.call_soon_threadsafe(queue.put_nowait, ("__ERROR__", e))
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug(
                        "[WorkerThread] TTS stream close failed: %s",
                        sentence_id,
                        exc_info=True,
                    )
            loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", None))

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    synthesis_start = time.time()
    tts_text = _strip_tts_fullwidth_parentheses(text)
    if tts_text != text:
        logger.info(f"[TTS-TEXT-FILTER] removed full-width parentheses: {sentence_id}")
    params = get_sovits_params(tts_text, is_first_sentence)
    if force_graph:
        params["enable_cuda_graph"] = True
        params["enable_static_kv"] = True
        logger.info(f"[Graph Serial] force-enabled CUDA Graph parameters: sentence_id={sentence_id}")
    params["ref_audio_path"] = _get_ref_audio(tts_text)
    params["prompt_text"] = _get_ref_text(tts_text)
    processed_text = correct_pronunciation_for_tts(tts_text)

    producer_chunk_size_seconds = (
        chunk_size_seconds if (stream_to_player and is_first_sentence) else None
    )
    params["chunk_size_seconds"] = producer_chunk_size_seconds
    # TTS producer is started after the first-sentence audio cache check below.
    logger.info(f"[Monitor] TTS producer task submitted to worker thread: {sentence_id}")

    _released = False
    def _release_now():
        nonlocal _released
        if _released:
            return
        _released = True
        if on_synthesis_done is not None:
            on_synthesis_done()
        if task_semaphore is not None:
            task_semaphore.release()
            logger.debug(f"[Semaphore] released experimental TTS permit: {sentence_id}")

    if is_first_sentence and _playback_manager is not None:
        try:
            cached = get_first_sentence_audio_cache().lookup(processed_text, params)
        except Exception as exc:
            cached = None
            logger.debug("[FirstSentenceAudioCache] lookup failed: %s", exc)
        if cached is not None:
            cached_sr, cached_audio = cached
            logger.info(
                "[FirstSentenceAudioCache] hit id=%s samples=%s sr=%s",
                sentence_id,
                len(cached_audio),
                cached_sr,
            )
            log_latency_marker(
                logger,
                "first_chunk_generated",
                id=sentence_id,
                samples=len(cached_audio),
                cache=1,
            )
            _release_now()
            if _is_interrupted(interrupt_epoch):
                logger.info("[TTS-INTERRUPT] drop cached first sentence: %s", sentence_id)
                return
            if stream_to_player:
                s1_queue: asyncio.Queue = asyncio.Queue()
                _active_stream_queues.add(s1_queue)
                try:
                    asyncio.create_task(
                        _playback_manager.play_s1_stream(
                            s1_queue,
                            sentence_id,
                            text,
                            playback_epoch=interrupt_epoch,
                        )
                    )
                    await s1_queue.put((cached_sr, cached_audio))
                    await s1_queue.put(None)
                finally:
                    _active_stream_queues.discard(s1_queue)
            else:
                await _playback_manager.add_to_playlist(
                    cached_audio,
                    cached_sr,
                    sentence_id,
                    text,
                    segments=segments,
                    playback_epoch=interrupt_epoch,
                )
            return

    if _is_interrupted(interrupt_epoch):
        logger.info("[TTS-INTERRUPT] skip stale job before producer submit: %s", sentence_id)
        _release_now()
        return
    producer_future = loop.run_in_executor(
        _tts_executor,
        tts_producer,
        loop,
        queue,
        processed_text,
        params,
        interrupt_epoch,
    )
    logger.info(f"[Monitor] TTS producer task submitted: {sentence_id}")

    if stream_to_player and _playback_manager is not None:
        s1_queue: asyncio.Queue = asyncio.Queue()
        _active_stream_queues.add(s1_queue)
        asyncio.create_task(
            _playback_manager.play_s1_stream(
                s1_queue,
                sentence_id,
                text,
                playback_epoch=interrupt_epoch,
            )
        )

        chunk_count = 0
        cache_chunks = []
        cache_sr = None
        stream_total_samples = 0
        stream_sample_rate = None
        try:
            while True:
                item = await queue.get()
                if _is_interrupted(interrupt_epoch):
                    logger.info("[TTS-INTERRUPT] stop streaming playback feed: %s", sentence_id)
                    await s1_queue.put(None)
                    await _wait_producer_after_interrupt(
                        queue,
                        producer_future,
                        sentence_id,
                        force_graph=force_graph,
                    )
                    _release_now()
                    return
                if isinstance(item, tuple) and isinstance(item[0], str) and item[0].startswith("__"):
                    signal, data = item
                    if signal == "__DONE__":
                        break
                    if signal == "__ERROR__":
                        logger.error(f"[S1Streaming] worker thread returned error: {data}")
                        await s1_queue.put(None)
                        _release_now()
                        return

                sr, audio_chunk, text_item = item
                if audio_chunk is not None and len(audio_chunk) > 0:
                    stream_total_samples += len(audio_chunk)
                    stream_sample_rate = sr
                    if is_first_sentence:
                        cache_chunks.append(audio_chunk)
                        cache_sr = sr
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info(
                            f"[TTS-CHUNK] first audio chunk generated (streaming): "
                            f"{sentence_id} ({len(audio_chunk)} samples)"
                        )
                        log_latency_marker(
                            logger,
                            "first_chunk_generated",
                            id=sentence_id,
                            samples=len(audio_chunk),
                        )
                    await s1_queue.put((sr, audio_chunk))

            _release_now()
            _update_rtf_ema(
                time.time() - synthesis_start,
                stream_total_samples,
                stream_sample_rate,
                sentence_id,
            )
            logger.info(f"[Streaming] inference completed, lock released, playback continues: {sentence_id}")
            if cache_chunks and cache_sr:
                full_cached_audio = np.concatenate(cache_chunks)
                get_first_sentence_audio_cache().store(
                    processed_text,
                    params,
                    cache_sr,
                    full_cached_audio,
                    raw_text=tts_text,
                    source="runtime_stream",
                )
            await s1_queue.put(None)
        except Exception as e:
            logger.error(f"[S1Streaming] playback chain failed: {sentence_id} {e}\n{traceback.format_exc()}")
            await s1_queue.put(None)
        finally:
            _active_stream_queues.discard(s1_queue)
            _release_now()
        return

    audio_chunks = []
    sample_rate = None
    while True:
        item = await queue.get()
        if _is_interrupted(interrupt_epoch):
            logger.info("[TTS-INTERRUPT] drop queued synthesis result: %s", sentence_id)
            await _wait_producer_after_interrupt(
                queue,
                producer_future,
                sentence_id,
                force_graph=force_graph,
            )
            _release_now()
            return
        if isinstance(item, tuple) and isinstance(item[0], str) and item[0].startswith("__"):
            signal, data = item
            if signal == "__DONE__":
                break
            if signal == "__ERROR__":
                logger.error(f"[WorkerThread] worker thread returned error: {data}")
                _release_now()
                return
        sr, audio_chunk, text_item = item
        if audio_chunk is not None and len(audio_chunk) > 0:
            if sample_rate is None:
                sample_rate = sr
            elif sample_rate != sr:
                logger.warning(f"[TTS] sentence_id={sentence_id} sample_rate changed: {sample_rate} -> {sr}")
                sample_rate = sr
            audio_chunks.append(audio_chunk)
            if len(audio_chunks) == 1:
                logger.info(f"[TTS-CHUNK] first audio chunk generated: {sentence_id} ({len(audio_chunk)} samples)")
                if is_first_sentence:
                    log_latency_marker(
                        logger,
                        "first_chunk_generated",
                        id=sentence_id,
                        samples=len(audio_chunk),
                    )

    try:
        if audio_chunks and _playback_manager is not None:
            if _is_interrupted(interrupt_epoch):
                logger.info("[TTS-INTERRUPT] drop completed synthesis: %s", sentence_id)
                return
            full_audio_data = np.concatenate(audio_chunks)
            _update_rtf_ema(
                time.time() - synthesis_start,
                len(full_audio_data),
                sample_rate or 24000,
                sentence_id,
            )
            if is_first_sentence:
                get_first_sentence_audio_cache().store(
                    processed_text,
                    params,
                    sample_rate or 24000,
                    full_audio_data,
                    raw_text=tts_text,
                    source="runtime",
                )
            logger.info(f"[Monitor] TTS synthesis completed; submitting playlist item: {sentence_id}")
            sentence_seq = _parse_sentence_seq(sentence_id)
            _release_now()
            if sentence_seq == 2:
                logger.info("[CrosstalkGuard] second sentence synthesized; waiting for first sentence playback to finish")
                await _playback_manager.player_is_ready.wait()
                logger.info("[CrosstalkGuard] first sentence playback finished; starting second sentence")
            await _playback_manager.add_to_playlist(
                full_audio_data,
                sample_rate or 24000,
                sentence_id,
                text,
                segments=segments,
                playback_epoch=interrupt_epoch,
            )
        else:
            logger.warning(f"TTS produced no valid audio data: {sentence_id}")
    finally:
        _release_now()


def _current_cuda_graph_enabled() -> bool:
    return os.environ.get("ENABLE_CUDA_GRAPH", "0") == "1"


def _current_experimental_tts_enabled() -> bool:
    return bool(USE_EXPERIMENTAL_TTS_STREAM)


async def _synthesize_cuda_graph(
    text,
    sentence_id,
    is_first,
    *,
    stream_tts,
    segments,
    interrupt_epoch,
    task_semaphore,
):
    await speak_stream_graph_serial(
        text,
        sentence_id,
        is_first,
        stream_tts=stream_tts,
        segments=segments,
        interrupt_epoch=interrupt_epoch,
        task_semaphore=task_semaphore,
    )


async def _synthesize_experimental(
    text,
    sentence_id,
    is_first,
    *,
    stream_tts,
    segments,
    interrupt_epoch,
    task_semaphore,
):
    del stream_tts
    await speak_stream_enhanced_asyncio_queue(
        text,
        sentence_id,
        is_first,
        segments=segments,
        interrupt_epoch=interrupt_epoch,
        task_semaphore=task_semaphore,
    )


async def _synthesize_enhanced(
    text,
    sentence_id,
    is_first,
    *,
    stream_tts,
    segments,
    interrupt_epoch,
    task_semaphore,
):
    del stream_tts
    await speak_stream_enhanced(
        text,
        sentence_id,
        is_first,
        segments=segments,
        interrupt_epoch=interrupt_epoch,
        task_semaphore=task_semaphore,
    )


_SYNTHESIS_BACKENDS = SynthesisBackends(
    cuda_graph=_synthesize_cuda_graph,
    experimental=_synthesize_experimental,
    default=_synthesize_enhanced,
)

# =============================================================================
# 句子调度工作线程
# =============================================================================
async def play_sentence_worker():
    """从 pending_sentence_items 队列取出句子，创建对应 TTS 合成任务。

    职责简化：仅调度，播放完全交给 PlaybackManager。
    """
    logger.info("sentence processing worker started")
    while True:
        try:
            job = await _utterance_scheduler.next_job(_pending_sentence_items)
            # pending 轮门控：投机轮未确认前扣住，作废则整轮丢弃
            gate = await _gate_job_turn(job.turn_id)
            if gate == "drop":
                logger.info(
                    "[TTS-GATE] dropped sentence of discarded turn: %s turn=%s",
                    job.utterance_id,
                    job.turn_id,
                )
                for _ in range(job.consumed_count):
                    _pending_sentence_items.task_done()
                continue
            sentence_id = job.utterance_id
            sentence = job.text
            is_first = job.is_first
            stream_tts = job.stream_tts
            playback_segments = job.playback_segments() if job.is_merged else None
            job_epoch = (
                int(job.tts_epoch)
                if job.tts_epoch is not None
                else _tts_interrupt_epoch
            )
            if _is_interrupted(job_epoch):
                logger.info(
                    "[TTS-SUPERSEDE] dropped late speech id=%s source=%s "
                    "request_epoch=%s current_epoch=%s",
                    job.utterance_id,
                    job.source,
                    job_epoch,
                    _tts_interrupt_epoch,
                )
                for _ in range(job.consumed_count):
                    _pending_sentence_items.task_done()
                continue
            backend_name, synth = select_synthesis(
                job,
                cuda_graph_enabled=_current_cuda_graph_enabled(),
                experimental_enabled=_current_experimental_tts_enabled(),
                backends=_SYNTHESIS_BACKENDS,
                logger=logger,
            )
            logger.info(
                f"dequeued sentence and preparing TTS task: '{sentence[:30]}...' "
                f"(ID: {sentence_id}, first={is_first}, stream_tts={stream_tts}, "
                f"source={job.source}, turn={job.turn_id or '-'}, backend={backend_name})"
            )
            if job.is_merged:
                logger.info(
                    f"[UtteranceScheduler] merged {job.consumed_count} sentence(s): "
                    f"{[segment.sentence_id for segment in job.segments]}"
                )
            if is_first:
                log_latency_marker(
                    logger,
                    "first_sentence_dequeued",
                    id=sentence_id,
                    chars=len(sentence.strip()),
                )
            try:
                _sha = _compute_text_sha1(sentence)
            except Exception:
                _sha = "sha_err"
            if not hasattr(play_sentence_worker, "_intent_counts"):
                play_sentence_worker._intent_counts = {}
            cnt = play_sentence_worker._intent_counts.get(sentence_id, 0) + 1
            play_sentence_worker._intent_counts[sentence_id] = cnt
            logger.info(f"[TTS-START-INTENT] id={sentence_id} sha1={_sha} first={is_first} intent_count={cnt}")

            speech_candidate = _strip_tts_fullwidth_parentheses(sentence)
            if (
                _is_punctuation_only_sentence(sentence)
                or not speech_candidate.strip()
                or _is_punctuation_only_sentence(speech_candidate)
            ):
                logger.info(
                    "[TTS-SKIP] punctuation-only or parenthetical sentence skipped; "
                    "using a short silence placeholder: %s text=%s",
                    sentence_id,
                    protected_text(sentence),
                )
                if _playback_manager is not None and not _is_interrupted(job_epoch):
                    placeholder_audio = np.zeros(960, dtype=np.float32)  # about 40ms @ 24kHz
                    await _playback_manager.add_to_playlist(
                        placeholder_audio,
                        24000,
                        sentence_id,
                        "",
                        playback_epoch=job_epoch,
                    )
                for _ in range(job.consumed_count):
                    _pending_sentence_items.task_done()
                continue

            task_semaphore = _exp_tts_semaphore
            tts_coro = synth(
                sentence,
                sentence_id,
                is_first,
                stream_tts=stream_tts,
                segments=playback_segments,
                interrupt_epoch=job_epoch,
                task_semaphore=task_semaphore,
            )

            if task_semaphore is not None:
                await task_semaphore.acquire()
                logger.debug(f"[Semaphore] acquired semaphore; starting synthesis: {sentence_id}")

            if _is_interrupted(job_epoch):
                logger.info("[TTS-INTERRUPT] skip stale job before task create: %s", sentence_id)
                _release_task_semaphore(task_semaphore, sentence_id)
                for _ in range(job.consumed_count):
                    _pending_sentence_items.task_done()
                continue

            asyncio.create_task(tts_coro)
            for _ in range(job.consumed_count):
                _pending_sentence_items.task_done()

        except asyncio.CancelledError:
            logger.info("sentence processing worker cancelled")
            break
        except Exception as e:
            logger.error(f"sentence processing worker failed: {e}", exc_info=True)


# =============================================================================
# CUDA Graph 预热
# =============================================================================

async def warmup_graph_pipeline():
    """在启用 CUDA Graph 时进行一次隐式预热（不产生可听播放）。"""
    try:
        if os.environ.get('ENABLE_CUDA_GRAPH', '0') != '1':
            return
        if _tts_runtime is None:
            return
        logger.info("[Graph Warmup] starting CUDA Graph warmup")

        if _llm_warmup_fn is not None:
            try:
                warmup_question = "ごく簡単に自己紹介を一文だけでして。"
                _llm_warmup_fn(warmup_question)
                logger.info("[Graph Warmup] LLM warmup triggered")
            except Exception as e:
                logger.warning(f"[Graph Warmup] LLM warmup failed: {e}")

        try:
            warmup_text = "これはテストです。" if TTS_OUTPUT_LANGUAGE != "英文" else "This is a warmup test."
            params = get_sovits_params(warmup_text, is_first_sentence=True)
            params['ref_audio_path'] = _get_ref_audio(warmup_text)
            params['prompt_text'] = _get_ref_text(warmup_text)
            logger.info("[Graph Warmup] starting local TTS warmup inference")
            start_t = time.time()
            await asyncio.to_thread(
                _tts_runtime.infer,
                warmup_text,
                params['ref_audio_path'],
                params['prompt_text'],
                params["text_language"],
                params["prompt_language"],
                params["how_to_cut"],
                params.get("top_k", 20),
                params["top_p"],
                params["temperature"],
                params["speed"],
                params["sample_steps"],
                params.get("ref_free", False),
                params["pause_second"],
                params.get("if_freeze", False),
                None,
                params.get("if_sr", False),
                True,   # enable_cuda_graph
                True,   # enable_static_kv
                params.get("max_sec_override"),
            )
            logger.info(f"[Graph Warmup] local TTS warmup completed in {time.time() - start_t:.2f}s; audio discarded")

            stream_warmup_text = (
                ("This is a streaming warmup. Preparing for stable first response."
                 if TTS_OUTPUT_LANGUAGE == "英文" else
                 "これは流式音声の事前準備です。最初の応答を安定させるために短く温めます。")
            )
            stream_params = get_sovits_params(stream_warmup_text, is_first_sentence=True)
            stream_params['ref_audio_path'] = _get_ref_audio(stream_warmup_text)
            stream_params['prompt_text'] = _get_ref_text(stream_warmup_text)

            def _stream_warmup_consume(chunk_seconds: float):
                consumed = 0
                for sr, audio_chunk, text_item in _tts_runtime.infer_stream(
                    text=stream_warmup_text,
                    ref_audio_path=stream_params['ref_audio_path'],
                    prompt_text=stream_params['prompt_text'],
                    text_language=stream_params["text_language"],
                    prompt_language=stream_params["prompt_language"],
                    how_to_cut=stream_params["how_to_cut"],
                    top_k=stream_params.get("top_k", 20),
                    top_p=stream_params["top_p"],
                    temperature=stream_params["temperature"],
                    speed=stream_params["speed"],
                    sample_steps=stream_params["sample_steps"],
                    ref_free=stream_params.get("ref_free", False),
                    pause_second=stream_params["pause_second"],
                    if_freeze=stream_params.get("if_freeze", False),
                    inp_refs=None,
                    if_sr=stream_params.get("if_sr", False),
                    enable_cuda_graph=True,
                    enable_static_kv=True,
                    chunk_size_seconds=chunk_seconds,
                    max_sec_override=stream_params.get("max_sec_override"),
                ):
                    if audio_chunk is None or len(audio_chunk) == 0:
                        continue
                    consumed += 1
                    if consumed >= 2:
                        break
                return consumed

            stream_chunk_variants = (0.35, 0.45, 0.60, 0.75)
            for stream_chunk_seconds in stream_chunk_variants:
                logger.info(
                    f"[Graph Warmup] starting first-sentence streaming warmup (chunk={stream_chunk_seconds:.2f}s)"
                )
                start_stream_t = time.time()
                consumed = await asyncio.to_thread(_stream_warmup_consume, stream_chunk_seconds)
                logger.info(
                    f"[Graph Warmup] first-sentence streaming warmup completed in {time.time() - start_stream_t:.2f}s; "
                    f"warmed {consumed} chunk(s)"
                )
        except Exception as e:
            logger.warning(f"[Graph Warmup] TTS warmup failed: {e}")

        logger.info("[Graph Warmup] warmup finished; future turns will reuse Graph / static KV")
    except Exception as e:
        logger.warning(f"[Graph Warmup] warmup coroutine failed: {e}")
