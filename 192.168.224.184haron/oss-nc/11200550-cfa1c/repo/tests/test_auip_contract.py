from __future__ import annotations

from server.auip_contract import AUIP_SCHEMA, AuipProtocolError, parse_manifest


def _manifest() -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {
            "id": "gomoku",
            "title": "Gomoku",
            "version": "0.1.0",
            "interactionSummary": (
                "Place one legal stone per turn. Examples: 'take center' maps "
                "to game.place_stone at a center cell; 'block that line' maps "
                "to one legal defensive coordinate."
            ),
        },
        "events": {
            "game.move_committed": {
                "beat": True,
                "participantOpportunity": True,
            },
            "game.finished": {"beat": True, "importance": "important", "terminal": True},
        },
        "actions": {
            "game.place_stone": {
                "description": "Place one stone on the current board.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "minimum": 0, "maximum": 14},
                        "y": {"type": "integer", "minimum": 0, "maximum": 14},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            }
        },
        "stances": ["spectator", "participant"],
    }


def test_manifest_is_small_and_machine_verified() -> None:
    manifest = parse_manifest(_manifest())
    assert manifest.app_id == "gomoku"
    assert "take center" in manifest.interaction_summary
    assert manifest.to_dict()["app"]["interactionSummary"] == (
        manifest.interaction_summary
    )
    assert manifest.events["game.finished"].terminal is True
    assert manifest.events["game.move_committed"].participant_opportunity is True
    assert (
        manifest.to_dict()["events"]["game.move_committed"]["participantOpportunity"]
        is True
    )
    assert manifest.actions["game.place_stone"].risk == "local_execution"
    assert manifest.actions["game.place_stone"].input_schema["required"] == ["x", "y"]
    assert manifest.stances == ("spectator", "participant")
    assert manifest.to_dict()["schema"] == AUIP_SCHEMA


def test_grid_precondition_round_trips_without_changing_action_payload_schema() -> None:
    manifest = _manifest()
    action = manifest["actions"]["game.place_stone"]
    action["preconditions"] = [
        {
            "kind": "grid_cell_empty/v1",
            "statePath": "board",
            "xField": "x",
            "yField": "y",
        }
    ]
    parsed = parse_manifest(manifest)
    exported = parsed.to_dict()["actions"]["game.place_stone"]
    assert exported["preconditions"] == action["preconditions"]
    assert exported["inputSchema"] == action["inputSchema"]

    action["preconditions"][0]["kind"] = "custom_expression/v1"
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "unsupported_action_precondition"
    else:
        raise AssertionError("AUIP v0 must not acquire an expression language")


def test_action_availability_precondition_round_trips_without_expression_rules() -> None:
    manifest = _manifest()
    manifest["situationKinds"] = ["action_availability/v1"]
    action = manifest["actions"]["game.place_stone"]
    action["preconditions"] = [
        {
            "kind": "action_available/v1",
            "statePath": "actionAvailability",
        }
    ]

    parsed = parse_manifest(manifest)
    exported = parsed.to_dict()["actions"]["game.place_stone"]
    assert exported["preconditions"] == action["preconditions"]
    assert exported["inputSchema"] == action["inputSchema"]

    action["preconditions"][0]["operator"] = "equals"
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "invalid_action_precondition"
    else:
        raise AssertionError("action availability must not become an expression language")


def test_manifest_rejects_unknown_authority_and_untyped_names() -> None:
    manifest = _manifest()
    manifest["actions"]["game.place_stone"]["risk"] = "external_side_effect"
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "unsupported_action_risk"
    else:
        raise AssertionError("AUIP v0 must not silently acquire external authority")

    manifest = _manifest()
    manifest["events"]["MOVE"] = {}
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "invalid_semantic_type"
    else:
        raise AssertionError("semantic events require a namespaced type")

    manifest = _manifest()
    manifest["events"]["game.move_committed"]["participantOpportunity"] = "false"
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "invalid_participant_opportunity"
    else:
        raise AssertionError("automatic participant scheduling requires a real boolean")


def test_event_flags_are_booleans_and_terminal_cannot_schedule_a_decision() -> None:
    for field, code in (("beat", "invalid_beat"), ("terminal", "invalid_terminal")):
        manifest = _manifest()
        manifest["events"]["game.finished"][field] = "false"
        try:
            parse_manifest(manifest)
        except AuipProtocolError as exc:
            assert exc.code == code
        else:
            raise AssertionError(f"{field} must reject truthy strings")

    manifest = _manifest()
    manifest["events"]["game.finished"]["participantOpportunity"] = True
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "terminal_participant_opportunity"
    else:
        raise AssertionError("a completed AppSession cannot schedule another decision")


def test_participant_opportunity_requires_participant_stance() -> None:
    manifest = _manifest()
    manifest["stances"] = ["spectator"]
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "participant_opportunity_requires_participant"
    else:
        raise AssertionError("spectator-only apps cannot assign Participant decisions")


