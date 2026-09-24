"""Opt-in real-game acceptance of profile creation and subsequent saved Start.

Requires Vite and installed game/Agent/script paths. Launches the game twice,
or reattaches twice with --attach-running; advance dialogue when prompted.
Only the native path picker is substituted.
Settings and reports go to an isolated directory under output/diagnostics.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright, expect

from server.handlers.vn_launch_handler import VNLaunchHandler
from tools.probes.verify_vn_profiles_ui import HTML
from vn_player.runtime import VNPlayerRuntime


async def run(args) -> None:
    output = ROOT / "output" / "diagnostics" / f"vn-profile-live-{time.strftime('%Y%m%d-%H%M%S')}"
    workspace = output / "workspace"
    workspace.mkdir(parents=True)
    paths = {"game": str(Path(args.game).resolve()), "agent": str(Path(args.agent).resolve()), "hook": str(Path(args.hook).resolve())}
    for path in paths.values():
        if not Path(path).is_file():
            raise FileNotFoundError(path)

    # This acceptance measures local launch/ingress; semantic model comparisons
    # are a separate, explicitly enabled experiment.
    for name in ("VN_LLM_ENABLED", "VN_IMMEDIATE_LLM_ENABLED", "VN_LOOKAHEAD_LLM_ENABLED",
                 "VN_REASONER_LLM_ENABLED", "VN_SUMMARY_LLM_ENABLED", "VN_RETROSPECTIVE_LLM_ENABLED"):
        os.environ[name] = "0"

    runtime = None

    def new_handler():
        nonlocal runtime
        runtime = VNPlayerRuntime(workspace)
        async def runtime_status():
            return runtime.status()
        handler = VNLaunchHandler()
        handler.configure(workspace, runtime_start=runtime.start, runtime_stop=runtime.stop,
                          runtime_status=runtime_status, runtime_line=runtime.ingest_line)
        return handler

    handler = new_handler()
    calls = []
    reports = []

    async def backend(method, params):
        if method != "vn.launch.status":
            calls.append({"method": method, "params": params})
        return await handler.handle(method, params)

    html = HTML.replace("window.amadeus =", """
setInterval(async () => {
  const status = await send('vn.launch.status', {});
  for (const listener of window.vnSubscribers['vn.launch.status'] || []) listener(status);
}, 1000);
window.amadeus =""")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1120, "height": 900})
        await page.context.grant_permissions(["local-network-access"], origin=args.url)
        await page.expose_function("backend", backend)
        await page.expose_function("pickFile", lambda kind: {"ok": True, "cancelled": False, "path": paths[kind], "detail": ""})
        await page.route("**/vn-profile-test", lambda route: route.fulfill(content_type="text/html", body=html))
        try:
            await page.goto(args.url.rstrip("/") + "/vn-profile-test")
            await page.get_by_role("button", name="Add game", exact=True).click()
            form = page.get_by_role("dialog")
            await form.get_by_label("Game name", exact=True).fill(args.name)
            await form.get_by_role("tab", name="Text connection", exact=True).click()
            for label in ("Game executable", "Game hook script (.js)"):
                await form.get_by_role("button", name=f"Browse: {label}", exact=True).click()
            agent_details = form.locator("details").filter(has=page.get_by_label("Agent installation (shared by all games)", exact=True))
            if await agent_details.get_attribute("open") is None:
                await agent_details.locator("summary").click()
            await form.get_by_role("button", name="Browse: Agent installation (shared by all games)", exact=True).click()
            await form.get_by_role("tab", name="Play preferences", exact=True).click()
            await form.get_by_label("Exit wallpaper before game").uncheck()
            if args.attach_running:
                await form.get_by_role("tab", name="Text connection", exact=True).click()
                await form.get_by_label("Launch game with").select_option("manual")
            else:
                await form.get_by_label("Close games launched by VN Player on stop").check()
                if args.steam_app_id:
                    await form.get_by_role("tab", name="Text connection", exact=True).click()
                    await form.get_by_label("Launch game with").select_option("steam")
                    await form.get_by_label("Steam app ID").fill(args.steam_app_id)
            await page.screenshot(path=str(output / "saved-settings.png"))
            await form.get_by_role("button", name="Save and test text", exact=True).click()

            for attempt in (1, 2):
                if attempt == 2:
                    handler = new_handler()
                    await page.reload()
                    await page.locator("select").first.select_option(label=args.name)
                    await page.get_by_role("button", name="Start", exact=True).click()
                deadline = time.monotonic() + args.timeout
                print(json.dumps({"phase": attempt, "action": "advance game dialogue", "output": str(output)}, ensure_ascii=False), flush=True)
                last_status = ""
                last_count = -1
                while time.monotonic() < deadline:
                    state = await handler._manager.status()
                    status = state["status"]
                    if status == "error":
                        raise RuntimeError(state["error"])
                    if status != last_status:
                        print(json.dumps({"phase": attempt, "status": status, "game": state["game"], "hook": state["hook"]}, ensure_ascii=False), flush=True)
                        last_status = status
                    count = state["bridge"].get("lineCount", 0)
                    if count != last_count:
                        print(json.dumps({"phase": attempt, "captured": count, "required": args.lines}), flush=True)
                        last_count = count
                    if count >= args.lines:
                        break
                    await asyncio.sleep(.5)
                else:
                    raise TimeoutError(f"Phase {attempt}: did not receive {args.lines} game observations")
                if attempt == 1:
                    assert not runtime.enabled
                    await expect(page.get_by_role("region", name="Captured text")).to_contain_text(state["capturedLines"][-1]["text"])
                else:
                    assert runtime.enabled
                    state["runtimeObservations"] = runtime.store.short_memory()
                await page.screenshot(path=str(output / f"capture-{attempt}.png"))
                reports.append({"phase": attempt, "state": state})
                (output / f"capture-{attempt}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                await page.get_by_role("button", name="Stop", exact=True).click()
                await expect(page.get_by_role("button", name="Start", exact=True)).to_be_enabled()
                print(json.dumps({"phase": attempt, "result": "passed", "count": state["bridge"]["lineCount"]}), flush=True)
            first_pid = reports[0]["state"]["game"]["pid"]
            second_pid = reports[1]["state"]["game"]["pid"]
            if args.attach_running:
                assert first_pid == second_pid
                assert all(report["state"]["game"]["status"] == "external_running" for report in reports)
                print("PASS: saved profile reloaded; ordinary Start reattached Agent to the externally launched game", flush=True)
            else:
                assert first_pid != second_pid
                print("PASS: saved profile reloaded in a new manager; ordinary Start launched and injected a fresh game process without manual Agent steps", flush=True)
        finally:
            await handler._manager.stop({"closeGame": True, "reason": "profile_live_acceptance"})
            (output / "report.json").write_text(json.dumps({"phases": reports, "calls": calls}, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--name", default="VN live acceptance")
    parser.add_argument("--game", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("--lines", type=int, default=3)
    parser.add_argument("--attach-running", action="store_true", help="Validate a game already started by its storefront; does not qualify automatic game launch")
    parser.add_argument("--steam-app-id", default="", help="Start this Steam app through the saved profile")
    parser.add_argument("--timeout", type=int, default=180)
    asyncio.run(run(parser.parse_args()))
