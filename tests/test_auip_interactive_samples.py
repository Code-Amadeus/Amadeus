"""Contract checks for AUIP samples outside alternating board games."""

from __future__ import annotations

import json
import re
from pathlib import Path

from server.auip_contract import parse_manifest


ROOT = Path(__file__).resolve().parents[1]


def _sample(name: str) -> tuple[dict[str, object], str, str]:
    directory = ROOT / "examples" / name
    manifest = json.loads((directory / "auip.manifest.json").read_text(encoding="utf-8"))
    html = (directory / "index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script id="auip-manifest" type="application/json">\s*(.*?)\s*</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    assert json.loads(match.group(1)) == manifest
    script_name = "game.js" if name == "auip-2048" else "simulation.js"
    script = (directory / script_name).read_text(encoding="utf-8")
    return manifest, html, script


def test_2048_declares_non_alternating_semantic_slides() -> None:
    manifest, html, script = _sample("auip-2048")
    parsed = parse_manifest(manifest)

    assert parsed.app_id == "merge-2048"
    assert parsed.objective.startswith("Combine equal tiles")
    assert "game.slide" in parsed.interaction_summary
    assert parsed.situation_kinds == ("grid/v1", "choice/v1")
    assert set(parsed.actions) == {"game.slide"}
    assert parsed.events["game.finished"].terminal is True
    assert "turn" not in json.dumps(manifest)
    assert '../../sdk/auip-core/managed-v0.js' in html
    assert '../../sdk/auip-core/situations-v0.js' in html
    assert '../../sdk/auip-web/auip-v0.js' in html
    assert "AmadeusAUIP.createManagedApp" in script
    assert '"game.slide": (payload, tx)' in script
    assert "payload.direction" in script
    assert "AmadeusAUIPSituations.gridSituation" in script
    assert "AmadeusAUIPSituations.choiceSituation" in script
    assert "available: legal.has(direction)" in script
    assert "expected_revision" not in script
    assert "publishState" not in script
    assert "speechSynthesis" not in script
    assert "provider.run" not in script


def test_reactor_declares_sparse_control_over_a_continuous_clock() -> None:
    manifest, html, script = _sample("auip-reactor")
    parsed = parse_manifest(manifest)

    assert parsed.app_id == "reactor-drift"
    assert "reactor.set_cooling" in parsed.interaction_summary
    assert parsed.situation_kinds == ("scalars/v1", "choice/v1")
    assert set(parsed.actions) == {"reactor.set_cooling"}
    assert parsed.events["reactor.heat_warning"].importance == "important"
    assert parsed.events["reactor.stabilized"].terminal is True
    assert '../../sdk/auip-core/managed-v0.js' in html
    assert '../../sdk/auip-core/situations-v0.js' in html
    assert '../../sdk/auip-web/auip-v0.js' in html
    assert "AmadeusAUIP.createManagedApp" in script
    assert '"reactor.set_cooling": (payload, tx)' in script
    assert "AmadeusAUIPSituations.scalarSituation" in script
    assert "AmadeusAUIPSituations.choiceSituation" in script
    assert "payload.level" in script
    assert "auip.checkpoint" in script
    assert "window.setInterval(tick, TICK_MS)" in script
    assert "tickCount % CHECKPOINT_TICKS" in script
    assert "awaitingControl" in script
    assert "no stable control window is open" in script
    assert "semanticEventCount" in script
    assert "speechSynthesis" not in script
    assert "chat.send" not in script


def test_bullet_hell_separates_smooth_local_control_from_sparse_auip_facts() -> None:
    manifest, html, script = _sample("auip-bullet-hell")
    parsed = parse_manifest(manifest)

    assert parsed.controller is not None
    assert parsed.situation_kinds == ("choice/v1", "controller/v1")
    assert "one sustained tactic" in parsed.interaction_summary
    assert '../../sdk/auip-core/controller-v0.js' in html
    assert "const CONTROL_TICK_MS = 100" in script
    assert "requestAnimationFrame(animate)" in script
    assert 'data-testid="player"' in html
    assert "controlIntent" in script
    assert "field: semanticField()" in script
    assert 'enemyPressure: countBand(' in script
    assert 'projectilePressure: pressureBand(' in script
    assert 'rewardOpportunity: countBand(' in script
    shared_field = script.split("function semanticField", 1)[1].split(
        "function snapshot", 1
    )[0]
    assert "formationGap" not in shared_field
    assert "playerDistance" not in shared_field
    assert "playerDistance" not in script
    assert "player-distance" not in html
    assert 'follow: "player_following_started"' in script
    assert "CHECKPOINT_INTERVAL_MS" in script
    assert "checkpointIfDue" in script
    assert "never every frame" not in script
    assert "setInterval(() =>" in script
    assert "}, CONTROL_TICK_MS)" in script
    shared_snapshot = script.split("function snapshot", 1)[1].split(
        "function renderMotion", 1
    )[0]
    assert "state.enemies" not in shared_snapshot
    assert "state.bullets" not in shared_snapshot
    assert "state.rewards" not in shared_snapshot


def test_v0_shared_state_is_not_a_hidden_information_channel() -> None:
    reference = (ROOT / "skills" / "auip-authoring" / "references" / "protocol-v0.md").read_text(
        encoding="utf-8"
    )
    assert "shared projection" in reference
    assert "hidden-information" in reference


def test_authoring_contract_separates_participant_identity_from_app_role() -> None:
    skill = (ROOT / "skills" / "auip-authoring" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    reference = (
        ROOT / "skills" / "auip-authoring" / "references" / "protocol-v0.md"
    ).read_text(encoding="utf-8")
    controller = (
        ROOT / "skills" / "auip-authoring" / "references" / "controller-v0.md"
    ).read_text(encoding="utf-8")
    corpus = "\n".join((skill, interface := (
        ROOT / "skills" / "auip-authoring" / "references" / "interface-v0.md"
    ).read_text(encoding="utf-8"), controller, reference))

    # The entrypoint retains shared decisions while exact Controller ABI and
    # causal tests are progressively disclosed only to Controller authoring.
    assert "references/controller-v0.md" in skill
    assert "participant identity" in skill
    assert "application role" in skill
    assert "typed configuration action" in skill
    assert "local input" in skill
    assert "explicit application transition" in skill
    assert "exact MCP-compatible object JSON Schema" in skill
    assert "original mechanics" in corpus
    assert "ordinary failure condition" in corpus
    assert "createReactiveController(controllerCallbacks)" in controller
    assert "do not invoke\ncallbacks directly" in controller
    assert "terminal:true" in skill
    assert "universal lifecycle enum" in skill
    assert "checkpointIfDue" in skill
    assert "120 stable render/physics frames" in controller
    assert "low-frequency policy" in controller
    assert "independently meaningful controls" in skill
    assert "One-shot displacement" in interface
    assert "movement/target/objective dimensions" in interface
    assert "separate\naccepted projection cache" in interface
    assert "state_changed_without_checkpoint" in skill
    assert "less than the background\ncheckpoint interval" in interface
    assert '<script src="sdk/auip-core/managed-v0.js"></script>' in interface
    assert '<script src="sdk/auip-web/auip-v0.js"></script>' in interface
    assert "managedModule.default || globalThis.AmadeusAUIPManaged" in interface
    assert (
        "controllerModule.default || globalThis.AmadeusAUIPController"
        in controller
    )
    assert "absent API must never be hidden by prose" in skill
    assert "one proposal boundary" in skill
    assert "actionA + actionB" in interface
    assert "settled alternative" in skill
    assert "Stable follow targets use app-owned identity and availability" in controller
    assert "nearest" in controller and "entity inference" in controller
    assert "target loss" in controller


if __name__ == "__main__":
    test_2048_declares_non_alternating_semantic_slides()
    test_reactor_declares_sparse_control_over_a_continuous_clock()
    test_v0_shared_state_is_not_a_hidden_information_channel()
    test_authoring_contract_separates_participant_identity_from_app_role()
    print("ok: AUIP diverse samples keep mechanics, timing, and visibility explicit")
