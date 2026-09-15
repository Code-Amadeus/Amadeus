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
  const MIN_SUPPORTED_RESOLUTION = 0.25;
  const MAX_SUPPORTED_RESOLUTION = 4;

  function supportedFps(value) {
    const fps = Number(value);
    return Number.isFinite(fps) && fps >= MIN_SUPPORTED_FPS && fps <= MAX_SUPPORTED_FPS
      ? fps
      : null;
  }

  function supportedResolution(value) {
    if (value === null || value === undefined || value === "") return null;
    const resolution = Number(value);
    return Number.isFinite(resolution)
      && resolution >= MIN_SUPPORTED_RESOLUTION
      && resolution <= MAX_SUPPORTED_RESOLUTION
      ? resolution
      : null;
  }

  function resolveRenderBudget(options) {
    const devicePixelRatio = Number(options && options.devicePixelRatio);
    const nativeResolution = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0
      ? devicePixelRatio
      : 1;
    const maxResolution = supportedResolution(options && options.maxResolution);
    return {
      maxFps: supportedFps(options && options.maxFps) || STANDARD_MAX_FPS,
      resolution: maxResolution === null
        ? nativeResolution
        : Math.min(nativeResolution, maxResolution),
    };
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
    MIN_SUPPORTED_RESOLUTION,
    MAX_SUPPORTED_RESOLUTION,
    supportedFps,
    supportedResolution,
    resolveRenderBudget,
    createFrameRateController,
    installWallpaperEngineListener,
  };
});
