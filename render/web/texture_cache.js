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

  function createTextureCache({ renderer, pixi, fetch: fetchAsset = root.fetch?.bind(root),
    Worker: WorkerClass = root.Worker, location = root.location, worker = workerUrl } = {}) {
    const pending = new Map(), workers = [], writing = new Set(), invalidUrls = new Set();
    const controllers = new Set();
    const counters = { cacheHits: 0, cacheMisses: 0, cacheReadFailures: 0, cacheWrites: 0,
      cacheWriteFailures: 0, cacheWriteSkipped: 0, cachePendingWrites: 0, cacheCompressedBytes: 0 };
    let disposed = false, failed = false, writesDisabled = false, sequence = 0, nextDecode = 0;
    let encoder;
    function makeWorker() {
      const instance = new WorkerClass(worker, { type: 'module' });
      instance.onmessage = ({ data }) => {
        const job = pending.get(data.id);
        if (!job) return;
        pending.delete(data.id); clearTimeout(job.timer);
        data.error ? job.reject(Error(data.error)) : job.resolve(data);
      };
      instance.onerror = error => {
        failed = true;
        for (const [id, job] of pending) if (job.worker === instance) {
          clearTimeout(job.timer); pending.delete(id); job.reject(Error(error.message || 'Cache worker unavailable'));
        }
      };
      return instance;
    }
    function run(kind, data) {
      if (disposed) return Promise.reject(cancelled());
      if (failed) return Promise.reject(Error('Texture cache worker unavailable'));
      let instance;
      try {
        if (kind === 'encode') instance = encoder ||= makeWorker();
        else {
          if (workers.length < 2) workers.push(makeWorker());
          instance = workers[nextDecode++ % workers.length];
        }
      } catch (error) { failed = true; return Promise.reject(error); }
      return new Promise((resolve, reject) => {
        const id = ++sequence;
        const timer = setTimeout(() => { pending.delete(id); failed = true; reject(Error('Texture cache worker timed out')); }, 30000);
        pending.set(id, { resolve, reject, timer, worker: instance });
        try { instance.postMessage({ id, kind, ...data }, [data.buffer]); }
        catch (error) { clearTimeout(timer); pending.delete(id); failed = true; reject(error); }
      });
    }
    function canUse(url) {
      if (disposed || failed || !WorkerClass || !location || !fetchAsset) return false;
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
      if (disposed || failed || writesDisabled || !key || !token || writing.has(key)) return;
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
          if (response.status === 403 || response.status === 503) writesDisabled = true;
          if (counters.cacheWriteFailures === 0) root.console?.warn('[TextureCache] Rejection detail:', await response.text?.());
          throw Error('Cache write rejected: ' + response.status);
        }
        counters.cacheWrites++; invalidUrls.delete(url);
      }).catch(error => {
        if (disposed) return;
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
      stats: () => ({ ...counters }) };
  }
  return { createTextureCache, MIME };
});
