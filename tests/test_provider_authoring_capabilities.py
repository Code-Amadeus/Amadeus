from pathlib import Path
import json
import subprocess
import sys
import tempfile

from agent_host.provider_authoring import (
    auip_authoring_bundle_metrics,
    materialize_auip_runtime_assets,
    requires_auip_authoring,
    stage_auip_authoring_bundle,
    with_host_authoring_capabilities,
)


ROOT = Path(__file__).resolve().parents[1]
STAGED_SKILL = ROOT / ".amadeus-test" / "skills" / "auip-authoring" / "SKILL.md"
AUTHORING_SKILL = ROOT / "skills" / "auip-authoring" / "SKILL.md"
AUTHORING_INTERFACE = (
    ROOT / "skills" / "auip-authoring" / "references" / "interface-v0.md"
)
AUTHORING_CONTROLLER = (
    ROOT / "skills" / "auip-authoring" / "references" / "controller-v0.md"
)


def test_optional_authoring_capability_leaves_relevance_to_the_agent() -> None:
    prompt = with_host_authoring_capabilities(
        "Build a local game.",
        source_user_text="把它改成能和你一起玩。",
        source_user_context="刚才做了一个本地小游戏。",
        authoring_skill_path=str(STAGED_SKILL),
    )

    assert prompt.startswith("Build a local game.")
    assert str(STAGED_SKILL) in prompt
    assert "If and only if" in prompt
    assert "ordinary sites" in prompt
    assert "intent=" not in prompt
    assert '"把它改成能和你一起玩。"' in prompt
    assert "bounded intent evidence" in prompt
    assert "semantic authority" in prompt
    assert "role-authored task is only an execution brief" in prompt
    assert "not a different user-visible actor" in prompt
    assert "second-person references" not in prompt
    assert "An in-app bot or scripted auto-player does not satisfy" in prompt
    assert "bounded reference context" in prompt
    assert '"刚才做了一个本地小游戏。"' in prompt


def test_adjudicated_preparation_is_a_required_provider_prerequisite() -> None:
    prompt = with_host_authoring_capabilities(
        "guess_number_game.html を開く",
        source_user_text=(
            "直接打开刚才那个小游戏吧，你在旁边看着我玩，顺便评论一下。"
        ),
        require_auip_preparation=True,
        authoring_skill_path=str(STAGED_SKILL),
    )

    assert prompt.startswith("Host-authorized AUIP application prerequisite:")
    assert str(STAGED_SKILL) in prompt
    assert "one AUIP-capable delivery" in prompt
    assert "Never create a second app merely to add AUIP" in prompt
    assert "create a valid AUIP manifest" in prompt
    assert "Host owns product launch" in prompt
    assert "Do not launch the user-facing app" in prompt
    assert "bounded author-owned boot test" in prompt
    assert "Web-binding\ncapture stub is not sufficient" in prompt
    assert "primary interactive loop" in prompt
    assert "Reactive Controller or remain spectator-only" in prompt
    assert "stable complete `actionTypes`" in prompt
    assert "compact interface" in prompt
    assert "opaque dependencies" in prompt
    assert "validate_auip_manifest.py" in prompt
    assert "sync_auip_manifest.py" in prompt
    assert "Do not open or inspect either implementation" in prompt
    assert "guess_number_game.html を開く" in prompt
    assert '"直接打开刚才那个小游戏吧，你在旁边看着我玩，顺便评论一下。"' in prompt
    assert "Optional host authoring capability" not in prompt


def test_unstaged_optional_capability_is_not_advertised() -> None:
    assert with_host_authoring_capabilities("Build a report.") == "Build a report."


def test_only_host_derived_auip_sources_require_authoring() -> None:
    assert requires_auip_authoring("auip_prepare") is True
    assert requires_auip_authoring("auip_create") is True
    assert requires_auip_authoring("llm_delegate") is False
    assert requires_auip_authoring("arbitrary") is False


