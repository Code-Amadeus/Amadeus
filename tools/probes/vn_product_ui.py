"""Exercise production VN events in the browser; substitute hardware and model output."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

from PIL import Image
from playwright.async_api import async_playwright, expect
from server.event_bus import bus
from server.handlers.vn_launch_handler import VNLaunchHandler
from server.handlers.vn_player_handler import VNPlayerHandler
from server.protocol import Method
from server.vn_text_sources import AgentVNTextSource
from vn_player.llm_client import VNLLMClient
from vn_player.schemas import default_response
from tools.probes.verify_vn_profiles_ui import HTML, ROOT


async def run(url):
    output = ROOT / "output/diagnostics/vn-profiles-ui"
    output.mkdir(parents=True, exist_ok=True)
    calls, errors, model_ready = [], [], [True]
    page = None
    async def publish(method, payload):
        if page and not page.is_closed():
            await page.evaluate("([m,p]) => (window.vnSubscribers?.[m] || []).forEach(fn => fn(p))", [str(method), payload])

    class FixtureAgent(AgentVNTextSource):
        async def _launch_agent(self, profile, *, target_pid, attach):
            assert target_pid == os.getpid() and attach
            self._hook = {"status": "running"}
        def _start_bridge(self, *_args):
            self._bridge = {"status": "running", "lineCount": 0}
        async def start(self, profile, params, *, target_pid):
            await super().start(profile, params, target_pid=target_pid)
            for line in ["海边的咖啡馆今天重新开门。", "海边的咖啡馆今天重新开门。", "留下\n离开"]:
                await self._receive_agent_message(json.dumps({"type": "copyText", "sentence": line}))

    with tempfile.TemporaryDirectory(prefix="vn-product-ui-") as temporary:
        root = Path(temporary)
        game = root / "steamapps/common/Acceptance/game.exe"
        game.parent.mkdir(parents=True)
        (root / "steamapps/appmanifest_3345060.acf").write_text('"appid" "3345060"\n"name" "Acceptance"\n"installdir" "Acceptance"')
        files = {"game": str(game), "agent": str(root / "agent.exe"), "hook": str(root / "hook.js")}
        for file in files.values():
            Path(file).touch()
        vn, launch = VNPlayerHandler(), VNLaunchHandler()
        asr = {"active": False, "source": "", "source_payload": {}}
        async def asr_control(method, params):
            if method == Method.ASR_START:
                asr.update(active=True, source="vn_player", source_payload=params["source_payload"])
                return {"status": "listening"}
            if asr["source_payload"].get("input_id") == params.get("input_id"):
                asr.update(active=False, source="", source_payload={})
            return {"status": "stopped"}
        buffer = io.BytesIO()
        Image.new("RGB", (320, 180), "#17365a").save(buffer, format="JPEG")
        image = {"frame": {"dataUrl": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(), "mime": "image/jpeg", "width": 320, "height": 180}, "actualScope": "game_window", "game": {"pid": os.getpid()}}
        vn.configure(root, event_emit=publish, speak_callback=AsyncMock(), asr_control=asr_control,
                     asr_state=lambda: asr, capture_game_view=AsyncMock(return_value={"visual_context": image}))
        vn._runtime._llm_enabled = vn._runtime._immediate_llm_enabled = True
        vn._runtime._silence_pressure_enabled = False
        async def complete(*_args, **kwargs):
            calls.append({"model": True, "has_image": bool(kwargs.get("visual_context"))})
            return {**default_response("speak"), "importance": .9, "confidence": .9,
                    "speak": {"text": "我们先看看咖啡馆接下来会发生什么。", "priority": "normal"}}
        vn._runtime._immediate_response = complete
        launch.configure(root, runtime_start=lambda p: vn.handle(Method.VN_START, {**p, "summary_llm_enabled": False, "retrospective_llm_enabled": False}),
                         runtime_stop=lambda p: vn.handle(Method.VN_STOP, p), runtime_status=lambda: vn.handle(Method.VN_STATUS, {}),
                         runtime_line=lambda p: vn.handle(Method.VN_LINE, p), runtime_overlay=vn.set_overlay_url)
        launch._manager.project_root = ROOT  # bundled assets; profile storage remains isolated
        launch._manager.vn_root = root / "no-developer-install"
        async def overlay(_profile, _params):
            launch._manager._state["overlay"] = {"status": "running", "visible": True, "url": "http://127.0.0.1:8788/reaction"}
            return "http://127.0.0.1:8788/reaction"
        launch._manager._launch_overlay = overlay
        async def backend(method, params):
            calls.append({"method": method, "params": params})
            if method == "tts.interrupt":
                return {"status": "interrupted"}
            return await (vn.handle(method, params) if method in vn.methods else launch.handle(method, params))

        with patch("server.vn_launch_manager._find_game_pid", return_value=os.getpid()), \
             patch("server.vn_launch_manager.AgentVNTextSource", FixtureAgent), \
             patch("server.vn_launch_manager._set_overlay_visible"), \
             patch("server.visual_runtime.capture_game_window", return_value=image), \
             patch.object(VNLLMClient, "configured", lambda _: model_ready[0]), \
             patch.object(VNLLMClient, "supports_visual", lambda _: True):
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(viewport={"width": 1120, "height": 940})
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.context.grant_permissions(["local-network-access"], origin=url)
                await page.expose_function("backend", backend)
                await page.expose_function("pickFile", lambda kind: {"ok": True, "cancelled": False, "path": files[kind], "detail": ""})
                await page.route("**/vn-profile-test", lambda route: route.fulfill(content_type="text/html", body=HTML))
                bus.on(Method.VN_LAUNCH_STATUS, publish)
                try:
                    await page.goto(url.rstrip("/") + "/vn-profile-test")
                    await expect(page.get_by_role("option", name="Add your first game")).to_have_count(1)
                    await page.get_by_role("button", name="Add game", exact=True).click()
                    form = page.get_by_role("dialog")
                    await form.get_by_role("tab", name="Play preferences").click()
                    await form.get_by_role("button", name="Save", exact=True).click()
                    await expect(form.get_by_label("Game name")).to_be_focused()
                    await form.get_by_label("Game name").fill("Coffee house")
                    await form.get_by_role("radio", name="Mystery VN", exact=False).check()
                    await expect(form.get_by_role("list", name="Companion abilities").locator("li")).to_have_count(6)
                    await form.get_by_role("radio", name="General VN", exact=False).check()
                    await expect(form.get_by_role("list", name="Companion abilities").locator("li")).to_have_count(4)
                    await form.get_by_role("tab", name="Text connection").click()
                    for label in ("Game executable", "Game hook script (.js)", "Agent installation (shared by all games)"):
                        await form.get_by_role("button", name=f"Browse: {label}", exact=True).click()
                    await expect(form.get_by_label("Steam app ID")).to_have_value("3345060")
                    await expect(form.get_by_label("Launch game with")).to_have_value("steam")
                    await form.get_by_role("tab", name="Play preferences").click()
                    await expect(form.get_by_label("Portrait overlay", exact=True)).to_be_checked()
                    await form.get_by_role("button", name="Save and test text").click()
                    await expect(page.get_by_role("region", name="Captured text").locator("article")).to_have_count(3)
                    assert not vn._runtime.enabled
                    await page.get_by_role("button", name="Text looks right — start companion").click()
                    await expect(page.get_by_text("Following your game", exact=True)).to_be_visible()
                    feed = page.locator(".vn-feed")
                    await expect(feed.locator(".game-line")).to_have_count(3)
                    await expect(feed.locator(".companion-line")).to_have_count(1)
                    await expect(feed.locator(".companion-line")).to_contain_text("我们先看看")
                    await page.get_by_role("button", name="Pause comments", exact=True).click()
                    await expect(page.get_by_text("Following quietly", exact=True)).to_be_visible()
                    await page.get_by_label("Commentary frequency", exact=True).select_option("quiet")
                    await page.get_by_role("switch", name="Voice input (ASR)").click()
                    await page.get_by_label("VN vision", exact=True).select_option("on_question")
                    await page.get_by_role("button", name="Preview game view").click()
                    await expect(page.get_by_alt_text("Attached game view")).to_be_visible()
                    await page.get_by_role("textbox", name="Message to companion").fill("这家店今天开门了吗？")
                    await page.get_by_role("button", name="Send", exact=True).click()
                    await expect(feed.locator(".player-line")).to_have_count(1)
                    await expect(feed.locator(".companion-line")).to_have_count(2)
                    assert any(call.get("has_image") for call in calls)
                    assert [item["method"] for item in vn._runtime.activity()][-2:] == ["vn.player.event", "vn.reaction"]
                    count = await feed.locator("article").count()
                    await page.reload()
                    await expect(feed.locator("article")).to_have_count(count)
                    await expect(page.get_by_role("switch", name="Voice input (ASR)")).to_be_checked()
                    await expect(page.get_by_label("VN vision", exact=True)).to_have_value("on_question")
                    await expect(page.get_by_label("Commentary frequency", exact=True)).to_have_value("quiet")
                    await page.get_by_role("button", name="Hide portrait").click()
                    await expect(page.get_by_role("button", name="Show portrait")).to_be_visible()
                    await page.get_by_role("button", name="End session", exact=True).click()
                    await expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
                    assert not asr["active"]
                    saved = launch._manager._profiles.load().profiles[0]
                    assert saved.commentaryFrequency == "balanced" and saved.voiceInput is False and saved.visionMode == "off"
                    model_ready[0] = False
                    await page.get_by_role("button", name="Start", exact=True).click()
                    await expect(page.get_by_text("Following text · model unavailable", exact=True)).to_be_visible()
                    await expect(page.get_by_role("switch", name="Voice input (ASR)")).to_be_disabled()
                    await page.get_by_role("button", name="End session", exact=True).click()
                    await expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
                    model_ready[0] = True
                    await page.evaluate("localStorage.setItem('amadeus.ui.locale', 'zh-CN')")
                    await page.add_init_script("localStorage.setItem('amadeus.ui.locale', 'zh-CN')")
                    await page.reload()
                    await page.get_by_role("button", name="启动", exact=True).click()
                    await expect(page.get_by_text("正在跟读游戏", exact=True)).to_be_visible()
                    await page.screenshot(path=str(output / "session-inputs-zh.png"))
                    await page.evaluate("document.documentElement.dataset.theme = 'wallpaper-slice'")
                    await page.screenshot(path=str(output / "session-dark-zh.png"))
                    await page.evaluate("delete document.documentElement.dataset.theme")
                    await page.set_viewport_size({"width": 480, "height": 850})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    await page.screenshot(path=str(output / "session-narrow-zh.png"), full_page=True)
                    await page.get_by_role("button", name="结束陪玩", exact=True).click()
                    await expect(page.get_by_role("button", name="启动", exact=True)).to_be_enabled()
                    await page.set_viewport_size({"width": 1120, "height": 940})
                    await page.get_by_role("button", name="编辑游戏配置", exact=True).click()
                    await page.screenshot(path=str(output / "editor-zh.png"))
                    await form.get_by_role("tab", name="取文连接", exact=True).click()
                    await page.screenshot(path=str(output / "connection-zh.png"))
                    await form.get_by_role("tab", name="游玩偏好", exact=True).click()
                    await page.screenshot(path=str(output / "preferences-zh.png"))
                    assert not errors, errors
                    (output / "report.json").write_text(json.dumps({"result": "passed", "runtime_activity": vn._runtime.activity(), "calls": calls, "page_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                    print(json.dumps({"page": await page.locator('body').inner_text(), "errors": errors,
                                      "launch": launch._manager._state}, ensure_ascii=False), flush=True)
                    raise
                finally:
                    await launch._manager.stop()
                    bus.off(Method.VN_LAUNCH_STATUS, publish)
                    await browser.close()
    print("PASS: real runtime events, history recovery, profile defaults, session controls, Steam detection, clean setup and responsive Chinese UI")
