"""Owned-buffer Torch/MLX bridge, production qualification, and call lifetime."""
from __future__ import annotations

import importlib.metadata
import platform
import threading
import time

import numpy as np
import mlx.core as mx

from .generation import generate
from .model import T2SModel
from .weights import export_checkpoint


def require_metal(acoustic_device):
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise RuntimeError("TTS_T2S_BACKEND=mlx requires Apple Silicon macOS; use torch and restart")
    if str(acoustic_device) != "mps" or not mx.metal.is_available():
        raise RuntimeError("MLX T2S requires MLX Metal and TTS_DEVICE=mps; use torch and restart")
    with mx.stream(mx.gpu):
        check = mx.sum(mx.ones((2, 2)) @ mx.ones((2, 2)))
        mx.eval(check)
        if check.item() != 8:
            raise RuntimeError("MLX Metal computation failed")


class MLXSemanticDecoder:
    backend = "mlx"

    def __init__(self, model, *, device, purpose):
        self.model = model
        self.device = device
        self.purpose = purpose
        self._lock = threading.Lock()
        self.last_timings = {}
        self.info = {
            "semantic_backend": "mlx", "mlx": importlib.metadata.version("mlx"),
            "dtype": "float32", "cache_impl": "mlx_dynamic_reference",
            "cuda_graph": "not_applicable", "compile": False,
            "purpose": purpose, "mlx_device": "cpu" if device == mx.cpu else "metal",
        }

    @classmethod
    def from_checkpoint(cls, checkpoint, *, acoustic_device, cache_root):
        require_metal(acoustic_device)
        directory = export_checkpoint(checkpoint, cache_root)
        with mx.stream(mx.gpu):
            model = T2SModel.from_artifact(directory)
        return cls(model, device=mx.gpu, purpose="production_experiment")

    @classmethod
    def for_numerical_test(cls, artifact, *, device=mx.cpu):
        with mx.stream(device):
            model = T2SModel.from_artifact(artifact)
        return cls(model, device=device, purpose="numerical_test")

    def infer_panel(self, x, x_lens, prompts, bert_feature, top_k=-100, top_p=100,
                    early_stop_num=-1, temperature=1.0, repetition_penalty=1.35,
                    *, enable_cuda_graph=None, enable_static_kv=None):
        import torch

        with self._lock, mx.stream(self.device):
            model = self.model
            if model is None:
                raise RuntimeError("MLX semantic decoder is closed")
            started = time.perf_counter()
            if prompts is None or x.ndim != 2 or x.shape[0] != 1:
                raise ValueError("Experimental MLX T2S supports batch=1 with reference semantic tokens")
            if prompts.ndim != 2 or tuple(prompts.shape[:1]) != (1,) or prompts.shape[1] == 0:
                raise ValueError("MLX T2S requires a nonempty batch=1 reference")
            if tuple(bert_feature.shape) != (1, model.config.bert_dim, x.shape[1]):
                raise ValueError("MLX BERT input must have shape [1, bert_dim, phone_length]")
            if x_lens.numel() != 1 or int(x_lens.item()) != x.shape[1] or x.shape[1] == 0:
                raise ValueError("MLX T2S requires one unpadded phone sequence")
            arrays = []
            for value, bound in ((x, model.config.phoneme_vocab_size),
                                 (prompts, model.config.eos)):
                if value.dtype not in (torch.int32, torch.int64):
                    raise ValueError("MLX phoneme and semantic IDs must be integer tensors")
                owned = value.detach().cpu().numpy().copy()
                if owned.min() < 0 or owned.max() >= bound:
                    raise ValueError("MLX input ID is outside the checkpoint vocabulary")
                arrays.append(mx.array(owned.astype(np.int32)))
            bert = np.array(bert_feature.detach().float().cpu().numpy(), copy=True)
            if not np.isfinite(bert).all():
                raise ValueError("MLX BERT input contains non-finite values")
            arrays.append(mx.array(bert))
            mx.eval(arrays)
            bridged = time.perf_counter()
            prediction, idx = generate(
                model, *arrays, top_k=top_k, top_p=top_p, temperature=temperature,
                repetition_penalty=repetition_penalty, early_stop_num=early_stop_num,
            )
            mx.eval(prediction)
            decoded = time.perf_counter()
            result = torch.from_numpy(np.array(prediction, dtype=np.int64, copy=True)).to(x.device)
            if x.device.type == "mps":
                torch.mps.synchronize()
            elif x.device.type == "cuda":
                torch.cuda.synchronize(x.device)
            finished = time.perf_counter()
            self.last_timings = {
                "bridge_in_ms": (bridged - started) * 1000,
                "ar_ms": (decoded - bridged) * 1000,
                "bridge_out_ms": (finished - decoded) * 1000,
                "semantic_total_ms": (finished - started) * 1000,
            }
            return result, idx

    def close(self):
        with self._lock:
            self.model = None
