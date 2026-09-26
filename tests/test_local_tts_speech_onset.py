"""The GPT-SoVITS adapter starts each synthesized item at its speech onset."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest


_MODEL_LESS = os.environ.get("AMADEUS_E2E_NO_TTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
pytestmark = pytest.mark.skipif(
    _MODEL_LESS,
    reason="full TTS runtime is disabled in model-less CI",
)

# Same tier contract as tests/test_local_tts_semantic_guard.py.
pytest.importorskip("torch", reason="test requires the local-cu124 tier (torch)")
pytest.importorskip("librosa", reason="test requires the local-cu124 extra (librosa)")

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not _MODEL_LESS:
    from local_tts_infer import TTSInferencer
else:
    TTSInferencer = object

RATE = 24000


def _item_with_generated_lead() -> np.ndarray:
    audio = np.zeros(int(RATE * 1.2), dtype=np.float32)
    audio[int(RATE * 0.9):] = 0.2
    return audio


def test_numpy_and_tensor_items_start_at_the_speech_preroll() -> None:
    inferencer = TTSInferencer.__new__(TTSInferencer)
    audio = _item_with_generated_lead()
    kept = int(RATE * 0.3) + int(RATE * 0.05)

    assert len(inferencer._trim_generated_lead(audio, RATE)) == kept
    trimmed_tensor = inferencer._trim_generated_lead(torch.from_numpy(audio), RATE)
    assert torch.is_tensor(trimmed_tensor) and trimmed_tensor.shape[0] == kept


def test_item_voiced_from_the_start_is_returned_as_is() -> None:
    inferencer = TTSInferencer.__new__(TTSInferencer)
    audio = np.full(RATE, 0.2, dtype=np.float32)

    assert inferencer._trim_generated_lead(audio, RATE) is audio
