// Usage: node tools/probes/benchmark_frame_store.cjs BASELINE_STORE_JS CANDIDATE_STORE_JS
// Synthetic warm-loop API cost only; no real network, decoding, GPU or renderer.
const { performance } = require('node:perf_hooks');
const path = require('node:path');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function measure(modulePath, count) {
  const { createFrameStore } = require(path.resolve(modulePath));
  let now = 0;
  const store = createFrameStore({ budgetBytes: 2000, maxInFlight: 3, backend: {
    now: () => now,
    load: async url => ({ texture: { url }, cpuBytes: 1, gpuBytes: 1 }),
    upload: async () => {}, destroy() {},
  } });
  const urls = Array.from({ length: count }, (_, i) => `frame-${i}`);
  store.replaceViews('all', urls);
  store.replaceDemand('warm', urls.slice(0, 883).map(url => ({ url, priority: 65 })));
  await flush();
  const samples = [];
  for (let i = 0; i < 3600; i++) {
    now += 16.67;
    const requests = Array.from({ length: 31 }, (_, k) => ({
      url: urls[(i + k) % 120], priority: 100, deadline: now + k * 16.67,
    }));
    const start = performance.now();
    store.replaceDemand('current', requests);
    store.replacePins('display', [urls[i % 120]]);
    // Include the scheduled pump without adding a setImmediate wait to timings.
    await Promise.resolve();
    if (i >= 600) samples.push(performance.now() - start);
  }
  samples.sort((a, b) => a - b);
  const result = { entries: count, p50Ms: samples[Math.floor(samples.length * .5)],
    p99Ms: samples[Math.floor(samples.length * .99)], maxMs: samples.at(-1), framesMeasured: samples.length };
  store.destroy();
  return result;
}

(async () => {
  if (process.argv.length !== 4) throw Error('Expected baseline and candidate frame_store.js paths');
  const results = [];
  for (const [name, modulePath] of [['baseline', process.argv[2]], ['candidate', process.argv[3]]]) {
    for (const count of [7552, 15104]) results.push({ name, ...await measure(modulePath, count) });
  }
  console.log(JSON.stringify({
    scope: 'Synthetic warm resident loop; identical current-demand/display-pin calls and scheduled pump; no real I/O, GPU or renderer; 600 warmup + 3000 measured frames.',
    results,
  }, null, 2));
})().catch(error => { console.error(error); process.exitCode = 1; });
