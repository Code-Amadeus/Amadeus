from __future__ import annotations

import os

import pytest

from config import settings


@pytest.fixture(autouse=True)
def restore_tts_device_environment():
    previous = os.environ.get("TTS_DEVICE")
    yield
    if previous is None:
        os.environ.pop("TTS_DEVICE", None)
    else:
        os.environ["TTS_DEVICE"] = previous


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


def test_named_tts_voice_profile_selects_an_atomic_checkpoint_pair() -> None:
    gpt, sovits = settings._resolve_tts_voice_paths(
        "kurisu_v2pro",
        "ignored-gpt.ckpt",
        "ignored-sovits.pth",
    )

    assert gpt.endswith("weights/gpt/v2Pro/kurisu_v2pro-e15.ckpt")
    assert sovits.endswith("weights/sovits/v2Pro/kurisu_v2pro.pth")


def test_custom_tts_voice_profile_preserves_explicit_paths() -> None:
    assert settings._resolve_tts_voice_paths(
        "custom",
        "voices/custom.ckpt",
        "voices/custom.pth",
    ) == ("voices/custom.ckpt", "voices/custom.pth")


def test_unknown_tts_voice_profile_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported TTS_VOICE_PROFILE"):
        settings._resolve_tts_voice_paths("unknown", "", "")
