(function (root, factory) {
  const api = factory(root);
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.FrameTextureCache = api;
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';
  const MIME = 'application/x-amadeus-bc7';
  const script = root.document?.currentScript?.src;
  const workerUrl = script ? new URL('./texture_cache_worker.mjs', script).href : './texture_cache_worker.mjs';
  const cancelled = () => Object.assign(Error('Texture cache disposed'), { name: 'AbortError' });
  const unavailable = message => Object.assign(Error(message), { cacheTransient: true });
  const COOLDOWN_MS = 30000;

  function createTextureCache({ renderer, pixi, fetch: fetchAsset = root.fetch?.bind(root),
    Worker: WorkerClass = root.Worker, location = root.location, worker = workerUrl } = {}) {
    const pending = new Map(), workers = [], writing = new Set(), invalidUrls = new Set();
    const controllers = new Set();
    const counters = { cacheHits: 0, cacheMisses: 0, cacheReadFailures: 0, cacheWrites: 0,
      cacheWriteFailures: 0, cacheWriteSkipped: 0, cachePendingWrites: 0, cacheCompressedBytes: 0,
      cacheWorkerRestarts: 0, cacheWorkerFailures: 0 };
    const health = { decode: { failures: 0, retryAt: 0 }, encode: { failures: 0, retryAt: 0 } };
    let disposed = false, writesDisabled = false, writeRetryAt = 0, sequence = 0, nextDecode = 0;
    let encoder;
    function workerFailed(kind, instance, message) {
      if (disposed) return;
      if (instance) {
        // One incident per worker, even if several queued jobs time out together.
        if (instance === encoder) encoder = undefined;
        else {
          const index = workers.indexOf(instance);
          if (index < 0) return;
          workers.splice(index, 1);
        }
        instance.onmessage = instance.onerror = null;
        instance.terminate(); counters.cacheWorkerRestarts++;
      }
      const state = health[kind];
      counters.cacheWorkerFailures++;
      if (++state.failures >= 3) state.retryAt = Date.now() + COOLDOWN_MS;
      for (const [id, job] of pending) if (job.worker === instance) {
        clearTimeout(job.timer); pending.delete(id); job.reject(unavailable(message));
      }
    }
    function makeWorker(kind) {
      const instance = new WorkerClass(worker, { type: 'module' });
      instance.onmessage = ({ data }) => {
        const job = pending.get(data.id);
        if (!job || job.worker !== instance) return;
        if (data.cacheTransient) { workerFailed(kind, instance, data.error); return; }
        pending.delete(data.id); clearTimeout(job.timer);
        health[kind].failures = 0; health[kind].retryAt = 0;
        data.error ? job.reject(Error(data.error)) : job.resolve(data);
      };
      instance.onerror = error => workerFailed(kind, instance, error.message || 'Cache worker unavailable');
      return instance;
    }
    function run(kind, data) {
      if (disposed) return Promise.reject(cancelled());
      if (Date.now() < health[kind].retryAt) return Promise.reject(unavailable('Cache worker cooling down'));
      let instance;
      try {
        if (kind === 'encode') instance = encoder ||= makeWorker(kind);
        else {
          if (workers.length < 2) workers.push(makeWorker(kind));
          instance = workers[nextDecode++ % workers.length];
        }
      } catch (error) {
        workerFailed(kind, null, error.message);
        return Promise.reject(unavailable(error.message));
      }
      return new Promise((resolve, reject) => {
        const id = ++sequence;
        const timer = setTimeout(() => workerFailed(kind, instance, 'Texture cache worker timed out'), 30000);
        pending.set(id, { resolve, reject, timer, worker: instance });
        try { instance.postMessage({ id, kind, ...data }, [data.buffer]); }
        catch (error) { workerFailed(kind, instance, error.message); }
      });
    }
    function canUse(url) {
      if (disposed || Date.now() < health.decode.retryAt || !WorkerClass || !location || !fetchAsset) return false;
      try {
        const address = new URL(url, location.href);
        return ['http:', 'https:'].includes(location.protocol) && address.origin === location.origin
          && Boolean(renderer.gl?.getExtension('EXT_texture_compression_bptc'));
      } catch (_) { return false; }
    }
    function sourceUrl(url) {
      const address = new URL(url, location.href);
      address.searchParams.set('source', '1');
      return address.href;
    }
    async function decode(buffer, key) {
      const bytes = buffer.byteLength;
      const frame = await run('decode', { buffer, key });
      const resource = new pixi.CompressedTextureResource(null, { format: 36492,
        width: frame.width, height: frame.height, levels: 1,
        levelBuffers: [{ levelID: 0, levelWidth: frame.width, levelHeight: frame.height,
          levelBuffer: new Uint8Array(frame.buffer) }] });
      counters.cacheHits++; counters.cacheCompressedBytes += bytes;
      return resource;
    }
    function save({ url, key, token }, resource) {
      if (disposed || writesDisabled || Date.now() < Math.max(writeRetryAt, health.encode.retryAt)
        || !key || !token || writing.has(key)) return;
      const level = resource?._levelBuffers?.[0];
      if (writing.size >= 2 || resource.format !== 36492 || resource.levels !== 1
        || !level || level.levelBuffer.byteLength > 16 * 1024 * 1024) {
        counters.cacheWriteSkipped++; return;
      }
      writing.add(key); counters.cachePendingWrites = writing.size;
      // Copy only admitted jobs. Never transfer/detach the store's restoration copy.
      const buffer = level.levelBuffer.slice().buffer;
      const controller = new AbortController(); controllers.add(controller);
      run('encode', { key, width: resource.width, height: resource.height, buffer }).then(async result => {
        if (disposed) throw cancelled();
        const response = await fetchAsset(new URL('/_texture-cache/' + key, location.href).href,
          { method: 'POST', headers: { 'Content-Type': MIME, 'X-Amadeus-Cache-Token': token },
            body: result.buffer, signal: controller.signal });
        if (!response.ok) {
          if (response.status === 403) writesDisabled = true;
          if (counters.cacheWriteFailures === 0) root.console?.warn('[TextureCache] Rejection detail:', await response.text?.());
          throw Error('Cache write rejected: ' + response.status);
        }
        counters.cacheWrites++; writeRetryAt = 0; invalidUrls.delete(url);
      }).catch(error => {
        if (disposed) return;
        // No autonomous retries or retained payload queue. A later playback load
        // can repopulate the cache once a transient disk/network fault clears.
        writeRetryAt = Date.now() + COOLDOWN_MS;
        counters.cacheWriteFailures++;
        if (counters.cacheWriteFailures === 1) root.console?.warn('[TextureCache] Write unavailable; playback continues', error);
      }).finally(() => {
        writing.delete(key); controllers.delete(controller); counters.cachePendingWrites = writing.size;
      });
    }
    function dispose() {
      if (disposed) return;
      disposed = true;
      for (const controller of controllers) controller.abort();
      for (const job of pending.values()) { clearTimeout(job.timer); job.reject(cancelled()); }
      pending.clear();
      for (const instance of [...workers, ...(encoder ? [encoder] : [])]) instance.terminate();
    }
    return { canUse, decode, save, dispose, sourceUrl,
      requestUrl: url => invalidUrls.has(url) ? sourceUrl(url) : url,
      miss: () => { counters.cacheMisses++; },
      invalidate: url => { counters.cacheReadFailures++; invalidUrls.add(url); },
      stats: () => ({ ...counters, cacheWritesDisabled: writesDisabled,
        cacheWriteCoolingDown: Date.now() < Math.max(writeRetryAt, health.encode.retryAt),
        cacheDecodeCoolingDown: Date.now() < health.decode.retryAt }) };
  }
  return { createTextureCache, MIME };
});
