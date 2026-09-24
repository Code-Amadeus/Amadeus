"""Opt-in real-game acceptance of profile creation and subsequent saved Start.

Requires Vite and installed game/Agent/script paths. Launches the game twice;
advance dialogue when prompted. Only the native path picker is substituted.
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

    html = HTML.replace("const subscribe = () => () => {};", """
const listeners = new Map();
const subscribe = (method, fn) => { listeners.set(method, fn); return () => listeners.delete(method); };
setInterval(async () => { const status = await send('vn.launch.status', {}); listeners.get('vn.launch.status')?.(status); }, 1000);
""")
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
            for button in await form.get_by_role("button", name="Browse", exact=True).all():
                await button.click()
            await form.get_by_label("Exit wallpaper before game").uncheck()
            await form.get_by_label("Close games launched by VN Player on stop").check()
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
                while time.monotonic() < deadline:
                    state = await handler._manager.status()
                    status = state["status"]
                    if status == "error":
                        raise RuntimeError(state["error"])
                    if status != last_status:
                        print(json.dumps({"phase": attempt, "status": status, "game": state["game"], "hook": state["hook"]}, ensure_ascii=False), flush=True)
                        last_status = status
                    if state["bridge"].get("lineCount", 0) >= args.lines:
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
            assert reports[0]["state"]["game"]["pid"] != reports[1]["state"]["game"]["pid"]
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
    parser.add_argument("--timeout", type=int, default=180)
    asyncio.run(run(parser.parse_args()))