def test_authoring_contract_separates_decision_participant_from_reactive_control() -> None:
    skill = AUTHORING_SKILL.read_text(encoding="utf-8")
    interface = AUTHORING_INTERFACE.read_text(encoding="utf-8")
    controller = AUTHORING_CONTROLLER.read_text(encoding="utf-8")

    for text in (skill, interface):
        assert "low-frequency transaction model" in text
        assert "Controller" in text
        assert "spectator-only" in text
        assert "response horizon" in text
        assert "universal" in text
    assert "cannot change action selection, ordinary legality" in interface
    assert "may not drift\nprivately behind an unchanged AUIP revision" in interface
    assert "thin application-local rule system or AI" in interface
    assert "exact policy" in interface
    assert "lifetime of effects" in skill
    assert "lifetime of effects" in interface
    assert "one exact atomic payload" in interface
    assert "latest telemetry revision" in controller
    assert "latest data-plane revision" in interface
    assert "Ordinary Decision" in controller
    assert "receive no exception" in controller
    assert "whitelist is scoped by action type" in interface
    assert "synthetic active governance status" in controller
    assert "rejects an adapter that discards it" in controller
    for text in (skill, interface):
        normalized = " ".join(text.split())
        assert "core participation outcome" in normalized
        assert "lifecycle" in normalized
        assert "ordinary failure" in normalized
        assert "human input" in normalized
    assert "primary interactive loop" in skill
    assert "player actuators remain idle" in interface
    assert "actionTypes" in skill
    assert "stable complete set" in interface
    assert "governed action with no current option is unavailable" in interface.lower()
    assert "validates the first\nsnapshot synchronously" in interface
    assert "capture stub" in skill
    assert "initial render and primary\n  input binding" in skill
    assert 'importance:"important"' in skill


def test_authoring_skill_requests_truthful_intermediate_milestones() -> None:
    skill = AUTHORING_SKILL.read_text(encoding="utf-8")

    assert "chosen AUIP shape" in skill
    assert "integration underway" in skill
    assert "validation underway" in skill
    assert "progress line is intermediate" in skill
    assert "continue the same turn" in skill
    assert "first shape DESIGN" in skill


def test_authoring_budget_cannot_change_business_action_granularity() -> None:
    skill = AUTHORING_SKILL.read_text(encoding="utf-8")
    interface = AUTHORING_INTERFACE.read_text(encoding="utf-8")
    corpus = " ".join((skill + "\n" + interface).split())

    assert "independently meaningful source transition" in corpus
    assert "solve`/`apply_plan` macro" in corpus
    assert "projection target is advisory" in corpus
    assert "stable option family with `available:false`" in corpus


def test_required_preparation_rejects_an_unstaged_contract() -> None:
    try:
        with_host_authoring_capabilities(
            "Prepare the game.",
            require_auip_preparation=True,
        )
    except ValueError as exc:
        assert "workspace-local" in str(exc)
    else:
        raise AssertionError("required AUIP preparation must not receive a host path")


