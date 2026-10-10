from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.handlers.voice import VoiceHandlers, close_voice_input_services
from server.ws_handler import ConnectionManager


def test_voice_handlers_register_before_services_are_created() -> None:
    voice = VoiceHandlers()
    manager = ConnectionManager()
    for handler in voice.handlers:
        manager.register_handler(handler)
        assert all(manager._request_handlers[method] is handler for method in handler.methods)
    assert voice.wake.service() is None
    assert voice.asr._asr_manager is None


def test_voice_composition_binds_callbacks_without_starting_lazy_services() -> None:
    voice = VoiceHandlers(tts=Mock(), asr=Mock(), wake=Mock())
    callbacks = {name: Mock() for name in (
        "on_interrupt", "asr_manager_factory", "on_asr_unload", "on_recognized",
        "on_listening_stopped", "on_ready_to_listen", "tts_playing_fn",
        "wake_resumable_fn", "wake_service_factory",
    )}
    playback, player = object(), object()
    voice.configure(playback_manager=playback, player=player, **callbacks)
    voice.tts.configure.assert_called_once_with(playback_manager=playback, player=player, on_interrupt=callbacks["on_interrupt"])
    voice.asr.configure.assert_called_once_with(
        asr_manager_factory=callbacks["asr_manager_factory"], on_unload=callbacks["on_asr_unload"],
        **{key: callbacks[key] for key in ("on_recognized", "on_listening_stopped", "on_ready_to_listen", "tts_playing_fn", "wake_resumable_fn")},
    )
    voice.wake.configure.assert_called_once_with(wake_service_factory=callbacks["wake_service_factory"])
    assert all(callback.call_count == 0 for callback in callbacks.values())


def test_voice_input_shutdown_uses_current_instances_before_shared_microphone(monkeypatch) -> None:
    from asr import mic_input_service

    closed = []
    monkeypatch.setattr(mic_input_service, "close_mic_input_service", lambda: closed.append("mic"))
    wake = SimpleNamespace(close=lambda: closed.append("wake"))
    asr = SimpleNamespace(close=lambda: closed.append("asr"))
    close_voice_input_services(wake, asr)
    assert closed == ["wake", "asr", "mic"]
    closed.clear()
    close_voice_input_services(None, None)
    assert closed == ["mic"]


def test_registration_rejects_conflicts_atomically_and_keeps_original_owner() -> None:
    manager = ConnectionManager()
    original = SimpleNamespace(methods=["fixture.existing"])
    manager.register_handler(original)
    with pytest.raises(ValueError, match="already registered"):
        manager.register_handler(SimpleNamespace(methods=["fixture.new", "fixture.existing"]))
    assert manager._request_handlers == {"fixture.existing": original}
    with pytest.raises(ValueError, match="duplicate methods"):
        manager.register_handler(SimpleNamespace(methods=["fixture.new", "fixture.new"]))
    assert manager._request_handlers == {"fixture.existing": original}


def test_domain_extension_registers_with_the_same_composition_loop() -> None:
    extra = SimpleNamespace(methods=["voice.fixture"])

    class ExtendedVoice(VoiceHandlers):
        @property
        def handlers(self):
            return *super().handlers, extra

    manager = ConnectionManager()
    for handler in ExtendedVoice().handlers:
        manager.register_handler(handler)
    assert manager._request_handlers["voice.fixture"] is extra
