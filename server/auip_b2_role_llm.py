"""Single-call role choice for AUIP B2 closed and open action spaces."""

from __future__ import annotations

from typing import Any, Mapping

from config import settings
from server.assistant_language import (
    current_assistant_language,
    text_matches_assistant_language,
)
from server.auip_action_candidates import AuipActionCandidate
from server.auip_contract import AuipProtocolError, validate_payload
from server.auip_narration_llm import (
    call_auip_schema,
    call_auip_tool,
    has_auip_model_config,
)
from server.inherited_role_prompt import inherited_main_role_prompt
from tools.text_utils import parse_tags_and_clean


_TOOL_NAME = "auip_b2_select_candidate"
_OPEN_ACTION_PREFIX = "auip_b2_open_action_"
_PROTOCOL_TERMS = (
    "auip",
    "candidate_id",
    "payload",
    "schema",
    "receipt",
    "revision",
)


B2_ROLE_ADDON = """
[AUIP B2 DISCRETE ROLE DECISION]
You are continuing the main character inside one active application branch.
The supplied host facts are accepted current application state. Branch dialogue
provides continuity but never overrides those facts. The supplied candidate ids
refer to exact action type and payload pairs privately fixed by the Host at this
revision. Select one id; do not invent, copy, or modify action fields or payload.

Use the application's objective and interaction summary only as domain
background. Prefer an immediately objective-completing choice, then prevention
of an immediate adverse result, then one reasonable continuation. Follow a
concrete legal user proposal. You may select a supported alternative when your
character judgment disagrees, but then use instruction_relation=safe_alternative
and make the reason understandable in the natural spoken line. Do not search
for a globally optimal move when no urgent fact distinguishes one.

`instruction_relation` describes only a human instruction. It is absent from
an application-assigned automatic turn because the Host already knows no human
instruction exists. If a human instruction is present, return `follows` for
its selected outcome or `safe_alternative` for a different supported choice
with a visible reason.

For a private automatic choice, `choice_reason` must name one concise semantic
priority or judgment—such as defence, pressure, positioning, following, or the
application objective. Do not use coordinates, legality, turn ownership, or
"one available choice" as the reason. This reason may later support sparse
in-character interpretation after the exact action is accepted.

Follow the payload's `presentation_contract`. A foreground line is held
privately until the application accepts the selected action; it must be concise,
in character, true after acceptance, and describe only the supported semantic
outcome. A private automatic choice emits no speech field because verified
post-action presentation has a different cadence owner. Never mention models,
agents, delegation, candidates, Host machinery, payload, schema, receipt, or
revision. In a foreground coordinate line, name the selected (x,y) accurately.
Return only the required structured object.
[/AUIP B2 DISCRETE ROLE DECISION]
"""


B2_OPEN_ROLE_ADDON = """
[AUIP B2 OPEN-PAYLOAD ROLE DECISION]
You are continuing the main character inside one active application branch.
The supplied host facts are accepted current application state. Branch dialogue
provides continuity but never overrides those facts. Some available actions are
exact revision-bound candidates privately fixed by the Host. Other available
actions have an open typed argument space and are exposed as native tools whose
payload field is the application's declared input contract.

Choose exactly one supplied tool. Use the fixed-candidate tool when an exact
candidate is the best outcome; do not invent or modify its action or payload.
Use one open-action tool only when its declared semantic action is the best
outcome, and fill its payload exactly from the accepted state and the user's
bounded instruction. Never choose a convenient empty action when its described
effect works against the current objective or instruction.

Use the application's objective and interaction summary only as domain
background. Prefer an immediately objective-completing choice, then prevention
of an immediate adverse result, then one reasonable continuation. Follow a
concrete legal user proposal. You may select a supported alternative when your
character judgment disagrees, but then use instruction_relation=safe_alternative
and explain the factual reason in the natural spoken line.

`instruction_relation` describes only a human instruction. It is absent from
an automatic turn because the Host owns that fact. With a human instruction,
return follows for the selected outcome or safe_alternative for a different
supported outcome.

Follow presentation_contract. A foreground line is held until the application
accepts the selected proposal; it must be concise, in character, true after
acceptance, and promise only that proposal. A private automatic choice has no
speech field because verified post-action presentation has a separate cadence
owner. Never mention models, agents, delegation, candidates, Host machinery,
payload, schemas, receipts, revisions, or AUIP. Return exactly one native tool
call and no prose outside it.
[/AUIP B2 OPEN-PAYLOAD ROLE DECISION]
"""


