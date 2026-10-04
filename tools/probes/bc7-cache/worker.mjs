import { decodeFrame } from './decode.mjs';
self.onmessage = async ({ data: { id, bytes, entry } }) => {
  try {
    const result = await decodeFrame(bytes, entry);
    self.postMessage({ id, ...result }, [result.buffer]);
  } catch (error) { self.postMessage({ id, error: error.message }); }
};
