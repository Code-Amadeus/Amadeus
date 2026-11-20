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
    const maxFps = supportedFps(options && options.maxFps) || STANDARD_MAX_FPS;
    const sampling = options?.textureSampling;
    return {
      maxFps,
      textureSampling: sampling === undefined || sampling === null || sampling === '' ? maxFps === 60
        : sampling === true || sampling === "1" || sampling === "true",
      resolution: maxResolution === null
        ? nativeResolution
        : Math.min(nativeResolution, maxResolution),
    };
  }

  // Present on the closest available vsync, carrying fractional time forward.
  // Only whole missed periods are discarded after a stall; clipping ordinary
  // overshoot loses time on displays whose refresh rate is not a multiple of FPS.
  function createPresentGate() {
    let last = null;
    let balance = 0;
    const deltas = [];
    return {
      reset() { last = null; balance = 0; deltas.length = 0; },
      shouldPresent(time, maxFps) {
        if (last === null || time < last) { last = time; balance = 0; return true; }
        const dt = time - last;
        if (!(dt > 0)) return false;
        last = time;
        deltas.push(dt);
        if (deltas.length > 31) deltas.shift();
        const sorted = deltas.slice().sort((a, b) => a - b);
        const vsync = sorted[sorted.length >> 1];
        const period = 1000 / maxFps;
        balance += dt;
        if (balance < period - Math.min(vsync, period) / 2) return false;
        balance -= period;
        if (balance >= period) balance %= period;
        return true;
      },
    };
  }

  function createFrameRateController(ticker, configuredMaxFps) {
    const projectMaxFps = supportedFps(configuredMaxFps) || STANDARD_MAX_FPS;
    let hostMaxFps = null;
    let effectiveMaxFps = projectMaxFps;
    const gate = createPresentGate();
    const update = ticker.update;
    ticker.update = function (time = performance.now()) {
      if (gate.shouldPresent(time, effectiveMaxFps)) update.call(this, time);
    };

    function apply() {
      const nextMaxFps = hostMaxFps === null
        ? projectMaxFps
        : Math.min(projectMaxFps, hostMaxFps);
      if (nextMaxFps !== effectiveMaxFps) gate.reset();
      effectiveMaxFps = nextMaxFps;
      ticker.maxFPS = 0;
      return effectiveMaxFps;
    }

    return {
      projectMaxFps,
      get effectiveMaxFps() { return effectiveMaxFps; },
      setHostMaxFps(value) {
        hostMaxFps = supportedFps(value);
        return apply();
      },
      apply,
    };
  }

  // Keep source indices and duration intact; only choose which images need decoding.
  function createFrameSamplingPlan(frameCount, intervalMs, maxFps, requiredIndices = []) {
    const count = Math.max(0, Math.floor(frameCount));
    const samples = Math.min(count, Math.max(1, Math.ceil(count * intervalMs * maxFps / 1000)));
    const retained = new Set();
    const timelineIndices = [];
    for (let i = 0; i < samples; i++) {
      const index = Math.floor(i * count / samples);
      retained.add(index);
      timelineIndices.push(index);
    }
    if (count) { retained.add(0); retained.add(count - 1); }
    for (const index of requiredIndices) {
      if (Number.isInteger(index) && index >= 0 && index < count) retained.add(index);
    }
    const indices = Array.from(retained).sort((a, b) => a - b);
    const sourceIndex = new Uint32Array(count);
    let cursor = 0;
    for (let i = 0; i < count; i++) {
      while (cursor + 1 < indices.length && Math.abs(indices[cursor + 1] - i) < Math.abs(indices[cursor] - i)) cursor++;
      sourceIndex[i] = indices[cursor];
    }
    return { indices, sourceIndex, timelineIndices, sampleIntervalMs: samples ? count * intervalMs / samples : 0 };
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
    createPresentGate,
    createFrameSamplingPlan,
    installWallpaperEngineListener,
  };
});
