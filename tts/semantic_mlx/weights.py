"""Strict, content-addressed derivation from the existing trusted checkpoint."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile


REFERENCE_SHA = "77161583eb32b4ae6893289e1e020731b6ae0b5c"
OMINIX_SHA = "4988a3fcfa48b8cb5d0780a501b92c6a41401523"
CONVERTER_REVISION = "fp32-t2s-v1"
CONTRACT_REVISION = "infer-panel-naive-v1"
SCHEMA = "amadeus.gsv_t2s_conversion.v1"


@dataclass(frozen=True)
class T2SConfig:
    hidden_dim: int
    num_heads: int
    num_layers: int
    phoneme_vocab_size: int
    vocab_size: int
    eos: int
    max_sec: float
    bert_dim: int = 1024
    norm_eps: float = 1e-5
    x_scale: float = 1.0

    @property
    def ffn_dim(self):
        # This is what Text2SemanticDecoder constructs; it does not consume
        # the training config's optional linear_units field.
        return self.hidden_dim * 4

    @classmethod
    def from_checkpoint(cls, config):
        try:
            model = config["model"]
            result = cls(
                hidden_dim=int(model["hidden_dim"]),
                num_heads=int(model["head"]), num_layers=int(model["n_layer"]),
                phoneme_vocab_size=int(model["phoneme_vocab_size"]),
                vocab_size=int(model["vocab_size"]), eos=int(model["EOS"]),
                max_sec=float(config["data"]["max_sec"]),
            )
            embedding_dim = int(model["embedding_dim"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Unsupported T2S checkpoint configuration") from exc
        if (min(result.hidden_dim, result.num_heads, result.num_layers,
                result.phoneme_vocab_size, result.vocab_size) <= 0
                or result.hidden_dim % result.num_heads
                or result.hidden_dim % 2
                or embedding_dim != result.hidden_dim
                or result.eos != result.vocab_size - 1
                or not math.isfinite(result.max_sec) or result.max_sec <= 0):
            raise ValueError("Unsupported T2S dimensions, EOS, or generation budget")
        return result

    def expected_shapes(self):
        h = self.hidden_dim
        shapes = {
            "bert_proj.weight": (h, self.bert_dim), "bert_proj.bias": (h,),
            "ar_text_embedding.word_embeddings.weight": (self.phoneme_vocab_size, h),
            "ar_audio_embedding.word_embeddings.weight": (self.vocab_size, h),
            "ar_text_position.alpha": (1,), "ar_audio_position.alpha": (1,),
            "ar_predict_layer.weight": (self.vocab_size, h),
        }
        layer = {
            "self_attn.in_proj_weight": (3 * h, h), "self_attn.in_proj_bias": (3 * h,),
            "self_attn.out_proj.weight": (h, h), "self_attn.out_proj.bias": (h,),
            "linear1.weight": (self.ffn_dim, h), "linear1.bias": (self.ffn_dim,),
            "linear2.weight": (h, self.ffn_dim), "linear2.bias": (h,),
            "norm1.weight": (h,), "norm1.bias": (h,),
            "norm2.weight": (h,), "norm2.bias": (h,),
        }
        for index in range(self.num_layers):
            shapes.update({f"h.layers.{index}.{name}": shape for name, shape in layer.items()})
        return shapes


def file_sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_checkpoint(path):
    """Load only a user-selected trusted local checkpoint; no unsafe retry."""
    import torch

    with Path(path).open("rb") as handle:
        identity = hashlib.file_digest(handle, "sha256").hexdigest()
        handle.seek(0)
        checkpoint = torch.load(handle, map_location="cpu", weights_only=True)
    config = T2SConfig.from_checkpoint(checkpoint["config"])
    expected = config.expected_shapes()
    source = checkpoint["weight"]
    expected_source = {"model." + key for key in expected}
    missing = sorted(expected_source - source.keys())
    unexpected = sorted(source.keys() - expected_source)
    if missing or unexpected:
        raise ValueError(f"T2S weight keys mismatch: missing={missing}, unexpected={unexpected}")
    weights = {}
    for key, shape in expected.items():
        value = source["model." + key]
        if not isinstance(value, torch.Tensor) or tuple(value.shape) != shape:
            raise ValueError(f"T2S weight shape mismatch: {key}; expected {shape}")
        if value.dtype not in (torch.float16, torch.float32, torch.bfloat16):
            raise ValueError(f"Unsupported weight dtype: {key}: {value.dtype}")
        if not bool(torch.isfinite(value).all()):
            raise ValueError(f"Non-finite T2S weight: {key}")
        weights[key] = value.detach().contiguous()
    return config, weights, identity


def validate_artifact(directory, *, source_sha=None, source_config=None):
    """Reject damaged/stale artifacts instead of silently changing engines."""
    from safetensors import safe_open

    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["schema"] != SCHEMA
            or manifest["converter_revision"] != CONVERTER_REVISION
            or manifest["runtime_contract_revision"] != CONTRACT_REVISION
            or (source_sha is not None and manifest["source_checkpoint_sha256"] != source_sha)):
        raise ValueError("MLX conversion identity mismatch; export to a fresh cache directory")
    weights_path = directory / "model.safetensors"
    if file_sha256(weights_path) != manifest["weights_sha256"]:
        raise ValueError("MLX converted weights checksum mismatch; artifact is damaged")
    config = T2SConfig(**manifest["config"])
    if source_config is not None and config != source_config:
        raise ValueError("MLX conversion configuration differs from source checkpoint")
    if (config.hidden_dim <= 0 or config.num_heads <= 0
            or config.hidden_dim % config.num_heads or config.hidden_dim % 2
            or config.num_layers <= 0 or config.phoneme_vocab_size <= 0
            or config.vocab_size <= 0 or config.bert_dim != 1024
            or config.norm_eps != 1e-5 or config.x_scale != 1.0
            or config.eos != config.vocab_size - 1
            or not math.isfinite(config.max_sec) or config.max_sec <= 0):
        raise ValueError("MLX conversion configuration is invalid")
    expected = config.expected_shapes()
    with safe_open(weights_path, framework="numpy") as handle:
        if set(handle.keys()) != set(expected):
            raise ValueError("MLX artifact weight keys mismatch")
        for key, shape in expected.items():
            if tuple(handle.get_slice(key).get_shape()) != shape:
                raise ValueError(f"MLX artifact weight shape mismatch: {key}")
    return config, manifest


def export_checkpoint(checkpoint, cache_root):
    from safetensors.torch import save_file

    config, weights, identity = read_checkpoint(checkpoint)
    parent = Path(cache_root) / identity
    target = parent / CONVERTER_REVISION
    if target.exists():
        validate_artifact(target, source_sha=identity, source_config=config)
        return target
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".conversion-", dir=parent))
    try:
        weights_path = staging / "model.safetensors"
        save_file(weights, str(weights_path))
        manifest = {
            "schema": SCHEMA, "source_checkpoint_sha256": identity,
            "amadeus_reference_sha": REFERENCE_SHA,
            "reference_implementation": "OminiX-ai/OminiX-MLX",
            "reference_implementation_sha": OMINIX_SHA,
            "converter_revision": CONVERTER_REVISION,
            "runtime_contract_revision": CONTRACT_REVISION,
            "config": asdict(config),
            "source_dtypes": sorted({str(value.dtype) for value in weights.values()}),
            "inference_dtype": "float32", "weights_sha256": file_sha256(weights_path),
            "validated_on": [],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        validate_artifact(staging, source_sha=identity, source_config=config)
        try:
            os.rename(staging, target)
        except OSError:
            if not target.is_dir():
                raise
            validate_artifact(target, source_sha=identity, source_config=config)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
