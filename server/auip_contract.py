"""AUIP v0 manifest and message validation.

AUIP is a cooperative application protocol, not another execution Provider.
Applications declare semantic events and bounded actions; the host owns
session identity, action authority, revisions, and context projection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping


AUIP_SCHEMA = "amadeus.auip/v0"
MAX_MESSAGE_BYTES = 64 * 1024

AuipActor = Literal["app", "user", "kurisu", "system"]
AuipStance = Literal["spectator", "participant"]
AuipImportance = Literal["ambient", "normal", "important", "blocking"]
AuipActionRisk = Literal["none", "local_execution"]
AuipActionPreconditionKind = Literal[
    "action_available/v1",
    "grid_cell_empty/v1",
]
AuipControllerTakeover = Literal["immediate", "safe_point"]

_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,78}[a-z0-9])?$")
_TYPE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_IMPORTANCE = frozenset({"ambient", "normal", "important", "blocking"})
_STANCES = frozenset({"spectator", "participant"})
_ACTION_RISKS = frozenset({"none", "local_execution"})
_ACTORS = frozenset({"app", "user", "kurisu", "system"})
_SITUATION_KINDS = frozenset(
    {
        "action_availability/v1",
        "grid/v1",
        "choice/v1",
        "scalars/v1",
        "sequence/v1",
        "controller/v1",
    }
)
_CONTROLLER_TAKEOVER = frozenset({"immediate", "safe_point"})
_ACTION_PRECONDITION_KINDS = frozenset(
    {"action_available/v1", "grid_cell_empty/v1"}
)
_STATE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,79}$")


class AuipProtocolError(ValueError):
    """A caller supplied an invalid or unauthorized AUIP value."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "invalid_auip_message")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


@dataclass(frozen=True, slots=True)
class AuipEventSpec:
    type: str
    beat: bool = False
    importance: AuipImportance = "normal"
    terminal: bool = False
    participant_opportunity: bool = False
    controller_effect: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "beat": self.beat,
            "importance": self.importance,
            "terminal": self.terminal,
            "participantOpportunity": self.participant_opportunity,
            "controllerEffect": self.controller_effect,
        }


@dataclass(frozen=True, slots=True)
class AuipActionPrecondition:
    """One narrow Host-checkable condition over a standard situation shape."""

    kind: AuipActionPreconditionKind
    state_path: str
    x_field: str = ""
    y_field: str = ""

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": self.kind,
            "statePath": self.state_path,
        }
        if self.kind == "grid_cell_empty/v1":
            value["xField"] = self.x_field
            value["yField"] = self.y_field
        return value


@dataclass(frozen=True, slots=True)
class AuipActionSpec:
    type: str
    description: str
    risk: AuipActionRisk = "local_execution"
    input_schema: dict[str, Any] | None = None
    preconditions: tuple[AuipActionPrecondition, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": self.type,
            "description": self.description,
            "risk": self.risk,
        }
        if self.input_schema is not None:
            value["inputSchema"] = _json_copy(self.input_schema)
        if self.preconditions:
            value["preconditions"] = [item.to_dict() for item in self.preconditions]
        return value


@dataclass(frozen=True, slots=True)
class AuipControllerSpec:
    policy_actions: tuple[str, ...]
    lease_duration_ms: int
    max_action_rate_hz: float
    takeover: AuipControllerTakeover

    def to_dict(self) -> dict[str, Any]:
        return {
            "policyActions": list(self.policy_actions),
            "leaseDurationMs": self.lease_duration_ms,
            "maxActionRateHz": self.max_action_rate_hz,
            "takeover": self.takeover,
        }


@dataclass(frozen=True, slots=True)
class AuipManifest:
    app_id: str
    title: str
    version: str
    objective: str
    interaction_summary: str
    events: dict[str, AuipEventSpec]
    actions: dict[str, AuipActionSpec]
    stances: tuple[AuipStance, ...]
    situation_kinds: tuple[str, ...] = ()
    controller: AuipControllerSpec | None = None
    schema: str = AUIP_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "app": {
                "id": self.app_id,
                "title": self.title,
                "version": self.version,
            },
            "events": {
                key: value.to_dict() | {"type": key}
                for key, value in self.events.items()
            },
            "actions": {
                key: value.to_dict() | {"type": key}
                for key, value in self.actions.items()
            },
            "stances": list(self.stances),
        }
        if self.situation_kinds:
            value["situationKinds"] = list(self.situation_kinds)
        if self.objective:
            value["app"]["objective"] = self.objective
        if self.interaction_summary:
            value["app"]["interactionSummary"] = self.interaction_summary
        if self.controller is not None:
            value["controller"] = self.controller.to_dict()
        return value


