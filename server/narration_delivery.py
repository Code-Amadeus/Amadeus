"""Source-neutral boundary for host-authored narration delivery.

This module intentionally owns no cadence, priority, queue, retry, merge,
supersession, terminal, or retention policy.  Work, VN, and AUIP decide what
to say before calling it.  The existing TTS pipeline remains the sole speech
scheduler; this boundary only preserves source identity and normalizes the
sink's enqueue receipt.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

NarrationSourceKind = Literal["work", "auip", "vn", "host"]
NarrationSink = Callable[[dict[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]] | Any]

_SOURCE_KINDS = frozenset({"work", "auip", "vn", "host"})
_SINK_STATUSES = frozenset({"queued", "dropped", "skipped", "error"})
_DIAGNOSTIC_KEYS = (
    "reason",
    "sentence_id",
    "last_sentence_id",
    "pending",
    "mode",
)


@dataclass(frozen=True, slots=True)
class NarrationRequest:
    """One already-authored utterance offered to the existing TTS sink."""

    request_id: str
    source_kind: NarrationSourceKind
    source_id: str
    session_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        request_id = str(self.request_id or "").strip()
        source_kind = str(self.source_kind or "").strip().lower()
        source_id = str(self.source_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        if source_kind not in _SOURCE_KINDS:
            raise ValueError(f"unsupported narration source: {source_kind or '<empty>'}")
        if not source_id:
            raise ValueError("source_id is required")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class NarrationDeliveryReceipt:
    request_id: str
    source_kind: NarrationSourceKind
    source_id: str
    status: str
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        """True only when the real sink confirmed enqueueing."""

        return self.status == "queued"

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "status": self.status,
            "accepted": self.accepted,
            **dict(self.diagnostics),
        }


async def deliver_narration(
    request: NarrationRequest,
    sink: NarrationSink | None,
) -> NarrationDeliveryReceipt:
    """Forward one request and normalize its enqueue receipt without policy."""

    if sink is None:
        return _receipt(request, "error", {"reason": "tts_unavailable"})
    try:
        # The authored payload stays opaque.  Delivery adds only its reserved
        # identity envelope so downstream playback/presentation can attribute
        # the utterance without guessing from provider- or scene-specific
        # payload fields.
        result = sink(
            {
                **dict(request.payload),
                "_narration_delivery": {
                    "source_kind": request.source_kind,
                    "source_id": request.source_id,
                    "session_id": request.session_id,
                    "request_id": request.request_id,
                },
            }
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        logger.exception(
            "narration sink failed source=%s source_id=%s request_id=%s",
            request.source_kind,
            request.source_id,
            request.request_id,
        )
        return _receipt(
            request,
            "error",
            {"reason": f"sink_failed:{exc.__class__.__name__}"},
        )

    if not isinstance(result, Mapping):
        return _receipt(request, "queued_legacy_sink")
    raw_status = str(result.get("status") or "").strip().lower()
    status = raw_status if raw_status in _SINK_STATUSES else "unknown"
    diagnostics = {
        key: _bounded_value(result[key])
        for key in _DIAGNOSTIC_KEYS
        if key in result
    }
    if status == "unknown" and raw_status:
        diagnostics["sink_status"] = _bounded_value(raw_status)
    return _receipt(request, status, diagnostics)


def _receipt(
    request: NarrationRequest,
    status: str,
    diagnostics: Mapping[str, Any] | None = None,
) -> NarrationDeliveryReceipt:
    return NarrationDeliveryReceipt(
        request_id=request.request_id,
        source_kind=request.source_kind,
        source_id=request.source_id,
        status=status,
        diagnostics=dict(diagnostics or {}),
    )


def _bounded_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:240]
