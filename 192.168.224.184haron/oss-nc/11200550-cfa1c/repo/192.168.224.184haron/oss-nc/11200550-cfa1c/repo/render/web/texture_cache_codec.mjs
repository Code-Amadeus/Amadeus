import { init, compress } from './vendor/zstd/index.web.js';
import { Module } from './vendor/zstd/module.js';

export const MAX_RAW_BYTES = 16 * 1024 * 1024;
const KEY = /^[a-f0-9]{64}$/;
let initialized;
const ready = () => initialized ||= Promise.resolve().then(() => init()).catch(error => {
  throw Object.assign(new Error('Texture cache codec unavailable', { cause: error }), { cacheTransient: true });
});
export const sha256 = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)),
  byte => byte.toString(16).padStart(2, '0')).join('');

function dimensions(meta) {
  return Number.isInteger(meta.width) && Number.isInteger(meta.height)
    && meta.width > 0 && meta.height > 0 && meta.width <= 4096 && meta.height <= 4096
    && meta.width % 4 === 0 && meta.height % 4 === 0 && meta.rawBytes === meta.width * meta.height;
}

export async function encodeFrame({ key, width, height, buffer }) {
  const meta = { version: 1, key, width, height, rawBytes: buffer.byteLength };
  if (!KEY.test(key) || !dimensions(meta)) throw Error('Unsupported cache frame');
  await ready();
  // Interactive derivation favors bounded CPU work over maximum disk compression.
  const payload = compress(new Uint8Array(buffer), 3);
  meta.rawSha256 = await sha256(buffer);
  meta.compressedSha256 = await sha256(payload);
  const header = new TextEncoder().encode(JSON.stringify(meta));
  const body = new Uint8Array(4 + header.length + payload.length);
  new DataView(body.buffer).setUint32(0, header.length, true);
  body.set(header, 4); body.set(payload, 4 + header.length);
  return { buffer: body.buffer };
}

export async function decodeFrame({ key, buffer }) {
  if (!KEY.test(key) || buffer.byteLength < 5 || buffer.byteLength > MAX_RAW_BYTES + 8192)
    throw Error('Invalid cache frame');
  const length = new DataView(buffer).getUint32(0, true);
  if (!length || length > 4096 || length + 4 >= buffer.byteLength) throw Error('Invalid cache header');
  const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, length)));
  if (meta.version !== 1 || meta.key !== key || !dimensions(meta) || !KEY.test(meta.rawSha256))
    throw Error('Cache identity mismatch');
  const payload = new Uint8Array(buffer, 4 + length);
  const { buffer: decoded } = await decodePayload(payload, meta);
  return { width: meta.width, height: meta.height, buffer: decoded };
}

// Both the product container and the historical prebuilt-cache probe use this
// bounded BC7 decoder. The caller owns its container/identity validation.
export async function decodePayload(payload, meta) {
  if (!dimensions(meta) || !payload.byteLength || payload.byteLength > MAX_RAW_BYTES + 8192)
    throw Error('Invalid BC7 dimensions or size');
  const validateStart = performance.now();
  if (await sha256(payload) !== meta.compressedSha256) throw Error('Cache checksum mismatch');
  await ready();
  const inflateStart = performance.now();
  // Allocate from the validated dimensions, never an untrusted zstd frame-size header.
  const source = Module._malloc(payload.length), target = Module._malloc(meta.rawBytes);
  let raw;
  try {
    Module.HEAPU8.set(payload, source);
    const size = Module._ZSTD_decompress(target, meta.rawBytes, source, payload.length);
    if (Module._ZSTD_isError(size) || size !== meta.rawBytes) throw Error('Invalid BC7 payload');
    raw = Module.HEAPU8.slice(target, target + size);
  } finally { Module._free(source); Module._free(target); }
  const inflateMs = performance.now() - inflateStart;
  if (await sha256(raw) !== meta.rawSha256) throw Error('BC7 checksum mismatch');
  return { buffer: raw.buffer, inflateMs, validateMs: performance.now() - validateStart - inflateMs };
}
