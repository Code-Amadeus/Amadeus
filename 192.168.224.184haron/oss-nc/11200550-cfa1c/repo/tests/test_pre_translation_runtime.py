from __future__ import annotations

import os

from tts.pre_translation_runtime import (
    PreTranslationRuntime,
    configured_default_enabled,
)


def test_runtime_value_can_change_without_mutating_process_environment(monkeypatch) -> None:
    monkeypatch.delenv("AMADEUS_PRE_TRANSLATION_ENABLED", raising=False)
    state = PreTranslationRuntime(False)

    state.configure(True)

    assert state.is_enabled() is True
    assert "AMADEUS_PRE_TRANSLATION_ENABLED" not in os.environ


def test_configured_default_accepts_legacy_typo_alias(monkeypatch) -> None:
    monkeypatch.delenv("AMADEUS_PRE_TRANSLATION_ENABLED", raising=False)
    monkeypatch.setenv("AMADUES_PRE_TRANSLATION_ENABLED", "on")
    assert configured_default_enabled() is True


def test_canonical_pre_translation_value_wins_over_alias(monkeypatch) -> None:
    monkeypatch.setenv("AMADEUS_PRE_TRANSLATION_ENABLED", "0")
    monkeypatch.setenv("AMADUES_PRE_TRANSLATION_ENABLED", "1")
    assert configured_default_enabled() is False
