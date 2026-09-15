(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.RenderBudget = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const MIN_SUPPORTED_FPS = 10;
  const MAX_SUPPORTED_FPS = 240;
  const STANDARD_MAX_FPS = 60;

  function supportedFps(value) {
    const fps = Number(value);
    return Number.isFinite(fps) && fps >= MIN_SUPPORTED_FPS && fps <= MAX_SUPPORTED_FPS
      ? fps
      : null;
  }

  function createFrameRateController(ticker, configuredMaxFps) {
    const projectMaxFps = supportedFps(configuredMaxFps) || STANDARD_MAX_FPS;
    let hostMaxFps = null;

    function apply() {
      const effectiveMaxFps = hostMaxFps === null
        ? projectMaxFps
        : Math.min(projectMaxFps, hostMaxFps);
      ticker.maxFPS = effectiveMaxFps;
      return effectiveMaxFps;
    }

    return {
      projectMaxFps,
      setHostMaxFps(value) {
        hostMaxFps = supportedFps(value);
        return apply();
      },
      apply,
    };
  }

  function installWallpaperEngineListener(target, controller) {
    const listener = target.wallpaperPropertyListener || {};
    const previous = listener.applyGeneralProperties;
    listener.applyGeneralProperties = function (properties) {
      try {
        if (typeof previous === "function") previous.call(this, properties);
      } finally {
        controller.setHostMaxFps(properties && properties.fps);
      }
    };
    target.wallpaperPropertyListener = listener;
  }

  return {
    MIN_SUPPORTED_FPS,
    MAX_SUPPORTED_FPS,
    STANDARD_MAX_FPS,
    supportedFps,
    createFrameRateController,
    installWallpaperEngineListener,
  };
});
