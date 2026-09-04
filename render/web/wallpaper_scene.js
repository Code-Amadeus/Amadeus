/**
 * Desktop wallpaper adapter.
 *
 * Ownership boundary:
 * - character runtime delegates to render/web/renderer.js
 * - desktop scene owns wallpaper background, CRT mask, and occasional scanline noise
 * - wallpaperApp keeps the Python-facing flat API for compatibility
 */
(function () {
  "use strict";

  function callRender(method, args) {
    if (!window.renderApp || typeof window.renderApp[method] !== "function") {
      console.warn("[WallpaperScene] renderApp method unavailable:", method);
      return undefined;
    }
    return window.renderApp[method].apply(window.renderApp, args || []);
  }

  function diag(event, data, level) {
    const payload = data || {};
    const logLevel = level || "info";
    try {
      const fn = logLevel === "error" ? console.error : (logLevel === "warning" ? console.warn : console.info);
      fn.call(console, "[WallpaperDiag]", event, payload);
    } catch (err) {}
    try {
      if (typeof window.__weBridgeLog === "function") {
        window.__weBridgeLog(event, payload, logLevel);
      }
    } catch (err) {}
  }

  function urlFlagEnabled(name) {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = String(params.get(name) || "").toLowerCase();
      return value === "1" || value === "true" || value === "yes" || value === "on";
    } catch (err) {
      return false;
    }
  }

  function urlFlagDisabled(name) {
    try {
      const params = new URLSearchParams(window.location.search || "");
      const value = String(params.get(name) || "").toLowerCase();
      return value === "0" || value === "false" || value === "no" || value === "off";
    } catch (err) {
      return false;
    }
  }

  const characterRuntime = {
    setMode(mode) { callRender("setMode", [mode]); },
    loadSpriteFrames(emotion, urls) { callRender("loadSpriteFrames", [emotion, urls]); },
    loadSpriteClipFrames(emotion, inUrls, loopUrls, outUrls) {
      callRender("loadSpriteClipFrames", [emotion, inUrls, loopUrls, outUrls]);
    },
    setSpriteClipConfig(emotion, cfg) { callRender("setSpriteClipConfig", [emotion, cfg]); },
    setIdleFrameIntervalMs(emotion, ms) { callRender("setIdleFrameIntervalMs", [emotion, ms]); },
    loadMouthConfig(emotion, cfg) { callRender("loadMouthConfig", [emotion, cfg]); },
    setEmotion(emotion) { callRender("setEmotion", [emotion]); },
    setSpeaking(speaking) { callRender("setSpeaking", [speaking]); },
    setIdleAnimation(playing) { callRender("setIdleAnimation", [playing]); },
    setMouth(value) { callRender("setMouth", [value]); },
    loadSpriteForgeGraph(payload) { callRender("loadSpriteForgeGraph", [payload]); },
    triggerSpriteForgeIntent(label, options) { callRender("triggerSpriteForgeIntent", [label, options || {}]); },
    holdSpriteFrame(frameIndex) { callRender("holdSpriteFrame", [frameIndex]); },
    clearSpriteHold() { callRender("clearSpriteHold", []); },
    releaseSpriteForge(options) { callRender("releaseSpriteForge", [options || {}]); },
    setSubtitle(text) { callRender("setSubtitle", [text]); },
  };

  const wallpaperSubtitle = {
    app: null,
    container: null,
    frame: null,
    text: null,
    frameUrl: "",
    currentText: "",
    enabled: true,
    _aspect: 2564 / 430,
    _bounds: null,

    init(app, payload) {
      this.app = app;
      const params = new URLSearchParams(window.location.search || "");
      this.enabled = !urlFlagDisabled("wallpaperSubtitle") && !urlFlagEnabled("noWallpaperSubtitle");
      this.frameUrl = String((payload && payload.subtitleFrameUrl) || params.get("subtitleFrame") || "");
      if (!this.container && this.app && window.PIXI) {
        this.container = new PIXI.Container();
        this.container.visible = false;
        this.frame = new PIXI.Sprite();
        this.text = new PIXI.Text("", {
          fontFamily: "Arial, 'Microsoft YaHei', sans-serif",
          fontSize: 15,
          fill: 0xf2f7fb,
          align: "center",
          wordWrap: true,
          wordWrapWidth: 360,
          breakWords: true,
          lineHeight: 20,
          dropShadow: true,
          dropShadowColor: 0x071019,
          dropShadowAlpha: 0.85,
          dropShadowDistance: 1,
          dropShadowBlur: 2,
        });
        this.text.anchor.set(0.5, 0.5);
        this.container.addChild(this.frame);
        this.container.addChild(this.text);
        this.app.stage.addChild(this.container);
      }
      this._loadFrame();
      this.updateVisibility();
    },

    _loadFrame() {
      if (!this.frame || !this.frameUrl || !window.PIXI) return;
      const tex = PIXI.Texture.from(this.frameUrl);
      const apply = () => {
        if (!this.frame || !tex || !tex.baseTexture || !tex.baseTexture.valid) return;
        this.frame.texture = tex;
        this._aspect = Math.max(1, (tex.width || 2564) / Math.max(1, tex.height || 430));
        this.layout(this._bounds);
        diag("subtitle.frame_ready", { url: this.frameUrl, width: tex.width || 0, height: tex.height || 0 });
      };
      if (tex.baseTexture && tex.baseTexture.valid) {
        apply();
      } else if (tex.baseTexture) {
        tex.baseTexture.once("loaded", apply);
        tex.baseTexture.once("error", (err) => diag("subtitle.frame_error", { error: String(err) }, "warning"));
      }
    },

    setText(value) {
      this.currentText = String(value || "").trim();
      if (this.text) this.text.text = this.currentText;
      this.layout(this._bounds);
      this.updateVisibility();
    },

    layout(bounds) {
      this._bounds = bounds || this._bounds;
      if (!this.container || !this.frame || !this.text || !this._bounds) return;
      const b = this._bounds;
      const width = Math.max(180, Math.min(b.width * 0.72, b.width - 24));
      const height = Math.max(34, Math.min(width / this._aspect, b.height * 0.16));
      const x = b.x + (b.width - width) / 2;
      const y = b.y + b.height - height - Math.max(3, b.height * 0.012);
      this.frame.x = x;
      this.frame.y = y;
      if (this.frame.texture && this.frame.texture.valid) {
        this.frame.width = width;
        this.frame.height = height;
      }
      const fontSize = Math.max(11, Math.min(17, Math.round(width / 34)));
      this.text.style.fontSize = fontSize;
      this.text.style.lineHeight = Math.round(fontSize * 1.32);
      this.text.style.wordWrapWidth = width * 0.82;
      this.text.x = x + width / 2;
      this.text.y = y + height * 0.52;
    },

    updateVisibility() {
      if (!this.container) return;
      const visible = !!(this.enabled && this.currentText);
      this.container.visible = visible;
      if (visible && this.app && this.app.stage && this.app.stage.children.includes(this.container)) {
        this.app.stage.addChild(this.container);
      }
    },
  };

  const keyboardSfx = {
    ctx: null,
    master: null,
    sampleUrl: "",
    sampleBuffer: null,
    sampleSource: null,
    sampleGain: null,
    sampleLoading: false,
    sampleFailed: false,
    enabled: false,
    suppressed: false,
    ducked: false,
    resumeHandlersInstalled: false,
    lastDiagAt: 0,
    elapsed: 0,
    nextDelay: 0.12,
    masterVolume: 0.45,
    duckedVolume: 0.09,
    sampleVolume: 4.0,

    configure(options) {
      const nextUrl = String((options && options.sampleUrl) || "");
      if (this.sampleUrl !== nextUrl) {
        this.sampleUrl = nextUrl;
        this.sampleBuffer = null;
        this.sampleFailed = false;
        this.sampleLoading = false;
        this._stopSampleLoop();
      }
    },

    setEnabled(enabled) {
      const next = !!enabled;
      if (this.enabled === next) return;
      this.enabled = next;
      this.elapsed = 0;
      this.nextDelay = this._nextDelay(true);
      diag("keyboard_sfx.enabled", { enabled: next, sampleUrl: this.sampleUrl || "" });
      if (next) {
        this._resume();
        this._applyVolume(0.04);
        this._loadSample();
      } else {
        this._stopSampleLoop();
      }
    },

    setSuppressed(suppressed) {
      const next = !!suppressed;
      if (this.suppressed === next) return;
      this.suppressed = next;
      if (this.suppressed) {
        this._stopSampleLoop();
      } else if (this.enabled) {
        this._resume();
        this._applyVolume(0.08);
        if (this.sampleBuffer) this._startSampleLoop();
      }
    },

    setDucked(ducked) {
      const next = !!ducked;
      if (this.ducked === next) return;
      this.ducked = next;
      this._applyVolume(next ? 0.18 : 0.20);
      diag("keyboard_sfx.ducked", { ducked: next, volume: this._targetVolume() });
    },

    ensureAudible() {
      if (!this.enabled || this.suppressed) return;
      this._resume();
      this._applyVolume(0.08);
      if (this.sampleBuffer) this._startSampleLoop();
      else if (this.sampleUrl && !this.sampleFailed) this._loadSample();
    },

    tick(dt) {
      if (!this.enabled || this.suppressed) return;
      if (!this._ensure()) return;
      this._resume();
      if (this.ctx && this.ctx.state !== "running") {
        this._throttledDiag("keyboard_sfx.audio_context_suspended", { state: this.ctx.state || "" }, "warning");
        return;
      }
      if (this.sampleBuffer) {
        this._startSampleLoop();
        return;
      }
      if (this.sampleUrl && !this.sampleFailed) {
        this._loadSample();
        return;
      }
      this.elapsed += Math.max(0, dt || 0);
      if (this.elapsed < this.nextDelay) return;
      this.elapsed = 0;
      this.nextDelay = this._nextDelay(false);
      this._click();
    },

    _ensure() {
      if (this.ctx && this.master) return true;
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return false;
      try {
        this.ctx = new AudioCtx();
        this.master = this.ctx.createGain();
        this.master.gain.value = this._targetVolume();
        this.master.connect(this.ctx.destination);
        this._installResumeHandlers();
        return true;
      } catch (err) {
        return false;
      }
    },

    _targetVolume() {
      return this.ducked ? this.duckedVolume : this.masterVolume;
    },

    _applyVolume(rampSeconds) {
      if (!this.master || !this.ctx) return;
      const now = this.ctx.currentTime || 0;
      const target = Math.max(0.0001, this._targetVolume());
      try {
        this.master.gain.cancelScheduledValues(now);
        this.master.gain.setValueAtTime(Math.max(0.0001, this.master.gain.value || target), now);
        this.master.gain.exponentialRampToValueAtTime(target, now + Math.max(0.01, Number(rampSeconds) || 0.08));
      } catch (err) {
        this.master.gain.value = target;
      }
    },

    _resume() {
      if (!this._ensure()) return;
      if (this.ctx && this.ctx.state === "suspended") {
        try {
          const result = this.ctx.resume();
          if (result && typeof result.catch === "function") result.catch(() => {});
        } catch (err) {}
      }
    },

    _loadSample() {
      if (!this.sampleUrl || this.sampleBuffer || this.sampleLoading || this.sampleFailed) return;
      if (!this._ensure()) return;
      this.sampleLoading = true;
      fetch(this.sampleUrl, { cache: "force-cache" })
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.arrayBuffer();
        })
        .then((data) => this.ctx.decodeAudioData(data))
        .then((buffer) => {
          this.sampleBuffer = buffer;
          this.sampleFailed = false;
          console.info("[KeyboardSfx] sample loaded:", this.sampleUrl);
          diag("keyboard_sfx.sample_loaded", { url: this.sampleUrl, duration: Number((buffer.duration || 0).toFixed(3)) });
          if (this.enabled && !this.suppressed) this._startSampleLoop();
        })
        .catch((err) => {
          this.sampleFailed = true;
          console.warn("[KeyboardSfx] sample load failed; using procedural fallback:", err);
          diag("keyboard_sfx.sample_failed", { url: this.sampleUrl, error: String(err && (err.message || err)) }, "warning");
        })
        .finally(() => {
          this.sampleLoading = false;
        });
    },

    _startSampleLoop() {
      if (this.sampleSource || !this.sampleBuffer || !this._ensure()) return;
      const now = this.ctx.currentTime;
      const source = this.ctx.createBufferSource();
      const gain = this.ctx.createGain();
      source.buffer = this.sampleBuffer;
      source.loop = true;
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(this.sampleVolume, now + 0.16);
      source.connect(gain);
      gain.connect(this.master);
      source.onended = () => {
        try { gain.disconnect(); } catch (err) {}
        if (this.sampleSource === source) {
          this.sampleSource = null;
          this.sampleGain = null;
          if (this.enabled && !this.suppressed) {
            diag("keyboard_sfx.loop_ended_recover", {});
            this._startSampleLoop();
          }
        }
      };
      const duration = Math.max(0.01, this.sampleBuffer.duration || 0.01);
      const stableStart = duration > 2.2 ? 1.35 : 0;
      const stableSpan = Math.max(0.01, duration - stableStart - 0.35);
      const offset = stableStart + Math.random() * stableSpan;
      source.start(now, offset);
      this.sampleSource = source;
      this.sampleGain = gain;
      diag("keyboard_sfx.loop_start", {
        state: this.ctx ? this.ctx.state : "",
        offset: Number(offset.toFixed(2)),
        masterGain: this.master ? Number((this.master.gain.value || 0).toFixed(2)) : 0,
        sampleGain: this.sampleVolume,
      });
    },

    _stopSampleLoop() {
      const source = this.sampleSource;
      const gain = this.sampleGain;
      this.sampleSource = null;
      this.sampleGain = null;
      if (!source) return;
      diag("keyboard_sfx.loop_stop", {});
      try {
        const now = this.ctx ? this.ctx.currentTime : 0;
        if (gain && this.ctx) {
          gain.gain.cancelScheduledValues(now);
          gain.gain.setValueAtTime(Math.max(0.0001, gain.gain.value || 0.0001), now);
          gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.08);
        }
        source.stop((this.ctx ? this.ctx.currentTime : 0) + 0.09);
      } catch (err) {
        try { source.disconnect(); } catch (_e) {}
      }
    },

    _throttledDiag(event, data, level) {
      const now = Date.now();
      if (now - this.lastDiagAt < 2000) return;
      this.lastDiagAt = now;
      diag(event, data || {}, level || "info");
    },

    _installResumeHandlers() {
      if (this.resumeHandlersInstalled) return;
      this.resumeHandlersInstalled = true;
      const resume = () => this._resume();
      try {
        window.addEventListener("pointerdown", resume, { passive: true });
        window.addEventListener("keydown", resume);
        window.addEventListener("touchstart", resume, { passive: true });
      } catch (err) {}
    },

    _nextDelay(first) {
      if (first) return 0.08 + Math.random() * 0.10;
      if (Math.random() < 0.08) return 0.34 + Math.random() * 0.44;
      return 0.055 + Math.random() * 0.135;
    },

    _noiseBuffer(duration) {
      const sampleRate = this.ctx.sampleRate || 48000;
      const length = Math.max(1, Math.floor(sampleRate * duration));
      const buffer = this.ctx.createBuffer(1, length, sampleRate);
      const data = buffer.getChannelData(0);
      for (let i = 0; i < length; i += 1) {
        const t = i / Math.max(1, length - 1);
        const envelope = Math.pow(1 - t, 3.2);
        data[i] = (Math.random() * 2 - 1) * envelope;
      }
      return buffer;
    },

    _click() {
      if (!this.ctx || !this.master) return;
      const now = this.ctx.currentTime;
      const duration = 0.018 + Math.random() * 0.014;
      const source = this.ctx.createBufferSource();
      const filter = this.ctx.createBiquadFilter();
      const gain = this.ctx.createGain();
      source.buffer = this._noiseBuffer(duration);
      filter.type = "bandpass";
      filter.frequency.value = 1500 + Math.random() * 1300;
      filter.Q.value = 0.9 + Math.random() * 0.8;
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.16 + Math.random() * 0.07, now + 0.002);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + duration);
      source.connect(filter);
      filter.connect(gain);
      gain.connect(this.master);
      source.start(now);
      source.stop(now + duration + 0.006);

      const thock = this.ctx.createOscillator();
      const thockGain = this.ctx.createGain();
      thock.type = "triangle";
      thock.frequency.setValueAtTime(92 + Math.random() * 38, now);
      thockGain.gain.setValueAtTime(0.0001, now);
      thockGain.gain.exponentialRampToValueAtTime(0.035, now + 0.004);
      thockGain.gain.exponentialRampToValueAtTime(0.0001, now + 0.045);
      thock.connect(thockGain);
      thockGain.connect(this.master);
      thock.start(now);
      thock.stop(now + 0.052);
    },
  };

  const scenarioRuntime = {
    app: null,
    container: null,
    backplateSprite: null,
    sprite: null,
    payload: null,
    graph: { nodes: [], edges: [] },
    resources: {},
    enabled: false,
    active: false,
    activeActivity: null,
    activityConfigs: {},
    activityDefaults: {
      work: {
        entryLabel: "computer use",
        labels: ["computer use"],
        resourceHints: ["computer_use/computer_uses_mastered", "computer_uses_mastered"],
        sceneIds: ["computer_use"],
        keyboardSfxLabels: ["computer use"],
        keyboardSfxResourceHints: ["computer_use/computer_uses_mastered", "computer_uses_mastered"],
        holdDuringSpeech: true,
        stayWithinActivity: true,
      },
    },
    speaking: false,
    asrStatus: "",
    asrSuppressesWorkSfx: false,
    idleSeconds: 0,
    inactivitySeconds: 60,
    staticHoldSeconds: 15,
    sourceCrop: null,
    sourceCropNorm: null,
    backplateCropNorm: null,
    placementMode: "cover",
    backplateUrl: "",
    currentNodeId: null,
    currentResource: null,
    frameIndex: 0,
    frameElapsed: 0,
    nodeElapsed: 0,
    _savedVisibility: null,
    _fadeAlpha: 1,
    _fadeDur: 0.35,
    _fadeT: 0,
    _fadeDir: 0,
    _pendingNode: null,
    _fadingOut: false,
    _fadeCharacterIn: false,
    _characterFadeTargets: null,
    _textureCache: null,
    _maxTextureCache: 256,
    _textureLoading: false,
    _textureLoadToken: 0,
    _textureLoadStartedAt: 0,
    _textureLoadUrl: "",
    _textureLoadTimeoutMs: 12000,
    _pendingFetchController: null,
    _lastHeartbeatAt: 0,
    _lastComputerUseGateKey: "",
    enableFramePlayback: false,

    init(app, payload) {
      this.app = app;
      this.payload = payload || {};
      this.enabled = !!this.payload.enabled;
      this.graph = this.payload.graph || { nodes: [], edges: [] };
      this.resources = this.payload.resources || {};
      this.activityConfigs = Object.assign({}, this.activityDefaults, this.payload.activities || {});
      keyboardSfx.configure({
        sampleUrl: String(this.payload.keyboardSfxUrl || ""),
      });
      this.inactivitySeconds = Math.max(1, Number(this.payload.inactivitySeconds) || 60);
      this.staticHoldSeconds = Math.max(1, Number(this.payload.staticHoldSeconds) || 15);
      this._maxTextureCache = Math.max(32, Number(this.payload.maxTextureCache) || 256);
      this.enableFramePlayback = !!this.payload.enableFramePlayback;
      if (!this._textureCache) this._textureCache = new Map();
      this.sourceCrop = Array.isArray(this.payload.sourceCrop) ? this.payload.sourceCrop : null;
      this.sourceCropNorm = Array.isArray(this.payload.sourceCropNorm) ? this.payload.sourceCropNorm : null;
      this.backplateCropNorm = Array.isArray(this.payload.backplateCropNorm) ? this.payload.backplateCropNorm : null;
      this.placementMode = String(this.payload.placementMode || "cover");
      this.backplateUrl = String(this.payload.backplateUrl || "");
      if (!this.container && this.app && window.PIXI) {
        this.container = new PIXI.Container();
        this.container.visible = false;
        this.backplateSprite = new PIXI.Sprite();
        this.sprite = new PIXI.Sprite();
        this.backplateSprite.visible = false;
        this.app.stage.addChild(this.backplateSprite);
        this.container.addChild(this.sprite);
        this.app.stage.addChild(this.container);
        this._syncLayerOrder();
      }
      if (this.backplateUrl) this._setBackplate(this.backplateUrl);
      if (!this.enabled) {
        this._updateComputerUseSfx();
        console.info("[ScenarioRuntime] disabled:", this.payload.reason || "no scenario payload");
        diag("scenario.disabled", { reason: this.payload.reason || "no scenario payload" });
        return;
      }
      console.info("[ScenarioRuntime] ready:", {
        nodes: (this.graph.nodes || []).length,
        staticHoldSeconds: this.staticHoldSeconds,
        inactivitySeconds: this.inactivitySeconds,
      });
      diag("scenario.ready", {
        nodes: (this.graph.nodes || []).length,
        staticHoldSeconds: this.staticHoldSeconds,
        inactivitySeconds: this.inactivitySeconds,
        resources: Object.keys(this.resources || {}).length,
        framePlayback: this.enableFramePlayback,
        hasBackplate: !!this.backplateUrl,
      });
    },

    layout(bounds, mask) {
      if (!this.container || !this.sprite) return;
      this._syncLayerOrder();
      this.container.mask = mask || null;
      if (this.backplateSprite) {
        this.backplateSprite.mask = mask || null;
        this._placeSpriteIn(this.backplateSprite, bounds);
      }
      this._placeSprite(bounds);
    },

    _syncLayerOrder() {
      if (!this.app || !this.app.stage) return;
      const stage = this.app.stage;
      const renderApp = window.renderApp || {};
      const spriteLayer = renderApp._sprite && renderApp._sprite.container;
      const live2dLayer = renderApp._live2d && renderApp._live2d.container;
      const subtitleLayer = wallpaperSubtitle && wallpaperSubtitle.container;
      const characterLayers = [spriteLayer, live2dLayer].filter((layer) => layer && stage.children.includes(layer));

      if (this.backplateSprite && stage.children.includes(this.backplateSprite)) {
        stage.removeChild(this.backplateSprite);
      }
      if (subtitleLayer && stage.children.includes(subtitleLayer)) {
        stage.removeChild(subtitleLayer);
      }
      if (this.container && stage.children.includes(this.container)) {
        stage.removeChild(this.container);
      }

      const bgIndex = this.backplateSprite ? stage.children.indexOf(desktopScene.bg) : -1;
      const characterIndexes = characterLayers
        .map((layer) => stage.children.indexOf(layer))
        .filter((index) => index >= 0);
      const characterBottomIndex = characterIndexes.length ? Math.min.apply(null, characterIndexes) : -1;
      const characterTopIndex = characterIndexes.length ? Math.max.apply(null, characterIndexes) : -1;
      const backplateIndex = characterBottomIndex >= 0 ? characterBottomIndex : Math.max(0, bgIndex + 1);
      if (this.backplateSprite) {
        stage.addChildAt(this.backplateSprite, Math.min(backplateIndex, stage.children.length));
      }

      const refreshedCharacterTopIndex = characterLayers
        .map((layer) => stage.children.indexOf(layer))
        .filter((index) => index >= 0)
        .reduce((max, index) => Math.max(max, index), characterTopIndex >= 0 ? characterTopIndex : -1);
      if (subtitleLayer) {
        const subtitleIndex = refreshedCharacterTopIndex >= 0 ? refreshedCharacterTopIndex + 1 : stage.children.length;
        stage.addChildAt(subtitleLayer, Math.min(subtitleIndex, stage.children.length));
      }

      const refreshedSubtitleIndex = subtitleLayer ? stage.children.indexOf(subtitleLayer) : -1;
      const scenarioIndex = refreshedSubtitleIndex >= 0
        ? refreshedSubtitleIndex + 1
        : (refreshedCharacterTopIndex >= 0 ? refreshedCharacterTopIndex + 1 : stage.children.length);
      if (this.container) {
        stage.addChildAt(this.container, Math.min(scenarioIndex, stage.children.length));
      }
    },

    noteActivity() {
      this.idleSeconds = 0;
      if (this.activeActivity) return;
      if (this.active) this.deactivate();
    },

    setSpeaking(speaking) {
      this.speaking = !!speaking;
      this._updateComputerUseSfx();
      if (this.activeActivity && this._activityHoldDuringSpeech(this.activeActivity)) {
        this.idleSeconds = 0;
        if (!this.active) this._ensureActivityScene(this.activeActivity);
        return;
      }
      if (this.speaking) {
        this.noteActivity();
      } else {
        this.idleSeconds = 0;
      }
    },

    setActivity(activity) {
      const next = this._normalizeActivity(activity);
      if (this.activeActivity === next) {
        if (next) this._ensureActivityScene(next);
        return;
      }
      this.activeActivity = next;
      this.idleSeconds = 0;
      this._updateComputerUseSfx();
      diag("scenario.activity", { activity: this.activeActivity || "" });
      if (this.activeActivity) {
        this._ensureActivityScene(this.activeActivity);
      } else if (this.active) {
        this.deactivate();
      }
    },

    setWorkMode(enabled) {
      this.setActivity(enabled ? "work" : null);
    },

    setAsrStatus(payload) {
      const status = String((payload && payload.status) || payload || "").trim().toLowerCase();
      this.asrStatus = status;
      this.asrSuppressesWorkSfx = this._asrStatusSuppressesWorkSfx(status);
      this._updateComputerUseSfx();
    },

    tick(dt) {
      keyboardSfx.tick(dt);
      if (!this.enabled || !this.container || !desktopScene._crtBounds) return;
      this._updateComputerUseSfx();
      this._tickFade(dt);
      if (this.activeActivity && !this.active) {
        this._ensureActivityScene(this.activeActivity);
        return;
      }
      if (!this.active) {
        if (!this.speaking) {
          this.idleSeconds += dt;
          if (this.idleSeconds >= this.inactivitySeconds) this.activate();
        }
        return;
      }
      if (this.speaking && !(this.activeActivity && this._activityHoldDuringSpeech(this.activeActivity))) {
        this.deactivate();
        return;
      }
      if (this._fadeDir !== 0) return;
      if (this._textureLoading) {
        const elapsed = Date.now() - this._textureLoadStartedAt;
        if (elapsed > this._textureLoadTimeoutMs) {
          diag("scenario.texture_timeout", {
            url: this._textureLoadUrl,
            elapsedMs: elapsed,
            containerVisible: !!(this.container && this.container.visible),
          }, "error");
          this._textureLoading = false;
          if (!this.container || !this.container.visible) {
            this._abortActivation("texture load timeout before first scenario frame");
          }
        }
        return;
      }
      if (!this.container.visible) return;
      this._heartbeat();
      this.nodeElapsed += dt;
      if (this.currentResource && this.currentResource.type === "frames") {
        this._tickFrames(dt);
        return;
      }
      if (this.nodeElapsed >= this.staticHoldSeconds) {
        this._goNext();
      }
    },

    _textureState(texture) {
      const base = texture && texture.baseTexture;
      return {
        present: !!texture,
        valid: !!(texture && texture.valid),
        width: texture ? (texture.width || 0) : 0,
        height: texture ? (texture.height || 0) : 0,
        baseValid: !!(base && base.valid),
        baseWidth: base ? (base.width || 0) : 0,
        baseHeight: base ? (base.height || 0) : 0,
        destroyed: !!(base && base.destroyed),
      };
    },

    _displayState(displayObject) {
      return {
        present: !!displayObject,
        visible: !!(displayObject && displayObject.visible),
        renderable: !!(displayObject && displayObject.renderable !== false),
        alpha: displayObject && Number.isFinite(displayObject.alpha) ? Number(displayObject.alpha.toFixed(3)) : null,
        blendMode: displayObject && displayObject.blendMode !== undefined ? displayObject.blendMode : null,
        texture: this._textureState(displayObject && displayObject.texture),
      };
    },

    _heartbeat(force) {
      const now = Date.now();
      if (!force && now - this._lastHeartbeatAt < 1000) return;
      this._lastHeartbeatAt = now;
      const renderer = this.app && this.app.renderer;
      const gl = renderer && renderer.gl;
      const stage = this.app && this.app.stage;
      const bg = desktopScene && desktopScene.bg;
      const children = stage && stage.children ? stage.children.slice(0, 12).map((child, index) => ({
        index,
        type: child && child.constructor ? child.constructor.name : "",
        visible: !!(child && child.visible),
        renderable: child && child.renderable !== false,
        alpha: child && Number.isFinite(child.alpha) ? Number(child.alpha.toFixed(3)) : null,
        children: child && child.children ? child.children.length : 0,
      })) : [];
      diag("scenario.heartbeat", {
        active: this.active,
        node: this.currentNodeId || "",
        loading: this._textureLoading,
        loadUrl: this._textureLoadUrl || "",
        container: {
          visible: !!(this.container && this.container.visible),
          alpha: this.container && Number.isFinite(this.container.alpha) ? Number(this.container.alpha.toFixed(3)) : null,
          children: this.container && this.container.children ? this.container.children.length : 0,
        },
        sprite: {
          visible: !!(this.sprite && this.sprite.visible),
          alpha: this.sprite && Number.isFinite(this.sprite.alpha) ? Number(this.sprite.alpha.toFixed(3)) : null,
          x: this.sprite ? Math.round(this.sprite.x || 0) : 0,
          y: this.sprite ? Math.round(this.sprite.y || 0) : 0,
          scaleX: this.sprite && this.sprite.scale ? Number((this.sprite.scale.x || 0).toFixed(4)) : 0,
          scaleY: this.sprite && this.sprite.scale ? Number((this.sprite.scale.y || 0).toFixed(4)) : 0,
          texture: this._textureState(this.sprite && this.sprite.texture),
        },
        bg: {
          visible: !!(bg && bg.visible),
          alpha: bg && Number.isFinite(bg.alpha) ? Number(bg.alpha.toFixed(3)) : null,
          texture: this._textureState(bg && bg.texture),
        },
        renderer: {
          screen: this.app && this.app.screen ? [this.app.screen.width, this.app.screen.height] : [0, 0],
          contextLost: !!(gl && typeof gl.isContextLost === "function" && gl.isContextLost()),
        },
        ambient: {
          layer: this._displayState(desktopScene && desktopScene.ambientLayer),
          low: this._displayState(desktopScene && desktopScene.ambientLowSprite),
          delta: this._displayState(desktopScene && desktopScene.ambientSprite),
          glow: this._displayState(desktopScene && desktopScene.glowLayer),
          scanline: this._displayState(desktopScene && desktopScene.scanlineLayer),
        },
        stageChildren: children,
      });
    },

    activate(preferredNode) {
      if (!this.enabled || this.active) return;
      const startNode = preferredNode || this._startNode();
      if (!startNode) return;
      diag("scenario.activate", { startNode: startNode.id, label: startNode.label || "" });
      this.active = true;
      this.idleSeconds = 0;
      this._lastHeartbeatAt = 0;
      if (this.container) {
        this.container.visible = false;
        this.container.alpha = 0;
      }
      const ok = this._showNode(startNode.id, () => this._completeActivate(startNode));
      if (!ok) this._abortActivation("start node resource unavailable");
    },

    deactivate() {
      if (!this.active || this._fadingOut) return;
      if (!this.container || !this.container.visible) {
        this._abortActivation("interrupted before first scenario frame");
        return;
      }
      this._pendingNode = null;
      this._fadingOut = true;
      this._updateComputerUseSfx();
      this._restoreCharacter(0);
      this._fadeCharacterIn = true;
      this._startFade(-1);
      console.info("[ScenarioRuntime] exit idle scenario graph (fading)");
    },

    _completeActivate(startNode) {
      if (!this.active || !this.container) return;
      if (!this._savedVisibility) this._saveAndHideCharacter();
      this.container.visible = true;
      this.container.alpha = 0;
      this._startFade(1);
      console.info("[ScenarioRuntime] enter idle scenario graph:", startNode.label || startNode.id);
      diag("scenario.enter", { node: startNode.id, label: startNode.label || "" });
      this._updateComputerUseSfx();
      this._heartbeat(true);
    },

    _abortActivation(reason) {
      console.warn("[ScenarioRuntime] idle scenario aborted:", reason);
      diag("scenario.abort", { reason: reason }, "warning");
      this.active = false;
      this._pendingNode = null;
      this._fadingOut = false;
      this._fadeDir = 0;
      this._textureLoading = false;
      this._textureLoadToken += 1;
      if (this._pendingFetchController) {
        try { this._pendingFetchController.abort(); } catch (_e) {}
        this._pendingFetchController = null;
      }
      this.currentNodeId = null;
      this.currentResource = null;
      this._updateComputerUseSfx();
      if (this.container) {
        this.container.visible = false;
        this.container.alpha = 0;
      }
      if (this.sprite && (!this.sprite.texture || this.sprite.texture === PIXI.Texture.EMPTY)) {
        this.sprite.visible = false;
      }
      this._restoreCharacter();
    },

    _startFade(dir) {
      this._fadeDir = dir;
      this._fadeT = 0;
      this._fadeAlpha = dir > 0 ? 0 : 1;
      if (this.container) this.container.alpha = this._fadeAlpha;
    },

    _tickFade(dt) {
      if (this._fadeDir === 0) return;
      this._fadeT = Math.min(1, this._fadeT + dt / Math.max(0.01, this._fadeDur));
      this._fadeAlpha = this._fadeDir > 0 ? this._fadeT : (1 - this._fadeT);
      if (this.container) this.container.alpha = this._fadeAlpha;
      if (this._fadeDir < 0 && this._fadeCharacterIn) {
        this._setCharacterFadeProgress(this._fadeT);
      }
      if (this._fadeT < 1) return;
      this._fadeDir = 0;
      if (this._fadingOut) {
        this._fadingOut = false;
        this._completeDeactivate();
      } else if (this._pendingNode) {
        const id = this._pendingNode;
        this._pendingNode = null;
        this._showNode(id, () => this._startFade(1));
      }
    },

    _completeDeactivate() {
      this.active = false;
      this.currentNodeId = null;
      this.currentResource = null;
      this._updateComputerUseSfx();
      this.nodeElapsed = 0;
      this.frameElapsed = 0;
      if (this.container) this.container.visible = false;
      if (this._fadeCharacterIn) {
        this._setCharacterFadeProgress(1);
        this._fadeCharacterIn = false;
      } else {
        this._restoreCharacter();
      }
      console.info("[ScenarioRuntime] exit complete");
    },

    _startNode() {
      const nodes = this.graph.nodes || [];
      if (this.activeActivity) {
        const activityNode = this._activityNode(this.activeActivity);
        if (activityNode) return activityNode;
      }
      return nodes.find((n) => String(n.sceneId || "").toLowerCase() === "stand_thinking") || nodes[0] || null;
    },

    _activityConfig(activity) {
      const key = String(activity || "");
      return Object.assign({}, this.activityDefaults[key] || {}, this.activityConfigs[key] || {});
    },

    _normalizeActivity(activity) {
      const value = String(activity || "").trim().toLowerCase();
      if (!value) return null;
      if (value === "computer use" || value === "computer_use" || value === "computer-use") return "work";
      return value;
    },

    _activityHoldDuringSpeech(activity) {
      const cfg = this._activityConfig(activity);
      return cfg.holdDuringSpeech !== false;
    },

    _asrStatusSuppressesWorkSfx(status) {
      const value = String(status || "").toLowerCase();
      // Passive hot listening should not mute the work ambience; it is how the
      // user can interrupt while Kurisu keeps working. These states represent
      // active speech/playback handoff windows where extra key clicks are noisy.
      return [
        "loading",
        "paused_tts",
        "waiting_turn_complete",
        "capturing",
        "recording",
        "speech_start",
        "vad_start",
      ].indexOf(value) >= 0;
    },

    _isComputerUseActivityActive() {
      // The backend "work" signal enters the computer-use activity domain, but
      // the keyboard loop belongs to the concrete graph node. This keeps
      // transitions such as sleep_desk/read/stand nodes from carrying key clicks.
      return this._currentNodeAllowsKeyboardSfx(this.activeActivity);
    },

    _currentNodeMatchesActivity(activity) {
      if (!this.active || !this.currentNodeId) return false;
      const ids = this._activityNodeIds(activity);
      if (ids.has(this.currentNodeId)) return true;
      const cfg = this._activityConfig(activity);
      const node = this._nodeById(this.currentNodeId) || {};
      const labels = (Array.isArray(cfg.labels) ? cfg.labels : (cfg.entryLabel ? [cfg.entryLabel] : []))
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const hints = (Array.isArray(cfg.resourceHints) ? cfg.resourceHints : [])
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const sceneIds = (Array.isArray(cfg.sceneIds) ? cfg.sceneIds : (cfg.entrySceneId ? [cfg.entrySceneId] : []))
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const label = String(node.label || "").toLowerCase();
      const sceneId = String(node.sceneId || "").toLowerCase();
      const resource = String(node.resource || "").toLowerCase();
      return (labels.length && labels.indexOf(label) >= 0)
        || (sceneIds.length && sceneIds.indexOf(sceneId) >= 0)
        || (hints.length && hints.some((hint) => resource.includes(hint)));
    },

    _currentNodeAllowsKeyboardSfx(activity) {
      if (!this.active || !this.currentNodeId || !activity) return false;
      const cfg = this._activityConfig(activity);
      const node = this._nodeById(this.currentNodeId) || {};
      const labels = (Array.isArray(cfg.keyboardSfxLabels) ? cfg.keyboardSfxLabels : [])
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const hints = (Array.isArray(cfg.keyboardSfxResourceHints) ? cfg.keyboardSfxResourceHints : [])
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const sceneIds = (Array.isArray(cfg.keyboardSfxSceneIds) ? cfg.keyboardSfxSceneIds : [])
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      if (!labels.length && !hints.length && !sceneIds.length) return false;

      const label = String(node.label || "").toLowerCase();
      const sceneId = String(node.sceneId || "").toLowerCase();
      const resource = String(node.resource || "").toLowerCase();
      return (labels.length && labels.indexOf(label) >= 0)
        || (sceneIds.length && sceneIds.indexOf(sceneId) >= 0)
        || (hints.length && hints.some((hint) => resource.includes(hint)));
    },

    _computerUseSfxAllowed() {
      // Semantic rule: the keyboard ambience follows the active graph node.
      // The backend work signal may enter the activity domain, but only the
      // concrete computer-use node is allowed to produce key clicks. Speech
      // ducks it instead of stopping it; passive ASR listening is intentionally
      // not a hard gate, because the user can interrupt while she works.
      return !!(
        this.enabled
        && this._isComputerUseActivityActive()
        && !this._fadingOut
      );
    },

    _computerUseSfxDucked() {
      return !!(this.speaking && this._isComputerUseActivityActive());
    },

    _updateComputerUseSfx() {
      const allowed = this._computerUseSfxAllowed();
      const ducked = allowed && this._computerUseSfxDucked();
      const gate = {
        activity: this.activeActivity || "",
        node: this.currentNodeId || "",
        nodeLabel: (this._nodeById(this.currentNodeId) || {}).label || "",
        nodeSceneId: (this._nodeById(this.currentNodeId) || {}).sceneId || "",
        speaking: !!this.speaking,
        asrStatus: this.asrStatus || "",
        asrSuppressesWorkSfx: !!this.asrSuppressesWorkSfx,
        fadingOut: !!this._fadingOut,
        allowed,
        ducked,
      };
      const gateKey = JSON.stringify(gate);
      if (this._lastComputerUseGateKey !== gateKey) {
        this._lastComputerUseGateKey = gateKey;
        diag("keyboard_sfx.gate", gate, allowed ? "info" : "warning");
      }
      keyboardSfx.setSuppressed(!allowed);
      keyboardSfx.setEnabled(allowed);
      keyboardSfx.setDucked(ducked);
      if (allowed) keyboardSfx.ensureAudible();
    },

    _activityNode(activity) {
      const nodes = this.graph.nodes || [];
      const cfg = this._activityConfig(activity);
      const entryLabel = String(cfg.entryLabel || cfg.label || "").toLowerCase();
      const entrySceneId = String(cfg.entrySceneId || cfg.sceneId || "").toLowerCase();
      const entryHint = String(
        cfg.entryResourceHint
        || (Array.isArray(cfg.resourceHints) && cfg.resourceHints.length ? cfg.resourceHints[0] : "")
        || ""
      ).toLowerCase();
      const ids = this._activityNodeIds(activity);
      return (entryLabel ? nodes.find((n) => String(n.label || "").toLowerCase() === entryLabel) : null)
        || (entryHint ? nodes.find((n) => String(n.resource || "").toLowerCase().includes(entryHint)) : null)
        || (entrySceneId ? nodes.find((n) => String(n.sceneId || "").toLowerCase() === entrySceneId) : null)
        || nodes.find((n) => ids.has(n.id))
        || null;
    },

    _activityNodeIds(activity) {
      const cfg = this._activityConfig(activity);
      const nodes = this.graph.nodes || [];
      const labels = (Array.isArray(cfg.labels) ? cfg.labels : (cfg.entryLabel ? [cfg.entryLabel] : []))
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const hints = (Array.isArray(cfg.resourceHints) ? cfg.resourceHints : [])
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const sceneIds = (Array.isArray(cfg.sceneIds) ? cfg.sceneIds : (cfg.entrySceneId ? [cfg.entrySceneId] : []))
        .map((v) => String(v || "").toLowerCase())
        .filter(Boolean);
      const byLabel = labels.length
        ? nodes.filter((n) => labels.indexOf(String(n.label || "").toLowerCase()) >= 0)
        : [];
      const byHint = !byLabel.length && hints.length
        ? nodes.filter((n) => hints.some((hint) => String(n.resource || "").toLowerCase().includes(hint)))
        : [];
      const byScene = !byLabel.length && !byHint.length && sceneIds.length
        ? nodes.filter((n) => sceneIds.indexOf(String(n.sceneId || "").toLowerCase()) >= 0)
        : [];
      return new Set(byLabel.concat(byHint, byScene).map((n) => n.id));
    },

    _ensureActivityScene(activity) {
      if (!this.enabled) return;
      const allowed = this._activityNodeIds(activity);
      const current = this.currentNodeId && allowed.has(this.currentNodeId) ? this._nodeById(this.currentNodeId) : null;
      const node = current || this._activityNode(activity);
      if (!node) return;
      this.idleSeconds = 0;
      if (!this.active) {
        this.activate(node);
        return;
      }

      this._pendingNode = null;
      this._fadingOut = false;
      this._fadeCharacterIn = false;
      this._characterFadeTargets = null;
      this._fadeDir = 0;
      if (!this._savedVisibility) this._saveAndHideCharacter();
      const reveal = () => {
        if (!this._savedVisibility) this._saveAndHideCharacter();
        if (this.container) {
          this.container.visible = true;
          this.container.alpha = 1;
        }
        this._syncLayerOrder();
        this._updateComputerUseSfx();
        this._heartbeat(true);
      };
      if (this.currentNodeId === node.id && this.container && this.container.visible) {
        reveal();
        return;
      }
      const ok = this._showNode(node.id, reveal);
      if (!ok) this._abortActivation("activity scene resource unavailable");
    },

    _nodeById(id) {
      return (this.graph.nodes || []).find((n) => n.id === id) || null;
    },

    _showNode(id, onReady) {
      const node = this._nodeById(id);
      if (!node || !this.sprite) return false;
      const res = this.resources[id] || {};
      this.currentNodeId = id;
      this.currentResource = res;
      this._updateComputerUseSfx();
      this.nodeElapsed = 0;
      this.frameElapsed = 0;
      this.frameIndex = 0;
      if (res.type === "frames" && Array.isArray(res.frames) && res.frames.length > 0) {
        diag("scenario.show_node", { id, type: "frames", frames: res.frames.length, url: res.frames[0], framePlayback: this.enableFramePlayback });
        if (!this.enableFramePlayback) {
          this.currentResource = { type: "image", url: res.frames[0], frameSource: true, frameCount: res.frames.length };
          diag("scenario.frames_static", { id, frames: res.frames.length, url: res.frames[0] });
          return this._setTexture(res.frames[0], onReady);
        }
        return this._setTexture(res.frames[0], onReady);
      } else if (res.type === "image" && res.url) {
        diag("scenario.show_node", { id, type: "image", url: res.url });
        return this._setTexture(res.url, onReady);
      } else if (res.type === "video" && res.url) {
        console.warn("[ScenarioRuntime] video resource is not decoded to frames:", node.label || id);
      } else {
        console.warn("[ScenarioRuntime] missing resource:", node.label || id, res);
      }
      return false;
    },

    _tickFrames(dt) {
      const res = this.currentResource;
      const frames = (res && res.frames) || [];
      if (!frames.length) return;
      const fps = Math.max(1, Math.min(60, Number(res.fps) || 30));
      this.frameElapsed += dt;
      const frameDuration = 1 / fps;
      let looped = false;
      while (this.frameElapsed >= frameDuration) {
        this.frameElapsed -= frameDuration;
        this.frameIndex += 1;
        if (this.frameIndex >= frames.length) {
          this.frameIndex = 0;
          looped = true;
        }
      }
      this._setTexture(frames[this.frameIndex]);
      if (looped) this._goNext();
    },

    _goNext() {
      const next = this._chooseNext(this.currentNodeId);
      if (!next) return;
      if (next === this.currentNodeId) {
        this.nodeElapsed = 0;
        return;
      }
      // Load next node while keeping old texture visible — no fade-out gap.
      // _setTexture only hides the sprite when there is no existing texture,
      // so the previous frame stays on-screen until the new one is ready.
      this._showNode(next);
    },

    _chooseNext(fromId) {
      if (this.activeActivity) return this._chooseActivityNext(this.activeActivity, fromId);
      const edges = (this.graph.edges || [])
        .filter((e) => e.from === fromId && this._nodeById(e.to))
        .map((e) => ({ to: e.to, prob: Math.max(0, Number(e.prob) || 0) }))
        .filter((e) => e.prob > 0);
      if (!edges.length) return fromId;
      const total = edges.reduce((sum, e) => sum + e.prob, 0);
      let r = Math.random() * total;
      for (const edge of edges) {
        r -= edge.prob;
        if (r <= 0) return edge.to;
      }
      return edges[edges.length - 1].to;
    },

    _chooseActivityNext(activity, fromId) {
      const cfg = this._activityConfig(activity);
      if (cfg.stayWithinActivity === false) return this._chooseWeightedNext(fromId);
      const ids = this._activityNodeIds(activity);
      const edges = (this.graph.edges || [])
        .filter((e) => e.from === fromId && ids.has(e.to) && this._nodeById(e.to))
        .map((e) => ({ to: e.to, prob: Math.max(0, Number(e.prob) || 0) }))
        .filter((e) => e.prob > 0);
      if (!edges.length) return ids.has(fromId) ? fromId : ((this._activityNode(activity) || {}).id || fromId);
      const total = edges.reduce((sum, e) => sum + e.prob, 0);
      let r = Math.random() * total;
      for (const edge of edges) {
        r -= edge.prob;
        if (r <= 0) return edge.to;
      }
      return edges[edges.length - 1].to;
    },

    _chooseWeightedNext(fromId) {
      const edges = (this.graph.edges || [])
        .filter((e) => e.from === fromId && this._nodeById(e.to))
        .map((e) => ({ to: e.to, prob: Math.max(0, Number(e.prob) || 0) }))
        .filter((e) => e.prob > 0);
      if (!edges.length) return fromId;
      const total = edges.reduce((sum, e) => sum + e.prob, 0);
      let r = Math.random() * total;
      for (const edge of edges) {
        r -= edge.prob;
        if (r <= 0) return edge.to;
      }
      return edges[edges.length - 1].to;
    },

    _setTexture(url, onReady) {
      if (!this.sprite || !url) return false;
      if (!this._textureCache) this._textureCache = new Map();
      const cached = this._getCachedTexture(url);
      if (cached) {
        this._textureLoading = false;
        const changed = this.sprite.texture !== cached;
        if (changed) this.sprite.texture = cached;
        this.sprite.visible = true;
        this._placeSprite(desktopScene._crtBounds);
        if (typeof onReady === "function") onReady();
        if (changed || typeof onReady === "function") {
          diag("scenario.texture_cached", { url, cacheSize: this._textureCache ? this._textureCache.size : 0 });
        }
        return true;
      }

      diag("scenario.texture_load_start", { url });
      // Abort any in-flight fetch from the previous _setTexture call so we don't
      // pile up dozens of concurrent HTTP requests against the asset server.
      if (this._pendingFetchController) {
        try { this._pendingFetchController.abort(); } catch (_e) {}
        this._pendingFetchController = null;
      }
      const token = ++this._textureLoadToken;
      this._textureLoading = true;
      this._textureLoadStartedAt = Date.now();
      this._textureLoadUrl = url;
      let settled = false;
      const failTexture = (err) => {
        if (token !== this._textureLoadToken) return;
        if (settled) return;
        settled = true;
        this._textureLoading = false;
        console.warn("[ScenarioRuntime] texture load failed:", url, err);
        diag("scenario.texture_error", { url, error: String(err && (err.message || err)) }, "error");
        if (typeof onReady === "function") this._abortActivation("texture load failed");
      };
      const applyTexture = (base, objectUrl, sourceImage) => {
        if (token !== this._textureLoadToken) return;
        if (settled) return;
        try {
          const tex = this._textureForBase(base);
          this._rememberTexture(url, tex, tex === base ? null : base, objectUrl || "", sourceImage || null);
          if (this.sprite.texture !== tex) this.sprite.texture = tex;
          this.sprite.visible = true;
          this._placeSprite(desktopScene._crtBounds);
          settled = true;
          this._textureLoading = false;
          diag("scenario.texture_ready", {
            url,
            width: tex.width || (tex.baseTexture && tex.baseTexture.width) || 0,
            height: tex.height || (tex.baseTexture && tex.baseTexture.height) || 0,
            cacheSize: this._textureCache ? this._textureCache.size : 0,
          });
          if (typeof onReady === "function") onReady();
        } catch (err) {
          failTexture(err);
        }
      };
      const loadImageSource = (src, sourceKind, objectUrl) => {
        const img = new Image();
        if (sourceKind !== "blob") {
          img.crossOrigin = "anonymous";
        }
        img.onload = () => {
          if (token !== this._textureLoadToken) {
            // Stale load — revoke the blob URL immediately to prevent memory leak.
            if (objectUrl) {
              try { URL.revokeObjectURL(objectUrl); } catch (_e) {}
            }
            return;
          }
          diag("scenario.image_ready", {
            url,
            sourceKind,
            width: img.naturalWidth || img.width,
            height: img.naturalHeight || img.height,
          });
          try {
            applyTexture(PIXI.Texture.from(img), objectUrl, img);
          } catch (err) {
            if (objectUrl) {
              try { URL.revokeObjectURL(objectUrl); } catch (revokeErr) {}
            }
            failTexture(err);
          }
        };
        img.onerror = (err) => {
          if (objectUrl) {
            try { URL.revokeObjectURL(objectUrl); } catch (revokeErr) {}
          }
          failTexture(err);
        };
        img.src = src;
      };

      const startDirectImageFallback = (reason) => {
        diag("scenario.direct_image_fallback", { url, reason: String(reason || "") }, "warning");
        loadImageSource(url, "direct", "");
      };

      const startDirectImageLoad = () => {
        diag("scenario.direct_image_load_start", { url });
        loadImageSource(url, "direct", "");
      };

      const startFetchLoad = () => {
        if (token !== this._textureLoadToken) return;
        if (typeof fetch !== "function" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") {
          startDirectImageFallback("fetch/blob unavailable");
          return;
        }
        let controller = null;
        let timeoutId = null;
        try {
          if (typeof AbortController !== "undefined") {
            controller = new AbortController();
            this._pendingFetchController = controller;
          }
          if (controller) {
            timeoutId = setTimeout(() => {
              try { controller.abort(); } catch (err) {}
            }, this._textureLoadTimeoutMs);
          }
          diag("scenario.fetch_start", { url });
          fetch(url, {
            cache: "no-store",
            signal: controller ? controller.signal : undefined,
          }).then((res) => {
            if (timeoutId) clearTimeout(timeoutId);
            if (this._pendingFetchController === controller) this._pendingFetchController = null;
            if (!res.ok) throw new Error("HTTP " + res.status);
            return res.blob().then((blob) => ({ res, blob }));
          }).then(({ res, blob }) => {
            if (token !== this._textureLoadToken) return;
            diag("scenario.fetch_ready", {
              url,
              status: res.status,
              type: blob.type || res.headers.get("content-type") || "",
              bytes: blob.size || 0,
            });
            if (!blob.size) throw new Error("empty image blob");
            const objectUrl = URL.createObjectURL(blob);
            loadImageSource(objectUrl, "blob", objectUrl);
          }).catch((err) => {
            if (timeoutId) clearTimeout(timeoutId);
            if (this._pendingFetchController === controller) this._pendingFetchController = null;
            if (token !== this._textureLoadToken) return;
            diag("scenario.fetch_error", { url, error: String(err && (err.message || err)) }, "warning");
            startDirectImageFallback(err);
          });
        } catch (err) {
          if (timeoutId) clearTimeout(timeoutId);
          if (this._pendingFetchController === controller) this._pendingFetchController = null;
          startDirectImageFallback(err);
        }
      };
      const startPixiUrlLoad = () => {
        if (token !== this._textureLoadToken) return;
        try {
          diag("scenario.pixi_url_load_start", { url });
          const base = PIXI.Texture.from(url);
          const ready = () => {
            if (token !== this._textureLoadToken) return;
            applyTexture(base, "");
          };
          const fail = (err) => {
            if (token !== this._textureLoadToken) return;
            failTexture(err || new Error("PIXI url texture load failed"));
          };
          if (base && base.baseTexture && base.baseTexture.valid) {
            ready();
          } else if (base && base.baseTexture) {
            base.baseTexture.once("loaded", ready);
            base.baseTexture.once("error", fail);
          } else {
            fail(new Error("PIXI.Texture.from returned no baseTexture"));
          }
        } catch (err) {
          failTexture(err);
        }
      };
      if (!this.sprite.texture || this.sprite.texture === PIXI.Texture.EMPTY) {
        this.sprite.visible = false;
      }
      startFetchLoad();
      return true;
    },

    _getCachedTexture(url) {
      if (!this._textureCache) return null;
      const entry = this._textureCache.get(url);
      if (!entry) return null;
      this._textureCache.delete(url);
      this._textureCache.set(url, entry);
      return entry.texture || entry;
    },

    _rememberTexture(url, texture, sourceTexture, objectUrl, sourceImage) {
      if (!this._textureCache) this._textureCache = new Map();
      this._textureCache.set(url, {
        texture,
        sourceTexture,
        objectUrl: objectUrl || "",
        sourceImage: sourceImage || null,
      });
      this._trimTextureCache();
    },

    _trimTextureCache() {
      if (!this._textureCache || this._textureCache.size <= this._maxTextureCache) return;
      let guard = 0;
      while (this._textureCache.size > this._maxTextureCache && guard < this._maxTextureCache + 8) {
        guard += 1;
        const first = this._textureCache.entries().next().value;
        if (!first) return;
        const [url, entry] = first;
        const tex = entry.texture || entry;
        if (this.sprite && this.sprite.texture === tex) {
          this._textureCache.delete(url);
          this._textureCache.set(url, entry);
          continue;
        }
        this._textureCache.delete(url);
        diag("scenario.texture_evict", {
          url,
          cacheSize: this._textureCache.size,
          maxTextureCache: this._maxTextureCache,
        });
        this._destroyCachedTexture(url, entry);
      }
    },

    _destroyCachedTexture(url, entry) {
      const tex = entry && (entry.texture || entry);
      const source = entry && entry.sourceTexture;
      const objectUrl = entry && entry.objectUrl;
      try {
        if (tex && typeof tex.destroy === "function") {
          tex.destroy(!source);
        }
        if (PIXI.Texture && typeof PIXI.Texture.removeFromCache === "function") {
          const removed = PIXI.Texture.removeFromCache(url);
          if (removed && removed !== tex && removed !== source && typeof removed.destroy === "function") {
            removed.destroy(true);
          }
        }
        if (source && source !== tex && typeof source.destroy === "function") {
          source.destroy(true);
        }
        if (objectUrl) {
          try { URL.revokeObjectURL(objectUrl); } catch (revokeErr) {}
        }
      } catch (err) {
        console.warn("[ScenarioRuntime] texture cache eviction failed:", url, err);
      }
    },

    _activeSourceCrop(base, cropNormOverride) {
      if (!base || !base.baseTexture || !base.baseTexture.valid) return null;
      const bw = Math.max(1, base.baseTexture.width || 1);
      const bh = Math.max(1, base.baseTexture.height || 1);
      const cropNorm = cropNormOverride === undefined ? this.sourceCropNorm : cropNormOverride;
      if (cropNorm && cropNorm.length >= 4) {
        return [
          Number(cropNorm[0]) * bw,
          Number(cropNorm[1]) * bh,
          Number(cropNorm[2]) * bw,
          Number(cropNorm[3]) * bh,
        ];
      }
      return this.sourceCrop && this.sourceCrop.length >= 4 ? this.sourceCrop : null;
    },

    _textureForBase(base, cropNormOverride) {
      const crop = this._activeSourceCrop(base, cropNormOverride);
      if (!crop || crop.length < 4 || !base.baseTexture.valid) return base;
      const sx = Math.max(0, Number(crop[0]) || 0);
      const sy = Math.max(0, Number(crop[1]) || 0);
      const sw = Math.max(1, Number(crop[2]) || (base.baseTexture.width - sx));
      const sh = Math.max(1, Number(crop[3]) || (base.baseTexture.height - sy));
      const frame = new PIXI.Rectangle(
        Math.min(sx, Math.max(0, base.baseTexture.width - 1)),
        Math.min(sy, Math.max(0, base.baseTexture.height - 1)),
        Math.min(sw, Math.max(1, base.baseTexture.width - sx)),
        Math.min(sh, Math.max(1, base.baseTexture.height - sy))
      );
      return new PIXI.Texture(base.baseTexture, frame);
    },

    _setBackplate(url) {
      if (!this.backplateSprite || !url || !window.PIXI) return;
      const base = PIXI.Texture.from(url);
      const apply = () => {
        if (!this.backplateSprite || !base || !base.baseTexture || !base.baseTexture.valid) return;
        this.backplateSprite.texture = this._textureForBase(base, this.backplateCropNorm);
        this.backplateSprite.visible = true;
        this.backplateSprite.alpha = 1;
        this._placeSpriteIn(this.backplateSprite, desktopScene._crtBounds);
        diag("scenario.backplate_ready", {
          url,
          width: this.backplateSprite.texture ? this.backplateSprite.texture.width : 0,
          height: this.backplateSprite.texture ? this.backplateSprite.texture.height : 0,
        });
      };
      const fail = (err) => {
        console.warn("[ScenarioRuntime] backplate load failed:", url, err);
        diag("scenario.backplate_error", { url, error: String(err && (err.message || err)) }, "warning");
      };
      try {
        if (base && base.baseTexture && base.baseTexture.valid) {
          apply();
        } else if (base && base.baseTexture) {
          base.baseTexture.once("loaded", apply);
          base.baseTexture.once("error", fail);
        }
      } catch (err) {
        fail(err);
      }
    },

    _placeSprite(bounds) {
      if (!this.sprite || !bounds || !this.sprite.texture) return;
      this._placeSpriteIn(this.sprite, bounds);
    },

    _placeSpriteIn(sprite, bounds) {
      if (!sprite || !bounds || !sprite.texture) return;
      if (this.placementMode === "crt_screen") {
        this._stretchSprite(sprite, bounds);
      } else {
        this._fitSprite(sprite, bounds);
      }
    },

    _stretchSprite(sprite, bounds) {
      const tex = sprite.texture;
      const tw = Math.max(1, tex.width || tex.orig?.width || 1);
      const th = Math.max(1, tex.height || tex.orig?.height || 1);
      sprite.scale.set(bounds.width / tw, bounds.height / th);
      sprite.x = bounds.x;
      sprite.y = bounds.y;
    },

    _fitSprite(sprite, bounds) {
      if (!sprite || !bounds || !sprite.texture) return;
      const tex = sprite.texture;
      const tw = Math.max(1, tex.width || tex.orig?.width || 1);
      const th = Math.max(1, tex.height || tex.orig?.height || 1);
      const scale = Math.max(bounds.width / tw, bounds.height / th);
      sprite.scale.set(scale);
      sprite.x = bounds.x + (bounds.width - tw * scale) / 2;
      sprite.y = bounds.y + (bounds.height - th * scale) / 2;
    },

    _saveAndHideCharacter() {
      const app = window.renderApp;
      const sprite = app && app._sprite && app._sprite.container;
      const live2d = app && app._live2d && app._live2d.container;
      const fadeTargets = this._characterFadeTargets || {};
      this._savedVisibility = {
        spriteVisible: fadeTargets.spriteVisible !== undefined ? fadeTargets.spriteVisible : (sprite ? sprite.visible : null),
        spriteAlpha: fadeTargets.spriteAlpha !== undefined ? fadeTargets.spriteAlpha : (sprite ? sprite.alpha : null),
        live2dVisible: fadeTargets.live2dVisible !== undefined ? fadeTargets.live2dVisible : (live2d ? live2d.visible : null),
        live2dAlpha: fadeTargets.live2dAlpha !== undefined ? fadeTargets.live2dAlpha : (live2d ? live2d.alpha : null),
      };
      this._characterFadeTargets = null;
      if (sprite) {
        sprite.visible = false;
        sprite.alpha = 0;
      }
      if (live2d) {
        live2d.visible = false;
        live2d.alpha = 0;
      }
    },

    _restoreCharacter(alphaProgress) {
      const app = window.renderApp;
      const sprite = app && app._sprite && app._sprite.container;
      const live2d = app && app._live2d && app._live2d.container;
      const saved = this._savedVisibility || {};
      const progress = Number.isFinite(alphaProgress) ? Math.max(0, Math.min(1, alphaProgress)) : null;
      const targets = {
        spriteAlpha: saved.spriteAlpha !== null && saved.spriteAlpha !== undefined ? saved.spriteAlpha : 1,
        live2dAlpha: saved.live2dAlpha !== null && saved.live2dAlpha !== undefined ? saved.live2dAlpha : 1,
        spriteVisible: saved.spriteVisible !== null && saved.spriteVisible !== undefined ? saved.spriteVisible : true,
        live2dVisible: saved.live2dVisible !== null && saved.live2dVisible !== undefined ? saved.live2dVisible : false,
      };
      if (sprite) {
        sprite.visible = targets.spriteVisible;
        sprite.alpha = progress === null ? targets.spriteAlpha : targets.spriteAlpha * progress;
      }
      if (live2d) {
        live2d.visible = targets.live2dVisible;
        live2d.alpha = progress === null ? targets.live2dAlpha : targets.live2dAlpha * progress;
      }
      this._characterFadeTargets = progress === null ? null : targets;
      this._savedVisibility = null;
    },

    _setCharacterFadeProgress(progress) {
      const targets = this._characterFadeTargets;
      if (!targets) return;
      const p = Math.max(0, Math.min(1, Number(progress) || 0));
      const app = window.renderApp;
      const sprite = app && app._sprite && app._sprite.container;
      const live2d = app && app._live2d && app._live2d.container;
      if (sprite) {
        sprite.visible = targets.spriteVisible;
        sprite.alpha = targets.spriteAlpha * p;
      }
      if (live2d) {
        live2d.visible = targets.live2dVisible;
        live2d.alpha = targets.live2dAlpha * p;
      }
      if (p >= 1) this._characterFadeTargets = null;
    },
  };

  const desktopScene = {
    app: null,
    cfg: {},
    bg: null,
    mask: null,
    staticOverlay: null,
    ambientLayer: null,
    glowLayer: null,
    scanlineLayer: null,
    canvasSurface: null,
    ambientLowSprite: null,
    ambientSprite: null,
    backgroundUrl: "",
    ambientUrl: "",
    ambientLowUrl: "",
    ambientDeltaUrl: "",
    _resizeBound: null,
    _tickerBound: null,
    _points: null,
    _crtBounds: null,
    _defaultSubtitleEnabled: false,
    _time: 0,
    _speakingGate: 0,
    _mouthPulse: 0,
    _ambientNoise: 0,
    _ambientNoiseTarget: 0,
    _ambientNoiseTimer: 0,
    _ambientBurst: 0,
    _ambientBurstAge: 0,
    _ambientBurstDuration: 0,
    _ambientBurstDelay: 2.2,
    _scanlineFlash: 0,
    _lastSubtitleText: "",
    _subtitleClearTimer: null,
    _subtitleClearDelayMs: 15000,
    _mode: "sprite",
    _externalCanvasHost: false,

    init(payload) {
      this.app = callRender("getPixiApp", []);
      if (!this.app || !window.PIXI) {
        console.error("[WallpaperScene] Pixi app unavailable; desktop scene not initialized");
        return;
      }

      if (!this.mask) {
        this.mask = new PIXI.Graphics();
        this.mask.renderable = false;
        this.staticOverlay = new PIXI.Graphics();
        this.ambientLayer = new PIXI.Container();
        this.glowLayer = new PIXI.Graphics();
        this.scanlineLayer = new PIXI.Graphics();
        if (PIXI.BLEND_MODES && PIXI.BLEND_MODES.ADD) {
          this.glowLayer.blendMode = PIXI.BLEND_MODES.ADD;
        }
        this.app.stage.addChild(this.mask);
        this.app.stage.addChild(this.staticOverlay);
        this.app.stage.addChild(this.ambientLayer);
        this.app.stage.addChild(this.glowLayer);
        this.app.stage.addChild(this.scanlineLayer);
        this.scanlineLayer.mask = this.mask;
        callRender("setSpriteViewportMask", [this.mask]);
        this._resizeBound = () => this.layout();
        this._tickerBound = (delta) => this.tick(delta);
        window.addEventListener("resize", this._resizeBound);
        this.app.ticker.add(this._tickerBound);
      }

      this.cfg = (payload && payload.crtConfig) || {};
      this._externalCanvasHost = new URLSearchParams(window.location.search || "").get("sliceHost") === "electron";
      this.setDefaultSubtitleEnabled(!!(payload && payload.defaultSubtitleEnabled));
      this._ambientBurstDelay = this._ambientBurstInterval();
      this.setAmbientLow((payload && payload.ambientLowUrl) || "");
      this.setAmbientDelta((payload && payload.ambientDeltaUrl) || "");
      this.setBackground((payload && payload.backgroundUrl) || "");
      wallpaperSubtitle.init(this.app, payload || {});
      if (!this._externalCanvasHost && !this.canvasSurface && typeof window.createCrtCanvasSurface === "function") {
        this.canvasSurface = window.createCrtCanvasSurface();
      }
      scenarioRuntime.init(this.app, (payload && payload.scenario) || {});
      if (!this._defaultSubtitleEnabled) characterRuntime.setSubtitle("");
      this.layout();
      console.log("[WallpaperScene] initialized with separated scene/character controllers");
      diag("desktop.init", {
        hasBackground: !!(payload && payload.backgroundUrl),
        hasAmbientLow: !!(payload && payload.ambientLowUrl),
        hasAmbientDelta: !!(payload && payload.ambientDeltaUrl),
        scenarioEnabled: !!(payload && payload.scenario && payload.scenario.enabled),
        stageChildren: this.app && this.app.stage ? this.app.stage.children.length : 0,
      });
    },

    setBackground(url) {
      if (!this.app) return;
      if (!url) {
        console.warn("[WallpaperScene] empty background url; keeping previous background");
        diag("background.empty_url_keep_existing", { hadBackground: !!this.bg }, "warning");
        return;
      }
      if (this.bg && this.backgroundUrl === url) {
        diag("background.same_url_keep_existing", { url });
        return;
      }

      const previousBg = this.bg;
      const previousUrl = this.backgroundUrl;
      const tex = PIXI.Texture.from(url);
      diag("background.load_start", { url, previousUrl: previousUrl || "" });
      const installBackground = () => {
        const nextBg = new PIXI.Sprite(tex);
        this.app.stage.addChildAt(nextBg, 0);
        this.bg = nextBg;
        this.backgroundUrl = url;
        this.layout();
        if (previousBg && previousBg !== nextBg) {
          try {
            this.app.stage.removeChild(previousBg);
            previousBg.destroy({ texture: true, baseTexture: true });
          } catch (err) {
            console.warn("[WallpaperScene] previous background cleanup failed:", err);
          }
        }
        diag("background.ready", {
          url,
          width: tex.baseTexture ? tex.baseTexture.width : 0,
          height: tex.baseTexture ? tex.baseTexture.height : 0,
          stageChildren: this.app && this.app.stage ? this.app.stage.children.length : 0,
        });
      };
      const failBackground = (err) => {
        console.warn("[WallpaperScene] background load failed; keeping previous background:", url, err);
        diag("background.error_keep_existing", {
          url,
          previousUrl: previousUrl || "",
          error: String(err && (err.message || err)),
        }, "error");
      };
      if (!tex.baseTexture.valid) {
        tex.baseTexture.once("loaded", installBackground);
        tex.baseTexture.once("error", failBackground);
      } else {
        installBackground();
      }
      if (!this.ambientDeltaUrl) {
        this._buildAmbientFromBackground(url);
      }
    },

    setAmbientDelta(url) {
      this.ambientDeltaUrl = url || "";
      if (!this.ambientDeltaUrl) {
        this._buildAmbientFromBackground(this.backgroundUrl);
        return;
      }
      this._loadAmbientSprite(this.ambientDeltaUrl, "delta");
    },

    setAmbientLow(url) {
      this.ambientLowUrl = url || "";
      if (!this.ambientLowUrl) return;
      this._loadAmbientLowSprite(this.ambientLowUrl);
    },

    _replaceAmbientLowSprite(texture) {
      if (this.ambientLowSprite) {
        this.ambientLayer.removeChild(this.ambientLowSprite);
        this.ambientLowSprite.destroy({ texture: true, baseTexture: true });
      }
      this.ambientLowSprite = new PIXI.Sprite(texture);
      this.ambientLowSprite.width = this.app.screen.width;
      this.ambientLowSprite.height = this.app.screen.height;
      this.ambientLowSprite.alpha = this._ambientLowIdleAlpha();
      this.ambientLayer.addChildAt(this.ambientLowSprite, 0);
      console.info("[WallpaperScene] ambient low layer ready:", {
        alpha: this.ambientLowSprite.alpha,
      });
      this._drawGlow();
    },

    _loadAmbientLowSprite(url) {
      if (!this.app || !url) return;
      const texture = PIXI.Texture.from(url);
      const applyTexture = () => this._replaceAmbientLowSprite(texture);
      if (texture.baseTexture.valid) {
        applyTexture();
      } else {
        texture.baseTexture.once("loaded", applyTexture);
        texture.baseTexture.once("error", (err) => {
          console.warn("[WallpaperScene] ambient low image failed:", url, err);
        });
      }
    },

    _replaceAmbientSprite(texture, sourceKind) {
      if (this.ambientSprite) {
        this.ambientLayer.removeChild(this.ambientSprite);
        this.ambientSprite.destroy({ texture: true, baseTexture: true });
      }
      this.ambientSprite = new PIXI.Sprite(texture);
      this.ambientSprite.width = this.app.screen.width;
      this.ambientSprite.height = this.app.screen.height;
      this.ambientSprite.alpha = this._ambientBaseAlpha();
      if (sourceKind === "generated-mask" && PIXI.BLEND_MODES && PIXI.BLEND_MODES.ADD) {
        this.ambientSprite.blendMode = PIXI.BLEND_MODES.ADD;
      }
      this.ambientLayer.addChild(this.ambientSprite);
      console.info("[WallpaperScene] ambient layer ready:", {
        source: sourceKind,
        alpha: this.ambientSprite.alpha,
      });
      this._drawGlow();
    },

    _loadAmbientSprite(url, sourceKind) {
      if (!this.app || !url) return;
      const texture = PIXI.Texture.from(url);
      const applyTexture = () => this._replaceAmbientSprite(texture, sourceKind);
      if (texture.baseTexture.valid) {
        applyTexture();
      } else {
        texture.baseTexture.once("loaded", applyTexture);
        texture.baseTexture.once("error", (err) => {
          console.warn("[WallpaperScene] ambient delta image failed:", url, err);
        });
      }
    },

    _cfgNumber(key, fallback) {
      const value = Number(this.cfg[key]);
      return Number.isFinite(value) ? value : fallback;
    },

    _ambientBaseAlpha() {
      return Math.max(0, Math.min(1.5, this._cfgNumber("ambient_alpha_idle", 0.0)));
    },

    _ambientPulseAlpha() {
      return Math.max(0, Math.min(1.5, this._cfgNumber("ambient_alpha_pulse", 0.95)));
    },

    _ambientLowIdleAlpha() {
      return Math.max(0, Math.min(1.5, this._cfgNumber("ambient_low_alpha_idle", 0.78)));
    },

    _ambientLowMinAlpha() {
      return Math.max(0, Math.min(1.5, this._cfgNumber("ambient_low_alpha_min", 0.12)));
    },

    _ambientBurstInterval() {
      const minSeconds = Math.max(0, this._cfgNumber("ambient_burst_interval_min", 12));
      const maxSeconds = Math.max(minSeconds, this._cfgNumber("ambient_burst_interval_max", 20));
      return minSeconds + Math.random() * (maxSeconds - minSeconds);
    },

    _ambientBurstEnabled() {
      if (urlFlagEnabled("noAmbientBurst") || urlFlagDisabled("ambientBurst")) return false;
      if (this.cfg && Object.prototype.hasOwnProperty.call(this.cfg, "ambient_burst_enabled")) {
        const value = this.cfg.ambient_burst_enabled;
        if (typeof value === "boolean") return value;
        const normalized = String(value).toLowerCase();
        return normalized === "1" || normalized === "true" || normalized === "yes" || normalized === "on";
      }
      return true;
    },

    setDefaultSubtitleEnabled(enabled) {
      this._defaultSubtitleEnabled = !!enabled;
      window.__DISABLE_RENDERER_SUBTITLE__ = !this._defaultSubtitleEnabled;
      if (!this._defaultSubtitleEnabled) {
        characterRuntime.setSubtitle("");
      } else if (this._lastSubtitleText) {
        characterRuntime.setSubtitle(this._lastSubtitleText);
      }
      diag("subtitle.default_enabled", { enabled: this._defaultSubtitleEnabled });
    },

    _clearSubtitleTimer() {
      if (this._subtitleClearTimer) {
        window.clearTimeout(this._subtitleClearTimer);
        this._subtitleClearTimer = null;
      }
    },

    _scheduleSubtitleClear(expectedText) {
      this._clearSubtitleTimer();
      if (!expectedText) return;
      this._subtitleClearTimer = window.setTimeout(() => {
        this._subtitleClearTimer = null;
        if (this._lastSubtitleText !== expectedText) return;
        this._lastSubtitleText = "";
        wallpaperSubtitle.setText("");
        characterRuntime.setSubtitle("");
        diag("subtitle.auto_cleared", { delayMs: this._subtitleClearDelayMs });
      }, this._subtitleClearDelayMs);
    },

    _crtPoints() {
      const app = this.app;
      const imgSize = this.cfg.img_size || [app.screen.width, app.screen.height];
      const rawPoints = this.cfg.crt_polygon || this.cfg.crt_corners || [
        [0, 0],
        [app.screen.width, 0],
        [app.screen.width, app.screen.height],
        [0, app.screen.height],
      ];
      const sx = app.screen.width / Math.max(1, Number(imgSize[0]) || 1);
      const sy = app.screen.height / Math.max(1, Number(imgSize[1]) || 1);
      return rawPoints.map((p) => ({ x: Number(p[0]) * sx, y: Number(p[1]) * sy }));
    },

    _computeBounds(points) {
      const xs = points.map((p) => p.x);
      const ys = points.map((p) => p.y);
      const x = Math.min(...xs);
      const y = Math.min(...ys);
      const right = Math.max(...xs);
      const bottom = Math.max(...ys);
      return { x, y, width: right - x, height: bottom - y, right, bottom };
    },

    layout() {
      if (!this.app || !this.mask || !this.staticOverlay) return;
      if (this.bg) {
        this.bg.x = 0;
        this.bg.y = 0;
        this.bg.width = this.app.screen.width;
        this.bg.height = this.app.screen.height;
      }
      if (this.ambientLowSprite) {
        this.ambientLowSprite.width = this.app.screen.width;
        this.ambientLowSprite.height = this.app.screen.height;
      }
      if (this.ambientSprite) {
        this.ambientSprite.width = this.app.screen.width;
        this.ambientSprite.height = this.app.screen.height;
      }

      const points = this._crtPoints();
      const bounds = this._computeBounds(points);
      this._points = points;
      this._crtBounds = bounds;

      this.mask.clear();
      this.mask.beginFill(0xffffff, 1);
      this.mask.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i += 1) this.mask.lineTo(points[i].x, points[i].y);
      this.mask.lineTo(points[0].x, points[0].y);
      this.mask.endFill();

      this.staticOverlay.clear();
      this._drawPolygon(this.staticOverlay, points, 0x07121c, 0.10);

      callRender("setSpriteViewportBounds", [bounds]);
      scenarioRuntime.layout(bounds, this.mask);
      wallpaperSubtitle.layout(bounds);
      if (this.canvasSurface && typeof this.canvasSurface.layout === "function") {
        this.canvasSurface.layout(bounds);
      }
      this._drawGlow();
      this._drawScanlineNoise();
    },

    setCanvas(payload) {
      if (this._externalCanvasHost) return;
      if (!this.canvasSurface && typeof window.createCrtCanvasSurface === "function") {
        this.canvasSurface = window.createCrtCanvasSurface();
        if (this._crtBounds) this.canvasSurface.layout(this._crtBounds);
      }
      if (this.canvasSurface && typeof this.canvasSurface.setPayload === "function") {
        this.canvasSurface.setPayload(payload || {});
      }
    },

    setCanvasPresentation(profile) {
      if (this._externalCanvasHost) return;
      if (!this.canvasSurface && typeof window.createCrtCanvasSurface === "function") {
        this.canvasSurface = window.createCrtCanvasSurface();
        if (this._crtBounds) this.canvasSurface.layout(this._crtBounds);
      }
      if (this.canvasSurface && typeof this.canvasSurface.setPresentation === "function") {
        this.canvasSurface.setPresentation(profile || {});
      }
    },

    setAttention(payload) {
      if (this._externalCanvasHost) return;
      if (!this.canvasSurface && typeof window.createCrtCanvasSurface === "function") {
        this.canvasSurface = window.createCrtCanvasSurface();
        if (this._crtBounds) this.canvasSurface.layout(this._crtBounds);
      }
      if (this.canvasSurface && typeof this.canvasSurface.setAttention === "function") {
        this.canvasSurface.setAttention(payload || {});
      }
    },

    toggleCanvas() {
      if (this._externalCanvasHost) return;
      if (!this.canvasSurface && typeof window.createCrtCanvasSurface === "function") {
        this.canvasSurface = window.createCrtCanvasSurface();
        if (this._crtBounds) this.canvasSurface.layout(this._crtBounds);
      }
      if (this.canvasSurface && typeof this.canvasSurface.toggle === "function") {
        this.canvasSurface.toggle();
      }
    },

    setMode(mode) {
      const value = String(mode || "sprite").toLowerCase();
      const isWorkMode = value === "work" || value === "working" || value === "work_surface" || value === "provider_work";
      this._mode = value;
      scenarioRuntime.setActivity(isWorkMode ? "work" : null);
      if (isWorkMode) return null;
      if (value === "hybrid") return "both";
      if (value === "sprite" || value === "live2d" || value === "both") return value;
      return "sprite";
    },

    setActivity(activity) {
      const value = String(activity || "").toLowerCase();
      this._mode = value || "sprite";
      scenarioRuntime.setActivity(value || null);
    },

    setWorkMode(enabled) {
      this._mode = enabled ? "work" : "sprite";
      scenarioRuntime.setWorkMode(!!enabled);
    },

    setSpeaking(speaking) {
      this._speakingGate = speaking ? 1 : 0;
      scenarioRuntime.setSpeaking(!!speaking);
      wallpaperSubtitle.updateVisibility();
    },

    setAsrStatus(payload) {
      scenarioRuntime.setAsrStatus(payload || {});
    },

    setMouth(value) {
      const v = Math.max(0, Math.min(1, Number(value) || 0));
      if (v > this._mouthPulse) this._mouthPulse = v;
      if (v > 0.03) scenarioRuntime.noteActivity();
    },

    tick(delta) {
      if (!this.app || !this._crtBounds || !this._points) return;
      const dt = Math.min(0.05, Math.max(0, (this.app.ticker.deltaMS || (delta * 16.6667)) / 1000));
      this._time += dt;
      const decay = this._speakingGate > 0 ? 2.8 : 5.5;
      this._mouthPulse = Math.max(0, this._mouthPulse - dt * decay);
      this._ambientNoiseTimer -= dt;
      if (this._ambientNoiseTimer <= 0) {
        this._ambientNoiseTimer = 0.28 + Math.random() * 0.42;
        this._ambientNoiseTarget = (Math.random() - 0.5) * 0.018;
      }
      this._ambientNoise += (this._ambientNoiseTarget - this._ambientNoise) * Math.min(1, dt * 4.2);
      if (this._ambientBurstEnabled() && this._ambientBurstDuration > 0) {
        this._ambientBurstAge += dt;
        const t = Math.min(1, this._ambientBurstAge / this._ambientBurstDuration);
        this._ambientBurst = Math.sin(Math.PI * t);
        if (t >= 1) {
          this._ambientBurst = 0;
          this._ambientBurstDuration = 0;
          this._ambientBurstDelay = this._ambientBurstInterval();
        }
      } else if (this._ambientBurstEnabled()) {
        this._ambientBurstDelay -= dt;
        if (this._ambientBurstDelay <= 0) {
          this._ambientBurstAge = 0;
          this._ambientBurstDuration = 0.75 + Math.random() * 0.60;
        }
      } else {
        this._ambientBurst = 0;
        this._ambientBurstDuration = 0;
        this._ambientBurstAge = 0;
      }
      this._scanlineFlash = Math.max(0, this._scanlineFlash - dt * 3.8);
      if (Math.random() < dt * 0.075) {
        this._scanlineFlash = 0.75 + Math.random() * 0.25;
      }
      scenarioRuntime.tick(dt);
      wallpaperSubtitle.updateVisibility();
      this._drawGlow();
      this._drawScanlineNoise();
    },

    _drawGlow() {
      if (!this.glowLayer || !this._crtBounds || !this._points) return;
      const pulse = Math.max(0, Math.min(1, this._mouthPulse));
      this.glowLayer.clear();
      this._drawAmbientLamp(pulse);
      if (pulse <= 0.01) return;

      const screenAlpha = pulse * 0.018;
      const roomAlpha = pulse * 0.006;
      this.glowLayer.beginFill(0x73d8ff, roomAlpha);
      this.glowLayer.drawRect(0, 0, this.app.screen.width, this.app.screen.height);
      this.glowLayer.endFill();
      this._drawPolygon(this.glowLayer, this._points, 0x9be9ff, screenAlpha);
    },

    _scaledRect(rect) {
      if (!rect || rect.length < 4 || !this.app) return null;
      const imgSize = this.cfg.img_size || [this.app.screen.width, this.app.screen.height];
      const sx = this.app.screen.width / Math.max(1, Number(imgSize[0]) || 1);
      const sy = this.app.screen.height / Math.max(1, Number(imgSize[1]) || 1);
      return {
        x: Number(rect[0]) * sx,
        y: Number(rect[1]) * sy,
        width: Number(rect[2]) * sx,
        height: Number(rect[3]) * sy,
      };
    },

    _scaledPoint(point, fallback) {
      const imgSize = this.cfg.img_size || [this.app.screen.width, this.app.screen.height];
      const sx = this.app.screen.width / Math.max(1, Number(imgSize[0]) || 1);
      const sy = this.app.screen.height / Math.max(1, Number(imgSize[1]) || 1);
      const p = point && point.length >= 2 ? point : fallback;
      return {
        x: Number(p[0]) * sx,
        y: Number(p[1]) * sy,
      };
    },

    _buildAmbientFromBackground(url) {
      if (!url || this.ambientUrl === url) return;
      this.ambientUrl = url;
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        try {
          const w = img.naturalWidth || img.width;
          const h = img.naturalHeight || img.height;
          const rectCfg = this.cfg.ambient_rect;
          if (!rectCfg || rectCfg.length < 4 || w <= 0 || h <= 0) return;

          const imgSize = this.cfg.img_size || [w, h];
          const sx = w / Math.max(1, Number(imgSize[0]) || 1);
          const sy = h / Math.max(1, Number(imgSize[1]) || 1);
          const focus = {
            x: Math.max(0, Math.floor(Number(rectCfg[0]) * sx)),
            y: Math.max(0, Math.floor(Number(rectCfg[1]) * sy)),
            width: Math.max(1, Math.floor(Number(rectCfg[2]) * sx)),
            height: Math.max(1, Math.floor(Number(rectCfg[3]) * sy)),
          };
          const padX = Math.round(focus.width * 0.55);
          const padY = Math.round(focus.height * 0.19);
          const rect = {
            x: Math.max(0, focus.x - padX),
            y: Math.max(0, focus.y - padY),
            width: focus.width + padX * 2,
            height: focus.height + padY * 2,
          };
          rect.width = Math.min(rect.width, w - rect.x);
          rect.height = Math.min(rect.height, h - rect.y);

          const source = document.createElement("canvas");
          source.width = w;
          source.height = h;
          const sourceCtx = source.getContext("2d", { willReadFrequently: true });
          sourceCtx.drawImage(img, 0, 0);
          const src = sourceCtx.getImageData(rect.x, rect.y, rect.width, rect.height);
          const raw = document.createElement("canvas");
          raw.width = w;
          raw.height = h;
          const rawCtx = raw.getContext("2d");
          const out = rawCtx.createImageData(rect.width, rect.height);
          const featherX = Math.max(1, rect.width * 0.28);
          const featherY = Math.max(1, rect.height * 0.24);

          for (let i = 0; i < src.data.length; i += 4) {
            const r = src.data[i];
            const g = src.data[i + 1];
            const b = src.data[i + 2];
            const px = (i / 4) % rect.width;
            const py = Math.floor((i / 4) / rect.width);
            let edge = Math.min(
              (px + 1) / featherX,
              (rect.width - px) / featherX,
              (py + 1) / featherY,
              (rect.height - py) / featherY,
              1
            );
            edge = edge * edge * (3 - 2 * edge);
            const cyan = Math.max(0, Math.min(g, b) - r * 0.55);
            const blue = Math.max(0, b - Math.max(r, g) * 0.45);
            const luma = (r * 0.24 + g * 0.52 + b * 0.24) / 255;
            let a = Math.max(0, Math.min(1, cyan / 120 + blue / 180 + luma * 0.13 - 0.08));
            a = Math.pow(a, 1.08) * edge;
            out.data[i] = 84;
            out.data[i + 1] = 200;
            out.data[i + 2] = 255;
            out.data[i + 3] = Math.round(a * 235);
          }
          rawCtx.putImageData(out, rect.x, rect.y);

          const blurred = document.createElement("canvas");
          blurred.width = w;
          blurred.height = h;
          const blurredCtx = blurred.getContext("2d");
          blurredCtx.filter = "blur(36px)";
          blurredCtx.drawImage(raw, 0, 0);
          blurredCtx.globalCompositeOperation = "lighter";
          blurredCtx.filter = "blur(14px)";
          blurredCtx.drawImage(raw, 0, 0);

          this._replaceAmbientSprite(PIXI.Texture.from(blurred), "generated-mask");
          console.info("[WallpaperScene] generated ambient mask:", {
            rect,
          });
        } catch (err) {
          console.warn("[WallpaperScene] ambient mask generation failed:", err);
        }
      };
      img.onerror = () => console.warn("[WallpaperScene] ambient source image failed:", url);
      img.src = url;
    },

    _drawAmbientLamp(pulse) {
      if (!this.ambientSprite && !this.ambientLowSprite) return;
      const idleDrift = 0.004 * (0.5 + 0.5 * Math.cos(this._time * 0.38 + 0.7));
      const burst = this._ambientBurst * (0.82 + Math.max(0, this._ambientNoise) * 2.8);
      const speech = this._speakingGate ? 0.16 : 0;
      const mouth = Math.max(0, Math.min(1, pulse)) * 0.22;
      const intensity = Math.max(0, Math.min(1.2, burst + speech + mouth + idleDrift + this._ambientNoise));
      if (this.ambientLowSprite) {
        const lowIdle = this._ambientLowIdleAlpha();
        const lowMin = this._ambientLowMinAlpha();
        this.ambientLowSprite.alpha = Math.max(0, Math.min(1.25, lowMin + (1 - Math.min(1, intensity)) * (lowIdle - lowMin)));
      }
      if (this.ambientSprite) {
        const alpha = this._ambientBaseAlpha() + intensity * this._ambientPulseAlpha();
        this.ambientSprite.alpha = Math.max(0, Math.min(1.25, alpha));
      }
    },

    _drawScanlineNoise() {
      if (!this.scanlineLayer || !this._crtBounds) return;
      const bounds = this._crtBounds;
      this.scanlineLayer.clear();
      const scanAlpha = Math.max(0, Math.min(0.30, ((this.cfg.scanline_alpha || 32) / 255)));
      const scanOffset = (this._time * 14) % 3;
      this.scanlineLayer.lineStyle(1, 0x000000, scanAlpha * 0.82);
      for (let y = bounds.y + scanOffset; y < bounds.bottom; y += 3) {
        this.scanlineLayer.moveTo(bounds.x, y);
        this.scanlineLayer.lineTo(bounds.right, y);
      }

      if (this._scanlineFlash > 0) {
        const n = 1 + Math.floor(this._scanlineFlash * 4);
        for (let i = 0; i < n; i += 1) {
          const y = bounds.y + Math.random() * bounds.height;
          const x = bounds.x + Math.random() * bounds.width * 0.25;
          const w = bounds.width * (0.25 + Math.random() * 0.75);
          const a = 0.025 + this._scanlineFlash * (0.035 + Math.random() * 0.045);
          this.scanlineLayer.lineStyle(1 + Math.random() * 1.5, 0xc9f7ff, a);
          this.scanlineLayer.moveTo(x, y);
          this.scanlineLayer.lineTo(Math.min(bounds.right, x + w), y + (Math.random() - 0.5) * 2);
        }
      }
    },

    _drawPolygon(graphics, points, color, alpha) {
      if (!points || points.length < 3 || alpha <= 0) return;
      graphics.beginFill(color, alpha);
      graphics.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i += 1) graphics.lineTo(points[i].x, points[i].y);
      graphics.lineTo(points[0].x, points[0].y);
      graphics.endFill();
    },

    _strokePolygon(graphics, points) {
      if (!points || points.length < 3) return;
      graphics.moveTo(points[0].x, points[0].y);
      for (let i = 1; i < points.length; i += 1) graphics.lineTo(points[i].x, points[i].y);
      graphics.lineTo(points[0].x, points[0].y);
    },
  };

  try {
    const pixiApp = callRender("getPixiApp", []);
    if (!pixiApp || !pixiApp.view) throw new Error("Pixi view is unavailable");
    pixiApp.view.addEventListener("webglcontextlost", function (event) {
      console.error("[WallpaperScene] WebGL context lost; preventing default restore path", event);
      diag("webgl.context_lost", {}, "error");
      if (event && typeof event.preventDefault === "function") event.preventDefault();
    }, false);
    pixiApp.view.addEventListener("webglcontextrestored", function () {
      console.info("[WallpaperScene] WebGL context restored");
      diag("webgl.context_restored");
      if (window.wallpaperApp && window.wallpaperApp.scene) {
        window.wallpaperApp.scene.layout();
      }
    }, false);
  } catch (err) {
    console.warn("[WallpaperScene] WebGL context monitor unavailable:", err);
  }

  window.wallpaperApp = {
    scene: desktopScene,
    character: characterRuntime,

    initDesktopScene(payload) { desktopScene.init(payload || {}); },
    setCanvas(payload) { desktopScene.setCanvas(payload || {}); },
    setCanvasPresentation(profile) { desktopScene.setCanvasPresentation(profile || {}); },
    setAttention(payload) { desktopScene.setAttention(payload || {}); },
    toggleCanvas() { desktopScene.toggleCanvas(); },
    setDefaultSubtitleEnabled(enabled) { desktopScene.setDefaultSubtitleEnabled(enabled); },

    setMode(mode) {
      const characterMode = desktopScene.setMode(mode);
      if (characterMode) characterRuntime.setMode(characterMode);
    },
    setActivity(activity) { desktopScene.setActivity(activity || null); },
    setWorkMode(enabled) { desktopScene.setWorkMode(!!enabled); },
    setAsrStatus(payload) { desktopScene.setAsrStatus(payload || {}); },
    loadSpriteFrames(emotion, urls) { characterRuntime.loadSpriteFrames(emotion, urls); },
    loadSpriteClipFrames(emotion, inUrls, loopUrls, outUrls) {
      characterRuntime.loadSpriteClipFrames(emotion, inUrls, loopUrls, outUrls);
    },
    setSpriteClipConfig(emotion, cfg) { characterRuntime.setSpriteClipConfig(emotion, cfg); },
    setIdleFrameIntervalMs(emotion, ms) { characterRuntime.setIdleFrameIntervalMs(emotion, ms); },
    loadMouthConfig(emotion, cfg) { characterRuntime.loadMouthConfig(emotion, cfg); },
    setEmotion(emotion) { characterRuntime.setEmotion(emotion); },
    setSpeaking(speaking) {
      desktopScene.setSpeaking(speaking);
      characterRuntime.setSpeaking(speaking);
    },
    setIdleAnimation(playing) { characterRuntime.setIdleAnimation(playing); },
    setMouth(value) {
      desktopScene.setMouth(value);
      characterRuntime.setMouth(value);
    },
    loadSpriteForgeGraph(payload) { characterRuntime.loadSpriteForgeGraph(payload || {}); },
    triggerSpriteForgeIntent(label, options) { characterRuntime.triggerSpriteForgeIntent(label, options); },
    holdSpriteFrame(frameIndex) { characterRuntime.holdSpriteFrame(frameIndex); },
    clearSpriteHold() { characterRuntime.clearSpriteHold(); },
    releaseSpriteForge(options) { characterRuntime.releaseSpriteForge(options); },
    setSubtitle(text) {
      const value = String(text || "");
      desktopScene._lastSubtitleText = value;
      if (value) scenarioRuntime.noteActivity();
      wallpaperSubtitle.setText(value);
      characterRuntime.setSubtitle(desktopScene._defaultSubtitleEnabled ? value : "");
      desktopScene._scheduleSubtitleClear(value);
    },
  };

  console.log("[WallpaperScene] ready");
})();
