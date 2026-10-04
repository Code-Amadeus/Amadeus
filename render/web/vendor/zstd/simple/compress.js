import { Module } from '../module.js';
import { isError } from '../errors/index.js';
const compressBound = (size) => {
    const bound = Module['_ZSTD_compressBound'];
    return bound(size);
};
export const compress = (buf, level) => {
    const bound = compressBound(buf.byteLength);
    const malloc = Module['_malloc'];
    const compressed = malloc(bound);
    const src = malloc(buf.byteLength);
    Module.HEAP8.set(buf, src);
    const free = Module['_free'];
    try {
        /*
          @See https://zstd.docsforge.com/dev/api/ZSTD_compress/
          size_t ZSTD_compress( void* dst, size_t dstCapacity, const void* src, size_t srcSize, int compressionLevel);
          Compresses `src` content as a single zstd compressed frame into already allocated `dst`.
          Hint : compression runs faster if `dstCapacity` >=  `ZSTD_compressBound(srcSize)`.
          @return : compressed size written into `dst` (<= `dstCapacity),
                    or an error code if it fails (which can be tested using ZSTD_isError()).
        */
        const _compress = Module['_ZSTD_compress'];
        const sizeOrError = _compress(compressed, bound, src, buf.byteLength, level !== null && level !== void 0 ? level : 3);
        if (isError(sizeOrError)) {
            throw new Error(`Failed to compress with code ${sizeOrError}`);
        }
        // // Copy buffer
        // // Uint8Array.prototype.slice() return copied buffer.
        const data = new Uint8Array(Module.HEAPU8.buffer, compressed, sizeOrError).slice();
        free(compressed, bound);
        free(src, buf.byteLength);
        return data;
    }
    catch (e) {
        free(compressed, bound);
        free(src, buf.byteLength);
        throw e;
    }
};