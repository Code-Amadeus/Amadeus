from pathlib import Path

import pytest

from tools.package_spriteforge_character import _mouth_runtime_config, _sidecar


def mouth_profile():
    return {
        "profiles": {"surprise_speaking": {
            "root": "speaking/frames", "phase": "loop", "frame_names": ["open.png"],
            "closed_frame_idx": 0,
            "closed_source": {"root": "transition/frames", "phase": "loop",
                              "frame_name": "closed.png", "anchor": {"cx": 4, "cy": -196}},
        }},
    }


def texture(workspace: Path, name: str):
    path = _sidecar(workspace / name, "_ktx2_uastc_q4_z18")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test texture")
    return path


def test_explicit_closed_source_cannot_fall_back_to_open_speaking_frame(tmp_path):
    texture(tmp_path, "speaking/frames/loop/open.png")
    with pytest.raises(ValueError, match="missing KTX2 overlay"):
        _mouth_runtime_config(tmp_path, mouth_profile(), "_ktx2_uastc_q4_z18")


def test_explicit_closed_source_preserves_its_texture_and_anchor(tmp_path):
    closed = texture(tmp_path, "transition/frames/loop/closed.png")
    runtime, overlays = _mouth_runtime_config(tmp_path, mouth_profile(), "_ktx2_uastc_q4_z18")
    assert overlays["surprise_speaking"][0] == closed
    assert runtime["profiles"]["surprise_speaking"]["runtime_overlay_anchor"]["cy"] == -196


def test_profile_without_borrowed_source_keeps_own_frame(tmp_path):
    own = texture(tmp_path, "speaking/frames/loop/open.png")
    profile = mouth_profile()
    del profile["profiles"]["surprise_speaking"]["closed_source"]
    _, overlays = _mouth_runtime_config(tmp_path, profile, "_ktx2_uastc_q4_z18")
    assert overlays["surprise_speaking"][0] == own
