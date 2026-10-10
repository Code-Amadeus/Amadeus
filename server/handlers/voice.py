"""Composition of built-in voice handlers and their existing runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from server.handlers.asr_handler import AsrHandler
from server.handlers.tts_handler import TtsHandler
from server.handlers.wake_handler import WakeHandler
from server.ws_handler import RequestHandler


@dataclass
class VoiceHandlers:
    # Construction stays lightweight: methods are registered before the runtime
    # starts, then configure() binds the shared services and callbacks.
    tts: TtsHandler = field(default_factory=TtsHandler)
    asr: AsrHandler = field(default_factory=AsrHandler)
    wake: WakeHandler = field(default_factory=WakeHandler)

    @property
    def handlers(self) -> tuple[RequestHandler, ...]:
        return self.tts, self.asr, self.wake

    def configure(
        self, *, playback_manager: Any, player: Any, on_interrupt: Callable,
        asr_manager_factory: Callable, on_asr_unload: Callable,
        on_recognized: Callable, on_listening_stopped: Callable,
        on_ready_to_listen: Callable, tts_playing_fn: Callable,
        wake_resumable_fn: Callable, wake_service_factory: Callable,
    ) -> None:
        self.tts.configure(playback_manager=playback_manager, player=player, on_interrupt=on_interrupt)
        self.asr.configure(
            asr_manager_factory=asr_manager_factory, on_unload=on_asr_unload,
            on_recognized=on_recognized, on_listening_stopped=on_listening_stopped,
            on_ready_to_listen=on_ready_to_listen, tts_playing_fn=tts_playing_fn,
            wake_resumable_fn=wake_resumable_fn,
        )
        self.wake.configure(wake_service_factory=wake_service_factory)


def close_voice_input_services(wake_service: Any, asr_manager: Any) -> None:
    """Close only already-created services, before releasing their shared mic.

The application supplies the current instances because its scene callbacks own
lazy creation/replacement; shutdown must never invoke a factory to discover one.
"""
    for service in (wake_service, asr_manager):
        if service is not None:
            close = getattr(service, "close", None)
            if callable(close):
                close()
    try:
        from asr.mic_input_service import close_mic_input_service

        close_mic_input_service()
    except Exception:
        pass
