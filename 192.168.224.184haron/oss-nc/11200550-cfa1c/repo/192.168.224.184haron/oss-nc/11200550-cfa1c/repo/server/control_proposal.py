"""Immutable handoff at the transport's complete-control boundary.

The inline DELEGATE transport has a useful property: a closing ``]`` proves
that its one structured proposal is complete while the role-model stream is
still open.  Native tool-call arguments, by contrast, are only complete when
their response stream finishes.  This module names that difference without
making either transport, an LLM client, or a Provider authoritative.

The snapshot is deliberately read-only.  A shadow observer may measure or
adjudicate it, but cannot mutate the action that the existing dispatcher sees.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import time
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence


ControlProposalTransport = Literal["inline_tag", "native_tool_call"]
ControlProposalCommitPoint = Literal["delegate_tag_closed", "response_stream_finished"]

_PAYLOAD_FIELDS = ("task", "url", "query", "text")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class ControlProposalBatch:
    """A complete set of proposals at one transport-defined commit point.

    ``proposals`` contains the role model's original attrs for comparison and
    audit, before host grounding can correct them. ``prior_messages`` is frozen
    at turn start, so a background shadow cannot accidentally read the current
    role reply after the turn is persisted.

    Decision callers must use :meth:`decision_payloads`, which exposes only the
    natural-language/provider payload used to align result indexes and never
    exposes the role model's proposed control fields.
    """

    turn_id: str
    session_id: str
    user_text: str
    transport: ControlProposalTransport
    commit_point: ControlProposalCommitPoint
    proposals: tuple[Mapping[str, Any], ...]
    prior_messages: tuple[Mapping[str, str], ...]
    sealed_at_monotonic: float

    def decision_payloads(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                key: proposal[key]
                for key in _PAYLOAD_FIELDS
                if proposal.get(key) not in (None, "")
            }
            for proposal in self.proposals
        )


def seal_control_proposals(
    actions: Sequence[Mapping[str, Any]],
    *,
    turn_id: str,
    session_id: str,
    user_text: str,
    transport: ControlProposalTransport,
    prior_messages: Iterable[Mapping[str, Any]] = (),
) -> ControlProposalBatch:
    """Snapshot complete DELEGATE attrs without changing dispatch authority."""

    proposals = tuple(
        _freeze(action.get("attrs") if isinstance(action.get("attrs"), Mapping) else {})
        for action in actions
        if str(action.get("type") or "").upper() == "DELEGATE"
    )
    if not proposals:
        raise ValueError("a control proposal batch requires at least one DELEGATE")
    if transport == "inline_tag":
        # StreamTagParser intentionally accepts only the first DELEGATE in one
        # role turn.  A focus modifier plus its operation therefore belongs in
        # that single proposal; pretending this path is a multi-tag batch would
        # reintroduce the focus/execute race under a different abstraction.
        if len(proposals) != 1:
            raise ValueError("the inline transport commits exactly one DELEGATE")
        commit_point: ControlProposalCommitPoint = "delegate_tag_closed"
    elif transport == "native_tool_call":
        commit_point = "response_stream_finished"
    else:
        raise ValueError(f"unknown control proposal transport: {transport}")
    frozen_history = tuple(
        _freeze(
            {
                "role": str(message.get("role") or ""),
                "content": str(message.get("content") or ""),
            }
        )
        for message in prior_messages
        if str(message.get("role") or "") in {"user", "assistant"}
        and str(message.get("content") or "")
    )
    return ControlProposalBatch(
        turn_id=str(turn_id or ""),
        session_id=str(session_id or ""),
        user_text=str(user_text or ""),
        transport=transport,
        commit_point=commit_point,
        proposals=proposals,
        prior_messages=frozen_history,
        sealed_at_monotonic=time.monotonic(),
    )
