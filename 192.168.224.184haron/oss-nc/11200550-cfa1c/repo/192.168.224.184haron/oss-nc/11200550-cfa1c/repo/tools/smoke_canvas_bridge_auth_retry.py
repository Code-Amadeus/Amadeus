from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = ROOT / "render" / "web" / "crt_canvas_surface.js"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(
            """
const window = globalThis;
const calls = { bridgeInfo: 0, canvasAction: 0, unauthorized: 0, authorized: 0 };
window.location = { origin: "http://127.0.0.1:17777", search: "" };
window.__amadeusBridgePort = "17797";
window.__amadeusBridgeToken = "stale-token";
window.__weBridgeLog = function () {};
window.setInterval = function () {};
window.setTimeout = function () {};
window.clearTimeout = function () {};
window.localStorage = { getItem() { return null; }, setItem() {} };
window.document = {
  getElementById() { return null; },
  createElement() { return element(); },
  head: { appendChild() {} },
  body: { appendChild() {}, removeChild() {} },
};
globalThis.navigator = {};
function element() {
  return {
    innerHTML: "",
    textContent: "",
    id: "",
    style: { setProperty() {} },
    classList: { remove() {}, add() {}, contains() { return false; }, toggle() {} },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    appendChild() {},
  };
}
globalThis.fetch = async function (url, options) {
  const text = String(url || "");
  if (text.includes("/wallpaper/bridge-info")) {
    calls.bridgeInfo += 1;
    return new Response(JSON.stringify({
      bridgePort: "17797",
      assetPort: "17777",
      bridgeToken: "fresh-token"
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  if (text.includes("/wallpaper/canvas-action")) {
    calls.canvasAction += 1;
    const token = options && options.headers && options.headers["X-Amadeus-Bridge-Token"];
    if (token !== "fresh-token") {
      calls.unauthorized += 1;
      return new Response(JSON.stringify({ ok: false, error: "unauthorized" }), {
        status: 403,
        headers: { "Content-Type": "application/json" }
      });
    }
    calls.authorized += 1;
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    });
  }
  throw new Error("unexpected fetch url: " + text);
};
"""
        )
        tmp.write(source.read_text(encoding="utf-8"))
        tmp.write(
            """
const root = {
  clientWidth: 800,
  clientHeight: 600,
  style: { setProperty() {} },
  appendChild() {},
};
const surface = window.createCrtCanvasSurface(root);
surface.setPayload({
  mode: "browser",
  phase: "Preview",
  title: "Auth Retry Page",
  url: "https://example.com/",
  browserSessionId: "browser_auth_retry",
  links: [],
  open: true
});
surface.__testPostCanvasAction("url", "open", { url: "https://example.com/" })
  .then(() => {
    if (calls.unauthorized !== 1 || calls.authorized !== 1 || calls.bridgeInfo !== 1) {
      throw new Error("unexpected call counts: " + JSON.stringify(calls));
    }
    console.log("canvas bridge auth retry smoke ok");
    process.exit(0);
  })
  .catch((err) => {
    console.error(err && (err.stack || err));
    process.exit(1);
  });
"""
        )

    try:
        text = tmp_path.read_text(encoding="utf-8")
        marker = "      toggle() {\n"
        text = text.replace(marker, "      __testPostCanvasAction: postCanvasAction,\n" + marker, 1)
        tmp_path.write_text(text, encoding="utf-8")
        result = subprocess.run(["node", str(tmp_path)], cwd=str(ROOT), text=True, capture_output=True, timeout=15)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)
        print(result.stdout.strip())
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
