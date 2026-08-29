"""Shared, product-inert helpers for independent control-plane probes.

The role reply from the current turn is deliberately absent from ``messages``.
Only the production contract, prior conversation, and current user utterance
are eligible evidence for the shadow decision.
"""

from __future__ import annotations

from typing import Any

from server.control_decision import CONTROL_FIELDS, PAYLOAD_FIELDS


def adjudication_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Clone a production-shaped history and add the shadow-only instruction."""

    cloned = [dict(message) for message in messages]
    if not cloned or str(cloned[0].get("role") or "") != "system":
        raise ValueError("control adjudication requires a leading system message")
    if not any(str(item.get("role") or "") == "user" for item in cloned[1:]):
        raise ValueError("control adjudication requires a current user message")
    return cloned


def delegate_attrs(reply: str) -> list[dict[str, Any]]:
    """Parse every DELEGATE control from one completed model response.

    The shipping streaming role parser intentionally stops at its first
    DELEGATE. A future turn-final control response has different framing: it is
    complete, non-spoken, and may contain genuinely independent actions. Use
    the existing whole-text parser that already preserves their source order.
    """

    from tools.text_utils import parse_tags_and_clean

    _cleaned, actions = parse_tags_and_clean(str(reply or ""))
    return [
        dict(action.get("attrs") or {})
        for action in actions
        if action.get("type") == "DELEGATE"
    ]


def normalize_control_actions(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold an equivalent split focus modifier into its adjacent action.

    This is deliberately structural. It does not infer intent from language,
    inspect provider ids, or rewrite execution payloads. Two adjacent actions
    are equivalent to the contract's compound form only when the first is a
    taskless focus, both name the same provider, and their declared destination
    fields do not conflict.
    """

    normalized = [dict(action) for action in actions]
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(normalized):
        current = normalized[index]
        current_intent = str(current.get("intent") or "").strip().lower()
        if (
            current_intent == "focus"
            and not str(current.get("task") or "").strip()
            and index + 1 < len(normalized)
        ):
            following = dict(normalized[index + 1])
            same_provider = str(current.get("provider") or "").strip() == str(
                following.get("provider") or ""
            ).strip()
            following_intent = str(following.get("intent") or "").strip().lower()
            project_id = str(current.get("project_id") or "").strip()
            expected_focus = "set" if project_id else "clear"
            following_focus = str(following.get("focus") or "").strip().lower()
            following_project = str(following.get("project_id") or "").strip()
            compatible_destination = (
                following_focus in {"", expected_focus}
                and (not project_id or not following_project or following_project == project_id)
            )
            if (
                same_provider
                and following_intent not in {"", "focus"}
                and compatible_destination
            ):
                following["focus"] = expected_focus
                if project_id and not following_project:
                    following["project_id"] = project_id
                result.append(following)
                index += 2
                continue
        result.append(current)
        index += 1
    return result


def project_control_actions(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove execution payload from an independent control decision.

    ``task``, ``url``, ``query`` and ``text`` belong to the role proposal and
    existing host validation. Letting the independent decision author a second
    copy would create two competing natural-language sources of truth.
    """

    return [
        {key: value for key, value in action.items() if key in CONTROL_FIELDS}
        for action in actions
    ]


def filter_known_fact_controls(
    actions: list[dict[str, Any]],
    *,
    provider_ids: set[str] | frozenset[str],
    project_ids: set[str] | frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply only registry facts that the host can prove deterministically."""

    accepted: list[dict[str, Any]] = []
    rejections: list[str] = []
    known_providers = {str(value or "").strip() for value in provider_ids}
    known_projects = {str(value or "").strip() for value in project_ids}
    for index, action in enumerate(actions):
        provider = str(action.get("provider") or "").strip()
        project_id = str(action.get("project_id") or "").strip()
        if not provider or provider not in known_providers:
            rejections.append(f"action {index + 1}: provider is not registered")
            continue
        if project_id and project_id not in known_projects:
            rejections.append(f"action {index + 1}: project_id is not registered")
            continue
        accepted.append(dict(action))
    return accepted, rejections


def merge_proposal_controls(
    proposals: list[dict[str, Any]],
    controls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Gate canonical controls on role proposals and preserve role payload.

    Canonical decisions may suppress or correct an existing proposal, but they
    never create a new action when the role emitted none. Pairing is by source
    order and bounded by the shorter side; count mismatches are reported so a
    caller can surface or retain the existing omission path instead of guessing.
    """

    if not proposals:
        return [], (["canonical action ignored because no role proposal exists"] if controls else [])
    if not controls:
        return [], ["role proposal suppressed because canonical control is NONE"]

    notes: list[str] = []
    if len(proposals) != len(controls):
        notes.append(
            f"action count mismatch: proposals={len(proposals)} controls={len(controls)}"
        )
    merged: list[dict[str, Any]] = []
    for proposal, control in zip(proposals, controls):
        action = dict(control)
        intent = str(action.get("intent") or "").strip().lower()
        # A canonical focus is a taskless control. Retaining an accidental role
        # task would trigger the handler's legacy compound-focus degradation.
        if intent != "focus":
            for key in PAYLOAD_FIELDS:
                value = proposal.get(key)
                if value not in (None, ""):
                    action[key] = value
        merged.append(action)
    return merged, notes
