"""Acceleration preferences resolved against the speech runtime's actual device."""
from __future__ import annotations

import os


def acceleration_mode(key: str, *, legacy_key: str = "") -> str:
    raw = os.environ.get(key, os.environ.get(legacy_key, "auto") if legacy_key else "auto").strip().lower()
    if raw in {"", "auto"}:
        return "auto"
    if raw in {"1", "true", "yes", "on"}:
        return "1"
    if raw in {"0", "false", "no", "off"}:
        return "0"
    raise ValueError(f"{key} must be auto, 0 or 1")


def cuda_graph_enabled(device: object, *, is_rocm: bool = False) -> bool:
    mode = acceleration_mode("ENABLE_CUDA_GRAPH")
    # HIP retains its existing explicit opt-in; automatic selection is NVIDIA-only.
    return str(device).split(":", 1)[0] == "cuda" and mode != "0" and (mode == "1" or not is_rocm)


def flash_attention_eligible(device: object, *, is_rocm: bool, dtype: str, capability: tuple[int, int]) -> bool:
    mode = acceleration_mode("TTS_T2S_FLASH_ATTN", legacy_key="ENABLE_T2S_FLASH_ATTN_KVCACHE")
    return (mode != "0" and not is_rocm and str(device).split(":", 1)[0] == "cuda"
            and dtype in {"torch.float16", "torch.bfloat16"} and capability[0] >= 8)
