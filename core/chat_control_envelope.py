"""Transport-owned parsing for inline chat control envelopes.

This module stops at the protocol boundary.  It separates visible text,
delegate proposals, action-existence outcomes, and expression actions without
grounding an identity or dispatching work.  Those host-owned decisions remain
with the chat runtime's control handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm.action_existence_protocol import decode_control_envelope
from llm.stream_parser import StreamTagParser


@dataclass(frozen=True)
class InlineControlChunk:
    """One parser commit point from the role model's streaming transport."""

    cleaned_text: str
    delegate_actions: tuple[dict[str, Any], ...]
    auip_actions: tuple[dict[str, Any], ...]
    expression_actions: tuple[dict[str, Any], ...]
    control_seen: bool = False
    control_valid: bool = False
    control_error: str = ""
    explicit_no_control: bool = False
    history_control_text: str = ""
    # text / action (presentation only) / control, in original stream order.
    ordered_parts: tuple[tuple[str, Any], ...] = ()
    had_actions: bool = False


def parse_inline_control_chunk(
    parser: StreamTagParser,
    raw_content: str,
) -> InlineControlChunk:
    """Parse a stream fragment without applying any host-side authority."""

    cleaned, actions, ordered_parts = parser.process_chunk_parts(raw_content)
    if not actions:
        return InlineControlChunk(
            cleaned_text=cleaned,
            delegate_actions=(),
            auip_actions=(),
            expression_actions=(),
            ordered_parts=tuple(ordered_parts),
        )

    delegates, controls, auip_actions, expressions = [], [], [], []
    classified_parts = []
    for kind, value in ordered_parts:
        if kind == "text":
            classified_parts.append((kind, value))
            continue
        action_type = value.get("type")
        if action_type == "DELEGATE":
            delegates.append(value)
        elif action_type == "CONTROL":
            controls.append(value)
        elif action_type == "AUIP":
            auip_actions.append(value)
        else:
            expressions.append(value)
            classified_parts.append(("action", value))
            continue
        classified_parts.append(("control", value))
    control_seen = False
    control_valid = False
    control_error = ""
    explicit_no_control = False
    history_control_text = ""
    if controls:
        decoded = decode_control_envelope(controls[0])
        control_seen = decoded.seen
        control_valid = decoded.valid
        control_error = str(decoded.error or "")
        if decoded.valid and decoded.delegate and decoded.action is not None:
            delegates.append(dict(decoded.action))
        elif decoded.valid:
            explicit_no_control = True
            history_control_text = str(controls[0].get("raw") or "")

    return InlineControlChunk(
        cleaned_text=cleaned,
        delegate_actions=tuple(delegates),
        auip_actions=tuple(auip_actions),
        expression_actions=tuple(expressions),
        control_seen=control_seen,
        control_valid=control_valid,
        control_error=control_error,
        explicit_no_control=explicit_no_control,
        history_control_text=history_control_text,
        ordered_parts=tuple(classified_parts),
        had_actions=True,
    )
