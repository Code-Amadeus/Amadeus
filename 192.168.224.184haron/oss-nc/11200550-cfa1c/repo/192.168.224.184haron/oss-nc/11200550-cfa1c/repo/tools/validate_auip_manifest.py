from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.auip_contract import AuipProtocolError, parse_manifest  # noqa: E402


SMALL_CHOICE_SPACE_MAX = 32
ROLE_ACTION_TYPES_MAX_CHARS = 520
_DOTTED_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_/-])([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+)(?![A-Za-z0-9_/-])"
)


def validate_file(path: Path) -> dict:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuipProtocolError("manifest_read_failed", str(exc)) from exc
    parsed = parse_manifest(source)
    if "participant" in parsed.stances and "spectator" not in parsed.stances:
        raise AuipProtocolError(
            "participant_requires_spectator",
            (
                "an authored participant surface must preserve the lower-authority "
                "read-only spectator mode"
            ),
        )
    if "participant" in parsed.stances and not parsed.situation_kinds:
        raise AuipProtocolError(
            "missing_situation_kinds",
            "participant authoring must declare at least one standard situation kind",
        )
    if "participant" in parsed.stances and not parsed.actions:
        raise AuipProtocolError(
            "missing_participant_actions",
            "participant authoring must expose at least one real typed application action",
        )
    if "participant" in parsed.stances and not parsed.interaction_summary:
        raise AuipProtocolError(
            "missing_interaction_summary",
            (
                "participant authoring must describe its domain affordances "
                "with natural-language examples in app.interactionSummary"
            ),
        )
    if "participant" in parsed.stances:
        _validate_role_action_surface(
            parsed.interaction_summary,
            tuple(parsed.actions),
        )
        _validate_action_precondition_surfaces(parsed)
    if parsed.controller is not None and not any(
        event.controller_effect for event in parsed.events.values()
    ):
        raise AuipProtocolError(
            "controller_requires_effect_event",
            (
                "Controller authoring must declare at least one sparse app-authored "
                "event with controllerEffect=true so an accepted policy can produce "
                "a lease-correlated application fact"
            ),
        )
    canonical = parsed.to_dict()
    if "participant" in parsed.stances:
        finite_size = _finite_action_space_size(canonical.get("actions"))
        if (
            finite_size is not None
            and 0 < finite_size <= SMALL_CHOICE_SPACE_MAX
            and "choice/v1" not in parsed.situation_kinds
        ):
            raise AuipProtocolError(
                "small_action_space_requires_choice",
                (
                    f"finite action space size {finite_size} must publish choice/v1; "
                    "retain the source application's action granularity and exact "
                    "payload, keep stable options with available=false when illegal, "
                    "and exceed the advisory projection budget rather than inventing "
                    "a whole-solution macro"
                ),
            )
    return canonical


