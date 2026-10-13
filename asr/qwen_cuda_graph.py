"""Model-owned static single-token Qwen decoding; capture is explicitly admitted.

Preparation requires the caller's model lock and a known quiet CUDA window for
all owners in this process. Normal requests never capture on a cache miss.
No global attention/math/thread flags or model weights are changed here.
"""
from __future__ import annotations

import gc
import time
from core.cuda_execution import speech_cuda

_OWNER_ATTRIBUTE = "_amadeus_qwen_cuda_graphs"
_FAILURE_ATTRIBUTE = "_amadeus_qwen_cuda_graph_failure"
DEFAULT_CAPACITIES = (384,)
_FATAL_CUDA_MARKERS = (
    "unspecified launch failure",
    "illegal memory access",
    "device-side assert",
    "cuda error",
    "cublas_status",
    "cudnn_status",
)


def is_fatal_qwen_cuda_error(exc, *, include_context=False):
    """Preserve inference string policy; preparation also checks wrapped errors."""
    error, seen = exc, set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        text = str(error).lower()
        if any(marker in text for marker in _FATAL_CUDA_MARKERS):
            return True
        if not include_context:
            return False
        error = error.__cause__ or error.__context__
    return False


def invalidate_qwen_cuda_graphs(model):
    """Release graph pools, static KV and weight references before model changes."""
    buckets = getattr(model, _OWNER_ATTRIBUTE, None)
    if buckets is not None:
        for state in buckets.values():
            state.close()
        delattr(model, _OWNER_ATTRIBUTE)
    if hasattr(model, _FAILURE_ATTRIBUTE):
        delattr(model, _FAILURE_ATTRIBUTE)


