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
        {"maxFps": 60, "resolution": 2.5, "textureSampling": False},
        {"maxFps": 30, "resolution": 1.5, "textureSampling": False},
        {"maxFps": 45, "resolution": 1, "textureSampling": False},
        {"maxFps": 60, "resolution": 2, "textureSampling": False},
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


def test_texture_sampling_is_an_explicit_opt_in_independent_of_fps() -> None:
    from config.settings import declared_environment_fields
    from config.environment import EnvironmentReader

    field = next(f for f in declared_environment_fields() if f.key == "RENDER_TEXTURE_SAMPLING")
    assert field.default is False
    assert EnvironmentReader({}).boolean(field.key, field.default) is False
    result = _run_node(f"""
const budget = require({json.dumps(str(RENDER_BUDGET))});
const cases = [undefined, null, false, 'false', '0', '', 'yes', true, 'true', '1'];
process.stdout.write(JSON.stringify(cases.map(textureSampling =>
  budget.resolveRenderBudget({{maxFps:30,textureSampling}}).textureSampling)));
""")
    assert result == [False] * 7 + [True] * 3


@pytest.mark.parametrize("enabled", [False, True])
def test_sampling_flag_reaches_chat_wallpaper_and_bridge_discovery(tmp_path, monkeypatch, enabled) -> None:
    import asyncio
    from urllib.parse import parse_qs, urlparse
    from server.handlers import render_handler
    from server.handlers.wallpaper_handler import WallpaperHandler
    from wallpaper import wallpaper_engine_bridge as bridge_module

    monkeypatch.setattr(render_handler, "RENDER_TEXTURE_SAMPLING", enabled)
    handler = render_handler.RenderHandler()
    handler.configure(ROOT)
    query = parse_qs(urlparse(asyncio.run(handler._start({}))["url"]).query)
    assert query["renderTextureSampling"] == [str(int(enabled))]

    monkeypatch.setattr(bridge_module, "RENDER_TEXTURE_SAMPLING", enabled)
    host = object.__new__(bridge_module.WallpaperEngineBridgeHost)
    host._asset_port, host._bridge_port, host._slice_host = 17778, 17797, "electron"
    for url in [host.url, host.lively_url]:
        assert parse_qs(urlparse(url).query)["renderTextureSampling"] == [str(int(enabled))]
    assert host.render_texture_sampling is enabled
    # The backend discovery endpoint used by the generic Lively URL.
    wall = WallpaperHandler()
    from types import SimpleNamespace
    wall._wallpaper_host = SimpleNamespace(render_texture_sampling=enabled)
    assert wall.bridge_info()["renderTextureSampling"] is enabled
    assert wall._status("started")["renderTextureSampling"] is enabled


@pytest.mark.parametrize(("query_flag", "bridge_flag", "expected", "fetches"), [
    (None, True, True, 1),
    (None, None, False, 1),
    ("0", True, False, 0),
    ("1", False, True, 0),
])
def test_lively_forwards_opt_in_and_defaults_older_descriptors_off(query_flag, bridge_flag, expected, fetches):
    query = "?assetPort=17778&bridgePort=17797&graphicsProfile=standard&renderMaxFps=30"
    if query_flag is not None:
        query += "&renderTextureSampling=" + query_flag
    info = {"assetPort": 17778, "bridgePort": 17797, "graphicsProfile": "standard", "renderMaxFps": 30}
    if bridge_flag is not None:
        info["renderTextureSampling"] = bridge_flag
    result = _run_node(f"""
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync({json.dumps(str(ROOT / 'wallpaper/lively/index.html'))},'utf8');
const script=html.match(/<script\\b[^>]*>([\\s\\S]*?)<\\/script>/i)[1];
const iframe={{src:''}};let fetches=0;
const context={{URLSearchParams,console,
  window:{{location:{{search:{json.dumps(query)},origin:'http://127.0.0.1:17777'}}}},
  document:{{getElementById:()=>iframe}},
  fetch:async()=>{{fetches++;return {{ok:true,json:async()=>({json.dumps(info)})}};}}
}};
vm.runInNewContext(script,context);
setImmediate(()=>process.stdout.write(JSON.stringify({{url:iframe.src,fetches}})));
""")
    from urllib.parse import parse_qs, urlparse
    assert parse_qs(urlparse(result["url"]).query)["renderTextureSampling"] == [str(int(expected))]
    assert result["fetches"] == fetches
