from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import urllib.request
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from render.server import AssetServer
from server.handlers.asr_handler import AsrHandler
from server.protocol import Method
from wallpaper import wallpaper_engine_bridge


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VOICE_STATE_PATH = _PROJECT_ROOT / "render" / "web" / "wallpaper_voice_state.js"
_VOICE_STATE_URL = "/render/web/wallpaper_voice_state.js"
_SCENE_URL = "/render/web/wallpaper_scene.js"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


def test_wallpaper_voice_state_reduces_the_continuous_voice_sequence() -> None:
    source = _VOICE_STATE_PATH.read_text(encoding="utf-8")
    node_runner = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");

const sandbox = { window: {} };
vm.runInNewContext(require("node:fs").readFileSync(0, "utf8"), sandbox, {
  filename: "wallpaper_voice_state.js",
});
const api = sandbox.window.AmadeusWallpaperVoiceState;

let state = api.initial();
state = api.reduce(state, {
  status: "awake",
  source: "wake",
  awake_deadline_ms: 61000,
});
assert.equal(state.phase, "listening");
assert.equal(api.remainingSeconds(state, 1000), 60);

state = api.reduce(state, {
  status: "recognized",
  source: "wake",
  text: "你好",
});
assert.equal(state.phase, "recognized");
assert.equal(state.userText, "你好");
assert.equal(api.remainingSeconds(state, 2000), null);

state = api.reduce(state, { status: "thinking", source: "wake", text: "你好" });
assert.equal(state.phase, "thinking");

state = api.reduce(state, {
  status: "turn_complete",
  source: "wake",
  awake_deadline_ms: 65000,
});
assert.equal(state.phase, "listening");
assert.equal(api.remainingSeconds(state, 5000), 60);

state = api.reduce(state, {
  status: "idle",
  source: "wake",
  reason: "awake_timeout",
});
assert.equal(state.phase, "idle");
assert.equal(state.userText, "");
"""

    completed = subprocess.run(
        ["node", "-e", node_runner],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_wallpaper_asset_service_loads_voice_state_before_the_scene() -> None:
    server = AssetServer(_PROJECT_ROOT, start_port=_free_port())
    port = server.start()
    try:
        for loader_path in (
            "/render/web/wallpaper.html",
            "/render/web/wallpaper_engine.html",
        ):
            loader = _get_text(f"http://127.0.0.1:{port}{loader_path}")
            assert loader.index(_VOICE_STATE_URL) < loader.index(_SCENE_URL)

        reducer = _get_text(f"http://127.0.0.1:{port}{_VOICE_STATE_URL}")
        assert reducer
    finally:
        server.stop()


def test_wallpaper_voice_state_participates_in_client_asset_revision() -> None:
    assert _VOICE_STATE_PATH in wallpaper_engine_bridge._WALLPAPER_CLIENT_ASSETS


def test_wallpaper_scene_presents_voice_status_and_dual_subtitles(monkeypatch) -> None:
    async def latest_snapshot(status: str, *, speaking: bool) -> list[dict]:
        host = wallpaper_engine_bridge.WallpaperEngineBridgeHost.__new__(
            wallpaper_engine_bridge.WallpaperEngineBridgeHost
        )
        host._state = wallpaper_engine_bridge._BridgeState()
        host.set_asr_status(
            {
                "status": "awake",
                "source": "wake",
                "awake_deadline_ms": 1700000060000,
            }
        )
        host.set_speaking(True)
        host.set_subtitle("第一句")
        host.set_speaking(speaking)
        if status == "waiting_turn_complete":
            handler = AsrHandler()
            handler._active = True
            handler._source = "wake"
            handler._waiting_turn_complete = True

            async def publish_to_bridge(method, payload):
                if method != Method.ASR_STATUS:
                    return
                host.set_asr_status(payload)
                if payload.get("status") == "waiting_turn_complete":
                    handler._waiting_turn_complete = False

            monkeypatch.setattr("server.handlers.asr_handler.bus.emit", publish_to_bridge)
            await handler._dispatch_recognized("断线重连")
            await handler._wait_until_turn_complete()
        else:
            host.set_asr_status({"status": status, "source": "wake", "text": "断线重连"})

        calls = host._state.snapshot()["calls"]
        assert sorted(call.get("method") for call in calls) == sorted(
            [
                "setAsrStatus",
                "setSpeaking",
                "setSubtitle",
            ]
        )
        return calls

    reconnect_snapshots = {
        "recognized": asyncio.run(latest_snapshot("recognized", speaking=False)),
        "thinking": asyncio.run(latest_snapshot("thinking", speaking=False)),
        "waiting": asyncio.run(latest_snapshot("waiting_turn_complete", speaking=False)),
        "speaking": asyncio.run(latest_snapshot("waiting_turn_complete", speaking=True)),
    }
    node_runner = r"""