def parse_manifest(value: Any) -> AuipManifest:
    source = _mapping(value, "manifest")
    _assert_message_size(source)
    schema = _text(source.get("schema"), "schema", max_len=80)
    if schema != AUIP_SCHEMA:
        raise AuipProtocolError("unsupported_schema", schema)

    app = _mapping(source.get("app"), "app")
    app_id = _identifier(app.get("id"), "app.id")
    title = _text(app.get("title"), "app.title", max_len=120)
    version = _text(app.get("version") or "0.1.0", "app.version", max_len=40)
    objective = _optional_text(app.get("objective"), "app.objective", max_len=240)
    interaction_summary = _optional_text(
        app.get("interactionSummary"),
        "app.interactionSummary",
        max_len=640,
    )

    raw_events = _mapping(source.get("events"), "events")
    raw_actions = _mapping(source.get("actions"), "actions")
    if not raw_events:
        raise AuipProtocolError("missing_events", "declare at least one semantic event")

    events: dict[str, AuipEventSpec] = {}
    for raw_type, raw_spec in raw_events.items():
        event_type = _semantic_type(raw_type, "event type")
        spec = _mapping(raw_spec, f"events.{event_type}")
        beat = spec.get("beat", False)
        if not isinstance(beat, bool):
            raise AuipProtocolError("invalid_beat", event_type)
        terminal = spec.get("terminal", False)
        if not isinstance(terminal, bool):
            raise AuipProtocolError("invalid_terminal", event_type)
        importance = str(spec.get("importance") or "normal").strip().lower()
        if importance not in _IMPORTANCE:
            raise AuipProtocolError("invalid_importance", event_type)
        participant_opportunity = spec.get("participantOpportunity", False)
        if not isinstance(participant_opportunity, bool):
            raise AuipProtocolError("invalid_participant_opportunity", event_type)
        controller_effect = spec.get("controllerEffect", False)
        if not isinstance(controller_effect, bool):
            raise AuipProtocolError("invalid_controller_effect", event_type)
        if terminal and participant_opportunity:
            raise AuipProtocolError(
                "terminal_participant_opportunity",
                (
                    f"{event_type} cannot assign a Participant decision after "
                    "ending the AppSession"
                ),
            )
        events[event_type] = AuipEventSpec(
            type=event_type,
            beat=beat,
            importance=importance,  # type: ignore[arg-type]
            terminal=terminal,
            participant_opportunity=participant_opportunity,
            controller_effect=controller_effect,
        )

    actions: dict[str, AuipActionSpec] = {}
    for raw_type, raw_spec in raw_actions.items():
        action_type = _semantic_type(raw_type, "action type")
        spec = _mapping(raw_spec, f"actions.{action_type}")
        risk = str(spec.get("risk") or "local_execution").strip().lower()
        if risk not in _ACTION_RISKS:
            raise AuipProtocolError("unsupported_action_risk", action_type)
        actions[action_type] = AuipActionSpec(
            type=action_type,
            description=_text(spec.get("description"), f"actions.{action_type}.description", max_len=240),
            risk=risk,  # type: ignore[arg-type]
            input_schema=_action_input_schema(
                spec.get("inputSchema"),
                f"actions.{action_type}.inputSchema",
            ),
            preconditions=_action_preconditions(
                spec.get("preconditions"),
                f"actions.{action_type}.preconditions",
            ),
        )

    raw_stances = source.get("stances")
    if not isinstance(raw_stances, list) or not raw_stances:
        raise AuipProtocolError("missing_stances")
    stances: list[AuipStance] = []
    for raw_stance in raw_stances:
        stance = str(raw_stance or "").strip().lower()
        if stance not in _STANCES:
            raise AuipProtocolError("invalid_stance", stance)
        if stance not in stances:
            stances.append(stance)  # type: ignore[arg-type]

    opportunity_events = sorted(
        event.type for event in events.values() if event.participant_opportunity
    )
    if opportunity_events and "participant" not in stances:
        raise AuipProtocolError(
            "participant_opportunity_requires_participant",
            ",".join(opportunity_events),
        )

    raw_situation_kinds = source.get("situationKinds", [])
    if not isinstance(raw_situation_kinds, list):
        raise AuipProtocolError("invalid_situation_kinds")
    situation_kinds: list[str] = []
    for raw_kind in raw_situation_kinds:
        kind = str(raw_kind or "").strip().lower()
        if kind not in _SITUATION_KINDS:
            raise AuipProtocolError("unsupported_situation_kind", kind)
        if kind not in situation_kinds:
            situation_kinds.append(kind)

    controller = _controller_spec(
        source.get("controller"),
        actions=actions,
        stances=tuple(stances),
        situation_kinds=tuple(situation_kinds),
    )
    if controller is None:
        marked = sorted(
            event.type for event in events.values() if event.controller_effect
        )
        if marked:
            raise AuipProtocolError(
                "controller_effect_requires_controller",
                ",".join(marked),
            )

    return AuipManifest(
        app_id=app_id,
        title=title,
        version=version,
        objective=objective,
        interaction_summary=interaction_summary,
        events=events,
        actions=actions,
        stances=tuple(stances),
        situation_kinds=tuple(situation_kinds),
        controller=controller,
    )


