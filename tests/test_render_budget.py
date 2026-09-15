from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from config.settings import _resolve_graphics_profile


ROOT = Path(__file__).resolve().parents[1]
RENDER_BUDGET = ROOT / "render" / "web" / "render_budget.js"


def _run_node(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


def test_every_renderer_host_loads_budget_before_renderer() -> None:
    for relative in (
        "render/web/index.html",
        "render/web/wallpaper.html",
        "render/web/wallpaper_engine.html",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert source.index("render_budget.js") < source.rindex("renderer.js")


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


def test_render_budget_resolves_frame_rate_and_resolution_together() -> None:
    result = _run_node(
        f"""
const budget = require({json.dumps(str(RENDER_BUDGET))});
const cases = [
  {{ maxFps: 60, maxResolution: null, devicePixelRatio: 2.5 }},
  {{ maxFps: 30, maxResolution: 1.5, devicePixelRatio: 2.5 }},
  {{ maxFps: 45, maxResolution: 1.5, devicePixelRatio: 1 }},
  {{ maxFps: 0, maxResolution: 0, devicePixelRatio: 2 }},
].map(value => budget.resolveRenderBudget(value));
process.stdout.write(JSON.stringify(cases));
"""
    )
    assert result == [
        {"maxFps": 60, "resolution": 2.5},
        {"maxFps": 30, "resolution": 1.5},
        {"maxFps": 45, "resolution": 1},
        {"maxFps": 60, "resolution": 2},
    ]


def test_project_and_wallpaper_engine_limits_use_lower_supported_value() -> None:
    result = _run_node(
        f"""
const budget = require({json.dumps(str(RENDER_BUDGET))});
const ticker = {{ maxFPS: 0 }};
const controller = budget.createFrameRateController(ticker, 30);
const values = [controller.apply()];
values.push(controller.setHostMaxFps(60));
values.push(controller.setHostMaxFps(20));
values.push(controller.setHostMaxFps(10));
process.stdout.write(JSON.stringify({{ values, ticker: ticker.maxFPS }}));
"""
    )
    assert result == {"values": [30, 30, 20, 10], "ticker": 10}


def test_invalid_wallpaper_engine_limit_restores_project_profile() -> None:
    result = _run_node(
        f"""
const budget = require({json.dumps(str(RENDER_BUDGET))});
const ticker = {{ maxFPS: 0 }};
const controller = budget.createFrameRateController(ticker, 60);
const values = [5, 0, -1, NaN, 241].map(value => controller.setHostMaxFps(value));
process.stdout.write(JSON.stringify({{ values, ticker: ticker.maxFPS }}));
"""
    )
    assert result == {"values": [60, 60, 60, 60, 60], "ticker": 60}


def test_wallpaper_listener_preserves_existing_callback_and_applies_updates() -> None:
    result = _run_node(
        f"""
const budget = require({json.dumps(str(RENDER_BUDGET))});
const calls = [];
const target = {{
  wallpaperPropertyListener: {{
    applyGeneralProperties(properties) {{ calls.push(properties.fps); }},
    applyUserProperties() {{}},
  }},
}};
const ticker = {{ maxFPS: 0 }};
const controller = budget.createFrameRateController(ticker, 60);
budget.installWallpaperEngineListener(target, controller);
target.wallpaperPropertyListener.applyGeneralProperties({{ fps: 24 }});
process.stdout.write(JSON.stringify({{
  calls,
  ticker: ticker.maxFPS,
  keptUserListener: typeof target.wallpaperPropertyListener.applyUserProperties === "function",
}}));
"""
    )
    assert result == {"calls": [24], "ticker": 24, "keptUserListener": True}
