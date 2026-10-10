from __future__ import annotations

import os

from contextlib import AbstractContextManager

import pytest


torch = pytest.importorskip("torch", reason="test requires the local-model tier")
pytest.importorskip("librosa", reason="test requires the local-model tier")

from local_tts_infer import (
    TTSInferencer,
    _allows_nvidia_cuda_extensions,
    _uses_torch_cuda_device_api,
)


def _inferencer(device: str, *, uses_torch_cuda_api: bool) -> TTSInferencer:
    inferencer = TTSInferencer.__new__(TTSInferencer)
    inferencer.device = device
    inferencer._uses_torch_cuda_api = uses_torch_cuda_api
    inferencer._allows_nvidia_cuda_extensions = uses_torch_cuda_api
    inferencer._tts_device_idx = 0
    return inferencer


def test_unavailable_cuda_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires a usable PyTorch CUDA/HIP device"):
        TTSInferencer(device="cuda:0")


def test_unavailable_mps_device_fails_before_model_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="requires an available PyTorch MPS backend"):
        TTSInferencer(device="mps")


def test_cpu_device_context_never_enters_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    monkeypatch.setattr(
        torch.cuda,
        "device",
        lambda *_args, **_kwargs: pytest.fail("CPU inference entered a CUDA context"),
    )

    context = inferencer._device_context()
    assert isinstance(context, AbstractContextManager)
    with context:
        pass


def test_cpu_synchronization_never_calls_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("CPU inference synchronized CUDA"),
    )

    inferencer._synchronize_device()


def test_mps_synchronization_uses_mps(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("mps", uses_torch_cuda_api=False)
    calls: list[str] = []
    monkeypatch.setattr(torch.mps, "synchronize", lambda: calls.append("mps"))
    monkeypatch.setattr(
        torch.cuda,
        "synchronize",
        lambda *_args, **_kwargs: pytest.fail("MPS inference synchronized CUDA"),
    )

    inferencer._synchronize_device()

    assert calls == ["mps"]


def test_cuda_context_preserves_selected_device(monkeypatch: pytest.MonkeyPatch) -> None:
    inferencer = _inferencer("cuda:2", uses_torch_cuda_api=True)
    inferencer._tts_device_idx = 2
    entered: list[int] = []

    class _FakeContext:
        def __enter__(self):
            entered.append(2)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(torch.cuda, "device", lambda index: _FakeContext())

    with inferencer._device_context():
        pass

    assert entered == [2]


def test_rocm_uses_torch_cuda_api_without_enabling_nvidia_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", "6.2")

    uses_torch_cuda_api = _uses_torch_cuda_device_api("cuda:0")

    assert uses_torch_cuda_api is True
    assert _allows_nvidia_cuda_extensions(uses_torch_cuda_api) is False


def test_rocm_stream_vocoder_reuses_final_chunk_shape() -> None:
    inferencer = _inferencer("cpu", uses_torch_cuda_api=False)
    shapes = []

    class Vocoder:
        def __call__(self, mel):
            shapes.append(tuple(mel.shape))
            return torch.ones((1, 1, mel.shape[-1] * 4)),

    inferencer.bigvgan_model = Vocoder()
    first = inferencer._run_bigvgan_stream_chunk(torch.ones((1, 100, 8)), target_frames=8)
    last = inferencer._run_bigvgan_stream_chunk(torch.ones((1, 100, 3)), target_frames=8)

    assert shapes == [(1, 100, 8), (1, 100, 8)]
    assert first.shape[-1] == 32
    assert last.shape[-1] == 12


@pytest.mark.parametrize("value", ["", "80.0", "eighty"])
def test_malformed_stream_bucket_setting_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from local_tts_infer import _stream_bucket_mels

    monkeypatch.setenv("TTS_BIGVGAN_STREAM_BUCKET_MELS", value)

    assert _stream_bucket_mels() == 80


def test_stream_bucket_setting_is_a_positive_frame_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_tts_infer import _stream_bucket_mels

    monkeypatch.setenv("TTS_BIGVGAN_STREAM_BUCKET_MELS", "96")
    assert _stream_bucket_mels() == 96
    monkeypatch.setenv("TTS_BIGVGAN_STREAM_BUCKET_MELS", "0")
    assert _stream_bucket_mels() == 1


def test_cfm_omits_only_unneeded_padding_masks() -> None:
    from GPT_SoVITS.module.models import CFM

    observed = []

    class Estimator(torch.nn.Module):
        def forward(self, x, *_args, use_padding_mask=True, **_kwargs):
            observed.append(use_padding_mask)
            return x.transpose(2, 1)

    cfm = CFM(2, Estimator())
    prompt = torch.zeros((1, 2, 1))
    for length in (4, 3):
        cfm.inference(torch.zeros((1, 4, 2)), torch.tensor([length]), prompt, n_timesteps=2)
    cfm.inference(
        torch.zeros((2, 4, 2)), torch.tensor([4, 4]), torch.zeros((2, 2, 1)), n_timesteps=1
    )

    # Unpadded single sample, padded single sample, then a batch.
    assert observed == [False, False, True, True, True]


def test_unpadded_dit_output_matches_full_mask() -> None:
    from GPT_SoVITS.f5_tts.model.backbones.dit import DiT

    torch.manual_seed(1)
    model = DiT(
        dim=16, depth=1, heads=2, dim_head=8,
        mel_dim=4, text_dim=4, dropout=0,
    ).eval()
    noise = torch.randn(1, 4, 5)
    condition = torch.randn(1, 4, 5)
    text = torch.randn(1, 4, 5)
    arguments = (
        noise, condition, torch.tensor([5]),
        torch.tensor([0.5]), torch.tensor([0.1]), text,
    )
    with torch.inference_mode():
        masked = model(*arguments, use_padding_mask=True)
        unmasked = model(*arguments, use_padding_mask=False)

    torch.testing.assert_close(unmasked, masked)


def test_nvidia_cuda_device_allows_nvidia_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.version, "hip", None)

    uses_torch_cuda_api = _uses_torch_cuda_device_api("cuda:0")

    assert uses_torch_cuda_api is True
    assert _allows_nvidia_cuda_extensions(uses_torch_cuda_api) is True


