from __future__ import annotations

import pytest

from config.settings import _resolve_graphics_profile


@pytest.mark.parametrize(
    ("profile", "custom_fps", "custom_resolution", "expected"),
    [
        ("standard", 30, 1.5, (60, None)),
        ("power_saving", 60, 2.0, (30, 1.5)),
        ("custom", 10, 0.25, (10, 0.25)),
        ("custom", 240, 4.0, (240, 4.0)),
    ],
)
def test_graphics_profile_selection(
    profile: str,
    custom_fps: int,
    custom_resolution: float,
    expected: tuple[int, float | None],
) -> None:
    assert _resolve_graphics_profile(profile, custom_fps, custom_resolution) == expected


@pytest.mark.parametrize("fps", [1, 5, 9, 241])
def test_graphics_profile_rejects_unsupported_custom_fps(fps: int) -> None:
    with pytest.raises(ValueError, match="RENDER_MAX_FPS must be between 10 and 240"):
        _resolve_graphics_profile("custom", fps, 1.5)


def test_graphics_profile_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError, match="GRAPHICS_PROFILE must be one of"):
        _resolve_graphics_profile("battery", 30, 1.5)
