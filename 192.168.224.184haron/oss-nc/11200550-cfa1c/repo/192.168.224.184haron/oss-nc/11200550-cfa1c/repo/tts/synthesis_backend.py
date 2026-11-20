"""Synthesis backend selection for the TTS worker."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from config.settings import USE_EXPERIMENTAL_TTS_STREAM

SynthesisFn = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class SynthesisBackends:
    cuda_graph: SynthesisFn
    experimental: SynthesisFn
    default: SynthesisFn


def select_synthesis(
    job: Any,
    *,
    cuda_graph_enabled: bool,
    experimental_enabled: bool | None = None,
    backends: SynthesisBackends,
    logger=None,
) -> tuple[str, SynthesisFn]:
    """Return (backend_name, synthesis coroutine factory)."""
    del job  # reserved for later per-job routing without changing the worker API
    if experimental_enabled is None:
        experimental_enabled = bool(USE_EXPERIMENTAL_TTS_STREAM)

    if cuda_graph_enabled:
        if logger is not None:
            logger.info("[Graph Serial Mode] CUDA Graph enabled; switching to serial TTS production")
        return "cuda_graph_serial", backends.cuda_graph
    if experimental_enabled:
        if logger is not None:
            logger.info("[Batch Synthesis] batch synthesis with concurrent processing")
        return "experimental_asyncio_queue", backends.experimental
    return "enhanced", backends.default
