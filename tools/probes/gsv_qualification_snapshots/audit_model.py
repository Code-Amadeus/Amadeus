"""FP32, POST-LN/ReLU T2S matching Amadeus's actual inference blocks.

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
    """Per-generation cache containing only valid positions; no capacity gaps."""
    layers: list[tuple[mx.array, mx.array]]
    text_length: int
    audio_length: int


class T2SModel:
    def __init__(self, config: T2SConfig, weights: dict[str, mx.array]):
        self.config = config
        expected = config.expected_shapes()
        if set(weights) != set(expected):
            raise ValueError("T2S model weight keys do not match configuration")
        for key, shape in expected.items():
            if tuple(weights[key].shape) != shape:
                raise ValueError(f"T2S model weight shape mismatch: {key}")
        self.weights = {key: value.astype(mx.float32) for key, value in weights.items()}
        self.positions = self._positions(4000)
        mx.eval(list(self.weights.values()), self.positions)

    @classmethod
    def from_artifact(cls, directory):
        from pathlib import Path

        config, _ = validate_artifact(directory)
        return cls(config, mx.load(str(Path(directory) / "model.safetensors")))

    def _positions(self, length):
        h = self.config.hidden_dim
        frequencies = mx.exp(mx.arange(0, h, 2, dtype=mx.float32) * -(math.log(10000.0) / h))
        angles = mx.arange(length, dtype=mx.float32)[:, None] * frequencies[None, :]
        return mx.stack((mx.sin(angles), mx.cos(angles)), axis=-1).reshape(1, length, h)

    def _position(self, value, kind, offset=0):
        required = offset + value.shape[1]
        if required > self.positions.shape[1]:
            self.positions = self._positions(required)
        return (value * self.config.x_scale
                + self.weights[f"ar_{kind}_position.alpha"]
                * self.positions[:, offset:required])

    def _linear(self, value, name, bias=True):
        result = value @ self.weights[name + ".weight"].T
        return result + self.weights[name + ".bias"] if bias else result

    def _norm(self, value, name):
        centered = value - mx.mean(value, axis=-1, keepdims=True)
        normalized = centered * mx.rsqrt(mx.mean(centered * centered, axis=-1, keepdims=True)
                                        + self.config.norm_eps)
        return normalized * self.weights[name + ".weight"] + self.weights[name + ".bias"]

    def _block(self, value, index, mask=None, previous=None):
        name = f"h.layers.{index}."
        qkv = (value @ self.weights[name + "self_attn.in_proj_weight"].T
               + self.weights[name + "self_attn.in_proj_bias"])
        q, k, v = mx.split(qkv, 3, axis=-1)
        heads = self.config.num_heads
        dim = self.config.hidden_dim // heads
        q, k, v = [item.reshape(1, -1, heads, dim).transpose(0, 2, 1, 3) for item in (q, k, v)]
        if previous is not None:
            k = mx.concatenate((previous[0], k), axis=2)
            v = mx.concatenate((previous[1], v), axis=2)
        scores = (q @ k.transpose(0, 1, 3, 2)) * (1 / math.sqrt(dim))
        if mask is not None:
            scores = scores + mask
        attention = mx.softmax(scores, axis=-1, precise=True) @ v
        attention = attention.transpose(0, 2, 1, 3).reshape(1, -1, self.config.hidden_dim)
        value = self._norm(value + self._linear(attention, name + "self_attn.out_proj"), name + "norm1")
        feedforward = self._linear(mx.maximum(self._linear(value, name + "linear1"), 0), name + "linear2")
        return self._norm(value + feedforward, name + "norm2"), (k, v)

    @staticmethod
    def attention_mask(text_length, audio_length):
        length = text_length + audio_length
        query = mx.arange(length)[:, None]
        key = mx.arange(length)[None, :]
        blocked = mx.where(query < text_length, key >= text_length, key > query)
        return mx.where(blocked, -float("inf"), 0.0)[None, None, :, :]

    def prefill(self, phones, prompt, bert, *, trace=None):
        # Public boundary BERT layout is [B, 1024, phone_length].
        text = self.weights["ar_text_embedding.word_embeddings.weight"][phones]
        text = self._position(text + self._linear(bert.transpose(0, 2, 1), "bert_proj"), "text")
        audio = self._position(self.weights["ar_audio_embedding.word_embeddings.weight"][prompt], "audio")
        value = mx.concatenate((text, audio), axis=1)
        if trace is not None:
            trace["embedding"] = value
        mask = self.attention_mask(phones.shape[1], prompt.shape[1])
        layers = []
        for index in range(self.config.num_layers):
            value, kv = self._block(value, index, mask)
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
            value, kv = self._block(value, index, previous=previous)
            layers.append(kv)
            if trace is not None:
                trace[f"layer_{index}"] = value
        logits = self._linear(value[:, -1], "ar_predict_layer", bias=False)
        return logits, KVCache(layers, cache.text_length, cache.audio_length + 1)
