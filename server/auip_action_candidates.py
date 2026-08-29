"""Host-compiled, revision-bound candidates for discrete AUIP actions.

The model may select one opaque candidate id.  It never authors an action type
or payload on this path.  Compilation is deliberately limited to action spaces
whose exact members can be derived mechanically from accepted state and the
manifest: ``choice/v1``, ``grid_cell_empty/v1``, empty payloads, and small
closed enum products.  Open or unbounded schemas remain on the existing split
Participant path instead of being guessed here.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

from server.auip_contract import AuipProtocolError, validate_payload
from server.auip_runtime import AuipRuntime


@dataclass(frozen=True, slots=True)
class AuipActionCandidate:
    candidate_id: str
    action_type: str
    payload: dict[str, Any]
    semantic_label: str
    revision: int
    decision_generation: int
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", validate_payload(self.payload))


@dataclass(frozen=True, slots=True)
class AuipCandidateCompilation:
    context: dict[str, Any]
    candidates: dict[str, AuipActionCandidate]
    uncovered_action_types: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether candidates cover every currently available action family."""

        return not self.uncovered_action_types


def compile_auip_action_candidates(
    runtime: AuipRuntime,
    app_session_id: str,
) -> AuipCandidateCompilation:
    """Compile one immutable candidate map from the current accepted snapshot."""

    context = runtime.participant_context(app_session_id, max_chars=7200)
    revision = int(context.get("revision") or 0)
    generation = int(context.get("decision_generation") or 0)
    output: dict[str, AuipActionCandidate] = {}
    expected_action_types: set[str] = set()
    choice_types = {
        str(value or "").strip().lower()
        for value in context.get("choice_action_types") or []
        if str(value or "").strip()
    }

    def append(
        *,
        action_type: str,
        payload: Mapping[str, Any],
        semantic_label: str,
        source: str,
        candidate_id: str = "",
    ) -> None:
        clean_type = str(action_type or "").strip().lower()
        clean_payload = validate_payload(payload)
        clean_id = str(candidate_id or "").strip() or _candidate_id(
            clean_type,
            clean_payload,
        )
        candidate = AuipActionCandidate(
            candidate_id=clean_id,
            action_type=clean_type,
            payload=clean_payload,
            semantic_label=str(semantic_label or clean_type).strip()[:240],
            revision=revision,
            decision_generation=generation,
            source=str(source or "")[:80],
        )
        try:
            runtime.check_action_preconditions(
                app_session_id=app_session_id,
                type=candidate.action_type,
                payload=dict(candidate.payload),
                expected_revision=candidate.revision,
            )
        except AuipProtocolError:
            return
        output[candidate.candidate_id] = candidate

    for option in context.get("available_choice_options") or []:
        if not isinstance(option, Mapping):
            continue
        action_type = str(option.get("action") or "").strip().lower()
        if not action_type:
            continue
        expected_action_types.add(action_type)
        append(
            action_type=action_type,
            payload=option.get("payload") or {},
            semantic_label=str(option.get("label") or action_type),
            source="choice/v1",
        )

    state = context.get("state") if isinstance(context.get("state"), Mapping) else {}
    available_actions = context.get("available_actions")
    available_actions = available_actions if isinstance(available_actions, Mapping) else {}
    for action_index, (raw_type, raw_spec) in enumerate(sorted(available_actions.items())):
        action_type = str(raw_type or "").strip().lower()
        if not action_type or action_type in choice_types:
            continue
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        schema = spec.get("inputSchema")
        schema = schema if isinstance(schema, Mapping) else {}
        required = {
            str(value or "").strip()
            for value in schema.get("required") or []
            if str(value or "").strip()
        }
        preconditions = [
            value
            for value in spec.get("preconditions") or []
            if isinstance(value, Mapping)
        ]
        if not _action_family_advertised_available(
            action_type=action_type,
            preconditions=preconditions,
            state=state,
        ):
            continue
        grid_condition = next(
            (
                value
                for value in preconditions
                if str(value.get("kind") or "").strip().lower()
                == "grid_cell_empty/v1"
            ),
            None,
        )
        if grid_condition is not None:
            x_field = str(grid_condition.get("xField") or "").strip()
            y_field = str(grid_condition.get("yField") or "").strip()
            grid = _nested(state, str(grid_condition.get("statePath") or ""))
            if (
                not x_field
                or not y_field
                or not isinstance(grid, Mapping)
            ):
                expected_action_types.add(action_type)
                continue
            rows = grid.get("rows")
            empty = str(grid.get("empty") or ".")
            if not isinstance(rows, list) or len(empty) != 1:
                expected_action_types.add(action_type)
                continue
            empty_cells = [
                (x, y)
                for y, row in enumerate(rows)
                if isinstance(row, str)
                for x, value in enumerate(row)
                if value == empty
            ]
            if not empty_cells:
                continue
            expected_action_types.add(action_type)
            if required.difference({x_field, y_field}):
                continue
            for x, y in empty_cells:
                append(
                    action_type=action_type,
                    payload={x_field: x, y_field: y},
                    semantic_label=f"Coordinate ({x},{y})",
                    source="grid_cell_empty/v1",
                    candidate_id=f"g{action_index}_{y:02d}_{x:02d}",
                )
            continue

        expected_action_types.add(action_type)
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        additional = schema.get("additionalProperties", True)
        if not required and not properties and additional is False:
            append(
                action_type=action_type,
                payload={},
                semantic_label=str(spec.get("description") or action_type),
                source="closed_empty_payload",
            )
            continue

        finite_fields: list[tuple[str, list[Any]]] = []
        finite = bool(required) and required == set(properties) and additional is False
        for key in sorted(required):
            prop = properties.get(key)
            values = prop.get("enum") if isinstance(prop, Mapping) else None
            if not isinstance(values, list) or not values or len(values) > 8:
                finite = False
                break
            finite_fields.append((key, list(values)))
        if not finite:
            continue
        combinations = math.prod(len(values) for _, values in finite_fields)
        if combinations > 32:
            continue
        for values in itertools.product(*(values for _, values in finite_fields)):
            payload = {
                finite_fields[index][0]: value
                for index, value in enumerate(values)
            }
            append(
                action_type=action_type,
                payload=payload,
                semantic_label=(
                    str(spec.get("description") or action_type)
                    + " "
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                ),
                source="finite_schema",
            )

    return AuipCandidateCompilation(
        context=dict(context),
        candidates=output,
        uncovered_action_types=tuple(
            sorted(
                expected_action_types.difference(
                    candidate.action_type for candidate in output.values()
                )
            )
        ),
    )


def _candidate_id(action_type: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        [str(action_type or "").strip().lower(), validate_payload(payload)],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "cand_" + hashlib.sha256(encoded).hexdigest()[:12]


def _nested(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _action_family_advertised_available(
    *,
    action_type: str,
    preconditions: list[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> bool:
    """Read only type-level availability; payload legality stays authoritative."""

    for precondition in preconditions:
        if str(precondition.get("kind") or "").strip().lower() != "action_available/v1":
            continue
        surface = _nested(state, str(precondition.get("statePath") or ""))
        if not isinstance(surface, Mapping):
            # Runtime validation should make this unreachable. Treat an
            # unreadable surface as uncovered rather than claiming B2 owns it.
            return True
        available = {
            str(value or "").strip().lower()
            for value in surface.get("availableActionTypes") or []
            if str(value or "").strip()
        }
        if action_type not in available:
            return False
    return True
