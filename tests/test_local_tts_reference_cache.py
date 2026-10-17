"""Reference-cache contracts with real CPU audio I/O and synthetic model outputs.

Only production methods are compiled, so no checkpoints, character assets, Hub
access, or GPU are needed. The audio codecs, resampler, and tensor operations are
real; only the expensive neural networks are replaced.
"""
import ast
import logging
import os
import sys
import threading
import time
import traceback
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

torch = pytest.importorskip("torch", reason="reference preparation requires CPU tensors")
sf = pytest.importorskip("soundfile", reason="reference WAV decoding requires soundfile")
librosa = pytest.importorskip("librosa", reason="reference preparation requires librosa")
torchaudio = pytest.importorskip("torchaudio", reason="reference resampling requires torchaudio")

from GPT_SoVITS.module.mel_processing import mel_spectrogram_torch, spectrogram_torch
from tts.semantic_stability import assess_semantic_candidate, SemanticGenerationError
from tts.speech_onset import SpeechOnsetGate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(monkeypatch):
    source = ast.parse((ROOT / "local_tts_infer.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body
               if isinstance(node, ast.ClassDef) and node.name == "TTSInferencer")
    selected = {
        "_build_session_cache", "_build_synthesis_cache", "_normalize_reference_prompt",
        "warm_reference_cache", "_warmup_session_cache_runtime", "_clone_cached_value",
        "get_spepc", "_resample", "_semantic_guard_enabled", "_infer_semantic_with_guard",
        "_get_effective_max_sec", "_apply_fade_out", "infer", "infer_stream",
    }
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name in selected]
    helpers = [node for node in source.body if isinstance(node, ast.FunctionDef)
               and node.name in {"_default_lang_code", "_default_ref_free_prompt", "process_text"}]
    scope = dict(torch=torch, sf=sf, librosa=librosa, np=np, os=os, time=time,
                 traceback=traceback, logger=logging.getLogger(__name__),
                 cuda_work=lambda fn: fn, _TTS_OUTPUT_LANGUAGE="日文",
                 _TTS_REF_TEXT_JA="Reference。", _TTS_REF_TEXT_EN="Reference.",
                 mel_spectrogram_torch=mel_spectrogram_torch,
                 spectrogram_torch=spectrogram_torch,
                 assess_semantic_candidate=assess_semantic_candidate,
                 SemanticGenerationError=SemanticGenerationError,
                 SpeechOnsetGate=SpeechOnsetGate)
    exec(compile(ast.Module(body=helpers + [cls], type_ignores=[]),
                 "local_tts_infer.py", "exec"), scope)
    engine = scope["TTSInferencer"]()
    engine.model_version = "v3"
    engine.device = "cpu"
    engine.is_half = engine.is_v2pro = engine.is_rocm = False
    engine.cuda_graph_enabled = engine._sovits_sync_timing_enabled = False
    engine.hz, engine.max_sec = 50, 8
    engine._session_cache = {}
    engine._sovits_decode_lock = threading.Lock()
    engine.splits = set("。！？.!?")
    engine.dict_language = {"日文": "all_ja", "英文": "en"}
    engine.hps = SimpleNamespace(data=SimpleNamespace(
        sampling_rate=24000, filter_length=1024, hop_length=256, win_length=1024))
    engine.ssl_model = SimpleNamespace(model=Mock(return_value={
        "last_hidden_state": torch.ones(1, 8, 4)}))
    engine.vq_model = SimpleNamespace(
        extract_latent=Mock(return_value=torch.tensor([[[1, 2, 3]]])),
        decode_encp=Mock(side_effect=lambda *args: (torch.ones(1, 100, 20), torch.ones(1))),
        decode=Mock(return_value=torch.full((1, 1, 5120), 0.1)),
        cfm=SimpleNamespace(inference=Mock(side_effect=lambda fea, *a, **kw: fea.transpose(1, 2))),
    )
    def semantic(_ids, _length, prompt, _bert, **kwargs):
        tokens = torch.arange(70).reshape(1, -1)
        return (tokens, 0) if prompt is None else (torch.cat([prompt, tokens], 1), 70)
    engine.t2s_model = SimpleNamespace(model=SimpleNamespace(infer_panel=Mock(side_effect=semantic)))
    engine.get_phones_and_bert = Mock(side_effect=lambda text, lang: (
        list(range(48)), torch.zeros(1024, 48), text))
    engine.bigvgan_model = Mock(side_effect=lambda mel: torch.full((1, 1, mel.shape[-1] * 256), 0.1))
    engine._device_context = nullcontext
    engine._synchronize_device = Mock()
    engine._trim_generated_lead = lambda audio, sr: audio
    engine._finalize_stream_chunk = lambda audio, sr, **kwargs: audio.float().numpy()
    monkeypatch.setenv("TTS_SEMANTIC_GUARD", "1")
    # Model absence is intentional; codecs and CPU tensor operations remain real.
    monkeypatch.setitem(sys.modules, "torchcodec", None)
    monkeypatch.setattr(torchaudio, "load", Mock(side_effect=ImportError("TorchCodec is unavailable")))
    return engine, scope


