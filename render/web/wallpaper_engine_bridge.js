(function () {
  "use strict";

  const WE_CLIENT_ID = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);

  // Resolved bridge port. null means it has not been discovered yet, with the
  // URL param used as the initial fallback.
  // Filled asynchronously by resolveBridgePort(); bridgePort() returns the real
  // port after that.
  var _resolvedBridgePort = null;
  var _resolvedAssetVersion = "";
  var _assetReloading = false;

  function bridgePort() {
    if (_resolvedBridgePort !== null) return _resolvedBridgePort;
    var params = new URLSearchParams(window.location.search || "");
    return params.get("bridgePort") || "17797";
  }

  // Ask the same-origin asset server for the actual bridge port.
  // The asset server port is stable because Lively/Wallpaper Engine stores it
  // in the page URL, while the bridge server may shift after port conflicts.
  // This endpoint keeps multi-port state consistent.
  async function resolveBridgePort() {
    try {
      var res = await fetch(
        window.location.origin + "/wallpaper/bridge-info",
        { cache: "no-store" }
      );
      var info = await res.json();
      if (info && info.assetVersion) _resolvedAssetVersion = String(info.assetVersion);
      if (info && Number.isFinite(Number(info.bridgePort))) {
        var discovered = String(info.bridgePort);
        var fromUrl = (new URLSearchParams(window.location.search || "")).get("bridgePort") || "17797";
        if (discovered !== fromUrl) {
          console.warn(
            "[WEBridge] bridge port mismatch: URL says " + fromUrl +
            " but server is on " + discovered + " — using discovered port"
          );
          try {
            clientLog("bridge.port_mismatch", { urlParam: fromUrl, actual: discovered }, "warning");
          } catch (e) {}
        }
        _resolvedBridgePort = discovered;
        window.__amadeusBridgePort = discovered;
        if (info.bridgeToken) window.__amadeusBridgeToken = String(info.bridgeToken);
        if ((!window.__amadeusBridgeToken || !_resolvedAssetVersion) && Number.isFinite(Number(info.assetPort))) {
          try {
            var assetOrigin = "http://127.0.0.1:" + String(info.assetPort);
            if (assetOrigin !== window.location.origin) {
              var assetRes = await fetch(assetOrigin + "/wallpaper/bridge-info", { cache: "no-store" });
              var assetInfo = await assetRes.json();
              if (assetInfo && assetInfo.bridgeToken) window.__amadeusBridgeToken = String(assetInfo.bridgeToken);
              if (assetInfo && assetInfo.assetVersion) _resolvedAssetVersion = String(assetInfo.assetVersion);
              if (assetInfo && Number.isFinite(Number(assetInfo.bridgePort))) {
                _resolvedBridgePort = String(assetInfo.bridgePort);
                window.__amadeusBridgePort = _resolvedBridgePort;
              }
            }
          } catch (err) {
            console.warn("[WEBridge] bridge token fallback failed:", err);
            try {
              clientLog("bridge.token_fallback_failed", { error: String(err && (err.stack || err)) }, "warning");
            } catch (e) {}
          }
        }
        return;
      }
    } catch (err) {
      // Old asset servers may not support this endpoint, or the fetch may fail.
      // Fall back to the URL param in that case.
      console.warn("[WEBridge] bridge-info fetch failed, using URL param:", err);
    }
    // Fallback: keep using bridgePort from the URL.
    _resolvedBridgePort = (new URLSearchParams(window.location.search || "")).get("bridgePort") || "17797";
    window.__amadeusBridgePort = _resolvedBridgePort;
  }

  function installAssetVersionWatchdog() {
    setInterval(async function () {
      if (_assetReloading) return;
      try {
        var res = await fetch(
          window.location.origin + "/wallpaper/bridge-info",
          { cache: "no-store" }
        );
        if (!res.ok && res.status !== 503) return;
        var info = await res.json();
        if ((!info.assetVersion || !info.bridgeToken) && Number.isFinite(Number(info.assetPort))) {
          var assetOrigin = "http://127.0.0.1:" + String(info.assetPort);
          if (assetOrigin !== window.location.origin) {
            try {
              var assetRes = await fetch(assetOrigin + "/wallpaper/bridge-info", { cache: "no-store" });
              if (assetRes.ok) info = Object.assign({}, info, await assetRes.json());
            } catch (err) {}
          }
        }
        var nextVersion = String(info && info.assetVersion || "");
        if (_resolvedAssetVersion && nextVersion && nextVersion !== _resolvedAssetVersion) {
          _assetReloading = true;
          clientLog("bridge.asset_version_changed", {
            previous: _resolvedAssetVersion,
            current: nextVersion,
          }, "warning");
          window.setTimeout(function () { window.location.reload(); }, 50);
          return;
        }
        if (nextVersion) _resolvedAssetVersion = nextVersion;

        var nextToken = String(info && info.bridgeToken || "");
        if (nextToken && nextToken !== String(window.__amadeusBridgeToken || "")) {
          window.__amadeusBridgeToken = nextToken;
          clientLog("bridge.action_token_refreshed", {});
        }

        if (info && Number.isFinite(Number(info.bridgePort))) {
          var nextPort = String(info.bridgePort);
          if (nextPort !== bridgePort()) {
            var previousPort = bridgePort();
            _resolvedBridgePort = nextPort;
            window.__amadeusBridgePort = nextPort;
            clientLog("bridge.runtime_port_changed", {
              previous: previousPort,
              current: nextPort,
            }, "warning");
            loadStateWithRetry(2).then(connectEvents);
          }
        }
      } catch (err) {
        // A server restart briefly makes discovery unavailable. The next poll
        // refreshes the token/port or reloads changed client assets.
      }
    }, 5000);
  }

  function flagEnabled(name) {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = String(params.get(name) || "").toLowerCase();
      return value === "1" || value === "true" || value === "yes" || value === "on";
    } catch (err) {
      return false;
    }
  }

  function flagDisabled(name) {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = String(params.get(name) || "").toLowerCase();
      return value === "0" || value === "false" || value === "no" || value === "off";
    } catch (err) {
      return false;
    }
  }

  function numberParam(name, fallback) {
    try {
      const params = new URLSearchParams(window.location.search || "");
      if (!params.has(name)) return fallback;
      const parsed = Number(params.get(name));
      return Number.isFinite(parsed) ? parsed : fallback;
    } catch (err) {
      return fallback;
    }
  }

  function weSpriteLiteEnabled() {
    if (flagDisabled("weSpriteLite")) return false;
    return flagEnabled("weSpriteLite");
  }

  function weSpriteDeferEnabled() {
    if (flagDisabled("weSpriteDefer")) return false;
    return flagEnabled("weSpriteDefer");
  }

  function normalizeEmotion(value) {
    return String(value || "").trim().toLowerCase();
  }

  function capUrlList(urls, limit) {
    if (!Array.isArray(urls)) return urls;
    if (!Number.isFinite(limit)) return urls;
    if (limit <= 0) return [];
    return urls.slice(0, Math.max(1, Math.floor(limit)));
  }

  const _weSpriteFrameUrls = Object.create(null);
  const _weSpriteLoadedEmotions = Object.create(null);
  let _weCurrentEmotion = "";
  let _weFallbackSpriteLoaded = false;

  function ensureWeSpriteEmotionLoaded(emotion, reason) {
    const key = normalizeEmotion(emotion);
    if (!key || _weSpriteLoadedEmotions[key]) return false;
    const urls = _weSpriteFrameUrls[key];
    const fn = window.wallpaperApp && window.wallpaperApp.loadSpriteFrames;
    if (!urls || !urls.length || typeof fn !== "function") {
      clientLog("sprite.defer_pending", {
        emotion: emotion,
        reason: reason || "",
        hasUrls: !!(urls && urls.length),
        hasMethod: typeof fn === "function",
      }, "warning");
      return false;
    }
    try {
      fn.call(window.wallpaperApp, emotion, urls);
      _weSpriteLoadedEmotions[key] = true;
      _weFallbackSpriteLoaded = true;
      clientLog("sprite.defer_load", {
        emotion: emotion,
        reason: reason || "",
        frames: urls.length,
      });
      return true;
    } catch (err) {
      clientLog("sprite.defer_load_failed", {
        emotion: emotion,
        reason: reason || "",
        error: String(err && (err.stack || err)),
      }, "error");
      return false;
    }
  }

  function cloneCall(call) {
    return JSON.parse(JSON.stringify(call || {}));
  }

  function endpoint(path) {
    return "http://127.0.0.1:" + bridgePort() + "/wallpaper/" + path;
  }

  function clientLog(event, data, level) {
    try {
      fetch(endpoint("client-log"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event: event,
          data: data || {},
          level: level || "info",
          ts: Date.now(),
          href: window.location.href,
          clientId: WE_CLIENT_ID,
        }),
        keepalive: true,
      }).catch(function () {});
    } catch (err) {}
  }

  window.__weBridgeLog = clientLog;
  window.addEventListener("error", function (ev) {
    clientLog("window.error", {
      message: ev.message || "",
      source: ev.filename || "",
      line: ev.lineno || 0,
      col: ev.colno || 0,
      error: String(ev.error && (ev.error.stack || ev.error)),
    }, "error");
  });
  window.addEventListener("unhandledrejection", function (ev) {
    clientLog("window.unhandled_rejection", {
      reason: String(ev.reason && (ev.reason.stack || ev.reason)),
    }, "error");
  });

  function waitForWallpaperApp(cb, attempt) {
    attempt = attempt || 0;
    if (window.wallpaperApp) {
      cb();
      return;
    }
    if (attempt > 400) {
      console.error("[WEBridge] wallpaperApp did not become ready");
      clientLog("wallpaperApp.timeout", { attempts: attempt }, "error");
      return;
    }
    setTimeout(function () { waitForWallpaperApp(cb, attempt + 1); }, 50);
  }

  var _lastPointer = { x: -1, y: -1, at: 0 };
  document.addEventListener("mousemove", function (ev) {
    _lastPointer.x = ev.clientX;
    _lastPointer.y = ev.clientY;
    _lastPointer.at = Date.now();
  }, { passive: true, capture: true });

  var _wheelStats = { received: 0, noPointer: 0, dispatched: 0 };

  function dispatchPointerWheel(payload) {
    // Wallpaper hosts never deliver wheel events; the server forwards desktop
    // wheel input and we replay it at the page's last known cursor position.
    _wheelStats.received += 1;
    if (_lastPointer.x < 0 || _lastPointer.y < 0) {
      _wheelStats.noPointer += 1;
      if (_wheelStats.noPointer <= 3 || _wheelStats.noPointer % 200 === 0) {
        clientLog("pointer_wheel.no_cursor", _wheelStats, "warning");
      }
      return;
    }
    var el = document.elementFromPoint(_lastPointer.x, _lastPointer.y);
    if (!el) return;
    _wheelStats.dispatched += 1;
    if (_wheelStats.dispatched <= 8 || _wheelStats.dispatched % 200 === 0) {
      clientLog("pointer_wheel.dispatched", {
        stats: _wheelStats,
        x: _lastPointer.x,
        y: _lastPointer.y,
        pointerAgeMs: _lastPointer.at ? (Date.now() - _lastPointer.at) : -1,
        target: String(el.className || el.tagName || "").slice(0, 80),
      });
    }
    el.dispatchEvent(new WheelEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: _lastPointer.x,
      clientY: _lastPointer.y,
      deltaX: Number(payload && payload.deltaX) || 0,
      deltaY: Number(payload && payload.deltaY) || 0,
      deltaMode: 0,
    }));
  }

  function applyCall(call) {
    if (!call || !call.method) return;
    if (call.method === "pointerWheel") {
      dispatchPointerWheel(call.args && call.args[0]);
      return;
    }
    call = cloneCall(call);
    const skipByFlag = {
      noCharacter: {
        setMode: true,
        loadSpriteFrames: true,
        loadSpriteClipFrames: true,
        setSpriteClipConfig: true,
        setIdleFrameIntervalMs: true,
        loadMouthConfig: true,
        setEmotion: true,
        setSpeaking: true,
        setMouth: true,
        holdSpriteFrame: true,
        clearSpriteHold: true,
        setSubtitle: true,
        loadLive2DModel: true,
        loadTransitionFrames: true,
      },
      noSpriteFrames: {
        loadSpriteFrames: true,
        loadSpriteClipFrames: true,
        loadTransitionFrames: true,
        setSpriteClipConfig: true,
        setIdleFrameIntervalMs: true,
      },
      noLive2D: {
        loadLive2DModel: true,
      },
      noMouth: {
        loadMouthConfig: true,
        setMouth: true,
      },
      noSpeaking: {
        setSpeaking: true,
      },
      noEmotion: {
        setEmotion: true,
      },
      noMode: {
        setMode: true,
      },
      noSubtitle: {
        setSubtitle: true,
      },
    };
    for (const flagName of Object.keys(skipByFlag)) {
      if (flagEnabled(flagName) && skipByFlag[flagName][call.method]) {
        clientLog("call.skipped_by_flag", { method: call.method, flag: flagName }, "warning");
        return;
      }
    }
    if (weSpriteLiteEnabled()) {
      if (call.method === "loadSpriteFrames") {
        const limit = numberParam("weSpriteFrameLimit", 1);
        const original = Array.isArray(call.args && call.args[1]) ? call.args[1].length : 0;
        call.args[1] = capUrlList(call.args && call.args[1], limit);
        const emotion = call.args && call.args[0];
        const key = normalizeEmotion(emotion);
        if (key) {
          _weSpriteFrameUrls[key] = call.args[1] || [];
        }
        clientLog("sprite.lite_frames", {
          emotion: emotion,
          original: original,
          kept: Array.isArray(call.args && call.args[1]) ? call.args[1].length : 0,
          limit: limit,
          deferred: weSpriteDeferEnabled(),
        });
        if (weSpriteDeferEnabled()) {
          if (key && key === _weCurrentEmotion) {
            ensureWeSpriteEmotionLoaded(emotion, "current_emotion");
          } else if (!_weFallbackSpriteLoaded) {
            ensureWeSpriteEmotionLoaded(emotion, "first_available");
          }
          return;
        }
      } else if (call.method === "loadSpriteClipFrames") {
        const limit = numberParam("weSpriteClipLimit", 1);
        const original = {
          in: Array.isArray(call.args && call.args[1]) ? call.args[1].length : 0,
          loop: Array.isArray(call.args && call.args[2]) ? call.args[2].length : 0,
          out: Array.isArray(call.args && call.args[3]) ? call.args[3].length : 0,
        };
        call.args[1] = capUrlList(call.args && call.args[1], limit);
        call.args[2] = capUrlList(call.args && call.args[2], limit);
        call.args[3] = capUrlList(call.args && call.args[3], limit);
        clientLog("sprite.lite_clip_frames", {
          emotion: call.args && call.args[0],
          original: original,
          kept: {
            in: Array.isArray(call.args && call.args[1]) ? call.args[1].length : 0,
            loop: Array.isArray(call.args && call.args[2]) ? call.args[2].length : 0,
            out: Array.isArray(call.args && call.args[3]) ? call.args[3].length : 0,
          },
          limit: limit,
        });
        if (!flagEnabled("weSpriteClips")) {
          clientLog("sprite.clip_skipped_for_we", { emotion: call.args && call.args[0] }, "warning");
          return;
        }
      } else if (call.method === "loadTransitionFrames") {
        const limit = numberParam("weSpriteTransitionLimit", 0);
        const original = Array.isArray(call.args && call.args[2]) ? call.args[2].length : 0;
        call.args[2] = capUrlList(call.args && call.args[2], limit);
        clientLog("sprite.lite_transition_frames", {
          from: call.args && call.args[0],
          to: call.args && call.args[1],
          original: original,
          kept: Array.isArray(call.args && call.args[2]) ? call.args[2].length : 0,
          limit: limit,
        });
        if (!Array.isArray(call.args && call.args[2]) || call.args[2].length === 0) {
          clientLog("sprite.transition_skipped_for_we", {
            from: call.args && call.args[0],
            to: call.args && call.args[1],
          }, "warning");
          return;
        }
      } else if (call.method === "setEmotion") {
        _weCurrentEmotion = normalizeEmotion(call.args && call.args[0]);
        ensureWeSpriteEmotionLoaded(call.args && call.args[0], "setEmotion");
      }
    }
    if (call.method === "initDesktopScene") {
      const payload = (call.args && call.args[0]) || {};
      if (flagEnabled("noScenario") && payload.scenario) {
        payload.scenario.enabled = false;
      }
      if (flagEnabled("noAmbient")) {
        payload.ambientUrl = "";
        payload.ambientLowUrl = "";
        payload.ambientDeltaUrl = "";
      }
      if (flagEnabled("noBackground")) {
        payload.backgroundUrl = "";
      }
      call.args = [payload];
    }
    const fn = window.wallpaperApp && window.wallpaperApp[call.method];
    if (typeof fn !== "function") {
      console.warn("[WEBridge] missing wallpaperApp method:", call.method);
      clientLog("call.missing_method", { method: call.method }, "warning");
      return;
    }
    try {
      if (call.method === "initDesktopScene") {
        const payload = (call.args && call.args[0]) || {};
        clientLog("call.initDesktopScene", {
          hasBackground: !!payload.backgroundUrl,
          hasScenario: !!(payload.scenario && payload.scenario.enabled),
        });
      }
      fn.apply(window.wallpaperApp, call.args || []);
    } catch (err) {
      console.error("[WEBridge] call failed:", call.method, err);
      clientLog("call.failed", { method: call.method, error: String(err && (err.stack || err)) }, "error");
    }
  }

  async function loadState() {
    const res = await fetch(endpoint("state"), { cache: "no-store" });
    const state = await res.json();
    (state.calls || []).forEach(applyCall);
  }

  function loadStateWithRetry(attemptsLeft) {
    return loadState().catch(function (err) {
      if (attemptsLeft > 0) {
        console.warn("[WEBridge] loadState failed, retrying in 1s... (" + attemptsLeft + " left)");
        clientLog("state.retry", { attemptsLeft: attemptsLeft, error: String(err) }, "warning");
        return new Promise(function (resolve) { setTimeout(resolve, 1000); })
          .then(function () { return loadStateWithRetry(attemptsLeft - 1); });
      }
      console.error("[WEBridge] loadState failed after all retries:", err);
      clientLog("state.failed", { error: String(err) }, "error");
    });
  }

  var _es = null;

  function connectEvents() {
    if (_es) { try { _es.close(); } catch (e) {} }
    _es = new EventSource(endpoint("events"));
    _es.onmessage = function (ev) {
      try {
        applyCall(JSON.parse(ev.data));
      } catch (err) {
        console.error("[WEBridge] bad event:", err);
        clientLog("event.bad_payload", { error: String(err) }, "error");
      }
    };
    _es.onerror = function () {
      console.warn("[WEBridge] event stream disconnected; browser will retry");
      clientLog("event.disconnected", {}, "warning");
    };
  }

  function patchPixiOpaque(attempt) {
    attempt = attempt || 0;
    try {
      var pixiApp = window.renderApp && window.renderApp.getPixiApp && window.renderApp.getPixiApp();
      if (!pixiApp || !pixiApp.renderer) {
        if (attempt === 0 || attempt === 5 || attempt === 20) {
          clientLog("pixi.opaque_pending", {
            attempt: attempt,
            hasRenderApp: !!window.renderApp,
            hasGetPixiApp: !!(window.renderApp && window.renderApp.getPixiApp),
            hasPixiApp: !!pixiApp,
          }, "warning");
        }
        if (attempt < 40) {
          setTimeout(function () { patchPixiOpaque(attempt + 1); }, 100);
        }
        return;
      }
      if (typeof pixiApp.renderer.backgroundColor !== "undefined") {
        pixiApp.renderer.backgroundColor = 0x05070b;
        pixiApp.renderer.backgroundAlpha = 1;
      }
      if (pixiApp.renderer.background) {
        pixiApp.renderer.background.color = 0x05070b;
        pixiApp.renderer.background.alpha = 1;
      }
      console.info("[WEBridge] Pixi canvas set to opaque for desktop rendering");
      clientLog("pixi.opaque", {
        attempt: attempt,
        screen: pixiApp.renderer.screen ? [pixiApp.renderer.screen.width, pixiApp.renderer.screen.height] : null,
        backgroundAlpha: pixiApp.renderer.backgroundAlpha,
        hasBackground: !!pixiApp.renderer.background,
      });
    } catch (err) {
      console.warn("[WEBridge] patchPixiOpaque failed:", err);
      clientLog("pixi.opaque_failed", { error: String(err && (err.stack || err)) }, "warning");
    }
  }

  function getPixiApp() {
    try {
      return window.renderApp && window.renderApp.getPixiApp && window.renderApp.getPixiApp();
    } catch (e) { return null; }
  }

  function restartTicker() {
    try {
      var pixiApp = getPixiApp();
      if (!pixiApp || !pixiApp.ticker) return;
      if (!pixiApp.ticker.started) {
        pixiApp.ticker.start();
        clientLog("ticker.restarted_after_visibility", {
          hidden: document.hidden,
          state: document.visibilityState,
        }, "warning");
      }
    } catch (err) {
      clientLog("ticker.restart_failed", { error: String(err) }, "warning");
    }
  }

  function patchTickerVisibility() {
    document.addEventListener("visibilitychange", function () {
      clientLog("visibility.change", {
        hidden: document.hidden,
        state: document.visibilityState,
        tickerStarted: !!(getPixiApp() && getPixiApp().ticker && getPixiApp().ticker.started),
      }, "warning");
      if (!document.hidden) return;
      restartTicker();
      if (typeof queueMicrotask === "function") queueMicrotask(restartTicker);
      setTimeout(restartTicker, 0);
      setTimeout(restartTicker, 100);
    }, true);
  }

  function installPixiTickerWatchdog() {
    setInterval(function () {
      try {
        var pixiApp = getPixiApp();
        if (!pixiApp || !pixiApp.ticker) return;
        if (!pixiApp.ticker.started) {
          pixiApp.ticker.start();
          clientLog("ticker.watchdog_restart", {
            hidden: document.hidden,
            state: document.visibilityState,
          }, "warning");
        }
      } catch (err) {
        clientLog("ticker.watchdog_failed", { error: String(err) }, "warning");
      }
    }, 1000);
  }

  function patchWallpaperRaf() {
    if (typeof window.wallpaperRequestAnimationFrame !== "function") {
      clientLog("raf.wallpaper_api_unavailable", {}, "warning");
      return;
    }
    var _native = window.requestAnimationFrame;
    window.requestAnimationFrame = function (cb) {
      return window.wallpaperRequestAnimationFrame(cb);
    };
    window.cancelAnimationFrame = function () {};
    clientLog("raf.patched_to_wallpaper");
    // Restart PIXI ticker so it picks up the patched rAF
    var pixiApp = getPixiApp();
    if (pixiApp && pixiApp.ticker) {
      pixiApp.ticker.stop();
      pixiApp.ticker.start();
      clientLog("ticker.restarted_with_wallpaper_raf");
    }
  }

  function installAliveHeartbeat() {
    setInterval(function () {
      try {
        var pixiApp = getPixiApp();
        clientLog("runtime.alive", {
          hidden: document.hidden,
          state: document.visibilityState,
          weRaf: typeof window.wallpaperRequestAnimationFrame === "function",
          tickerStarted: !!(pixiApp && pixiApp.ticker && pixiApp.ticker.started),
          time: Date.now(),
        });
      } catch (err) {}
    }, 5000);
  }

  function installProbes() {
    var intervalCount = 0;
    var rafCount = 0;
    var pixiTickCount = 0;

    // Probe A: setInterval — reports both itself and rafCount so we can compare
    setInterval(function () {
      intervalCount++;
      try {
        clientLog("probe.interval", {
          intervalCount: intervalCount,
          rafCount: rafCount,
          pixiTickCount: pixiTickCount,
          hidden: document.hidden,
          now: Date.now(),
        });
      } catch (err) {}
    }, 1000);

    // Probe B: requestAnimationFrame loop
    function rafLoop() {
      rafCount++;
      requestAnimationFrame(rafLoop);
    }
    requestAnimationFrame(rafLoop);

    // Probe C: PIXI ticker listener
    try {
      var pixiApp = getPixiApp();
      if (pixiApp && pixiApp.ticker) {
        pixiApp.ticker.add(function () { pixiTickCount++; });
      }
    } catch (err) {}
  }

  function installCanvasPixelProbe() {
    if (!flagEnabled("canvasProbe")) {
      clientLog("canvas.probe_skipped", { enabled: false });
      return;
    }
    var probeCanvas = document.createElement("canvas");
    probeCanvas.width = 24;
    probeCanvas.height = 14;
    var ctx = probeCanvas.getContext("2d", { willReadFrequently: true });
    setInterval(function () {
      try {
        var canvas = document.querySelector("#canvas-container canvas");
        if (!canvas || !ctx) {
          clientLog("canvas.probe", { present: !!canvas, readable: !!ctx }, "warning");
          return;
        }
        ctx.clearRect(0, 0, probeCanvas.width, probeCanvas.height);
        ctx.drawImage(canvas, 0, 0, canvas.width, canvas.height, 0, 0, probeCanvas.width, probeCanvas.height);
        var data = ctx.getImageData(0, 0, probeCanvas.width, probeCanvas.height).data;
        var sum = 0;
        var max = 0;
        var nonBlack = 0;
        var pixels = data.length / 4;
        for (var i = 0; i < data.length; i += 4) {
          var lum = (data[i] + data[i + 1] + data[i + 2]) / 3;
          sum += lum;
          if (lum > max) max = lum;
          if (lum > 6) nonBlack++;
        }
        clientLog("canvas.probe", {
          present: true,
          width: canvas.width,
          height: canvas.height,
          clientWidth: canvas.clientWidth,
          clientHeight: canvas.clientHeight,
          avg: Math.round((sum / Math.max(1, pixels)) * 10) / 10,
          max: Math.round(max * 10) / 10,
          nonBlack: nonBlack,
          pixels: pixels,
        });
      } catch (err) {
        clientLog("canvas.probe_failed", { error: String(err && (err.stack || err)) }, "warning");
      }
    }, 2000);
    clientLog("canvas.probe_installed");
  }

  function installDomSentinel() {
    try {
      var el = document.createElement("div");
      el.id = "we-dom-sentinel";
      el.textContent = "WE DOM 0";
      el.style.position = "fixed";
      el.style.top = "10px";
      el.style.right = "10px";
      el.style.zIndex = "2147483647";
      el.style.padding = "4px 7px";
      el.style.border = "1px solid rgba(120, 255, 210, 0.75)";
      el.style.borderRadius = "4px";
      el.style.background = "rgba(0, 18, 22, 0.72)";
      el.style.color = "#86ffd7";
      el.style.font = "12px/1.2 Consolas, monospace";
      el.style.pointerEvents = "none";
      el.style.textShadow = "0 0 6px rgba(80, 255, 210, 0.75)";
      el.style.mixBlendMode = "normal";
      document.body.appendChild(el);
      var count = 0;
      setInterval(function () {
        count++;
        el.textContent = "WE DOM " + count;
        el.style.color = count % 2 ? "#86ffd7" : "#ffd36f";
        el.style.borderColor = count % 2 ? "rgba(120, 255, 210, 0.75)" : "rgba(255, 211, 111, 0.75)";
      }, 1000);
      clientLog("dom.sentinel_installed", { id: el.id });
    } catch (err) {
      clientLog("dom.sentinel_failed", { error: String(err && (err.stack || err)) }, "warning");
    }
  }

  async function initBridge() {
    // 1. Discover the real bridge port first. The asset server port is stable,
    //    but the bridge port may shift after conflicts.
    await resolveBridgePort();
    installAssetVersionWatchdog();

    // 2. Apply renderer patches. This is port-independent and can run in parallel.
    patchPixiOpaque();
    patchWallpaperRaf();
    patchTickerVisibility();
    installPixiTickerWatchdog();
    if (flagEnabled("debugSentinel")) installDomSentinel();
    if (flagEnabled("debugHeartbeat")) installAliveHeartbeat();
    if (flagEnabled("debugProbes")) installProbes();
    installCanvasPixelProbe();

    if (flagEnabled("noBridgeState")) {
      clientLog("bridge.state_skipped_by_flag", { flag: "noBridgeState" }, "warning");
      return;
    }

    // 3. Fetch initial state with the resolved port, then establish the SSE stream.
    loadStateWithRetry(5)
      .then(function () {
        if (flagEnabled("noEvents")) {
          clientLog("bridge.events_skipped_by_flag", { flag: "noEvents" }, "warning");
          return;
        }
        connectEvents();
      })
      .then(function () {
        console.info("[WEBridge] connected, bridgePort=" + bridgePort());
        clientLog("bridge.connected", { bridgePort: bridgePort() });
      })
      .catch(function (err) {
        console.error("[WEBridge] unexpected error:", err);
        clientLog("bridge.unexpected_error", { error: String(err) }, "error");
      });
  }

  waitForWallpaperApp(function () {
    initBridge();
  });
})();
