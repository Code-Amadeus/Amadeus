import { encodeFrame, decodeFrame } from './texture_cache_codec.mjs';
let queue = Promise.resolve();
self.onmessage = ({ data: { id, kind, ...data } }) => {
  queue = queue.then(async () => {
    try {
      const result = await (kind === 'encode' ? encodeFrame(data) : decodeFrame(data));
      self.postMessage({ id, ...result }, [result.buffer]);
    } catch (error) { self.postMessage({ id, error: error.message }); }
  });
};