def has_b2_role_model_config() -> bool:
    return has_auip_model_config(settings.AUIP_ACTION_PROVIDER)


async def choose_b2_role_action(
    *,
    context: Mapping[str, Any],
    candidates: Mapping[str, AuipActionCandidate],
    user_instruction: str,
    branch_messages: list[Mapping[str, Any]],
    trigger: str,
    speech_required: bool = True,
) -> dict[str, Any]:
    """Return a candidate id and its single matching role line."""

    if not candidates:
        raise AuipProtocolError("b2_candidates_unavailable")
    candidate_ids = list(candidates)
    payload = _role_payload(
        context=context,
        candidates=candidates,
        user_instruction=user_instruction,
        branch_messages=branch_messages,
        trigger=trigger,
        speech_required=speech_required,
    )
    properties: dict[str, Any] = {
        "candidate_id": {"type": "string", "enum": candidate_ids},
        "choice_reason": {"type": "string", "minLength": 1},
    }
    required = ["candidate_id", "choice_reason"]
    if str(user_instruction or "").strip():
        properties["instruction_relation"] = _instruction_relation_property()
        required.append("instruction_relation")
    if speech_required:
        properties["speech"] = {"type": "string", "minLength": 1}
        required.append("speech")
    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    arguments = await call_auip_schema(
        system_prompt=(
            inherited_main_role_prompt("base") + "\n\n" + B2_ROLE_ADDON
        ),
        payload=payload,
        schema=schema,
        schema_name=_TOOL_NAME,
        max_tokens=700,
        provider=settings.AUIP_ACTION_PROVIDER,
        model=settings.AUIP_ACTION_MODEL,
        reasoning_effort=settings.AUIP_ACTION_REASONING_EFFORT,
        service_tier=getattr(settings, "AUIP_ACTION_SERVICE_TIER", "auto"),
        timeout_s=getattr(settings, "AUIP_ACTION_TIMEOUT_S", 8.0),
    )
    if arguments is None:
        raise AuipProtocolError("b2_role_decision_unavailable")
    candidate_id = str(arguments.get("candidate_id") or "").strip()
    if candidate_id not in candidates:
        raise AuipProtocolError("b2_candidate_not_available")
    presentation = _validated_role_presentation(
        arguments,
        user_instruction=user_instruction,
        speech_required=speech_required,
    )
    return {
        "candidate_id": candidate_id,
        **presentation,
    }


