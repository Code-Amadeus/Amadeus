"""Host-owned facts for the structured AUIP presentation lane.

This module is deliberately model- and application-neutral.  It converts one
Host-accepted AUIP observation into bounded, identified facts.  Presentation
models may select and phrase those facts; they cannot create event authority,
actor identity, receipts, actions, or delivery history.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Mapping, Sequence

from server.auip_contract import AuipProtocolError


PRESENTATION_ACTIONS = frozenset({"silent", "surface", "speak"})
PRESENTATION_REASON_CODES = frozenset(
    {"novel", "tactical", "consequence", "terminal", "repetitive", "mechanical"}
)

_DROP_DETAIL_KEYS = frozenset(
    {
        "board",
        "rows",
        "final_board",
        # Proactive role presentation should explain semantic meaning, not
        # recite low-level move telemetry. Exact coordinates remain available
        # to the action/receipt and read-only query lanes.
        "x",
        "y",
        "movecount",
        "moves",
        "turn",
        "nextplayer",
        "nextturn",
        "privatetelemetry",
        "hiddentelemetry",
        "attachticket",
    }
)
_PRIORITY_KEYS = (
    "winner",
    "winnerSide",
    "winner_owner",
    "roleBindings",
    "binding",
    "accepted",
    "performed",
    "user_at_fault",
    "side",
    "mark",
    "position",
    "placed",
    "effect",
    "effects",
    "action",
    "action_type",
    "resulting_revision",
    "following_event",
    "subject_owner",
    "score",
    "moveCount",
    "lastMove",
    "phase",
    "turn",
    "heat",
    "safeInterval",
    "safeMaximum",
    "trend",
)


@dataclass(frozen=True, slots=True)
class AuipPresentationDecision:
    """One validated presentation result before source-neutral delivery."""

    action: str
    selected_fact_ids: tuple[str, ...] = ()
    display_text: str = ""
    emotion: str = "thinking"
    reason_code: str = ""
    valid: bool = True
    error: str = ""


def compile_auip_host_facts(observation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Compile one accepted event and correlated receipt into structured facts."""

    event = _mapping(observation.get("event"))
    event_id = _clean(event.get("event_id"), 160)
    if not event_id:
        raise AuipProtocolError("missing_value", "event_id")
    receipt = _mapping(observation.get("latest_verified_self_action"))
    correlated_receipt = receipt if _receipt_follows_event(event, receipt) else {}
    reported_actor = _clean(event.get("actor"), 40) or "app"
    verified_actor = _verified_actor(reported_actor, correlated_receipt)
    terminal = event.get("terminal") is True
    controller_effect = event.get("controller_effect") is True
    authority = (
        "accepted_terminal_event"
        if terminal
        else "accepted_controller_lease_and_event"
        if controller_effect
        else "accepted_event"
    )
    details, omitted = _project_value(
        {
            "event_type": _clean(event.get("type"), 160),
            "payload": _mapping(event.get("payload")),
            "state": _mapping(observation.get("state")),
            **(
                {"controller_lease": _mapping(event.get("controller_lease"))}
                if controller_effect
                else {}
            ),
        },
        path="event",
    )
    event_fact: dict[str, Any] = {
        "fact_id": f"event:{event_id}",
        "authority": authority,
        "kind": _clean(event.get("type"), 160) or "accepted_event",
        "revision": _integer_or_none(event.get("revision")),
        "importance": _clean(event.get("importance"), 40) or "normal",
        "terminal": terminal,
        "actor": {
            "reported": reported_actor,
            "verified": verified_actor,
        },
        "subject_owners": _subject_owners(details),
        "details": details,
        "omitted_fields": sorted(set(omitted)),
    }
    facts: list[dict[str, Any]] = []
    if correlated_receipt:
        receipt_details, receipt_omitted = _project_value(
            {
                "action_type": correlated_receipt.get("type"),
                "payload": _mapping(correlated_receipt.get("payload")),
                "effects": _mapping(correlated_receipt.get("effects")),
                "accepted": True,
                "resulting_revision": correlated_receipt.get("resulting_revision"),
                "following_event": event.get("type"),
            },
            path="receipt",
        )
        action_id = _clean(correlated_receipt.get("action_id"), 160)
        facts.append(
            {
                "fact_id": f"receipt:{action_id or event_id}",
                "authority": "accepted_action_receipt",
                "kind": "accepted_self_action_result",
                "revision": _integer_or_none(
                    correlated_receipt.get("resulting_revision")
                ),
                "importance": "normal",
                "terminal": False,
                "actor": {"reported": "kurisu", "verified": "kurisu"},
                "subject_owners": ["kurisu"],
                "details": receipt_details,
                "omitted_fields": sorted(set(receipt_omitted)),
            }
        )
    facts.append(event_fact)
    _attach_declared_outcome(facts)
    return _bound_fact_envelope(facts)


