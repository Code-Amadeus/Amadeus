// Probe-only seam: replace fetched UASTC bytes with verified BC7 resources.
// The renderer, FrameStore budget/accounting, upload queue and lifecycle stay intact.
export function installBc7Cache(index) {
  const stats = window.__bc7Cache = { installed: false, hits: 0, misses: 0, failures: 0,
    compressedBytes: 0, inflateMs: 0, validateMs: 0, unsupported: 0 };
  const resources = new WeakMap(), workers = [], pending = new Map();
  let sequence = 0, nextWorker = 0;
  function decode(bytes, entry) {
    if (!workers.length) for (let i = 0; i < 2; i++) {
      const worker = new Worker('/tools/probes/bc7-cache/worker.mjs', { type: 'module' });
      worker.onmessage = ({ data }) => {
        const job = pending.get(data.id);
        if (!job) return;
        pending.delete(data.id);
        data.error ? job.reject(Error(data.error)) : job.resolve(data);
      };
      worker.onerror = event => {
        for (const [id, job] of pending) if (job.worker === worker) {
          pending.delete(id); job.reject(Error(event.message || 'Cache worker failed'));
        }
      };
      workers.push(worker);
    }
    return new Promise((resolve, reject) => {
      const id = ++sequence, worker = workers[nextWorker++ % workers.length];
      pending.set(id, { resolve, reject, worker });
      worker.postMessage({ id, bytes, entry }, [bytes]);
    });
  }
  const install = () => {
    const pixi = window.PIXI, shim = window.PixiBasisKtx2Shim;
    if (stats.installed || !shim?.KTX2Parser || !pixi?.settings?.ADAPTER) return;
    const parser = shim.KTX2Parser, transcode = parser.transcode;
    parser.transcode = function (bytes, ...args) {
      const entry = resources.get(bytes);
      if (!entry) return transcode.call(this, bytes, ...args);
      resources.delete(bytes);
      const result = [new pixi.CompressedTextureResource(null, { format: index.internalFormat,
        width: (entry.width + 3) & ~3, height: (entry.height + 3) & ~3, levels: 1,
        levelBuffers: [{ levelID: 0, levelWidth: (entry.width + 3) & ~3, levelHeight: (entry.height + 3) & ~3,
          levelBuffer: new Uint8Array(bytes) }] })];
      result.basisFormat = index.basisFormat;
      return Promise.resolve(result);
    };
    const adapter = pixi.settings.ADAPTER, fetchAsset = adapter.fetch;
    adapter.fetch = async function (url, init) {
      const address = new URL(String(url), location.href);
      const prefix = ['/spriteforge/', '/assets/spriteforge/runtime/kurisu/']
        .find(value => address.pathname.startsWith(value));
      const rel = prefix ? decodeURIComponent(address.pathname.slice(prefix.length)) : null;
      const entry = index.entries[rel];
      if (!entry) {
        if (/\.ktx2$/.test(address.pathname)) stats.misses++;
        return fetchAsset.call(this, url, init);
      }
      const gl = window.wallpaperApp?.scene?.app?.renderer?.gl;
      if (!gl?.getExtension('EXT_texture_compression_bptc')) {
        stats.unsupported++;
        return fetchAsset.call(this, url, init);
      }
      // One owning fallback for missing/corrupt derived data. Abort remains abort.
      try {
        const response = await fetchAsset.call(this, '/__bc7_cache/' + entry.file, init);
        if (!response.ok) throw Error('Cache fetch failed ' + response.status);
        const result = await decode(await response.arrayBuffer(), entry);
        if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
        resources.set(result.buffer, entry);
        stats.hits++; stats.compressedBytes += entry.compressedBytes;
        stats.inflateMs += result.inflateMs; stats.validateMs += result.validateMs;
        return { ok: true, arrayBuffer: async () => result.buffer };
      } catch (error) {
        if (init?.signal?.aborted) throw error;
        stats.failures++;
        console.warn('[BC7 experiment] Derived cache failed; using source', String(error));
        return fetchAsset.call(this, url, init);
      }
    };
    stats.installed = true;
    document.removeEventListener('load', loaded, true);
  };
  const loaded = event => { if (event.target?.tagName === 'SCRIPT') install(); };
  document.addEventListener('load', loaded, true);
  install();
}
