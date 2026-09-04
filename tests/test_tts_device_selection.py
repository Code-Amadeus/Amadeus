from __future__ import annotations

from config import settings


def test_apple_silicon_auto_tts_device_defaults_to_mps(monkeypatch) -> None:
    values = {
        "TTS_BACKEND": "gpt_sovits",
        "TTS_DEVICE": "auto",
    }
    monkeypatch.setattr(settings, "TTS_BACKEND", "gpt_sovits")
    monkeypatch.setattr(settings, "_str", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(settings.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(settings.platform, "machine", lambda: "arm64")

    assert settings._resolve_tts_device() == "mps"


def test_intel_macos_auto_tts_device_defaults_to_cpu(monkeypatch) -> None:
    values = {
        "TTS_BACKEND": "gpt_sovits",
        "TTS_DEVICE": "auto",
    }
    monkeypatch.setattr(settings, "TTS_BACKEND", "gpt_sovits")
    monkeypatch.setattr(settings, "_str", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(settings.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(settings.platform, "machine", lambda: "x86_64")

    assert settings._resolve_tts_device() == "cpu"


def test_explicit_tts_device_is_preserved(monkeypatch) -> None:
    values = {
        "TTS_DEVICE": "mps",
    }
    monkeypatch.setattr(settings, "TTS_BACKEND", "gpt_sovits")
    monkeypatch.setattr(settings, "_str", lambda key, default="": values.get(key, default))
    monkeypatch.setattr(settings.platform, "system", lambda: "Darwin")

    assert settings._resolve_tts_device() == "mps"