async def choose_b2_open_role_action(
    *,
    context: Mapping[str, Any],
    candidates: Mapping[str, AuipActionCandidate],
    uncovered_action_types: tuple[str, ...] | list[str],
    user_instruction: str,
    branch_messages: list[Mapping[str, Any]],
    trigger: str,
    speech_required: bool = True,
) -> dict[str, Any]:
    """Choose one locked candidate or author one declared open payload."""

    available = context.get("available_actions")
    available = available if isinstance(available, Mapping) else {}
    uncovered = tuple(
        action_type
        for action_type in dict.fromkeys(
            str(value or "").strip().lower()
            for value in uncovered_action_types
        )
        if action_type and action_type in available
    )
    tools: list[dict[str, Any]] = []
    action_by_tool: dict[str, str] = {}
    common_properties = _role_result_properties(
        speech_required=speech_required,
        user_instruction=user_instruction,
    )
    common_required = [
        "choice_reason",
        *(
            ["instruction_relation"]
            if str(user_instruction or "").strip()
            else []
        ),
        *(["speech"] if speech_required else []),
    ]
    if candidates:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": (
                        "Select one exact Host-declared candidate from the supplied "
                        "catalog or compact grid contract."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {
                                "type": "string",
                                "enum": list(candidates),
                            },
                            **common_properties,
                        },
                        "required": ["candidate_id", *common_required],
                        "additionalProperties": False,
                    },
                },
            }
        )
    for index, action_type in enumerate(uncovered):
        raw_spec = available.get(action_type)
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        input_schema = spec.get("inputSchema")
        if not isinstance(input_schema, Mapping):
            input_schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            }
        tool_name = f"{_OPEN_ACTION_PREFIX}{index}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": str(
                        spec.get("description") or f"Propose {action_type}."
                    )[:240],
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "payload": dict(input_schema),
                            **common_properties,
                        },
                        "required": ["payload", *common_required],
                        "additionalProperties": False,
                    },
                },
            }
        )
        action_by_tool[tool_name] = action_type
    if not tools or not uncovered:
        raise AuipProtocolError("b2_open_actions_unavailable")

    payload = {
        **_role_payload(
            context=context,
            candidates=candidates,
            user_instruction=user_instruction,
            branch_messages=branch_messages,
            trigger=trigger,
            speech_required=speech_required,
        ),
        "open_action_contracts": [
            {
                "action_type": action_type,
                "description": str(
                    (
                        available.get(action_type)
                        if isinstance(available.get(action_type), Mapping)
                        else {}
                    ).get("description")
                    or action_type
                )[:240],
            }
            for action_type in uncovered
        ],
    }
    decision = await call_auip_tool(
        system_prompt=(
            inherited_main_role_prompt("base")
            + "\n\n"
            + B2_OPEN_ROLE_ADDON
        ),
        payload=payload,
        tools=tools,
        max_tokens=700,
        provider=settings.AUIP_ACTION_PROVIDER,
        model=settings.AUIP_ACTION_MODEL,
        reasoning_effort=settings.AUIP_ACTION_REASONING_EFFORT,
        service_tier=getattr(settings, "AUIP_ACTION_SERVICE_TIER", "auto"),
        timeout_s=getattr(settings, "AUIP_ACTION_TIMEOUT_S", 8.0),
    )
    if decision is None:
        raise AuipProtocolError("b2_open_role_decision_unavailable")
    tool_name, arguments = decision
    presentation = _validated_role_presentation(
        arguments,
        user_instruction=user_instruction,
        speech_required=speech_required,
    )
    if tool_name == _TOOL_NAME:
        candidate_id = str(arguments.get("candidate_id") or "").strip()
        if candidate_id not in candidates:
            raise AuipProtocolError("b2_candidate_not_available")
        return {"candidate_id": candidate_id, **presentation}
    action_type = action_by_tool.get(tool_name, "")
    if not action_type:
        raise AuipProtocolError("b2_open_action_not_available")
    return {
        "action_type": action_type,
        "payload": validate_payload(arguments.get("payload") or {}),
        **presentation,
    }


def _instruction_relation_property() -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["follows", "safe_alternative"],
    }


def _role_result_properties(
    *,
    speech_required: bool,
    user_instruction: str,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "choice_reason": {"type": "string", "minLength": 1},
    }
    if str(user_instruction or "").strip():
        properties["instruction_relation"] = _instruction_relation_property()
    if speech_required:
        properties["speech"] = {"type": "string", "minLength": 1}
    return properties


def _validated_role_presentation(
    arguments: Mapping[str, Any],
    *,
    user_instruction: str,
    speech_required: bool,
) -> dict[str, Any]:
    has_instruction = bool(str(user_instruction or "").strip())
    relation = (
        str(arguments.get("instruction_relation") or "").strip().lower()
        if has_instruction
        else "not_applicable"
    )
    reason = str(arguments.get("choice_reason") or "").strip()[:600]
    raw_speech = str(arguments.get("speech") or "").strip()[:1000]
    speech, presentation_actions = parse_tags_and_clean(raw_speech)
    speech = str(speech or "").strip()[:800]
    expected_relations = (
        {"follows", "safe_alternative"}
        if has_instruction
        else {"not_applicable"}
    )
    if relation not in expected_relations:
        raise AuipProtocolError(
            "invalid_b2_instruction_relation",
            f"relation={relation or '<empty>'}; user_instruction={bool(str(user_instruction or '').strip())}",
        )
    missing_fields: list[str] = []
    if not reason:
        missing_fields.append("choice_reason")
    if speech_required and not speech:
        missing_fields.append("speech")
    if missing_fields:
        raise AuipProtocolError(
            "invalid_b2_role_decision",
            f"missing={','.join(missing_fields)}",
        )
    if speech_required and not text_matches_assistant_language(
        speech,
        current_assistant_language(),
    ):
        raise AuipProtocolError("invalid_b2_role_language")
    folded = speech.casefold()
    if speech_required and any(term in folded for term in _PROTOCOL_TERMS):
        raise AuipProtocolError("b2_role_protocol_leak")
    if any(
        str(action.get("type") or "").upper() in {"DELEGATE", "AUIP"}
        for action in presentation_actions
        if isinstance(action, Mapping)
    ):
        raise AuipProtocolError("b2_role_protocol_leak")
    emotion = next(
        (
            str((action.get("attrs") or {}).get("preset") or "").strip()
            for action in presentation_actions
            if isinstance(action, Mapping)
            and str(action.get("type") or "").upper() == "EMO"
            and str((action.get("attrs") or {}).get("preset") or "").strip()
        ),
        "neutral",
    )
    return {
        "instruction_relation": relation,
        "choice_reason": reason,
        "speech": speech,
        "emotion": emotion[:80],
    }


