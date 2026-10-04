import { decodePayload } from '../../../render/web/texture_cache_codec.mjs';

export async function decodeFrame(bytes, entry) {
  if (!Number.isInteger(entry.width) || !Number.isInteger(entry.height)
    || entry.width < 1 || entry.height < 1 || entry.width > 4096 || entry.height > 4096
    || entry.rawBytes !== Math.ceil(entry.width / 4) * Math.ceil(entry.height / 4) * 16
    || bytes.byteLength !== entry.compressedBytes) throw Error('Invalid cached frame dimensions or size');
  return decodePayload(new Uint8Array(bytes), { ...entry,
    width: Math.ceil(entry.width / 4) * 4, height: Math.ceil(entry.height / 4) * 4 });
}
