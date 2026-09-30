"""Internal semantic boundary; importing it never imports Torch or MLX.

Generation parameters and the (prediction, idx) result belong to the existing
infer_panel contract. CUDA graph/static KV flags are Torch execution hints.
"""
from __future__ import annotations

import importlib.metadata
import importlib
from typing import Any, Protocol


class SemanticDecoder(Protocol):
    def infer_panel(self, *args: Any, **kwargs: Any) -> tuple[Any, int]: ...


class TorchSemanticDecoder:
    backend = "torch"

    def __init__(self, decoder: Any):
        self.decoder = decoder
        self.info = {
            "semantic_backend": "torch",
            "torch": importlib.metadata.version("torch"),
        }

    def infer_panel(self, *args: Any, **kwargs: Any) -> tuple[Any, int]:
        return self.decoder.infer_panel(*args, **kwargs)


def load_mlx_decoder(checkpoint, *, acoustic_device, cache_root):
    try:
        importlib.import_module("mlx.core")
        importlib.import_module("safetensors")
    except ImportError as exc:
        raise RuntimeError(
            "TTS_T2S_BACKEND=mlx requires working optional mlx-t2s dependencies "
            "and the MLX native runtime on Apple Silicon; install them, then restart"
        ) from exc

    from tts.semantic_mlx.runtime import MLXSemanticDecoder

    return MLXSemanticDecoder.from_checkpoint(
        checkpoint, acoustic_device=acoustic_device, cache_root=cache_root
    )
