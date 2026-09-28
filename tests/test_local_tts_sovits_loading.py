"""SoVITS loading must keep the acoustic version apart from the text-symbol version."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


pytest.importorskip("torch", reason="test requires the local-model tier")
pytest.importorskip("librosa", reason="test requires the local-model tier")

from local_tts_infer import TTSInferencer


def _decoder() -> Mock:
    decoder = Mock()
    decoder.to.return_value = decoder
    return decoder


def _load_sovits(monkeypatch, tmp_path, *, model_version, text_embedding_rows):
    """Run the real loader and report which constructor ran with which version."""
    weights = {}
    if text_embedding_rows is not None:
        weights["enc_p.text_embedding.weight"] = SimpleNamespace(shape=(text_embedding_rows, 192))
    checkpoint = {
        "weight": weights,
        "config": {
            "data": {"filter_length": 2048, "hop_length": 640, "n_speakers": 1},
            "train": {"segment_size": 20480},
            "model": {"version": "v2"},
        },
    }
    v3_constructor = Mock(return_value=_decoder())
    v2_constructor = Mock(return_value=_decoder())
    monkeypatch.setattr("local_tts_infer.load_sovits_new", lambda _: checkpoint)
    monkeypatch.setattr("local_tts_infer.SynthesizerTrnV3", v3_constructor)
    monkeypatch.setattr("local_tts_infer.SynthesizerTrn", v2_constructor)

    inferencer = TTSInferencer.__new__(TTSInferencer)
    # Neither ".pth" nor "pretrained": load the checkpoint directly instead of base + LoRA.
    inferencer.sovits_path = str(tmp_path / "voice.weights")
    inferencer.device = "cpu"
    inferencer.is_half = False
    inferencer.model_version = model_version

    inferencer._load_sovits_model()
    return inferencer, v3_constructor, v2_constructor


def test_v3_weights_reuse_v2_symbols_but_keep_the_v3_acoustic_version(monkeypatch, tmp_path):
    inferencer, v3_constructor, v2_constructor = _load_sovits(
        monkeypatch, tmp_path, model_version="v3", text_embedding_rows=None
    )

    v2_constructor.assert_not_called()
    assert v3_constructor.call_args.kwargs["version"] == "v3"
    assert inferencer.hps.model.version == "v3"
    # Text still uses the v2 symbol set that v3 checkpoints carry.
    assert inferencer.sovits_version == "v2"


@pytest.mark.parametrize(
    "model_version,text_embedding_rows,expected",
    [("v2", 732, "v2"), ("v1", 322, "v1")],
)
def test_legacy_weights_keep_the_version_from_their_weight_table(
    monkeypatch, tmp_path, model_version, text_embedding_rows, expected
):
    inferencer, v3_constructor, v2_constructor = _load_sovits(
        monkeypatch,
        tmp_path,
        model_version=model_version,
        text_embedding_rows=text_embedding_rows,
    )

    v3_constructor.assert_not_called()
    assert v2_constructor.call_args.kwargs["version"] == expected
    assert inferencer.sovits_version == expected
