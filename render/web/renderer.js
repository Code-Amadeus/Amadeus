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
  const app = new PIXI.Application({
    resizeTo: document.getElementById("canvas-container"),
    backgroundAlpha: 0,          // Transparent background
    autoDensity: true,
    resolution: window.devicePixelRatio || 1,
    antialias: true,
  });
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
      this._texturePromisesByUrl = new Map();
      this._compressedTextureRuntimePromise = null;
      this._compressedTextureRuntimeReady = false;
      this._framePromises = {};
      this._frameSetStates = {};
      this._frameLoadQueue = new Map();
      this._frameLoadSerial = 0;
      this._activeFrameSetLoads = 0;
      this._maxConcurrentFrameSetLoads = 1;
      this._frameLoadPumpTimer = null;
      this._lastSpeechStateChangedAt = 0;
      this._framesPerLoadSlice = 2;
      this._speechQuietBeforeSpeculativeMs = 900;
      this._speculativeLoadEpoch = 0;
      this._pinnedFrameLabels = new Set([
        "idle",
        "speaking_short",
        "speaking_med",
        "speaking_long",
        "speaking_trans",
        "speaking_loop1",
        "speaking_loop2",
        "closed_eye_trans",
        "speaking_closed_eye_1",
        "speaking_closed_eye_2",
        "smile_speaking",
        "sad_speaking",
        "shy_speaking1",
        "shy_speaking2",
        "surprise_speaking",
        "angry_speaking",
      ]);
      this._warmFrameLabels = new Set([
        "thinking_trans",
        "thinking_speaking1",
        "thinking_speaking2",
        "thinking_to_serious",
        "thinking_to_key_point",
        "key_point_speaking",
        "serious_to_thinking",
      ]);
      /** emotion → PIXI.Texture[][] (transition from→to) */
      this._transitions = {};

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

      this._startIdleTicker();
    }

    // ---- Asset loading ----

    loadFrames(emotion, urls) {
      if (this._sameUrlList(this._frameUrls[emotion], urls) && this._frames[emotion]) {
        return;
      }
      this._frameUrls[emotion] = Array.isArray(urls) ? urls.slice() : [];
      console.log("[SpriteRenderer] registerFrames:", emotion, urls.length, "urls, first:", (urls[0]||'').slice(0, 100));
      const textures = new Array(urls.length);
      this._frames[emotion] = textures;
      this._framePromises[emotion] = new Array(urls.length);
      this._frameSetStates[emotion] = "cold";

      // Poster frame keeps low-frequency graph hops from flashing blank while
      // the full clip is still cold. The rest of the clip is loaded by the
      // priority scheduler below.
      this._ensureFrameIndex(emotion, 0);

      const priority = this._initialFrameSetPriority(emotion);
      if (priority > 0) {
        this._queueFrameSet(emotion, priority, { reason: "register" });
      }
    }

    loadTransitionFrames(fromEmotion, toEmotion, urls) {
      const key = `${fromEmotion}->${toEmotion}`;
      const textures = new Array(urls.length);
      this._transitions[key] = textures;
      let loaded = 0;
      urls.forEach((u, idx) => {
        this._loadTextureFromImage(u).then((tex) => {
          if (!tex) return;
          textures[idx] = tex;
          loaded++;
          if (loaded === urls.length) {
            console.log("[SpriteRenderer] transition ready:", key, loaded);
          }
        });
      });
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
      this._cycleCompletedForEmotion = "";
      this._hideMouthLayer();
      this._queueFrameSet(emotion, this._currentFrameSetPriority(emotion), { reason: "current" });

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
        this.clearSpeculativeFrameLoads();
      } else {
        this._scheduleFrameLoadPump(320);
      }
      if (speaking) {
        this._frameIdx = 0;
        this._cycleCompletedForEmotion = "";
        this._held = false;
      }
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
    }

    setClipConfig(emotion, config) {
      this._clipConfigs[emotion] = config;
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
      const urls = Array.isArray(config.frameUrls) ? config.frameUrls : [];
      const textures = new Array(urls.length);
      this._mouthTextures[label] = textures;
      urls.forEach((u, idx) => {
        this._loadTextureFromImage(u).then((tex) => {
          if (!tex) return;
          textures[idx] = tex;
          if (label === this._currentEmotion) {
            this._updateMouthLayer();
          }
        });
      });
      console.log("[SpriteRenderer] mouth config for", label, JSON.stringify(config).slice(0, 120));
    }

    _sameUrlList(a, b) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) {
        if (a[i] !== b[i]) return false;
      }
      return true;
    }

    prefetchLabels(labels, priority = "interactive", options = {}) {
      const score = this._priorityScore(priority);
      if (!score) return;
      for (const label of labels || []) {
        const emotion = String(label || "").trim();
        if (!emotion) continue;
        this._queueFrameSet(emotion, score, {
          reason: options.reason || priority,
          speculative: !!options.speculative,
        });
      }
    }

    clearSpeculativeFrameLoads() {
      let cleared = 0;
      this._speculativeLoadEpoch += 1;
      for (const [label, entry] of Array.from(this._frameLoadQueue.entries())) {
        if (entry && entry.speculative) {
          this._frameLoadQueue.delete(label);
          cleared += 1;
        }
      }
      if (cleared > 0) {
        console.log("[SpriteRenderer] cleared speculative frame loads:", cleared);
      }
    }

    _initialFrameSetPriority(emotion) {
      if (this._pinnedFrameLabels.has(emotion)) return 100;
      if (this._warmFrameLabels.has(emotion)) return 65;
      return 0;
    }

    _currentFrameSetPriority(emotion) {
      if (this._pinnedFrameLabels.has(emotion)) return 100;
      if (this._warmFrameLabels.has(emotion)) return 90;
      if (/speaking|trans|thinking|key_point/.test(String(emotion || ""))) return 90;
      return 65;
    }

    _priorityScore(priority) {
      if (typeof priority === "number") return priority;
      if (priority === "pinned" || priority === "current") return 100;
      if (priority === "interactive") return 90;
      if (priority === "speaking") return 85;
      if (priority === "warm") return 65;
      if (priority === "ambient") return 25;
      if (priority === "poster") return 10;
      return 0;
    }

    _queueFrameSet(emotion, priority, options = {}) {
      if (!emotion || !this._frameUrls[emotion] || !this._frameUrls[emotion].length) return;
      const currentState = this._frameSetStates[emotion];
      if (currentState === "warm" && priority < 100) return;
      const existing = this._frameLoadQueue.get(emotion);
      const next = {
        priority: Math.max(priority, existing ? existing.priority : 0),
        serial: existing ? existing.serial : ++this._frameLoadSerial,
        speculative: !!(options.speculative || (existing && existing.speculative)),
        speculativeEpoch: options.speculative ? this._speculativeLoadEpoch : (existing && existing.speculativeEpoch) || this._speculativeLoadEpoch,
        reason: options.reason || (existing && existing.reason) || "",
      };
      this._frameLoadQueue.set(emotion, next);
      this._scheduleFrameLoadPump(0);
    }

    _scheduleFrameLoadPump(delayMs = 0) {
      if (this._frameLoadPumpTimer) return;
      this._frameLoadPumpTimer = setTimeout(() => {
        this._frameLoadPumpTimer = null;
        this._pumpFrameLoadQueue();
      }, Math.max(0, delayMs));
    }

    _pumpFrameLoadQueue() {
      while (this._activeFrameSetLoads < this._maxConcurrentFrameSetLoads && this._frameLoadQueue.size > 0) {
        let selectedLabel = "";
        let selectedEntry = null;
        for (const [label, entry] of this._frameLoadQueue.entries()) {
          if (this._shouldDeferFrameSet(entry)) continue;
          if (
            !selectedEntry ||
            entry.priority > selectedEntry.priority ||
            (entry.priority === selectedEntry.priority && entry.serial < selectedEntry.serial)
          ) {
            selectedLabel = label;
            selectedEntry = entry;
          }
        }
        if (!selectedLabel) {
          this._scheduleFrameLoadPump(360);
          return;
        }
        this._frameLoadQueue.delete(selectedLabel);
        this._activeFrameSetLoads += 1;
        this._loadFrameSet(selectedLabel, selectedEntry).finally(() => {
          this._activeFrameSetLoads = Math.max(0, this._activeFrameSetLoads - 1);
          this._scheduleFrameLoadPump(0);
        });
      }
    }

    _shouldDeferFrameSet(entry) {
      if (!entry) return false;
      if (entry.priority >= 90) return false;
      if (this._speaking) return true;
      const quietMs = Date.now() - this._lastSpeechStateChangedAt;
      return quietMs < this._speechQuietBeforeSpeculativeMs;
    }

    async _loadFrameSet(emotion, entry) {
      const urls = this._frameUrls[emotion] || [];
      if (!urls.length) return;
      this._frameSetStates[emotion] = "loading";
      console.log("[SpriteRenderer] frame load start:", emotion, urls.length, entry && entry.reason ? entry.reason : "");
      let loadedThisSlice = 0;
      for (let i = 0; i < urls.length; i += 1) {
        if (entry && entry.speculative && entry.speculativeEpoch !== this._speculativeLoadEpoch) {
          this._frameSetStates[emotion] = "cold";
          return;
        }
        if (this._shouldDeferFrameSet(entry)) {
          this._frameSetStates[emotion] = "cold";
          this._queueFrameSet(emotion, entry.priority, entry);
          return;
        }
        await this._ensureFrameIndex(emotion, i);
        loadedThisSlice += 1;
        if (loadedThisSlice >= this._framesPerLoadSlice) {
          loadedThisSlice = 0;
          await this._yieldFrameLoadSlice(entry);
        }
      }
      this._frameSetStates[emotion] = "warm";
      console.log("[SpriteRenderer] frame load ready:", emotion, urls.length);
    }

    _yieldFrameLoadSlice(entry) {
      const delay = this._speaking ? 16 : (entry && entry.priority >= 90 ? 0 : 24);
      if (typeof requestIdleCallback === "function" && (!entry || entry.priority < 90) && !this._speaking) {
        return new Promise((resolve) => requestIdleCallback(resolve, { timeout: 80 }));
      }
      return new Promise((resolve) => setTimeout(resolve, delay));
    }

    _ensureFrameIndex(emotion, idx) {
      const urls = this._frameUrls[emotion] || [];
      const textures = this._frames[emotion] || [];
      if (!urls.length || idx < 0 || idx >= urls.length) return Promise.resolve(null);
      if (textures[idx]) return Promise.resolve(textures[idx]);
      if (!this._framePromises[emotion]) this._framePromises[emotion] = new Array(urls.length);
      if (this._framePromises[emotion][idx]) return this._framePromises[emotion][idx];

      const promise = this._loadTextureFromImage(urls[idx]).then((tex) => {
        if (tex) {
          textures[idx] = tex;
          if (emotion === this._currentEmotion && idx === this._frameIdx) {
            this._showFrame(idx);
          }
        }
        return tex;
      }).finally(() => {
        if (this._framePromises[emotion]) this._framePromises[emotion][idx] = null;
      });
      this._framePromises[emotion][idx] = promise;
      return promise;
    }

    holdFrame(which) {
      if (which === undefined || which === null) {
        this._heldFrameIdx = this._frameIdx; // hold current
      } else if (which === -1) {
        const frames = this._getEmotionFrames();
        this._heldFrameIdx = frames ? frames.length - 1 : this._frameIdx; // hold last
      } else {
        this._heldFrameIdx = which;
      }
      this._held = true;
      this._activeFramePhase = "frames";
      this._activeFrameIdx = this._heldFrameIdx;
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
      const frames = this._getEmotionFrames();
      if (!frames || frames.length === 0) return;
      const targetIdx = idx % frames.length;
      let texture = frames[targetIdx];
      if (!texture) {
        // file:// assets are decoded asynchronously. Falling back to the first
        // loaded frame creates visible mid-animation snaps; keep the previous
        // frame until the requested texture is ready.
        const hasCurrentTexture = this.sprite.texture && this.sprite.texture.height > 1;
        if (hasCurrentTexture) return;
        texture = frames.find(Boolean);
      }
      if (!texture) return;
      this._frameIdx = targetIdx;
      this._activeFramePhase = "frames";
      this._activeFrameIdx = targetIdx;
      this._applyFrame(texture);
    }

    _loadTextureFromImage(url) {
      const key = String(url || "");
      if (!key) return Promise.resolve(null);

      const cached = this._texturePromisesByUrl.get(key);
      if (cached) {
        return cached.then((texture) => {
          if (texture && !texture.destroyed && texture.baseTexture && !texture.baseTexture.destroyed) {
            return texture;
          }
          this._texturePromisesByUrl.delete(key);
          return this._loadTextureFromImage(key);
        });
      }

      const loadPromise = this._isKtx2Url(key)
        ? this._loadTextureFromCompressedAsset(key)
        : this._loadTextureFromRasterImage(key);

      this._texturePromisesByUrl.set(key, loadPromise);
      return loadPromise;
    }

    _isKtx2Url(url) {
      return /\.ktx2(?:[?#]|$)/i.test(String(url || ""));
    }

    _pngFallbackUrl(url) {
      const key = String(url || "");
      if (!this._isKtx2Url(key)) return "";
      return key
        .replace(/(frames[^/?#]*?)_ktx2_uastc_q\d+_z\d+(?=[/\\])/i, "$1")
        .replace(/\.ktx2(?=([?#]|$))/i, ".png");
    }

    _loadTextureFromRasterImage(key) {
      return new Promise((resolve) => {
        const img = new Image();
        img.decoding = "async";
        if (/^https?:\/\//i.test(key)) {
          img.crossOrigin = "anonymous";
        }
        img.onload = async () => {
          try {
            if (typeof img.decode === "function") {
              try {
                await img.decode();
              } catch (_) {
                // onload already fired; some Chromium/file:// paths reject decode().
              }
            }
            const tex = PIXI.Texture.from(img);
            if (tex.baseTexture && typeof tex.baseTexture.update === "function") {
              tex.baseTexture.update();
            }
            await this._prepareTexture(tex);
            resolve(tex);
          } catch (e) {
            console.error("[SpriteRenderer] Texture.from failed:", key.slice(0, 160), e);
            resolve(null);
          }
        };
        img.onerror = () => {
          console.error("[SpriteRenderer] IMG FAIL:", key.slice(0, 160));
          resolve(null);
        };
        img.src = key;
      }).then((texture) => {
        if (!texture) {
          this._texturePromisesByUrl.delete(key);
        }
        return texture;
      });
    }

    async _ensureCompressedTextureRuntime() {
      if (this._compressedTextureRuntimeReady) return true;
      if (this._compressedTextureRuntimePromise) {
        return this._compressedTextureRuntimePromise;
      }
      this._compressedTextureRuntimePromise = (async () => {
        if (!window.PixiBasisKtx2Shim || !PIXI.Assets) {
          console.warn("[SpriteRenderer] KTX2 loader unavailable; falling back to PNG");
          return false;
        }
        try {
          await PixiBasisKtx2Shim.KTX2Parser.loadTranscoder(
            "./vendor/basis_transcoder.js",
            "./vendor/basis_transcoder.wasm"
          );
          await PIXI.Assets.init({
            texturePreference: { format: ["ktx2", "ktx", "png"] },
          });
          this._compressedTextureRuntimeReady = true;
          console.log("[SpriteRenderer] KTX2 compressed texture runtime ready");
          return true;
        } catch (e) {
          console.warn("[SpriteRenderer] KTX2 runtime init failed; falling back to PNG", e);
          return false;
        }
      })();
      return this._compressedTextureRuntimePromise;
    }

    async _loadTextureFromCompressedAsset(key) {
      if (!(await this._ensureCompressedTextureRuntime())) {
        const fallback = this._pngFallbackUrl(key);
        return fallback ? this._loadTextureFromImage(fallback) : null;
      }
      try {
        const loaded = await PIXI.Assets.load({
          src: key,
          format: "ktx2",
          loadParser: "loadKTX2",
        });
        const texture = Array.isArray(loaded) ? loaded[0] : loaded;
        if (!texture) throw new Error("PIXI.Assets.load returned no texture");
        const tex = texture.baseTexture ? texture : new PIXI.Texture(texture);
        await this._prepareTexture(tex);
        return tex;
      } catch (e) {
        console.warn("[SpriteRenderer] KTX2 load failed, trying PNG fallback:", key.slice(0, 160), e);
        this._texturePromisesByUrl.delete(key);
        const fallback = this._pngFallbackUrl(key);
        return fallback ? this._loadTextureFromImage(fallback) : null;
      }
    }

    _prepareTexture(texture) {
      return new Promise((resolve) => {
        const prepare = app.renderer && app.renderer.plugins && app.renderer.plugins.prepare;
        if (!prepare || typeof prepare.upload !== "function") {
          resolve();
          return;
        }
        try {
          prepare.upload(texture, resolve);
        } catch (_) {
          resolve();
        }
      });
    }

    _applyFrame(texture) {
      if (!texture) return;
      this.sprite.texture = texture;
      this._applyCurrentTransform();
      this._updateMouthLayer();
    }

    _applyCurrentTransform() {
      const texture = this.sprite.texture;
      if (!texture || texture.height <= 1) {
        this.sprite.scale.set(1);
        return;
      }
      if (this._viewportBounds) {
        const b = this._viewportBounds;
        const maxW = b.width * 0.64;
        const maxH = b.height * 0.90;
        const scale = Math.min(maxW / texture.width, maxH / texture.height);
        this.sprite.scale.set(scale);
        this.sprite.x = b.x + b.width * 0.52;
        this.sprite.y = b.y + b.height * 0.96;
        return;
      }
      const h = app.screen.height;
      const scale = h / texture.height;
      this.sprite.scale.set(scale);
      this.sprite.x = app.screen.width / 2;
      this.sprite.y = h;
    }

    _hideMouthLayer() {
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
        const steps = Math.min(4, Math.floor(elapsed / interval));
        elapsed -= steps * interval;

        let advanced = false;
        for (let i = 0; i < steps; i++) {
          if (onceThenHold && this._frameIdx >= frames.length - 1) {
            break;
          }

          if (onceThenHold) {
            this._frameIdx = Math.min(this._frameIdx + 1, frames.length - 1);
          } else if (this._speaking && mouthCfg && frames.length > 1) {
            this._frameIdx = (this._frameIdx + 1) % frames.length;
          } else if (this._speaking && frames.length > 1) {
            this._frameIdx = (this._frameIdx % (frames.length - 1)) + 1;
          } else {
            this._frameIdx = (this._frameIdx + 1) % frames.length;
          }
          advanced = true;
        }

        if (!advanced) {
          return;
        }

        this._showFrame(this._frameIdx);
        if (onceThenHold && this._frameIdx >= frames.length - 1) {
          this._notifyCycleComplete(emotion);
        } else if (!onceThenHold && this._frameIdx === 0) {
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
      let idx = 0;
      const step = () => {
        if (idx >= this._transitionQueue.length) {
          this._transitionQueue = [];
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
      if (typeof this.sprite.clearSpeculativeFrameLoads === "function") {
        this.sprite.clearSpeculativeFrameLoads();
      }
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
      this._prefetchLabels(labels, priority, { reason: `trigger:${targetLabel}` });
    }

    _prefetchNodeNeighborhood(nodeId) {
      const edges = (this.graph.edges || [])
        .filter((e) => e.from === nodeId && Number(e.prob || 0) > 0)
        .sort((a, b) => Number(b.prob || 0) - Number(a.prob || 0));
      if (!edges.length) return;
      const immediate = new Set();
      const speculative = new Set();
      edges.forEach((edge, index) => {
        const label = this._label(edge.to);
        if (!label) return;
        if (index === 0 || Number(edge.prob || 0) >= 0.4) immediate.add(label);
        else speculative.add(label);
      });
      this._prefetchLabels(immediate, "warm", { reason: "graph-next" });
      this._prefetchLabels(speculative, "ambient", { reason: "graph-speculative", speculative: true });
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
