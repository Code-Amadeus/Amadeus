from __future__ import annotations

import json
import tempfile
from pathlib import Path

from server.auip_contract import AuipProtocolError
from tools.sync_auip_manifest import sync_manifest
from tools.validate_auip_manifest import main, validate_file


def _manifest() -> dict:
    return {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "shared-game",
            "title": "Shared Game",
            "version": "0.1.0",
            "interactionSummary": (
                "Operate one declared app action. Examples: 'do the next step' "
                "maps to the current action; 'show me' remains observation."
            ),
        },
        "events": {"game.ready": {"beat": True}},
        "actions": {},
        "stances": ["spectator"],
    }


def test_manifest_validator_has_a_stable_cli_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-manifest-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(_manifest()), encoding="utf-8")
        assert validate_file(path)["app"]["id"] == "shared-game"
        assert main([str(path)]) == 0
        path.write_text("{}", encoding="utf-8")
        assert main([str(path)]) == 2


def test_manifest_preserves_a_bounded_static_objective() -> None:
    manifest = _manifest()
    manifest["app"]["objective"] = "Route every signal to one available destination."
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-objective-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        canonical = validate_file(path)
        assert canonical["app"]["objective"] == manifest["app"]["objective"]

        manifest["app"]["objective"] = "x" * 241
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "value_too_long"
        else:
            raise AssertionError("an unbounded objective must be rejected")


