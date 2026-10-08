"""Bounded, local-only qualification of the optional Cubism renderer.

Restricted Core/model files stay outside the public source tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from render.server import AssetServer
from playwright.sync_api import sync_playwright

HTML = """<!doctype html><html><body style="margin:0;background:#263442">
<script src="/render/web/vendor/pixi.min.js"></script>
<script src="/probe-core/live2dcubismcore.min.js"></script>
<script src="/render/web/vendor/pixi-live2d-display.cubism4.min.js"></script>
<script>
window.probeReady = (async () => {
  const app = new PIXI.Application({width:800,height:700,backgroundAlpha:0,preserveDrawingBuffer:true});
  document.body.appendChild(app.view);
  const container = new PIXI.Container(); app.stage.addChild(container);
  const mask = new PIXI.Graphics().beginFill(0xffffff).drawRect(120,60,560,620).endFill();
  app.stage.addChild(mask); container.mask=mask;
  const model = await PIXI.live2d.Live2DModel.from("/probe-model/ENTRY",{
    autoUpdate:false,autoInteract:false,motionPreload:"ALL",idleMotionGroup:"ProbeNoIdle"
  });
  container.addChild(model);
  const original={width:model.internalModel.width,height:model.internalModel.height};
  model.anchor.set(.5,1);model.position.set(400,680);
  model.scale.set(Math.min(560/original.width,620/original.height));
  let mouth=0; let appliedMouth=0; let values={};
  model.internalModel.on("beforeModelUpdate",()=>{
    model.internalModel.coreModel.setParameterValueById("PARAM_MOUTH_OPEN_Y",mouth); appliedMouth=model.internalModel.coreModel.getParameterValueById("PARAM_MOUTH_OPEN_Y");
    values=Object.fromEntries(model.internalModel.coreModel._model.parameters.ids.map(id=>[id,model.internalModel.coreModel.getParameterValueById(id)]));
  });
  app.ticker.add(()=>model.update(app.ticker.deltaMS));
  window.probe={app,model,mask,container,original,getValues(){return values;},getMouth(){return appliedMouth;},setMouth(v){mouth=v;}};
  return {pixi:PIXI.VERSION,adapter:PIXI.live2d.VERSION,core:Live2DCubismCore.Version.csmGetVersion(),
    original,parameters:model.internalModel.coreModel._model.parameters.ids,
    lipSync:model.internalModel.settings.getLipSyncParameters()};
})();</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--browser", default="C:/Program Files/Google/Chrome/Application/chrome.exe")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/live2d-p0")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    html_path = args.output / "probe.html"
    html_path.write_text(HTML.replace("ENTRY", args.model.name), encoding="utf-8")
    server = AssetServer(ROOT, start_port=18170)
    server.mount_static("/probe-core", args.core.parent)
    server.mount_static("/probe-model", args.model.parent)
    port = server.start()
    report = {"errors": [], "console": [], "expressions": {}}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=args.browser, headless=True,
                args=["--enable-webgl", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
                timeout=15000,
            )
            page = browser.new_page(viewport={"width":800,"height":700})
            page.set_default_timeout(20000)
            page.on("pageerror", lambda error: report["errors"].append(str(error)))
            page.on("console", lambda message: report["console"].append(message.text[:500]))
            page.goto(f"http://127.0.0.1:{port}/{html_path.relative_to(ROOT).as_posix()}")
            report["versions"] = page.evaluate("() => window.probeReady")
            page.wait_for_timeout(1300)
            for name in ("Angry", "Disappointed", "Smile", "Thinking"):
                ok = page.evaluate("(name)=>probe.model.expression(name)", name)
                page.wait_for_timeout(1200)
                screenshot = page.screenshot(path=str(args.output / f"{name.lower()}.png"))
                report["expressions"][name] = {"accepted":ok,"pixel_sha256":hashlib.sha256(screenshot).hexdigest(),"parameters":page.evaluate("()=>probe.getValues()")}
            for value in (0, .8, 0):
                page.evaluate("(v)=>probe.setMouth(v)", value)
                page.wait_for_timeout(150)
                actual = page.evaluate("()=>probe.getMouth()")
                report.setdefault("mouth", []).append({"target":value,"actual":actual})
                page.screenshot(path=str(args.output / f"mouth-{value}.png"))
            report["mask"] = page.evaluate("""() => {
              const gl=probe.app.renderer.gl;
              const alpha=(x,y)=>{probe.app.render();gl.bindFramebuffer(gl.FRAMEBUFFER,null);const pixel=new Uint8Array(4);gl.readPixels(x,700-y,1,1,gl.RGBA,gl.UNSIGNED_BYTE,pixel);return pixel[3];};
              probe.container.mask=null;
              const unmasked=alpha(530,500);
              probe.mask.clear().beginFill(0xffffff).drawRect(120,60,560,400).endFill();
              probe.container.mask=probe.mask;
              const directRect=alpha(530,500);
              probe.container.removeChild(probe.model);
              const source=new PIXI.Container();source.addChild(probe.model);
              const rt=PIXI.RenderTexture.create({width:800,height:700});
              probe.app.renderer.render(source,{renderTexture:rt,clear:true});
              probe.container.addChild(new PIXI.Sprite(rt));
              const textureRect=alpha(530,500);
              probe.mask.clear().beginFill(0xffffff).drawPolygon([120,60,680,60,680,650,120,350]).endFill();
              const texturePolygon={outside:alpha(430,600),inside:alpha(530,400)};
              return {unmasked,directRect,textureRect,texturePolygon};
            }""")
            page.screenshot(path=str(args.output / "mask-render-texture.png"))
            page.evaluate("()=>{probe.model.destroy({children:true,texture:true,baseTexture:true});probe.app.destroy(true);}")
            report["destroyed"] = page.evaluate("()=>probe.model.destroyed")
            browser.close()
    except Exception as error:
        report["failure"] = str(error)
    finally:
        server.stop()
    report["elapsed_condition"] = "800x700; headless Chrome; SwiftShader; one Pixi canvas"
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    success = not report.get("failure") and not report["errors"]
    success = success and all(item["accepted"] for item in report["expressions"].values())
    success = success and len(report["expressions"]) == 4
    success = success and all(abs(item["target"] - item["actual"]) < .001 for item in report.get("mouth", []))
    success = success and report.get("mask", {}).get("unmasked",0) > 0 and report.get("mask", {}).get("textureRect") == 0 and report.get("mask", {}).get("texturePolygon",{}).get("outside") == 0 and report.get("mask", {}).get("texturePolygon",{}).get("inside",0) > 0
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
