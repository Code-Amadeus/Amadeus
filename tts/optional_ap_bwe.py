"""Lazy boundary for the optional AP-BWE audio post-processor."""

from __future__ import annotations

import importlib
from typing import Any


class APBWEUnavailable(RuntimeError):
    """Raised when the optional AP-BWE implementation is not installed."""


def create_ap_bwe(device: Any, config_adapter: Any):
    """Create AP-BWE only when an explicit super-resolution request needs it."""

    try:
        module = importlib.import_module("tools.audio_sr")
    except ImportError as exc:
        raise APBWEUnavailable(
            "AP-BWE is an optional audio super-resolution component and is not installed"
        ) from exc

    backend = getattr(module, "AP_BWE", None)
    if backend is None:
        raise APBWEUnavailable("tools.audio_sr does not expose AP_BWE")
    return backend(device, config_adapter)