def test_grid_precondition_links_standard_state_to_required_integer_payload() -> None:
    manifest = _manifest()
    manifest["stances"] = ["spectator", "participant"]
    manifest["situationKinds"] = ["grid/v1"]
    manifest["actions"] = {
        "game.place": {
            "description": "Place at an empty coordinate in state.board.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["x", "y"],
                "additionalProperties": False,
            },
            "preconditions": [
                {
                    "kind": "grid_cell_empty/v1",
                    "statePath": "board",
                    "xField": "x",
                    "yField": "y",
                }
            ],
        }
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-grid-condition-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        canonical = validate_file(path)
        assert canonical["actions"]["game.place"]["preconditions"] == (
            manifest["actions"]["game.place"]["preconditions"]
        )

        manifest["situationKinds"] = ["sequence/v1"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "grid_precondition_requires_grid_situation"
        else:
            raise AssertionError("grid precondition must name a published grid/v1")

        manifest["situationKinds"] = ["grid/v1"]
        manifest["actions"]["game.place"]["inputSchema"]["required"] = ["x"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "grid_precondition_requires_integer_coordinate"
        else:
            raise AssertionError("precondition coordinates must be required integers")


def test_action_precondition_requires_declared_availability_situation() -> None:
    manifest = _manifest()
    manifest["stances"] = ["spectator", "participant"]
    manifest["situationKinds"] = ["action_availability/v1", "choice/v1"]
    manifest["actions"] = {
        "game.advance": {
            "description": "Advance only while the app publishes this action as available.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "preconditions": [
                {
                    "kind": "action_available/v1",
                    "statePath": "actionAvailability",
                }
            ],
        }
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-action-condition-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert validate_file(path)["actions"]["game.advance"]["preconditions"]

        manifest["situationKinds"] = ["choice/v1"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "action_precondition_requires_availability_situation"
        else:
            raise AssertionError(
                "action availability preconditions need a declared standard situation"
            )


def test_authored_participant_preserves_lower_authority_observe_mode() -> None:
    manifest = _manifest()
    manifest["stances"] = ["participant"]
    manifest["situationKinds"] = ["sequence/v1"]
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-stances-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "participant_requires_spectator"
        else:
            raise AssertionError("participant authoring must retain spectator mode")

        manifest["stances"] = ["spectator", "participant"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "missing_participant_actions"
        else:
            raise AssertionError("participant stance without an action is unusable")

        manifest["actions"] = {
            "game.advance": {
                "description": "Advance one real application step.",
                "risk": "local_execution",
            }
        }
        manifest["situationKinds"] = ["choice/v1"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert validate_file(path)["stances"] == ["spectator", "participant"]

        del manifest["app"]["interactionSummary"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "missing_interaction_summary"
        else:
            raise AssertionError("participant authoring requires a domain briefing")


def test_interaction_summary_references_one_declared_public_action_per_example() -> None:
    manifest = _manifest()
    manifest["stances"] = ["spectator", "participant"]
    manifest["situationKinds"] = ["choice/v1"]
    manifest["actions"] = {
        "game.advance": {
            "description": "Advance one real application step.",
            "risk": "local_execution",
        },
        "game.reset": {
            "description": "Reset the current application state.",
            "risk": "local_execution",
        },
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-summary-") as temp:
        path = Path(temp) / "auip.manifest.json"

        manifest["app"]["interactionSummary"] = (
            "Examples: 'advance' maps to game.advance; 'reset' maps to game.reset."
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert set(validate_file(path)["actions"]) == {"game.advance", "game.reset"}

        manifest["app"]["interactionSummary"] = (
            "Example: 'fly' maps to game.fly."
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "interaction_summary_unknown_action"
            assert exc.detail == "game.fly"
        else:
            raise AssertionError("summary cannot advertise an undeclared action")

        manifest["app"]["interactionSummary"] = (
            "Example: 'advance and reset' maps to game.advance + game.reset."
        )
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "interaction_summary_multi_action_example"
        else:
            raise AssertionError("one summary example cannot promise two receipts")


def test_terminal_event_cannot_advertise_a_post_terminal_decision() -> None:
    manifest = _manifest()
    manifest["events"] = {
        "game.experience_finished": {
            "beat": True,
            "terminal": True,
            "participantOpportunity": True,
        }
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-lifecycle-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "terminal_participant_opportunity"
        else:
            raise AssertionError("terminal events cannot assign impossible later work")


def test_participant_small_action_spaces_require_closed_choice_projection() -> None:
    manifest = _manifest()
    manifest["stances"] = ["spectator", "participant"]
    manifest["situationKinds"] = ["scalars/v1"]
    manifest["actions"] = {
        "game.connect": {
            "description": "Connect one source to one channel.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["A", "B", "C"]},
                    "channel": {
                        "type": "string",
                        "enum": ["red", "green", "blue"],
                    },
                },
                "required": ["source", "channel"],
                "additionalProperties": False,
            },
        },
        "game.reset": {
            "description": "Reset.",
            "risk": "none",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-small-choice-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "small_action_space_requires_choice"
            assert "size 10" in exc.detail
            assert "exact payload" in exc.detail
            assert "whole-solution macro" in exc.detail
        else:
            raise AssertionError("a finite small action space must publish choice/v1")

        manifest["situationKinds"].append("choice/v1")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert "choice/v1" in validate_file(path)["situationKinds"]

        del manifest["actions"]["game.connect"]["inputSchema"][
            "additionalProperties"
        ]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "open_action_input_schema"
        else:
            raise AssertionError("an open action schema is not an exact payload contract")

        connect = manifest["actions"]["game.connect"]["inputSchema"]
        connect["additionalProperties"] = False
        connect["properties"]["source"] = {
            "type": "string",
            "pattern": "^(A|B|C)$",
        }
        connect["properties"]["channel"] = {
            "type": "string",
            "pattern": "^(?:red|green|blue)$",
        }
        manifest["situationKinds"] = ["scalars/v1"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "small_action_space_requires_choice"
        else:
            raise AssertionError("literal regex alternations remain a finite action space")


def test_authored_controller_requires_a_lease_correlated_effect_event() -> None:
    manifest = _manifest()
    manifest["app"]["interactionSummary"] = (
        "Example: 'stay safe' maps to game.set_policy."
    )
    manifest["stances"] = ["spectator", "participant"]
    manifest["situationKinds"] = ["choice/v1", "controller/v1"]
    manifest["actions"] = {
        "game.set_policy": {
            "description": "Set one sustained local policy.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["safe"]},
                },
                "required": ["mode"],
                "additionalProperties": False,
            },
        }
    }
    manifest["controller"] = {
        "policyActions": ["game.set_policy"],
        "leaseDurationMs": 30000,
        "maxActionRateHz": 10,
        "takeover": "immediate",
    }
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-controller-effect-") as temp:
        path = Path(temp) / "auip.manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            validate_file(path)
        except AuipProtocolError as exc:
            assert exc.code == "controller_requires_effect_event"
        else:
            raise AssertionError("a Controller without an observable app effect is incomplete")

        manifest["events"]["game.controller_effect"] = {
            "beat": False,
            "controllerEffect": True,
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert validate_file(path)["events"]["game.controller_effect"][
            "controllerEffect"
        ] is True


def test_embedded_manifest_is_generated_from_the_host_verified_source() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-manifest-sync-") as temp:
        root = Path(temp)
        manifest_path = root / "auip.manifest.json"
        entry_path = root / "index.html"
        manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
        entry_path.write_text(
            '<script id="auip-manifest" type="application/json">\n  {}\n</script>',
            encoding="utf-8",
        )
        sync_manifest(manifest_path, entry_path)
        assert sync_manifest(manifest_path, entry_path, check=True)["app"]["id"] == "shared-game"
        manifest = _manifest()
        manifest["app"]["title"] = "Changed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            sync_manifest(manifest_path, entry_path, check=True)
        except AuipProtocolError as exc:
            assert exc.code == "embedded_manifest_out_of_sync"
        else:
            raise AssertionError("check mode must detect a stale embedded manifest")


def test_embedded_manifest_slot_accepts_equivalent_inline_formatting() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-auip-inline-sync-") as temp:
        root = Path(temp)
        manifest_path = root / "auip.manifest.json"
        entry_path = root / "index.html"
        manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
        entry_path.write_text(
            '<script id="auip-manifest" type="application/json">{}</script>',
            encoding="utf-8",
        )
        sync_manifest(manifest_path, entry_path)
        assert sync_manifest(manifest_path, entry_path, check=True)["app"]["id"] == "shared-game"


def _main() -> None:
    test_manifest_validator_has_a_stable_cli_contract()
    test_embedded_manifest_is_generated_from_the_host_verified_source()
    print("ok: AUIP authoring has a deterministic manifest validator")


if __name__ == "__main__":
    _main()