@pytest.mark.parametrize("device,rocm,mode,expected", [
    ("cuda:1", False, "auto", True), ("cuda:1", False, "0", False),
    ("cpu", False, "auto", False), ("cpu", False, "1", False),
    ("mps", False, "auto", False), ("cuda:0", True, "auto", False),
])
def test_acceleration_setup_uses_loaded_device_and_respects_off(monkeypatch, device, rocm, mode, expected):
    from types import SimpleNamespace
    infer = _inferencer(device, uses_torch_cuda_api=device.startswith("cuda"))
    infer.is_rocm = rocm
    infer._allows_nvidia_cuda_extensions = device.startswith("cuda") and not rocm
    decoder = SimpleNamespace(use_static_kv_cache=True, cuda_graph_enabled=False)
    infer.t2s_model = SimpleNamespace(model=decoder)
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", mode)
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", "0")
    infer._configure_t2s_acceleration()
    assert decoder.cuda_graph_enabled is expected
    assert decoder.use_static_kv_cache is device.startswith("cuda")
    if not expected:
        decoder.precapture_cuda_graph = lambda *_: pytest.fail("disabled acceleration captured a graph")
        infer._maybe_precapture_t2s_graph()


@pytest.mark.parametrize("mode,major,dtype,extension", [
    ("auto", 8, torch.float16, True), ("1", 8, torch.float16, True),
    ("auto", 8, torch.float16, False), ("0", 8, torch.float16, True),
    ("auto", 7, torch.float16, True), ("auto", 8, torch.float32, True),
])
def test_flash_setup_selects_actual_device_and_keeps_sdpa_when_unavailable(monkeypatch, mode, major, dtype, extension):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import AR.models.t2s_flash_attn as flash
    infer = _inferencer("cuda:1", uses_torch_cuda_api=True)
    infer.is_rocm = False
    decoder = SimpleNamespace(use_flash_attn_kvcache=False, flash_attn_kvcache_mode="off")
    infer.t2s_model = SimpleNamespace(model=decoder, parameters=lambda: iter([SimpleNamespace(dtype=dtype)]))
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "auto")
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", mode)
    devices = []
    monkeypatch.setattr(torch.cuda, "get_device_capability", lambda device: devices.append(str(device)) or (major, 0))
    monkeypatch.setattr(infer, "_device_context", nullcontext)
    monkeypatch.setattr(flash, "_flash_attn_with_kvcache", object() if extension else None)
    selected = object()
    monkeypatch.setattr(flash, "build_flash_static_transformer", lambda *_args, **_kwargs: selected)
    infer._configure_t2s_acceleration()
    eligible = mode != "0" and major >= 8 and dtype == torch.float16
    assert devices == (["cuda:1"] if mode != "0" else [])
    assert decoder.use_flash_attn_kvcache is (eligible and extension)
    if eligible and extension:
        assert decoder.t2s_transformer_static is selected


@pytest.mark.skipif(os.environ.get("AMADEUS_RUN_GPU_TESTS") != "1", reason="explicit NVIDIA kernel probe")
def test_auto_graph_and_flash_capture_replay_on_nvidia(monkeypatch):
    pytest.importorskip("flash_attn")
    if not torch.cuda.is_available() or torch.version.hip or torch.cuda.get_device_capability(0)[0] < 8:
        pytest.skip("requires NVIDIA Ampere or newer")
    from AR.models.t2s_model import Text2SemanticDecoder
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "auto")
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", "auto")
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN_MODE", "valid")
    infer = _inferencer("cuda:0", uses_torch_cuda_api=True)
    infer.is_rocm = False
    config = {"model": {"hidden_dim": 128, "embedding_dim": 128, "head": 2,
                       "n_layer": 1, "vocab_size": 17, "phoneme_vocab_size": 32,
                       "dropout": 0.0, "EOS": 16}}
    model = torch.nn.Module()
    model.model = Text2SemanticDecoder(config).half().to(infer.device).eval()
    infer.t2s_model = model
    with torch.inference_mode():
        infer._configure_t2s_acceleration()
        decoder = model.model
        assert decoder.cuda_graph_enabled and decoder.use_flash_attn_kvcache
        # Use one actual aligned graph key; this is not a model or audio benchmark.
        assert decoder.precapture_cuda_graph([256], kv_len_range=(32, 32)) == {256: True}
        key = (256, 32)
        decoder._replay_cuda_graph(decoder.bucket_graphs[key], infer.device)
        torch.cuda.synchronize(infer.device)
        assert torch.isfinite(decoder.bucket_static_outputs[key]["logits"]).all()
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "0")
    assert not infer.cuda_graph_enabled
