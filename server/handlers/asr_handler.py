"""Adapter for ASR, wrapping ASRManager with a non-blocking listen loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from collections.abc import Callable

from config.settings import ASR_BACKEND, ASR_IDLE_UNLOAD_SECONDS, ASR_TURN_COMPLETE_TIMEOUT_SECONDS
from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)


class AsrHandler(RequestHandler):
    methods = [Method.ASR_START, Method.ASR_STOP]

    def __init__(self) -> None:
        self._asr_manager = None
        self._asr_manager_factory: Callable[[], Any] | None = None
        self._init_lock: asyncio.Lock | None = None
        self._listen_task: asyncio.Task | None = None
        self._unload_task: asyncio.Task | None = None
        self._on_unload: Callable[[Any], None] | None = None
        self._on_recognized: Callable[[dict[str, Any]], Any] | None = None
        self._on_listening_stopped: Callable[[dict[str, Any]], Any] | None = None
        self._on_ready_to_listen: Callable[[dict[str, Any]], Any] | None = None
        self._tts_playing_fn: Callable[[], bool] | None = None
        self._active = False
        self._one_shot = False
        self._source = ""
        self._wake_payload: dict[str, Any] = {}
        self._source_payload: dict[str, Any] = {}
        self._awake_until = 0.0
        self._awake_seconds = 0.0
        self._ready_callback_sent = False
        self._waiting_turn_complete = False
        self._finish_after_turn_complete = False
        self._desired_backend = str(ASR_BACKEND or "qwen3_asr").strip().lower()

    @property
    def backend_name(self) -> str:
        manager = self._asr_manager
        if manager is not None:
            current = str(getattr(manager, "_backend_name", "") or "").strip().lower()
            if current:
                return current
        return self._desired_backend

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def source(self) -> str:
        return self._source

    @staticmethod
    def _normalize_chat_mic_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        normalized = dict(payload or {})
        mode = str(normalized.get("mode") or "").strip().lower()
        if mode not in {"manual", "auto"}:
            mode = "auto" if normalized.get("auto_send") is True else "manual"
        normalized["mode"] = mode
        normalized["auto_send"] = mode == "auto"
        return normalized

    def _chat_mic_mode(self) -> str:
        if self._source != "chat_mic":
            return ""
        mode = str(self._source_payload.get("mode") or "").strip().lower()
        if mode in {"manual", "auto"}:
            return mode
        return "auto" if self._source_payload.get("auto_send") is True else "manual"

    def is_continuous_chat_mic(self) -> bool:
        return self._active and self._source == "chat_mic" and self._chat_mic_mode() == "auto"

    async def set_backend(self, value: object) -> str:
        from asr.registry import asr_backend_ids

        name = str(value or "").strip().lower()
        if name not in set(asr_backend_ids()):
            raise ValueError(f"unsupported ASR backend: {value!r}")
        if self._active:
            raise RuntimeError("stop ASR listening before switching backend")
        self._desired_backend = name
        if self._asr_manager is not None:
            # Model loading may exceed the WebSocket request timeout. Release
            # the idle backend now and let the existing lazy factory load the
            # selected backend on the next ASR session.
            await self.unload()
        return name

    def configure(
        self,
        asr_manager=None,
        asr_manager_factory: Callable[[], Any] | None = None,
        on_unload: Callable[[Any], None] | None = None,
        on_recognized: Callable[[dict[str, Any]], Any] | None = None,
        on_listening_stopped: Callable[[dict[str, Any]], Any] | None = None,
        on_ready_to_listen: Callable[[dict[str, Any]], Any] | None = None,
        tts_playing_fn: Callable[[], bool] | None = None,
    ) -> None:
        self._asr_manager = asr_manager
        self._asr_manager_factory = asr_manager_factory
        self._on_unload = on_unload
        self._on_recognized = on_recognized
        self._on_listening_stopped = on_listening_stopped
        self._on_ready_to_listen = on_ready_to_listen
        self._tts_playing_fn = tts_playing_fn

    async def _ensure_asr_manager(self):
        if self._unload_task:
            self._unload_task.cancel()
            self._unload_task = None
        if self._asr_manager is not None:
            return self._asr_manager
        if self._asr_manager_factory is None:
            return None
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._asr_manager is not None:
                return self._asr_manager
            await bus.emit(Method.ASR_STATUS, {"status": "loading"})
            self._asr_manager = await asyncio.to_thread(self._asr_manager_factory)
            return self._asr_manager

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.ASR_START:
            return await self._start(params)
        if method == Method.ASR_STOP:
            return await self._stop(params)
        return None

    async def _start(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self.start_listening(params)

    async def start_listening(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        requested_source = str(params.get("source") or "").strip()
        requested_payload = dict(
            params.get("source_payload")
            or params.get("sourcePayload")
            or {}
        )
        if requested_source == "chat_mic":
            requested_payload = self._normalize_chat_mic_payload(requested_payload)
        requested_mode = str(requested_payload.get("mode") or "").strip().lower()

        if self._active:
            if requested_source == "chat_mic" and self._source == "chat_mic":
                current_mode = self._chat_mic_mode()
                if current_mode == requested_mode:
                    await self._emit_listening_status()
                    return {
                        "status": "listening",
                        "source": "chat_mic",
                        "mode": current_mode,
                    }
                logger.info(
                    "switching chat mic mode: %s -> %s",
                    current_mode or "unknown",
                    requested_mode or "unknown",
                )
                await self.stop_listening(reason="chat_mic_mode_switch")
            elif requested_source == "chat_mic" and self._source == "wake":
                logger.info("chat mic preempting active wake ASR")
                await self.stop_listening(reason="preempted_by_chat_mic")
            elif requested_source == "wake" and self._source == "chat_mic":
                return {
                    "status": "ignored",
                    "source": self._source,
                    "reason": "chat_mic_active",
                }
            elif requested_source and requested_source == self._source:
                if requested_source == "wake":
                    self._wake_payload = dict(params.get("wake") or {})
                    self._source_payload = requested_payload
                    self._arm_awake(params)
                    await self._emit_listening_status()
                    return {
                        "status": "awake",
                        "awake_seconds": self._awake_seconds,
                    }
                await self._emit_listening_status()
                return {
                    "status": "listening",
                    "source": self._source,
                }
            else:
                return {
                    "status": "already_listening",
                    "source": self._source,
                }

        try:
            asr_manager = await self._ensure_asr_manager()
        except Exception as exc:
            logger.exception("asr lazy init failed")
            await bus.emit(Method.ASR_STATUS, {"status": "error", "error": str(exc)})
            return {"status": "error", "error": str(exc)}
        if asr_manager is None:
            return {"status": "error", "error": "ASR manager unavailable"}

        self._active = True
        self._source = requested_source
        self._wake_payload = dict(params.get("wake") or {})
        self._source_payload = requested_payload

        if self._source == "chat_mic":
            # Manual Mic is always one-shot; Auto Mic is always continuous.
            self._one_shot = self._chat_mic_mode() == "manual"
        elif self._source == "barge_in":
            self._one_shot = True
        else:
            self._one_shot = bool(params.get("one_shot", False))

        self._finish_after_turn_complete = bool(
            params.get("finish_after_turn_complete", self._source == "wake")
        )
        self._arm_awake(params)
        self._ready_callback_sent = False
        await self._emit_listening_status()
        self._listen_task = asyncio.create_task(self._listen_loop())
        return {
            "status": "awake" if self._is_awake_session() else "listening",
            "source": self._source,
            **({"mode": self._chat_mic_mode()} if self._source == "chat_mic" else {}),
        }

    async def _stop(self, params: dict[str, Any]) -> dict[str, Any]:
        expected_source = str((params or {}).get("source") or "")
        if expected_source and self._source and expected_source != self._source:
            return {"status": "ignored", "source": self._source, "expected_source": expected_source}
        reason = str((params or {}).get("reason") or "manual_stop")
        return await self.stop_listening(reason=reason)

    async def stop_listening(self, reason: str = "manual_stop") -> dict[str, Any]:
        if not self._active and self._listen_task is None:
            return {"status": "idle", "reason": reason}

        manager = self._asr_manager
        if manager is not None:
            cancel_capture = getattr(manager, "cancel_capture", None)
            if callable(cancel_capture):
                try:
                    cancel_capture()
                except Exception:
                    logger.exception("failed to cancel active ASR capture")

        self._active = False
        self._one_shot = False

        task = self._listen_task
        self._listen_task = None
        current_task = asyncio.current_task()
        if task is not None and task is not current_task:
            task.cancel()

        await self._finish_listening(reason)
        return {"status": "stopped", "reason": reason}

    def _arm_awake(self, params: dict[str, Any]) -> None:
        awake_seconds = float(params.get("awake_seconds") or 0.0)
        if self._source == "wake" and awake_seconds > 0:
            self._one_shot = False
            self._awake_seconds = awake_seconds
            self._awake_until = time.monotonic() + awake_seconds

    def _is_awake_session(self) -> bool:
        return self._source == "wake" and self._awake_until > 0

    def _clear_session_state(self) -> dict[str, Any]:
        info = {
            "source": self._source,
            "wake": self._wake_payload,
            "source_payload": self._source_payload,
            "awake": self._is_awake_session(),
        }
        self._one_shot = False
        self._source = ""
        self._wake_payload = {}
        self._source_payload = {}
        self._awake_until = 0.0
        self._awake_seconds = 0.0
        self._ready_callback_sent = False
        self._waiting_turn_complete = False
        self._finish_after_turn_complete = False
        return info

    async def _emit_listening_status(self) -> None:
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_asr_listening(
                source=self._source or "",
                hot_window=self._is_awake_session(),
            )
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
        if self._is_awake_session():
            remaining = max(0.0, self._awake_until - time.monotonic())
            await bus.emit(
                Method.ASR_STATUS,
                {
                    "status": "awake",
                    "source": "wake",
                    "awake_remaining": remaining,
                    "source_payload": self._source_payload,
                },
            )
            return
        await bus.emit(
            Method.ASR_STATUS,
            {"status": "listening", "source": self._source or "", "source_payload": self._source_payload},
        )

    async def _wait_until_manager_ready(self, asr_manager) -> bool:
        if getattr(asr_manager, "is_ready", True):
            return True

        await bus.emit(
            Method.ASR_STATUS,
            {"status": "loading", "source": self._source or ""},
        )

        while self._active and not getattr(asr_manager, "is_ready", True):
            load_error = str(
                getattr(asr_manager, "load_error", "")
                or getattr(asr_manager, "_backend_load_err", "")
                or ""
            ).strip()
            if load_error:
                await bus.emit(
                    Method.ASR_STATUS,
                    {
                        "status": "error",
                        "source": self._source or "",
                        "error": load_error,
                    },
                )
                self._active = False
                await self._finish_listening("backend_unavailable")
                return False

            if self._is_awake_session() and time.monotonic() >= self._awake_until:
                self._active = False
                await self._finish_listening("awake_timeout")
                return False
            await asyncio.sleep(0.1)
        return self._active

    async def _notify_ready_to_listen(self) -> None:
        if self._ready_callback_sent or self._on_ready_to_listen is None:
            return
        self._ready_callback_sent = True
        payload = {
            "source": self._source,
            "wake": self._wake_payload,
            "awake": self._is_awake_session(),
        }
        result = self._on_ready_to_listen(payload)
        if hasattr(result, "__await__"):
            await result

    def _tts_is_playing(self) -> bool:
        if self._tts_playing_fn is None:
            return False
        try:
            return bool(self._tts_playing_fn())
        except Exception:
            return False

    def is_waiting_turn_complete(self) -> bool:
        return self._active and self._waiting_turn_complete

    async def _wait_until_tts_idle(self) -> bool:
        if not self._tts_is_playing():
            return True
        await bus.emit(Method.ASR_STATUS, {"status": "paused_tts", "source": self._source or ""})
        while self._active and self._tts_is_playing():
            if self._is_awake_session():
                self._awake_until = max(self._awake_until, time.monotonic() + self._awake_seconds)
            await asyncio.sleep(0.1)
        return self._active

    async def _wait_until_turn_complete(self) -> bool:
        if not self._waiting_turn_complete:
            return True
        await bus.emit(Method.ASR_STATUS, {"status": "waiting_turn_complete", "source": self._source or ""})
        started = time.monotonic()
        while self._active and self._waiting_turn_complete:
            if self._is_awake_session():
                self._awake_until = max(self._awake_until, time.monotonic() + self._awake_seconds)
            if ASR_TURN_COMPLETE_TIMEOUT_SECONDS > 0:
                elapsed = time.monotonic() - started
                if elapsed >= ASR_TURN_COMPLETE_TIMEOUT_SECONDS:
                    logger.warning(
                        "asr turn-complete wait timed out after %.1fs; releasing listener",
                        elapsed,
                    )
                    await self.notify_turn_complete("timeout")
                    break
            await asyncio.sleep(0.1)
        return self._active

    async def notify_turn_complete(self, reason: str = "playback") -> None:
        if not self._waiting_turn_complete:
            return
        self._waiting_turn_complete = False
        if self._is_awake_session():
            self._awake_until = time.monotonic() + self._awake_seconds
            logger.info(
                "asr turn complete (%s); keeping Qwen ASR hot for %.1fs",
                reason,
                self._awake_seconds,
            )
        await bus.emit(Method.ASR_STATUS, {"status": "turn_complete", "source": self._source or "", "reason": reason})
        if self._finish_after_turn_complete:
            self._active = False
            await self._finish_listening(f"turn_complete:{reason}")

    async def _finish_listening(self, reason: str) -> None:
        session_info = self._clear_session_state()
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_asr_stopped(reason=reason)
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
        await bus.emit(Method.ASR_STATUS, {"status": "idle", "reason": reason, **session_info})
        self.schedule_unload()
        if self._on_listening_stopped is not None:
            payload = {"reason": reason, **session_info}
            result = self._on_listening_stopped(payload)
            if hasattr(result, "__await__"):
                await result

    def schedule_unload(self, delay_seconds: float | None = None) -> None:
        delay = ASR_IDLE_UNLOAD_SECONDS if delay_seconds is None else float(delay_seconds)
        if delay <= 0:
            self._unload_task = asyncio.create_task(self.unload())
            return
        if self._unload_task:
            self._unload_task.cancel()
        self._unload_task = asyncio.create_task(self._unload_after(delay))

    async def _unload_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._active:
                await self.unload()
        except asyncio.CancelledError:
            pass

    async def unload(self) -> dict[str, Any]:
        manager = self._asr_manager
        self._asr_manager = None
        self._unload_task = None
        if manager is not None:
            await asyncio.to_thread(manager.close)
            if self._on_unload is not None:
                self._on_unload(manager)
        await bus.emit(Method.ASR_STATUS, {"status": "unloaded"})
        return {"status": "unloaded"}

    async def _listen_loop(self) -> None:
        while self._active:
            try:
                asr_manager = await self._ensure_asr_manager()
                if asr_manager is None:
                    await bus.emit(
                        Method.ASR_STATUS,
                        {
                            "status": "error",
                            "source": self._source or "",
                            "error": "ASR manager unavailable",
                        },
                    )
                    self._active = False
                    await self._finish_listening("manager_unavailable")
                    break
                if not await self._wait_until_manager_ready(asr_manager):
                    break
                await self._notify_ready_to_listen()
                if not await self._wait_until_turn_complete():
                    break
                if not await self._wait_until_tts_idle():
                    break
                await self._emit_listening_status()

                create_capture_token = getattr(
                    asr_manager,
                    "create_capture_token",
                    None,
                )
                capture_token = (
                    create_capture_token()
                    if callable(create_capture_token)
                    else None
                )
                if capture_token is None:
                    text = await asyncio.to_thread(
                        asr_manager.listen_for_speech,
                        max_retries=0,
                    )
                else:
                    text = await asyncio.to_thread(
                        asr_manager.listen_for_speech,
                        max_retries=0,
                        cancel_event=capture_token,
                    )
                if text and self._active:
                    if self._is_awake_session() or self.is_continuous_chat_mic():
                        # Wake hot-window and Auto Mic both wait for the current
                        # chat/TTS turn to finish before they listen again.
                        self._waiting_turn_complete = True
                    payload: dict[str, Any] = {"text": text, "is_final": True}
                    if self._source:
                        payload["source"] = self._source
                    if self._wake_payload:
                        payload["wake"] = self._wake_payload
                    if self._source_payload:
                        payload["source_payload"] = self._source_payload
                    await bus.emit(Method.ASR_RECOGNIZED, payload)
                    if self._on_recognized is not None:
                        result = self._on_recognized(payload)
                        if hasattr(result, "__await__"):
                            await result
                elif self._active and self._is_awake_session():
                    remaining = max(0.0, self._awake_until - time.monotonic())
                    logger.info("awake ASR heard nothing; continuing for %.1fs", remaining)
                    await bus.emit(
                        Method.ASR_STATUS,
                        {
                            "status": "no_speech",
                            "source": "wake",
                            "awake_remaining": remaining,
                        },
                    )
                if self._one_shot:
                    self._active = False
                    await self._finish_listening("one_shot_complete")
                    break
                if self._is_awake_session() and time.monotonic() >= self._awake_until:
                    self._active = False
                    await self._finish_listening("awake_timeout")
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("asr listen error")
                await asyncio.sleep(0.5)

        current_task = asyncio.current_task()
        if self._listen_task is current_task:
            self._listen_task = None
