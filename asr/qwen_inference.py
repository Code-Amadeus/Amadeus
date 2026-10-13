"""Measured Qwen inference, with opt-in last-logits and single-request greedy.

The official wrapper still owns normalization, chunking, prompts and parsing.
Callers must hold the model's lifecycle lock for the entire call: the temporary
hooks below belong to that model instance and are removed even on failure.
Explicitly prepared CUDA graphs belong to the shared model and are invalidated
before residency/attention changes. Other engines retain no extra device state.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from functools import lru_cache
import inspect
import time
from typing import Any
from asr.backend import current_asr_call_context
from asr.qwen_cuda_graph import invalidate_qwen_cuda_graphs, select_qwen_cuda_graph
from core.cuda_execution import cuda_work

ENGINES = ("official", "prefill", "greedy", "cuda_graph")
AUDIO_FA2_TEXT_SDPA_CONFIGS = {
    "model": "flash_attention_2", "thinker": "flash_attention_2",
    "audio_tower": "flash_attention_2", "text_decoder": "sdpa",
}


def _qwen_attention_components(model):
    return {"model": model, "thinker": model.thinker,
            "audio_tower": model.thinker.audio_tower, "text_decoder": model.thinker.model}


def qwen_attention_configs(model):
    """Read live component configs; a loader request is not their execution fact."""
    return {name: getattr(module.config, "_attn_implementation", None)
            for name, module in _qwen_attention_components(model).items()}


def set_qwen_attention_configs(model, targets):
    """Apply a complete component mapping under the caller's lifecycle lock.

    Transformers 4.57's root setter can leave deep-subconfig temporary markers.
    Use each owning component's public setter so repeated changes and mixed
    configurations both reach the live audio/text attention modules.
    """
    if qwen_attention_configs(model) == targets:
        return dict(targets)
    invalidate_qwen_cuda_graphs(model)
    for name, component in _qwen_attention_components(model).items():
        component.set_attn_implementation(targets[name])
    actual = qwen_attention_configs(model)
    if actual != targets:
        raise RuntimeError(f"Attention configuration did not reach every Qwen submodel: {actual}")
    return actual


def apply_qwen_attention_policy(model, loader_attention_requested):
    """FA2-capable NVIDIA loaders retain audio windows and use SDPA text decode.

    CPU, ROCm's existing eager workaround and loaders without FA2 keep their
    current behavior. The loaders already own device/provider selection.
    """
    if loader_attention_requested == "flash_attention_2":
        set_qwen_attention_configs(model, AUDIO_FA2_TEXT_SDPA_CONFIGS)


@dataclass
class _Timings:
    device: Any
    profile: bool
    wall: dict[str, float] = field(default_factory=dict)
    events: dict[str, list[tuple[Any, Any]]] = field(default_factory=dict)
    graph_requests: list[dict] = field(default_factory=list)

    def start(self, stage: str):
        event = None
        if (self.profile and self.device.type == "cuda"
                and stage not in {"frontend", "detokenize"}):
            import torch
            event = torch.cuda.Event(enable_timing=True)
            event.record(torch.cuda.current_stream(self.device))
        return time.perf_counter(), event

    def end(self, stage: str, started):
        before, event = started
        self.wall[stage] = self.wall.get(stage, 0.0) + (time.perf_counter() - before) * 1000
        if event is not None:
            import torch
            end = torch.cuda.Event(enable_timing=True)
            end.record(torch.cuda.current_stream(self.device))
            self.events.setdefault(stage, []).append((event, end))

    @contextmanager
    def measure(self, stage: str):
        started = self.start(stage)
        try:
            yield
        finally:
            self.end(stage, started)

    def gpu_ms(self):
        # Explicit profile only. One final boundary wait; no per-token timing sync.
        if not self.events:
            return {}
        import torch
        torch.cuda.current_stream(self.device).synchronize()
        return {name: sum(begin.elapsed_time(end) for begin, end in pairs)
                for name, pairs in self.events.items()}


@contextmanager
def _model_hooks(thinker, timings: _Timings, last_logits: bool):
    """Keep official multimodal/rope/cache forwards and only trim head input."""
    handles = []
    first = True
    forward_stack = []
    encoder_stack = []

    def before_forward(_module, _args):
        nonlocal first
        if first:
            timings.end("generation_prepare", timings.generation_prepare_started)
        stage = "prefill_including_encoder" if first else "decode_forward"
        first = False
        forward_stack.append((stage, timings.start(stage)))

    def after_forward(_module, _args, _output):
        stage, started = forward_stack.pop()
        timings.end(stage, started)

    def before_encoder(_module, _args):
        encoder_stack.append(timings.start("encoder"))

    def after_encoder(_module, _args, _output):
        timings.end("encoder", encoder_stack.pop())

    try:
        handles.append(thinker.register_forward_pre_hook(before_forward))
        handles.append(thinker.register_forward_hook(after_forward))
        handles.append(thinker.audio_tower.register_forward_pre_hook(before_encoder))
        handles.append(thinker.audio_tower.register_forward_hook(after_encoder))
        if last_logits:
            # generate only consumes logits[:, -1, :]. Keep a sequence dimension
            # and all official embedding/placeholder/M-RoPE/mask/KV behavior.
            handles.append(thinker.lm_head.register_forward_pre_hook(
                lambda _module, args: (args[0][:, -1:, :], *args[1:])))
        yield
    finally:
        for handle in reversed(handles):
            handle.remove()


def _greedy_configuration_supported(generation_config):
    from transformers.generation import GenerationMode
    return (generation_config.get_generation_mode() == GenerationMode.GREEDY_SEARCH
            and generation_config.num_return_sequences == 1 and generation_config.use_cache
            and generation_config.cache_implementation is None
            and generation_config.prefill_chunk_size is None
            and not any((generation_config.output_scores, generation_config.output_logits,
                         generation_config.output_attentions, generation_config.output_hidden_states,
                         generation_config.is_assistant)))


def _greedy_decode(self, input_ids, logits_processor, stopping_criteria,
                   generation_config, synced_gpus=False, streamer=None,
                   decode_timings=None, graph_state=None, graph_statistics=None, **model_kwargs):
    """Transformers custom_generate callable, after *official* preparation.

    This deliberately supports only the live ASR contract: one decoder-only
    greedy request and a dynamic KV cache. Unsupported modes fail visibly;
    there is no exception retry through a second engine.
    """
    import torch
    from transformers.generation.utils import GenerateDecoderOnlyOutput
    from qwen_asr.core.transformers_backend.modeling_qwen3_asr import Qwen3ASRThinkerCausalLMOutputWithPast

    if (input_ids.shape[0] != 1 or self.config.is_encoder_decoder
            or not _greedy_configuration_supported(generation_config) or synced_gpus or streamer is not None):
        raise ValueError("Qwen greedy engine requires batch=1, dynamic-cache, plain greedy generation")

    model_kwargs = self._get_initial_cache_position(input_ids.shape[1], input_ids.device, model_kwargs)
    first = True
    graph_loaded = False
    next_tokens = None  # Prefill selects a token before any continuation step.
    prompt_mask = model_kwargs["attention_mask"]
    while True:
        if first:
            # Only prefill owns multimodal embedding/scatter and initial rope.
            model_inputs = self.prepare_inputs_for_generation(input_ids, **model_kwargs)
            outputs = self(**model_inputs, return_dict=True)
            first = False
        else:
            if graph_state is not None:
                if not graph_loaded:
                    graph_state.load_prefill(model_kwargs["past_key_values"], prompt_mask, self.rope_deltas)
                    graph_loaded = True
                with decode_timings.measure("decode_forward") if decode_timings is not None else nullcontext():
                    logits = graph_state.step(next_tokens, replay=True)
                outputs = Qwen3ASRThinkerCausalLMOutputWithPast(
                    logits=logits, past_key_values=graph_state.cache, rope_deltas=self.rope_deltas)
                if graph_statistics is not None:
                    graph_statistics["replay_steps"] += 1
            else:
                # The loop already knows this is a continuation. Official
                # prepare/thinker each test a CUDA scalar in Python; reuse the
                # owning decoder and continuation rope formula directly.
                cache_position = model_kwargs["cache_position"]
                positions = (cache_position.view(1, 1, -1) + self.rope_deltas.view(1, 1, -1)).expand(3, 1, -1)

                with decode_timings.measure("decode_forward") if decode_timings is not None else nullcontext():
                    decoded = self.model(
                        input_ids=next_tokens[:, None],
                        attention_mask=model_kwargs["attention_mask"], position_ids=positions,
                        past_key_values=model_kwargs["past_key_values"],
                        use_cache=True, cache_position=cache_position, return_dict=True)
                    outputs = Qwen3ASRThinkerCausalLMOutputWithPast(
                        logits=self.lm_head(decoded.last_hidden_state),
                        past_key_values=decoded.past_key_values, rope_deltas=self.rope_deltas)
        model_kwargs = self._update_model_kwargs_for_generation(
            outputs, model_kwargs, is_encoder_decoder=False)
        # Match official float32 score processing (including model defaults).
        logits = outputs.logits[:, -1, :].to(copy=True, dtype=torch.float32, device=input_ids.device)
        scores = logits_processor(input_ids, logits)
        next_tokens = scores.argmax(dim=-1)
        input_ids = torch.cat((input_ids, next_tokens[:, None]), dim=-1)
        del outputs
        # Same per-step stop boundary as official generate, not extra telemetry sync.
        if stopping_criteria(input_ids, None).all():
            break
    if generation_config.return_dict_in_generate:
        return GenerateDecoderOnlyOutput(sequences=input_ids, past_key_values=model_kwargs.get("past_key_values"))
    return input_ids


@lru_cache(maxsize=1)
def _transcriber_class():
    # Lazy import: remote-only installs and manager imports do not require torch.
    from qwen_asr.inference.qwen3_asr import Qwen3ASRModel

    class MeasuredTranscriber(Qwen3ASRModel):
        def _infer_asr_transformers(self, contexts, wavs, languages):
            outs = []
            with self.timings.measure("frontend"):
                texts = [self._build_text_prompt(context=c, force_language=lang)
                         for c, lang in zip(contexts, languages)]
            # The live backend is batch=1; keep official chunks independent.
            for text, wav, context in zip(texts, wavs, contexts):
                with self.timings.measure("frontend"):
                    inputs = self.processor(text=[text], audio=[wav], return_tensors="pt", padding=True)
                    self.context_tokens.append(len(self.processor.tokenizer.encode(context, add_special_tokens=False)))
                with self.timings.measure("h2d"):
                    inputs = inputs.to(self.model.device).to(self.model.dtype)
                thinker = self.model.thinker
                kwargs = {}
                actual_engine = self.engine
                graph_state = None
                if self.engine == "cuda_graph":
                    prompt_tokens = inputs["input_ids"].shape[1]
                    required_capacity = prompt_tokens + self.max_new_tokens
                    if not _greedy_configuration_supported(thinker.generation_config):
                        reason = "unsupported_generation_configuration"
                    else:
                        graph_state, reason = select_qwen_cuda_graph(
                            self.model, required_capacity)
                    if graph_state is None:
                        actual_engine = "official"
                    graph_statistics = {"actual_engine": actual_engine, "fallback_reason": reason,
                                        "capacity": graph_state.capacity if graph_state is not None else None,
                                        "cache_hit": graph_state is not None, "replay_steps": 0,
                                        "capture_ms": 0.0, "prompt_tokens": prompt_tokens,
                                        "required_capacity": required_capacity}
                    self.timings.graph_requests.append(graph_statistics)
                self.actual_engines.append(actual_engine)
                if actual_engine in {"greedy", "cuda_graph"}:
                    if "custom_generate" not in inspect.signature(thinker.generate).parameters:
                        raise RuntimeError("This Transformers runtime lacks callable custom_generate support")
                    kwargs["custom_generate"] = _greedy_decode
                    kwargs["decode_timings"] = self.timings
                    if actual_engine == "cuda_graph":
                        kwargs["graph_state"] = graph_state
                        kwargs["graph_statistics"] = graph_statistics
                with _model_hooks(thinker, self.timings, actual_engine != "official"):
                    with self.timings.measure("generation"):
                        self.timings.generation_prepare_started = self.timings.start("generation_prepare")
                        generated = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens, **kwargs)
                with self.timings.measure("detokenize"):
                    ids = generated.sequences[:, inputs["input_ids"].shape[1]:]
                    ids_cpu = ids.cpu()
                    self.token_ids.append(ids_cpu[0].tolist())
                    decoded = self.processor.batch_decode(ids_cpu, skip_special_tokens=True,
                                                          clean_up_tokenization_spaces=False)
                    outs.extend(list(decoded))
            return outs
    return MeasuredTranscriber


@cuda_work
def transcribe_qwen(wrapper, audio, sample_rate: int, context: str, language: str | None,
                    max_tokens: int, *, engine: str = "official", profile: bool = False):
    """Return official transcription objects plus metadata (never transcript text).

    ``official`` uses the original generation loop, with measured chunk inference;
    ``prefill`` changes only lm_head input; ``greedy`` also replaces its loop.
    Returned token IDs are for local benchmark comparison, not production logs.
    """
    import torch
    if engine not in ENGINES:
        raise ValueError(f"Unknown Qwen inference engine: {engine}")
    if wrapper.backend != "transformers":
        raise ValueError("Measured Qwen inference requires the Transformers backend")
    transcriber = _transcriber_class()(
        backend=wrapper.backend, model=wrapper.model, processor=wrapper.processor,
        forced_aligner=wrapper.forced_aligner, max_inference_batch_size=1, max_new_tokens=max_tokens)
    transcriber.engine = engine
    transcriber.timings = _Timings(torch.device(wrapper.model.device), profile)
    transcriber.context_tokens = []
    transcriber.token_ids = []
    transcriber.actual_engines = []
    started = time.perf_counter()
    with torch.inference_mode():
        results = transcriber.transcribe(audio=(audio, sample_rate), language=language, context=context)
    gpu = transcriber.timings.gpu_ms()
    wall = transcriber.timings.wall
    # Prefill includes embeddings, audio scatter, rope and vocabulary projection.
    # Host values measure elapsed submission/wait time; CUDA events are separate.
    first_wall = wall.pop("prefill_including_encoder", 0.0)
    wall["prefill"] = max(0.0, first_wall - wall.get("encoder", 0.0))
    # Token selection and stop checks are included; preparation is separate.
    wall["decode"] = max(0.0, wall.get("generation", 0.0) - first_wall - wall.get("generation_prepare", 0.0))
    if gpu:
        first_gpu = gpu.pop("prefill_including_encoder", 0.0)
        gpu["prefill"] = max(0.0, first_gpu - gpu.get("encoder", 0.0))
        gpu["decode"] = max(0.0, gpu.get("generation", 0.0) - first_gpu - gpu.get("generation_prepare", 0.0))
    for stage in ("frontend", "h2d", "encoder", "prefill", "decode_forward", "detokenize"):
        wall.setdefault(stage, 0.0)
    tokens = transcriber.token_ids
    # These are the *outer* official generate defaults, which override thinker config.
    eos = inspect.signature(wrapper.model.generate).parameters["eos_token_id"].default
    eos_ids = [eos] if isinstance(eos, int) else list(eos)
    stops = ["eos" if ids and ids[-1] in eos_ids else "budget" if len(ids) >= max_tokens else "criteria"
             for ids in tokens]
    device = torch.device(wrapper.model.device)
    actual = set(transcriber.actual_engines)
    metrics = {**current_asr_call_context(), "engine": next(iter(actual)) if len(actual) == 1 else "mixed",
               "requested_engine": engine, "actual_engines": transcriber.actual_engines, "profile": profile,
               "graph": transcriber.timings.graph_requests,
               "attention_configs": qwen_attention_configs(wrapper.model),
               "device": str(device), "device_name": torch.cuda.get_device_name(device) if device.type == "cuda" else "CPU",
               "audio_ms": len(audio) / sample_rate * 1000, "language": language or "auto",
               "context_tokens": transcriber.context_tokens, "generated_tokens": sum(map(len, tokens)),
               "max_new_tokens": max_tokens, "stop_reasons": stops, "chunk_count": len(tokens),
               "wall_ms": wall, "gpu_ms": gpu, "inference_wall_ms": (time.perf_counter() - started) * 1000,
               "token_ids": tokens}
    return results, metrics