const vm = require("node:vm");
const fs = require("node:fs");

class Point {
  constructor() { this.x = 0; this.y = 0; }
  set(x, y) { this.x = x; this.y = y === undefined ? x : y; }
}

class Container {
  constructor() {
    this.children = [];
    this.visible = true;
    this.renderable = true;
    this.alpha = 1;
    this.x = 0;
    this.y = 0;
    this.mask = null;
  }
  addChild(...items) {
    for (const item of items) {
      this.removeChild(item);
      this.children.push(item);
    }
    return items[items.length - 1];
  }
  addChildAt(item, index) {
    this.removeChild(item);
    this.children.splice(Math.max(0, Math.min(index, this.children.length)), 0, item);
    return item;
  }
  removeChild(item) {
    this.children = this.children.filter((child) => child !== item);
    return item;
  }
}

class Graphics extends Container {
  clear() {}
  beginFill() {}
  lineStyle() {}
  moveTo() {}
  lineTo() {}
  endFill() {}
  drawRect() {}
}

class Sprite extends Container {
  constructor(texture) {
    super();
    this.texture = texture || { valid: false, width: 1, height: 1 };
    this.anchor = new Point();
    this.scale = new Point();
    this.width = 1;
    this.height = 1;
  }
  destroy() {}
}

class Text extends Sprite {
  constructor(text, style) {
    super();
    this.text = text;
    this.style = Object.assign({}, style || {});
  }
}

const stage = new Container();
const timers = new Map();
let nextTimer = 1;
const clock = { now: 1700000000000 };
const app = {
  screen: { width: 1000, height: 600 },
  stage,
  ticker: {
    deltaMS: 16.6667,
    add(fn) { this.fn = fn; },
  },
  view: { addEventListener() {} },
};
const PIXI = {
  Container,
  Graphics,
  Sprite,
  Text,
  Texture: { from() { return { valid: true, width: 1, height: 1, baseTexture: { valid: true } }; } },
  BLEND_MODES: { ADD: "add" },
};
const renderApp = new Proxy({ getPixiApp() { return app; } }, {
  get(target, property) {
    if (property in target) return target[property];
    return () => {};
  },
});
const windowObject = {
  PIXI,
  renderApp,
  location: { search: "" },
  addEventListener() {},
  setTimeout(fn) {
    const id = nextTimer++;
    timers.set(id, fn);
    return id;
  },
  clearTimeout(id) { timers.delete(id); },
};
const context = {
  window: windowObject,
  PIXI,
  URLSearchParams,
  console: { log() {}, info() {}, warn() {}, error() {} },
  setTimeout: windowObject.setTimeout,
  clearTimeout: windowObject.clearTimeout,
  Date: { now() { return clock.now; } },
  Math,
  Map,
  AbortController,
};

vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);

const runtime = context.window.wallpaperApp;
runtime.initDesktopScene({
  crtConfig: {
    img_size: [1000, 600],
    crt_polygon: [[0, 0], [1000, 0], [1000, 600], [0, 600]],
  },
});
const reconnectCalls = JSON.parse(process.argv[3]);

function texts() {
  const found = [];
  function visit(item) {
    if (item instanceof Text) found.push(item);
    for (const child of item.children || []) visit(child);
  }
  visit(stage);
  return found;
}
function textValue(value) {
  return texts().find((item) => item.text === value);
}
function statusValue() {
  return texts().map((item) => item.text).find((value) =>
    value === "READY"
    || value === "已唤醒"
    || value.startsWith("正在听")
    || value === "思考中"
    || value === "正在说话"
  );
}

