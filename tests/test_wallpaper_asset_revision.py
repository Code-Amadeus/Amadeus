from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.handlers.wallpaper_handler import WallpaperHandler
from wallpaper import wallpaper_engine_bridge
from wallpaper.scene_assets import _crt_bounds_norm


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wallpaper_asset_revision_changes_with_client_asset(tmp_path, monkeypatch) -> None:
    client_asset = tmp_path / "client.js"
    client_asset.write_text("first", encoding="utf-8")
    monkeypatch.setattr(
        wallpaper_engine_bridge,
        "_WALLPAPER_CLIENT_ASSETS",
        (client_asset,),
    )

    first = wallpaper_engine_bridge._wallpaper_asset_revision()
    client_asset.write_text("second-version", encoding="utf-8")
    second = wallpaper_engine_bridge._wallpaper_asset_revision()

    assert first != second
    assert len(first) == len(second) == 16


def test_bridge_info_is_idle_until_manual_start_and_exposes_asset_version() -> None:
    handler = WallpaperHandler()
    idle = handler.bridge_info()
    assert idle["running"] is False
    assert len(idle["assetVersion"]) == 16

    class Host:
        asset_port = 17778
        bridge_port = 17797
        action_token = "token"
        asset_version = "asset-revision"
        url = "http://127.0.0.1:17778/wallpaper"
        lively_url = "http://127.0.0.1:17778/lively"
        slice_host = "electron"
        slice_bounds = {"x": 0.25, "y": 0.2, "width": 0.5, "height": 0.5}

    handler._wallpaper_host = Host()
    info = handler.bridge_info()
    assert info["running"] is True
    assert info["assetVersion"] == "asset-revision"
    assert info["sliceHost"] == "electron"
    assert info["sliceBounds"] == Host.slice_bounds


def test_electron_slice_uses_normalized_crt_geometry_and_shared_canvas_channel() -> None:
    bounds = _crt_bounds_norm(
        {
            "img_size": [100, 200],
            "crt_polygon": [[20, 40], [80, 40], [80, 160], [20, 160]],
        }
    )
    assert {key: round(value, 6) for key, value in bounds.items()} == {
        "x": 0.2,
        "y": 0.2,
        "width": 0.6,
        "height": 0.6,
    }

    state = wallpaper_engine_bridge._BridgeState()
    canvas_client = state.add_canvas_client()
    state.publish({"method": "setMouth", "args": [0.5]})
    assert canvas_client.empty()

    canvas_call = {"method": "setCanvas", "args": [{"title": "Shared surface"}]}
    state.publish(canvas_call, replay="canvas")
    assert canvas_client.get_nowait() == canvas_call
    presentation_call = {
        "method": "setCanvasPresentation",
        "args": [{"presentation_locale": "zh-CN"}],
    }
    state.publish(presentation_call, replay="canvasPresentation")
    assert canvas_client.get_nowait() == presentation_call
    attention_call = {
        "method": "setAttention",
        "args": [{"requests": [{"id": "attention-1"}]}],
    }
    state.publish(attention_call, replay="attention")
    assert canvas_client.get_nowait() == attention_call
    assert state.canvas_snapshot() == {
        "calls": [presentation_call, canvas_call, attention_call]
    }

    host = object.__new__(wallpaper_engine_bridge.WallpaperEngineBridgeHost)
    host._asset_port = 17778
    host._bridge_port = 17797
    host._slice_host = "electron"
    assert host.url.endswith("&host=webwallpaper&sliceHost=electron")
    assert host.lively_url.endswith("&bridgePort=17797&sliceHost=electron")


