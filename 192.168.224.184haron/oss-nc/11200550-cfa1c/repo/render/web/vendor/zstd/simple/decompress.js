import { Module } from '../module.js';
import { isError } from '../errors/index.js';
const getFrameContentSize = (src, size) => {
    const getSize = Module['_ZSTD_getFrameContentSize'];
    return getSize(src, size);
};
export const decompress = (buf, opts = { defaultHeapSize: 1024 * 1024 }) => {
    const malloc = Module['_malloc'];
    const src = malloc(buf.byteLength);
    Module.HEAP8.set(buf, src);
    const contentSize = getFrameContentSize(src, buf.byteLength);
    const size = contentSize === -1 ? opts.defaultHeapSize : contentSize;
    const free = Module['_free'];
    const heap = malloc(size);
    try {
        /*
          @See https://zstd.docsforge.com/dev/api/ZSTD_decompress/
          compressedSize : must be the exact size of some number of compressed and/or skippable frames.
          dstCapacity is an upper bound of originalSize to regenerate.
          If user cannot imply a maximum upper bound, it's better to use streaming mode to decompress data.
          @return: the number of bytes decompressed into dst (<= dstCapacity), or an errorCode if it fails (which can be tested using ZSTD_isError()).
        */
        const _decompress = Module['_ZSTD_decompress'];
        const sizeOrError = _decompress(heap, size, src, buf.byteLength);
        if (isError(sizeOrError)) {
            throw new Error(`Failed to compress with code ${sizeOrError}`);
        }
        // Copy buffer
        // Uint8Array.prototype.slice() return copied buffer.
        const data = new Uint8Array(Module.HEAPU8.buffer, heap, sizeOrError).slice();
        free(heap, size);
        free(src, buf.byteLength);
        return data;
    }
    catch (e) {
        free(heap, size);
        free(src, buf.byteLength);
        throw e;
    }
};