def validate_actor(value: Any) -> AuipActor:
    actor = str(value or "").strip().lower()
    if actor not in _ACTORS:
        raise AuipProtocolError("invalid_actor", actor)
    return actor  # type: ignore[return-value]


def validate_payload(value: Any, *, name: str = "payload") -> dict[str, Any]:
    payload = _mapping(value, name)
    _assert_message_size(payload)
    return _json_copy(payload)


def validate_state(value: Any) -> dict[str, Any]:
    return validate_payload(value, name="state")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuipProtocolError("invalid_object", name)
    return {str(key): item for key, item in value.items()}


def _action_input_schema(value: Any, name: str) -> dict[str, Any] | None:
    """Keep action arguments in the public MCP Tool schema shape.

    AUIP does not implement a second JSON Schema engine.  The schema is a
    model-facing action contract; application mechanics and the accepted
    receipt remain authoritative for whether an action actually happened.
    The Host only verifies that the declaration is a bounded object schema
    before passing it to a native function-tool or an eventual MCP facade.
    """

    if value is None:
        return None
    source = _mapping(value, name)
    if str(source.get("type") or "") != "object":
        raise AuipProtocolError("invalid_action_input_schema", f"{name}.type")
    properties = source.get("properties", {})
    if not isinstance(properties, Mapping):
        raise AuipProtocolError("invalid_action_input_schema", f"{name}.properties")
    canonical = _json_copy(source)
    _assert_message_size(canonical)
    return canonical


def _action_preconditions(
    value: Any,
    name: str,
) -> tuple[AuipActionPrecondition, ...]:
    """Parse only portable conditions the Host can prove mechanically.

    This is deliberately not an expression language and does not alter action
    payloads.  Applications remain the final authority through their receipt;
    the declaration merely prevents an obviously invalid proposal from reaching
    the speaking-role gate or application request boundary.
    """

    if value is None:
        return ()
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise AuipProtocolError("invalid_action_preconditions", name)
    output: list[AuipActionPrecondition] = []
    for index, raw in enumerate(value):
        item_name = f"{name}[{index}]"
        source = _mapping(raw, item_name)
        kind = str(source.get("kind") or "").strip().lower()
        if kind not in _ACTION_PRECONDITION_KINDS:
            raise AuipProtocolError("unsupported_action_precondition", kind)
        expected_fields = (
            {"kind", "statePath", "xField", "yField"}
            if kind == "grid_cell_empty/v1"
            else {"kind", "statePath"}
        )
        if set(source) != expected_fields:
            raise AuipProtocolError("invalid_action_precondition", item_name)
        state_path = _state_path(source.get("statePath"), f"{item_name}.statePath")
        x_field = (
            _state_field(source.get("xField"), f"{item_name}.xField")
            if kind == "grid_cell_empty/v1"
            else ""
        )
        y_field = (
            _state_field(source.get("yField"), f"{item_name}.yField")
            if kind == "grid_cell_empty/v1"
            else ""
        )
        output.append(
            AuipActionPrecondition(
                kind=kind,  # type: ignore[arg-type]
                state_path=state_path,
                x_field=x_field,
                y_field=y_field,
            )
        )
    return tuple(output)


