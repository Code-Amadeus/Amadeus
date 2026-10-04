import { ZSTDDecoder } from './vendor/zstddec.mjs';
const decoder = new ZSTDDecoder();
const ready = decoder.init();
const sha256 = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)),
  byte => byte.toString(16).padStart(2, '0')).join('');

export async function decodeFrame(bytes, entry) {
  if (!Number.isInteger(entry.width) || !Number.isInteger(entry.height)
    || entry.width < 1 || entry.height < 1 || entry.width > 4096 || entry.height > 4096
    || entry.rawBytes !== Math.ceil(entry.width / 4) * Math.ceil(entry.height / 4) * 16
    || bytes.byteLength !== entry.compressedBytes) throw Error('Invalid cached frame dimensions or size');
  const validateStart = performance.now();
  if (await sha256(bytes) !== entry.compressedSha256) throw Error('Cached frame checksum mismatch');
  await ready;
  const inflateStart = performance.now();
  const raw = decoder.decode(new Uint8Array(bytes), entry.rawBytes);
  const inflateMs = performance.now() - inflateStart;
  if (raw.byteLength !== entry.rawBytes || await sha256(raw) !== entry.rawSha256) throw Error('Decoded BC7 checksum mismatch');
  return { buffer: raw.buffer, inflateMs, validateMs: performance.now() - validateStart - inflateMs };
}
