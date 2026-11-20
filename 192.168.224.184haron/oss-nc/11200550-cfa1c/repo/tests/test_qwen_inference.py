"""CPU semantic parity against the installed official tiny Qwen implementation."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import numpy as np
import pytest


torch = pytest.importorskip("torch")
pytest.importorskip("qwen_asr")
from qwen_asr.core.transformers_backend.configuration_qwen3_asr import Qwen3ASRConfig
from qwen_asr.core.transformers_backend.modeling_qwen3_asr import Qwen3ASRForConditionalGeneration
from qwen_asr.inference.qwen3_asr import Qwen3ASRModel
from transformers.feature_extraction_utils import BatchFeature

from asr.backend import asr_call_context, current_asr_call_context
from asr.qwen_inference import (
    AUDIO_FA2_TEXT_SDPA_CONFIGS, _greedy_decode, _model_hooks, _Timings,
    apply_qwen_attention_policy, qwen_attention_configs, set_qwen_attention_configs, transcribe_qwen,
)
from tools.asr.benchmark_qwen_replay import _ATTENTION_PRESETS, apply_attention_override, edit_distance, quality, summarize


@pytest.fixture
def model():
    old_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    torch.manual_seed(123)
    config = Qwen3ASRConfig(thinker_config=dict(
        audio_config=dict(d_model=16, encoder_layers=1, encoder_attention_heads=2,
                          encoder_ffn_dim=32, output_dim=16, downsample_hidden_size=8),
        text_config=dict(vocab_size=48, hidden_size=16, intermediate_size=32,
                         num_hidden_layers=1, num_attention_heads=2, num_key_value_heads=1,
                         head_dim=8, rope_scaling=dict(rope_type="default", mrope_section=[1, 1, 2])),
        audio_token_id=40, pad_token_id=0, eos_token_id=2))
    config._attn_implementation = "eager"
    result = Qwen3ASRForConditionalGeneration(config).eval()
    yield result
    torch.set_num_threads(old_threads)


def generate(model, ids, engine, budget=4, **kwargs):
    timings = _Timings(torch.device("cpu"), False)
    timings.generation_prepare_started = timings.start("generation_prepare")
    with torch.inference_mode(), _model_hooks(model.thinker, timings, engine != "official"):
        return model.generate(input_ids=ids, attention_mask=(ids != 0).long(),
                              max_new_tokens=budget, eos_token_id=[1, 2],
                              **({"custom_generate": _greedy_decode} if engine == "greedy" else {}), **kwargs).sequences


@pytest.mark.parametrize("eos", [1, 2])
def test_both_eos_and_child_generation_config_processors_are_preserved(model, eos):
    # Defaults belong to thinker, not the wrapper's unused generation config.
    model.thinker.generation_config.forced_eos_token_id = eos
    model.thinker.generation_config.min_new_tokens = 3
    ids = torch.tensor([[0, 3, 4, 5]])
    official = generate(model, ids, "official")
    assert official.shape[1] == ids.shape[1] + 4
    assert official[0, -1] == eos
    for engine in ("prefill", "greedy"):
        assert torch.equal(official, generate(model, ids, engine))


def test_budget_and_long_short_alternation_have_no_stale_kv_mask_or_rope(model):
    model.thinker.generation_config.suppress_tokens = [1, 2]
    for length in (3, 17, 4, 9, 3):
        ids = torch.tensor([[0] + list(range(3, 3 + length))])
        official = generate(model, ids, "official", budget=5)
        assert official.shape[1] == ids.shape[1] + 5
        for engine in ("prefill", "greedy"):
            assert torch.equal(official, generate(model, ids, engine, budget=5))


def test_hook_failure_is_visible_and_next_official_forward_keeps_all_positions(model, monkeypatch):
    original = model.thinker.lm_head.forward
    monkeypatch.setattr(model.thinker.lm_head, "forward", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic")))
    with pytest.raises(RuntimeError, match="synthetic"):
        generate(model, torch.tensor([[3, 4, 5]]), "greedy")
    monkeypatch.setattr(model.thinker.lm_head, "forward", original)
    assert not model.thinker._forward_hooks and not model.thinker._forward_pre_hooks
    assert not model.thinker.audio_tower._forward_hooks and not model.thinker.lm_head._forward_pre_hooks
    with torch.inference_mode():
        outputs = model.thinker(input_ids=torch.tensor([[3, 4, 5]]), attention_mask=torch.ones(1, 3), use_cache=False)
    assert outputs.logits.shape == (1, 3, 48)


@pytest.mark.parametrize("configuration", [
    {"do_sample": True},
    {"penalty_alpha": 0.6, "top_k": 4},
    {"num_beams": 2},
    {"prompt_lookup_num_tokens": 2},
    {"dola_layers": "low"},
], ids=["sample", "contrastive", "beam", "prompt_lookup", "dola"])
def test_unsupported_generation_modes_fail_before_inference_without_an_engine_retry(model, configuration):
    for name, value in configuration.items():
        setattr(model.thinker.generation_config, name, value)
    entered = Mock()
    handle = model.thinker.register_forward_pre_hook(entered)
    try:
        with pytest.raises(ValueError, match="plain greedy"):
            generate(model, torch.tensor([[3, 4, 5]]), "greedy")
    finally:
        handle.remove()
    entered.assert_not_called()
    assert not model.thinker.lm_head._forward_pre_hooks


class SyntheticProcessor:
    def __init__(self):
        self.tokenizer = SimpleNamespace(encode=lambda text, **_: [ord(c) % 30 + 3 for c in text])
        self.seen = []
        self.messages = []

    def apply_chat_template(self, messages, **_):
        self.messages.append(messages)
        return messages[0]["content"] + ":audio:"

    def __call__(self, text, audio, **_):
        # Synthetic deterministic features; the real Qwen audio encoder/scatter,
        # mask and rotary decoder still execute. Outer normalization is official.
        self.seen.append((list(text), [wav.copy() for wav in audio]))
        ids = [3] + self.tokenizer.encode(text[0])[:10] + [40] * 4 + [4]
        return BatchFeature({"input_ids": torch.tensor([ids]), "attention_mask": torch.ones(1, len(ids), dtype=torch.long),
                             "input_features": torch.full((1, 128, 32), float(np.mean(audio[0]))),
                             "feature_attention_mask": torch.ones(1, 32, dtype=torch.long)})

    def batch_decode(self, ids, **_):
        return ["language Chinese<asr_text>" + " ".join(map(str, row.tolist())) for row in ids]


@pytest.mark.parametrize("language", [None, "English", "Chinese"])
@pytest.mark.parametrize("chunk_seconds", [None, 0.05])
def test_official_normalization_prompt_chunk_parse_and_multimodal_input_parity(model, language, chunk_seconds, monkeypatch):
    if chunk_seconds is not None:
        monkeypatch.setattr("qwen_asr.inference.qwen3_asr.MAX_ASR_INPUT_SECONDS", chunk_seconds)
    processor = SyntheticProcessor()
    wrapper = Qwen3ASRModel("transformers", model, processor, max_inference_batch_size=1, max_new_tokens=4)
    context = "数字42 mixed context"
    # Non-default rate and stereo are normalized by the original outer pipeline.
    pcm = np.stack((np.linspace(-0.1, 0.1, 800), np.linspace(0, 0.2, 800)), axis=1).astype(np.float32)
    golden = wrapper.transcribe(audio=(pcm, 8000), context=context, language=language)
    expected_input = processor.seen[-1]
    with asr_call_context("speculative", "semantic-test"):
        for engine in ("official", "prefill", "greedy"):
            results, metrics = transcribe_qwen(wrapper, pcm, 8000, context, language, 4, engine=engine)
            assert results == golden
            assert processor.seen[-1][0] == expected_input[0]
            assert np.array_equal(processor.seen[-1][1][0], expected_input[1][0])
            assert processor.messages[-1][0]["content"] == context
            assert metrics["request_kind"] == "speculative" and metrics["request_id"] == "semantic-test"
            chunks = metrics["chunk_count"]
            assert chunks >= (2 if chunk_seconds else 1)
            assert metrics["context_tokens"] == [len(context)] * chunks
            assert metrics["generated_tokens"] == 4 * chunks and metrics["stop_reasons"] == ["budget"] * chunks
            assert metrics["gpu_ms"] == {} and metrics["device_name"] == "CPU"
            assert metrics["wall_ms"]["encoder"] > 0 and metrics["wall_ms"]["prefill"] > 0
            assert "text" not in metrics and context not in str(metrics)
    assert current_asr_call_context()["request_kind"] == "direct"
    assert wrapper.max_new_tokens == 4


def test_speculative_request_context_survives_worker_and_invalidated_result_stays_uncommitted():
    from asr.manager import _SpeculativeTranscription
    entered, release = threading.Event(), threading.Event()
    contexts = []

    def transcribe(*_, **__):
        contexts.append(current_asr_call_context())
        entered.set()
        assert release.wait(2)
        return "recognized"

    on_result = Mock()
    speculative = _SpeculativeTranscription(SimpleNamespace(transcribe=transcribe), "", 16000, on_result)
    audio = np.zeros(1600, dtype=np.float32)
    with asr_call_context("final", "outer"):
        speculative.submit(audio)
        assert current_asr_call_context()["request_id"] == "outer"
    assert entered.wait(2)
    speculative.invalidate()
    release.set()
    assert speculative._done.wait(2)
    assert speculative.consume(audio) is None
    on_result.assert_not_called()
    first = contexts[0]
    assert first["request_kind"] == "speculative"
    speculative.submit(audio)
    assert speculative._done.wait(2)
    assert speculative.consume(audio) == "recognized"
    assert contexts[1]["request_kind"] == "speculative"
    assert contexts[1]["request_id"] != first["request_id"]
    assert current_asr_call_context()["request_kind"] == "direct"


def test_quality_requires_reference_and_p95_uses_nearest_rank():
    assert edit_distance("42", "24") == 2
    assert quality("hello world", "hello word")["wer"] == 0.5
    assert quality("中文42", "中文24")["cer"] == 0.5
    assert quality("", "anything")["cer"] is None
    records = [{"group": "warm", "engine": "official", "sample_id": "a", "metrics": {"total_wall_ms": i}}
               for i in range(1, 21)]
    assert summarize(records)[0]["p95_ms"] == 19


def test_offline_attention_override_reaches_live_audio_and_text_modules(model):
    backend = SimpleNamespace(_lock=threading.RLock(), _model=SimpleNamespace(model=model))
    original = qwen_attention_configs(model)
    apply_attention_override(backend, "current")
    assert qwen_attention_configs(model) == original
    apply_attention_override(backend, "sdpa")
    assert set(qwen_attention_configs(model).values()) == {"sdpa"}
    assert model.thinker.model.layers[0].self_attn.config._attn_implementation == "sdpa"
    assert model.thinker.audio_tower.layers[0].self_attn.config._attn_implementation == "sdpa"


def test_offline_attention_can_switch_sdpa_eager_sdpa_on_one_model(model):
    backend = SimpleNamespace(_lock=threading.RLock(), _model=SimpleNamespace(model=model))
    original = qwen_attention_configs(model)
    for implementation in ("sdpa", "eager", "sdpa"):
        apply_attention_override(backend, implementation)
        assert set(qwen_attention_configs(model).values()) == {implementation}
        assert model.thinker.model.layers[0].self_attn.config._attn_implementation == implementation
        assert model.thinker.audio_tower.layers[0].self_attn.config._attn_implementation == implementation
    apply_attention_override(backend, "current", current_configs=original)
    assert qwen_attention_configs(model) == original
    mixed = {"model": "sdpa", "thinker": "eager", "audio_tower": "sdpa", "text_decoder": "eager"}
    apply_attention_override(backend, "current", current_configs=mixed)
    assert qwen_attention_configs(model) == mixed
    apply_attention_override(backend, "sdpa")
    apply_attention_override(backend, "current", current_configs=mixed)
    assert qwen_attention_configs(model) == mixed


def test_offline_audio_text_preset_uses_separate_owning_configs(model, monkeypatch):
    preset = "audio_fa2_text_sdpa"
    assert _ATTENTION_PRESETS[preset] == {
        "model": "flash_attention_2", "thinker": "flash_attention_2",
        "audio_tower": "flash_attention_2", "text_decoder": "sdpa"}
    # CPU execution verifies the same mixed-component routing with eager audio,
    # without making FA2 installation/GPU availability a unit-test prerequisite.
    cpu_targets = {"model": "eager", "thinker": "eager", "audio_tower": "eager", "text_decoder": "sdpa"}
    monkeypatch.setitem(_ATTENTION_PRESETS, preset, cpu_targets)
    backend = SimpleNamespace(_lock=threading.RLock(), _model=SimpleNamespace(model=model))
    original = qwen_attention_configs(model)
    for implementation in (preset, "sdpa", preset):
        apply_attention_override(backend, implementation)
    assert qwen_attention_configs(model) == cpu_targets
    assert model.thinker.audio_tower.layers[0].self_attn.config._attn_implementation == "eager"
    assert model.thinker.model.layers[0].self_attn.config._attn_implementation == "sdpa"
    apply_attention_override(backend, "current", current_configs=original)
    assert qwen_attention_configs(model) == original


@pytest.mark.parametrize("requested", ["eager", "sdpa"])
def test_loader_policy_preserves_cpu_rocm_and_missing_fa_attention(requested):
    # These loaders already own their device/ROCm workaround and must not enter
    # the CUDA policy setter or require another model capability.
    untouched = SimpleNamespace(set_attn_implementation=Mock(side_effect=AssertionError("unexpected setter")))
    apply_qwen_attention_policy(untouched, requested)
    untouched.set_attn_implementation.assert_not_called()


def test_fa2_loader_and_offline_preset_share_verified_component_policy(monkeypatch):
    import asr.qwen_inference as inference
    model = object()
    configure = Mock()
    monkeypatch.setattr(inference, "set_qwen_attention_configs", configure)
    apply_qwen_attention_policy(model, "flash_attention_2")
    configure.assert_called_once_with(model, AUDIO_FA2_TEXT_SDPA_CONFIGS)
    assert _ATTENTION_PRESETS["audio_fa2_text_sdpa"] == AUDIO_FA2_TEXT_SDPA_CONFIGS


def test_component_attention_configuration_fails_visibly_when_not_applied(model, monkeypatch):
    monkeypatch.setattr(model.thinker.model, "set_attn_implementation", lambda _implementation: None)
    targets = {"model": "eager", "thinker": "eager", "audio_tower": "eager", "text_decoder": "sdpa"}
    with pytest.raises(RuntimeError, match="did not reach every Qwen submodel"):
        set_qwen_attention_configs(model, targets)


def test_sidecar_exposes_stage_metrics_without_logging_transcript_or_token_ids(caplog):
    import io
    import json
    import logging
    from asr.backend import asr_call_context
    from asr.backends.qwen3_asr import Qwen3ASRBackend

    backend = Qwen3ASRBackend()
    metrics = {"request_id": "sidecar-test", "request_kind": "final",
               "wall_ms": {"encoder": 1.0, "decode": 2.0}, "token_ids": [[123, 456]]}
    result = {"type": "result", "text": "private test transcript", "metrics": metrics}
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(
        (json.dumps(result) + "\n").encode()), poll=lambda: None)
    backend._proc = process
    with caplog.at_level(logging.INFO), asr_call_context("final", "sidecar-test"):
        assert backend.transcribe(np.zeros(160, dtype=np.float32)) == result["text"]
    sent = json.loads(process.stdin.getvalue())
    assert sent["engine"] == "cuda_graph"
    assert sent["request_id"] == "sidecar-test" and sent["request_kind"] == "final"
    assert backend.last_inference_metrics["wall_ms"] == metrics["wall_ms"]
    assert backend.last_inference_metrics["deployment"] == "sidecar"
    logged = next(record.getMessage() for record in caplog.records if " timing " in record.getMessage())
    payload = json.loads(logged.split(" timing ", 1)[1])
    assert payload["wall_ms"] == metrics["wall_ms"]
    assert "token_ids" not in payload and result["text"] not in logged
    process.stdout = io.BytesIO(b'{"type":"error","msg":"synthetic failure"}\n')
    assert backend.transcribe(np.zeros(160, dtype=np.float32)) is None
    assert backend.last_inference_metrics == {}
