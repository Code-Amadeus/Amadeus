// Probe-only IVF reader. A packet carries one visible VP9 frame in our export.
export function parseIvf(buffer, index) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const ascii = (start, end) => String.fromCharCode(...bytes.subarray(start, end));
  if (bytes.length < 32 || ascii(0, 4) !== 'DKIF' || view.getUint16(4, true) !== 0
      || view.getUint16(6, true) !== 32 || ascii(8, 12) !== 'VP90') throw Error('Expected VP9 IVF');
  const width = view.getUint16(12, true), height = view.getUint16(14, true);
  const frames = [];
  for (let offset = 32; offset < bytes.length;) {
    if (offset + 12 > bytes.length) throw Error('Truncated IVF frame header');
    const size = view.getUint32(offset, true);
    if (!size || offset + 12 + size > bytes.length) throw Error('Truncated IVF payload');
    frames.push({ offset: offset + 12, size, data: bytes.subarray(offset + 12, offset + 12 + size) });
    offset += 12 + size;
  }
  if (!frames.length || frames.length !== view.getUint32(24, true)
      || frames.length !== index.length) throw Error('IVF/index frame count mismatch');
  for (let i = 0; i < frames.length; i++) {
    const entry = index[i], frame = frames[i];
    if (entry.sourceIndex !== i || entry.offset !== frame.offset || entry.size !== frame.size
        || typeof entry.key !== 'boolean') throw Error('IVF/index identity mismatch');
    frame.key = entry.key;
    frame.sourceIndex = i;
  }
  if (!frames[0].key) throw Error('First frame must be a key frame');
  return { width, height, frames };
}

export function keyframeAtOrBefore(frames, index) {
  if (!Number.isInteger(index) || index < 0 || index >= frames.length) throw Error('Invalid source index');
  for (let i = index; i >= 0; i--) if (frames[i].key) return i;
  throw Error('No recovery key frame');
}
