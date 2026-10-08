"""Local qualification of the actual render and wallpaper pages.

Pass your licensed Core/model and an existing asset root. No private assets are
copied into the checkout. This is a browser/renderer qualification; physical
audio and native desktop embedding are qualified separately.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import psutil
from playwright.sync_api import sync_playwright

from render.server import AssetServer
from render.visual_profile import VisualProfileStore, draft_profile
from server.character_presentation import CharacterPresentationCoordinator
from server.handlers.wallpaper_handler import WallpaperHandler
from server.protocol import Method
from tts.mouth_signal import MouthSignalRouter
from tts.playback import StreamPlayer
from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost

DISPATCH = """(event) => {
  const p=event.params, app=renderApp;
  if(event.method==='render.character_intent')app.triggerCharacterIntent(p.label,p);
  else if(event.method==='render.character_release')app.releaseCharacter(p);
  else if(event.method==='render.speaking')app.setSpeaking(p.speaking);
  else if(event.method==='render.mouth')app.setMouth(p.value);
  else throw new Error('Unhandled probe render event: '+event.method);
}"""
READ = """() => ({status:renderApp._modelAdapter?.status(),mode:renderApp._mode,
  mouth:renderApp._modelAdapter?.smoothedMouth,
  actual:window.lastParameters||{},tickerCount:renderApp.getPixiApp().ticker.count,
  textureStats:renderApp.getTextureStats(),
  managedTextures:renderApp.getPixiApp().renderer.texture.managedTextures.length,
  model:!!renderApp._modelAdapter?.model,
  texture:!!renderApp._modelAdapter?.texture})"""


def scene_payload(asset_root: Path) -> dict:
    scene_root = asset_root / "scenarios/runtime"
    graph_path = scene_root / "scenario_graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8-sig")) if graph_path.is_file() else {"nodes": [], "edges": []}
    resources = {}
    for node in graph["nodes"]:
        relative = str(node.get("resource") or "")
        asset = scene_root / relative
        if asset.is_file():
            resources[node["id"]] = {"type": "image", "url": "/assets/scenarios/runtime/" + relative}
    return {
        "backgroundUrl": "/assets/images/amadeus_desktop_wallpaper.png",
        "ambientLowUrl": "/assets/images/amadeus_ambient_low_blend.png",
        "ambientDeltaUrl": "/assets/images/amadeus_ambient_high_blend.png",
        "subtitleFrameUrl": "/assets/images/subtitle_frame_big.png",
        "defaultSubtitleEnabled": True,
        "crtConfig": json.loads((ROOT / "wallpaper/crt_config.json").read_text(encoding="utf-8-sig")),
        "scenario": {"enabled": bool(resources), "graph": graph, "resources": resources,
                     "inactivitySeconds": 1, "placementMode": "crt_screen"},
    }


def processes():
    return psutil.Process().children(recursive=True)


def usage():
    children = processes()
    return {"rss_mb": sum(p.memory_info().rss for p in children if p.is_running()) / 1024**2,
            "cpu_seconds": sum(sum(p.cpu_times()[:2]) for p in children if p.is_running())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--browser", default="C:/Program Files/Google/Chrome/Application/chrome.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/live2d-surfaces")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    store = VisualProfileStore(args.output / "profiles.json", project_root=ROOT)
    profile, capabilities = draft_profile(str(args.model))
    profile["layouts"]["wallpaper"]["x"] = 0
    config = {"backend": "live2d", "selected_profile_id": profile["profile_id"],
              "core_path": str(args.core), "profiles": [profile]}
    store.save(config)
    server = AssetServer(ROOT, start_port=18400)
    server.mount_static("/assets", args.asset_root)
    port = server.start()
    host = WallpaperEngineBridgeHost(asset_port=18430, bridge_port=18460, slice_host="electron")
    host._asset_server.mount_static("/assets", args.asset_root)
    host.start()
    host._event("initDesktopScene", scene_payload(args.asset_root), bootstrap=True,
                bootstrap_key="initDesktopScene")
    host.configure_character(store.runtime_config("wallpaper", asset_server=host._asset_server))
    report = {"errors": [], "checks": {}, "warnings": capabilities["warnings"], "browser": "Chrome headless / SwiftShader"}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.browser, headless=True, timeout=15000,
                args=["--enable-webgl", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            main_page = context.new_page()
            main_page.set_default_timeout(25000)
            main_page.add_init_script("window.__DISABLE_RENDERER_WS__=true;")
            main_page.on("pageerror", lambda error: report["errors"].append("render: " + str(error)))
            main_page.goto(f"http://127.0.0.1:{port}/render/web/index.html?renderMaxFps=30")
            main_page.wait_for_function("!!window.renderApp")
            main_page.evaluate("(config)=>renderApp.configureCharacter(config)",
                               store.runtime_config("render", asset_server=server))
            assert main_page.evaluate("()=>renderApp._modelAdapter.state") == "ready"
            first_usage = usage()
            main_page.wait_for_timeout(1000)
            single_usage = usage()
            report["single_surface"] = {"rss_mb": single_usage["rss_mb"],
                                        "cpu_seconds_per_second": single_usage["cpu_seconds"] - first_usage["cpu_seconds"]}
            wall_browser = playwright.chromium.launch(
                executable_path=args.browser, headless=True, timeout=15000,
                args=["--enable-webgl", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            wall_context = wall_browser.new_context(viewport={"width":1280,"height":900})
            wall = wall_context.new_page()
            wall.set_default_timeout(25000)
            wall.on("pageerror", lambda error: report["errors"].append("wallpaper: " + str(error)))
            wall.goto(host.url + "&renderMaxFps=30")
            wall.wait_for_function("renderApp?._modelAdapter?.state==='ready'")
            report["checks"]["both_ready"] = [main_page.evaluate(READ), wall.evaluate(READ)]
            for page in (main_page, wall):
                page.evaluate("""() => {
                  renderApp._modelAdapter.model.internalModel.on('beforeModelUpdate',()=>{
                    const core=renderApp._modelAdapter.model.internalModel.coreModel;
                    window.lastParameters=Object.fromEntries(core._model.parameters.ids.map(id=>[id,core.getParameterValueById(id)]));
                  });
                }""")

            def emit(method, params):
                main_page.evaluate(DISPATCH, {"method": method.value if isinstance(method, Method) else method, "params": params})
                WallpaperHandler._apply_render_event(host, method, params)

            presentation = CharacterPresentationCoordinator(lambda method, params: emit(method, params),
                                                             emit_now=emit)
            presentation.set_payload_resolver(store.intent_payload)
            presentation.claim_now(source_kind="work", source_id="probe-work", label="work", tier="ambient")
            main_page.wait_for_timeout(800)
            wall.wait_for_function("renderApp._modelAdapter.expression==='Thinking'")
            presentation.claim_now(source_kind="chat", source_id="probe-line", label="smile")
            wall.wait_for_function("renderApp._modelAdapter.expression==='Smile'")
            for page in (main_page, wall):
                page.wait_for_function("window.lastParameters?.PARAM_EYE_L_SMILE > .9")
            report["checks"]["actual_smile_parameters"] = [main_page.evaluate(READ),wall.evaluate(READ)]
            for name, page in (("render", main_page), ("wallpaper", wall)):
                page.screenshot(path=str(args.output / f"{name}-smile.png"))
                assert page.evaluate("()=>lastParameters.PARAM_EYE_L_SMILE") > .9, page.evaluate("()=>({params:lastParameters,hidden:document.hidden,paused:renderApp._modelAdapter.paused})")
            router = MouthSignalRouter(primary_sink=lambda value: emit(Method.RENDER_MOUTH, {"value": value}))
            player = StreamPlayer(router)
            emit(Method.RENDER_SPEAKING, {"speaking": True})
            # The existing PCM owner computes each amplitude; no browser audio
            # or second analysis chain participates.
            samples = np.sin(np.arange(2400, dtype=np.float32) * (2*np.pi*180/24000)) * .25
            values = []
            for chunk in (samples, np.zeros(2400, dtype=np.float32), samples):
                values.append(player._emit_mouth_value_for_audio(chunk))
                main_page.wait_for_timeout(180)
                report.setdefault("mouth_windows", []).append([main_page.evaluate(READ), wall.evaluate(READ)])
            assert report["mouth_windows"][0][0]["actual"]["PARAM_MOUTH_OPEN_Y"] > .4
            assert report["mouth_windows"][1][0]["actual"]["PARAM_MOUTH_OPEN_Y"] < .08
            assert report["mouth_windows"][2][1]["actual"]["PARAM_MOUTH_OPEN_Y"] > .4
            emit(Method.RENDER_SPEAKING, {"speaking": False})
            emit(Method.RENDER_MOUTH, {"value": .95})  # stale tail must stay closed
            presentation.release_now(source_kind="chat", source_id="probe-line", handoff="after_speech")
            wall.wait_for_function("renderApp._modelAdapter.expression==='Thinking'")
            main_page.wait_for_timeout(100)
            assert all(page.evaluate("()=>renderApp._modelAdapter.smoothedMouth") == 0 for page in (main_page, wall))
            presentation.release_now(source_kind="work", source_id="probe-work", tier="ambient")
            wall.wait_for_function("renderApp._modelAdapter.expression===''" )
            report["checks"]["source_restoration_and_closed_tail"] = True

            host.set_activity("work")
            main_page.wait_for_timeout(1400)
            report["checks"]["live2d_activity"] = wall.evaluate("""() => ({
                activity:wallpaperApp.scene._mode,modelVisible:renderApp.getModelCharacterContainer().visible,
                background:wallpaperApp.scene.bg.texture.valid,model:!!renderApp._modelAdapter.model})""")
            assert report["checks"]["live2d_activity"]["modelVisible"]
            wall.screenshot(path=str(args.output / "wallpaper-work-live2d.png"))

            # Force the model through the CRT boundary and compare unmasked and
            # masked pixels in the actual character layer, including its grade.
            report["checks"]["wallpaper_polygon"] = wall.evaluate("""() => {
              const app=renderApp.getPixiApp(),layer=renderApp.getModelCharacterContainer();
              const adapter=renderApp._modelAdapter,oldLayout=adapter.config.layout;
              adapter.config.layout={scale:2.5,x:-.4,y:.25};adapter.setViewport(adapter.viewport);adapter.update(16);
              const mask=layer.mask,rt=PIXI.RenderTexture.create({width:app.screen.width,height:app.screen.height});
              layer.mask=null;app.renderer.render(layer,{renderTexture:rt,clear:true});
              const unclipped=app.renderer.extract.pixels(rt);
              layer.mask=mask;app.renderer.render(layer,{renderTexture:rt,clear:true});
              const clipped=app.renderer.extract.pixels(rt), points=wallpaperApp.scene._points;
              const inside=(x,y)=>{let c=false;for(let i=0,j=points.length-1;i<points.length;j=i++){
                const a=points[i],b=points[j];if((a.y>y)!=(b.y>y)&&x<(b.x-a.x)*(y-a.y)/(b.y-a.y)+a.x)c=!c;
              }return c;};
              let outsideBefore=0,outsideAfter=0,insideAfter=0;
              const w=app.screen.width,h=app.screen.height;
              for(let y=4;y<h-4;y+=4)for(let x=4;x<w-4;x+=4){
                const alpha=(y*w+x)*4+3;
                if(inside(x,y)){if(clipped[alpha]>100)insideAfter++;}
                else if(!inside(x-3,y)&&!inside(x+3,y)&&!inside(x,y-3)&&!inside(x,y+3)){
                  if(unclipped[alpha]>100)outsideBefore++;
                  if(clipped[alpha]>100)outsideAfter++;
                }
              }
              rt.destroy(true);adapter.config.layout=oldLayout;adapter.setViewport(adapter.viewport);
              return {outsideBefore,outsideAfter,insideAfter};
            }""")
            polygon = report["checks"]["wallpaper_polygon"]
            assert polygon["outsideBefore"] > 0 and polygon["outsideAfter"] == 0 and polygon["insideAfter"] > 100

            # Real per-page lifecycle: fixed listener count and destroyed RT/model.
            baseline_count = main_page.evaluate("()=>renderApp.getPixiApp().ticker.count")
            cycles = []
            for index in range(5):
                store.reload_revision += 1
                current = store.runtime_config("render", asset_server=server)
                main_page.evaluate("(config)=>renderApp.configureCharacter(config)", current)
                loaded = main_page.evaluate(READ)
                assert loaded["tickerCount"] == baseline_count
                sprite = {**current, "backend": "sprite"}
                main_page.evaluate("(config)=>renderApp.configureCharacter(config)", sprite)
                unloaded = main_page.evaluate(READ)
                assert not unloaded["model"] and not unloaded["texture"]
                assert unloaded["textureStats"]["residentBytes"] == 0
                main_page.evaluate("(config)=>renderApp.configureCharacter(config)", current)
                cycles.append({"loaded": loaded, "unloaded": unloaded, "rss_mb": usage()["rss_mb"]})
            report["reload_cycles"] = cycles
            main_page.set_viewport_size({"width": 600, "height": 800})
            main_page.wait_for_timeout(150)
            resized = main_page.evaluate(READ)
            assert resized["status"]["diagnostic"]["render_texture"]["width"] == 600
            assert resized["status"]["diagnostic"]["render_texture"]["height"] == 800
            report["checks"]["resize"] = resized
            main_page.evaluate("()=>renderApp._modelAdapter.setPaused(true)")
            frozen = main_page.evaluate("()=>renderApp._modelAdapter.model.elapsedTime")
            main_page.wait_for_timeout(200)
            assert main_page.evaluate("()=>renderApp._modelAdapter.model.elapsedTime") == frozen
            main_page.evaluate("()=>renderApp._modelAdapter.setPaused(false)")
            main_page.wait_for_timeout(100)
            assert main_page.evaluate("()=>renderApp._modelAdapter.model.elapsedTime") > frozen
            report["checks"]["pause_resume"] = True

            before = usage()
            wall.wait_for_timeout(1500)
            after = usage()
            report["dual_surface"] = {"rss_mb": after["rss_mb"],
                                      "cpu_seconds_per_second": (after["cpu_seconds"]-before["cpu_seconds"])/1.5}
            wall_context.close()
            wall_browser.close()
            context.close()
            browser.close()
    except Exception as exc:
        report["failure"] = type(exc).__name__ + ": " + str(exc)
    finally:
        host.stop()
        server.stop()
    required = {"both_ready", "actual_smile_parameters", "source_restoration_and_closed_tail",
                "live2d_activity", "wallpaper_polygon", "resize", "pause_resume"}
    report["completed_checks"] = sorted(report["checks"])
    report["success"] = "failure" not in report and not report["errors"] and required <= report["checks"].keys() and len(report.get("reload_cycles", [])) == 5
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key:value for key,value in report.items() if key not in {"reload_cycles","mouth_windows"}}, ensure_ascii=False), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
