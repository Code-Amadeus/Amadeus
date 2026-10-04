(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.FrameTextureBackend = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  // The shim and its worker pool belong to the page, not to a display owner.
  // Share initialization without terminating workers borrowed by another caller.
  const transcoderInitializations = new WeakMap();

  function abortError() {
    const error = new Error("Frame texture operation cancelled");
    error.name = "AbortError";
    return error;
  }

  function pngFallbackUrl(url) {
    const key = String(url || "");
    if (!/\.ktx2(?:[?#]|$)/i.test(key)) return "";
    return key
      .replace(/(frames[^/?#]*?)_ktx2_uastc_q\d+_z\d+(?=[/\\])/i, "$1")
      .replace(/\.ktx2(?=([?#]|$))/i, ".png");
  }

  function waitWithAbort(promise, signal) {
    return new Promise((resolve, reject) => {
      const abort = () => reject(abortError());
      if (signal.aborted) { reject(abortError()); return; }
      signal.addEventListener("abort", abort, { once: true });
      Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
  }

  function resourceBytes(resource, compressed) {
    const buffers = new Set();
    const retain = value => {
      const buffer = ArrayBuffer.isView(value) ? value.buffer : value instanceof ArrayBuffer ? value : null;
      if (buffer) buffers.add(buffer);
    };
    retain(resource.data);
    retain(resource.buffer?.rawBinaryData);
    const levels = resource._levelBuffers || [];
    for (const level of levels) retain(level.levelBuffer);
    const cpuBytes = [...buffers].reduce((total, buffer) => total + buffer.byteLength, 0);
    const gpuBytes = compressed
      ? levels.reduce((total, level) => total + level.levelBuffer.byteLength, 0)
      : resource.data.byteLength;
    return { cpuBytes, gpuBytes };
  }

  function createFrameTextureBackend(options) {
    const renderer = options.renderer;
    const pixi = options.pixi || root.PIXI;
    const shim = options.shim || root.PixiBasisKtx2Shim;
    const fetchAsset = options.fetch || ((url, init) => pixi.settings.ADAPTER.fetch(url, init));
    const createImage = options.createImage || (() => new root.Image());
    const now = options.now || (() => root.performance.now());
    const owned = new Map(), loads = new Set(), uploads = new Map();
    const cache = options.cache === false ? null : options.cache
      || root.FrameTextureCache?.createTextureCache({ renderer, pixi });
    const cacheWrites = new Map();
    const counters = { fetchAttempts: 0, fetchCompleted: 0, fetchedPayloadBytes: 0,
      transcodeAttempts: 0, transcodesCompleted: 0, transcodeMs: 0, textureUploads: 0, textureUploadMs: 0 };
    const ticker = pixi.Ticker.system, canvas = renderer.view;
    let disposed = false, scheduled = false, contextLost = false;
    let extensionsContextUid = renderer.CONTEXT_UID;

    const lost = () => contextLost || Boolean(renderer.gl?.isContextLost());
    const check = signal => { if (disposed || signal.aborted) throw abortError(); };
    const stopTicker = () => { if (scheduled) { ticker.remove(uploadTick); scheduled = false; } };
    const schedule = () => {
      if (!scheduled && uploads.size && !disposed && !lost()) {
        scheduled = true;
        ticker.add(uploadTick, null, pixi.UPDATE_PRIORITY.NORMAL);
      }
    };

    function prepareCompressedContext(resource) {
      if (resource instanceof pixi.CompressedTextureResource && extensionsContextUid !== renderer.CONTEXT_UID) {
        // Pixi 7's restore handler emits contextChange without getExtensions.
        // Cached extension objects survive, but WebGL extension enablement does
        // not. Use the context owner's initializer before uploads or redraws.
        renderer.context.getExtensions();
        extensionsContextUid = renderer.CONTEXT_UID;
      }
    }

    function uploadTick() {
      if (disposed || lost()) { stopTicker(); return; }
      const started = now();
      for (const [texture, job] of uploads) {
        if (lost()) { stopTicker(); return; }
        try {
          const base = texture.baseTexture;
          if (!owned.has(texture) || !base || base.destroyed) throw abortError();
          prepareCompressedContext(base.resource);
          const gpu = base._glTextures[renderer.CONTEXT_UID];
          if (!gpu || gpu.dirtyId !== base.dirtyId) renderer.texture.bind(base);
          // A loss during bind must leave this job queued until restoration.
          if (lost()) { stopTicker(); return; }
          const uploaded = base._glTextures[renderer.CONTEXT_UID];
          if (!uploaded || uploaded.dirtyId !== base.dirtyId) throw new Error("Frame texture upload did not complete");
          uploads.delete(texture);
          persist(texture);
          job.resolve();
        } catch (error) {
          uploads.delete(texture);
          job.reject(error);
        }
        // A single synchronous GL upload cannot be preempted. Bound subsequent
        // uploads by elapsed time rather than admitting a whole decoded clip.
        if (now() - started >= 2) break;
      }
      if (!uploads.size) stopTicker();
    }

    const onLost = () => { contextLost = true; stopTicker(); };
    const onRestored = () => {
      contextLost = false;
      // Already admitted textures redraw through Pixi without entering this
      // queue. Enable their formats before the next draw as well. If none are
      // owned now, the first later compressed upload handles the new context.
      for (const texture of owned.keys()) prepareCompressedContext(texture.baseTexture?.resource);
      schedule();
    };
    canvas?.addEventListener("webglcontextlost", onLost);
    canvas?.addEventListener("webglcontextrestored", onRestored);

    function destroy(texture) {
      if (!owned.has(texture)) return;
      const releaseSource = owned.get(texture);
      owned.delete(texture);
      cacheWrites.delete(texture);
      const job = uploads.get(texture);
      if (job) { uploads.delete(texture); job.reject(abortError()); }
      if (!uploads.size) stopTicker();
      try { texture.destroy(true); }
      finally { releaseSource?.(); }
    }

    function persist(texture) {
      const entry = cacheWrites.get(texture);
      if (!entry) return;
      cacheWrites.delete(texture);
      cache.save(entry, texture.baseTexture.resource);
    }

    function makeTexture(resource, baseOptions, releaseSource) {
      let base;
      resource.internal = true;
      // Observe the resource boundary, including Pixi GC/restore uploads which
      // bypass our queue. This is synchronous CPU submission time, not GPU time.
      const uploadResource = resource.upload;
      resource.upload = function (...args) {
        const started = now();
        const uploaded = uploadResource.apply(this, args);
        if (uploaded) { counters.textureUploads++; counters.textureUploadMs += now() - started; }
        return uploaded;
      };
      try {
        base = new pixi.BaseTexture(resource, baseOptions);
        const texture = new pixi.Texture(base);
        owned.set(texture, releaseSource);
        return texture;
      } catch (error) {
        if (base) base.destroy(); else resource.destroy();
        releaseSource?.();
        throw error;
      }
    }

    function ensureTranscoder() {
      const parser = shim?.KTX2Parser;
      if (!parser) return Promise.reject(new Error("KTX2 transcoder unavailable"));
      if (!transcoderInitializations.has(parser)) {
        const initialized = Promise.resolve().then(async () => {
          if (!parser.ktx2Binding && !(parser.TranscoderWorker?.jsSource && parser.TranscoderWorker?.wasmSource)) {
            await parser.loadTranscoder("./vendor/basis_transcoder.js", "./vendor/basis_transcoder.wasm");
          }
          if (typeof root.Worker !== "undefined") parser.TRANSCODER_WORKER_POOL_LIMIT = 2;
          return parser;
        });
        transcoderInitializations.set(parser, initialized);
      }
      return transcoderInitializations.get(parser);
    }

    async function loadCompressed(url, signal) {
      // Use Pixi's adapter, including its supported file:// behavior in the GUI.
      const useCache = cache?.canUse(url);
      const read = async address => {
        check(signal);
        counters.fetchAttempts++;
        const response = await fetchAsset(address, { signal,
          ...(useCache ? { headers: { Accept: 'application/x-amadeus-bc7, image/ktx2' } } : {}) });
        if (response.ok === false) throw new Error(`Frame fetch failed (${response.status})`);
        const bytes = await response.arrayBuffer();
        counters.fetchCompleted++; counters.fetchedPayloadBytes += bytes.byteLength;
        check(signal);
        return { response, bytes };
      };
      let { response, bytes } = await read(useCache ? cache.requestUrl(url) : url);
      check(signal);
      let resources, cacheWrite;
      if (useCache && response.headers?.get('Content-Type') === 'application/x-amadeus-bc7') {
        try {
          resources = [await cache.decode(bytes, response.headers.get('X-Amadeus-BC7-Key'))];
          resources.basisFormat = 6;
          if (disposed || signal.aborted) { resources[0].destroy(); throw abortError(); }
        } catch (error) {
          check(signal);
          cache.invalidate(url);
          ({ response, bytes } = await read(cache.sourceUrl(url)));
        }
      }
      if (!resources) {
        const parser = await waitWithAbort(ensureTranscoder(), signal);
        check(signal);
        if (useCache && response.headers?.get('X-Amadeus-BC7-Key')) {
          cache.miss();
          cacheWrite = { url, key: response.headers.get('X-Amadeus-BC7-Key'), token: response.headers.get('X-Amadeus-Cache-Token') };
        }
        // The existing worker API cannot cancel one submitted transcode. Keep the
        // caller's load slot occupied until it finishes, then destroy cancelled
        // results. Racing this work against abort would hide still-active jobs.
        let decoded;
        counters.transcodeAttempts++;
        const started = now();
        const transcoded = Promise.resolve(parser.transcode(bytes)).then(resources => {
          counters.transcodesCompleted++;
          counters.transcodeMs += now() - started;
          decoded = resources;
          if (disposed || signal.aborted) {
            for (const resource of resources || []) resource.destroy();
            throw abortError();
          }
          return resources;
        });
        try { resources = await transcoded; check(signal); }
        catch (error) { for (const resource of decoded || []) resource.destroy(); throw error; }
      }
      if (!resources?.[0]) throw new Error("KTX2 decoder returned no frame");
      const resource = resources[0];
      for (const extra of resources.slice(1)) extra.destroy();
      const compressed = resource instanceof pixi.CompressedTextureResource;
      const basisFormat = resources.basisFormat;
      let accounting;
      try { accounting = resourceBytes(resource, compressed); }
      catch (error) { resource.destroy(); throw error; }
      const texture = makeTexture(resource, {
        mipmap: compressed && resource.levels > 1 ? pixi.MIPMAP_MODES.ON_MANUAL : pixi.MIPMAP_MODES.OFF,
        alphaMode: pixi.ALPHA_MODES.NO_PREMULTIPLIED_ALPHA,
        type: shim.BASIS_FORMAT_TO_TYPE[basisFormat],
        format: basisFormat === shim.BASIS_FORMATS.cTFRGBA32 ? pixi.FORMATS.RGBA : pixi.FORMATS.RGB,
      });
      if (cacheWrite && compressed && basisFormat === 6) cacheWrites.set(texture, cacheWrite);
      return { texture, ...accounting };
    }

    function loadRaster(url, signal) {
      return new Promise((resolve, reject) => {
        const image = createImage();
        let settled = false;
        const clearSource = () => { image.onload = image.onerror = null; image.src = ""; };
        const finish = (error, result) => {
          if (settled) return;
          settled = true;
          signal.removeEventListener("abort", abort);
          image.onload = image.onerror = null;
          if (error) { clearSource(); reject(error); } else resolve(result);
        };
        const abort = () => finish(abortError());
        if (signal.aborted || disposed) { abort(); return; }
        signal.addEventListener("abort", abort, { once: true });
        image.decoding = "async";
        if (/^https?:\/\//i.test(url)) image.crossOrigin = "anonymous";
        image.onerror = () => finish(new Error("Frame image decode failed"));
        image.onload = async () => {
          try {
            // Chromium may reject decode() after a successful file:// onload.
            if (typeof image.decode === "function") { try { await image.decode(); } catch (_) {} }
            if (settled) return;
            check(signal);
            const resource = new pixi.ImageResource(image, { autoLoad: false, createBitmap: false });
            const bytes = resource.width * resource.height * 4;
            // Inherit the same BaseTexture defaults as the legacy Texture.from
            // path, including mipmap filtering. CPU counts the decoded image;
            // GPU also counts the mip levels Pixi generates for this context.
            // Retain the Image for Pixi's ordinary context-restoration upload.
            const texture = makeTexture(resource, undefined, clearSource);
            finish(null, { texture, cpuBytes: bytes, gpuBytes: rasterGpuBytes(texture.baseTexture) });
          } catch (error) { finish(error); }
        };
        try { image.src = url; }
        catch (error) { finish(error); }
      });
    }

    function rasterGpuBytes(base) {
      let width = base.realWidth, height = base.realHeight;
      let bytes = width * height * 4;
      // Match TextureSystem.updateTextureStyle/setStyle: manual mips are not
      // generated, POW2 requires POT, and WebGL1 suppresses every NPOT chain.
      const generatesMips = base.mipmap >= 1 && base.mipmap !== pixi.MIPMAP_MODES.ON_MANUAL
        && (base.isPowerOfTwo || (base.mipmap !== pixi.MIPMAP_MODES.POW2 && renderer.context.webGLVersion === 2));
      if (generatesMips) while (width > 1 || height > 1) {
        width = Math.max(1, Math.floor(width / 2));
        height = Math.max(1, Math.floor(height / 2));
        bytes += width * height * 4;
      }
      return bytes;
    }

    async function load(url, signal) {
      if (disposed || signal?.aborted) throw abortError();
      const controller = new AbortController();
      const cancel = () => controller.abort();
      signal?.addEventListener("abort", cancel, { once: true });
      loads.add(controller);
      let result;
      try {
        if (/\.ktx2(?:[?#]|$)/i.test(url)) {
          try { result = await loadCompressed(url, controller.signal); }
          catch (error) {
            check(controller.signal);
            root.console.warn("[FrameTextureBackend] KTX2 load failed, trying PNG fallback:", String(url).slice(0, 160), error);
            result = await loadRaster(pngFallbackUrl(url), controller.signal);
          }
        }
        else result = await loadRaster(url, controller.signal);
        check(controller.signal);
        return result;
      } finally {
        if (controller.signal.aborted && result) destroy(result.texture);
        loads.delete(controller);
        signal?.removeEventListener("abort", cancel);
      }
    }

    function upload(texture) {
      if (disposed || !owned.has(texture)) return Promise.reject(abortError());
      if (uploads.has(texture)) return uploads.get(texture).promise;
      const base = texture.baseTexture, gpu = base?._glTextures[renderer.CONTEXT_UID];
      if (!lost() && gpu && gpu.dirtyId === base.dirtyId) { persist(texture); return Promise.resolve(); }
      const job = {};
      job.promise = new Promise((resolve, reject) => { job.resolve = resolve; job.reject = reject; });
      uploads.set(texture, job);
      schedule();
      return job.promise;
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      for (const controller of loads) controller.abort();
      cache?.dispose();
      for (const texture of [...owned.keys()]) destroy(texture);
      stopTicker();
      canvas?.removeEventListener("webglcontextlost", onLost);
      canvas?.removeEventListener("webglcontextrestored", onRestored);
    }

    return { load, upload, destroy, now, dispose, stats: () => ({ ...counters, ...cache?.stats() }) };
  }

  return { createFrameTextureBackend };
});