def test_authoring_bundle_preserves_the_skill_relative_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_authoring_bundle_") as temp:
        skill_path = stage_auip_authoring_bundle(Path(temp) / "bundle")
        bundle_root = skill_path.parents[2]

        assert skill_path.is_file()
        assert not (skill_path.parent / "references" / "protocol-v0.md").exists()
        assert (skill_path.parent / "references" / "interface-v0.md").is_file()
        assert (skill_path.parent / "references" / "controller-v0.md").is_file()
        assert (skill_path.parent / "assets" / "auip.manifest.json").is_file()
        assert (bundle_root / "sdk" / "auip-core" / "managed-v0.js").is_file()
        assert (bundle_root / "sdk" / "auip-core" / "controller-v0.js").is_file()
        assert (bundle_root / "sdk" / "auip-core" / "situations-v0.js").is_file()
        assert not (bundle_root / "sdk" / "auip-core" / "README.md").exists()
        assert (bundle_root / "sdk" / "auip-web" / "auip-v0.js").is_file()
        assert (bundle_root / "server" / "auip_contract.py").is_file()
        assert (bundle_root / "tools" / "validate_auip_manifest.py").is_file()
        assert (bundle_root / "tools" / "sync_auip_manifest.py").is_file()

        completed = subprocess.run(
            [
                sys.executable,
                str(bundle_root / "tools" / "validate_auip_manifest.py"),
                str(skill_path.parent / "assets" / "auip.manifest.json"),
            ],
            cwd=bundle_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert "ok: AUIP v0 manifest" in completed.stdout


def test_host_materializes_opaque_runtime_assets_without_provider_copying() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_runtime_assets_") as temp:
        assets = materialize_auip_runtime_assets(Path(temp))
        assert set(assets) == {
            "sdk/auip-core/managed-v0.js",
            "sdk/auip-core/controller-v0.js",
            "sdk/auip-core/situations-v0.js",
            "sdk/auip-web/auip-v0.js",
        }
        for relative_name, identity in assets.items():
            target = Path(identity["path"])
            assert target == Path(temp).resolve() / Path(relative_name)
            assert target.is_file()
            assert len(identity["sha256"]) == 64


def test_workspace_materialization_never_overwrites_a_conflicting_runtime() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_runtime_conflict_") as temp:
        root = Path(temp)
        target = root / "sdk" / "auip-core" / "managed-v0.js"
        target.parent.mkdir(parents=True)
        target.write_text("user-owned different bytes", encoding="utf-8")

        try:
            materialize_auip_runtime_assets(root, replace_existing=False)
        except OSError as exc:
            assert "conflicting AUIP runtime asset target" in str(exc)
        else:
            raise AssertionError("a caller-owned runtime conflict must fail closed")

        assert target.read_text(encoding="utf-8") == "user-owned different bytes"


def test_host_managed_authoring_bundle_omits_opaque_implementation_sources() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_lean_authoring_bundle_") as temp:
        skill_path = stage_auip_authoring_bundle(
            Path(temp),
            include_opaque_dependencies=False,
        )
        bundle_root = skill_path.parents[2]
        assert skill_path.is_file()
        assert (skill_path.parent / "references" / "interface-v0.md").is_file()
        assert (skill_path.parent / "references" / "controller-v0.md").is_file()
        assert not (skill_path.parent / "references" / "protocol-v0.md").exists()
        assert not (bundle_root / "sdk" / "auip-core" / "managed-v0.js").exists()
        assert not (bundle_root / "sdk" / "auip-web" / "auip-v0.js").exists()
        assert (bundle_root / "server" / "auip_contract.py").is_file()
        validator = bundle_root / "tools" / "validate_auip_manifest.py"
        assert validator.is_file()
        sync = bundle_root / "tools" / "sync_auip_manifest.py"
        assert sync.is_file()
        metrics = auip_authoring_bundle_metrics(skill_path)
        assert metrics["staged_file_count"] == 8
        assert metrics["required_read_file_count"] == 2
        assert metrics["required_read_bytes"] < metrics["staged_bytes"]
        assert metrics["required_read_bytes"] < 40_000
        completed = subprocess.run(
            [
                sys.executable,
                str(validator),
                str(skill_path.parent / "assets" / "auip.manifest.json"),
            ],
            cwd=bundle_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        participant_manifest = Path(temp) / "participant-without-situation.json"
        participant_manifest.write_text(
            json.dumps(
                {
                    "schema": "amadeus.auip/v0",
                    "app": {"id": "test-app", "title": "Test app"},
                    "events": {"app.ready": {"beat": True}},
                    "actions": {},
                        "stances": ["spectator", "participant"],
                }
            ),
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [sys.executable, str(validator), str(participant_manifest)],
            cwd=bundle_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert rejected.returncode == 2
        assert "missing_situation_kinds" in rejected.stderr
        entry = Path(temp) / "entry.html"
        entry.write_text(
            '<script id="auip-manifest" type="application/json">{}</script>',
            encoding="utf-8",
        )
        synced = subprocess.run(
            [
                sys.executable,
                str(sync),
                str(skill_path.parent / "assets" / "auip.manifest.json"),
                str(entry),
            ],
            cwd=bundle_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert synced.returncode == 0, synced.stderr


def test_authoring_read_budget_is_independent_of_checkout_line_endings() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_authoring_line_endings_") as temp:
        skill_path = stage_auip_authoring_bundle(
            Path(temp),
            include_opaque_dependencies=False,
        )
        interface_path = skill_path.parent / "references" / "interface-v0.md"

        for path in (skill_path, interface_path):
            canonical = (
                path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            )
            path.write_bytes(canonical)

        baseline = auip_authoring_bundle_metrics(skill_path)

        for path in (skill_path, interface_path):
            canonical = path.read_bytes()
            path.write_bytes(canonical.replace(b"\n", b"\r\n"))

        windows_checkout = auip_authoring_bundle_metrics(skill_path)

        assert windows_checkout["staged_bytes"] > baseline["staged_bytes"]
        assert (
            windows_checkout["required_read_bytes"]
            == baseline["required_read_bytes"]
        )


if __name__ == "__main__":
    test_optional_authoring_capability_leaves_relevance_to_the_agent()
    test_adjudicated_preparation_is_a_required_provider_prerequisite()
    test_unstaged_optional_capability_is_not_advertised()
    test_required_preparation_rejects_an_unstaged_contract()
    test_authoring_bundle_preserves_the_skill_relative_layout()
    print("ok: code Providers see only workspace-local AUIP authoring inputs")
