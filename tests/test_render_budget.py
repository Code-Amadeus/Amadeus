from __future__ import annotations

import json
import subprocess
from pathlib import Path


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