def test_electron_slice_assets_reuse_the_wallpaper_surface_without_a_second_ui() -> None:
    html = (_PROJECT_ROOT / "render" / "web" / "electron_slice.html").read_text(encoding="utf-8")
    host = (_PROJECT_ROOT / "render" / "web" / "electron_slice_host.js").read_text(encoding="utf-8")
    surface = (_PROJECT_ROOT / "render" / "web" / "crt_canvas_surface.js").read_text(encoding="utf-8")
    main = (_PROJECT_ROOT / "electron" / "src" / "main" / "index.ts").read_text(encoding="utf-8")
    main_preload = (_PROJECT_ROOT / "electron" / "src" / "preload" / "index.mts").read_text(encoding="utf-8")
    slice_preload = (_PROJECT_ROOT / "electron" / "src" / "preload" / "slice.cts").read_text(encoding="utf-8")
    electron_tsconfig = (_PROJECT_ROOT / "electron" / "tsconfig.json").read_text(encoding="utf-8")
    desktop_layer = (_PROJECT_ROOT / "wallpaper" / "windows_desktop_layer.py").read_text(encoding="utf-8")
    wallpaper_scene = (_PROJECT_ROOT / "render" / "web" / "wallpaper_scene.js").read_text(encoding="utf-8")
    lively = (_PROJECT_ROOT / "wallpaper" / "lively" / "index.html").read_text(encoding="utf-8")

    assert '<html lang="en">' in html
    assert '<script src="/render/web/crt_canvas_surface.js"></script>' in html
    assert "createCrtCanvasSurface()" in host
    assert 'bridgeEndpoint("canvas-state")' in host
    assert 'bridgeEndpoint("canvas-events")' in host
    assert "setElectronSliceShape" in host
    assert "shapeFlushTimer" in host
    assert "window.setTimeout(flushShapeUpdate, 50)" in host
    assert "shapeRetryTimer" in host
    assert "accepted === true" in host
    assert "setCanvasPresentation" in host
    assert "setAttention" in host
    assert "surface.setAttention" in host
    assert "setPresentation(profile)" in surface
    assert "setAttention(payload)" in surface
    assert 'postCanvasAction("attention", "resolve"' in surface
    assert 'postCanvasAction("attention", "presented"' in surface
    assert 'presentationLocale: "en-US"' in surface
    assert 'presentationText[state.presentationLocale] || presentationText["en-US"]' in surface
    assert "nextCardHtml !== renderedCardHtml" in surface
    assert "if (!cardChanged) return;" in surface
    assert "if (event.isTrusted) return;" in surface
    assert ".crt-canvas-surface *:not(.crt-canvas-task-filters)::-webkit-scrollbar" in surface
    assert "width: 3px;" in surface
    assert "border-radius: 999px;" in surface
    assert "scrollbar-width: auto;" not in surface
    assert "scrollbar-width: thin;" not in surface
    assert "scrollbar-color:" not in surface
    assert "one WebKit rule" in surface
    assert "markActionPending(button, true);" in surface
    assert "routed to Locus provider" not in surface
    assert "Locus reports" not in surface
    assert "windows_desktop_layer.py" in main
    assert "--watch" in main
    assert "startElectronSliceDesktopMonitor" in main
    assert "electronSlicePlacementReady" in main
    assert "electronSliceShape" in main
    assert "resetElectronSliceRenderReadiness" in main
    assert "reconcileElectronSliceReadiness" in main
    assert "renderer shape committed" in main
    assert "renderer load failed" in main
    assert "window.showInactive()" in main
    assert "window.setIgnoreMouseEvents(false)" not in main
    assert "alwaysOnTop: false" in main
    assert "preload', 'slice.cjs'" in main
    assert "sandbox: true" in main
    assert "window.setShape(electronSliceShape)" in main
    assert "setElectronSliceShape" not in main_preload
    assert "setElectronSliceShape" in slice_preload
    assert "onElectronSliceShown" not in slice_preload
    assert '"src/**/*.cts"' in electron_tsconfig
    assert "restartBackend" not in slice_preload
    assert "openAuipApp" not in slice_preload
    assert "def find_wallpaper_parent" in desktop_layer
    assert "if (this._externalCanvasHost) return;" in wallpaper_scene
    assert 'if (info.sliceHost === "electron") sliceHost = "electron";' in lively


