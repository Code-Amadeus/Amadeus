import { Module } from '../module.js';
import { isError } from '../errors/index.js';
const compressBound = (size) => {
    const bound = Module['_ZSTD_compressBound'];
    return bound(size);
};
export const createCCtx = () => {
    return Module['_ZSTD_createCCtx']();
};
export const freeCCtx = (cctx) => {
    return Module['_ZSTD_freeCCtx'](cctx);
};
export const compressUsingDict = (cctx, buf, dict, level) => {
    const bound = compressBound(buf.byteLength);
    const malloc = Module['_malloc'];
    const compressed = malloc(bound);
    const src = malloc(buf.byteLength);
    Module.HEAP8.set(buf, src);
    // Setup dict
    const pdict = malloc(dict.byteLength);
    Module.HEAP8.set(dict, pdict);
    const free = Module['_free'];
    try {
        /*
          @See https://zstd.docsforge.com/dev/api/ZSTD_compress_usingDict/
          size_t ZSTD_compress_usingDict(ZSTD_CCtx* cctx,
                             void* dst, size_t dstCapacity,
                             const void* src, size_t srcSize,
                             const void* dict, size_t dictSize,
                             int compressionLevel)
        */
        const _compress = Module['_ZSTD_compress_usingDict'];
        const sizeOrError = _compress(cctx, compressed, bound, src, buf.byteLength, pdict, dict.byteLength, level !== null && level !== void 0 ? level : 3);
        if (isError(sizeOrError)) {
            throw new Error(`Failed to compress with code ${sizeOrError}`);
        }
        // // Copy buffer
        // // Uint8Array.prototype.slice() return copied buffer.
        const data = new Uint8Array(Module.HEAPU8.buffer, compressed, sizeOrError).slice();
        free(compressed, bound);
        free(src, buf.byteLength);
        free(pdict, dict.byteLength);
        return data;
    }
    catch (e) {
        free(compressed, bound);
        free(src, buf.byteLength);
        free(pdict, dict.byteLength);
        throw e;
    }
};