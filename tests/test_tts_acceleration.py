"""Device ownership and explicit overrides for automatic TTS acceleration."""
from types import SimpleNamespace

import pytest

from config.tts_acceleration import acceleration_mode, cuda_graph_enabled, flash_attention_eligible
from tts.backend import TTSRuntimeAdapter, BaseTTSBackend
from tts.backends.gpt_sovits import GPTSoVITSBackend


@pytest.mark.parametrize("device,hip,automatic", [
    ("cuda:1", False, True), ("cuda:0", True, False),
    ("cpu", False, False), ("mps", False, False), ("remote", False, False),
])
@pytest.mark.parametrize("mode", [None, "auto", "0", "1"])
def test_graph_policy_uses_actual_speech_device(monkeypatch, device, hip, automatic, mode):
    monkeypatch.delenv("ENABLE_CUDA_GRAPH", raising=False)
    if mode is not None:
        monkeypatch.setenv("ENABLE_CUDA_GRAPH", mode)
    expected = (device.startswith("cuda") if mode == "1" else False if mode == "0" else automatic)
    assert cuda_graph_enabled(device, is_rocm=hip) is expected


@pytest.mark.parametrize("value,expected", [("true", "1"), ("off", "0"), ("auto", "auto"), ("", "auto")])
def test_legacy_boolean_spellings_remain_explicit(monkeypatch, value, expected):
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", value)
    assert acceleration_mode("TTS_T2S_FLASH_ATTN") == expected


def test_invalid_preference_is_not_silently_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "atuo")
    with pytest.raises(ValueError, match="ENABLE_CUDA_GRAPH"):
        cuda_graph_enabled("cuda:0")


@pytest.mark.parametrize("device,hip,dtype,cap,eligible", [
    ("cuda:1", False, "torch.float16", (8, 9), True),
    ("cuda:0", False, "torch.bfloat16", (9, 0), True),
    ("cuda:0", False, "torch.float32", (8, 9), False),
    ("cuda:0", False, "torch.float16", (7, 5), False),
    ("cuda:0", True, "torch.float16", (9, 0), False),
    ("cpu", False, "torch.float16", (8, 9), False),
    ("mps", False, "torch.float16", (8, 9), False),
])
def test_flash_policy_preserves_device_precision_boundaries(monkeypatch, device, hip, dtype, cap, eligible):
    monkeypatch.delenv("ENABLE_T2S_FLASH_ATTN_KVCACHE", raising=False)
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", "auto")
    assert flash_attention_eligible(device, is_rocm=hip, dtype=dtype, capability=cap) is eligible
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", "0")
    assert not flash_attention_eligible(device, is_rocm=hip, dtype=dtype, capability=cap)


def test_flash_legacy_override_and_canonical_precedence(monkeypatch):
    monkeypatch.delenv("TTS_T2S_FLASH_ATTN", raising=False)
    monkeypatch.setenv("ENABLE_T2S_FLASH_ATTN_KVCACHE", "0")
    assert acceleration_mode("TTS_T2S_FLASH_ATTN", legacy_key="ENABLE_T2S_FLASH_ATTN_KVCACHE") == "0"
    monkeypatch.setenv("TTS_T2S_FLASH_ATTN", "auto")
    assert acceleration_mode("TTS_T2S_FLASH_ATTN", legacy_key="ENABLE_T2S_FLASH_ATTN_KVCACHE") == "auto"


@pytest.mark.parametrize("deployment", ["embedded", "subprocess"])
@pytest.mark.parametrize("device,hip,expected", [("cuda:1", False, True), ("cpu", False, False), ("cuda:0", True, False)])
def test_backend_uses_loaded_runtime_not_host_configuration(monkeypatch, deployment, device, hip, expected):
    monkeypatch.setenv("TTS_DEVICE", "cuda:0")
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "auto")
    backend = GPTSoVITSBackend()
    backend.deployment = deployment
    backend._inferencer = SimpleNamespace(device=device, is_rocm=hip)
    backend._ready_info = {"device": device, "hip": "7.2" if hip else None, "cuda_available": True}
    runtime = TTSRuntimeAdapter(backend)
    assert runtime.cuda_graph_enabled is expected
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "0")
    assert runtime.cuda_graph_enabled is False


def test_remote_backend_never_inherits_local_graph_preference(monkeypatch):
    class Remote(BaseTTSBackend):
        deployment = "remote"
        def synthesize(self, request):
            raise AssertionError("not synthesizing in a policy test")
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "1")
    assert TTSRuntimeAdapter(Remote()).cuda_graph_enabled is False


def test_live_auto_and_manual_modes_drive_synthesis_and_parameters(monkeypatch):
    import tts.pipeline as pipeline
    from tts.synthesis_backend import select_synthesis
    backend = GPTSoVITSBackend()
    backend._inferencer = SimpleNamespace(device="cuda:1", is_rocm=False)
    monkeypatch.setattr(pipeline, "_tts_runtime", TTSRuntimeAdapter(backend))
    monkeypatch.setenv("ENABLE_CUDA_GRAPH", "0")
    monkeypatch.setattr(pipeline, "_exp_tts_semaphore", None)
    monkeypatch.setattr(pipeline, "_exp_tts_concurrency", 1)
    for mode, enabled in [("auto", True), ("parallel", False), ("cuda_graph", True), ("parallel2", False)]:
        assert pipeline.reconfigure_tts_mode_name(mode) == mode
        assert pipeline.current_tts_mode() == mode
        assert pipeline.get_sovits_params("hello", False)["enable_cuda_graph"] is enabled
        name, _ = select_synthesis(None, cuda_graph_enabled=pipeline._current_cuda_graph_enabled(), experimental_enabled=True, backends=pipeline._SYNTHESIS_BACKENDS)
        assert name == ("cuda_graph_serial" if enabled else "experimental_asyncio_queue")
