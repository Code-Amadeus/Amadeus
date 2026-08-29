"""Reversible dispatch policy for a proposal-gated ControlDecision.

The language model produces a control decision and the host validates entity
identity separately.  This module owns only the final rollout boundary: which
already-complete proposal controls may reach the existing dispatcher when the
decision is authoritative, and what happens when that decision is unavailable.

It deliberately knows nothing about Provider implementations, UI rendering,
or conversation language.  Callers remain responsible for wrapping returned
attribute mappings as their transport's DELEGATE action.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Literal, Mapping, Sequence


ControlAuthorityDisposition = Literal[
    "accepted",
    "corrected",
    "suppressed",
    "audit_unavailable",
    "failed_closed",
]


@dataclass(frozen=True, slots=True)
class ControlAuthorityResolution:
    """One auditable, payload-preserving dispatch choice."""

    disposition: ControlAuthorityDisposition
    actions: tuple[Mapping[str, Any], ...] = ()
    decision_status: str = ""
    decision_outcome: str = ""
    reason: str = ""
    notes: tuple[str, ...] = ()

    @property
    def should_announce_block(self) -> bool:
        # Suppression is a successful control decision: the user's turn did
        # not authorize the proposed action.  It is audit evidence, not a
        # failed WorkItem and therefore must not manufacture a spoken work
        # report.  Only an invalid/incomplete authority decision needs a
        # visible correction because execution was promised but could not be
        # verified safely.
        return self.disposition == "failed_closed"


@dataclass(frozen=True, slots=True)
class ControlAuthorityCapture:
    """A synchronously frozen host context plus its deferred model job."""

    result: Any = None
    error_reason: str = ""


def capture_control_authority(
    adjudicator,
    batch,
    *,
    capture_method: str = "capture",
) -> ControlAuthorityCapture:
    """Freeze host facts at tag closure, before any asynchronous dispatch."""

    capture = getattr(adjudicator, str(capture_method or "capture"), None)
    if not callable(capture):
        return ControlAuthorityCapture(
            error_reason=(
                "authority adjudicator has no "
                f"{str(capture_method or 'capture')} boundary"
            )
        )
    try:
        return ControlAuthorityCapture(result=capture(batch))
    except Exception as exc:
        return ControlAuthorityCapture(
            error_reason=f"context capture failed: {type(exc).__name__}"
        )


def _has_persistent_focus_side_effect(control: Mapping[str, Any]) -> bool:
    """Return whether fallback would mutate the Session's durable binding."""

    intent = str(control.get("intent") or "").strip().lower()
    focus = str(control.get("focus") or "").strip().lower()
    return intent == "focus" or focus in {"set", "clear"}


def resolve_control_authority(
    *,
    decision_status: str,
    decision_outcome: str,
    canonical_actions: Sequence[Mapping[str, Any]] = (),
    fallback_actions: Sequence[Mapping[str, Any]] = (),
    reason: str = "",
    notes: Sequence[str] = (),
    allow_unavailable_fallback: bool = True,
) -> ControlAuthorityResolution:
    """Choose canonical, fallback, or fail-closed controls.

    ``fallback_actions`` are the role proposal after the pre-existing host
    annotations and structural grounding.  They are used only when the control
    backend was unreachable.  Invalid or incomplete decisions cannot safely
    claim that a partial candidate traversal was exhaustive and therefore fail
    closed.  A persistent focus side effect also fails closed while the legacy
    focus guard remains part of the canary's escape path.
    """

    status = str(decision_status or "").strip().lower()
    outcome = str(decision_outcome or "").strip().lower()
    frozen_notes = tuple(
        str(note or "").strip()
        for note in notes
        if str(note or "").strip()
    )

    if status == "ok":
        canonical = tuple(dict(action) for action in canonical_actions)
        if not canonical:
            return ControlAuthorityResolution(
                disposition="suppressed",
                decision_status=status,
                decision_outcome=outcome,
                reason=str(reason or "decision returned no accepted proposal"),
                notes=frozen_notes,
            )
        disposition: ControlAuthorityDisposition = (
            "corrected" if outcome == "diverge" else "accepted"
        )
        return ControlAuthorityResolution(
            disposition=disposition,
            actions=canonical,
            decision_status=status,
            decision_outcome=outcome,
            reason=str(reason or ""),
            notes=frozen_notes,
        )

    fallback = tuple(dict(action) for action in fallback_actions)
    if bool(allow_unavailable_fallback) and status == "unavailable" and fallback and not any(
        _has_persistent_focus_side_effect(action) for action in fallback
    ):
        return ControlAuthorityResolution(
            disposition="audit_unavailable",
            actions=fallback,
            decision_status=status,
            decision_outcome=outcome,
            reason=str(reason or "control decision unavailable"),
            notes=frozen_notes,
        )

    return ControlAuthorityResolution(
        disposition="failed_closed",
        decision_status=status or "unavailable",
        decision_outcome=outcome,
        reason=str(reason or "control decision did not produce a complete result"),
        notes=frozen_notes,
    )


async def adjudicate_control_authority(
    capture: ControlAuthorityCapture,
    *,
    fallback_actions: Sequence[Mapping[str, Any]] = (),
    timeout_s: float,
    allow_unavailable_fallback: bool = True,
) -> ControlAuthorityResolution:
    """Run one bounded resolver call and apply the rollout failure policy.

    Runtime transport code should not need to understand evidence status or
    distinguish capture, query, and deadline failures. They are all normalized
    here; only a model/transport ``unavailable`` result may use the established
    non-focus fallback.
    """

    if capture.error_reason:
        return resolve_control_authority(
            decision_status="unavailable",
            decision_outcome="unavailable",
            fallback_actions=fallback_actions,
            reason=capture.error_reason,
            allow_unavailable_fallback=allow_unavailable_fallback,
        )
    result = capture.result

    try:
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=max(0.001, float(timeout_s)))
    except asyncio.TimeoutError:
        return resolve_control_authority(
            decision_status="unavailable",
            decision_outcome="unavailable",
            fallback_actions=fallback_actions,
            reason=f"authority deadline exceeded ({float(timeout_s):.1f}s)",
            allow_unavailable_fallback=allow_unavailable_fallback,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return resolve_control_authority(
            decision_status="unavailable",
            decision_outcome="unavailable",
            fallback_actions=fallback_actions,
            reason=f"authority query failed: {type(exc).__name__}",
            allow_unavailable_fallback=allow_unavailable_fallback,
        )

    if result is None:
        return resolve_control_authority(
            decision_status="unavailable",
            decision_outcome="unavailable",
            fallback_actions=fallback_actions,
            reason="authority returned no evidence",
            allow_unavailable_fallback=allow_unavailable_fallback,
        )
    return resolve_control_authority(
        decision_status=str(getattr(result, "decision_status", "") or ""),
        decision_outcome=str(getattr(result, "outcome", "") or ""),
        canonical_actions=getattr(result, "canonical_actions", ()) or (),
        fallback_actions=fallback_actions,
        reason=str(getattr(result, "reason", "") or ""),
        notes=getattr(result, "notes", ()) or (),
        allow_unavailable_fallback=allow_unavailable_fallback,
    )
