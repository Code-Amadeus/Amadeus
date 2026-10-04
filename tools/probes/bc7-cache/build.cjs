// Experimental derived cache. Never rewrites the character pack or an existing cache.
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const zlib = require('node:zlib');
const { Worker, isMainThread, parentPort, workerData } = require('node:worker_threads');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

async function atomic(file, bytes) {
  const temporary = file + '.partial';
  const handle = await fs.open(temporary, 'wx');
  try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
  await fs.rename(temporary, file);
}

async function derive({ root, output, files }) {
  const vendor = path.join(root, 'render/web/vendor');
  const pack = path.join(root, 'assets/spriteforge/runtime/kurisu');
  const B = await require(path.join(vendor, 'basis_transcoder.js'))({
    wasmBinary: await fs.readFile(path.join(vendor, 'basis_transcoder.wasm')) });
  B.initializeBasis();
  let transcodeMs = 0, compressMs = 0;
  const entries = {};
  for (const rel of files) {
    const source = await fs.readFile(path.join(pack, rel));
    const texture = new B.KTX2File(new Uint8Array(source));
    let raw, width, height;
    try {
      if (!texture.isValid() || texture.getLevels() !== 1 || texture.getLayers() > 1 || texture.getFaces() !== 1)
        throw Error('Experiment supports one-level 2D textures only: ' + rel);
      width = texture.getWidth(); height = texture.getHeight();
      const start = performance.now();
      if (!texture.startTranscoding()) throw Error('Transcoder initialization failed: ' + rel);
      raw = new Uint8Array(texture.getImageTranscodedSizeInBytes(0, 0, 0, 6));
      if (!texture.transcodeImage(raw, 0, 0, 0, 6, 0, -1, -1)) throw Error('BC7 transcode failed: ' + rel);
      transcodeMs += performance.now() - start;
    } finally { texture.close(); texture.delete(); }
    if (raw.byteLength !== Math.ceil(width / 4) * Math.ceil(height / 4) * 16) throw Error('Unexpected BC7 size');
    const start = performance.now();
    const compressed = zlib.zstdCompressSync(raw, { params: { [zlib.constants.ZSTD_c_compressionLevel]: 15 } });
    compressMs += performance.now() - start;
    if (!Buffer.from(raw).equals(zlib.zstdDecompressSync(compressed))) throw Error('Cache roundtrip mismatch');
    const sourceSha256 = hash(source), compressedSha256 = hash(compressed), rawSha256 = hash(raw);
    const file = sourceSha256 + '-' + hash(rel).slice(0, 12) + '.bc7.zst';
    await atomic(path.join(output, file), compressed);
    entries[rel] = { file, width, height, sourceSha256, compressedSha256, rawSha256,
      sourceBytes: source.byteLength, compressedBytes: compressed.byteLength, rawBytes: raw.byteLength };
    if (Object.keys(entries).length % 250 === 0) parentPort?.postMessage({ progress: Object.keys(entries).length });
  }
  return { entries, transcodeMs, compressMs };
}

async function main() {
  const root = path.resolve(process.argv[2]), output = path.resolve(process.argv[3]);
  await fs.mkdir(output); // Explicitly refuse reuse, partial or complete.
  const started = performance.now(), cpu = process.cpuUsage();
  const pack = path.join(root, 'assets/spriteforge/runtime/kurisu');
  const manifestBytes = await fs.readFile(path.join(pack, 'runtime_manifest.json'));
  const manifest = JSON.parse(manifestBytes);
  async function collect(directory, prefix = '') {
    const result = [];
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw Error('Refusing linked assets');
      const rel = prefix + entry.name;
      if (entry.isDirectory()) result.push(...await collect(path.join(directory, entry.name), rel + '/'));
      else if (entry.name.endsWith('.ktx2')) result.push(rel);
    }
    return result;
  }
  const files = (await collect(pack)).sort();
  const runs = await Promise.all([0, 1].map(worker => new Promise((resolve, reject) => {
    const w = new Worker(__filename, { workerData: { root, output, files: files.filter((_, i) => i % 2 === worker) } });
    w.on('error', reject);
    w.on('message', message => message.entries ? resolve(message) : console.log(JSON.stringify({ worker, ...message })));
    w.on('exit', code => { if (code) reject(Error('Cache builder worker exit ' + code)); });
  })));
  const entries = Object.assign({}, ...runs.map(run => run.entries));
  const index = { schema: 'amadeus.bc7-experiment.v1', basisFormat: 6, internalFormat: 36492,
    compression: 'zstd-15', pack: { id: manifest.id, version: manifest.version, manifestSha256: hash(manifestBytes) },
    transcoder: {}, entries };
  for (const name of ['basis_transcoder.js', 'basis_transcoder.wasm'])
    index.transcoder[name] = hash(await fs.readFile(path.join(root, 'render/web/vendor', name)));
  await atomic(path.join(output, 'index.json'), JSON.stringify(index));
  const used = process.cpuUsage(cpu);
  const report = { complete: true, frames: files.length, wallSeconds: (performance.now() - started) / 1000,
    cpuSeconds: (used.user + used.system) / 1e6, workers: 2,
    transcodeMs: runs.reduce((n, r) => n + r.transcodeMs, 0), compressMs: runs.reduce((n, r) => n + r.compressMs, 0),
    sourceBytes: Object.values(entries).reduce((n, e) => n + e.sourceBytes, 0),
    compressedBytes: Object.values(entries).reduce((n, e) => n + e.compressedBytes, 0),
    rawBytes: Object.values(entries).reduce((n, e) => n + e.rawBytes, 0), indexSha256: hash(JSON.stringify(index)) };
  await atomic(path.join(output, 'build-summary.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}
if (isMainThread) main().catch(error => { console.error(error); process.exitCode = 1; });
else derive(workerData).then(result => parentPort.postMessage(result)).catch(error => { throw error; });