def test_shared_canvas_and_slice_host_are_javascript_syntax_valid() -> None:
    for relative_path in (
        "render/web/crt_canvas_surface.js",
        "render/web/electron_slice_host.js",
    ):
        subprocess.run(
            ["node", "--check", str(_PROJECT_ROOT / relative_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        )


def test_electron_wallpaper_start_selects_the_external_slice_host(monkeypatch) -> None:
    created: list[object] = []

    class Host:
        asset_port = 17778
        bridge_port = 17797
        action_token = "token"
        asset_version = "revision"
        slice_host = "electron"
        slice_bounds = {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6}
        url = "http://127.0.0.1:17778/wallpaper"
        lively_url = "http://127.0.0.1:17778/lively"

        def __init__(self, *, slice_host: str) -> None:
            self.slice_host = slice_host
            created.append(self)

        def set_canvas_action_handler(self, handler) -> None:
            self.canvas_action_handler = handler

        def start(self):
            return self

        def set_canvas(self, payload) -> None:
            self.canvas = payload

        def set_canvas_presentation(self, profile) -> None:
            self.presentation = profile

        def stop(self) -> None:
            pass

    class Animator:
        def __init__(self, host) -> None:
            self.host = host

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    import render.spriteforge_animator as animator_module

    monkeypatch.setattr(wallpaper_engine_bridge, "WallpaperEngineBridgeHost", Host)
    monkeypatch.setattr(animator_module, "SpriteForgeAnimator", Animator)

    handler = WallpaperHandler()
    result = asyncio.run(handler._start({"slice_host": "electron"}))
    assert created and created[0].slice_host == "electron"
    assert result["sliceHost"] == "electron"
    assert result["sliceBounds"] == Host.slice_bounds
    asyncio.run(handler._stop({}))


def test_wallpaper_host_starts_without_the_optional_character_pack(tmp_path, monkeypatch) -> None:
    class Host:
        asset_port = 17778
        bridge_port = 17797
        action_token = "token"
        asset_version = "revision"
        slice_host = "wallpaper"
        slice_bounds = {"x": 0.2, "y": 0.2, "width": 0.6, "height": 0.6}
        url = "http://127.0.0.1:17778/wallpaper"
        lively_url = "http://127.0.0.1:17778/lively"

        def __init__(self, *, slice_host: str) -> None:
            self.slice_host = slice_host

        def set_canvas_action_handler(self, handler) -> None:
            self.canvas_action_handler = handler

        def start(self):
            return self

        def set_canvas(self, payload) -> None:
            self.canvas = payload

        def set_canvas_presentation(self, profile) -> None:
            self.presentation = profile

        def stop(self) -> None:
            pass

    import render.spriteforge_animator as animator_module

    monkeypatch.setattr(wallpaper_engine_bridge, "WallpaperEngineBridgeHost", Host)
    monkeypatch.setattr(
        animator_module,
        "SPRITEFORGE_RUNTIME_ROOT",
        tmp_path / "character-pack-not-installed",
    )

    handler = WallpaperHandler()
    result = asyncio.run(handler._start({}))

    assert result["status"] == "started"
    assert handler._wallpaper_host is not None
    assert handler._wallpaper_animator is not None
    assert handler._wallpaper_animator.available is False
    asyncio.run(handler._stop({}))


def test_lively_wrapper_discovers_canonical_ports_from_backend() -> None:
    html = (_PROJECT_ROOT / "wallpaper" / "lively" / "index.html").read_text(
        encoding="utf-8"
    )
    node_runner = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync(0, "utf8");
const scriptMatch = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i);
if (!scriptMatch) {
  throw new Error("Lively wrapper has no executable script");
}

const events = [];
const fetchCalls = [];
let iframeSrc = "";
const iframe = {};
Object.defineProperty(iframe, "src", {
  get() {
    return iframeSrc;
  },
  set(value) {
    events.push("iframe-src");
    iframeSrc = value;
  },
});

const context = {
  URLSearchParams,
  window: {
    location: {
      search: "",
      origin: "http://127.0.0.1:17777",
      port: "17777",
    },
  },
  fetch: async (url, options) => {
    events.push("bridge-info");
    fetchCalls.push({ url, options });
    return {
      ok: true,
      json: async () => ({ assetPort: 17778, bridgePort: 17797 }),
    };
  },
  document: {
    getElementById: (id) => {
      if (id !== "amadeus-wallpaper") {
        throw new Error(`Unexpected element id: ${id}`);
      }
      return iframe;
    },
  },
  console: { warn: () => {} },
};

(async () => {
  await vm.runInNewContext(scriptMatch[1], context);
  process.stdout.write(JSON.stringify({ events, fetchCalls, iframeSrc }));
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
"""

    completed = subprocess.run(
        ["node", "-e", node_runner],
        input=html,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["fetchCalls"] == [
        {
            "url": "http://127.0.0.1:17777/wallpaper/bridge-info",
            "options": {"cache": "no-store"},
        }
    ]
    assert result["events"] == ["bridge-info", "iframe-src"]
    assert result["iframeSrc"] == (
        "http://127.0.0.1:17778/render/web/wallpaper_engine.html"
        "?bridgePort=17797&host=lively"
    )


def test_lively_wrapper_waits_for_manual_start_before_loading_iframe() -> None:
    html = (_PROJECT_ROOT / "wallpaper" / "lively" / "index.html").read_text(
        encoding="utf-8"
    )
    node_runner = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync(0, "utf8");
const scriptMatch = html.match(/<script\b[^>]*>([\s\S]*?)<\/script>/i);
if (!scriptMatch) {
  throw new Error("Lively wrapper has no executable script");
}

const bridgeResponses = [
  { running: false },
  { running: true, assetPort: 17778, bridgePort: 17797 },
];
const events = [];
const fetchCalls = [];
const iframeAssignments = [];
const timers = [];
let iframeSrc = "";
const iframe = {};
Object.defineProperty(iframe, "src", {
  get() {
    return iframeSrc;
  },
  set(value) {
    events.push("iframe-src");
    iframeAssignments.push(value);
    iframeSrc = value;
  },
});

function fakeSetTimeout(callback, delay) {
  events.push("retry-scheduled");
  timers.push({ callback, delay });
  return timers.length;
}

function fakeClearTimeout() {}

const browserWindow = {
  location: {
    search: "",
    origin: "http://127.0.0.1:17777",
    port: "17777",
  },
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
};
const context = {
  URLSearchParams,
  window: browserWindow,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  fetch: async (url, options) => {
    const responseIndex = fetchCalls.length;
    events.push(`bridge-info-${responseIndex + 1}`);
    fetchCalls.push({ url, options });
    return {
      ok: true,
      json: async () => bridgeResponses[Math.min(responseIndex, 1)],
    };
  },
  document: {
    getElementById: (id) => {
      if (id !== "amadeus-wallpaper") {
        throw new Error(`Unexpected element id: ${id}`);
      }
      return iframe;
    },
  },
  console: { warn: () => {} },
};

(async () => {
  const execution = Promise.resolve(vm.runInNewContext(scriptMatch[1], context));
  let executionError = "";
  execution.catch((error) => {
    executionError = error.stack || String(error);
  });

  // Let the first bridge-info response and its retry scheduling settle without
  // running the mocked timer.
  await new Promise((resolve) => setImmediate(resolve));
  const afterFirstResponse = {
    fetchCount: fetchCalls.length,
    iframeSrc,
    iframeAssignments: iframeAssignments.slice(),
    pendingTimers: timers.length,
  };

  // Manually advance exactly one retry, as if the wallpaper bridge had just
  // been started from the app.
  if (timers.length > 0) {
    const retry = timers.shift();
    const retryResult = retry.callback();
    if (retryResult && typeof retryResult.then === "function") {
      await retryResult;
    }
    await new Promise((resolve) => setImmediate(resolve));
  }

  process.stdout.write(JSON.stringify({
    afterFirstResponse,
    events,
    fetchCalls,
    iframeAssignments,
    iframeSrc,
    executionError,
  }));
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exitCode = 1;
});
"""

    completed = subprocess.run(
        ["node", "-e", node_runner],
        input=html,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["executionError"] == ""
    assert result["afterFirstResponse"] == {
        "fetchCount": 1,
        "iframeSrc": "",
        "iframeAssignments": [],
        "pendingTimers": 1,
    }
    assert result["fetchCalls"] == [
        {
            "url": "http://127.0.0.1:17777/wallpaper/bridge-info",
            "options": {"cache": "no-store"},
        },
        {
            "url": "http://127.0.0.1:17777/wallpaper/bridge-info",
            "options": {"cache": "no-store"},
        },
    ]
    assert result["iframeAssignments"] == [
        "http://127.0.0.1:17778/render/web/wallpaper_engine.html"
        "?bridgePort=17797&host=lively"
    ]
    assert result["iframeSrc"] == result["iframeAssignments"][0]


def test_canvas_asset_rewrite_preserves_permission_and_workspace_paths() -> None:
    host = object.__new__(wallpaper_engine_bridge.WallpaperEngineBridgeHost)
    host._asset_port = 17778
    project_path = str(wallpaper_engine_bridge._PROJECT_ROOT)
    screenshot_path = str(
        wallpaper_engine_bridge._PROJECT_ROOT
        / "assets"
        / "images"
        / "amadeus_desktop_wallpaper.png"
    )

    rewritten = host._rewrite_canvas_payload(
        {
            "screenshot": screenshot_path,
            "taskDock": {
                "workspaceFocusPath": project_path,
                "items": [{"workspacePath": project_path}],
            },
            "permissionRequest": {
                "scope": [project_path],
                "scope_paths": [project_path],
            },
        }
    )

    assert rewritten["screenshot"].startswith("http://127.0.0.1:17778/")
    assert rewritten["taskDock"]["workspaceFocusPath"] == project_path
    assert rewritten["taskDock"]["items"][0]["workspacePath"] == project_path
    assert rewritten["permissionRequest"]["scope"] == [project_path]
    assert rewritten["permissionRequest"]["scope_paths"] == [project_path]


if __name__ == "__main__":
    import tempfile

    class _MonkeyPatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    with tempfile.TemporaryDirectory() as tmp:
        patch = _MonkeyPatch()
        try:
            test_wallpaper_asset_revision_changes_with_client_asset(Path(tmp), patch)
        finally:
            patch.undo()
    print("ok: asset revision changes when a client asset changes")

    test_bridge_info_is_idle_until_manual_start_and_exposes_asset_version()
    print("ok: bridge info stays idle until manual start and exposes the asset version")

    test_electron_slice_uses_normalized_crt_geometry_and_shared_canvas_channel()
    print("ok: Electron Slice uses normalized CRT geometry and a canvas-only channel")

    test_electron_slice_assets_reuse_the_wallpaper_surface_without_a_second_ui()
    print("ok: Electron Slice reuses the shared surface behind a minimal preload")

    test_shared_canvas_and_slice_host_are_javascript_syntax_valid()
    print("ok: shared Canvas and Electron Slice host scripts are syntax-valid")

    patch = _MonkeyPatch()
    try:
        test_electron_wallpaper_start_selects_the_external_slice_host(patch)
    finally:
        patch.undo()
    print("ok: Electron wallpaper start selects the external Slice host")

    test_lively_wrapper_discovers_canonical_ports_from_backend()
    print("ok: Lively wrapper discovers canonical ports from the backend")

    test_lively_wrapper_waits_for_manual_start_before_loading_iframe()
    print("ok: Lively wrapper waits for manual start before loading the iframe")

    test_canvas_asset_rewrite_preserves_permission_and_workspace_paths()
    print("ok: canvas asset rewrite preserves permission and workspace paths")