def _state_field(value: Any, name: str) -> str:
    field = _text(value, name, max_len=80)
    if not _STATE_FIELD.fullmatch(field):
        raise AuipProtocolError("invalid_state_field", name)
    return field


def _state_path(value: Any, name: str) -> str:
    path = _text(value, name, max_len=180)
    parts = path.split(".")
    if not 1 <= len(parts) <= 8 or any(
        not _STATE_FIELD.fullmatch(part) for part in parts
    ):
        raise AuipProtocolError("invalid_state_path", name)
    return path


def _controller_spec(
    value: Any,
    *,
    actions: Mapping[str, AuipActionSpec],
    stances: tuple[AuipStance, ...],
    situation_kinds: tuple[str, ...],
) -> AuipControllerSpec | None:
    if value is None:
        return None
    source = _mapping(value, "controller")
    unknown = sorted(
        set(source)
        - {"policyActions", "leaseDurationMs", "maxActionRateHz", "takeover"}
    )
    if unknown:
        raise AuipProtocolError("unknown_controller_field", ",".join(unknown))
    raw_policy_actions = source.get("policyActions")
    if not isinstance(raw_policy_actions, list) or not raw_policy_actions:
        raise AuipProtocolError("missing_controller_policy_actions")
    policy_actions: list[str] = []
    for raw_action in raw_policy_actions:
        action_type = _semantic_type(raw_action, "controller.policyActions")
        if action_type not in actions:
            raise AuipProtocolError("unknown_controller_policy_action", action_type)
        if action_type not in policy_actions:
            policy_actions.append(action_type)
    for action_type in policy_actions:
        schema = actions[action_type].input_schema
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping) or not properties:
            raise AuipProtocolError("controller_policy_schema_required", action_type)
        if schema.get("additionalProperties") is not False:
            raise AuipProtocolError("controller_policy_schema_open", action_type)
    if "participant" not in stances or "spectator" not in stances:
        raise AuipProtocolError("controller_requires_participant_and_spectator")
    if "controller/v1" not in situation_kinds:
        raise AuipProtocolError("controller_situation_required")
    lease_duration_ms = _bounded_integer(
        source.get("leaseDurationMs"),
        "controller.leaseDurationMs",
        minimum=250,
        maximum=300_000,
    )
    try:
        max_action_rate_hz = float(source.get("maxActionRateHz"))
    except (TypeError, ValueError) as exc:
        raise AuipProtocolError(
            "invalid_controller_rate", "controller.maxActionRateHz"
        ) from exc
    if not 0 < max_action_rate_hz <= 240:
        raise AuipProtocolError(
            "invalid_controller_rate", "controller.maxActionRateHz"
        )
    takeover = str(source.get("takeover") or "").strip().lower()
    if takeover not in _CONTROLLER_TAKEOVER:
        raise AuipProtocolError("invalid_controller_takeover", takeover)
    return AuipControllerSpec(
        policy_actions=tuple(policy_actions),
        lease_duration_ms=lease_duration_ms,
        max_action_rate_hz=max_action_rate_hz,
        takeover=takeover,  # type: ignore[arg-type]
    )


def _bounded_integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise AuipProtocolError("invalid_integer", name)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AuipProtocolError("invalid_integer", name) from exc
    if parsed != value or not minimum <= parsed <= maximum:
        raise AuipProtocolError("invalid_integer", name)
    return parsed


def _text(value: Any, name: str, *, max_len: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise AuipProtocolError("missing_value", name)
    if len(text) > max_len:
        raise AuipProtocolError("value_too_long", name)
    return text


def _optional_text(value: Any, name: str, *, max_len: int) -> str:
    if value in (None, ""):
        return ""
    return _text(value, name, max_len=max_len)


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name, max_len=80).lower()
    if not _ID.fullmatch(text):
        raise AuipProtocolError("invalid_identifier", name)
    return text


def _semantic_type(value: Any, name: str) -> str:
    text = _text(value, name, max_len=120).lower()
    if not _TYPE.fullmatch(text):
        raise AuipProtocolError("invalid_semantic_type", text)
    return text


def _assert_message_size(value: Any) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AuipProtocolError("not_json_serializable", str(exc)) from exc
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise AuipProtocolError("message_too_large")


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise AuipProtocolError("not_json_serializable", str(exc)) from exc
