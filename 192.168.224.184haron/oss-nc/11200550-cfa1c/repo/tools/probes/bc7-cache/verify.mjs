import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

// Validate source identity before timing, once per session. No mtime-only keys.
export async function verifyCache(root, directory) {
  const raw = await fs.readFile(path.join(directory, 'index.json'));
  const index = JSON.parse(raw);
  if (index.schema !== 'amadeus.bc7-experiment.v1' || index.basisFormat !== 6 || index.internalFormat !== 36492)
    throw Error('Unsupported experimental cache');
  const pack = path.join(root, 'assets/spriteforge/runtime/kurisu');
  if (hash(await fs.readFile(path.join(pack, 'runtime_manifest.json'))) !== index.pack.manifestSha256)
    throw Error('Cache manifest is stale');
  for (const [name, expected] of Object.entries(index.transcoder)) {
    if (!['basis_transcoder.js', 'basis_transcoder.wasm'].includes(name)) throw Error('Invalid transcoder identity');
    if (hash(await fs.readFile(path.join(root, 'render/web/vendor', name))) !== expected) throw Error('Cache transcoder is stale');
  }
  for (const [rel, entry] of Object.entries(index.entries)) {
    const source = path.resolve(pack, rel);
    if (!source.startsWith(pack + path.sep) || !/^[a-f0-9]{64}-[a-f0-9]{12}\.bc7\.zst$/.test(entry.file))
      throw Error('Invalid derived cache path');
    if (hash(await fs.readFile(source)) !== entry.sourceSha256) throw Error('Cache source is stale: ' + rel);
  }
  return { index, sha256: hash(raw), entries: Object.keys(index.entries).length };
}