def compile_auip_decision_context(observation: Mapping[str, Any]) -> dict[str, str]:
    """Return receipt-bound role intent without promoting it to scene fact.

    The Participant already chose the accepted action from the Host-locked
    state.  Its reason is useful for first-person interpretation, but it is not
    application authority and must never be used to invent objective state.
    """

    event = _mapping(observation.get("event"))
    receipt = _mapping(observation.get("latest_verified_self_action"))
    if not _receipt_follows_event(event, receipt):
        return {}
    raw = _mapping(receipt.get("decision_context"))
    reason = _clean(raw.get("reason"), 600)
    if not reason:
        return {}
    return {
        "status": "accepted_action_bound",
        "kind": _clean(raw.get("kind"), 80) or "role_choice",
        "reason": reason,
        "instruction_relation": (
            _clean(raw.get("instruction_relation"), 40) or "not_applicable"
        ),
    }


def semantic_commentary_facts(
    facts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Expose receipt proof, not raw action telemetry, for cadence commentary.

    Application-declared important events retain their full fact envelope. A
    periodic role comment instead explains the receipt-bound decision context;
    it needs only proof that the selected assistant action was accepted.
    """

    receipt = next(
        (
            item
            for item in facts
            if _clean(item.get("authority"), 80) == "accepted_action_receipt"
            and _clean(item.get("kind"), 80) == "accepted_self_action_result"
        ),
        None,
    )
    if receipt is None:
        raise AuipProtocolError("semantic_commentary_receipt_missing")
    omitted = {
        *(
            _clean(item, 240)
            for item in receipt.get("omitted_fields") or []
            if _clean(item, 240)
        ),
        "receipt.action_type",
        "receipt.effects",
        "receipt.following_event",
        "receipt.payload",
        "receipt.resulting_revision",
    }
    return _bound_fact_envelope(
        [
            {
                "fact_id": _clean(receipt.get("fact_id"), 200),
                "authority": "accepted_action_receipt",
                "kind": "accepted_self_action_result",
                "importance": _clean(receipt.get("importance"), 40) or "normal",
                "terminal": False,
                "actor": copy.deepcopy(_mapping(receipt.get("actor"))),
                "subject_owners": copy.deepcopy(
                    receipt.get("subject_owners")
                    if isinstance(receipt.get("subject_owners"), list)
                    else []
                ),
                "details": {"accepted": True},
                "omitted_fields": sorted(omitted),
            }
        ]
    )


def compile_auip_operator_fact(
    *,
    app_session_id: str,
    outcome_id: str,
    revision: int | None,
    reason: str,
) -> list[dict[str, Any]]:
    """Compile one Host-owned blocked Participant outcome."""

    clean_reason = _clean(reason, 600)
    if not clean_reason:
        raise AuipProtocolError("missing_value", "operator_outcome.reason")
    identifier = _clean(outcome_id, 160) or f"revision-{revision or 0}"
    facts = [
        {
            "fact_id": f"operator:{identifier}",
            "authority": "host_operator_outcome",
            "kind": "blocked_self_request",
            "revision": _integer_or_none(revision),
            "importance": "blocking",
            "terminal": False,
            "actor": {"reported": "kurisu", "verified": "kurisu"},
            "subject_owners": ["kurisu"],
            "outcome": {
                "accepted": False,
                "performed": False,
                "user_at_fault": False,
            },
            "details": {
                "app_session_id": _clean(app_session_id, 160),
                "bounded_reason": clean_reason,
            },
            "omitted_fields": [],
        }
    ]
    return _bound_fact_envelope(facts)


def bounded_user_context(
    messages: Sequence[Mapping[str, Any]] | Any,
    *,
    topic_wrapper: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Expose only the latest user-provenance topic to presentation."""

    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return {}
    for item in reversed(messages):
        if not isinstance(item, Mapping):
            continue
        if _clean(item.get("role"), 24).lower() != "user":
            continue
        text = _clean(item.get("content"), 320)
        if text:
            guarded = topic_wrapper(text) if topic_wrapper is not None else text
            return {
                "source_role": "user",
                "latest_user_topic": _clean(guarded, 760),
            }
    return {}


def build_structured_presentation_payload(
    *,
    facts: Sequence[Mapping[str, Any]],
    app: Mapping[str, Any] | None,
    recent_messages: Sequence[Mapping[str, Any]] | Any,
    recent_delivered_narrations: Sequence[Mapping[str, Any]] | Any,
    profile_id: str,
    display_language: str,
    presentation_required: bool,
    host_reason_code: str,
    decision_context: Mapping[str, Any] | None = None,
    user_topic_wrapper: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build the only payload visible to the structured presenter."""

    clean_facts = _bound_fact_envelope([copy.deepcopy(dict(item)) for item in facts])
    recent = []
    if isinstance(recent_delivered_narrations, Sequence) and not isinstance(
        recent_delivered_narrations, (str, bytes)
    ):
        for item in recent_delivered_narrations[-4:]:
            if not isinstance(item, Mapping):
                continue
            text = _clean(item.get("text"), 320)
            if text:
                recent.append({"text": text, "terminal": bool(item.get("terminal"))})
    source_messages = (
        recent_messages
        if isinstance(recent_messages, Sequence)
        and not isinstance(recent_messages, (str, bytes))
        else []
    )
    user_context = bounded_user_context(
        source_messages,
        topic_wrapper=user_topic_wrapper,
    )
    return {
        "profile_id": _clean(profile_id, 80) or "game",
        "display_language": _clean(display_language, 80) or "japanese",
        "facts": clean_facts,
        "app": {
            key: copy.deepcopy((app or {}).get(key))
            for key in ("id", "title", "version", "objective", "interactionSummary")
            if (app or {}).get(key) not in (None, "")
        },
        "conversation_context": user_context,
        "omitted_non_user_conversation": any(
            isinstance(item, Mapping)
            and _clean(item.get("role"), 24).lower() == "assistant"
            for item in source_messages
        ),
        "recent_delivered_narrations": recent,
        "decision_context": {
            key: _clean(value, 600 if key == "reason" else 80)
            for key, value in _mapping(decision_context).items()
            if key in {"status", "kind", "reason", "instruction_relation"}
            and _clean(value, 600 if key == "reason" else 80)
        },
        "presentation_required": bool(presentation_required),
        "host_reason_code": _clean(host_reason_code, 40),
    }


def parse_structured_presentation_decision(
    value: Any,
    *,
    facts: Sequence[Mapping[str, Any]],
    presentation_required: bool,
    max_spoken_chars: int,
) -> AuipPresentationDecision:
    """Validate fact selection and prose without granting model authority."""

    if not isinstance(value, Mapping):
        return _invalid("missing_structured_presentation")
    action = _clean(value.get("action"), 24).lower()
    if action not in PRESENTATION_ACTIONS:
        return _invalid("invalid_presentation_action")
    raw_ids = value.get("selected_fact_ids")
    if not isinstance(raw_ids, list):
        return _invalid("invalid_selected_fact_ids")
    selected = tuple(dict.fromkeys(_clean(item, 200) for item in raw_ids if _clean(item, 200)))
    known = {
        _clean(item.get("fact_id"), 200)
        for item in facts
        if isinstance(item, Mapping) and _clean(item.get("fact_id"), 200)
    }
    if not set(selected).issubset(known):
        return _invalid("unknown_selected_fact_id")
    if presentation_required:
        if action != "speak":
            return _invalid("mandatory_presentation_suppressed")
        # Mandatory facts are promoted by the Host. Model-selected IDs remain
        # diagnostic and cannot narrow terminal/blocked/Controller truth.
        selected = tuple(sorted(known))
    elif action == "speak" and not selected:
        return _invalid("speak_without_selected_fact")
    display_text = " ".join(str(value.get("display_text") or "").split())
    if action == "speak" and not display_text:
        return _invalid("empty_structured_presentation")
    if action != "speak" and display_text:
        return _invalid("non_speak_with_display_text")
    if len(display_text) > max(24, int(max_spoken_chars)):
        return _invalid("structured_presentation_too_long")
    reason_code = _clean(value.get("reason_code"), 40).lower()
    if reason_code not in PRESENTATION_REASON_CODES:
        return _invalid("invalid_presentation_reason_code")
    return AuipPresentationDecision(
        action=action,
        selected_fact_ids=selected,
        display_text=display_text,
        emotion=_clean(value.get("emotion"), 40) or "thinking",
        reason_code=reason_code,
    )


def _invalid(error: str) -> AuipPresentationDecision:
    return AuipPresentationDecision(action="silent", valid=False, error=error)


def _receipt_follows_event(event: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    if not receipt or receipt.get("accepted") is not True:
        return False
    try:
        same_revision = int(event.get("revision")) == int(receipt.get("resulting_revision"))
    except (TypeError, ValueError):
        return False
    if not same_revision:
        return False
    caused_by = _clean(event.get("caused_by_action_id"), 160)
    action_id = _clean(receipt.get("action_id"), 160)
    if caused_by:
        return bool(action_id and caused_by == action_id)
    return _clean(event.get("actor"), 40).lower() == "kurisu"


def _verified_actor(reported: str, receipt: Mapping[str, Any]) -> str:
    clean = _clean(reported, 40).lower()
    if clean == "kurisu":
        return "kurisu" if receipt else "unknown"
    if clean in {"app", "system"}:
        return "application"
    if clean == "user":
        return "user"
    return "unknown"


def _attach_declared_outcome(facts: list[dict[str, Any]]) -> None:
    """Resolve explicitly declared winner ownership for round or session results."""

    result_fact = next(
        (item for item in facts if item.get("terminal") is True),
        None,
    )
    if result_fact is None:
        result_fact = next(
            (
                item
                for item in facts
                if any(
                    _first_nested(_mapping(item.get("details")), key) is not None
                    for key in ("winner", "winnerSide")
                )
            ),
            None,
        )
    if result_fact is None:
        return
    details = _mapping(result_fact.get("details"))
    winner = _first_nested(details, "winner")
    if winner is None:
        winner = _first_nested(details, "winnerSide")
    if winner is None:
        if result_fact.get("terminal") is not True:
            return
        result_fact["outcome"] = {
            "winner_side": "unknown",
            "winner_owner": "unknown",
            "loser_owner": "unknown",
            "method": "unknown",
        }
        return
    owner = _owner_for_winner(facts, winner)
    method = _first_text(details, ("reason", "method"))
    if not method:
        lines = _nested_values(details, "winning_line")
        if any(isinstance(item, list) and item for item in lines):
            method = "declared_winning_line"
    result_fact["outcome"] = {
        "winner_side": _clean(winner, 120) or "unknown",
        "winner_owner": owner,
        "loser_owner": (
            "user" if owner == "kurisu" else "kurisu" if owner == "user" else "unknown"
        ),
        "method": method or "unknown",
    }


def _owner_for_winner(facts: Sequence[Mapping[str, Any]], winner: Any) -> str:
    clean = _clean(winner, 120).lower()
    if clean in {"kurisu", "assistant", "participant"}:
        return "kurisu"
    if clean == "user":
        return "user"
    for fact in facts:
        for bindings in (
            *_nested_values(fact, "roleBindings"),
            *_nested_values(fact, "binding"),
        ):
            if not isinstance(bindings, Mapping):
                continue
            if _clean(bindings.get("participant") or bindings.get("kurisu"), 120).lower() == clean:
                return "kurisu"
            if _clean(bindings.get("user"), 120).lower() == clean:
                return "user"
    return "unknown"


def _subject_owners(value: Any) -> list[str]:
    return sorted(
        {
            _clean(item, 80)
            for item in _nested_values(value, "subject_owner")
            if _clean(item, 80)
        }
    )


def _project_value(value: Any, *, path: str, depth: int = 0) -> tuple[Any, list[str]]:
    if value is None or isinstance(value, (bool, int, float)):
        return value, []
    if isinstance(value, str):
        clean = _clean(value, 180)
        return clean, ([path] if len(str(value)) > len(clean) else [])
    if depth >= 4 and isinstance(value, list) and all(
        item is None or isinstance(item, (bool, int, float, str))
        for item in value[:12]
    ):
        projected = [
            _clean(item, 180) if isinstance(item, str) else item
            for item in value[:12]
        ]
        omitted = [f"{path}[12:{len(value)}]"] if len(value) > 12 else []
        return projected, omitted
    # Depth limits containers, not scalar leaves. Nested scalar/choice
    # projections commonly put the actual value one level below the metric id;
    # replacing that leaf with null destroys the comparison the presenter must
    # preserve.
    if depth >= 4:
        return None, [path]
    if isinstance(value, list):
        result: list[Any] = []
        omitted: list[str] = []
        for index, item in enumerate(value[:12]):
            projected, child = _project_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
            result.append(projected)
            omitted.extend(child)
        if len(value) > 12:
            omitted.append(f"{path}[12:{len(value)}]")
        return result, omitted
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        omitted: list[str] = []
        original = {str(key): item for key, item in value.items()}
        keys = [key for key in _PRIORITY_KEYS if key in original]
        keys.extend(sorted(key for key in original if key not in keys))
        for key in keys:
            child_path = f"{path}.{key}" if path else key
            if key.lower() in _DROP_DETAIL_KEYS:
                omitted.append(child_path)
                continue
            if len(result) >= 24:
                omitted.append(child_path)
                continue
            projected, child = _project_value(
                original[key],
                path=child_path,
                depth=depth + 1,
            )
            result[key] = projected
            omitted.extend(child)
        return result, omitted
    return _clean(value, 180), [path]


def _bound_fact_envelope(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 6000:
        raise AuipProtocolError("presentation_fact_envelope_too_large", str(len(encoded)))
    return copy.deepcopy(facts)


def _nested_values(value: Any, key: str) -> list[Any]:
    result: list[Any] = []
    if isinstance(value, Mapping):
        for current_key, item in value.items():
            if str(current_key).lower() == key.lower():
                result.append(item)
            result.extend(_nested_values(item, key))
    elif isinstance(value, list):
        for item in value:
            result.extend(_nested_values(item, key))
    return result


def _first_nested(value: Any, key: str) -> Any:
    found = _nested_values(value, key)
    return found[0] if found else None


def _first_text(value: Any, keys: Sequence[str]) -> str:
    for key in keys:
        for item in _nested_values(value, key):
            clean = _clean(item, 160)
            if clean:
                return clean
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[: max(1, int(limit))]


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
