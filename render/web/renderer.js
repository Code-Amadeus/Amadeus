/**
 * renderer.js — PixiJS render engine core
 *
 * Exposes window.renderApp for Python-side runJavaScript() calls:
 *   renderApp.setEmotion(emotion)
 *   renderApp.setSpeaking(bool)
 *   renderApp.setMouth(value 0-1)
 *   renderApp.setSubtitle(text)
 *   renderApp.loadSpriteFrames(emotion, [url, ...])
 *   renderApp.loadLive2DModel(url)
 *   renderApp.loadTransitionFrames(fromEmotion, toEmotion, [url, ...])
 *   renderApp.setMode('sprite'|'live2d'|'both')
 *
 * Deprecated compatibility boundary:
 * - SpriteRenderer and Live2DRenderer are the legacy foreground character paths.
 * - New wallpaper/work behavior should prefer SpriteForgeRuntime or
 *   wallpaper_scene.js scenario activities instead of adding new VTS/single
 *   sprite state here.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // PixiJS Application
  // ---------------------------------------------------------------------------
  const renderParams = new URLSearchParams(window.location.search || "");
  const graphicsProfile = renderParams.get("graphicsProfile") || "standard";
  const renderBudget = window.RenderBudget.resolveRenderBudget({
    maxFps: renderParams.get("renderMaxFps"),
    maxResolution: renderParams.get("renderMaxResolution"),
    textureSampling: renderParams.get("renderTextureSampling"),
    devicePixelRatio: window.devicePixelRatio,
  });
  const app = new PIXI.Application({
    resizeTo: document.getElementById("canvas-container"),
    backgroundAlpha: 0,          // Transparent background
    autoDensity: true,
    resolution: renderBudget.resolution,
    antialias: true,
  });
  const frameRateController = window.RenderBudget.createFrameRateController(
    app.ticker,
    renderBudget.maxFps,
  );
  frameRateController.apply();
  window.RenderBudget.installWallpaperEngineListener(window, frameRateController);
  document.getElementById("canvas-container").appendChild(app.view);

  // ---------------------------------------------------------------------------
  // SpriteRenderer — deprecated foreground sprite path kept for existing assets.
  // ---------------------------------------------------------------------------
  class SpriteRenderer {
    constructor(stage) {
      this.stage = stage;
      this.container = new PIXI.Container();
      stage.addChild(this.container);

      this.sprite = new PIXI.Sprite();
      this.sprite.anchor.set(0.5, 1.0);  // Bottom-center alignment
      this.container.addChild(this.sprite);

      /** emotion → PIXI.Texture[] */
      this._frames = {};
      this._frameUrls = {};
      this._textureSamplingEnabled = renderBudget.textureSampling;
      this._frameSamplingPlans = new Map();
      this._requiredFrameIndices = new Map();
      this._textureSampleFps = null;
      this._sampleTimeMs = null;
      this._lastSpeechStateChangedAt = 0;
      this._speechQuietBeforeWarmMs = 900;
      this._prefetchHints = new Map();
      this._textureUrls = new WeakMap();
      this._displayMisses = 0;
      /** emotion → PIXI.Texture[][] (transition from→to) */
      this._transitions = {};
      this._transitionUrls = {};

      this._currentEmotion = "normal";
      this._speaking = false;
      this._mouthValue = 0;
      this._frameIdx = 0;
      this._ticker = null;
      this._transitionQueue = [];  // Transition frame sequence currently playing
      this._frameIntervals = {};   // emotion → ms
      this._clipConfigs = {};      // emotion → {loopMode, frameIntervalMs}
      this._idleAnimationEnabled = true;
      this._held = false;          // frame hold active
      this._heldFrameIdx = 0;      // idx saved during hold
      this._cycleCompleteHandler = null;
      this._cycleCompletedForEmotion = "";
      this._mouthTextures = {};
      this._mouthConfigSignatures = {};
      this._activeFrameIdx = 0;
      this._activeFramePhase = "frames";
      this._mouthSourceAnchor = null;
      this._viewportBounds = null;

      this._mouthMask = new PIXI.Graphics();
      this.sprite.addChild(this._mouthMask);

      this._mouthOverlay = new PIXI.Sprite();
      this._mouthOverlay.anchor.set(0.5, 1.0);
      this._mouthOverlay.x = 0;
      this._mouthOverlay.y = 0;
      this._mouthOverlay.visible = false;
      this._mouthOverlay.mask = this._mouthMask;
      this.sprite.addChild(this._mouthOverlay);
      this._mouthConfigs = {};     // label → js_cfg

      this._frameBackend = window.FrameTextureBackend.createFrameTextureBackend({ renderer: app.renderer });
      this._frameStore = window.FrameStore.createFrameStore({
        backend: this._frameBackend,
        // With sampling off, a lower presentation rate still visits the same
        // source frames. Halving residency makes those frames churn on loops.
        budgetBytes: 2 * 1024 * 1024 * 1024,
        maxInFlight: 3,
        onViewChange: (view, index, texture) => this._onTextureViewChange(view, index, texture),
      });
      window.addEventListener("unload", () => {
        this._frameStore.destroy();
        this._frameBackend.dispose();
      }, { once: true });

      this._startIdleTicker();
    }

    // ---- Asset loading ----

    loadFrames(emotion, urls) {
      if (this._sameUrlList(this._frameUrls[emotion], urls) && this._frames[emotion]) return;
      const list = Array.isArray(urls) ? urls.slice() : [];
      this._frameUrls[emotion] = list;
      this._frames[emotion] = new Array(list.length);
      this._frameSamplingPlans.delete(emotion);
      this._requiredFrameIndices.delete(emotion);
      this._frameStore.replaceViews("frames:" + emotion, list);
      this._updateRequiredPins(emotion);
      this._refreshTextureDemand();
    }

    loadTransitionFrames(fromEmotion, toEmotion, urls) {
      const key = `${fromEmotion}->${toEmotion}`;
      const list = Array.isArray(urls) ? urls.slice() : [];
      this._transitions[key] = new Array(list.length);
      this._transitionUrls[key] = list;
      this._frameStore.replaceViews("transition:" + key, list);
      // The legacy API retains a complete transition until it is replaced.
      this._frameStore.replacePins("transition:" + key, list);
    }

    // ---- State control ----

    setEmotion(emotion) {
      if (emotion === this._currentEmotion) return;
      const key = `${this._currentEmotion}->${emotion}`;
      const transFrames = this._transitions[key];
      const prev = this._currentEmotion;
      console.log("[SpriteRenderer] setEmotion: %s → %s (transition=%s, hasFrames=%s)",
        prev, emotion, key in this._transitions, !!this._frames[emotion]);
      this._currentEmotion = emotion;
      this._frameIdx = 0;
      this._sampleTimeMs = null;
      this._cycleCompletedForEmotion = "";
      this._hideMouthLayer();
      this._refreshTextureDemand();

      if (transFrames && transFrames.length > 0) {
        this._playTransition(transFrames, () => this._showFrame(0));
      } else {
        this._showFrame(0);
      }
    }

    setSpeaking(speaking) {
      if (this._speaking === speaking) return;
      this._speaking = speaking;
      this._lastSpeechStateChangedAt = Date.now();
      if (speaking) {
        this._frameIdx = 0;
        this._sampleTimeMs = null;
        this._cycleCompletedForEmotion = "";
        this._held = false;
        this._frameStore.replacePins("hold", []);
      }
      this._refreshTextureDemand();
      this._updateMouthLayer();
    }

    setMouth(value) {
      this._mouthValue = Math.max(0, Math.min(1, value));
      this._updateMouthLayer();
    }

    setIdleAnimation(enabled) {
      this._idleAnimationEnabled = enabled;
    }

    setIdleFrameIntervalMs(emotion, intervalMs) {
      this._frameIntervals[emotion] = intervalMs;
      this._frameSamplingPlans.delete(emotion);
      this._refreshTextureDemand();
    }

    setClipConfig(emotion, config) {
      this._clipConfigs[emotion] = config;
      this._refreshTextureDemand();
    }

    setCycleCompleteHandler(handler) {
      this._cycleCompleteHandler = handler;
    }

    loadMouthConfig(label, config) {
      const signature = JSON.stringify(config || {});
      if (this._mouthConfigSignatures[label] === signature && this._mouthTextures[label]) {
        return;
      }
      this._mouthConfigSignatures[label] = signature;
      this._mouthConfigs[label] = config;
      this._frameSamplingPlans.delete(label);
      const urls = Array.isArray(config.frameUrls) ? config.frameUrls : [];
      const textures = new Array(urls.length);
      this._mouthTextures[label] = textures;
      if (label === this._currentEmotion) this._hideMouthLayer();
      this._frameStore.replaceViews("mouth:" + label, urls);
      this._frameStore.replacePins("mouth:" + label, urls);
      this._updateRequiredPins(label);
      this._refreshTextureDemand();
      console.log("[SpriteRenderer] mouth config for", label, JSON.stringify(config).slice(0, 120));
    }

    _sameUrlList(a, b) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) {
        if (a[i] !== b[i]) return false;
      }
      return true;
    }

    _sampleFrameIndex(emotion, idx, timeMs = null) {
      if (!this._textureSamplingEnabled) return idx;
      const count = (this._frameUrls[emotion] || []).length;
      const interval = this._frameIntervals[emotion];
      if (!count || !(interval > 0)) return idx;
      let plan = this._frameSamplingPlans.get(emotion);
      if (!plan) {
        // Changes to graphics settings take effect for texture sampling on reload.
        if (this._textureSampleFps === null) this._textureSampleFps = frameRateController.effectiveMaxFps;
        const cfg = this._mouthConfigs[emotion] || {};
        const required = Array.from(this._requiredFrameIndices.get(emotion) || []);
        if (Number.isInteger(cfg.closedFrameIdx)) required.push(cfg.closedFrameIdx);
        const openness = cfg.opennessByFrame || [];
        let minimum = Infinity, closed = -1;
        for (let i = 0; i < Math.min(count, openness.length); i++) {
          const value = Number(openness[i]);
          if (Number.isFinite(value) && value < minimum) { minimum = value; closed = i; }
        }
        if (closed >= 0) required.push(closed);
        plan = window.RenderBudget.createFrameSamplingPlan(count, interval, this._textureSampleFps, required);
        this._frameSamplingPlans.set(emotion, plan);
      }
      if (timeMs !== null && !this._held && idx !== count - 1) {
        const slot = Math.min(plan.timelineIndices.length - 1, Math.floor(timeMs / plan.sampleIntervalMs));
        const selected = plan.timelineIndices[slot] ?? idx;
        // The legacy speaking loop deliberately excludes its zero/neutral pose.
        if (selected === 0 && idx > 0 && this._speaking && !this._mouthConfigs[emotion]
            && this._clipConfigs[emotion]?.loopMode !== "once_then_hold") {
          return plan.indices.find(index => index > 0) ?? idx;
        }
        return selected;
      }
      return plan.sourceIndex[idx] ?? idx;
    }

    prefetchLabels(labels, priority = "interactive", options = {}) {
      const score = this._priorityScore(priority);
      if (!score) return;
      const owner = "prefetch:" + (options.reason || priority);
      this._prefetchHints.set(owner, {
        labels: Array.from(new Set((labels || []).filter(Boolean))),
        priority: score,
      });
      this._refreshTextureDemand();
    }

    _priorityScore(priority) {
      if (typeof priority === "number") return priority;
      return ({ pinned: 100, current: 100, interactive: 90, speaking: 85, warm: 65, ambient: 25, poster: 10 })[priority] || 0;
    }

    _updateRequiredPins(label) {
      const urls = this._frameUrls[label] || [];
      const cfg = this._mouthConfigs[label] || {};
      const indices = new Set([0]);
      if (Number.isInteger(cfg.closedFrameIdx)) indices.add(cfg.closedFrameIdx);
      let minimum = Infinity, closed = -1;
      for (let i = 0; i < Math.min(urls.length, (cfg.opennessByFrame || []).length); i++) {
        const value = Number(cfg.opennessByFrame[i]);
        if (Number.isFinite(value) && value < minimum) { minimum = value; closed = i; }
      }
      if (closed >= 0) indices.add(closed);
      this._frameStore.replacePins("required:" + label, [...indices].map(index => urls[index]).filter(Boolean));
    }

    _onTextureViewChange(view, index, texture) {
      const colon = view.indexOf(":");
      const kind = view.slice(0, colon), label = view.slice(colon + 1);
      const arrays = kind === "mouth" ? this._mouthTextures : kind === "transition" ? this._transitions : this._frames;
      const frames = arrays[label];
      if (!frames || index >= frames.length) return;
      frames[index] = texture;
      if (!texture) return;
      if (kind === "frames") {
        this._textureUrls.set(texture, this._frameUrls[label][index]);
        if (label === this._currentEmotion) {
          const target = this._held ? this._heldFrameIdx : this._frameIdx;
          if (index === this._sampleFrameIndex(label, target, this._held ? null : this._sampleTimeMs)) this._showFrame(target);
        }
      } else if (kind === "mouth") {
        const url = this._mouthConfigs[label]?.frameUrls?.[index];
        if (url) this._textureUrls.set(texture, url);
        if (label === this._currentEmotion) this._updateMouthLayer();
      } else if (kind === "transition") {
        this._textureUrls.set(texture, this._transitionUrls[label][index]);
      }
    }

    _refreshTextureDemand(sourceIndex = this._frameIdx) {
      const label = this._frames[this._currentEmotion] ? this._currentEmotion : "normal";
      const urls = this._frameUrls[label] || [];
      const interval = this._frameIntervals[label] || this._clipConfigs[label]?.frameIntervalMs || 150;
      const once = this._clipConfigs[label]?.loopMode === "once_then_hold";
      const requests = [], now = this._frameBackend.now();
      let index = this._held ? this._heldFrameIdx : sourceIndex;
      const count = this._held ? 1 : Math.min(urls.length, Math.ceil(500 / interval) + 1);
      for (let step = 0; step < count && urls.length; step++) {
        const selected = this._sampleFrameIndex(label, index, this._held ? null : index * interval);
        if (urls[selected]) requests.push({ url: urls[selected], priority: 100, deadline: now + step * interval });
        if (once && index >= urls.length - 1) break;
        index = this._speaking && !this._mouthConfigs[label] && !once && urls.length > 1
          ? (index % (urls.length - 1)) + 1 : (index + 1) % urls.length;
      }
      this._frameStore.replaceDemand("current", requests);
      const deferred = this._speaking || Date.now() - this._lastSpeechStateChangedAt < this._speechQuietBeforeWarmMs;
      for (const [owner, hint] of this._prefetchHints) {
        const wanted = [];
        if (!deferred || hint.priority >= 90) {
          for (const target of hint.labels) {
            const frames = this._frameUrls[target] || [];
            const timing = this._frameIntervals[target] || this._clipConfigs[target]?.frameIntervalMs || 150;
            const headMs = this._clipConfigs[target]?.loopMode === "once_then_hold" ? 750 : 250;
            for (let i = 0; i < Math.min(frames.length, Math.ceil(headMs / timing) + 1); i++) {
              const selected = this._sampleFrameIndex(target, i, i * timing);
              if (frames[selected]) wanted.push({ url: frames[selected], priority: hint.priority, deadline: Infinity });
            }
          }
        }
        this._frameStore.replaceDemand(owner, wanted);
      }
    }

    holdFrame(which) {
      if (which === undefined || which === null) {
        this._heldFrameIdx = this._textureSamplingEnabled ? this._activeFrameIdx : this._frameIdx;
      } else if (which === -1) {
        const frames = this._getEmotionFrames();
        this._heldFrameIdx = frames ? frames.length - 1 : this._frameIdx; // hold last
      } else {
        this._heldFrameIdx = which;
      }
      const emotion = this._currentEmotion;
      const validHold = this._textureSamplingEnabled && Number.isInteger(this._heldFrameIdx) && this._heldFrameIdx >= 0
        && this._heldFrameIdx < (this._frameUrls[emotion] || []).length;
      if (validHold && this._sampleFrameIndex(emotion, this._heldFrameIdx) !== this._heldFrameIdx) {
        if (!this._requiredFrameIndices.has(emotion)) this._requiredFrameIndices.set(emotion, new Set());
        this._requiredFrameIndices.get(emotion).add(this._heldFrameIdx);
        this._frameSamplingPlans.delete(emotion);
      }
      const heldUrl = this._frameUrls[emotion]?.[this._heldFrameIdx];
      this._frameStore.replacePins("hold", heldUrl ? [heldUrl] : []);
      this._held = true;
      if (!this._textureSamplingEnabled) {
        this._activeFramePhase = "frames";
        this._activeFrameIdx = this._heldFrameIdx;
      }
      this._showFrame(this._heldFrameIdx);
    }

    holdClosedFrame() {
      const frames = this._getEmotionFrames();
      if (!frames || frames.length === 0) {
        this.holdFrame(null);
        return;
      }
      const cfg = this._mouthConfigs[this._currentEmotion] || {};
      let idx = Number.isFinite(cfg.closedFrameIdx) ? Math.round(cfg.closedFrameIdx) : NaN;
      const openness = Array.isArray(cfg.opennessByFrame) ? cfg.opennessByFrame : [];
      if (openness.length) {
        let bestIdx = -1;
        let bestValue = Infinity;
        const n = Math.min(openness.length, frames.length);
        for (let i = 0; i < n; i += 1) {
          const value = Number(openness[i]);
          if (!Number.isFinite(value)) continue;
          if (value < bestValue && frames[i]) {
            bestValue = value;
            bestIdx = i;
          }
        }
        if (bestIdx >= 0) idx = bestIdx;
      }
      if (!Number.isFinite(idx)) idx = this._frameIdx;
      this.holdFrame(Math.max(0, Math.min(frames.length - 1, idx)));
    }

    clearHold() {
      this._held = false;
      this._frameStore.replacePins("hold", []);
      this._refreshTextureDemand();
    }

    resize(w, h) {
      this._applyCurrentTransform();
    }

    // ---- Internals ----

    setViewportBounds(bounds) {
      if (!bounds || !Number.isFinite(bounds.x) || !Number.isFinite(bounds.y) ||
          !Number.isFinite(bounds.width) || !Number.isFinite(bounds.height)) {
        this._viewportBounds = null;
      } else {
        this._viewportBounds = {
          x: Number(bounds.x),
          y: Number(bounds.y),
          width: Math.max(1, Number(bounds.width)),
          height: Math.max(1, Number(bounds.height)),
        };
        this._viewportBounds.right = this._viewportBounds.x + this._viewportBounds.width;
        this._viewportBounds.bottom = this._viewportBounds.y + this._viewportBounds.height;
      }
      this._applyCurrentTransform();
    }

    _getEmotionFrames() {
      return this._frames[this._currentEmotion] || this._frames["normal"] || null;
    }

    _showFrame(idx) {
      this._refreshTextureDemand(idx);
      const frames = this._getEmotionFrames();
      if (!frames || frames.length === 0) return;
      const logicalIdx = idx % frames.length;
      const emotion = this._frames[this._currentEmotion] ? this._currentEmotion : "normal";
      let targetIdx = this._sampleFrameIndex(emotion, logicalIdx, this._sampleTimeMs);
      let texture = frames[targetIdx];
      if (!texture) {
        this._displayMisses++;
        // file:// assets are decoded asynchronously. Falling back to the first
        // loaded frame creates visible mid-animation snaps; keep the previous
        // frame until the requested texture is ready.
        const hasCurrentTexture = this.sprite.texture && this.sprite.texture.height > 1;
        if (hasCurrentTexture) return;
        if (this._textureSamplingEnabled) {
          targetIdx = frames.findIndex(Boolean);
          texture = frames[targetIdx];
        } else {
          texture = frames.find(Boolean);
        }
      }
      if (!texture) return;
      this._frameIdx = logicalIdx;
      this._activeFramePhase = "frames";
      this._activeFrameIdx = targetIdx;
      this._applyFrame(texture);
    }

    _applyFrame(texture) {
      if (!texture) return;
      this.sprite.texture = texture;
      const url = this._textureUrls.get(texture);
      if (url) this._frameStore.get(url);
      this._frameStore.replacePins("display", url ? [url] : []);
      this._applyCurrentTransform();
      this._updateMouthLayer();
    }

    _applyCurrentTransform() {
      const texture = this.sprite.texture;
      if (!texture || texture.height <= 1) {
        this.sprite.scale.set(1);
        return;
      }
      // Fit the character by the canvas it was authored on, not by this frame: a frame
      // wider than the canvas (hair blowing past its edge) must extend beyond it, centred
      // at the bottom like every other frame, instead of shrinking the character.
      const cfg = this._clipConfigs[this._currentEmotion] || {};
      const fitWidth = cfg.canvasWidth > 0 ? cfg.canvasWidth : texture.width;
      const fitHeight = cfg.canvasHeight > 0 ? cfg.canvasHeight : texture.height;
      if (this._viewportBounds) {
        const b = this._viewportBounds;
        const maxW = b.width * 0.64;
        const maxH = b.height * 0.90;
        const scale = Math.min(maxW / fitWidth, maxH / fitHeight);
        this.sprite.scale.set(scale);
        this.sprite.x = b.x + b.width * 0.52;
        // Keep the lower artwork just inside the CRT viewport.
        this.sprite.y = b.y + b.height - Math.max(3, b.height * 0.012);
        return;
      }
      const h = app.screen.height;
      const scale = h / fitHeight;
      this.sprite.scale.set(scale);
      this.sprite.x = app.screen.width / 2;
      this.sprite.y = h;
    }

    _hideMouthLayer() {
      this._frameStore.replacePins("mouth-display", []);
      if (this._mouthOverlay) {
        this._mouthOverlay.visible = false;
        this._mouthOverlay.x = 0;
        this._mouthOverlay.y = 0;
      }
      this._mouthSourceAnchor = null;
      if (this._mouthMask) this._mouthMask.clear();
    }

    _updateMouthLayer() {
      const cfg = this._mouthConfigs[this._currentEmotion];
      const textures = this._mouthTextures[this._currentEmotion] || [];
      if (!cfg || !textures.length) {
        this._hideMouthLayer();
        return;
      }

      const openness = Array.isArray(cfg.openness) ? cfg.openness : [];
      const n = Math.min(openness.length || textures.length, textures.length);
      if (n <= 0) {
        this._hideMouthLayer();
        return;
      }

      let bestIdx = 0;
      let bestDist = Infinity;
      const mode = cfg.mode || "full_map";
      const silenceClose = mode === "silence_close";
      const targetValue = silenceClose ? 0.0 : this._mouthValue;
      for (let i = 0; i < n; i++) {
        const level = typeof openness[i] === "number" ? openness[i] : (i / Math.max(1, n - 1));
        const dist = Math.abs(level - targetValue);
        if (dist < bestDist) {
          bestDist = dist;
          bestIdx = i;
        }
      }

      const texture = textures[bestIdx];
      if (!this._isTextureReady(texture)) {
        this._hideMouthLayer();
        return;
      }

      this._mouthOverlay.texture = texture;
      const mouthUrl = this._textureUrls.get(texture);
      this._frameStore.replacePins("mouth-display", mouthUrl ? [mouthUrl] : []);
      const sourceAnchors = Array.isArray(cfg.sourceAnchors) ? cfg.sourceAnchors : null;
      this._mouthSourceAnchor = sourceAnchors && sourceAnchors[bestIdx] ? sourceAnchors[bestIdx] : null;

      if (silenceClose) {
        const threshold = Number.isFinite(cfg.silenceThreshold) ? cfg.silenceThreshold : 0.08;
        const shouldClose = !this._speaking || this._mouthValue <= threshold;
        this._mouthOverlay.visible = shouldClose;
        if (shouldClose) {
          const maskAmp = Number.isFinite(cfg.silenceMaskAmplitude) ? cfg.silenceMaskAmplitude : 0.75;
          this._drawMouthMask(maskAmp, cfg);
        } else {
          this._hideMouthLayer();
        }
        return;
      }

      if (this._speaking) {
        this._mouthOverlay.visible = true;
        this._drawMouthMask(Math.max(this._mouthValue, 0.05), cfg);
      } else {
        this._mouthOverlay.visible = this._mouthValue > 0.01;
        if (this._mouthValue > 0.01) {
          this._drawMouthMask(Math.max(this._mouthValue, 0.05), cfg);
        } else {
          this._hideMouthLayer();
        }
      }
    }

    _isTextureReady(texture) {
      return !!(
        texture &&
        !texture.destroyed &&
        texture.baseTexture &&
        !texture.baseTexture.destroyed &&
        texture.baseTexture.valid &&
        Number.isFinite(texture.width) &&
        Number.isFinite(texture.height) &&
        texture.width > 1 &&
        texture.height > 1
      );
    }

    _textureHeight(texture, fallback = 1) {
      if (!this._isTextureReady(texture)) return fallback;
      return Number.isFinite(texture.height) && texture.height > 1 ? texture.height : fallback;
    }

    _drawMouthMask(amplitude, cfg) {
      const g = this._mouthMask;
      g.clear();
      if (amplitude <= 0) return;
      const tex = this.sprite.texture;
      const th = this._textureHeight(tex, 1);
      if (th <= 1) return;

      const anchor = this._getMouthAnchor(cfg);
      const cx = Number.isFinite(anchor.cx) ? anchor.cx : cfg.cx;
      const cy = Number.isFinite(anchor.cy) ? anchor.cy : cfg.cy;
      const width = Number.isFinite(anchor.width) ? anchor.width : cfg.width;
      const height = Number.isFinite(anchor.height) ? anchor.height : cfg.height;
      const maskWidthMul = Number.isFinite(cfg.maskWidthMul) ? cfg.maskWidthMul : 1.0;
      const maskHeightMul = Number.isFinite(cfg.maskHeightMul) ? cfg.maskHeightMul : 1.0;
      const maskCyOffset = Number.isFinite(cfg.maskCyOffset) ? cfg.maskCyOffset : 0.0;
      const overlayH = this._textureHeight(this._mouthOverlay.texture, th);

      const localX = cx;
      const localY = cy - th / 2;
      const maskLocalY = localY + maskCyOffset;
      const source = this._mouthSourceAnchor || {};
      const sourceCx = Number.isFinite(source.cx)
        ? source.cx
        : (Number.isFinite(cfg.sourceCx) ? cfg.sourceCx : (Number.isFinite(cfg.cx) ? cfg.cx : 0));
      const sourceCy = Number.isFinite(source.cy)
        ? source.cy
        : (Number.isFinite(cfg.sourceCy) ? cfg.sourceCy : (Number.isFinite(cfg.cy) ? cfg.cy : 0));
      if (cfg.overlayAlign === "canvas") {
        this._mouthOverlay.x = 0;
        this._mouthOverlay.y = 0;
      } else {
        this._mouthOverlay.x = localX - sourceCx;
        this._mouthOverlay.y = localY - (sourceCy - overlayH / 2);
      }

      const wHalf = (Math.max(1, width) / 2) * 1.8 * maskWidthMul;
      const hHalf = (Math.max(1, height) / 2) * (1.0 + 1.5 * amplitude) * maskHeightMul;
      if (hHalf < 0.5) return;

      g.beginFill(0xFFFFFF, 1);
      g.drawEllipse(localX, maskLocalY, wHalf, hHalf);
      g.endFill();
    }

    _getMouthAnchor(cfg) {
      const track = Array.isArray(cfg.anchorTrack) ? cfg.anchorTrack : [];
      if (track.length > 0) {
        const rawIdx = Number.isFinite(this._activeFrameIdx) ? this._activeFrameIdx : this._frameIdx;
        const idx = Math.max(0, Math.min(track.length - 1, Math.round(rawIdx)));
        const anchor = track[idx];
        if (anchor && typeof anchor === "object") {
          return {
            cx: Number.isFinite(anchor.cx) ? anchor.cx : cfg.cx,
            cy: Number.isFinite(anchor.cy) ? anchor.cy : cfg.cy,
            width: Number.isFinite(anchor.width) ? anchor.width : cfg.width,
            height: Number.isFinite(anchor.height) ? anchor.height : cfg.height,
          };
        }
      }
      return cfg || {};
    }

    _startIdleTicker() {
      // Uses per-emotion frame intervals when available (SpriteForge), falls back to 150ms.
      let elapsed = 0;
      let lastEmotion = null;
      let silentGuardHit = 0;
      let tickCount = 0;
      app.ticker.add((delta) => {
        tickCount++;
        if (tickCount % 180 === 1) {  // ~every 3s at 60fps
          console.log("[SpriteRenderer] ticker alive: tick=%d, iAE=%s, held=%s, tq=%d, emo=%s, fidx=%d, intv=%d, deltaMS=%d",
            tickCount, this._idleAnimationEnabled, this._held, this._transitionQueue.length,
            this._currentEmotion, this._frameIdx,
            this._frameIntervals[this._currentEmotion] || (this._clipConfigs[this._currentEmotion] && this._clipConfigs[this._currentEmotion].frameIntervalMs) || 150,
            Math.round(app.ticker.deltaMS));
        }

        if (!this._idleAnimationEnabled) { silentGuardHit++; return; }
        if (this._held) return;
        if (this._transitionQueue.length > 0) return;

        const frames = this._getEmotionFrames();
        if (!frames || frames.length === 0 || !frames.some(Boolean)) {
          silentGuardHit++;
          if (silentGuardHit === 60) {
            console.log("[SpriteRenderer] no frames for emotion: %s, keys: %s",
              this._currentEmotion, Object.keys(this._frames).join(','));
          }
          return;
        }
        silentGuardHit = 0;

        const emotion = this._currentEmotion;
        if (emotion !== lastEmotion) {
          elapsed = 0;
          lastEmotion = emotion;
        }
        const cfg = this._clipConfigs[emotion];
        const onceThenHold = cfg && cfg.loopMode === "once_then_hold";
        const mouthCfg = this._mouthConfigs[emotion];
        const interval = this._frameIntervals[emotion] || (cfg && cfg.frameIntervalMs) || 150;

        elapsed += Math.min(app.ticker.deltaMS, 100);
        if (elapsed < interval) return;
        const steps = this._textureSamplingEnabled
          ? Math.floor(elapsed / interval) : Math.min(4, Math.floor(elapsed / interval));
        elapsed -= steps * interval;

        let advanced = false;
        let crossedCycle = false;
        for (let i = 0; i < steps; i++) {
          if (onceThenHold && this._frameIdx >= frames.length - 1) {
            break;
          }

          const previousIdx = this._frameIdx;
          if (onceThenHold) {
            this._frameIdx = Math.min(this._frameIdx + 1, frames.length - 1);
          } else if (this._speaking && mouthCfg && frames.length > 1) {
            this._frameIdx = (this._frameIdx + 1) % frames.length;
          } else if (this._speaking && frames.length > 1) {
            this._frameIdx = (this._frameIdx % (frames.length - 1)) + 1;
          } else {
            this._frameIdx = (this._frameIdx + 1) % frames.length;
          }
          if (!onceThenHold && this._frameIdx <= previousIdx) crossedCycle = true;
          advanced = true;
        }

        if (!advanced) {
          return;
        }

        if (this._textureSamplingEnabled) this._sampleTimeMs = this._frameIdx * interval + elapsed;
        this._showFrame(this._frameIdx);
        if (onceThenHold && this._frameIdx >= frames.length - 1) {
          this._notifyCycleComplete(emotion);
        } else if (!onceThenHold && (this._textureSamplingEnabled ? crossedCycle : this._frameIdx === 0)) {
          if (this._textureSamplingEnabled) this._cycleCompletedForEmotion = "";
          this._notifyCycleComplete(emotion);
        } else {
          this._cycleCompletedForEmotion = "";
        }
        if (this._frameIdx === 0) {
          console.log("[SpriteRenderer] looping: emotion=%s frames=%d interval=%d",
            emotion, frames.length, interval);
        }
      });

      console.log("[SpriteRenderer] idle ticker started");
    }

    _notifyCycleComplete(emotion) {
      if (!this._cycleCompleteHandler) return;
      if (this._cycleCompletedForEmotion === emotion) return;
      this._cycleCompletedForEmotion = emotion;
      try {
        this._cycleCompleteHandler(emotion);
      } catch (e) {
        console.warn("[SpriteRenderer] cycle handler failed:", e);
      }
    }

    _playTransition(frames, onDone) {
      this._transitionQueue = [...frames];
      this._frameStore.replacePins("transition-playing", frames.map(texture => this._textureUrls.get(texture)).filter(Boolean));
      let idx = 0;
      const step = () => {
        if (idx >= this._transitionQueue.length) {
          this._transitionQueue = [];
          this._frameStore.replacePins("transition-playing", []);
          onDone && onDone();
          return;
        }
        this._applyFrame(this._transitionQueue[idx++]);
        setTimeout(step, 50);  // 50 ms transition frame interval
      };
      step();
    }
  }

  // ---------------------------------------------------------------------------
  // Live2DRenderer — optional pixi-live2d-display wrapper
  // ---------------------------------------------------------------------------
  class SpriteForgeRuntime {
    constructor(sprite) {
      this.sprite = sprite;
      this.graph = { nodes: [], edges: [] };
      this.rootNodeId = null;
      this.currentNodeId = null;
      this.pendingExpression = null;
      this.forcedNodeId = null;
      this.speechActive = false;
      this.activeSpeechIntent = null;
      this.transitionHoldActive = false;
      this.postSpeechTimer = null;
      this.postSpeechHoldActive = false;
      this.deferredPresentationIntent = null;
      this.labelToIds = {};
      this.nodesById = {};
      this.nodeDurations = {};
      this.cfg = {};
      this._graphSignature = "";
      this.intentAliases = {
        work: "thinking",
        working: "thinking",
        provider_work: "thinking",
        tool_call: "thinking",
        coding: "thinking",
      };
    }

    loadGraph(payload) {
      const signature = JSON.stringify(payload || {});
      if (this._graphSignature === signature && this.currentNodeId) {
        return;
      }
      this._graphSignature = signature;
      this.graph = payload.graph || { nodes: [], edges: [] };
      this.rootNodeId = payload.rootNodeId || null;
      this.nodeDurations = payload.durations || {};
      this.cfg = payload.config || {};
      this.nodesById = {};
      this.labelToIds = {};
      for (const node of this.graph.nodes || []) {
        this.nodesById[node.id] = node;
        const label = node.label || "";
        if (!this.labelToIds[label]) this.labelToIds[label] = [];
        this.labelToIds[label].push(node.id);
        if (!this.rootNodeId && node.isRoot) this.rootNodeId = node.id;
      }
      if (!this.rootNodeId && this.graph.nodes && this.graph.nodes.length) {
        this.rootNodeId = this.graph.nodes[0].id;
      }
      if (this.rootNodeId) this._playNode(this.rootNodeId);
      // Entry heads are reloadable hints, not graph decisions. Do not call
      // _nextAutoNode here: prefetch must not consume an extra random draw.
      const entries = new Set([
        this.cfg.defaultSpeakingTriggerLabel, this.cfg.closedEyeSpeakingTriggerLabel,
        ...Object.values(this.cfg.emotionEntryByIntent || {}),
        ...(this.cfg.thinkingEntryLabels || []), ...(this.cfg.seriousEntryLabels || []),
        ...Object.values(this.cfg.postSpeechEmotionLabelByIntent || {}),
      ]);
      for (const edge of this.graph.edges || []) {
        if (edge.from === this.rootNodeId && Number(edge.prob || 0) === 0) entries.add(this._label(edge.to));
      }
      this._prefetchLabels(entries, "warm", { reason: "graph-entries" });
      console.log("[SpriteForgeRuntime] graph loaded:", (this.graph.nodes || []).length, "nodes");
    }

    trigger(label, options = {}) {
      if (!label) return;
      label = this._normalizeTriggerLabel(label);
      const afterSpeech = options && options.presentation_handoff === "after_speech";
      if (afterSpeech && (this.speechActive || this.postSpeechHoldActive)) {
        this.deferredPresentationIntent = label;
        if (this.speechActive) {
          this.setSpeaking(false);
          if (!this.postSpeechHoldActive) {
            this.deferredPresentationIntent = null;
            this.trigger(label);
          }
        }
        return;
      }
      this.deferredPresentationIntent = null;
      this._prefetchForTrigger(label, "interactive");
      this._clearPostSpeechTimer();
      this.transitionHoldActive = false;
      const intent = this._emotionIntent(label);
      if (intent && this.speechActive) {
        this.activeSpeechIntent = intent;
        label = (this.cfg.emotionEntryByIntent || {})[intent] || label;
        this._prefetchForTrigger(label, "interactive");
      }
      this.pendingExpression = label;
      this.sprite.clearHold();
      this._advanceNow();
      console.log("[SpriteForgeRuntime] intent:", label);
    }

    release(options = {}) {
      const afterSpeech = options && options.presentation_handoff === "after_speech";
      if (afterSpeech) {
        // The speech state machine owns its one-second closed-mouth hold and
        // emotion-specific exit.  A presentation claim ending must not erase
        // that release animation; it only cancels an obsolete deferred pose.
        this.deferredPresentationIntent = null;
        if (this.speechActive) this.setSpeaking(false);
        if (this.postSpeechHoldActive) {
          console.log("[SpriteForgeRuntime] presentation released after speech");
          return;
        }
        // If playback ended before a speaking performance was entered, there
        // is no historical hold to preserve.  Release the stale claim now.
      }
      this._clearPostSpeechTimer();
      this.deferredPresentationIntent = null;
      this.pendingExpression = null;
      this.forcedNodeId = null;
      this._prefetchLabels([], "interactive", { reason: "trigger" });
      this.speechActive = false;
      this.activeSpeechIntent = null;
      this.transitionHoldActive = false;
      this.sprite.setSpeaking(false);
      this.sprite.clearHold();
      if (this.rootNodeId) this._playNode(this.rootNodeId);
      console.log("[SpriteForgeRuntime] released to root");
    }

    _normalizeTriggerLabel(label) {
      const raw = String(label || "").trim();
      const key = raw.toLowerCase();
      const normalized = (this.cfg.triggerAliases || {})[key] || this.intentAliases[key] || raw;
      // Backend routing is authoritative and randomizes once before fan-out.
      // This deterministic fallback keeps direct browser/dev calls from
      // becoming no-ops without allowing GUI and wallpaper to disagree.
      return (this.cfg.semanticTriggerDefaults || {})[String(normalized).toLowerCase()] || normalized;
    }

    setSpeaking(speaking) {
      speaking = !!speaking;
      if (speaking) {
        const already = this.speechActive;
        this.speechActive = true;
        this.sprite.setSpeaking(true);
        this._clearPostSpeechTimer();
        this.deferredPresentationIntent = null;

        const currentLabel = this._label(this.currentNodeId);
        if (this.transitionHoldActive && this._has("transitionHoldLabels", currentLabel)) {
          this.forcedNodeId = this._nextAutoNode(this.currentNodeId);
          this.transitionHoldActive = false;
          this.sprite.clearHold();
          this._advanceNow();
          return;
        }
        const pendingIntent = this._emotionIntent(this.pendingExpression || "");
        if (pendingIntent) {
          this.activeSpeechIntent = pendingIntent;
          this.pendingExpression = (this.cfg.emotionEntryByIntent || {})[pendingIntent] || this.pendingExpression;
          this._prefetchForTrigger(this.pendingExpression, "speaking");
          this._advanceNow();
          return;
        }
        if (!already && !this.pendingExpression && !this._has("speakingReleaseLabels", currentLabel)) {
          let trigger = this.cfg.defaultSpeakingTriggerLabel || "speaking_short";
          const eligible = this._has("closedEyeEligibleLabels", currentLabel);
          const closedEye = this.cfg.closedEyeSpeakingTriggerLabel || "closed_eye_trans";
          const chance = Number(this.cfg.closedEyeSpeakingChance || 0);
          if (eligible && this._nodeByLabel(closedEye) && Math.random() < chance) {
            trigger = closedEye;
          }
          this.activeSpeechIntent = null;
          this.pendingExpression = trigger;
          this._prefetchForTrigger(trigger, "speaking");
          this._advanceNow();
        }
        return;
      }

      if (!this.speechActive && this.postSpeechHoldActive) {
        this.sprite.setSpeaking(false);
        return;
      }

      const currentLabel = this._label(this.currentNodeId);
      this.speechActive = false;
      this.sprite.setSpeaking(false);
      const pendingLabel = this.pendingExpression || "";
      const currentIsPerformance = this._has("speakingReleaseLabels", currentLabel);
      const pendingIsPerformance = this._has("speakingReleaseLabels", pendingLabel);
      if (pendingIsPerformance) this.pendingExpression = null;
      if (currentIsPerformance || pendingIsPerformance) {
        const releaseLabel = currentIsPerformance ? currentLabel : pendingLabel;
        const releaseNodeId = this._postSpeechReleaseNode(releaseLabel || currentLabel) || this.rootNodeId;
        if (typeof this.sprite.holdClosedFrame === "function") this.sprite.holdClosedFrame();
        else this.sprite.holdFrame(null);
        this._clearPostSpeechTimer();
        this.postSpeechHoldActive = true;
        this.postSpeechTimer = setTimeout(() => {
          this.postSpeechTimer = null;
          this.postSpeechHoldActive = false;
          this.activeSpeechIntent = null;
          this.sprite.clearHold();
          const deferredIntent = this.deferredPresentationIntent;
          this.deferredPresentationIntent = null;
          if (deferredIntent) {
            this.trigger(deferredIntent);
            return;
          }
          if (releaseNodeId) this._playNode(releaseNodeId);
          if (releaseNodeId && releaseNodeId !== this.rootNodeId) {
            this._schedulePostSpeechReleaseAdvance(releaseNodeId);
          }
        }, Number(this.cfg.postSpeechHoldSec || 1.0) * 1000);
      }
    }

    onCycleComplete(label) {
      if (label !== this._label(this.currentNodeId)) return;
      if (!this.speechActive && this._has("transitionHoldLabels", label)) {
        this.transitionHoldActive = true;
        this.sprite.holdFrame(-1);
        return;
      }
      this._advanceNow();
    }

    _advanceNow() {
      if (this.forcedNodeId) {
        const nodeId = this.forcedNodeId;
        this.forcedNodeId = null;
        this._playNode(nodeId);
        return;
      }
      if (this.pendingExpression) {
        const label = this.pendingExpression;
        this.pendingExpression = null;
        const entry = this._findTriggerEntry(label);
        if (entry) {
          this._playNode(entry);
          return;
        }
        const target = this._nodeByLabel(label);
        if (target) {
          this._playNode(target.id);
          return;
        }
        console.warn("[SpriteForgeRuntime] no trigger entry for:", label);
      }
      const next = this._nextAutoNode(this.currentNodeId);
      if (next) this._playNode(next);
    }

    _playNode(nodeId) {
      if (!nodeId || !this.nodesById[nodeId]) return;
      this.currentNodeId = nodeId;
      this.transitionHoldActive = false;
      this.sprite.clearHold();
      const label = this._label(nodeId);
      this.sprite.setEmotion(label);
      this._prefetchNodeNeighborhood(nodeId);
    }

    _label(nodeId) {
      const node = this.nodesById[nodeId || ""];
      return node ? (node.label || "") : "";
    }

    _nodeByLabel(label) {
      const ids = this.labelToIds[label] || [];
      return ids.length ? this.nodesById[ids[0]] : null;
    }

    _prefetchLabels(labels, priority, options = {}) {
      if (this.sprite && typeof this.sprite.prefetchLabels === "function") {
        this.sprite.prefetchLabels(Array.from(labels || []).filter(Boolean), priority, options);
      }
    }

    _labelsForNodeIds(ids) {
      const labels = new Set();
      for (const id of ids || []) {
        const label = this._label(id);
        if (label) labels.add(label);
      }
      return labels;
    }

    _prefetchForTrigger(targetLabel, priority) {
      const labels = new Set([targetLabel]);
      const targetIds = this._triggerTargetIds(targetLabel);
      for (const label of this._labelsForNodeIds(targetIds)) labels.add(label);
      const entry = this._findTriggerEntry(targetLabel);
      if (entry) {
        labels.add(this._label(entry));
        const next = this._nextAutoNode(entry);
        if (next) labels.add(this._label(next));
      }
      this._prefetchLabels(labels, priority, { reason: "trigger" });
    }

    _prefetchNodeNeighborhood(nodeId) {
      const edges = (this.graph.edges || [])
        .filter((e) => e.from === nodeId && Number(e.prob || 0) > 0);
      // Every direct successor can be selected by the next ordinary draw.
      // Prepare finite heads without choosing a winner or deprioritizing a
      // rare edge until after it has already become the visible animation.
      const next = this._labelsForNodeIds(edges.map(edge => edge.to));
      this._prefetchLabels(next, this.speechActive ? "interactive" : "warm", { reason: "graph-next" });
    }

    _has(name, label) {
      const arr = this.cfg[name] || [];
      return arr.indexOf(label) >= 0;
    }

    _addSet(out, name) {
      for (const item of this.cfg[name] || []) out.add(item);
    }

    _emotionIntent(label) {
      return (this.cfg.emotionIntentByLabel || {})[label] || null;
    }

    _triggerTargetIds(targetLabel) {
      const labels = new Set([targetLabel]);
      if (this._has("seriousSpeakingLabels", targetLabel) || this._has("seriousEntryLabels", targetLabel)) {
        this._addSet(labels, "seriousSpeakingLabels");
        this._addSet(labels, "seriousEntryLabels");
      } else if (this._has("defaultSpeakingLabels", targetLabel)) {
        this._addSet(labels, "defaultSpeakingLabels");
      } else if (this._has("closedEyeSpeakingLabels", targetLabel)) {
        this._addSet(labels, "closedEyeSpeakingLabels");
      } else if (
        this._has("thinkingSpeakingLabels", targetLabel) ||
        this._has("thinkingEntryLabels", targetLabel) ||
        this._has("seriousExitLabels", targetLabel)
      ) {
        this._addSet(labels, "thinkingSpeakingLabels");
        this._addSet(labels, "thinkingEntryLabels");
        this._addSet(labels, "seriousExitLabels");
      } else {
        const intent = this._emotionIntent(targetLabel);
        if (intent) {
          for (const [label, labelIntent] of Object.entries(this.cfg.emotionIntentByLabel || {})) {
            if (labelIntent === intent) labels.add(label);
          }
        }
      }

      const ids = new Set();
      for (const label of labels) {
        for (const id of this.labelToIds[label] || []) ids.add(id);
      }
      return ids;
    }

    _findTriggerEntry(targetLabel) {
      const targetIds = this._triggerTargetIds(targetLabel);
      const current = this._firstHopToAny(this.currentNodeId, targetIds);
      if (current) return current;
      return this._firstHopToAny(this.rootNodeId, targetIds);
    }

    _firstHopToAny(startId, targetIds) {
      if (!startId || !targetIds || targetIds.size === 0) return null;
      if (targetIds.has(startId)) return startId;

      const visited = new Set([startId]);
      const queue = [{ nodeId: startId, firstHop: null }];
      while (queue.length) {
        const item = queue.shift();
        const allowManual = item.nodeId === startId;
        const edges = (this.graph.edges || [])
          .filter((e) => e.from === item.nodeId && ((Number(e.prob || 0) > 0) || (allowManual && Number(e.prob || 0) === 0)))
          .sort((a, b) => {
            const at = targetIds.has(a.to) ? 0 : 1;
            const bt = targetIds.has(b.to) ? 0 : 1;
            if (at !== bt) return at - bt;
            const am = Number(a.prob || 0) === 0 ? 0 : 1;
            const bm = Number(b.prob || 0) === 0 ? 0 : 1;
            return am - bm;
          });
        for (const edge of edges) {
          const nextId = edge.to;
          const hop = item.firstHop || nextId;
          if (targetIds.has(nextId)) return hop;
          if (visited.has(nextId)) continue;
          visited.add(nextId);
          queue.push({ nodeId: nextId, firstHop: hop });
        }
      }
      return null;
    }

    _nextAutoNode(fromId) {
      const edges = (this.graph.edges || []).filter((e) => e.from === fromId && Number(e.prob || 0) > 0);
      if (!edges.length) return fromId;
      const total = edges.reduce((sum, e) => sum + Number(e.prob || 0), 0);
      let r = Math.random() * total;
      for (const edge of edges) {
        r -= Number(edge.prob || 0);
        if (r <= 0) return edge.to;
      }
      return edges[edges.length - 1].to;
    }

    _postSpeechReleaseNode(currentLabel) {
      if (this._has("nonEmotionSpeakingLabels", currentLabel)) return this.rootNodeId;
      const intent = this._emotionIntent(currentLabel) || this.activeSpeechIntent;
      const targetLabel = (this.cfg.postSpeechEmotionLabelByIntent || {})[intent || ""];
      const target = targetLabel ? this._nodeByLabel(targetLabel) : null;
      return target ? target.id : this.rootNodeId;
    }

    _schedulePostSpeechReleaseAdvance(releaseNodeId) {
      const durationMs = this._nodeDurationMs(releaseNodeId);
      this.postSpeechTimer = setTimeout(() => {
        this.postSpeechTimer = null;
        if (this.speechActive || this.currentNodeId !== releaseNodeId) return;
        this.sprite.clearHold();
        const next = this._nextAutoNode(releaseNodeId);
        this._playNode(next && next !== releaseNodeId ? next : this.rootNodeId);
      }, durationMs);
    }

    _nodeDurationMs(nodeId) {
      const durationSec = Number(this.nodeDurations[nodeId]);
      if (Number.isFinite(durationSec) && durationSec > 0) {
        return Math.max(40, durationSec * 1000);
      }
      const fallbackSec = Number(this.cfg.postSpeechEmotionHoldSec || 1.0);
      return Math.max(40, fallbackSec * 1000);
    }

    _clearPostSpeechTimer() {
      if (this.postSpeechTimer) {
        clearTimeout(this.postSpeechTimer);
        this.postSpeechTimer = null;
      }
      this.postSpeechHoldActive = false;
    }
  }

  // Live2DRenderer — deprecated VTS/Live2D foreground path kept for compatibility.
  class Live2DRenderer {
    constructor(stage) {
      this.stage = stage;
      this.container = new PIXI.Container();
      stage.addChild(this.container);
      this._model = null;
      this._available = typeof PIXI.live2d !== "undefined";
      if (!this._available) {
        console.warn("[Live2DRenderer] pixi-live2d-display is not loaded; Live2D is unavailable");
      }
    }

    async loadModel(url) {
      if (!this._available) return;
      try {
        const { Live2DModel } = PIXI.live2d;
        const model = await Live2DModel.from(url);
        if (this._model) {
          this.container.removeChild(this._model);
          this._model.destroy();
        }
        this._model = model;
        this.container.addChild(model);
        this._fitToStage();
        console.log("[Live2DRenderer] model loaded:", url);
      } catch (e) {
        console.error("[Live2DRenderer] model load failed:", e);
      }
    }

    setExpression(name) {
      if (!this._model) return;
      try {
        this._model.expression(name);
      } catch (e) {}
    }

    setMotion(group, priority) {
      if (!this._model) return;
      try {
        this._model.motion(group, undefined, priority);
      } catch (e) {}
    }

    setMouth(value) {
      if (!this._model) return;
      try {
        this._model.internalModel.coreModel.setParameterValueById(
          "ParamMouthOpenY", value
        );
      } catch (e) {}
    }

    resize(w, h) {
      if (!this._model) return;
      this._fitToStage();
    }

    _fitToStage() {
      if (!this._model) return;
      const w = app.renderer.width;
      const h = app.renderer.height;
      this._model.x = w / 2;
      this._model.y = h / 2;
      const scaleX = w / this._model.width;
      const scaleY = h / this._model.height;
      this._model.scale.set(Math.min(scaleX, scaleY));
      this._model.anchor.set(0.5);
    }
  }

  // ---------------------------------------------------------------------------
  // Subtitle overlay
  // ---------------------------------------------------------------------------
  class SubtitleOverlay {
    constructor(stage) {
      this.container = new PIXI.Container();
      stage.addChild(this.container);

      this._bg = new PIXI.Graphics();
      this.container.addChild(this._bg);

      this._text = new PIXI.Text("", {
        fontFamily: "Arial, sans-serif",
        fontSize: 16,
        fill: 0xffffff,
        wordWrap: true,
        wordWrapWidth: 400,
        align: "center",
        dropShadow: true,
        dropShadowDistance: 1,
      });
      this._text.anchor.set(0.5, 1);
      this.container.addChild(this._text);
      this.container.visible = false;
    }

    setText(text) {
      this._text.text = text;
      this.container.visible = !!text;
      this._redrawBg();
    }

    resize(w, h) {
      this._text.style.wordWrapWidth = w - 40;
      this._text.x = w / 2;
      this._text.y = h - 10;
      this._redrawBg();
    }

    _redrawBg() {
      const b = this._text.getBounds();
      this._bg.clear();
      if (!this.container.visible) return;
      this._bg.beginFill(0x000000, 0.5);
      this._bg.drawRoundedRect(b.x - 8, b.y - 6, b.width + 16, b.height + 12, 8);
      this._bg.endFill();
    }
  }

  // ---------------------------------------------------------------------------
  // RenderApp — top-level orchestrator exposed to Python
  // ---------------------------------------------------------------------------
  class RenderApp {
    constructor() {
      this.graphicsProfile = graphicsProfile;
      this._sprite = new SpriteRenderer(app.stage);
      this._live2d = new Live2DRenderer(app.stage);
      this._subtitle = new SubtitleOverlay(app.stage);
      this._spriteforgeRuntime = new SpriteForgeRuntime(this._sprite);
      this._sprite.setCycleCompleteHandler((label) => this._spriteforgeRuntime.onCycleComplete(label));
      this._mode = "sprite";  // 'sprite' | 'live2d' | 'both'

      // Initialize layers: sprite below, Live2D above, subtitles on top.
      app.stage.removeChildren();
      app.stage.addChild(this._sprite.container);
      app.stage.addChild(this._live2d.container);
      app.stage.addChild(this._subtitle.container);

      this._live2d.container.visible = false;

      // PixiJS' own resize event, fired after resizeTo applies, is more
      // reliable than window.resize. The callback w/h are physical pixels, so
      // use app.screen for logical pixels.
      app.renderer.on('resize', () => {
        const w = app.screen.width;
        const h = app.screen.height;
        this._sprite.resize(w, h);
        this._live2d.resize(w, h);
        this._subtitle.resize(w, h);
        this._sprite._showFrame(this._sprite._frameIdx);
      });

      // Force one recalculation after the first ticker frame, when the canvas
      // already has the correct size.
      app.ticker.addOnce(() => this._onResize());
    }

    // ---- Public API ----

    setEmotion(emotion) {
      if (this._mode !== "live2d") this._sprite.setEmotion(emotion);
      if (this._mode !== "sprite") this._live2d.setExpression(emotion);
    }

    setSpeaking(speaking) {
      if (this._spriteforgeRuntime.currentNodeId) {
        this._spriteforgeRuntime.setSpeaking(speaking);
      } else if (this._mode !== "live2d") {
        this._sprite.setSpeaking(speaking);
      }
    }

    setMouth(value) {
      if (this._mode !== "live2d") this._sprite.setMouth(value);
      if (this._mode !== "sprite") this._live2d.setMouth(value);
    }

    setSubtitle(text) {
      this._subtitle.setText(text);
    }

    loadSpriteFrames(emotion, urls) {
      this._sprite.loadFrames(emotion, urls);
    }

    loadTransitionFrames(fromEmotion, toEmotion, urls) {
      this._sprite.loadTransitionFrames(fromEmotion, toEmotion, urls);
    }

    async loadLive2DModel(url) {
      await this._live2d.loadModel(url);
    }

    setIdleAnimation(enabled) {
      this._sprite.setIdleAnimation(enabled);
    }

    setIdleFrameIntervalMs(emotion, intervalMs) {
      this._sprite.setIdleFrameIntervalMs(emotion, intervalMs);
    }

    setSpriteClipConfig(emotion, config) {
      this._sprite.setClipConfig(emotion, config);
    }

    loadMouthConfig(label, config) {
      this._sprite.loadMouthConfig(label, config);
    }

    loadSpriteForgeGraph(payload) {
      this._spriteforgeRuntime.loadGraph(payload || {});
    }

    triggerSpriteForgeIntent(label, options = {}) {
      this._spriteforgeRuntime.trigger(label, options);
    }

    releaseSpriteForge(options = {}) {
      this._spriteforgeRuntime.release(options);
    }

    holdSpriteFrame(which) {
      this._sprite.holdFrame(which);
    }

    holdSpriteClosedFrame() {
      this._sprite.holdClosedFrame();
    }

    clearSpriteHold() {
      this._sprite.clearHold();
    }

    setSpriteViewportBounds(bounds) {
      this._sprite.setViewportBounds(bounds);
    }

    setSpriteViewportMask(mask) {
      this._sprite.container.mask = mask || null;
    }

    getTextureStats() {
      return { ...this._sprite._frameStore.stats(), displayMiss: this._sprite._displayMisses };
    }

    getPixiApp() {
      return app;
    }

    setMode(mode) {
      const value = String(mode || "sprite").toLowerCase();
      if (value === "work" || value === "working" || value === "work_surface" || value === "provider_work") {
        return;
      }
      const normalized = value === "hybrid" ? "both" : value;
      if (["sprite", "live2d", "both"].indexOf(normalized) < 0) {
        console.warn("[RenderApp] ignoring unknown legacy render mode:", mode);
        return;
      }
      this._mode = normalized;
      this._sprite.container.visible = (normalized !== "live2d");
      this._live2d.container.visible = (normalized !== "sprite");
    }

    // ---- Internals ----

    _onResize() {
      const w = app.screen.width;
      const h = app.screen.height;
      this._sprite.resize(w, h);
      this._live2d.resize(w, h);
      this._subtitle.resize(w, h);
      this._sprite._showFrame(this._sprite._frameIdx);
    }
  }

  // Mount globally for Python runJavaScript() calls.
  window.renderApp = new RenderApp();
  console.log("[RenderEngine] renderer.js initialized");

  // ---------------------------------------------------------------------------
  // Render bridge. Embedded GUI renderers reuse the authenticated parent
  // control-plane socket; standalone development retains the direct socket.
  // ---------------------------------------------------------------------------
  (function connectWs() {
    if (window.__DISABLE_RENDERER_WS__) {
      console.log("[RenderBridge] WebSocket disabled for this host");
      return;
    }

    function dispatchRenderEvent(method, params) {
      const app = window.renderApp;
      if (!app) return;
      const p = params || {};
      if (method === "render.emotion" || method === "render.sprite_frames") {
        console.log("[RenderBridge] recv: %s %s", method, JSON.stringify(p).slice(0, 120));
      }
      switch (method) {
        case "render.emotion":
          app.setEmotion(p.emotion);
          break;
        case "render.speaking":
          app.setSpeaking(p.speaking);
          break;
        case "render.mouth":
          app.setMouth(p.value);
          break;
        case "render.subtitle":
          app.setSubtitle(p.text || "");
          break;
        case "render.sprite_frames":
          app.loadSpriteFrames(p.emotion, p.urls || []);
          break;
        case "render.mode":
          app.setMode(p.mode);
          break;
        case "render.idle_animation":
          app.setIdleAnimation(p.enabled);
          break;
        case "render.idle_frame_interval":
          app.setIdleFrameIntervalMs(p.emotion, p.intervalMs);
          break;
        case "render.sprite_clip_config":
          app.setSpriteClipConfig(p.emotion, p.config || {});
          break;
        case "render.mouth_config":
          app.loadMouthConfig(p.label, p.config || {});
          break;
        case "render.spriteforge_graph":
          app.loadSpriteForgeGraph(p);
          break;
        case "render.spriteforge_intent":
          app.triggerSpriteForgeIntent(p.label, p);
          break;
        case "render.spriteforge_release":
          app.releaseSpriteForge(p);
          break;
        case "render.hold_frame":
          app.holdSpriteFrame(p.which);
          break;
        case "render.clear_hold":
          app.clearSpriteHold();
          break;
      }
    }

    if (window.parent !== window) {
      window.addEventListener("message", event => {
        if (event.source !== window.parent) return;
        const message = event.data;
        if (!message || message.type !== "amadeus.render.event") return;
        dispatchRenderEvent(String(message.method || ""), message.params || {});
      });
      console.log("[RenderBridge] using authenticated parent event channel");
      return;
    }

    // Read backend WS URL from query param, or default to port 17777
    const params = new URLSearchParams(window.location.search);
    const wsUrl = params.get("ws") || `ws://127.0.0.1:17777/ws`;

    let ws = null;
    let reconnectTimer = null;

    function doConnect() {
      if (ws && ws.readyState === WebSocket.OPEN) return;

      try {
        ws = new WebSocket(wsUrl);
      } catch (e) {
        console.warn("[RenderBridge] WebSocket connect failed:", e);
        scheduleReconnect();
        return;
      }

      ws.onopen = () => {
        console.log("[RenderBridge] connected to", wsUrl);
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        // Request a state replay from the server
        try { ws.send(JSON.stringify({method: "render.ready", params: {}})); } catch(e) {}
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type !== "evt") return;
          dispatchRenderEvent(msg.method, msg.params || {});
        } catch (e) {
          console.warn("[RenderBridge] message error:", e);
        }
      };

      ws.onclose = () => {
        console.log("[RenderBridge] disconnected");
        ws = null;
        scheduleReconnect();
      };

      ws.onerror = () => {
        ws = null;
        scheduleReconnect();
      };
    }

    function scheduleReconnect() {
      if (reconnectTimer) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        doConnect();
      }, 2000);
    }

    doConnect();
  })();

})();
