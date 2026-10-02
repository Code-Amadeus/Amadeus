from __future__ import annotations

import json
from pathlib import Path

import pytest

import render.spriteforge_animator as animator_module
from render.character_pack import (
    CHARACTER_PACK_FORMAT,
    CharacterPackError,
    character_pack_status,
    load_character_pack,
    validate_character_pack_graph,
)
from render.spriteforge_animator import SpriteForgeAnimator


class _EngineProbe:
    def __init__(self) -> None:
        self.loaded: dict[str, list[str]] = {}
        self.mouth: dict[str, dict] = {}
        self.mode_calls: list[str] = []
        self.graph: dict | None = None

    def load_sprite_frames(self, label: str, urls: list[str]) -> None:
        self.loaded[label] = urls

    def set_idle_frame_interval_ms(self, _label: str, _interval_ms: int) -> None:
        return

    def set_sprite_clip_config(self, _label: str, _config: dict) -> None:
        return

    def load_mouth_config(self, label: str, config: dict) -> None:
        self.mouth[label] = config

    def set_mode(self, mode: str) -> None:
        self.mode_calls.append(mode)

    def set_idle_animation(self, _enabled: bool) -> None:
        return

    def load_spriteforge_graph(self, payload: dict) -> None:
        self.graph = payload


def _write_pack(root: Path, *, omit_idle_frame: bool = False) -> None:
    (root / "textures" / "idle").mkdir(parents=True)
    (root / "textures" / "speaking_short").mkdir(parents=True)
    if not omit_idle_frame:
        (root / "textures" / "idle" / "0000.ktx2").write_bytes(b"idle")
    (root / "textures" / "speaking_short" / "0000.ktx2").write_bytes(b"speak")

    graph = {
        "nodes": [
            {"id": "idle", "label": "idle", "isRoot": True},
            {"id": "speak", "label": "speaking_short"},
        ],
        "edges": [{"id": "edge", "from": "idle", "to": "speak", "prob": 1}],
    }
    mouth = {
        "expressions": {
            "neutral": {"cx": 4, "cy": -196, "width": 34, "height": 18, "curve": 0.2}
        },
        "profiles": {
            "speaking_short": {
                "mouth_set": "neutral",
                "cx": 4,
                "cy": -196,
                "width": 34,
                "height": 18,
                "openness": [0.0],
                "anchor_track": [],
                "runtime_overlay_anchor": {"cx": 4, "cy": -196, "width": 34, "height": 18},
            }
        },
    }
    manifest = {
        "format": CHARACTER_PACK_FORMAT,
        "id": "kurisu-test",
        "displayName": "Kurisu Test",
        "version": "1",
        "textureFormat": "ktx2",
        "graph": "graph_config.json",
        "mouthConfig": "spriteforge_mouth_config.json",
        "clips": {
            "idle": {
                "phase": "loop",
                "frameIntervalMs": 42,
                "loopMode": "loop",
                "frames": ["textures/idle/0000.ktx2"],
            },
            "speaking_short": {
                "phase": "loop",
                "frameIntervalMs": 21,
                "loopMode": "loop",
                "frames": ["textures/speaking_short/0000.ktx2"],
            },
        },
        "mouthOverlays": {"speaking_short": ["textures/speaking_short/0000.ktx2"]},
        "frameCount": 2,
        "textureCount": 2,
        "textureBytes": 9,
    }
    (root / "graph_config.json").write_text(json.dumps(graph), encoding="utf-8")
    (root / "spriteforge_mouth_config.json").write_text(json.dumps(mouth), encoding="utf-8")
    (root / "runtime_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_character_pack_manifest_is_the_only_frame_index(tmp_path: Path) -> None:
    root = tmp_path / "kurisu"
    _write_pack(root)

    pack = load_character_pack(root)

    assert set(pack.clip_paths) == {"idle", "speaking_short"}
    assert all(path.suffix == ".ktx2" for paths in pack.clip_paths.values() for path in paths)
    assert not list(root.rglob("*.png"))


def test_incomplete_character_pack_is_reported_without_a_path_exception(tmp_path: Path) -> None:
    missing = character_pack_status(tmp_path / "missing")
    assert missing["installed"] is False
    assert missing["state"] == "not_installed"

    incomplete_root = tmp_path / "incomplete"
    _write_pack(incomplete_root, omit_idle_frame=True)
    incomplete = character_pack_status(incomplete_root)
    assert incomplete["installed"] is False
    assert incomplete["state"] == "invalid"
    assert incomplete["reason"] == "incomplete_pack"


def test_character_pack_rejects_paths_outside_the_runtime_boundary(tmp_path: Path) -> None:
    root = tmp_path / "kurisu"
    _write_pack(root)
    manifest_path = root / "runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clips"]["idle"]["frames"] = ["../outside.ktx2"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    status = character_pack_status(root)

    assert status["installed"] is False
    assert status["reason"] == "invalid_manifest"


def test_runtime_graph_contract_rejects_ambiguous_or_dangling_topology() -> None:
    valid = {
        "nodes": [
            {"id": "idle", "label": "idle", "isRoot": True},
            {"id": "variant", "label": "variant"},
        ],
        "edges": [
            {"id": "auto", "from": "idle", "to": "variant", "prob": 1.2},
            {"id": "manual", "from": "idle", "to": "idle", "prob": 0},
        ],
    }

    validate_character_pack_graph(valid)

    duplicate_id = json.loads(json.dumps(valid))
    duplicate_id["nodes"][1]["id"] = "idle"
    with pytest.raises(CharacterPackError, match="duplicate graph node id"):
        validate_character_pack_graph(duplicate_id)

    dangling = json.loads(json.dumps(valid))
    dangling["edges"][0]["to"] = "missing"
    with pytest.raises(CharacterPackError, match="references an unknown node"):
        validate_character_pack_graph(dangling)

    negative_weight = json.loads(json.dumps(valid))
    negative_weight["edges"][0]["prob"] = -0.1
    with pytest.raises(CharacterPackError, match="finite non-negative prob"):
        validate_character_pack_graph(negative_weight)


def test_minimal_public_character_pack_example_is_valid() -> None:
    root = Path(__file__).resolve().parents[1] / "examples" / "character-pack-minimal"

    pack = load_character_pack(root)

    assert pack.manifest["id"] == "minimal-example"
    assert len(pack.graph["nodes"]) == 3
    assert any(edge["prob"] == 0 for edge in pack.graph["edges"])


def test_animator_registers_ktx2_directly_from_manifest(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "kurisu"
    _write_pack(root)
    monkeypatch.setattr(animator_module, "SPRITEFORGE_RUNTIME_ROOT", root)
    engine = _EngineProbe()

    animator = SpriteForgeAnimator(engine)
    assert animator.start() is True

    assert engine.mode_calls == ["sprite"]
    assert set(engine.loaded) >= {"idle", "speaking_short"}
    assert all(url.lower().endswith(".ktx2") for urls in engine.loaded.values() for url in urls)
    assert engine.mouth["speaking_short"]["frameUrls"][0].lower().endswith(".ktx2")
    assert engine.graph is not None


def test_animator_is_optional_when_character_pack_is_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(animator_module, "SPRITEFORGE_RUNTIME_ROOT", tmp_path / "missing")
    engine = _EngineProbe()

    animator = SpriteForgeAnimator(engine)

    assert animator.start() is False
    assert animator.available is False
    assert engine.mode_calls == []
    assert engine.loaded == {}


def test_local_character_pack_contains_only_indexed_runtime_textures() -> None:
    status = character_pack_status()
    if not status["installed"]:
        pytest.skip("optional local character pack is not installed")

    pack = load_character_pack()
    indexed = {
        path.resolve()
        for paths in (*pack.clip_paths.values(), *pack.mouth_overlay_paths.values())
        for path in paths
    }
    present = {path.resolve() for path in pack.root.rglob("*.ktx2")}
    assert present == indexed
    assert not list(pack.root.rglob("*.png"))