for (const call of reconnectCalls) runtime[call.method](...(call.args || []));
const reconnectUserSubtitle = !!textValue("你：断线重连");
const reconnectAssistantSubtitle = !!textValue("助手：第一句");
runtime.setSpeaking(false);
runtime.setAsrStatus({ status: "idle", source: "wake", reason: "test_reset" });
const ready = textValue("READY");
const readyLayout = ready ? { x: ready.x, y: ready.y } : null;
const now = clock.now;
runtime.setAsrStatus({ status: "awake", source: "wake", awake_deadline_ms: now + 60000 });
const awake = statusValue();
runtime.setAsrStatus({ status: "awake", source: "wake", awake_deadline_ms: now + 60000 });
const duplicateAwake = statusValue();
clock.now += 500;
app.ticker.fn(1);
const listening = statusValue();
runtime.setAsrStatus({ status: "recognized", source: "wake", text: "你好" });
const userSubtitle = !!textValue("你：你好");
runtime.setAsrStatus({ status: "thinking", source: "wake", text: "你好" });
const thinking = statusValue();
runtime.setSpeaking(true);
const speaking = statusValue();
runtime.setSubtitle("第一句");
const assistantSubtitle = !!textValue("助手：第一句");
runtime.setSpeaking(false);
runtime.setAsrStatus({
  status: "turn_complete",
  source: "wake",
  reason: "barge_in",
  awake_deadline_ms: clock.now + 60000,
});
const interruptedSubtitle = !!textValue("助手：第一句（已打断）");
runtime.setAsrStatus({ status: "recognized", source: "wake", text: "打断后的问题" });
const postInterruptUserSubtitle = !!textValue("你：打断后的问题");
runtime.setSubtitle("第一句");
runtime.setAsrStatus({ status: "idle", source: "wake", reason: "awake_timeout" });
const idle = statusValue();
const subtitleBeforeClear = !!textValue("助手：第一句");
for (const fn of Array.from(timers.values())) fn();
const subtitleAfterClear = texts().some((item) => item.text === "助手：第一句");
runtime.setSubtitle("普通字幕");
const inactiveSubtitle = !!textValue("普通字幕");

process.stdout.write(JSON.stringify({
  reconnectUserSubtitle,
  reconnectAssistantSubtitle,
  ready: !!ready,
  readyLayout,
  awake,
  duplicateAwake,
  listening,
  userSubtitle,
  thinking,
  speaking,
  assistantSubtitle,
  interruptedSubtitle,
  postInterruptUserSubtitle,
  idle,
  subtitleBeforeClear,
  subtitleAfterClear,
  inactiveSubtitle,
}));
"""
    reconnect_results = {}
    presentation_result = None
    for name, reconnect_calls in reconnect_snapshots.items():
        completed = subprocess.run(
            [
                "node",
                "-e",
                node_runner,
                str(_VOICE_STATE_PATH),
                str(_PROJECT_ROOT / "render" / "web" / "wallpaper_scene.js"),
                json.dumps(reconnect_calls, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        reconnect_results[name] = {
            "user": result.pop("reconnectUserSubtitle"),
            "assistant": result.pop("reconnectAssistantSubtitle"),
        }
        presentation_result = presentation_result or result
        assert result == presentation_result

    assert presentation_result == {
        "ready": True,
        "readyLayout": {"x": 988, "y": 12},
        "awake": "已唤醒",
        "duplicateAwake": "已唤醒",
        "listening": "正在听 · 60s",
        "userSubtitle": True,
        "thinking": "思考中",
        "speaking": "正在说话",
        "assistantSubtitle": True,
        "interruptedSubtitle": True,
        "postInterruptUserSubtitle": True,
        "idle": "READY",
        "subtitleBeforeClear": True,
        "subtitleAfterClear": False,
        "inactiveSubtitle": True,
    }
    assert reconnect_results == {
        "recognized": {"user": True, "assistant": False},
        "thinking": {"user": True, "assistant": False},
        "waiting": {"user": True, "assistant": False},
        "speaking": {"user": False, "assistant": True},
    }
