from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


if os.environ.get("AMADEUS_E2E_NO_TTS", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    pytest.skip("full TTS runtime is disabled in model-less CI", allow_module_level=True)

import torch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_tts_infer import TTSInferencer
from tts.semantic_stability import SemanticGenerationError


class _FakeSemanticDecoder:
    def __init__(self, candidates: list[list[int]]) -> None:
        self.candidates = candidates
        self.penalties: list[float] = []

    def infer_panel(self, *_args, **kwargs):
        self.penalties.append(float(kwargs["repetition_penalty"]))
        tokens = self.candidates[len(self.penalties) - 1]
        return torch.tensor([tokens], dtype=torch.long), len(tokens)


def _inferencer(candidates: list[list[int]]) -> tuple[TTSInferencer, _FakeSemanticDecoder]:
    decoder = _FakeSemanticDecoder(candidates)
    inferencer = TTSInferencer.__new__(TTSInferencer)
    inferencer.hz = 50
    inferencer.t2s_model = SimpleNamespace(model=decoder)
    return inferencer, decoder


def _call(inferencer: TTSInferencer):
    return inferencer._infer_semantic_with_guard(
        text_item="うーん……正直、まだ完全には分からないわ。",
        target_phone_count=48,
        all_phoneme_ids=torch.zeros(1, 4, dtype=torch.long),
        all_phoneme_len=torch.tensor([4]),
        prompt=torch.zeros(1, 2, dtype=torch.long),
        bert=torch.zeros(1, 1024, 4),
        top_k=5,
        top_p=1.0,
        temperature=0.6,
        effective_max_sec=8.0,
        enable_cuda_graph=True,
        enable_static_kv=True,
    )


@contextmanager
def _guard_setting(value: str):
    previous = os.environ.get("TTS_SEMANTIC_GUARD")
    os.environ["TTS_SEMANTIC_GUARD"] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TTS_SEMANTIC_GUARD", None)
        else:
            os.environ["TTS_SEMANTIC_GUARD"] = previous


def test_guard_discards_collapsed_candidate_and_returns_single_retry() -> None:
    with _guard_setting("1"):
        bad = list(range(20)) + [937] * 60
        good = [index % 23 for index in range(70)]
        inferencer, decoder = _inferencer([bad, good])

        candidate, count, attempts = _call(inferencer)

        assert attempts == 2
        assert count == len(good)
        assert candidate.shape == (1, 1, len(good))
        assert candidate.reshape(-1).tolist() == good
        assert decoder.penalties == [1.35, 1.5]


def test_guard_fails_closed_after_two_collapsed_candidates() -> None:
    with _guard_setting("1"):
        bad_a = [41] * 60
        bad_b = [77] * 70
        inferencer, decoder = _inferencer([bad_a, bad_b])

        try:
            _call(inferencer)
        except SemanticGenerationError as exc:
            assert "after 2 attempts" in str(exc)
        else:
            raise AssertionError("two collapsed candidates must fail closed")

        assert decoder.penalties == [1.35, 1.5]


def test_guard_off_preserves_one_attempt_path() -> None:
    with _guard_setting("0"):
        bad = [41] * 60
        inferencer, decoder = _inferencer([bad])

        candidate, count, attempts = _call(inferencer)

        assert attempts == 1
        assert count == len(bad)
        assert candidate.reshape(-1).tolist() == bad
        assert decoder.penalties == [1.35]


if __name__ == "__main__":
    test_guard_discards_collapsed_candidate_and_returns_single_retry()
    test_guard_fails_closed_after_two_collapsed_candidates()
    test_guard_off_preserves_one_attempt_path()
    print("all local TTS semantic guard tests passed")