def _validate_role_action_surface(
    interaction_summary: str,
    action_types: tuple[str, ...],
) -> None:
    """Keep one public role surface complete and summary examples executable."""

    encoded_types = json.dumps(
        sorted(action_types),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(encoded_types) > ROLE_ACTION_TYPES_MAX_CHARS:
        raise AuipProtocolError(
            "role_action_surface_too_large",
            (
                f"declared action types require {len(encoded_types)} characters; "
                f"maximum is {ROLE_ACTION_TYPES_MAX_CHARS}"
            ),
        )

    declared = set(action_types)
    prefixes = {value.split(".", 1)[0] for value in declared if "." in value}
    references = [
        match
        for match in _DOTTED_REFERENCE_RE.finditer(interaction_summary)
        if match.group(1).split(".", 1)[0] in prefixes
    ]
    unknown = sorted(
        {match.group(1) for match in references if match.group(1) not in declared}
    )
    if unknown:
        raise AuipProtocolError(
            "interaction_summary_unknown_action",
            ",".join(unknown),
        )
    for left, right in zip(references, references[1:]):
        if left.group(1) in declared and right.group(1) in declared:
            separator = interaction_summary[left.end() : right.start()]
            if "+" in separator:
                raise AuipProtocolError(
                    "interaction_summary_multi_action_example",
                    f"{left.group(1)} + {right.group(1)}",
                )


def _validate_action_precondition_surfaces(parsed) -> None:
    """Link Host-checkable preconditions to declared state and payload fields."""

    for action_type, spec in parsed.actions.items():
        for precondition in spec.preconditions:
            if precondition.kind == "action_available/v1":
                if "action_availability/v1" not in parsed.situation_kinds:
                    raise AuipProtocolError(
                        "action_precondition_requires_availability_situation",
                        action_type,
                    )
                continue
            if precondition.kind != "grid_cell_empty/v1":
                continue
            if "grid/v1" not in parsed.situation_kinds:
                raise AuipProtocolError(
                    "grid_precondition_requires_grid_situation",
                    action_type,
                )
            schema = spec.input_schema if isinstance(spec.input_schema, dict) else {}
            properties = (
                schema.get("properties")
                if isinstance(schema.get("properties"), dict)
                else {}
            )
            required = {
                str(value)
                for value in schema.get("required") or []
                if isinstance(value, str)
            }
            for field in (precondition.x_field, precondition.y_field):
                declaration = (
                    properties.get(field)
                    if isinstance(properties.get(field), dict)
                    else {}
                )
                if field not in required or declaration.get("type") != "integer":
                    raise AuipProtocolError(
                        "grid_precondition_requires_integer_coordinate",
                        f"{action_type}.{field}",
                    )


def _finite_action_space_size(value: object) -> int | None:
    """Return a conservative finite payload count, or ``None`` when unbounded."""

    if not isinstance(value, dict) or not value:
        return 0
    total = 0
    for action_type, raw_spec in value.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        schema = spec.get("inputSchema")
        if schema is None:
            total += 1
            continue
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return None
        if schema.get("additionalProperties") is not False:
            raise AuipProtocolError(
                "open_action_input_schema",
                f"{action_type} must set inputSchema.additionalProperties=false",
            )
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required = {
            str(item)
            for item in schema.get("required") or []
            if isinstance(item, str)
        }
        combinations = 1
        for name, raw_property in properties.items():
            prop = raw_property if isinstance(raw_property, dict) else {}
            if isinstance(prop.get("enum"), list) and prop["enum"]:
                variants = len(prop["enum"])
            elif "const" in prop:
                variants = 1
            elif isinstance(prop.get("oneOf"), list) and prop["oneOf"]:
                constants = [
                    item.get("const")
                    for item in prop["oneOf"]
                    if isinstance(item, dict) and "const" in item
                ]
                if len(constants) != len(prop["oneOf"]):
                    return None
                variants = len(constants)
            elif isinstance(prop.get("pattern"), str):
                variants = _finite_alternation_pattern_size(prop["pattern"])
                if variants is None:
                    return None
            else:
                return None
            if str(name) not in required:
                variants += 1
            combinations *= variants
            if combinations > SMALL_CHOICE_SPACE_MAX:
                return None
        total += combinations
        if total > SMALL_CHOICE_SPACE_MAX:
            return None
    return total


def _finite_alternation_pattern_size(pattern: str) -> int | None:
    """Recognize exact literal alternations such as ``^(A|B|C)$``."""

    match = re.fullmatch(r"\^\((?:\?:)?([^()]+)\)\$", str(pattern or ""))
    if match is None:
        return None
    alternatives = match.group(1).split("|")
    if not alternatives or any(
        not value or re.search(r"[^A-Za-z0-9_-]", value)
        for value in alternatives
    ):
        return None
    return len(set(alternatives))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an Amadeus AUIP v0 manifest.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--print-canonical", action="store_true")
    args = parser.parse_args(argv)
    try:
        canonical = validate_file(args.manifest)
    except AuipProtocolError as exc:
        print(f"AUIP manifest invalid: {exc}", file=sys.stderr)
        return 2
    if args.print_canonical:
        print(json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ok: AUIP v0 manifest {canonical['app']['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
