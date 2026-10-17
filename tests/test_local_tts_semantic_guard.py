from __future__ import annotations

import ast
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


# Exercise the production guard with CPU tensors, without importing/loading models.
torch = pytest.importorskip("torch", reason="semantic guard requires CPU tensors")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tts.semantic_stability import assess_semantic_candidate, SemanticGenerationError

source = ast.parse((ROOT / "local_tts_infer.py").read_text(encoding="utf-8"))
engine_class = next(node for node in source.body
                    if isinstance(node, ast.ClassDef) and node.name == "TTSInferencer")
engine_class.body = [node for node in engine_class.body
                     if isinstance(node, ast.FunctionDef) and node.name in {
                         "_semantic_guard_enabled", "_infer_semantic_with_guard"}]
scope = dict(os=os, logger=logging.getLogger(__name__),
             assess_semantic_candidate=assess_semantic_candidate,
             SemanticGenerationError=SemanticGenerationError)
exec(compile(ast.Module(body=[engine_class], type_ignores=[]), "local_tts_infer.py", "exec"), scope)
TTSInferencer = scope["TTSInferencer"]


class _FakeSemanticDecoder:
    def __init__(self, candidates: list[list[int]]) -> None:
        self.candidates = candidates
        self.penalties: list[float] = []

    def infer_panel(self, *_args, **kwargs):
        self.penalties.append(float(kwargs["repetition_penalty"]))
        tokens = self.candidates[len(self.penalties) - 1]
        candidate = torch.tensor([tokens], dtype=torch.long)
        prompt = _args[2]
        if prompt is None:
            return candidate, 0  # native decoder's reference-free return contract
        return torch.cat([prompt, candidate], dim=1), len(tokens)


def _inferencer(candidates: list[list[int]]) -> tuple[TTSInferencer, _FakeSemanticDecoder]:
    decoder = _FakeSemanticDecoder(candidates)
    inferencer = TTSInferencer.__new__(TTSInferencer)
    inferencer.hz = 50
    inferencer.t2s_model = SimpleNamespace(model=decoder)
    return inferencer, decoder


def _call(inferencer: TTSInferencer, *, reference_free=False):
    return inferencer._infer_semantic_with_guard(
        text_item="うーん……正直、まだ完全には分からないわ。",
        target_phone_count=48,
        all_phoneme_ids=torch.zeros(1, 4, dtype=torch.long),
        all_phoneme_len=torch.tensor([4]),
        prompt=None if reference_free else torch.full((1, 2), 999, dtype=torch.long),
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


@pytest.mark.parametrize("reference_free", [False, True])
@pytest.mark.parametrize("guard", ["0", "1"])
def test_actual_candidate_excludes_only_the_reference_prefix(reference_free, guard):
    tokens = list(range(175))
    inferencer, decoder = _inferencer([tokens])
    with _guard_setting(guard):
        candidate, count, attempts = _call(inferencer, reference_free=reference_free)
    assert candidate.shape == (1, 1, 175)
    assert candidate.reshape(-1).tolist() == tokens
    assert count == 175 and attempts == 1
    assert decoder.penalties == [1.35]


@pytest.mark.parametrize("reference_free", [False, True])
def test_truly_empty_generation_is_rejected_even_with_a_reference_prefix(reference_free):
    inferencer, decoder = _inferencer([[], []])
    with _guard_setting("1"), pytest.raises(SemanticGenerationError, match="empty_candidate"):
        _call(inferencer, reference_free=reference_free)
    assert decoder.penalties == [1.35, 1.5]


def test_reference_free_collapsed_generation_still_retries():
    good = list(range(70))
    inferencer, decoder = _inferencer([[41] * 60, good])
    with _guard_setting("1"):
        candidate, count, attempts = _call(inferencer, reference_free=True)
    assert candidate.reshape(-1).tolist() == good
    assert count == 70 and attempts == 2
    assert decoder.penalties == [1.35, 1.5]