def write_reference(tmp_path, *, rate=24000, channels=1):
    samples = (np.sin(np.arange(rate // 10) * 2 * np.pi * 440 / rate) * 0.25).astype(np.float32)
    if channels == 2:
        samples = np.stack([samples, samples * 0.5], axis=-1)
    path = tmp_path / "reference.wav"
    sf.write(path, samples, rate, subtype="PCM_16")
    return str(path)


def synthesize(engine, path, journey, *, reference_free=False):
    kwargs = dict(text="First。\nSecond。", ref_audio_path=path,
                  prompt_text=None if reference_free else "Reference。", how_to_cut="不切",
                  enable_cuda_graph=False, pause_second=0)
    if journey == "batch":
        sr, audio = engine.infer(**kwargs)
        assert sr == 24000 and audio.dtype == np.float32 and audio.size > 0
    else:
        if journey == "chunks":
            kwargs["chunk_size_seconds"] = 0.08
        chunks = list(engine.infer_stream(**kwargs))
        audio = np.concatenate([item[1] for item in chunks if item[1] is not None])
        assert all(item[0] == 24000 for item in chunks)
        assert audio.dtype == np.float32 and audio.size > 0
    assert np.isfinite(audio).all() and np.max(np.abs(audio)) > 0
    return audio


@pytest.mark.parametrize("rate", [16000, 24000, 48000])
@pytest.mark.parametrize("channels", [1, 2])
def test_reference_wav_decoding_preserves_mono_samples_and_24k_mel(runtime, tmp_path, rate, channels):
    engine, scope = runtime
    path = write_reference(tmp_path, rate=rate, channels=channels)
    decoded, _ = sf.read(path, dtype="float32", always_2d=True)
    expected = torch.from_numpy(decoded.mean(axis=1)).unsqueeze(0)
    if rate != 24000:
        expected = torchaudio.transforms.Resample(rate, 24000)(expected)
    observed = []
    def mel(audio, **kwargs):
        observed.append(audio.clone())
        return mel_spectrogram_torch(audio, **kwargs)
    scope["mel_spectrogram_torch"] = mel
    cache = engine._build_session_cache(path, "Reference。", "all_ja")
    assert len(observed) == 1
    assert observed[0].dtype == torch.float32 and observed[0].device.type == "cpu"
    torch.testing.assert_close(observed[0], expected)
    assert cache["mel2_norm"].shape[:2] == (1, 100)
    assert torch.isfinite(cache["mel2_norm"]).all()
    assert engine._build_session_cache(path, "Reference。", "all_ja") is cache
    engine.ssl_model.model.assert_called_once()
    torchaudio.load.assert_not_called()


@pytest.mark.parametrize("journey", ["batch", "stream", "chunks"])
def test_cold_and_cached_v3_requests_work_without_torchcodec(runtime, tmp_path, journey):
    engine, _ = runtime
    path = write_reference(tmp_path)
    first = synthesize(engine, path, journey)
    cached = next(iter(engine._session_cache.values()))
    before = {key: value.clone() for key, value in cached.items() if torch.is_tensor(value)}
    # A warm request must not decode the reference file again (including batch).
    Path(path).unlink()
    second = synthesize(engine, path, journey)
    np.testing.assert_array_equal(first, second)
    for key, value in before.items():
        torch.testing.assert_close(cached[key], value)
    engine.ssl_model.model.assert_called_once()
    assert engine.t2s_model.model.infer_panel.call_count == 4
    assert all(call.args[2] is not None for call in engine.t2s_model.model.infer_panel.call_args_list)
    torchaudio.load.assert_not_called()


@pytest.mark.parametrize("journey", ["batch", "stream"])
@pytest.mark.parametrize("stage", ["read", "mel", "encode"])
def test_reference_failure_keeps_original_error_and_never_runs_semantic_decode(
        runtime, tmp_path, journey, stage):
    engine, scope = runtime
    path = write_reference(tmp_path)
    original = RuntimeError("synthetic reference preparation failure")
    fail = Mock(side_effect=original)
    if stage == "read":
        scope["sf"] = SimpleNamespace(read=fail)
    elif stage == "mel":
        scope["mel_spectrogram_torch"] = fail
    else:
        engine.vq_model.decode_encp = fail
    with pytest.raises(RuntimeError) as error:
        synthesize(engine, path, journey)
    assert error.value is original
    assert engine._session_cache == {}
    engine.t2s_model.model.infer_panel.assert_not_called()
    engine.bigvgan_model.assert_not_called()


def test_failed_optional_warmup_can_recover_on_next_live_request(runtime, tmp_path, monkeypatch):
    engine, scope = runtime
    path = write_reference(tmp_path)
    original = RuntimeError("synthetic transient reference read failure")
    read = Mock(side_effect=original)
    scope["sf"] = SimpleNamespace(read=read)
    engine._resolve_runtime_warmup_ref = lambda: (path, "Reference。", "all_ja")
    monkeypatch.setenv("TTS_SESSION_WARMUP", "1")
    engine._warmup_session_cache_runtime()
    assert engine._session_cache == {}
    engine.t2s_model.model.infer_panel.assert_not_called()
    scope["sf"] = sf
    synthesize(engine, path, "batch")
    assert len(engine._session_cache) == 1


@pytest.mark.parametrize("journey", ["batch", "stream"])
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_legal_reference_free_generation_reaches_v1_v2_decoder(runtime, tmp_path, journey, version):
    engine, _ = runtime
    engine.model_version = version
    path = write_reference(tmp_path)
    synthesize(engine, path, journey, reference_free=True)
    assert all(call.args[2] is None for call in engine.t2s_model.model.infer_panel.call_args_list)
    assert engine.vq_model.decode.call_count == 2
    for call in engine.vq_model.decode.call_args_list:
        assert call.args[0].reshape(-1).tolist() == list(range(70))


@pytest.mark.parametrize("journey", ["batch", "stream"])
def test_v3_still_requires_reference_when_transcript_is_omitted(runtime, tmp_path, journey):
    engine, _ = runtime
    synthesize(engine, write_reference(tmp_path), journey, reference_free=True)
    assert all(call.args[2] is not None for call in engine.t2s_model.model.infer_panel.call_args_list)
