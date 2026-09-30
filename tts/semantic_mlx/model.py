"""POST-LN/ReLU T2S matching Amadeus's actual inference blocks.

Reference: GPT_SoVITS/AR/models/t2s_model.py at 77161583. OminiX T2S at
4988a3fc was consulted for MLX layout; source behavior remains authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import mlx.core as mx

from .weights import T2SConfig, validate_artifact


@dataclass
class KVCache:
    """Per-generation cache; text_length + audio_length is the valid prefix."""
    layers: list[tuple[mx.array, mx.array]]
    text_length: int
    audio_length: int


class T2SModel:
    CACHE_STEP = 256

    def __init__(self, config: T2SConfig, weights: dict[str, mx.array], *,
                 inference_dtype="float32"):
        if inference_dtype not in {"float32", "float16"}:
            raise ValueError("MLX inference dtype must be float32 or float16")
        self.config = config
        self.head_dim = config.hidden_dim // config.num_heads
        # MLX 0.32.2 Metal SDPA supports 64-wide heads, but not 32-wide
        # heads. Zero feature lanes preserve attention with the logical
        # scale below; storing them here doubles KV capacity for head_dim=32.
        self.cache_head_dim = 64 if self.head_dim == 32 else self.head_dim
        self.inference_dtype = inference_dtype
        self.dtype = mx.float16 if inference_dtype == "float16" else mx.float32
        expected = config.expected_shapes()
        if set(weights) != set(expected):
            raise ValueError("T2S model weight keys do not match configuration")
        for key, shape in expected.items():
            if tuple(weights[key].shape) != shape:
                raise ValueError(f"T2S model weight shape mismatch: {key}")
        self.weights = {key: value.astype(self.dtype) for key, value in weights.items()}
        # The exporter accepts finite FP32/BF16 weights too. A chosen FP16
        # runtime must reject overflow introduced by that explicit downcast.
        finite_checks = [(key, mx.all(mx.isfinite(self.weights[key])))
                         for key, value in weights.items()
                         if self.dtype == mx.float16 and value.dtype != mx.float16]
        self.positions = self._positions(4000)
        mx.eval(list(self.weights.values()), self.positions,
                [check for _, check in finite_checks])
        for key, check in finite_checks:
            if not bool(check.item()):
                raise ValueError(f"MLX float16 inference overflows source weight: {key}")

    @classmethod
    def from_artifact(cls, directory, *, inference_dtype="float32", validated_config=None):
        from pathlib import Path

        config = validated_config
        if config is None:
            config, _ = validate_artifact(directory)
        return cls(config, mx.load(str(Path(directory) / "model.safetensors")),
                   inference_dtype=inference_dtype)

    def _positions(self, length):
        h = self.config.hidden_dim
        frequencies = mx.exp(mx.arange(0, h, 2, dtype=mx.float32) * -(math.log(10000.0) / h))
        angles = mx.arange(length, dtype=mx.float32)[:, None] * frequencies[None, :]
        return mx.stack((mx.sin(angles), mx.cos(angles)), axis=-1).reshape(1, length, h).astype(self.dtype)

    def _position(self, value, kind, offset=0):
        required = offset + value.shape[1]
        if required > self.positions.shape[1]:
            self.positions = self._positions(required)
        return (value * self.config.x_scale
                + self.weights[f"ar_{kind}_position.alpha"]
                * self.positions[:, offset:required])

    def _linear(self, value, name, bias=True):
        if bias:
            return mx.addmm(self.weights[name + ".bias"],
                            value, self.weights[name + ".weight"].T)
        return value @ self.weights[name + ".weight"].T

    def _norm(self, value, name):
        return mx.fast.layer_norm(value, self.weights[name + ".weight"],
                                  self.weights[name + ".bias"], self.config.norm_eps)

    def _cache_prefix(self, previous, k, v, valid_length):
        if previous is None:
            capacity = ((valid_length + self.CACHE_STEP - 1) // self.CACHE_STEP) * self.CACHE_STEP
            shape = (k.shape[0], k.shape[1], capacity, k.shape[3])
            return (mx.slice_update(mx.zeros(shape, dtype=self.dtype), k,
                                    mx.array([0]), axes=(2,)),
                    mx.slice_update(mx.zeros(shape, dtype=self.dtype), v,
                                    mx.array([0]), axes=(2,)))
        capacity = previous[0].shape[2]
        if valid_length > capacity:
            extension = self.CACHE_STEP
            shape = (k.shape[0], k.shape[1], extension, k.shape[3])
            previous = (mx.concatenate((previous[0], mx.zeros(shape, dtype=self.dtype)), axis=2),
                        mx.concatenate((previous[1], mx.zeros(shape, dtype=self.dtype)), axis=2))
        offset = valid_length - k.shape[2]
        start = mx.array([offset])
        return (mx.slice_update(previous[0], k, start, axes=(2,)),
                mx.slice_update(previous[1], v, start, axes=(2,)))

    def _block(self, value, index, mask=None, previous=None, valid_length=None):
        name = f"h.layers.{index}."
        qkv = mx.addmm(self.weights[name + "self_attn.in_proj_bias"], value,
                       self.weights[name + "self_attn.in_proj_weight"].T)
        q, k, v = mx.split(qkv, 3, axis=-1)
        heads = self.config.num_heads
        dim = self.head_dim
        q, k, v = [item.reshape(1, -1, heads, dim).transpose(0, 2, 1, 3) for item in (q, k, v)]
        if self.cache_head_dim != dim:
            # Pad only the new rows, before inserting K/V into the cache.
            padding = [(0, 0), (0, 0), (0, 0), (0, self.cache_head_dim - dim)]
            q, k, v = [mx.pad(item, padding) for item in (q, k, v)]
        if valid_length is None:
            raise ValueError("MLX attention requires a valid cache length")
        cached = self._cache_prefix(previous, k, v, valid_length)
        k, v = (item[:, :, :valid_length] for item in cached)
        attention = mx.fast.scaled_dot_product_attention(
            q, k, v, scale=1 / math.sqrt(dim), mask=mask)
        attention = attention[..., :dim]
        attention = attention.transpose(0, 2, 1, 3).reshape(1, -1, self.config.hidden_dim)
        value = self._norm(value + self._linear(attention, name + "self_attn.out_proj"), name + "norm1")
        feedforward = self._linear(mx.maximum(self._linear(value, name + "linear1"), 0), name + "linear2")
        return self._norm(value + feedforward, name + "norm2"), cached

    @staticmethod
    def attention_mask(text_length, audio_length):
        length = text_length + audio_length
        query = mx.arange(length)[:, None]
        key = mx.arange(length)[None, :]
        return mx.where(query < text_length, key < text_length, key <= query)[None, None, :, :]

    def prefill(self, phones, prompt, bert, *, trace=None):
        # Public boundary BERT layout is [B, 1024, phone_length].
        text = self.weights["ar_text_embedding.word_embeddings.weight"][phones]
        text = self._position(text + self._linear(bert.astype(self.dtype).transpose(0, 2, 1), "bert_proj"), "text")
        audio = self._position(self.weights["ar_audio_embedding.word_embeddings.weight"][prompt], "audio")
        value = mx.concatenate((text, audio), axis=1)
        if trace is not None:
            trace["embedding"] = value
        mask = self.attention_mask(phones.shape[1], prompt.shape[1])
        layers = []
        for index in range(self.config.num_layers):
            value, kv = self._block(value, index, mask, valid_length=value.shape[1])
            layers.append(kv)
            if trace is not None:
                trace[f"layer_{index}"] = value
        logits = self._linear(value[:, -1], "ar_predict_layer", bias=False)
        return logits, KVCache(layers, phones.shape[1], prompt.shape[1])

    def decode_step(self, token, cache, *, trace=None):
        value = self.weights["ar_audio_embedding.word_embeddings.weight"][token]
        value = self._position(value, "audio", cache.audio_length)
        if trace is not None:
            trace["embedding"] = value
        layers = []
        for index, previous in enumerate(cache.layers):
            value, kv = self._block(value, index, previous=previous,
                                    valid_length=cache.text_length + cache.audio_length + 1)
            layers.append(kv)
            if trace is not None:
                trace[f"layer_{index}"] = value
        logits = self._linear(value[:, -1], "ar_predict_layer", bias=False)
        return logits, KVCache(layers, cache.text_length, cache.audio_length + 1)