class QwenStaticDecode:
    """The same text decoder with an initialized HF StaticCache and exact mask.

    CPU execution is available for semantic tests. CUDA capture uses synthetic
    zero state only, resets after warmup/capture, and loads the real prefill KV
    only after preparation. The graph updates its own position and valid mask.
    """
    def __init__(self, thinker, capacity: int):
        import torch
        # Persistent graph inputs must be ordinary mutable tensors even when
        # preparation was called by an inference-mode caller.
        self.graph = None
        try:
            with torch.inference_mode(False), torch.no_grad():
                self._initialize(thinker, capacity)
        except Exception:
            # Allocation can fail before the factory returns a state to its
            # caller. Release every partially initialized buffer here too.
            self.close()
            raise

    def _initialize(self, thinker, capacity: int):
        import torch
        from transformers.cache_utils import StaticCache
        if capacity < 1:
            raise ValueError("Static Qwen capacity must be positive")
        self.thinker = thinker
        self.capacity = capacity
        self.device = thinker.device
        self.dtype = thinker.dtype
        config = thinker.model.config
        if (getattr(config, "sliding_window", None) is not None
                or getattr(config, "attention_chunk_size", None) is not None
                or any(kind != "full_attention" for kind in getattr(config, "layer_types", []) or [])):
            raise ValueError("Qwen static decode requires full-attention cache layers")
        self.cache = StaticCache(config=config, max_cache_len=capacity)
        self.cache.early_initialization(1, config.num_key_value_heads, config.head_dim, self.dtype, self.device)
        self.token = torch.zeros((1, 1), dtype=torch.long, device=self.device)
        self.cache_position = torch.zeros((1,), dtype=torch.long, device=self.device)
        self.rope_delta = torch.zeros((1, 1), dtype=torch.long, device=self.device)
        self.positions = torch.zeros((3, 1, 1), dtype=torch.long, device=self.device)
        self.valid = torch.zeros((1, capacity), dtype=torch.bool, device=self.device)
        self.mask = torch.empty((1, 1, 1, capacity), dtype=self.dtype, device=self.device)
        self.graph = None
        self.logits = None
        self.closed = False
        self.statistics = {"capacity": capacity, "capture_ms": 0.0, "warmup_ms": 0.0,
                           "allocated_delta_bytes": 0, "reserved_delta_bytes": 0}
        self.reset()

    def reset(self):
        if self.closed:
            raise RuntimeError("Qwen static decode state has been released")
        import torch
        self.cache.reset()
        self.token.zero_()
        self.cache_position.zero_()
        self.rope_delta.zero_()
        self.positions.zero_()
        self.valid.zero_()
        self.mask.fill_(torch.finfo(self.dtype).min)

    def load_prefill(self, dynamic_cache, attention_mask, rope_deltas):
        """Replace the previous request's KV/validity, retaining fixed addresses."""
        length = attention_mask.shape[-1]
        if attention_mask.shape[0] != 1 or length >= self.capacity:
            raise ValueError("Prefill must fit a single-request static Qwen bucket")
        if len(dynamic_cache.layers) != len(self.cache.layers):
            raise ValueError("Prefill/static Qwen cache layer count differs")
        self.reset()
        for source, target in zip(dynamic_cache.layers, self.cache.layers):
            if source.keys.shape[-2] != length or source.values.shape[-2] != length:
                raise ValueError("Prefill cache length does not match its attention mask")
            target.keys[:, :, :length].copy_(source.keys)
            target.values[:, :, :length].copy_(source.values)
        self.valid[:, :length].copy_(attention_mask.bool())
        self.cache_position.fill_(length)
        self.rope_delta.copy_(rope_deltas)

    def forward(self):
        import torch
        # Include exactly the written prefix/current token. Zero tail KV is not
        # a substitute for a mask: every unwritten/padded slot stays excluded.
        self.valid.scatter_(1, self.cache_position.view(1, 1), True)
        self.mask.fill_(torch.finfo(self.dtype).min)
        self.mask.masked_fill_(self.valid.view(1, 1, 1, self.capacity), 0.0)
        self.positions.copy_((self.cache_position.view(1, 1, 1)
                              + self.rope_delta.view(1, 1, 1)).expand(3, 1, 1))
        outputs = self.thinker.model(
            input_ids=self.token, attention_mask=self.mask, position_ids=self.positions,
            past_key_values=self.cache, use_cache=True, cache_position=self.cache_position,
            return_dict=True)
        logits = self.thinker.lm_head(outputs.last_hidden_state)
        self.cache_position.add_(1)
        return logits

    def step(self, token, *, replay: bool):
        if self.closed:
            raise RuntimeError("Qwen static decode state has been released")
        self.token.copy_(token.reshape(1, 1))
        if replay:
            if self.graph is None:
                raise RuntimeError("Qwen CUDA graph was not prepared")
            self.graph.replay()
            return self.logits
        return self.forward()

    def capture(self):
        import torch
        if self.device.type != "cuda" or getattr(torch.version, "hip", None):
            raise ValueError("Qwen CUDA graph capture requires NVIDIA CUDA")
        allocated = torch.cuda.memory_allocated(self.device)
        reserved = torch.cuda.memory_reserved(self.device)
        # PyTorch 2.6 initializes shared CUDA-generator graph seed/offset state
        # during capture. Inference tensors there poison later no_grad captures
        # by other owners. Do not inherit an outer inference-mode scope.
        with torch.cuda.device(self.device), torch.inference_mode(False), torch.no_grad():
            stream = torch.cuda.Stream(device=self.device)
            current = torch.cuda.current_stream(self.device)
            stream.wait_stream(current)
            started = time.perf_counter()
            with torch.cuda.stream(stream):
                for _ in range(3):
                    self.reset()
                    self.forward()
            stream.synchronize()
            self.statistics["warmup_ms"] = (time.perf_counter() - started) * 1000
            self.reset()
            stream.wait_stream(current)
            graph = torch.cuda.CUDAGraph()
            self.graph = graph
            started = time.perf_counter()
            # Global error checks are intentional. Relaxed capture does not
            # supply the missing cross-owner quiescence guarantee.
            with torch.cuda.graph(graph, stream=stream, capture_error_mode="global"):
                logits = self.forward()
            stream.synchronize()
            current.wait_stream(stream)
            self.graph, self.logits = graph, logits
            self.statistics["capture_ms"] = (time.perf_counter() - started) * 1000
            self.reset()
        self.statistics.update(
            allocated_delta_bytes=torch.cuda.memory_allocated(self.device) - allocated,
            reserved_delta_bytes=torch.cuda.memory_reserved(self.device) - reserved,
            buffer_bytes=self.buffer_bytes())
        return dict(self.statistics)

    def buffer_bytes(self):
        tensors = [self.token, self.cache_position, self.rope_delta, self.positions, self.valid, self.mask]
        for layer in self.cache.layers:
            tensors.extend((layer.keys, layer.values))
        if self.logits is not None:
            tensors.append(self.logits)
        return sum(tensor.numel() * tensor.element_size() for tensor in tensors)

    def close(self):
        self.closed = True
        self.graph = self.logits = self.cache = self.thinker = None
        self.token = self.cache_position = self.rope_delta = self.positions = self.valid = self.mask = None


def prepare_qwen_cuda_graphs(model, capacities=DEFAULT_CAPACITIES, *, capture_allowed=False):
    """Caller admits an exclusive quiet capture window, under its lifecycle lock."""
    capacities = tuple(capacities)
    if not capacities or any(not isinstance(size, int) or size < 2 for size in capacities):
        raise ValueError("Qwen graph capacities must be integers greater than one")
    capacities = tuple(sorted(set(capacities)))
    failure = getattr(model, _FAILURE_ATTRIBUTE, None)
    if failure is not None:
        return {**failure, "failed": True, "deferred": False, "cache_hit": True,
                "buckets": [], "prepared_capacities": []}
    buckets = getattr(model, _OWNER_ATTRIBUTE, {})
    if all(capacity in buckets for capacity in capacities):
        # No CUDA query/synchronization/admission on a fully prepared hot model.
        return {"deferred": False, "buckets": [{**buckets[size].statistics, "cache_hit": True}
                for size in capacities], "prepared_capacities": sorted(buckets)}
    if not capture_allowed:
        raise RuntimeError("Qwen graph preparation requires a caller-admitted quiet CUDA window")
    with speech_cuda.try_capture() as admitted:
        if not admitted:
            return {"deferred": True, "reason": "other_cuda_work_active", "buckets": [],
                    "prepared_capacities": sorted(getattr(model, _OWNER_ATTRIBUTE, {}))}
        return _prepare_admitted(model, capacities)


