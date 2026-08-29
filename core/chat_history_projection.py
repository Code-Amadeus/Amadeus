"""Conversation-history projection for one completed chat turn.

The role stream may finish after the user switches Session or after a pending
turn is discarded.  This module owns those projection guards and the optional
Browser branch stamp; it does not own model streaming or work dispatch.
"""

from __future__ import annotations

import asyncio
import logging

from config.settings import (
    EMO_HISTORY_POLICIES,
    EMO_HISTORY_POLICY,
    PENDING_TURN_GATE_TIMEOUT_S,
)
from core.session_manager import conversation_history, get_current_session_id

logger = logging.getLogger("chat_history_projection")


def project_inline_role_history(
    ordered_parts: tuple[tuple[str, object], ...] | list[tuple[str, object]],
    *,
    policy: str | None = None,
) -> str:
    """Project visible text plus policy-selected EMO into model history.

    The parser supplies exact stream order. Execution controls remain excluded
    here because their effective, Host-adjudicated form is appended separately.
    GUI and TTS never consume this projection.
    """

    selected = str(policy or EMO_HISTORY_POLICY).strip().lower()
    if selected not in EMO_HISTORY_POLICIES:
        raise ValueError(f"unsupported EMO history policy: {selected!r}")
    projected: list[str] = []
    for kind, value in ordered_parts:
        if kind == "text":
            projected.append(str(value or ""))
            continue
        if kind != "action" or not isinstance(value, dict):
            continue
        if str(value.get("type") or "").strip().upper() != "EMO":
            continue
        if selected == "strip":
            continue
        attrs = value.get("attrs") if isinstance(value.get("attrs"), dict) else {}
        preset = str(attrs.get("preset") or "").strip().casefold()
        if selected == "expressive_only" and (not preset or preset == "normal"):
            continue
        projected.append(str(value.get("raw") or ""))
    return "".join(projected)


async def turn_allows_history(turn_id: str) -> bool:
    """Return whether the turn coordinator permits durable chat projection."""

    if not turn_id:
        return True
    try:
        from core.turn_coordinator import get_turn_coordinator

        coordinator = get_turn_coordinator()
        gate = coordinator.turn_gate(turn_id)
        if gate == "wait":
            gate = await asyncio.to_thread(
                coordinator.wait_turn_decided,
                turn_id,
                PENDING_TURN_GATE_TIMEOUT_S,
            )
        return gate != "drop"
    except Exception:
        return True


def stamp_active_branch_entries(count: int = 2) -> None:
    """Mark the just-projected entries as belonging to the active branch."""

    try:
        from server.interaction_branch import get_interaction_branch_coordinator

        coordinator = get_interaction_branch_coordinator()
        if coordinator is None:
            return
        branch = coordinator.active_branch_for_session(get_current_session_id() or "")
        if branch is None:
            return
        for entry in conversation_history.dialog[-max(1, count) :]:
            if isinstance(entry, dict):
                entry["branch_id"] = branch.branch_id
    except Exception:
        logger.debug("branch entry stamping failed", exc_info=True)


async def project_completed_turn(
    *,
    session_id: str,
    question: str,
    history_response: str,
    visible_response: str,
    turn_id: str,
    branch_continue_seen: bool,
) -> bool:
    """Project one accepted turn into its originating Session history."""

    current_session_id = get_current_session_id()
    if current_session_id != session_id:
        logger.warning(
            "[scope guard] session switched (turn=%r, current=%r); "
            "skipping history write.",
            session_id,
            current_session_id,
        )
        return False
    if not await turn_allows_history(turn_id):
        logger.info(
            "[pending guard] turn %s discarded; skipping history write.",
            turn_id,
        )
        return False

    conversation_history.add_user(question)
    conversation_history.add_assistant(
        history_response or visible_response,
        turn_id=turn_id,
    )
    if branch_continue_seen:
        stamp_active_branch_entries(2)
    return True
