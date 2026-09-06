(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search || "");
  const surface = window.createCrtCanvasSurface();
  const keyboardComposer = window.createWallpaperKeyboardComposer
    ? window.createWallpaperKeyboardComposer()
    : null;
  let bridgePort = normalizePort(params.get("bridgePort"));
  let sliceBounds = null;
  let canvasBounds = null;
  let keyboardInputToggleBounds = null;
  let keyboardComposerBounds = null;
  let eventSource = null;
  let shapeFrame = 0;
  let shapeFlushTimer = 0;
  let shapeRetryTimer = 0;

  function normalizePort(value) {
    const port = Number(value);
    return Number.isInteger(port) && port > 0 && port <= 65535 ? String(port) : "";
  }

  function bridgeEndpoint(path) {
    return "http://127.0.0.1:" + bridgePort + "/wallpaper/" + path;
  }

  function normalizeBounds(value) {
    if (!value || typeof value !== "object") return null;
    const bounds = {
      x: Number(value.x),
      y: Number(value.y),
      width: Number(value.width),
      height: Number(value.height),
    };
    return Object.values(bounds).every(Number.isFinite) && bounds.width > 0 && bounds.height > 0
      ? bounds
      : null;
  }

  function projectToSlice(bounds) {
    if (!bounds || !sliceBounds || !sliceBounds.width || !sliceBounds.height) return null;
    return {
      x: (bounds.x - sliceBounds.x) / sliceBounds.width * window.innerWidth,
      y: (bounds.y - sliceBounds.y) / sliceBounds.height * window.innerHeight,
      width: bounds.width / sliceBounds.width * window.innerWidth,
      height: bounds.height / sliceBounds.height * window.innerHeight,
    };
  }

  function layoutSurface() {
    const canvas = projectToSlice(canvasBounds);
    if (canvas) surface.layout(canvas);
    const toggle = projectToSlice(keyboardInputToggleBounds);
    const composer = projectToSlice(keyboardComposerBounds);
    if (keyboardComposer && toggle && composer) keyboardComposer.layout(toggle, composer);
    scheduleShapeUpdate();
  }

  function applyCall(call) {
    if (!call || typeof call !== "object") return;
    if (call.method === "setCanvas") {
      surface.setPayload((call.args && call.args[0]) || {});
      scheduleShapeUpdate();
    } else if (call.method === "toggleCanvas") {
      surface.toggle();
      scheduleShapeUpdate();
    } else if (call.method === "setCanvasPresentation") {
      surface.setPresentation((call.args && call.args[0]) || {});
      scheduleShapeUpdate();
    } else if (call.method === "setAttention") {
      surface.setAttention((call.args && call.args[0]) || {});
      scheduleShapeUpdate();
    }
  }

  function visibleInteractiveRects() {
    const targets = [
      { selector: ".crt-canvas-surface-dot", padding: 10 },
      { selector: ".crt-canvas-surface-status", padding: 10 },
      { selector: ".crt-canvas-surface-card", padding: 52 },
      { selector: "#wallpaper-keyboard-toggle:not([hidden])", padding: 4 },
      { selector: "#wallpaper-keyboard-composer:not([hidden])", padding: 4 },
    ];
    return targets.flatMap(({ selector, padding }) => (
      Array.from(document.querySelectorAll(selector)).map((element) => ({ element, padding }))
    )).map(({ element, padding }) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (
        style.display === "none"
        || style.visibility === "hidden"
        || style.pointerEvents === "none"
        || rect.width <= 0
        || rect.height <= 0
      ) return null;
      const left = Math.max(0, Math.floor(rect.left - padding));
      const top = Math.max(0, Math.floor(rect.top - padding));
      const right = Math.min(window.innerWidth, Math.ceil(rect.right + padding));
      const bottom = Math.min(window.innerHeight, Math.ceil(rect.bottom + padding));
      return { x: left, y: top, width: right - left, height: bottom - top };
    }).filter((rect) => rect && rect.width > 0 && rect.height > 0);
  }

  function flushShapeUpdate() {
    if (shapeFrame) window.cancelAnimationFrame(shapeFrame);
    if (shapeFlushTimer) window.clearTimeout(shapeFlushTimer);
    shapeFrame = 0;
    shapeFlushTimer = 0;
    const api = window.amadeus;
    if (!api || typeof api.setElectronSliceShape !== "function") return;
    Promise.resolve(api.setElectronSliceShape(visibleInteractiveRects())).then((accepted) => {
      if (accepted === true || shapeRetryTimer) return;
      shapeRetryTimer = window.setTimeout(() => {
        shapeRetryTimer = 0;
        scheduleShapeUpdate();
      }, 250);
    }, () => {
      if (shapeRetryTimer) return;
      shapeRetryTimer = window.setTimeout(() => {
        shapeRetryTimer = 0;
        scheduleShapeUpdate();
      }, 250);
    });
  }

  function scheduleShapeUpdate() {
    if (shapeFrame || shapeFlushTimer) return;
    shapeFrame = window.requestAnimationFrame(flushShapeUpdate);
    // Chromium may suspend rAF while a transparent BrowserWindow is hidden.
    // The timeout breaks the readiness deadlock without polling after commit.
    shapeFlushTimer = window.setTimeout(flushShapeUpdate, 50);
  }

  async function resolveBridge() {
    const response = await fetch(window.location.origin + "/wallpaper/bridge-info", { cache: "no-store" });
    if (!response.ok) throw new Error("bridge discovery failed: HTTP " + response.status);
    const info = await response.json();
    bridgePort = normalizePort(info.bridgePort) || bridgePort;
    if (!bridgePort) throw new Error("wallpaper bridge is unavailable");
    sliceBounds = normalizeBounds(info.sliceBounds);
    canvasBounds = normalizeBounds(info.canvasBounds);
    keyboardInputToggleBounds = normalizeBounds(info.keyboardInputToggleBounds);
    keyboardComposerBounds = normalizeBounds(info.keyboardComposerBounds);
    window.__amadeusBridgePort = bridgePort;
    window.__amadeusBridgeToken = String(info.bridgeToken || "");
    if (keyboardComposer) keyboardComposer.configure(bridgePort, window.__amadeusBridgeToken);
    layoutSurface();
  }

  async function loadCanvasState() {
    const response = await fetch(bridgeEndpoint("canvas-state"), { cache: "no-store" });
    if (!response.ok) throw new Error("canvas state failed: HTTP " + response.status);
    const state = await response.json();
    (Array.isArray(state.calls) ? state.calls : []).forEach(applyCall);
  }

  function connectCanvasEvents() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(bridgeEndpoint("canvas-events"));
    eventSource.onmessage = (event) => {
      try {
        applyCall(JSON.parse(event.data));
      } catch (error) {
        console.warn("[ElectronSlice] ignored malformed canvas event", error);
      }
    };
  }

  async function start() {
    layoutSurface();
    const root = document.body;
    if (root) {
      new MutationObserver(scheduleShapeUpdate).observe(root, {
        attributes: true,
        childList: true,
        subtree: true,
      });
      if (typeof ResizeObserver === "function") {
        new ResizeObserver(scheduleShapeUpdate).observe(root);
      }
    }
    window.addEventListener("resize", layoutSurface, { passive: true });
    await resolveBridge();
    await loadCanvasState();
    connectCanvasEvents();
    scheduleShapeUpdate();
  }

  start().catch((error) => {
    console.error("[ElectronSlice] startup failed", error);
    window.setTimeout(() => window.location.reload(), 2000);
  });
})();