def test_action_input_schema_keeps_the_public_mcp_tool_shape() -> None:
    manifest = _manifest()
    manifest["actions"]["game.place_stone"]["inputSchema"]["properties"]["move"] = {
        "type": "object",
        "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
    }
    parsed = parse_manifest(manifest)
    assert parsed.actions["game.place_stone"].input_schema["properties"]["move"][
        "type"
    ] == "object"

    manifest = _manifest()
    manifest["actions"]["game.place_stone"]["inputSchema"]["type"] = "array"
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "invalid_action_input_schema"
    else:
        raise AssertionError("MCP-compatible AUIP actions require object arguments")


def test_interaction_summary_is_bounded_background_not_a_domain_schema() -> None:
    manifest = _manifest()
    manifest["app"]["interactionSummary"] = "x" * 641
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "value_too_long"
        assert exc.detail == "app.interactionSummary"
    else:
        raise AssertionError("unbounded domain briefing must be rejected")


def test_manifest_declares_only_standard_situation_kinds() -> None:
    manifest = _manifest()
    manifest["situationKinds"] = ["grid/v1", "choice/v1", "choice/v1"]
    parsed = parse_manifest(manifest)
    assert parsed.situation_kinds == ("grid/v1", "choice/v1")
    assert parsed.to_dict()["situationKinds"] == ["grid/v1", "choice/v1"]

    manifest["situationKinds"] = ["gomoku-private/v1"]
    try:
        parse_manifest(manifest)
    except AuipProtocolError as exc:
        assert exc.code == "unsupported_situation_kind"
    else:
        raise AssertionError("participant projections must use the shared kind vocabulary")


def test_controller_manifest_binds_governance_to_exact_policy_actions() -> None:
    manifest = _manifest()
    manifest["events"]["game.move_committed"]["controllerEffect"] = True
    manifest["situationKinds"] = ["grid/v1", "controller/v1"]
    manifest["controller"] = {
        "policyActions": ["game.place_stone"],
        "leaseDurationMs": 30_000,
        "maxActionRateHz": 12,
        "takeover": "safe_point",
    }
    parsed = parse_manifest(manifest)
    assert parsed.controller is not None
    assert parsed.controller.policy_actions == ("game.place_stone",)
    assert parsed.controller.lease_duration_ms == 30_000
    assert parsed.controller.max_action_rate_hz == 12
    assert parsed.controller.takeover == "safe_point"
    assert parsed.events["game.move_committed"].controller_effect is True
    assert (
        parsed.to_dict()["events"]["game.move_committed"]["controllerEffect"]
        is True
    )
    assert parsed.to_dict()["controller"] == manifest["controller"]


def test_controller_manifest_rejects_missing_or_open_policy_contracts() -> None:
    def controller_manifest() -> dict:
        manifest = _manifest()
        manifest["situationKinds"] = ["controller/v1"]
        manifest["controller"] = {
            "policyActions": ["game.place_stone"],
            "leaseDurationMs": 30_000,
            "maxActionRateHz": 12,
            "takeover": "immediate",
        }
        return manifest

    missing_kind = controller_manifest()
    missing_kind["situationKinds"] = ["grid/v1"]
    try:
        parse_manifest(missing_kind)
    except AuipProtocolError as exc:
        assert exc.code == "controller_situation_required"
    else:
        raise AssertionError("Controller governance must be visible in state")

    open_schema = controller_manifest()
    open_schema["actions"]["game.place_stone"]["inputSchema"].pop(
        "additionalProperties"
    )
    try:
        parse_manifest(open_schema)
    except AuipProtocolError as exc:
        assert exc.code == "controller_policy_schema_open"
    else:
        raise AssertionError("Controller policy payloads require exact object schemas")

    unknown_action = controller_manifest()
    unknown_action["controller"]["policyActions"] = ["game.set_policy"]
    try:
        parse_manifest(unknown_action)
    except AuipProtocolError as exc:
        assert exc.code == "unknown_controller_policy_action"
    else:
        raise AssertionError("Controller policy actions must use the action catalog")

    no_controller = _manifest()
    no_controller["events"]["game.move_committed"]["controllerEffect"] = True
    try:
        parse_manifest(no_controller)
    except AuipProtocolError as exc:
        assert exc.code == "controller_effect_requires_controller"
    else:
        raise AssertionError("Controller effects require Host lease governance")

    invalid_marker = controller_manifest()
    invalid_marker["events"]["game.move_committed"]["controllerEffect"] = "yes"
    try:
        parse_manifest(invalid_marker)
    except AuipProtocolError as exc:
        assert exc.code == "invalid_controller_effect"
    else:
        raise AssertionError("Controller effect classification must be boolean")


def _main() -> None:
    test_manifest_is_small_and_machine_verified()
    test_manifest_rejects_unknown_authority_and_untyped_names()
    test_action_input_schema_keeps_the_public_mcp_tool_shape()
    test_manifest_declares_only_standard_situation_kinds()
    test_controller_manifest_binds_governance_to_exact_policy_actions()
    test_controller_manifest_rejects_missing_or_open_policy_contracts()
    print("ok: AUIP manifest is a narrow machine-verified contract")


if __name__ == "__main__":
    _main()
