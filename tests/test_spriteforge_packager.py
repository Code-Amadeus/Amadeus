"""Regression tests for the SpriteForge character packager.

Guards the closed-mouth repair recorded in update ``2026.09.15-mouth-close.1``:
when a mouth profile explicitly selects a closed source, a missing KTX2 sidecar
must fail the build instead of silently substituting the profile's own
(speaking-loop) frame.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.package_spriteforge_character import (
    DEFAULT_RELEASE_VERSION,
    build_plan,
    _mouth_runtime_config,
)

SUFFIX = "_ktx2_uastc_q4_z18"
FRAME_DIR = "frames_alpha_2x_gmfss"


def _write_texture(workspace: Path, project: str, name: str) -> Path:
    """Create an authoring PNG's KTX2 sidecar where the packager expects it."""
    sidecar = workspace / project / f"{FRAME_DIR}{SUFFIX}" / "idle" / f"{name}.ktx2"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"ktx2")
    return sidecar


def _write_frames(workspace: Path, project: str, name: str) -> None:
    frame = workspace / project / FRAME_DIR / "idle" / f"{name}.png"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"png")


def _profile(*, closed_source_project: str | None) -> dict:
    profile: dict = {
        "root": f"{FRAME_DIR}/idle",
        "phase": "flat",
        "frame_names": ["own_closed.png"],
        "anchor_track": [{"cx": 4.5, "cy": -193.0, "width": 34.0, "height": 20.19}],
        "closed_frame_idx": 0,
    }
    if closed_source_project is not None:
        profile["closed_source"] = {
            "root": f"{closed_source_project}/{FRAME_DIR}/idle",
            "phase": "flat",
            "frame_name": "explicit_closed.png",
            "anchor": {"cx": 4.0, "cy": -196.0, "width": 34.0, "height": 18.0},
        }
    return profile


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / FRAME_DIR).mkdir(parents=True)
    # The profile's own closed frame always has a usable sidecar.
    (root / FRAME_DIR / "idle").mkdir(parents=True)
    (root / FRAME_DIR / "idle" / "own_closed.png").write_bytes(b"png")
    _write_texture(root, ".", "own_closed")
    return root


def test_missing_explicit_closed_source_is_rejected(workspace: Path) -> None:
    """A missing explicit closed-mouth sidecar must not fall back to the own frame."""
    # Own frame sidecar exists, explicit closed source sidecar deliberately absent.
    raw = {"version": 2, "canvas_size": [800, 600], "profiles": {"angry_speaking": _profile(closed_source_project="missing")}}

    with pytest.raises(ValueError) as excinfo:
        _mouth_runtime_config(workspace, raw, SUFFIX)

    message = str(excinfo.value)
    assert "angry_speaking" in message
    assert "refusing to substitute" in message
    # The own (speaking-loop) frame must never be used as a silent substitute.
    assert "own_closed" not in message


def test_explicit_closed_source_used_when_present(workspace: Path) -> None:
    _write_texture(workspace, "explicit", "explicit_closed")
    raw = {"version": 2, "canvas_size": [800, 600], "profiles": {"angry_speaking": _profile(closed_source_project="explicit")}}

    runtime, overlays = _mouth_runtime_config(workspace, raw, SUFFIX)

    texture, anchor = overlays["angry_speaking"]
    assert texture.name == "explicit_closed.ktx2"
    assert "explicit" in texture.parts
    # Anchor comes from the explicit closed source, not the profile's own track.
    assert anchor == {"cx": 4.0, "cy": -196.0, "width": 34.0, "height": 18.0}
    assert runtime["profiles"]["angry_speaking"]["runtime_overlay_anchor"] == anchor


def test_prefer_own_closed_frame_ignores_closed_source(workspace: Path) -> None:
    """Labels in PREFER_OWN_CLOSED_FRAME keep using their own frame, even if the
    explicit closed source is missing."""
    raw = {
        "version": 2,
        "canvas_size": [800, 600],
        "profiles": {"key_point_speaking": _profile(closed_source_project="missing")},
    }

    _runtime, overlays = _mouth_runtime_config(workspace, raw, SUFFIX)

    texture, _anchor = overlays["key_point_speaking"]
    assert texture.name == "own_closed.ktx2"


def test_build_plan_uses_requested_release_version(tmp_path: Path) -> None:
    """The release label is settable; the old packager hardcoded a stale value."""
    root = tmp_path / "ws"
    root.mkdir()

    graph = {
        "nodes": [{"id": "n1", "label": "idle", "root": f"{FRAME_DIR}/idle", "isRoot": True}],
        "edges": [],
    }
    mouth = {"version": 2, "canvas_size": [800, 600], "profiles": {}}
    (root / "graph_config.json").write_text(json.dumps(graph), encoding="utf-8")
    (root / "spriteforge_mouth_config.json").write_text(json.dumps(mouth), encoding="utf-8")

    # One clip from the graph node plus the two post-emotion clips.
    for project in (".", "projects/kurisu_smile_loop", "projects/kurisu_sad_idle_loop"):
        _write_frames(root, project, "frame0001")
        _write_texture(root, project, "frame0001")

    plan = build_plan(
        root,
        quality=4,
        zcmp=18,
        ffprobe=tmp_path / "absent-ffprobe.exe",
        version="2026.09.15-mouth-close.1",
    )

    assert plan["manifest"]["version"] == "2026.09.15-mouth-close.1"
    assert plan["manifest"]["version"] != DEFAULT_RELEASE_VERSION
    assert plan["manifest"]["id"] == "kurisu"