def _recoverable_preparation_reason(exc, stage):
    import torch
    if isinstance(exc, torch.OutOfMemoryError):
        return "out_of_memory"
    if not isinstance(exc, RuntimeError) or stage != "capture":
        return None
    recoverable_capture_markers = (
        "operation not permitted when stream is capturing",
        "operation failed due to a previous error during capture",
    )
    # capture_end may wrap the original failure. A known capture-invalidated
    # message must not hide a fatal device error in that exception chain.
    error, seen = exc, set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (is_fatal_qwen_cuda_error(error) and not isinstance(error, torch.OutOfMemoryError)
                and not any(marker in str(error).lower() for marker in recoverable_capture_markers)):
            return None
        error = error.__cause__ or error.__context__
    text = str(exc).lower()
    if any(marker in text for marker in recoverable_capture_markers):
        return "capture_unavailable"
    # Preparation may be unsupported without a poisoned CUDA context. This
    # permission to keep the loaded model belongs only to the capture stage;
    # unknown CUDA/library errors and constructor contracts still propagate.
    if not any(marker in text for marker in ("cuda", "cublas", "cudnn")):
        return "capture_failed"
    return None


def _prepare_admitted(model, capacities):
    import torch
    if model.device.type != "cuda" or getattr(torch.version, "hip", None):
        raise ValueError("Qwen CUDA graph preparation requires NVIDIA CUDA")
    if model.thinker.model.config._attn_implementation != "sdpa":
        raise ValueError("Qwen CUDA graph preparation requires the measured SDPA text decoder")
    state = None
    stage = "synchronize"
    try:
        # Leases have drained, but asynchronous work may outlive its Python call.
        torch.cuda.synchronize(model.device)
        buckets = getattr(model, _OWNER_ATTRIBUTE, None)
        if buckets is None:
            buckets = {}
            setattr(model, _OWNER_ATTRIBUTE, buckets)
        evidence = []
        for capacity in capacities:
            if capacity in buckets:
                evidence.append({**buckets[capacity].statistics, "cache_hit": True})
                continue
            allocated = torch.cuda.memory_allocated(model.device)
            reserved = torch.cuda.memory_reserved(model.device)
            stage = "allocation"
            state = QwenStaticDecode(model.thinker, capacity)
            stage = "capture"
            state.capture()
            buckets[capacity] = state
            state.statistics.update(allocated_delta_bytes=torch.cuda.memory_allocated(model.device) - allocated,
                                    reserved_delta_bytes=torch.cuda.memory_reserved(model.device) - reserved)
            evidence.append({**state.statistics, "cache_hit": False})
            state = None
        return {"deferred": False, "buckets": evidence, "prepared_capacities": sorted(buckets)}
    except Exception as exc:
        if state is not None:
            state.close()
        invalidate_qwen_cuda_graphs(model)
        reason = _recoverable_preparation_reason(exc, stage)
        if reason is None:
            raise
        message = str(exc).splitlines()
        failure = {"reason": reason, "stage": stage,
                   "error": message[0] if message else type(exc).__name__}
        setattr(model, _FAILURE_ATTRIBUTE, failure)
        # Keep only diagnostic strings. Exception frames can otherwise retain
        # failed-constructor buffers or capture-local graph pools during cleanup.
        exc.__traceback__ = exc.__cause__ = exc.__context__ = None
        gc.collect()
        torch.cuda.empty_cache()
        return {**failure, "failed": True, "deferred": False, "cache_hit": False,
                "buckets": [], "prepared_capacities": []}


def qwen_cuda_graph_support_reason(model):
    failure = getattr(model, _FAILURE_ATTRIBUTE, None)
    if failure is not None:
        return "graph_preparation_failed:" + failure["reason"]
    import torch
    if model.device.type != "cuda":
        return "not_cuda"
    if getattr(torch.version, "hip", None):
        return "rocm"
    if model.thinker.model.config._attn_implementation != "sdpa":
        return "text_attention_not_sdpa"
    return None


def select_qwen_cuda_graph(model, required_capacity):
    """Choose before generation. Missing readiness never triggers live capture."""
    reason = qwen_cuda_graph_support_reason(model)
    if reason is not None:
        return None, reason
    buckets = getattr(model, _OWNER_ATTRIBUTE, {})
    for capacity in sorted(buckets):
        if required_capacity <= capacity:
            return buckets[capacity], None
    return None, "capacity_exceeded" if buckets else "graphs_not_prepared"
