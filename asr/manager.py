"""
ASR 管理模块 — Silero VAD + 可插拔 ASR 后端

Conversation 后端通过公开 registry 和 ASR_BACKEND 选择（默认 qwen3_asr）。
WakeService 使用独立 WAKE_ASR_BACKEND，可与 Conversation 后端并存。

公开接口与历史版本完全兼容：
  ASRManager.listen_for_speech() / set_language() / set_microphone_index()
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Optional

import numpy as np

from config.log_privacy import protected_text

# torch is a local-model tier (T2b) dependency. Remote ASR installs (L2) have
# no torch: every use below is guarded so device selection degrades to CPU
# (remote backends ignore the device hint anyway).
from config.settings import (
    ASR_BACKEND as _CFG_ASR_BACKEND,
    ASR_CONTEXT as _CFG_ASR_CONTEXT,
    ASR_LANGUAGE as _CFG_ASR_LANGUAGE,
    ASR_ENERGY_END_MS as _CFG_ASR_ENERGY_END_MS,
    ASR_ENERGY_END_RMS as _CFG_ASR_ENERGY_END_RMS,
    ASR_ENERGY_START_RMS as _CFG_ASR_ENERGY_START_RMS,
    ASR_HANDOFF_MAX_CAPTURE_SECONDS as _CFG_ASR_HANDOFF_MAX_CAPTURE_SECONDS,
    ASR_LISTEN_TIMEOUT_SECONDS as _CFG_ASR_LISTEN_TIMEOUT_SECONDS,
    ASR_MAX_SPEECH_SECONDS as _CFG_ASR_MAX_SPEECH_SECONDS,
    ASR_MIN_SPEECH_MS as _CFG_ASR_MIN_SPEECH_MS,
    ASR_PREROLL_MS as _CFG_ASR_PREROLL_MS,
    ASR_SPEECH_PAD_MS as _CFG_ASR_SPEECH_PAD_MS,
    ASR_SPECULATIVE_END_MS as _CFG_ASR_SPECULATIVE_END_MS,
    ASR_SPECULATIVE_TRANSCRIBE as _CFG_ASR_SPECULATIVE_TRANSCRIBE,
    ASR_VAD_SILENCE_MS as _CFG_ASR_VAD_SILENCE_MS,
    ASR_VAD_THRESHOLD as _CFG_ASR_VAD_THRESHOLD,
    QWEN3_ASR_DEVICE as _CFG_QWEN3_ASR_DEVICE,
)
from asr.microphone import configured_device_index
from asr.mic_input_service import get_mic_input_service
from asr.text_filter import is_asr_prompt_leak
from asr.backend import ASRBackendFatalError
from asr.registry import create_asr_backend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 录音 / VAD 参数
# ---------------------------------------------------------------------------
_SAMPLE_RATE    = 16000
_CHUNK_SAMPLES  = 512       # Silero VAD 6.x 在 16kHz 下要求每块 512 samples (32ms)
_VAD_THRESHOLD  = float(_CFG_ASR_VAD_THRESHOLD)
_SILENCE_MS     = int(_CFG_ASR_VAD_SILENCE_MS)
_SPEECH_PAD_MS  = int(_CFG_ASR_SPEECH_PAD_MS)
_MIN_SPEECH_MS  = int(_CFG_ASR_MIN_SPEECH_MS)
_MAX_SPEECH_SEC = float(_CFG_ASR_MAX_SPEECH_SECONDS)
_LISTEN_TIMEOUT = float(_CFG_ASR_LISTEN_TIMEOUT_SECONDS)
_PREROLL_MS     = int(_CFG_ASR_PREROLL_MS)
_ENERGY_END_RMS = float(_CFG_ASR_ENERGY_END_RMS)
_ENERGY_START_RMS = float(_CFG_ASR_ENERGY_START_RMS)
_ENERGY_END_MS  = int(_CFG_ASR_ENERGY_END_MS)
_HANDOFF_MAX_CAPTURE_SEC = float(_CFG_ASR_HANDOFF_MAX_CAPTURE_SECONDS)
_PREROLL_CHUNKS = max(1, int(_PREROLL_MS / (_CHUNK_SAMPLES / _SAMPLE_RATE * 1000)))
_SPECULATIVE_ENABLED = bool(_CFG_ASR_SPECULATIVE_TRANSCRIBE)
_SPECULATIVE_END_MS = int(_CFG_ASR_SPECULATIVE_END_MS)


class _SpeculativeTranscription:
    """两段式投机端点的第一段：短静音时并行转写已捕获的部分音频。

    - submit()：capture 线程在短静音（_SPECULATIVE_END_MS）触发，音频送后台线程转写。
    - invalidate()：说话恢复 / 段被丢弃时作废（无法中止 sidecar 调用，只丢结果）。
    - consume()：最终端点确认后，若投机仍有效则等待并复用其文本；
      返回 None 表示未命中，调用方按原路径重新转写完整音频。

    后端 transcribe 自带 IO 锁，投机调用与正式调用天然串行：
    未命中时的正式转写会等待在跑的投机调用结束（部分音频转写耗时短，可接受）。
    """

    def __init__(self, backend, context: str, sample_rate: int, on_result=None) -> None:
        self._backend = backend
        self._context = context
        self._sample_rate = sample_rate
        self._lock = threading.Lock()
        self._done: threading.Event | None = None
        self._valid = False
        self._submitted_at = 0.0
        self._audio_samples = 0
        self._text: Optional[str] = None
        self._error: Optional[Exception] = None
        # 投机文本回调（切片 D2）：转写完成且投机仍有效时，从工作线程回调
        # on_result(text)——供投机 LLM 启动使用。回调异常绝不影响识别路径。
        self._on_result = on_result

    def submit(self, audio: np.ndarray) -> None:
        with self._lock:
            if self._done is not None and not self._done.is_set():
                # 上一次投机（已作废）仍在 sidecar 中执行：不排队新任务，
                # 避免多次中途停顿导致投机请求堆积。
                return
            done = threading.Event()
            self._done = done
            self._text = None
            self._error = None
            self._valid = True
            self._audio_samples = int(audio.shape[0])
            self._submitted_at = time.monotonic()
        audio_ms = self._audio_samples / self._sample_rate * 1000.0
        logger.info("[ASR-SPEC] probable end: speculative transcribe submitted (%.0fms audio)", audio_ms)
        threading.Thread(
            target=self._run, args=(audio, done), daemon=True, name="asr-speculative"
        ).start()

    def _run(self, audio: np.ndarray, done: threading.Event) -> None:
        try:
            self._text = self._backend.transcribe(audio, self._sample_rate, context=self._context)
        except Exception as exc:
            self._error = exc
        finally:
            done.set()
        # 投机文本回调：仅在成功、非空且投机仍有效时触发
        if self._error is None and self._text and self._on_result is not None:
            with self._lock:
                still_valid = self._valid
            if still_valid:
                try:
                    self._on_result(self._text)
                except Exception:
                    logger.debug("[ASR-SPEC] on_result callback failed", exc_info=True)

    def invalidate(self, reason: str = "speech_resumed") -> None:
        with self._lock:
            if self._valid:
                self._valid = False
                logger.info("[ASR-SPEC] speculation invalidated: %s", reason)

    def consume(self, final_audio: np.ndarray, timeout: float = 8.0) -> Optional[str]:
        with self._lock:
            done = self._done
            valid = self._valid and done is not None
        if not valid:
            return None
        if int(final_audio.shape[0]) < self._audio_samples:
            # 最终音频比投机音频短：不是同一段，防御性放弃
            return None
        waited_from = time.monotonic()
        if not done.wait(timeout=timeout):
            logger.warning("[ASR-SPEC] speculative transcribe timed out; falling back to full transcribe")
            return None
        if self._error is not None:
            logger.warning("[ASR-SPEC] speculative transcribe failed (%s); falling back", self._error)
            return None
        text = self._text
        if not text:
            return None
        post_endpoint_ms = (time.monotonic() - waited_from) * 1000.0
        total_ms = (time.monotonic() - self._submitted_at) * 1000.0
        logger.info(
            "[ASR-SPEC] hit: reused speculative text "
            "(transcribe total %.0fms, post-endpoint wait %.0fms)",
            total_ms,
            post_endpoint_ms,
        )
        return text

def _build_backend(name: str):
    """Compatibility alias for the public backend registry."""
    return create_asr_backend(name)


# ---------------------------------------------------------------------------
# ASRManager
# ---------------------------------------------------------------------------

class ASRManager:
    """
    麦克风管理 + Silero VAD 分段 + ASR 后端推理。

    Parameters
    ----------
    backend : str
        后端名称；未传入时使用启动配置 ASR_BACKEND。
    """

    MICROPHONE_DEVICE_INDEX = configured_device_index()

    def __init__(self, backend: Optional[str] = None) -> None:
        backend_name = backend or _CFG_ASR_BACKEND or "qwen3_asr"

        self.language = str(_CFG_ASR_LANGUAGE or "auto")
        self.context: str = _CFG_ASR_CONTEXT  # 热词/领域提示，Qwen3-ASR 用作 system prompt
        self._mic_index: Optional[int] = self.MICROPHONE_DEVICE_INDEX
        self._init_lock = threading.Lock()
        # 可选：外部注入的 TTS 播放状态查询函数，返回 True 表示 TTS 正在播放
        # 播放期间暂停 pre-roll 写入，避免将 TTS 输出混入 ASR 输入
        self._tts_playing_fn = None
        self._tts_block_mic_fn = None
        # 可选：VAD 检测到语音起始时调用（用于 barge-in：打断正在播放的 TTS）
        self._on_speech_start_fn = None
        # 可选：投机文本回调（切片 D2，见 set_speculative_text_callback）
        self._speculative_text_fn = None

        # 后端就绪事件：后台线程加载完成后 set()，listen_for_speech() 等待它
        self._backend_ready = threading.Event()
        self._backend_load_err: Optional[str] = None

        # --- VAD ---（轻量，同步加载）
        self._vad_model = None
        self._init_vad()

        # --- ASR 后端：后台异步加载，不阻塞主进程启动 ---
        self._backend_name: str = backend_name
        self._backend = _build_backend(backend_name)
        set_language = getattr(self._backend, "set_language", None)
        if callable(set_language):
            set_language(self.language)
        logger.info(f"[ASR] backend {backend_name} loading in background...")
        threading.Thread(
            target=self._load_backend_bg,
            daemon=True,
            name="asr-backend-load",
        ).start()

    def close(self) -> None:
        try:
            close = getattr(self._backend, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _load_backend_bg(self) -> None:
        """在后台线程中加载 ASR 后端，完成后 set _backend_ready。"""
        try:
            cuda_available, torch_version = self._torch_runtime()
            requested_device = _CFG_QWEN3_ASR_DEVICE
            if requested_device in {"cuda", "cuda:0", "gpu"}:
                device = "cuda"
            elif requested_device == "cpu":
                device = "cpu"
            else:
                device = "cuda" if cuda_available else "cpu"
            if torch_version:
                logger.info(
                    "[ASR] torch runtime exe=%s torch=%s cuda_available=%s "
                    "cuda_version=%s visible=%s requested_device=%s selected_device=%s",
                    sys.executable,
                    torch_version,
                    cuda_available,
                    self._torch_cuda_version(),
                    os.environ.get("CUDA_VISIBLE_DEVICES"),
                    requested_device or "auto",
                    device,
                )
            else:
                logger.info(
                    "[ASR] torch absent (remote ASR install); device=%s backend=%s",
                    device,
                    self._backend_name,
                )
            self._backend.load(device)
            logger.info(f"[ASR] backend {self._backend_name} loaded (device={device})")
        except Exception as e:
            self._backend_load_err = str(e)
            logger.error(f"[ASR] backend load failed: {e}")
        finally:
            self._backend_ready.set()

    @staticmethod
    def _torch_runtime() -> tuple[bool, str]:
        """Return (cuda_available, torch_version); (False, "") without torch."""
        try:
            import torch

            return bool(torch.cuda.is_available()), str(torch.__version__)
        except ImportError:
            return False, ""

    @staticmethod
    def _torch_cuda_version() -> str | None:
        try:
            import torch

            return getattr(torch.version, "cuda", None)
        except ImportError:
            return None

    def _select_backend_device(self) -> str:
        requested_device = _CFG_QWEN3_ASR_DEVICE
        if requested_device in {"cuda", "cuda:0", "gpu"}:
            return "cuda"
        if requested_device == "cpu":
            return "cpu"
        cuda_available, _ = self._torch_runtime()
        return "cuda" if cuda_available else "cpu"

    def _recover_backend_after_failure(self, exc: Exception) -> bool:
        """Rebuild the current backend after a fatal runtime failure."""
        with self._init_lock:
            logger.warning("[ASR] backend fatal failure; rebuilding %s: %s", self._backend_name, exc)
            self._backend_ready.clear()
            self._backend_load_err = None
            old_backend = self._backend
            new_backend = _build_backend(self._backend_name)
            set_language = getattr(new_backend, "set_language", None)
            if callable(set_language):
                set_language(self.language)
            try:
                new_backend.load(self._select_backend_device())
            except Exception as load_exc:
                self._backend_load_err = str(load_exc)
                self._backend_ready.set()
                logger.error("[ASR] backend recovery failed: %s", load_exc)
                try:
                    close = getattr(new_backend, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
                return False
            self._backend = new_backend
            self._backend_ready.set()
            try:
                close = getattr(old_backend, "close", None)
                if callable(close):
                    close()
            except Exception:
                logger.debug("[ASR] old backend close skipped during recovery", exc_info=True)
            logger.info("[ASR] backend recovery complete: %s", self._backend_name)
            return True

    def _init_vad(self) -> None:
        try:
            from silero_vad import load_silero_vad

            self._vad_model = load_silero_vad()
            self._vad_model.eval()
            logger.info("[ASR] Silero VAD loaded")
        except Exception as e:
            # silero-vad pulls torch (L3 realtime tier). Remote-ASR installs
            # (L2) have neither: capture falls back to energy endpointing.
            self._vad_model = None
            logger.warning(
                "[ASR] Silero VAD unavailable (%s); ASR capture falls back to "
                "energy endpointing (install -e '.[vad'] for VAD-based "
                "endpointing and barge-in)",
                e,
            )

    # ------------------------------------------------------------------
    # 公开接口（与历史版本兼容）
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """后端是否已加载完成（可用于 GUI 显示加载状态）。"""
        return self._backend_ready.is_set() and self._backend_load_err is None

    def wait_until_ready(self, timeout: float = 120.0) -> bool:
        """阻塞等待后端就绪，返回是否成功。供需要同步等待的场景使用。"""
        return self._backend_ready.wait(timeout=timeout)

    def listen_for_speech(self, max_retries: int = 2) -> Optional[str]:
        """监听语音并返回识别文本；无有效输入返回 None。"""
        # 后端尚未就绪时等待（最多 120s），不阻塞主进程启动
        if not self._backend_ready.is_set():
            logger.info("[ASR] backend is loading; waiting for readiness...")
            if not self._backend_ready.wait(timeout=120.0):
                logger.error("[ASR] backend startup timed out; skipping this recognition attempt")
                return None
        if self._backend_load_err:
            logger.error(f"[ASR] backend unavailable: {self._backend_load_err}")
            return None

        logger.info("[ASR] listening started")
        for attempt in range(max_retries + 1):
            result = self._listen_once(listen_timeout_attempt=attempt)
            if result:
                return result
            if attempt < max_retries:
                logger.debug(f"[ASR] attempt {attempt + 1} recognized no speech; retrying...")
        logger.info("[ASR] no speech recognized after all attempts")
        return None

    def set_language(self, language_code: str) -> None:
        """Set the conversation recognizer language when supported."""
        self.language = str(language_code or "auto")
        setter = getattr(self._backend, "set_language", None)
        if callable(setter):
            setter(self.language)
        logger.info("[ASR] conversation language set: %s", self.language)

    def set_microphone_index(self, index: Optional[int]) -> None:
        """手动切换麦克风设备索引，传 None 恢复自动选择。"""
        self._mic_index = index
        ASRManager.MICROPHONE_DEVICE_INDEX = index
        logger.info(f"[ASR] microphone switched: [{index}]")

    def set_speculative_text_callback(self, fn) -> None:
        """注册投机文本回调（切片 D2）：投机转写完成且未作废时以
        fn(text) 从 ASR 工作线程调用。传 None 关闭。"""
        self._speculative_text_fn = fn

    def switch_backend(self, name: str) -> None:
        """热切换 ASR 后端，线程安全。新后端加载完成后才替换旧后端。"""
        if name == self._backend_name:
            logger.info(f"[ASR] backend is already {name}; no switch needed")
            return
        with self._init_lock:
            logger.info(f"[ASR] switching backend: {self._backend_name} -> {name}")
            self._backend_ready.clear()
            self._backend_load_err = None
            new_backend = _build_backend(name)
            set_language = getattr(new_backend, "set_language", None)
            if callable(set_language):
                set_language(self.language)
            requested_device = _CFG_QWEN3_ASR_DEVICE
            if requested_device in {"cuda", "cuda:0", "gpu"}:
                device = "cuda"
            elif requested_device == "cpu":
                device = "cpu"
            else:
                cuda_available, _ = self._torch_runtime()
                device = "cuda" if cuda_available else "cpu"
            new_backend.load(device)
            old = self._backend
            self._backend = new_backend
            self._backend_name = name
            self._backend_ready.set()
            try:
                close = getattr(old, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            logger.info(f"[ASR] backend switch complete: {name}")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _listen_once(self, *, listen_timeout_attempt: int = 0) -> Optional[str]:
        def _should_block_mic() -> bool:
            try:
                if self._tts_block_mic_fn is not None and self._tts_block_mic_fn():
                    return True
                if self._tts_playing_fn is not None and self._tts_playing_fn():
                    return True
            except Exception:
                return False
            return False

        mic_service = get_mic_input_service()
        mic_service.start(preferred_index=self._mic_index)
        speculative = (
            _SpeculativeTranscription(
                self._backend, self.context, _SAMPLE_RATE,
                on_result=self._speculative_text_fn,
            )
            if (
                _SPECULATIVE_ENABLED
                and _SPECULATIVE_END_MS > 0
                and bool(
                    getattr(
                        self._backend,
                        "supports_speculative_transcription",
                        True,
                    )
                )
            )
            else None
        )
        audio = mic_service.capture_utterance(
            vad_model=self._vad_model,
            threshold=_VAD_THRESHOLD,
            timeout_s=_LISTEN_TIMEOUT,
            min_silence_ms=_SILENCE_MS,
            speech_pad_ms=_SPEECH_PAD_MS,
            min_speech_ms=_MIN_SPEECH_MS,
            max_speech_sec=_MAX_SPEECH_SEC,
            preroll_ms=_PREROLL_MS,
            energy_end_rms=_ENERGY_END_RMS,
            energy_end_ms=_ENERGY_END_MS,
            energy_start_rms=_ENERGY_START_RMS,
            handoff_max_capture_sec=_HANDOFF_MAX_CAPTURE_SEC,
            block_mic_fn=_should_block_mic,
            on_speech_start=self._on_speech_start_fn,
            consume_handoff=True,
            probable_end_silence_ms=_SPECULATIVE_END_MS if speculative is not None else 0,
            on_probable_end=speculative.submit if speculative is not None else None,
            on_probable_end_cancelled=speculative.invalidate if speculative is not None else None,
        )
        self._mic_index = mic_service.mic_index
        if audio is None:
            msg = "[ASR] listen timed out; no speech detected"
            if listen_timeout_attempt <= 0:
                logger.info(msg)
            else:
                logger.debug(msg)
            return None
        # 两段式端点：短静音阶段的投机转写命中时直接复用，隐藏转写延迟
        text = speculative.consume(audio) if speculative is not None else None
        if text is None:
            try:
                text = self._backend.transcribe(audio, _SAMPLE_RATE, context=self.context)
            except ASRBackendFatalError as exc:
                if not self._recover_backend_after_failure(exc):
                    return None
                try:
                    logger.info("[ASR] retrying captured audio after backend recovery")
                    text = self._backend.transcribe(audio, _SAMPLE_RATE, context=self.context)
                except Exception as retry_exc:
                    logger.error("[ASR] retry after backend recovery failed: %s", retry_exc)
                    return None
        if is_asr_prompt_leak(text or "", context=self.context):
            logger.warning(
                "[ASR] dropped context-prompt leak from recognizer: %s",
                protected_text(text),
            )
            return None
        return text
