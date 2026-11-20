"""Static contract checks for the first AUIP-authored game.

The environment-dependent browser journey lives under ``tools/``.  These
checks belong in the ordinary test matrix because they validate the authored
artifact itself without requiring Chromium to be installed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from server.auip_contract import parse_manifest


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "auip-gomoku"


def test_gomoku_manifest_is_valid_and_matches_the_embedded_manifest() -> None:
    manifest = json.loads((SAMPLE / "auip.manifest.json").read_text(encoding="utf-8"))
    html = (SAMPLE / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script id="auip-manifest" type="application/json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    embedded = json.loads(match.group(1))

    assert embedded == manifest
    parsed = parse_manifest(manifest)
    assert parsed.app_id == "gomoku-nine"
    assert parsed.objective.startswith("Create five consecutive stones")
    assert parsed.events["game.move_committed"].participant_opportunity is False
    assert parsed.events["game.participant_turn_ready"].participant_opportunity is True
    assert parsed.events["game.round_finished"].terminal is False
    assert parsed.events["game.round_finished"].participant_opportunity is False
    assert parsed.events["game.experience_finished"].terminal is True
    assert parsed.actions["game.place_stone"].risk == "local_execution"
    assert parsed.actions["game.place_stone"].input_schema["required"] == ["x", "y"]
    assert "state.board.rows[payload.y][payload.x]" in parsed.actions[
        "game.place_stone"
    ].description
    assert [
        condition.kind
        for condition in parsed.actions["game.place_stone"].preconditions
    ] == ["action_available/v1", "grid_cell_empty/v1"]
    assert parsed.actions["game.take_first_move"].input_schema["required"] == [
        "x",
        "y",
    ]
    assert "Atomically bind the participant to black" in parsed.actions[
        "game.take_first_move"
    ].description
    assert parsed.actions["game.configure_participants"].input_schema["required"] == [
        "participantSide"
    ]
    assert {"game.resign", "game.restart_round", "game.finish_experience"} <= set(
        parsed.actions
    )
    assert parsed.situation_kinds == (
        "action_availability/v1",
        "grid/v1",
        "choice/v1",
    )
    assert parsed.stances == ("spectator", "participant")


def test_gomoku_uses_the_official_sdk_and_keeps_app_mechanics_local() -> None:
    html = (SAMPLE / "index.html").read_text(encoding="utf-8")
    game = (SAMPLE / "game.js").read_text(encoding="utf-8")

    assert '../../sdk/auip-core/managed-v0.js' in html
    assert '../../sdk/auip-core/situations-v0.js' in html
    assert '../../sdk/auip-web/auip-v0.js' in html
    assert 'src="./game.js"' in html
    assert "AmadeusAUIP.createManagedApp" in game
    assert "AmadeusAUIPSituations.gridSituation" in game
    assert "AmadeusAUIPSituations.actionAvailabilitySituation" in game
    assert '"game.configure_participants": (payload, tx)' in game
    assert '"game.take_first_move": (payload, tx)' in game
    assert '"game.place_stone": (payload, tx)' in game
    assert '"game.resign": (_payload, tx)' in game
    assert '"game.restart_round": (_payload, tx)' in game
    assert '"game.finish_experience": (_payload, tx)' in game
    assert "payload.participantSide" in game
    assert "payload.x" in game and "payload.y" in game
    assert "turn !== participantSide" in game
    assert "attached && turn !== userSide" in game
    assert "roleBindings" in game
    assert "participant_resigned" in game
    assert "lifecycle === \"playing\"" in game
    assert "lifecycle === \"round_finished\"" in game
    assert 'tx.reject("round is not active", "round_not_active")' in game
    assert '"first_move_not_available"' in game
    assert '"round_not_finished"' in game
    assert "tx.commit" in game
    assert "commitLocal" in game
    assert "expected_revision" not in game
    assert "publishState" not in game
    # The cooperative app declares facts and actions.  It does not call the
    # character, provider, or speech surfaces directly.
    assert "speechSynthesis" not in game
    assert "provider.run" not in game
    assert "chat.send" not in game


if __name__ == "__main__":
    test_gomoku_manifest_is_valid_and_matches_the_embedded_manifest()
    test_gomoku_uses_the_official_sdk_and_keeps_app_mechanics_local()
    print("ok: AUIP Gomoku sample satisfies the authored-app contract")
