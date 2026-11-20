/**
 * Optional Cubism adapter. Owns its model, offscreen texture and SDK calls.
 * The surface owns the Pixi clock, viewport, mask, visibility and color filters.
 */
(function (root) {
  "use strict";
  let sdkPromise = null;
  let coreSource = "";

  function script(url) {
    return new Promise((resolve, reject) => {
      const element = document.createElement("script");
      element.src = url;
      element.onload = resolve;
      element.onerror = () => {
        element.remove();
        reject(new Error("Cannot load the local Live2D dependency: " + url));
      };
      document.head.appendChild(element);
    });
  }

  async function ensureSDK(config) {
    if (coreSource && coreSource !== config.core_url) {
      throw new Error("Close and reopen Render or Wallpaper to apply the Cubism Core SDK change.");
    }
    if (!sdkPromise) {
      coreSource = config.core_url;
      sdkPromise = (async () => {
        const validCore = () => typeof root.Live2DCubismCore?.Version?.csmGetVersion === "function"
          && typeof root.Live2DCubismCore?.Moc?.fromArrayBuffer === "function"
          && typeof root.Live2DCubismCore?.Model?.fromMoc === "function";
        if (!validCore()) await script(config.core_url);
        if (!validCore()) throw new Error("The selected file is not a usable Cubism Core SDK.");
        if (!root.PIXI.live2d?.Live2DModel) {
          await script(new URL("./vendor/pixi-live2d-display.cubism4.min.js", document.baseURI).href);
        }
        if (!root.PIXI.live2d?.Live2DModel) throw new Error("The local Cubism display adapter is unavailable.");
        // Audio remains exclusively owned by Amadeus playback.
        root.PIXI.live2d.config.sound = false;
      })().catch(error => {
        sdkPromise = null;
        coreSource = "";
        throw error;
      });
    }
    await sdkPromise;
  }

  class Live2DCharacter {
    constructor(app, onStatus = () => {}) {
      this.app = app;
      this.container = new PIXI.Container();
      this.display = new PIXI.Sprite(PIXI.Texture.EMPTY);
      this.container.addChild(this.display);
      this.source = new PIXI.Container();
      this.model = null;
      this.texture = null;
      this.config = {};
      this.state = "unloaded";
      this.generation = 0;
      this.pending = Promise.resolve();
      this.intent = "normal";
      this.expressionRequest = 0;
      this.speaking = false;
      this.targetMouth = 0;
      this.smoothedMouth = 0;
      this.paused = false;
      this.viewport = { x: 0, y: 0, width: app.screen.width, height: app.screen.height };
      this.mouthParameters = [];
      this.onStatus = onStatus;
      this._tick = () => this.update(app.ticker.deltaMS);
      this._tickerAttached = false;
    }

    status(error = this.error || "") {
      return {
        surface: this.config.surface || "render",
        profile_id: this.config.profile_id || "",
        revision: this.config.revision || 0,
        runtime_id: this.config.runtime_id || "",
        state: this.state,
        ...(error ? { error } : {}),
        diagnostic: {
          expression: this.expression || "",
          art_bounds: this.artBounds || null,
          mouth_ids: this.mouthParameters.map(item => item.id),
          warnings: this.config.warnings || [],
          render_texture: this.texture ? {
            width: this.texture.width, height: this.texture.height,
            resolution: this.texture.baseTexture.resolution,
          } : null,
        },
      };
    }

    _report(state, error = "") {
      this.state = state;
      this.error = error;
      this.onStatus(this.status(error));
    }

    configure(config) {
      const sameModel = this.model && [
        "profile_id", "model_url", "core_url", "reload_revision",
      ].every(key => this.config[key] === config[key]);
      this.config = { ...config };
      if (sameModel && !config.error) {
        try {
          this._bindMouth();
          this._layout();
          this.applyIntent(this.intent);
          this._report("ready");
        } catch (error) {
          this._destroyModel();
          this._report("error", String(error.message || error));
        }
        return Promise.resolve(this.status());
      }
      const generation = ++this.generation;
      this._destroyModel();
      this.speaking = false;
      this.targetMouth = this.smoothedMouth = 0;
      this._report("loading");
      // Serialize SDK loads so old texture cache entries are disposed before a
      // new model using the same files begins loading.
      const previous = this.pending;
      const load = async () => {
        await previous.catch(() => {});
        if (generation !== this.generation) return;
        let candidate = null;
        try {
          if (config.error) throw new Error(config.error);
          await ensureSDK(config);
          if (generation !== this.generation) return;
          candidate = await new Promise((resolve, reject) => {
            const options = {
              autoUpdate: false, autoInteract: false,
              motionPreload: "IDLE",
              onLoad: () => resolve(candidate),
              onError: reject,
            };
            candidate = PIXI.live2d.Live2DModel.fromSync(config.model_url, options);
          });
          if (generation !== this.generation) {
            this._dispose(candidate);
            return;
          }
          const manager = candidate.internalModel.motionManager.expressionManager;
          for (let index = 0; index < (manager?.definitions.length || 0); index++) {
            if (!await manager.loadExpression(index)) {
              throw new Error("Cannot load the registered model expression: " + manager.definitions[index].Name);
            }
          }
          if (generation !== this.generation) {
            this._dispose(candidate);
            return;
          }
          this.model = candidate;
          candidate = null;
          // Freeze the initial visible artwork bounds. The exported canvas may
          // include empty space or artwork beyond its edge; animated expression
          // and breathing geometry must not change the framing every frame.
          this.artBounds = this._measureArtBounds();
          this.source.addChild(this.model);
          this.model.anchor.set(0, 0);
          this._bindMouth();
          this.model.internalModel.on("beforeModelUpdate", () => this._writeMouth());
          this._layout();
          await this.applyIntent(this.intent);
          if (!this._tickerAttached) {
            this.app.ticker.add(this._tick);
            this._tickerAttached = true;
          }
          this.update(0);
          this._report("ready");
        } catch (error) {
          if (candidate) this._dispose(candidate);
          if (generation !== this.generation) return;
          this._destroyModel();
          this._report("error", String(error.message || error));
        }
      };
      this.pending = load();
      return this.pending.then(() => this.status());
    }

    _measureArtBounds() {
      const internal = this.model.internalModel;
      const core = internal.coreModel;
      const transform = internal.localTransform;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let index = 0; index < core.getDrawableCount(); index++) {
        if (!(core.getDrawableOpacity(index) > 0)) continue;
        const vertices = internal.getDrawableVertices(index);
        for (let vertex = 0; vertex < vertices.length; vertex += 2) {
          const rawX = vertices[vertex], rawY = vertices[vertex + 1];
          const x = transform.a * rawX + transform.c * rawY + transform.tx;
          const y = transform.b * rawX + transform.d * rawY + transform.ty;
          if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
          minX = Math.min(minX, x); minY = Math.min(minY, y);
          maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
        }
      }
      if (!(maxX > minX && maxY > minY)) {
        throw new Error("The model has no usable visible drawable geometry.");
      }
      return { minX, minY, maxX, maxY, width: maxX - minX, height: maxY - minY };
    }

    _bindMouth() {
      const core = this.model.internalModel.coreModel;
      const available = core._model.parameters.ids;
      const declared = this.model.internalModel.settings.getLipSyncParameters();
      const ids = declared.length ? declared : (this.config.mouth?.parameter_ids || []);
      this.mouthParameters = ids.map(id => {
        const index = available.indexOf(id);
        if (index < 0) throw new Error("LipSync parameter is absent from this model: " + id);
        const low = core.getParameterMinimumValue(index);
        const high = core.getParameterMaximumValue(index);
        const closed = Math.max(low, Math.min(high, 0));
        return { id, low, high, closed };
      });
    }

    _writeMouth() {
      if (!this.model) return;
      const core = this.model.internalModel.coreModel;
      for (const parameter of this.mouthParameters) {
        const value = parameter.closed + (parameter.high - parameter.closed) * this.smoothedMouth;
        core.setParameterValueById(parameter.id, value);
      }
    }

    applyIntent(label) {
      this.intent = String(label || "normal").trim().toLowerCase();
      if (!this.model) return;
      const mapping = this.config.emotion_map || {};
      const expression = Object.prototype.hasOwnProperty.call(mapping, this.intent)
        ? mapping[this.intent] : null;
      if (!Object.prototype.hasOwnProperty.call(mapping, this.intent)) {
        console.warn("[Live2DCharacter] Unmapped semantic label uses the default pose:", this.intent);
      }
      return this.applyExpression(expression);
    }

    applyExpression(name) {
      if (!this.model) return;
      const request = ++this.expressionRequest;
      const model = this.model;
      const generation = this.generation;
      const manager = this.model.internalModel.motionManager.expressionManager;
      if (!manager) {
        if (name) this._report("error", "This model has no registered expressions.");
        return;
      }
      if (!name) {
        manager.reserveExpressionIndex = -1;
        manager.currentExpression = manager.defaultExpression;
        manager.resetExpression();
        this.expression = "";
        if (this.state === "ready") this._report("ready");
        return;
      }
      if (manager.getExpressionIndex(name) < 0) {
        manager.reserveExpressionIndex = -1;
        manager.currentExpression = manager.defaultExpression;
        manager.resetExpression();
        this.expression = "";
        console.warn("[Live2DCharacter] Missing model expression uses the default pose:", name);
        if (this.state === "ready") this._report("ready");
        return;
      }
      return model.expression(name).then(() => {
        if (this.model !== model || this.generation !== generation || request !== this.expressionRequest) return;
        this.expression = name;
        if (this.state === "ready") this._report("ready");
      }).catch(error => {
        if (this.model === model && this.generation === generation && request === this.expressionRequest) {
          this._report("error", String(error.message || error));
        }
      });
    }

    release() {
      this.applyIntent("normal");
    }

    setSpeaking(value) {
      this.speaking = value === true;
      if (!this.speaking) {
        this.targetMouth = this.smoothedMouth = 0;
        this._writeMouth();
      }
    }

    setMouth(value) {
      this.targetMouth = this.speaking ? Math.max(0, Math.min(1, Number(value) || 0)) : 0;
    }

    setViewport(bounds) {
      this.viewport = bounds ? {
        x: Number(bounds.x) || 0, y: Number(bounds.y) || 0,
        width: Math.max(1, Number(bounds.width) || 1),
        height: Math.max(1, Number(bounds.height) || 1),
      } : { x: 0, y: 0, width: this.app.screen.width, height: this.app.screen.height };
      this._layout();
    }

    setPaused(value) {
      this.paused = value === true;
    }

    _layout() {
      if (!this.model) return;
      const viewport = this.viewport;
      const layout = this.config.layout || {};
      const width = Math.ceil(viewport.width);
      const height = Math.ceil(viewport.height);
      // At most one viewport-sized target, with the same resolution budget as
      // the surface. Cubism draws inside it; ordinary Pixi draws/masks the result.
      if (!this.texture || this.texture.width !== width || this.texture.height !== height) {
        if (this.texture) this.texture.destroy(true);
        const resolution = Math.min(this.app.renderer.resolution || 1,
          4096 / width, 4096 / height);
        this.texture = PIXI.RenderTexture.create({ width, height, resolution });
        this.display.texture = this.texture;
      }
      this.display.position.set(viewport.x, viewport.y);
      this.display.width = viewport.width;
      this.display.height = viewport.height;
      const art = this.artBounds;
      const scale = Math.min(viewport.width / art.width,
        viewport.height / art.height) * (Number(layout.scale) || 1);
      this.model.scale.set(scale);
      this.model.position.set(
        viewport.width * (.5 + (Number(layout.x) || 0)) - (art.minX + art.maxX) * .5 * scale,
        viewport.height * (1 + (Number(layout.y) || 0)) - art.maxY * scale);

    }

    update(deltaMS) {
      if (!this.model || this.paused) return;
      for (let layer = this.container; layer; layer = layer.parent) {
        if (!layer.visible || !layer.renderable) return;
      }
      const dt = Math.max(0, Math.min(100, Number(deltaMS) || 0));
      const gain = Number(this.config.mouth?.gain ?? 1);
      const target = this.speaking ? Math.min(1, this.targetMouth * gain) : 0;
      const smoothing = Number(this.config.mouth?.smoothing_ms || 0);
      const blend = smoothing > 0 ? 1 - Math.exp(-dt / smoothing) : 1;
      this.smoothedMouth += (target - this.smoothedMouth) * blend;
      if (!this.speaking) this.smoothedMouth = 0;
      this.model.update(dt);
      this.app.renderer.render(this.source, { renderTexture: this.texture, clear: true });
    }

    _dispose(model) {
      // fromSync gives us ownership even when a bad file fails before its
      // internal model exists; its normal destroy method assumes that field.
      if (model.internalModel) {
        model.destroy({ children: true, texture: true, baseTexture: true });
      } else {
        model.emit("destroy");
        model.autoUpdate = false;
        for (const texture of model.textures || []) texture.destroy(true);
        PIXI.Container.prototype.destroy.call(model, { children: true });
      }
    }

    _destroyModel() {
      ++this.expressionRequest;
      if (this._tickerAttached) {
        this.app.ticker.remove(this._tick);
        this._tickerAttached = false;
      }
      if (this.model) {
        this.source.removeChild(this.model);
        this._dispose(this.model);
        this.model = null;
      }
      this.display.texture = PIXI.Texture.EMPTY;
      if (this.texture) {
        this.texture.destroy(true);
        this.texture = null;
      }
      this.mouthParameters = [];
      this.artBounds = null;
      this.expression = "";
      this.targetMouth = this.smoothedMouth = 0;
    }

    unload() {
      ++this.generation;
      this.speaking = false;
      this._destroyModel();
      this._report("unloaded");
    }

    destroy() {
      this.unload();
      this.source.destroy({ children: true });
      this.container.destroy({ children: true });
    }
  }

  root.Live2DCharacter = Live2DCharacter;
  if (typeof module !== "undefined" && module.exports) module.exports = { Live2DCharacter };
})(typeof window !== "undefined" ? window : globalThis);