def _role_payload(
    *,
    context: Mapping[str, Any],
    candidates: Mapping[str, AuipActionCandidate],
    user_instruction: str,
    branch_messages: list[Mapping[str, Any]],
    trigger: str,
    speech_required: bool,
) -> dict[str, Any]:
    state = context.get("state") if isinstance(context.get("state"), Mapping) else {}
    grid_candidates = [
        candidate
        for candidate in candidates.values()
        if candidate.source == "grid_cell_empty/v1"
    ]
    grid_contracts: list[dict[str, Any]] = []
    for action_type in sorted({item.action_type for item in grid_candidates}):
        group = [item for item in grid_candidates if item.action_type == action_type]
        prefixes = sorted({item.candidate_id.rsplit("_", 2)[0] for item in group})
        for prefix in prefixes:
            members = [
                item for item in group if item.candidate_id.startswith(prefix + "_")
            ]
            legal_ids = {item.candidate_id for item in members}
            grid = _first_situation(state, "grid/v1")
            width = int(grid.get("width") or 0) if isinstance(grid, Mapping) else 0
            height = int(grid.get("height") or 0) if isinstance(grid, Mapping) else 0
            unavailable = [
                f"{prefix}_{y:02d}_{x:02d}"
                for y in range(max(0, height))
                for x in range(max(0, width))
                if f"{prefix}_{y:02d}_{x:02d}" not in legal_ids
            ]
            grid_contracts.append(
                {
                    "action_type": action_type,
                    "candidate_id_format": f"{prefix}_YY_XX",
                    "encoding": (
                        "YY and XX are zero-padded y and x coordinates; "
                        f"(7,7) maps to {prefix}_07_07."
                    ),
                    "candidate_count": len(members),
                    "unavailable_candidate_ids": unavailable,
                }
            )
    return {
        "app": context.get("app") or {},
        "trigger": str(trigger or "")[:120],
        "presentation_contract": (
            "Return the one receipt-held role line for this foreground user turn."
            if speech_required
            else (
                "This is a private automatic action choice. Return no speech field; "
                "verified post-action presentation has a separate sparse cadence owner."
            )
        ),
        "branch_messages": [
            {
                "role": str(item.get("role") or "")[:20],
                "content": str(item.get("content") or "")[:700],
            }
            for item in branch_messages[-10:]
            if isinstance(item, Mapping)
        ],
        "host_facts": {
            "revision": int(context.get("revision") or 0),
            "state": state,
            "recent_verified_self_actions": list(
                context.get("recent_verified_self_actions") or []
            )[-3:],
        },
        "user_instruction": str(user_instruction or "").strip()[:1000],
        "candidate_catalog": [
            {
                "candidate_id": item.candidate_id,
                "meaning": item.semantic_label,
                "action_type": item.action_type,
                "payload": dict(item.payload),
            }
            for item in candidates.values()
            if item.source != "grid_cell_empty/v1"
        ],
        "grid_candidate_contracts": grid_contracts,
    }


def _first_situation(value: Any, kind: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        if str(value.get("kind") or "").strip().lower() == kind:
            return value
        for child in value.values():
            found = _first_situation(child, kind)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_situation(child, kind)
            if found is not None:
                return found
    return None
