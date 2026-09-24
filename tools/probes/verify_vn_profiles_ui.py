"""Headless UI acceptance using the real VN form, handler, store and adapter.

Run the Electron Vite dev server first, then:
  python tools/probes/verify_vn_profiles_ui.py --url http://127.0.0.1:5173

Only process discovery/injection and the native file picker are substituted.
No game, Agent, LLM or user's saved profile is touched.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright
from PIL import Image, ImageDraw

from server.handlers.vn_launch_handler import VNLaunchHandler
from server.vn_text_sources import AgentVNTextSource


HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div id="root" style="height:100vh;display:flex"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
</script><script type="module">
import React from '/node_modules/.vite/deps/react.js';
import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
import VNPage from '/src/renderer/components/VNPage.tsx';
import { I18nProvider } from '/src/renderer/i18n.tsx';
import '/src/renderer/styles/index.css';
const send = (method, params) => window.backend(method, params || {});
window.vnSubscribers = {};
const subscribe = (method, fn) => {
  (window.vnSubscribers[method] ||= []).push(fn);
  return () => { window.vnSubscribers[method] = window.vnSubscribers[method].filter(item => item !== fn); };
};
window.amadeus = {selectVNFile: kind => window.pickFile(kind), getDesktopSettings: async () => ({values:{}})};
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(I18nProvider, null, React.createElement(VNPage, {send, subscribe, connected:true})));
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    args = parser.parse_args()
    output = ROOT / "output" / "diagnostics" / "vn-profiles-ui"
    output.mkdir(parents=True, exist_ok=True)
    calls = []
    fixture_image = Image.new("RGB", (320, 180), "#17365a")
    fixture_draw = ImageDraw.Draw(fixture_image)
    fixture_draw.rectangle((0, 122, 320, 180), fill="#091b31")
    fixture_draw.ellipse((190, 24, 258, 92), fill="#d9b773")
    fixture_draw.text((15, 141), "Fixture game window", fill="white")
    fixture_buffer = io.BytesIO()
    fixture_image.save(fixture_buffer, format="JPEG")
    fixture_bytes = fixture_buffer.getvalue()
    fixture_data_url = "data:image/jpeg;base64," + base64.b64encode(fixture_bytes).decode("ascii")

    class FixtureAgent(AgentVNTextSource):
        async def _launch_agent(self, profile, *, target_pid, attach):
            assert target_pid == 12345 and attach
            calls.append({"injected": profile["hookHelper"], "pid": target_pid})

        def _start_bridge(self, profile, params):
            pass

        async def start(self, profile, params, *, target_pid):
            await super().start(profile, params, target_pid=target_pid)
            for text in ["你好。", "你好。", "【选择】\n留下\n离开"]:
                await self._receive_agent_message(json.dumps({"type": "copyText", "sentence": text}, ensure_ascii=False))

    with tempfile.TemporaryDirectory(prefix="vn-profile-ui-") as temporary:
        root = Path(temporary)
        files = {"game": str(root / "game.exe"), "agent": str(root / "agent.exe"), "hook": str(root / "game.js")}
        for file in files.values():
            Path(file).touch()
        handler = VNLaunchHandler()

        runtime_state = {"status": "stopped"}
        model_available = True
        async def start_runtime(params):
            nonlocal runtime_state
            calls.append({"runtime_start": params})
            runtime_state = {
                "status": "active", "session_id": params["session_id"],
                "capabilities": {
                    key: {"requested": bool(value), "available": True, "enabled": bool(value) and model_available,
                          "reason": ("ready" if model_available else "model_unavailable") if value else "disabled_by_profile"}
                    for key, value in params["capabilities"].items()
                },
                "visual": {"supported": model_available,
                           "reason": "" if model_available else "model_unconfigured"},
            }
            return runtime_state

        async def stop_runtime(_params):
            nonlocal runtime_state
            runtime_state = {"status": "stopped"}

        async def runtime_status():
            return runtime_state

        def configure():
            handler.configure(root, runtime_start=start_runtime,
                              runtime_stop=stop_runtime, runtime_status=runtime_status,
                              runtime_line=AsyncMock(return_value={}))

        configure()
        asr_response = {"status": "listening", "source": "vn_player"}

        async def backend(method, params):
            calls.append({"method": method, "params": params})
            if method == "asr.start":
                return asr_response
            if method in {"asr.stop", "vn.player.ask", "vn.choice.ask", "tts.interrupt"}:
                return {"status": "ok"}
            return await handler.handle(method, params)

        with patch("server.vn_launch_manager._find_game_pid", return_value=12345), \
             patch("server.vn_launch_manager.AgentVNTextSource", FixtureAgent), \
             patch("server.visual_runtime.capture_game_window", return_value={
                 "enabled": True, "mode": "on_demand", "scope": "game_window",
                 "actualScope": "game_window", "reason": "vn_player",
                 "capturedAt": "2026-09-24T00:00:00Z",
                 "frame": {"mime": "image/jpeg", "dataUrl": fixture_data_url,
                           "width": 320, "height": 180, "byteLength": len(fixture_bytes)},
                 "game": {"pid": 12345, "title": "Fixture game", "executable": files["game"]},
             }), sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1120, "height": 900})
            page.context.grant_permissions(["local-network-access"], origin=args.url)
            page.set_default_timeout(8000)
            errors = []
            def page_error(error):
                errors.append(str(error))
                print(f"Renderer error: {error}", flush=True)
            page.on("pageerror", page_error)
            page.on("console", lambda message: print(message.text, flush=True) if message.type == "error" else None)
            page.expose_function("backend", backend)
            page.expose_function("pickFile", lambda kind: {"ok": True, "cancelled": False, "path": files[kind], "detail": ""})
            page.route("**/vn-profile-test", lambda route: route.fulfill(content_type="text/html", body=HTML))
            page.goto(args.url.rstrip("/") + "/vn-profile-test")
            page.get_by_role("button", name="Add game", exact=True).click()
            form = page.get_by_role("dialog")
            form.get_by_role("tab", name="Play preferences", exact=True).click()
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form.get_by_role("tab", name="Game and companion", exact=True)).to_have_attribute("aria-selected", "true")
            expect(form.get_by_label("Game name", exact=True)).to_be_focused()
            form.get_by_label("Game name", exact=True).fill("UI acceptance game")
            form.get_by_role("tab", name="Game and companion", exact=True).focus()
            page.keyboard.press("ArrowDown")
            expect(form.get_by_role("tab", name="Text connection", exact=True)).to_be_focused()
            expect(form.get_by_role("tab", name="Text connection", exact=True)).to_have_attribute("aria-selected", "true")
            for label in ("Game executable", "Game hook script (.js)"):
                form.get_by_role("button", name=f"Browse: {label}", exact=True).click()
            agent_details = form.locator("details").filter(has=page.get_by_label("Agent installation (shared by all games)", exact=True))
            if agent_details.get_attribute("open") is None:
                agent_details.locator("summary").click()
            form.get_by_role("button", name="Browse: Agent installation (shared by all games)", exact=True).click()
            form.get_by_label("Launch game with").select_option("steam")
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form.get_by_label("Steam app ID")).to_be_focused()
            form.get_by_label("Steam app ID").fill("3345060")
            form.get_by_role("tab", name="Game and companion", exact=True).click()
            expect(form.get_by_role("radio", name="General VN", exact=False)).to_be_checked()
            abilities = form.get_by_role("list", name="Companion abilities")
            expect(abilities.locator("li")).to_have_count(4)
            expect(abilities.get_by_role("checkbox")).to_have_count(0)
            form.get_by_role("radio", name="Mystery VN", exact=False).check()
            expect(abilities.locator("li")).to_have_count(6)
            page.screenshot(path=str(output / "profile-editor.png"))
            form.get_by_role("button", name="Save and test text", exact=True).click()
            expect(form).not_to_be_visible()
            preview = page.get_by_role("region", name="Captured text", exact=True)
            expect(preview.get_by_text("你好。", exact=True)).to_have_count(2)
            expect(preview).to_contain_text("留下")
            expect(page.get_by_role("button", name="Edit game profile", exact=True)).to_be_disabled()
            page.screenshot(path=str(output / "capture-preview.png"))
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()

            # Recreate the handler and reload the renderer: settings come from disk.
            configure()
            page.reload()
            selector = page.get_by_label("Your game", exact=True)
            selector.select_option(label="UI acceptance game")
            page.get_by_role("button", name="Edit game profile", exact=True).click()
            expect(form.get_by_label("Launch game with")).to_have_value("steam")
            expect(form.get_by_label("Steam app ID")).to_have_value("3345060")
            expect(form.get_by_label("Game hook script (.js)", exact=True)).to_have_value(files["hook"])
            expect(form.get_by_role("radio", name="Mystery VN", exact=False)).to_be_checked()
            form.get_by_label("Game name", exact=True).fill("Renamed game")
            form.get_by_role("radio", name="General VN", exact=False).check()
            expect(abilities.locator("li")).to_have_count(4)
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form).not_to_be_visible()
            expect(selector.locator("option:checked")).to_have_text("Renamed game")
            page.get_by_role("button", name="Test text capture", exact=True).click()
            page.get_by_role("button", name="Text looks right — start companion", exact=True).click()
            expect(page.get_by_text("Companion is live", exact=True)).to_be_visible()
            page.evaluate("([method, payload]) => window.vnSubscribers[method].forEach(fn => fn(payload))",
                          ["vn.line", {"line": {"text": "The door creaks open."}}])
            page.evaluate("([method, payload]) => window.vnSubscribers[method].forEach(fn => fn(payload))",
                          ["vn.reaction", {"text": "Let's see what waits inside."}])
            expect(page.get_by_role("region", name="VN activity")).to_contain_text("The door creaks open.")
            expect(page.get_by_role("region", name="VN activity")).to_contain_text("Let's see what waits inside.")
            expect(page.get_by_role("button", name="Start microphone")).to_be_enabled()
            assert len([call for call in calls if call.get("method") == "asr.start"]) == 0
            page.get_by_role("button", name="Start microphone").click()
            expect(page.get_by_role("button", name="Stop microphone")).to_be_visible()
            expect(page.get_by_text("Listening", exact=True)).to_be_visible()
            page.get_by_role("button", name="Attach game view").click()
            expect(page.get_by_alt_text("Attached game view")).to_be_visible()
            page.screenshot(path=str(output / "companion-live-attachment.png"))
            page.get_by_placeholder("Ask about the current line, add a note, or inspect a choice...").fill("What happened?")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.get_by_alt_text("Attached game view")).to_have_count(0)
            asks = [call for call in calls if call.get("method") == "vn.player.ask"]
            assert asks and asks[-1]["params"]["visual_context"]["game"]["pid"] == 12345
            assert len([call for call in calls if call.get("method") == "asr.start"]) == 1
            page.get_by_role("button", name="Stop microphone").click()
            expect(page.get_by_role("button", name="Start microphone")).to_be_visible()
            asr_response = {"status": "already_listening"}
            page.get_by_role("button", name="Start microphone").click()
            expect(page.get_by_text("Microphone is busy in another session.")).to_be_visible()
            expect(page.get_by_role("button", name="Start microphone")).to_be_visible()
            asr_stop_count = len([call for call in calls if call.get("method") == "asr.stop"])
            starts = [call for call in calls if "runtime_start" in call]
            assert len(starts) == 1 and starts[0]["runtime_start"]["prompt_pack"] == "base"
            assert starts[0]["runtime_start"]["capabilities"]["retrospective"] is True
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
            assert asr_stop_count >= 1
            assert len([call for call in calls if call.get("method") == "asr.stop"]) == asr_stop_count

            # Failed validation keeps the editor open; existing profiles survive.
            page.get_by_role("button", name="Edit game profile", exact=True).click()
            form.get_by_role("tab", name="Text connection", exact=True).click()
            form.get_by_label("Game executable", exact=True).fill("relative.exe")
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form.get_by_role("alert")).to_contain_text("absolute")
            form.get_by_role("button", name="Cancel", exact=True).click()
            page.get_by_role("button", name="Edit game profile", exact=True).click()
            form.get_by_role("button", name="Cancel", exact=True).click()
            model_available = False
            page.get_by_role("button", name="Start", exact=True).click()
            expect(page.get_by_role("button", name="Start microphone")).to_be_disabled()
            expect(page.get_by_role("button", name="Attach game view")).to_be_disabled()
            expect(page.get_by_text("Game view: Choose a model to use this ability.")).to_be_visible()
            expect(page.get_by_text("The companion model is unavailable.").first).to_be_visible()
            expect(page.get_by_text("model_unconfigured")).to_have_count(0)
            page.screenshot(path=str(output / "interaction-disabled.png"))
            page.get_by_role("button", name="Stop", exact=True).click()
            assert len(handler._manager.profiles()["profiles"]) == 2
            assert len([call for call in calls if "injected" in call]) == 4
            assert [call["params"]["captureOnly"] for call in calls if call.get("method") == "vn.launch.start"] == [True, True, False, False]
            # Chinese localization and narrower app panels remain usable.
            page.add_init_script("localStorage.setItem('amadeus.ui.locale', 'zh-CN')")
            page.reload()
            page.wait_for_selector("#vn-game-select")
            page.get_by_label("我的游戏", exact=True).select_option(label="Renamed game")
            page.screenshot(path=str(output / "home-zh.png"))
            page.get_by_role("button", name="编辑游戏配置", exact=True).click()
            form.get_by_role("radio", name="推理视觉小说", exact=False).check()
            page.screenshot(path=str(output / "editor-zh.png"))
            form.get_by_role("tab", name="取文连接", exact=True).click()
            page.screenshot(path=str(output / "connection-zh.png"))
            form.get_by_role("tab", name="游玩偏好", exact=True).click()
            page.screenshot(path=str(output / "preferences-zh.png"))
            page.evaluate("document.documentElement.dataset.theme = 'wallpaper-slice'")
            page.mouse.move(0, 0)
            page.evaluate("async () => { await Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))) }")
            page.screenshot(path=str(output / "preferences-dark-zh.png"))
            page.evaluate("delete document.documentElement.dataset.theme")
            form.get_by_role("tab", name="游戏与陪玩", exact=True).click()
            page.set_viewport_size({"width": 480, "height": 780})
            expect(form.get_by_role("button", name="保存并测试取文", exact=True)).to_be_in_viewport()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / "editor-narrow-zh.png"))
            form.get_by_role("button", name="取消", exact=True).click()
            page.screenshot(path=str(output / "home-narrow-zh.png"))
            assert not errors, errors
            browser.close()
    (output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: profile persistence, explicit capture test, Base runtime Start, type-bound capabilities, Steam settings, capture-to-play, model availability, voice, game view, validation and Chinese layout")


if __name__ == "__main__":
    main()
