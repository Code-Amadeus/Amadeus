"""Headless UI acceptance using the real VN form, handler, store and adapter.

Run the Electron Vite dev server first, then:
  python tools/probes/verify_vn_profiles_ui.py --url http://127.0.0.1:5173

Only process discovery/injection and the native file picker are substituted.
No game, Agent, LLM or user's saved profile is touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import expect, sync_playwright

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
import '/src/renderer/styles/index.css';
const send = (method, params) => window.backend(method, params || {});
const subscribe = () => () => {};
window.amadeus = {selectVNFile: kind => window.pickFile(kind)};
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(VNPage, {send, subscribe, connected:true}));
</script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    args = parser.parse_args()
    output = ROOT / "output" / "diagnostics" / "vn-profiles-ui"
    output.mkdir(parents=True, exist_ok=True)
    calls = []

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

        def configure():
            handler.configure(root, runtime_start=AsyncMock(side_effect=AssertionError("Unexpected runtime start")),
                              runtime_stop=AsyncMock(), runtime_status=AsyncMock(return_value={"status": "stopped"}),
                              runtime_line=AsyncMock(side_effect=AssertionError("Unexpected story input")))

        configure()

        async def backend(method, params):
            calls.append({"method": method, "params": params})
            return await handler.handle(method, params)

        with patch("server.vn_launch_manager._find_game_pid", return_value=12345), \
             patch("server.vn_launch_manager.AgentVNTextSource", FixtureAgent), sync_playwright() as playwright:
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
            form.get_by_label("Game name", exact=True).fill("UI acceptance game")
            for button in form.get_by_role("button", name="Browse", exact=True).all():
                button.click()
            expect(form.get_by_label("Game executable", exact=True)).to_have_value(files["game"])
            form.get_by_label("Launch game if it is not running").uncheck()
            page.screenshot(path=str(output / "profile-editor.png"))
            form.get_by_role("button", name="Save and test text", exact=True).click()
            expect(form).not_to_be_visible()
            preview = page.get_by_role("region", name="Captured text", exact=True)
            expect(preview.get_by_text("你好。", exact=False)).to_have_count(2)
            expect(preview).to_contain_text("留下")
            expect(page.get_by_role("button", name="Edit game profile", exact=True)).to_be_disabled()
            page.screenshot(path=str(output / "capture-preview.png"))
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()

            # Recreate the handler and reload the renderer: settings come from disk.
            configure()
            page.reload()
            selector = page.locator("select").first
            selector.select_option(label="UI acceptance game")
            page.get_by_role("button", name="Edit game profile", exact=True).click()
            expect(form.get_by_label("Launch game if it is not running")).not_to_be_checked()
            expect(form.get_by_label("Game hook script (.js)", exact=True)).to_have_value(files["hook"])
            form.get_by_label("Game name", exact=True).fill("Renamed game")
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form).not_to_be_visible()
            expect(selector.locator("option:checked")).to_have_text("Renamed game")
            page.get_by_role("button", name="Start", exact=True).click()
            expect(preview).to_contain_text("离开")
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()

            # Failed validation keeps the editor open; existing profiles survive.
            page.get_by_role("button", name="Edit game profile", exact=True).click()
            form.get_by_label("Game executable", exact=True).fill("relative.exe")
            form.get_by_role("button", name="Save", exact=True).click()
            expect(form.get_by_role("alert")).to_contain_text("absolute")
            form.get_by_role("button", name="Cancel", exact=True).click()
            assert len(handler._manager.profiles()["profiles"]) == 2
            assert len([call for call in calls if "injected" in call]) == 2
            assert not errors, errors
            browser.close()
    (output / "calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: add, native-picker handoff, save/test, repeated text, restart, edit, saved Start, validation; no runtime calls")


if __name__ == "__main__":
    main()
